from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, UniqueConstraint, Date
from sqlalchemy.orm import relationship
from database import Base


def agora():
    return datetime.now(timezone.utc)


class Usuario(Base):
    """Um login do painel. papel: admin | usuario | leitor."""
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True)
    username = Column(String(50), nullable=False, unique=True, index=True)
    senha_hash = Column(Text, nullable=False)
    papel = Column(String(20), nullable=False, default="usuario")
    ativo = Column(Boolean, default=True, nullable=False)
    # so para 'leitor': de qual login ele enxerga as contas e os posts
    dono_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    sessao_versao = Column(Integer, default=1, nullable=False)  # +1 invalida todas as sessoes dele
    criado_em = Column(DateTime(timezone=True), default=agora)
    ultimo_login = Column(DateTime(timezone=True), nullable=True)
    contas = relationship("Conta", back_populates="dono", foreign_keys="Conta.usuario_id")


class Conta(Base):
    __tablename__ = "contas"
    id = Column(Integer, primary_key=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True, index=True)  # dono da conta
    username = Column(String(100), nullable=False)
    ig_user_id = Column(String(50), nullable=False, unique=True)
    access_token_enc = Column(Text, nullable=False)  # criptografado (Fernet)
    token_expira_em = Column(DateTime(timezone=True), nullable=True)
    ativa = Column(Boolean, default=True)
    conectada_em = Column(DateTime(timezone=True), default=agora)
    dono = relationship("Usuario", back_populates="contas", foreign_keys=[usuario_id])
    posts = relationship("Post", back_populates="conta")


class Cliente(Base):
    """Cliente da agencia que aparece no calendario de postagens."""
    __tablename__ = "clientes"
    id = Column(Integer, primary_key=True)
    nome = Column(String(100), nullable=False)
    dias = Column(String(20), default="0,2,4")            # dias de postagem: 0=seg ... 6=dom
    conta_id = Column(Integer, ForeignKey("contas.id"), nullable=True)  # conta do Instagram usada nos posts
    ativo = Column(Boolean, default=True, nullable=False)
    ordem = Column(Integer, default=0)
    conta = relationship("Conta")


class DiaManual(Base):
    """Dia marcado a mao como 'postado' (post feito fora do sistema)."""
    __tablename__ = "dia_manual"
    __table_args__ = (UniqueConstraint("cliente_id", "dia"),)
    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=False, index=True)
    dia = Column(Date, nullable=False)


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
    # calendario: a qual cliente/dia este post pertence e qual pauta foi usada
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=True, index=True)
    dia = Column(Date, nullable=True, index=True)
    pauta_grupo = Column(String(30), nullable=True)
    pauta_chave = Column(String(20), nullable=True)
    # confirmacao: so existe depois que o Instagram devolveu o id do post publicado
    confirmado_em = Column(DateTime(timezone=True), nullable=True)
    ig_permalink = Column(Text, nullable=True)
    conta = relationship("Conta", back_populates="posts")
    cliente = relationship("Cliente")


class PautaMarca(Base):
    """Pauta (de um cliente da agencia) marcada como utilizada. Vale para toda a equipe admin."""
    __tablename__ = "pauta_marcas"
    __table_args__ = (UniqueConstraint("grupo", "chave"),)
    id = Column(Integer, primary_key=True)
    grupo = Column(String(30), nullable=False, index=True)   # ex.: "divell"
    chave = Column(String(20), nullable=False)               # id estavel da pauta
    usada = Column(Boolean, default=False, nullable=False)


class Preferencia(Base):
    """Guarda por login (JSON) dados de telas, como o acompanhamento semanal."""
    __tablename__ = "preferencias"
    __table_args__ = (UniqueConstraint("usuario_id", "chave"),)
    id = Column(Integer, primary_key=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False, index=True)
    chave = Column(String(40), nullable=False)
    valor = Column(Text, nullable=False)
