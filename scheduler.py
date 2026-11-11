"""Um job por post (em vez de consultar o banco a cada 30s).
Assim o banco gratuito (Neon) pode 'dormir' entre um post e outro."""
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler

from crypto import decrypt
from database import SessionLocal
from instagram import publicar_imagem
from models import Conta, Post
from oauth import renovar_token

log = logging.getLogger("scheduler")
_sched = BackgroundScheduler(timezone="UTC")


def publicar_post(post_id: int):
    db = SessionLocal()
    try:
        p = (db.query(Post)
             .filter(Post.id == post_id, Post.status == "agendado")
             .with_for_update(skip_locked=True)  # evita publicar duas vezes
             .first())
        if not p:
            return
        p.status = "publicando"
        db.commit()
        try:
            if not p.conta.ativa:
                raise RuntimeError("Conta desconectada ou token expirado")
            p.ig_media_id = publicar_imagem(
                p.conta.ig_user_id, decrypt(p.conta.access_token_enc),
                p.imagem_url, p.legenda,
            )
            p.status = "publicado"
        except Exception as e:
            p.status = "erro"
            p.erro = str(e)[:500]
        db.commit()
    finally:
        db.close()


def agendar_post(post_id: int, quando: datetime):
    # misfire_grace_time=None: se o servico estava dormindo, publica assim que acordar
    _sched.add_job(publicar_post, "date", run_date=quando, args=[post_id],
                   id=f"post-{post_id}", replace_existing=True, misfire_grace_time=None)


def cancelar_agendamento(post_id: int):
    try:
        _sched.remove_job(f"post-{post_id}")
    except JobLookupError:
        pass


def carregar_pendentes():
    """Ao ligar (ou acordar), recria os jobs dos posts que ainda nao sairam."""
    db = SessionLocal()
    try:
        for p in db.query(Post).filter(Post.status == "agendado"):
            agendar_post(p.id, p.agendado_para)
    finally:
        db.close()


def renovar_tokens():
    """Renova tokens que vencem em ate 15 dias."""
    db = SessionLocal()
    try:
        agora = datetime.now(timezone.utc)
        limite = agora + timedelta(days=15)
        contas = db.query(Conta).filter(Conta.ativa.is_(True),
                                        Conta.token_expira_em <= limite).all()
        for c in contas:
            try:
                renovar_token(c)
            except Exception as e:
                log.error("Falha ao renovar token de @%s: %s", c.username, e)
                if c.token_expira_em <= agora:  # venceu: precisa reconectar
                    c.ativa = False
            db.commit()
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    _sched.start()
    carregar_pendentes()
    _sched.add_job(renovar_tokens, "interval", hours=24, max_instances=1,
                   next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1))
    return _sched