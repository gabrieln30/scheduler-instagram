from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from database import Base, engine, get_db
from models import Conta, Post
from oauth import router as oauth_router
from scheduler import start_scheduler, agendar_post, cancelar_agendamento
from storage import salvar_imagem, ImagemInvalida

TZ_LOCAL = ZoneInfo("America/Sao_Paulo")
MAX_BYTES = 8 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    sched = start_scheduler()
    yield
    sched.shutdown(wait=False)


app = FastAPI(title="Instagram Scheduler", lifespan=lifespan)
app.include_router(oauth_router)


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}  # nao toca no banco: o pinger nao deve acordar o Postgres


@app.get("/", include_in_schema=False)
def raiz():
    return RedirectResponse("/painel/")


@app.get("/contas")
def listar_contas(db: Session = Depends(get_db)):
    # nunca devolve o token
    return [{"id": c.id, "username": c.username, "ativa": c.ativa,
             "token_expira_em": c.token_expira_em}
            for c in db.query(Conta).order_by(Conta.username).all()]


@app.delete("/contas/{conta_id}")
def desconectar_conta(conta_id: int, db: Session = Depends(get_db)):
    c = db.get(Conta, conta_id)
    if not c:
        raise HTTPException(404, "Conta nao encontrada")
    c.ativa = False
    for p in db.query(Post).filter_by(conta_id=c.id, status="agendado"):
        p.status = "cancelado"
        cancelar_agendamento(p.id)
    db.commit()
    return {"ok": True}


@app.post("/posts")
def criar_post(  # 'def' (nao async): roda em thread, pois o upload bloqueia
    conta_id: int = Form(...),
    legenda: str = Form(""),
    agendado_para: Optional[datetime] = Form(None),  # vazio = publicar agora
    imagem: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    conta = db.get(Conta, conta_id)
    if not conta or not conta.ativa:
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

    p = Post(conta_id=conta_id, legenda=legenda, agendado_para=quando, imagem_url=url)
    db.add(p)
    db.commit()
    agendar_post(p.id, p.agendado_para)
    return {"id": p.id, "status": p.status, "agendado_para": p.agendado_para}


@app.get("/posts")
def listar_posts(db: Session = Depends(get_db)):
    posts = db.query(Post).order_by(Post.agendado_para.desc()).all()
    return [{"id": p.id, "conta": p.conta.username, "legenda": p.legenda,
             "imagem_url": p.imagem_url, "agendado_para": p.agendado_para,
             "status": p.status, "erro": p.erro} for p in posts]


@app.delete("/posts/{post_id}")
def cancelar_post(post_id: int, db: Session = Depends(get_db)):
    p = db.get(Post, post_id)
    if not p:
        raise HTTPException(404, "Post nao encontrado")
    if p.status != "agendado":
        raise HTTPException(400, f"Nao da para cancelar post com status '{p.status}'")
    p.status = "cancelado"
    db.commit()
    cancelar_agendamento(p.id)
    return {"ok": True}


# Front-end (deixe por ultimo, depois de todas as rotas)
app.mount("/painel", StaticFiles(directory="static", html=True), name="painel")