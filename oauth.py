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
# A configuracao do "Login do Facebook para Empresas" criada no painel da Meta.
# Sem ela, a Meta exibe a pagina "Esta pagina nao esta disponivel" depois do login.
LOGIN_CONFIG_ID = os.getenv("IG_LOGIN_CONFIG_ID")
FRONTEND_URL = os.getenv("FRONTEND_URL", "/painel/")
GRAPH_BASE = os.getenv("GRAPH_BASE", "https://graph.instagram.com/v21.0")
FB_GRAPH_BASE = os.getenv("FB_GRAPH_BASE", "https://graph.facebook.com/v21.0")
SCOPES = "instagram_business_basic,instagram_business_content_publish"
STATE_TTL = timedelta(minutes=10)
# O Login for Business abre um popup no dominio do Facebook. Guardamos tambem o
# state no servidor para que a verificacao CSRF nao dependa do navegador enviar
# o cookie no retorno entre dominios. O Render usa um unico worker.
_oauth_states: dict[str, datetime] = {}


@router.get("/login")
def login():
    """Redireciona o usuario para a tela de autorizacao do Instagram."""
    state = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    _oauth_states[state] = now
    for saved_state, created_at in list(_oauth_states.items()):
        if now - created_at > STATE_TTL:
            _oauth_states.pop(saved_state, None)
    params = {
        "client_id": APP_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "state": state,
    }
    if LOGIN_CONFIG_ID:
        # URL equivalente a "URL de login" gerada pelo painel da Meta. As
        # permissoes pertencem a configuracao, por isso nao enviamos scope aqui.
        params.update({
            "config_id": LOGIN_CONFIG_ID,
            "override_default_response_type": "true",
        })
        authorize_url = "https://www.facebook.com/dialog/oauth"
    else:
        params["scope"] = SCOPES
        authorize_url = "https://www.instagram.com/oauth/authorize"
    url = authorize_url + "?" + urlencode(params)
    resp = RedirectResponse(url)
    secure_cookie = REDIRECT_URI.startswith("https")
    resp.set_cookie("ig_oauth_state", state, max_age=600, httponly=True,
                    samesite="none" if secure_cookie else "lax", secure=secure_cookie)
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
    created_at = _oauth_states.pop(state, None) if state else None
    cookie_state = request.cookies.get("ig_oauth_state", "")
    valid_cookie = bool(state) and secrets.compare_digest(state, cookie_state)
    valid_server_state = bool(created_at) and datetime.now(timezone.utc) - created_at <= STATE_TTL
    if not code or not state or not (valid_cookie or valid_server_state):
        raise HTTPException(400, "State invalido ou ausente (possivel CSRF)")
    code = code.removesuffix("#_")  # a Meta anexa '#_' ao final do code

    with httpx.Client(timeout=30) as c:
        if LOGIN_CONFIG_ID:
            # O code foi emitido pelo Facebook Login for Business, portanto deve
            # ser trocado no Graph API (e nao em api.instagram.com). A
            # configuracao selecionada gera um token de usuario do sistema sem
            # expiracao, restrito aos ativos que ela contem.
            r = c.get("https://graph.facebook.com/v21.0/oauth/access_token", params={
                "client_id": APP_ID,
                "client_secret": APP_SECRET,
                "redirect_uri": REDIRECT_URI,
                "code": code,
            })
            item = r.json()
            if r.status_code >= 400:
                raise HTTPException(400, f"Falha ao trocar o code: {item}")
            token = item["access_token"]
            expira = None
        else:
            # Fluxo legado do Instagram Login: code -> token curto -> token longo.
            r = c.post("https://api.instagram.com/oauth/access_token", data={
                "client_id": APP_ID, "client_secret": APP_SECRET,
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI, "code": code,
            })
            body = r.json()
            if r.status_code >= 400:
                raise HTTPException(400, f"Falha ao trocar o code: {body}")
            item = body["data"][0] if "data" in body else body
            perms = item.get("permissions", "")
            if isinstance(perms, list):
                perms = ",".join(perms)
            if perms and "instagram_business_content_publish" not in perms:
                raise HTTPException(400, "Voce precisa autorizar a permissao de publicar conteudo")

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
        if LOGIN_CONFIG_ID:
            # O fluxo Facebook Login resolve a conta pelo Page conectado ao
            # perfil profissional. O token da pagina e o usado para publicar.
            r = c.get(f"{FB_GRAPH_BASE}/me/accounts", params={
                "fields": "id,name,access_token,instagram_business_account{id,username}",
                "access_token": token,
            })
            pages = r.json()
            page = next((p for p in pages.get("data", []) if p.get("instagram_business_account")), None)
            if page:
                perfil = page["instagram_business_account"]
                ig_user_id = str(perfil["id"])
                username = perfil.get("username", ig_user_id)
                token = page.get("access_token", token)
            else:
                # Algumas configuracoes restrigem diretamente a conta Instagram
                # em vez de expor uma Page. Tentamos esse formato como fallback.
                r = c.get(f"{FB_GRAPH_BASE}/debug_token", params={
                    "input_token": token,
                    "access_token": f"{APP_ID}|{APP_SECRET}",
                })
                debug = r.json()
                if r.status_code >= 400 or not debug.get("data", {}).get("is_valid"):
                    raise HTTPException(400, f"Falha ao validar o token: {debug}")
                target_ids = []
                for scope in debug["data"].get("granular_scopes", []):
                    if scope.get("scope") in ("instagram_basic", "instagram_content_publish"):
                        target_ids.extend(scope.get("target_ids") or [])
                ig_user_id = next(iter(dict.fromkeys(target_ids)), None)
                if not ig_user_id:
                    raise HTTPException(
                        400,
                        "Nenhuma conta Instagram foi encontrada. Na configuracao da Meta, adicione "
                        "as permissoes pages_show_list e pages_read_engagement, selecione a Pagina "
                        "do Facebook vinculada ao Instagram e conecte novamente.",
                    )
                r = c.get(f"{FB_GRAPH_BASE}/{ig_user_id}", params={
                    "fields": "id,username", "access_token": token})
                perfil = r.json()
                if r.status_code >= 400:
                    raise HTTPException(400, f"Falha ao ler o perfil: {perfil}")
                ig_user_id = str(perfil["id"])
                username = perfil.get("username", ig_user_id)
        else:
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
    if LOGIN_CONFIG_ID:
        return  # Login for Business gera token de usuario do sistema, sem expiracao.
    r = httpx.get("https://graph.instagram.com/refresh_access_token", params={
        "grant_type": "ig_refresh_token",
        "access_token": decrypt(conta.access_token_enc)}, timeout=30)
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(data)
    conta.access_token_enc = encrypt(data["access_token"])
    conta.token_expira_em = datetime.now(timezone.utc) + timedelta(
        seconds=data.get("expires_in", 5184000))
