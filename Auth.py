"""Login do painel: varios usuarios no banco (senha PBKDF2), 3 tipos de acesso e cookie de sessao assinado.

Tipos:
  admin    -> cria/gerencia logins, ve e edita as contas e posts de todos
  usuario  -> conecta contas e agenda posts, mas so enxerga o que e dele
  leitor   -> so consulta as contas e posts de um 'usuario' escolhido pelo admin
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from typing import Optional

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from database import get_db
from models import Conta, Usuario

log = logging.getLogger("auth")

# ADMIN_USER + (ADMIN_PASSWORD_HASH ou ADMIN_PASSWORD) so servem para criar o 1o administrador,
# quando o banco ainda nao tem nenhum login. Depois disso os logins sao criados no painel.
ADMIN_USER = os.getenv("ADMIN_USER")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
COOKIE = "sessao"
DURACAO = 7 * 24 * 3600  # 7 dias


def _segredo_da_sessao() -> str:
    segredo = os.getenv("SESSION_SECRET")
    if segredo:
        return segredo
    # Sem SESSION_SECRET o app ainda sobe: deriva um segredo estavel de outra chave secreta que ja existe.
    base = os.getenv("TOKEN_ENCRYPTION_KEY") or os.getenv("IG_APP_SECRET") or os.getenv("DATABASE_URL")
    if not base:
        raise RuntimeError("Defina SESSION_SECRET (ou TOKEN_ENCRYPTION_KEY) nas variaveis de ambiente.")
    log.warning("SESSION_SECRET nao definido: usando um segredo derivado. Defina SESSION_SECRET no Render.")
    return hmac.new(base.encode(), b"sessao-painel-v1", hashlib.sha256).hexdigest()


_serializer = URLSafeTimedSerializer(_segredo_da_sessao(), salt="sessao-painel")

PAPEIS = {"admin": "Administrador", "usuario": "Usuário", "leitor": "Somente leitura"}
RE_USUARIO = re.compile(r"^[a-z0-9._-]{3,32}$")
SENHA_MIN = 10


# ---------- senha ----------
def gerar_hash(senha: str, iteracoes: int = 600_000) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(salt), iteracoes).hex()
    return f"pbkdf2_sha256${iteracoes}${salt}${h}"


def verificar_senha(senha: str, armazenado: str) -> bool:
    try:
        _, it, salt, h = armazenado.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", senha.encode(), bytes.fromhex(salt), int(it)).hex()
        return hmac.compare_digest(calc, h)
    except Exception:
        return False


_HASH_FALSO = gerar_hash(secrets.token_hex(8))  # usado quando o usuario nao existe (evita vazar timing)


def checar_credenciais(db: Session, usuario: str, senha: str) -> Optional[Usuario]:
    u = db.query(Usuario).filter(Usuario.username == usuario.strip().lower()).first()
    ok = verificar_senha(senha, u.senha_hash if u else _HASH_FALSO)  # sempre calcula
    return u if (u and ok and u.ativo) else None


# ---------- sessao ----------
def criar_sessao(u: Usuario) -> str:
    return _serializer.dumps({"u": u.id, "v": u.sessao_versao})


def _ler_token(request: Request) -> Optional[dict]:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        return _serializer.loads(token, max_age=DURACAO)
    except (BadSignature, SignatureExpired):
        return None


def token_valido(request: Request) -> bool:
    """Checagem leve (so assinatura) usada para barrar as paginas estaticas."""
    return _ler_token(request) is not None


def usuario_logado(request: Request, db: Session) -> Optional[Usuario]:
    d = _ler_token(request)
    if not d:
        return None
    u = db.get(Usuario, d.get("u"))
    if not u or not u.ativo or u.sessao_versao != d.get("v"):
        return None  # desativado ou senha trocada: a sessao antiga morre na hora
    return u


def cookie_seguro(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def usuario_atual(request: Request, db: Session = Depends(get_db)) -> Usuario:
    u = usuario_logado(request, db)
    if not u:
        raise HTTPException(401, "Nao autenticado")
    return u


def so_admin(u: Usuario = Depends(usuario_atual)) -> Usuario:
    if u.papel != "admin":
        raise HTTPException(403, "Apenas administradores")
    return u


def pode_editar(u: Usuario = Depends(usuario_atual)) -> Usuario:
    if u.papel == "leitor":
        raise HTTPException(403, "Seu login e somente leitura")
    return u


# ---------- separacao dos dados por login ----------
def dono_efetivo(u: Usuario) -> Optional[int]:
    """None = ve tudo (admin). Caso contrario, id do dono dos dados que esse login enxerga."""
    if u.papel == "admin":
        return None
    if u.papel == "leitor":
        return u.dono_id or 0  # leitor sem dono nao ve nada (ids comecam em 1)
    return u.id


def filtrar_por_dono(q, u: Usuario, usuario_id: Optional[int] = None):
    dono = dono_efetivo(u)
    if dono is None:
        return q.filter(Conta.usuario_id == usuario_id) if usuario_id else q
    return q.filter(Conta.usuario_id == dono)


def pode_ver_conta(u: Usuario, conta: Conta) -> bool:
    dono = dono_efetivo(u)
    return dono is None or conta.usuario_id == dono


# ---------- 1o administrador + contas antigas ----------
def preparar_banco(db: Session):
    if db.query(Usuario).count() == 0:
        senha_hash = ADMIN_PASSWORD_HASH or (gerar_hash(ADMIN_PASSWORD) if ADMIN_PASSWORD and len(ADMIN_PASSWORD) >= SENHA_MIN else None)
        if ADMIN_USER and senha_hash:
            db.add(Usuario(username=ADMIN_USER.strip().lower(), senha_hash=senha_hash, papel="admin"))
            db.commit()
            log.warning("Primeiro administrador criado: %s", ADMIN_USER.strip().lower())
        else:
            log.error("Nenhum login existe ainda. Defina ADMIN_USER e ADMIN_PASSWORD (min. %d caracteres) "
                      "ou ADMIN_PASSWORD_HASH no Render e reinicie.", SENHA_MIN)
    admin = db.query(Usuario).filter_by(papel="admin").order_by(Usuario.id).first()
    if admin:  # contas criadas antes dos logins passam a ser do administrador
        db.query(Conta).filter(Conta.usuario_id.is_(None)).update({"usuario_id": admin.id})
        db.commit()


# ---------- limite de tentativas (em memoria; ok com 1 instancia) ----------
_falhas: dict = {}
MAX_FALHAS = 5
JANELA = 15 * 60


def ip_cliente(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()  # ultimo = adicionado pelo proxy do Render
    return request.client.host if request.client else "?"


def bloqueado(ip: str) -> bool:
    agora = time.time()
    _falhas[ip] = [t for t in _falhas.get(ip, []) if agora - t < JANELA]
    return len(_falhas[ip]) >= MAX_FALHAS


def registrar_falha(ip: str):
    _falhas.setdefault(ip, []).append(time.time())


def limpar_falhas(ip: str):
    _falhas.pop(ip, None)