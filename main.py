from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
import re
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Depends, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import Auth
from Auth import (COOKIE, DURACAO, PAPEIS, RE_USUARIO, SENHA_MIN, dono_efetivo, filtrar_por_dono,
                  pode_editar, pode_ver_conta, so_admin, usuario_atual, usuario_logado)
from database import Base, SessionLocal, engine, get_db
from models import Cliente, Conta, Post, Usuario, agora
from calendario import router as calendario_router, semear
from oauth import router as oauth_router
from pautas import router as pautas_router
from scheduler import start_scheduler, agendar_post, cancelar_agendamento
from storage import salvar_imagem, ImagemInvalida

TZ_LOCAL = ZoneInfo("America/Sao_Paulo")
MAX_BYTES = 8 * 1024 * 1024
Papel = Literal["admin", "usuario", "leitor"]
RE_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _migrar():
    """create_all nao altera tabelas que ja existem: acrescenta a coluna nova em 'contas'."""
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as con:
        con.execute(text("ALTER TABLE usuarios ALTER COLUMN username TYPE VARCHAR(254)"))
        con.execute(text("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS nome VARCHAR(120)"))
        con.execute(text("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS email VARCHAR(254)"))
        con.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_usuarios_email ON usuarios (email) WHERE email IS NOT NULL"))
        con.execute(text("ALTER TABLE contas ADD COLUMN IF NOT EXISTS usuario_id INTEGER REFERENCES usuarios(id)"))
        con.execute(text("CREATE INDEX IF NOT EXISTS ix_contas_usuario_id ON contas (usuario_id)"))
        con.execute(text("ALTER TABLE dia_manual ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'postado'"))
        for col, tipo in (("cliente_id", "INTEGER REFERENCES clientes(id)"), ("dia", "DATE"), ("pauta_grupo", "VARCHAR(30)"),
                          ("pauta_chave", "VARCHAR(20)"), ("confirmado_em", "TIMESTAMPTZ"), ("ig_permalink", "TEXT")):
            con.execute(text(f"ALTER TABLE posts ADD COLUMN IF NOT EXISTS {col} {tipo}"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    _migrar()
    with SessionLocal() as db:
        Auth.preparar_banco(db)
        semear(db)
    sched = start_scheduler()
    yield
    sched.shutdown(wait=False)


app = FastAPI(title="Instagram Scheduler", lifespan=lifespan)
app.include_router(oauth_router)
app.include_router(calendario_router)
app.include_router(pautas_router)


# ---------- paginas publicas e barreira do painel ----------
@app.middleware("http")
async def proteger_painel(request: Request, call_next):
    if request.url.path.startswith("/painel") and not Auth.token_valido(request):
        return RedirectResponse("/login")
    return await call_next(request)


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}  # nao toca no banco: o pinger nao deve acordar o Postgres


@app.get("/google241c82cc04b198c8.html", include_in_schema=False)
def google_verificacao():
    return PlainTextResponse("google-site-verification: google241c82cc04b198c8.html")


@app.get("/", include_in_schema=False)
def raiz():
    return RedirectResponse("/painel/")


# ---------- login / logout / eu ----------
def _definir_cookie(resp, request: Request, u: Usuario):
    resp.set_cookie(COOKIE, Auth.criar_sessao(u), max_age=DURACAO, httponly=True,
                    samesite="lax", secure=Auth.cookie_seguro(request))


@app.get("/login", include_in_schema=False)
def pagina_login(request: Request, db: Session = Depends(get_db)):
    if usuario_logado(request, db):
        return RedirectResponse("/painel/")
    return FileResponse("static/login.html")


@app.get("/criar-conta", include_in_schema=False)
def pagina_criar_conta(request: Request, db: Session = Depends(get_db)):
    """Tela pública para criar uma conta de usuário."""
    if usuario_logado(request, db):
        return RedirectResponse("/painel/")
    return FileResponse("static/criar-conta.html")


@app.post("/criar-conta", include_in_schema=False)
def cadastrar_conta(
    request: Request,
    nome: str = Form(...),
    sobrenome: str = Form(""),
    email: str = Form(...),
    senha: str = Form(...),
    db: Session = Depends(get_db),
):
    nome_completo = " ".join((nome.strip(), sobrenome.strip())).strip()
    email_normalizado = email.strip().lower()
    if not nome_completo or len(nome_completo) > 120:
        return RedirectResponse("/criar-conta?erro=nome", status_code=303)
    if len(email_normalizado) > 254 or not RE_EMAIL.fullmatch(email_normalizado):
        return RedirectResponse("/criar-conta?erro=email", status_code=303)
    if len(senha) < SENHA_MIN:
        return RedirectResponse("/criar-conta?erro=senha", status_code=303)
    if db.query(Usuario).filter(Usuario.email == email_normalizado).first():
        return RedirectResponse("/criar-conta?erro=existe", status_code=303)

    usuario = Usuario(
        username=email_normalizado,
        nome=nome_completo,
        email=email_normalizado,
        senha_hash=Auth.gerar_hash(senha),
        papel="usuario",
    )
    db.add(usuario)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse("/criar-conta?erro=existe", status_code=303)

    resp = RedirectResponse("/painel/", status_code=303)
    _definir_cookie(resp, request, usuario)
    return resp


@app.post("/login", include_in_schema=False)
def entrar(request: Request, usuario: str = Form(...), senha: str = Form(...), db: Session = Depends(get_db)):
    ip = Auth.ip_cliente(request)
    if Auth.bloqueado(ip):
        return RedirectResponse("/login?erro=bloqueado", status_code=303)
    u = Auth.checar_credenciais(db, usuario, senha)
    if not u:
        Auth.registrar_falha(ip)
        return RedirectResponse("/login?erro=1", status_code=303)
    Auth.limpar_falhas(ip)
    u.ultimo_login = agora()
    db.commit()
    resp = RedirectResponse("/painel/", status_code=303)
    _definir_cookie(resp, request, u)
    return resp


@app.post("/logout", include_in_schema=False)
def sair():
    resp = RedirectResponse("/login?saiu=1", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


def _eu(u: Usuario):
    return {"id": u.id, "username": u.username, "nome": u.nome, "email": u.email,
            "papel": u.papel, "papel_nome": PAPEIS[u.papel],
            "pode_editar": u.papel != "leitor"}


@app.get("/me")
def me(u: Usuario = Depends(usuario_atual)):
    return _eu(u)


class TrocaSenha(BaseModel):
    atual: str
    nova: str


@app.post("/me/senha")
def trocar_minha_senha(dados: TrocaSenha, request: Request, u: Usuario = Depends(usuario_atual),
                       db: Session = Depends(get_db)):
    if not Auth.verificar_senha(dados.atual, u.senha_hash):
        raise HTTPException(400, "A senha atual esta incorreta")
    if len(dados.nova) < SENHA_MIN:
        raise HTTPException(400, f"A nova senha precisa ter pelo menos {SENHA_MIN} caracteres")
    u.senha_hash = Auth.gerar_hash(dados.nova)
    u.sessao_versao += 1  # derruba as outras sessoes desse login
    db.commit()
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"ok": True})
    _definir_cookie(resp, request, u)  # mantem esta aba logada
    return resp


# ---------- gestao de logins (so admin) ----------
class NovoUsuario(BaseModel):
    username: str
    senha: str
    papel: Papel = "usuario"
    dono_id: Optional[int] = None


class EditaUsuario(BaseModel):
    papel: Optional[Papel] = None
    ativo: Optional[bool] = None
    senha: Optional[str] = None
    dono_id: Optional[int] = None


def _json_usuario(u: Usuario, db: Session):
    dono = db.get(Usuario, u.dono_id) if u.dono_id else None
    return {"id": u.id, "username": u.username, "nome": u.nome, "email": u.email,
            "papel": u.papel, "papel_nome": PAPEIS[u.papel],
            "ativo": u.ativo, "dono_id": u.dono_id, "dono": dono.username if dono else None,
            "contas": db.query(func.count(Conta.id)).filter(Conta.usuario_id == u.id).scalar(),
            "ultimo_login": u.ultimo_login}


def _validar_dono(db: Session, papel: str, dono_id: Optional[int]):
    if papel != "leitor":
        return None
    d = db.get(Usuario, dono_id) if dono_id else None
    if not d or d.papel != "usuario":
        raise HTTPException(400, "Escolha de qual login do tipo Usuário este leitor vai ver os dados")
    return d.id


@app.get("/usuarios")
def listar_usuarios(_: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    return [_json_usuario(u, db) for u in db.query(Usuario).order_by(Usuario.username).all()]


@app.post("/usuarios")
def criar_usuario(dados: NovoUsuario, _: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    nome = dados.username.strip().lower()
    if not RE_USUARIO.match(nome):
        raise HTTPException(400, "Usuario: 3 a 32 caracteres, so letras minusculas, numeros, ponto, hifen e underline")
    if len(dados.senha) < SENHA_MIN:
        raise HTTPException(400, f"A senha precisa ter pelo menos {SENHA_MIN} caracteres")
    if db.query(Usuario).filter(Usuario.username == nome).first():
        raise HTTPException(409, "Ja existe um login com esse nome")
    u = Usuario(username=nome, senha_hash=Auth.gerar_hash(dados.senha), papel=dados.papel,
                dono_id=_validar_dono(db, dados.papel, dados.dono_id))
    db.add(u)
    db.commit()
    return _json_usuario(u, db)


@app.patch("/usuarios/{uid}")
def editar_usuario(uid: int, dados: EditaUsuario, eu: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    u = db.get(Usuario, uid)
    if not u:
        raise HTTPException(404, "Login nao encontrado")
    campos = dados.model_dump(exclude_unset=True)
    if u.id == eu.id and ({"papel", "ativo"} & campos.keys()):
        if campos.get("papel", u.papel) != "admin" or campos.get("ativo", True) is False:
            raise HTTPException(400, "Voce nao pode tirar o proprio acesso de administrador")
    if "papel" in campos or "dono_id" in campos:
        papel = campos.get("papel", u.papel)
        u.dono_id = _validar_dono(db, papel, campos.get("dono_id", u.dono_id))
        u.papel = papel
    if "ativo" in campos and campos["ativo"] is not None:
        u.ativo = campos["ativo"]
        u.sessao_versao += 1
    if campos.get("senha"):
        if len(campos["senha"]) < SENHA_MIN:
            raise HTTPException(400, f"A senha precisa ter pelo menos {SENHA_MIN} caracteres")
        u.senha_hash = Auth.gerar_hash(campos["senha"])
        u.sessao_versao += 1
    db.commit()
    return _json_usuario(u, db)


@app.delete("/usuarios/{uid}")
def excluir_usuario(uid: int, eu: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    u = db.get(Usuario, uid)
    if not u:
        raise HTTPException(404, "Login nao encontrado")
    if u.id == eu.id:
        raise HTTPException(400, "Voce nao pode excluir o proprio login")
    if db.query(Conta).filter(Conta.usuario_id == u.id).count():
        raise HTTPException(400, "Esse login ainda tem contas do Instagram. Desative o login em vez de excluir.")
    db.query(Usuario).filter(Usuario.dono_id == u.id).update({"dono_id": None})
    db.delete(u)
    db.commit()
    return {"ok": True}


# ---------- contas (sempre filtradas pelo login) ----------
def _conta_permitida(db: Session, u: Usuario, conta_id: int) -> Conta:
    c = db.get(Conta, conta_id)
    if not c or not pode_ver_conta(u, c):
        raise HTTPException(404, "Conta nao encontrada")
    return c


@app.get("/contas")
def listar_contas(usuario_id: Optional[int] = None, u: Usuario = Depends(usuario_atual),
                  db: Session = Depends(get_db)):
    q = filtrar_por_dono(db.query(Conta), u, usuario_id).order_by(Conta.username)
    # nunca devolve o token
    return [{"id": c.id, "username": c.username, "ativa": c.ativa, "token_expira_em": c.token_expira_em,
             "dono": c.dono.username if c.dono else None} for c in q.all()]


@app.delete("/contas/{conta_id}")
def desconectar_conta(conta_id: int, u: Usuario = Depends(pode_editar), db: Session = Depends(get_db)):
    c = _conta_permitida(db, u, conta_id)
    c.ativa = False
    for p in db.query(Post).filter_by(conta_id=c.id, status="agendado"):
        p.status = "cancelado"
        cancelar_agendamento(p.id)
    db.commit()
    return {"ok": True}


# ---------- posts (sempre filtrados pelo login) ----------
@app.post("/posts")
def criar_post(  # 'def' (nao async): roda em thread, pois o upload bloqueia
    conta_id: int = Form(...),
    legenda: str = Form(""),
    agendado_para: Optional[datetime] = Form(None),  # vazio = publicar agora
    cliente_id: Optional[int] = Form(None),          # post do calendario (so admin)
    dia: Optional[date] = Form(None),
    pauta_grupo: Optional[str] = Form(None),
    pauta_chave: Optional[str] = Form(None),
    imagem: UploadFile = File(...),
    u: Usuario = Depends(pode_editar),
    db: Session = Depends(get_db),
):
    conta = _conta_permitida(db, u, conta_id)
    if cliente_id is not None:
        if u.papel != "admin":
            raise HTTPException(403, "Apenas administradores postam pelo calendario")
        cli = db.get(Cliente, cliente_id)
        if not cli or not cli.ativo or not dia:
            raise HTTPException(400, "Cliente ou dia invalido")
    if not conta.ativa:
        raise HTTPException(404, "Conta nao encontrada ou desconectada")
    if imagem.content_type not in ("image/jpeg", "image/png"):
        raise HTTPException(400, "Use uma imagem JPEG ou PNG")

    conteudo = imagem.file.read()
    if len(conteudo) > MAX_BYTES:
        raise HTTPException(400, "A imagem passa de 8 MB")

    quando = agendado_para or datetime.now(timezone.utc)
    if quando.tzinfo is None:                      # sem fuso = horario de Brasilia
        quando = quando.replace(tzinfo=TZ_LOCAL)
    quando = quando.astimezone(timezone.utc)

    try:
        url = salvar_imagem(conteudo, imagem.filename or "imagem")
    except ImagemInvalida as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Falha ao enviar a imagem para o Supabase Storage: {e}")

    p = Post(conta_id=conta.id, legenda=legenda, agendado_para=quando, imagem_url=url, cliente_id=cliente_id,
             dia=dia if cliente_id is not None else None, pauta_grupo=pauta_grupo, pauta_chave=pauta_chave)
    db.add(p)
    db.commit()
    agendar_post(p.id, p.agendado_para)
    return {"id": p.id, "status": p.status, "agendado_para": p.agendado_para}


@app.get("/posts")
def listar_posts(usuario_id: Optional[int] = None, u: Usuario = Depends(usuario_atual),
                 db: Session = Depends(get_db)):
    q = filtrar_por_dono(db.query(Post).join(Conta, Post.conta_id == Conta.id), u, usuario_id)
    return [{"id": p.id, "conta": p.conta.username, "dono": p.conta.dono.username if p.conta.dono else None,
             "legenda": p.legenda, "imagem_url": p.imagem_url, "agendado_para": p.agendado_para,
             "status": p.status, "erro": p.erro, "cliente": p.cliente.nome if p.cliente else None,
             "dia": p.dia, "permalink": p.ig_permalink, "confirmado_em": p.confirmado_em}
            for p in q.order_by(Post.agendado_para.desc()).all()]


@app.delete("/posts/{post_id}")
def cancelar_post(post_id: int, u: Usuario = Depends(pode_editar), db: Session = Depends(get_db)):
    p = db.get(Post, post_id)
    if not p or not pode_ver_conta(u, p.conta):
        raise HTTPException(404, "Post nao encontrado")
    if p.status != "agendado":
        raise HTTPException(400, f"Nao da para cancelar post com status '{p.status}'")
    p.status = "cancelado"
    db.commit()
    cancelar_agendamento(p.id)
    return {"ok": True}


app.mount("/painel", StaticFiles(directory="static", html=True), name="painel")
