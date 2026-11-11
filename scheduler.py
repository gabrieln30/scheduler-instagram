import logging
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from crypto import decrypt
from database import SessionLocal
from instagram import publicar_imagem
from models import Conta, Post
from oauth import renovar_token

log = logging.getLogger("scheduler")


def publicar_pendentes():
    db = SessionLocal()
    try:
        agora = datetime.now(timezone.utc)
        posts = (
            db.query(Post)
            .filter(Post.status == "agendado", Post.agendado_para <= agora)
            .with_for_update(skip_locked=True)  # evita publicar duas vezes
            .all()
        )
        for p in posts:
            p.status = "publicando"
        db.commit()

        for p in posts:
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


def renovar_tokens():
    """Roda 1x/dia: renova tokens que vencem em ate 15 dias."""
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
    s = BackgroundScheduler(timezone="UTC")
    s.add_job(publicar_pendentes, "interval", seconds=30, max_instances=1)
    s.add_job(renovar_tokens, "interval", hours=24, max_instances=1,
              next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1))
    s.start()
    return s
