"""Instagram API with Instagram Login (Business Login).
Nao exige Pagina do Facebook: o usuario entra com as credenciais do Instagram."""
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from crypto import encrypt, decrypt
from database import get_db
from models import Conta

router = APIRouter(prefix="/auth/instagram", tags=["auth"])

APP_ID = os.environ["IG_APP_ID"]
APP_SECRET = os.environ["IG_APP_SECRET"]
REDIRECT_URI = os.environ["IG_REDIRECT_URI"]
FRONTEND_URL = os.getenv("FRONTEND_URL", "/painel/")
GRAPH_BASE = os.getenv("GRAPH_BASE", "https://graph.instagram.com/v21.0")
SCOPES = "instagram_business_basic,instagram_business_content_publish"


@router.get("/login")
def login():
    """Redireciona o usuario para a tela de autorizacao do Instagram."""
    state = secrets.token_urlsafe(24)
    url = "https://www.instagram.com/oauth/authorize?" + urlencode({
        "client_id": APP_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    })
    resp = RedirectResponse(url)
    resp.set_cookie("ig_oauth_state", state, max_age=600, httponly=True,
                    samesite="lax", secure=REDIRECT_URI.startswith("https"))
    return resp


@router.get("/callback")
def callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if error:
        raise HTTPException(400, error_description or error)
    if not code or not state or state != request.cookies.get("ig_oauth_state"):
        raise HTTPException(400, "State invalido ou ausente (possivel CSRF)")
    code = code.removesuffix("#_")  # a Meta anexa '#_' ao final do code

    with httpx.Client(timeout=30) as c:
        # 1) code -> token de curta duracao (code vale 1h e so pode ser usado 1x)
        r = c.post("https://api.instagram.com/oauth/access_token", data={
            "client_id": APP_ID, "client_secret": APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI, "code": code,
        })
        body = r.json()
        if r.status_code >= 400:
            raise HTTPException(400, f"Falha ao trocar o code: {body}")
        item = body["data"][0] if "data" in body else body  # resposta vem em 'data'
        perms = item.get("permissions", "")
        if isinstance(perms, list):
            perms = ",".join(perms)
        if perms and "instagram_business_content_publish" not in perms:
            raise HTTPException(400, "Voce precisa autorizar a permissao de publicar conteudo")

        # 2) curta -> longa duracao (60 dias)
        r = c.get("https://graph.instagram.com/access_token", params={
            "grant_type": "ig_exchange_token",
            "client_secret": APP_SECRET,
            "access_token": item["access_token"],
        })
        longo = r.json()
        if r.status_code >= 400:
            raise HTTPException(400, f"Falha ao gerar token longo: {longo}")
        token = longo["access_token"]
        expira = datetime.now(timezone.utc) + timedelta(seconds=longo.get("expires_in", 5184000))

        # 3) dados da conta
        r = c.get(f"{GRAPH_BASE}/me", params={
            "fields": "user_id,username", "access_token": token})
        perfil = r.json()
        if r.status_code >= 400:
            raise HTTPException(400, f"Falha ao ler o perfil: {perfil}")
        ig_user_id = str(perfil.get("user_id") or item["user_id"])
        username = perfil.get("username", ig_user_id)

    conta = db.query(Conta).filter_by(ig_user_id=ig_user_id).first()
    if conta:  # reconectar conta existente
        conta.username = username
        conta.access_token_enc = encrypt(token)
        conta.token_expira_em = expira
        conta.ativa = True
    else:
        conta = Conta(username=username, ig_user_id=ig_user_id,
                      access_token_enc=encrypt(token), token_expira_em=expira)
        db.add(conta)
    db.commit()

    resp = RedirectResponse(f"{FRONTEND_URL}?conectado={username}")
    resp.delete_cookie("ig_oauth_state")
    return resp


def renovar_token(conta: Conta) -> None:
    """Renova o token longo por mais 60 dias (token precisa ter >24h e nao ter expirado)."""
    r = httpx.get("https://graph.instagram.com/refresh_access_token", params={
        "grant_type": "ig_refresh_token",
        "access_token": decrypt(conta.access_token_enc)}, timeout=30)
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(data)
    conta.access_token_enc = encrypt(data["access_token"])
    conta.token_expira_em = datetime.now(timezone.utc) + timedelta(
        seconds=data.get("expires_in", 5184000))