"""Login do painel: usuario + senha (hash PBKDF2) e cookie de sessao assinado."""
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

ADMIN_USER = os.environ["ADMIN_USER"]
ADMIN_PASSWORD_HASH = os.environ["ADMIN_PASSWORD_HASH"]
COOKIE = "sessao"
DURACAO = 7 * 24 * 3600  # 7 dias
_serializer = URLSafeTimedSerializer(os.environ["SESSION_SECRET"], salt="sessao-painel")


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


def checar_credenciais(usuario: str, senha: str) -> bool:
    ok_user = hmac.compare_digest(usuario.encode(), ADMIN_USER.encode())
    ok_senha = verificar_senha(senha, ADMIN_PASSWORD_HASH)  # sempre calcula (evita vazar timing)
    return ok_user and ok_senha


# ---------- sessao ----------
def criar_sessao(usuario: str) -> str:
    return _serializer.dumps({"u": usuario})


def usuario_logado(request: Request) -> Optional[str]:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    try:
        dados = _serializer.loads(token, max_age=DURACAO)
    except (BadSignature, SignatureExpired):
        return None
    return dados.get("u") if dados.get("u") == ADMIN_USER else None


def exigir_login(request: Request):
    """Dependencia das rotas da API: 401 se nao estiver logado."""
    if not usuario_logado(request):
        raise HTTPException(401, "Nao autenticado")


def cookie_seguro(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


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