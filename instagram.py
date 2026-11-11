import os
import time
import httpx

GRAPH_BASE = os.getenv("GRAPH_BASE", "https://graph.instagram.com/v21.0")


def _check(r: httpx.Response) -> dict:
    data = r.json()
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError(data.get("error", data))
    return data


def publicar_imagem(ig_user_id: str, token: str, image_url: str, legenda: str) -> str:
    with httpx.Client(timeout=30) as c:
        # 1) cria o container de midia
        cont = _check(c.post(
            f"{GRAPH_BASE}/{ig_user_id}/media",
            data={"image_url": image_url, "caption": legenda or "", "access_token": token},
        ))
        creation_id = cont["id"]

        # 2) espera o container ficar pronto
        for _ in range(10):
            st = _check(c.get(
                f"{GRAPH_BASE}/{creation_id}",
                params={"fields": "status_code", "access_token": token},
            ))
            if st.get("status_code") == "FINISHED":
                break
            if st.get("status_code") == "ERROR":
                raise RuntimeError("Container com erro na Meta")
            time.sleep(2)

        # 3) publica
        pub = _check(c.post(
            f"{GRAPH_BASE}/{ig_user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": token},
        ))
        return pub["id"]
