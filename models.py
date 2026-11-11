from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from database import Base


def agora():
    return datetime.now(timezone.utc)


class Conta(Base):
    __tablename__ = "contas"
    id = Column(Integer, primary_key=True)
    username = Column(String(100), nullable=False)
    ig_user_id = Column(String(50), nullable=False, unique=True)
    access_token_enc = Column(Text, nullable=False)  # criptografado (Fernet)
    token_expira_em = Column(DateTime(timezone=True), nullable=True)
    ativa = Column(Boolean, default=True)
    conectada_em = Column(DateTime(timezone=True), default=agora)
    posts = relationship("Post", back_populates="conta")


class Post(Base):
    __tablename__ = "posts"
    id = Column(Integer, primary_key=True)
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=False)
    imagem_url = Column(Text, nullable=False)
    legenda = Column(Text, default="")
    agendado_para = Column(DateTime(timezone=True), nullable=False)
    # agendado | publicando | publicado | erro | cancelado
    status = Column(String(20), default="agendado", index=True)
    ig_media_id = Column(String(50), nullable=True)
    erro = Column(Text, nullable=True)
    criado_em = Column(DateTime(timezone=True), default=agora)
    conta = relationship("Conta", back_populates="posts")
