"""Pautas dos clientes da agencia (so admin): marcacoes salvas no banco e exportacao do DOCX original
com o amarelo aplicado so nas pautas que mudaram. Tambem guarda preferencias por login (acompanhamento semanal)."""
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from Auth import so_admin, usuario_atual
from database import get_db
from models import PautaMarca, Preferencia, Usuario

router = APIRouter(tags=["pautas"])
DIR = Path(__file__).parent / "pautas_dados"
GRUPOS = json.loads((DIR / "pautas.json").read_text(encoding="utf-8"))
POR_ID = {g["id"]: g for g in GRUPOS}
HLX = '<w:highlight w:val="yellow"/>'
RPRW = "<w:rPr>" + HLX + "</w:rPr>"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _grupo(gid: str) -> dict:
    if gid not in POR_ID:
        raise HTTPException(404, "Cliente de pauta nao encontrado")
    return POR_ID[gid]


def _marcas(db: Session, g: dict) -> dict:
    """Mapa chave -> usada. Na 1a vez parte do amarelo que ja estava no documento original."""
    rows = {r.chave: r for r in db.query(PautaMarca).filter_by(grupo=g["id"])}
    novos = [PautaMarca(grupo=g["id"], chave=i["k"], usada=bool(i["u"])) for i in g["itens"] if i["k"] not in rows]
    if novos:
        db.add_all(novos)
        db.commit()
        rows = {r.chave: r for r in db.query(PautaMarca).filter_by(grupo=g["id"])}
    return {k: r.usada for k, r in rows.items()}


@router.get("/pautas")
def listar(_: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    out = []
    for g in GRUPOS:
        m = _marcas(db, g)
        out.append({"id": g["id"], "n": g["n"], "itens": [
            {"k": i["k"], "t": i["t"], "d": i["d"], "x": i["x"], "usada": m[i["k"]]} for i in g["itens"]]})
    return out


class Marca(BaseModel):
    usada: bool


@router.put("/pautas/{gid}/{chave}")
def marcar(gid: str, chave: str, dados: Marca, _: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    g = _grupo(gid)
    if chave not in {i["k"] for i in g["itens"]}:
        raise HTTPException(404, "Pauta nao encontrada")
    _marcas(db, g)
    db.query(PautaMarca).filter_by(grupo=gid, chave=chave).update({"usada": dados.usada})
    db.commit()
    return {"ok": True}


@router.post("/pautas/limpar-tudo")
def limpar_tudo(_: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    for g in GRUPOS:
        _marcas(db, g)
    db.query(PautaMarca).update({"usada": False})
    db.commit()
    return {"ok": True}


@router.post("/pautas/{gid}/limpar")
def limpar(gid: str, _: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    g = _grupo(gid)
    _marcas(db, g)
    db.query(PautaMarca).filter_by(grupo=gid).update({"usada": False})
    db.commit()
    return {"ok": True}


# ---------- DOCX original + amarelo ----------
def gerar_docx(g: dict, marcas: dict) -> bytes:
    """Devolve o documento original; so mexe nas pautas cuja marca difere do que o arquivo ja tinha."""
    src = zipfile.ZipFile(DIR / "docs" / (g["id"] + ".docx"))
    xml = src.read("word/document.xml").decode("utf-8")
    ed, vistos = [], set()
    for i in g["itens"]:
        cur = marcas.get(i["k"], bool(i["u"]))
        if cur == bool(i["u"]):
            continue
        a = i["e"] if cur else i["r"]
        for n in range(0, len(a), 3):
            if a[n] not in vistos:
                vistos.add(a[n])
                ed.append(a[n:n + 3])
    ed.sort()
    partes, pos = [], 0
    for ini, fim, kind in ed:
        partes += [xml[pos:ini], HLX if kind == 0 else RPRW if kind == 1 else ""]
        pos = fim
    partes.append(xml[pos:])
    novo = "".join(partes).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for info in src.infolist():
            out.writestr(info, novo if info.filename == "word/document.xml" else src.read(info),
                         compress_type=info.compress_type)
    return buf.getvalue()


def _nome(g: dict) -> str:
    return re.sub(r'[\\/:*?"<>|&]', "", g["n"]) + " - pauta marcada.docx"


def _anexo(dados: bytes, nome: str, mime: str) -> Response:
    return Response(dados, media_type=mime, headers={
        "Content-Disposition": f"attachment; filename=\"pauta.docx\"; filename*=UTF-8''{quote(nome)}"})


@router.get("/pautas-todos.zip")
def baixar_todos(_: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for g in GRUPOS:
            z.writestr(_nome(g), gerar_docx(g, _marcas(db, g)))
    return Response(buf.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": "attachment; filename=\"pautas.zip\"; filename*=UTF-8''" + quote("Pautas marcadas - todos os clientes.zip")})


@router.get("/pautas/{gid}/docx")
def baixar(gid: str, _: Usuario = Depends(so_admin), db: Session = Depends(get_db)):
    g = _grupo(gid)
    return _anexo(gerar_docx(g, _marcas(db, g)), _nome(g), DOCX_MIME)


# ---------- preferencias por login ----------
RE_CHAVE = re.compile(r"^[a-z0-9_-]{1,40}$")


class Pref(BaseModel):
    valor: Any


@router.get("/preferencias/{chave}")
def ler_pref(chave: str, u: Usuario = Depends(usuario_atual), db: Session = Depends(get_db)):
    if not RE_CHAVE.match(chave):
        raise HTTPException(400, "Chave invalida")
    r = db.query(Preferencia).filter_by(usuario_id=u.id, chave=chave).first()
    return {"valor": json.loads(r.valor) if r else None}


@router.put("/preferencias/{chave}")
def gravar_pref(chave: str, dados: Pref, u: Usuario = Depends(usuario_atual), db: Session = Depends(get_db)):
    if not RE_CHAVE.match(chave):
        raise HTTPException(400, "Chave invalida")
    txt = json.dumps(dados.valor, ensure_ascii=False)
    if len(txt) > 1_000_000:
        raise HTTPException(413, "Dados grandes demais")
    r = db.query(Preferencia).filter_by(usuario_id=u.id, chave=chave).first()
    if r:
        r.valor = txt
    else:
        db.add(Preferencia(usuario_id=u.id, chave=chave, valor=txt))
    db.commit()
    return {"ok": True}
