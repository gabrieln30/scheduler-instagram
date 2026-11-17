"""Guarda as imagens no Supabase Storage (bucket PUBLICO).
Converte tudo para JPEG antes de enviar, que e o formato que a API do Instagram aceita."""
import io
import os
import uuid

import httpx
from PIL import Image, ImageOps

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")      # https://SEU_REF.supabase.co
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]           # chave secreta: so no servidor!
BUCKET = os.getenv("SUPABASE_BUCKET", "posts")


class ImagemInvalida(Exception):
    pass


def _para_jpeg(conteudo: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(conteudo))
        img = ImageOps.exif_transpose(img)  # respeita a rotacao da foto
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            img = img.convert("RGBA")
            fundo = Image.new("RGB", img.size, (255, 255, 255))  # PNG transparente -> fundo branco
            fundo.paste(img, mask=img.split()[-1])
            img = fundo
        else:
            img = img.convert("RGB")
    except Exception as e:
        raise ImagemInvalida("O arquivo nao e uma imagem valida") from e
    saida = io.BytesIO()
    img.save(saida, "JPEG", quality=90, optimize=True)
    return saida.getvalue()


def salvar_imagem(conteudo: bytes, nome: str = "") -> str:
    jpeg = _para_jpeg(conteudo)
    caminho = f"{uuid.uuid4().hex}.jpg"
    headers = {"apikey": SUPABASE_KEY, "Content-Type": "image/jpeg"}
    if not SUPABASE_KEY.startswith("sb_"):  # chave antiga (JWT service_role) tambem vai no Authorization
        headers["Authorization"] = f"Bearer {SUPABASE_KEY}"
    r = httpx.post(f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{caminho}",
                   headers=headers, content=jpeg, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(r.text[:300])
    return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{caminho}"