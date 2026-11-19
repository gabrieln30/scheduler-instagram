"""Calendario de postagens (so admin): clientes, dias de postagem, status de cada dia.
O status 'postado' NUNCA e gravado aqui: ele vem do post, e o post so vira 'publicado' depois que o
Instagram confirma (ver scheduler.py). A unica excecao e a marcacao manual, para posts feitos fora do sistema."""
from datetime import date
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from Auth import so_admin
from database import get_db
from models import Cliente, Conta, DiaManual, Post, Usuario

router = APIRouter(tags=["calendario"])
INICIAIS = [("Divell", [0, 2, 4]), ("Full Gás", [0, 2, 4]), ("Ambyente", [0, 2, 4]),
            ("Brithânia Odontologia", [1, 3, 4]), ("Nobrepap", [0, 2, 4]), ("União Embalagem", [0, 2, 4])]
PRIORIDADE = {"publicado": 3, "publicando": 2, "agendado": 1, "erro": 0}


def semear(db: Session):
    if db.query(Cliente).count() == 0:
        db.add_all([Cliente(nome=n, dias=",".join(map(str, d)), ordem=i) for i, (n, d) in enumerate(INICIAIS)])
        db.commit()


def _json(c: Cliente):
    return {"id": c.id, "nome": c.nome, "dias": [int(x) for x in (c.dias or "").split(",") if x != ""],
            "conta_id": c.conta_id, "conta": c.conta.username if c.conta else None}


class NovoCliente(BaseModel):
    nome: str = Field(min_length=1, max_length=100)
    dias: List[int] = []
    conta_id: Optional[int] = None


class EditaCliente(BaseModel):
    nome: Optional[str] = Field(default=None, min_length=1, max_length=100)
    dias: Optional[List[int]] = None
    conta_id: Optional[int] = None


def _checar(db: Session, dias, conta_id):
    if dias is not None and any(d < 0 or d > 6 for d in dias):
        raise HTTPException(400, "Dia de postagem invalido")
    if conta_id is not None:
        c = db.get(Conta, conta_id)
        if not c or not c.ativa:
            raise HTTPException(400, "Conta do Instagram nao encontrada ou desconectada")


def _cliente(db: Session, cid: int) -> Cliente:
    c = db.get(Cliente, cid)
    if not c or not c.ativo:
        raise HTTPException(404, "Cliente nao encontrado")
    return c


@router.get("/clientes")
def listar_clientes(_: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    return [_json(c) for c in db.query(Cliente).filter(Cliente.ativo).order_by(Cliente.ordem, Cliente.id)]


@router.post("/clientes")
def criar_cliente(d: NovoCliente, _: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    _checar(db, d.dias, d.conta_id)
    ordem = (db.query(Cliente).count() or 0) + 1
    c = Cliente(nome=d.nome.strip(), dias=",".join(str(x) for x in sorted(set(d.dias))), conta_id=d.conta_id, ordem=ordem)
    db.add(c)
    db.commit()
    return _json(c)


@router.patch("/clientes/{cid}")
def editar_cliente(cid: int, d: EditaCliente, _: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    c = _cliente(db, cid)
    campos = d.model_dump(exclude_unset=True)
    _checar(db, campos.get("dias"), campos.get("conta_id"))
    if campos.get("nome"):
        c.nome = campos["nome"].strip()
    if campos.get("dias") is not None:
        c.dias = ",".join(str(x) for x in sorted(set(campos["dias"])))
    if "conta_id" in campos:
        c.conta_id = campos["conta_id"]
    db.commit()
    return _json(c)


@router.delete("/clientes/{cid}")
def remover_cliente(cid: int, _: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    c = _cliente(db, cid)
    c.ativo = False  # nao apaga: os posts antigos continuam no historico
    db.commit()
    return {"ok": True}


@router.get("/calendario")
def calendario(inicio: date, fim: date, _: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    """Estado de cada cliente/dia no periodo. 'slots[cliente][AAAA-MM-DD]' = melhor post do dia + marca manual."""
    if (fim - inicio).days > 62:
        raise HTTPException(400, "Periodo grande demais")
    clientes = db.query(Cliente).filter(Cliente.ativo).order_by(Cliente.ordem, Cliente.id).all()
    slots: dict = {}
    posts = (db.query(Post).filter(Post.cliente_id.isnot(None), Post.dia >= inicio, Post.dia <= fim,
                                   Post.status != "cancelado").order_by(Post.id).all())
    for p in posts:
        s = slots.setdefault(str(p.cliente_id), {}).setdefault(p.dia.isoformat(), {"post": None, "manual": False})
        atual = s["post"]
        if atual is None or (PRIORIDADE.get(p.status, 0), p.id) >= (PRIORIDADE.get(atual["status"], 0), atual["id"]):
            s["post"] = {"id": p.id, "status": p.status, "agendado_para": p.agendado_para, "legenda": p.legenda,
                         "imagem_url": p.imagem_url, "erro": p.erro, "permalink": p.ig_permalink,
                         "confirmado_em": p.confirmado_em, "conta": p.conta.username,
                         "pauta_grupo": p.pauta_grupo, "pauta_chave": p.pauta_chave}
    for m in db.query(DiaManual).filter(DiaManual.dia >= inicio, DiaManual.dia <= fim):
        slots.setdefault(str(m.cliente_id), {}).setdefault(m.dia.isoformat(), {"post": None, "manual": False})["manual"] = m.status or "postado"
    reservadas: dict = {}  # pautas ja ligadas a um post (em qualquer data) - nao sugerir de novo
    for g, k, cid in (db.query(Post.pauta_grupo, Post.pauta_chave, Post.cliente_id)
                      .filter(Post.cliente_id.isnot(None), Post.pauta_chave.isnot(None),
                              Post.status.in_(("agendado", "publicando", "publicado")))):
        reservadas.setdefault(str(cid), []).append(k)
    return {"clientes": [_json(c) for c in clientes], "slots": slots, "reservadas": reservadas}


class Manual(BaseModel):
    status: Optional[Literal["postado", "falta_criar", "a_postar"]] = None
    feito: Optional[bool] = None  # compatibilidade com a marcacao manual anterior


@router.put("/calendario/{cid}/{dia}/manual")
def marcar_manual(cid: int, dia: date, d: Manual, _: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    _cliente(db, cid)
    r = db.query(DiaManual).filter_by(cliente_id=cid, dia=dia).first()
    status = d.status if d.status is not None else ("postado" if d.feito else None)
    if status and not r:
        db.add(DiaManual(cliente_id=cid, dia=dia, status=status))
    elif status and r:
        r.status = status
    elif not status and r:
        db.delete(r)
    db.commit()
    return {"ok": True}
