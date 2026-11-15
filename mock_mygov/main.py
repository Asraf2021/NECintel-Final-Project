"""Simulyasiya olunmuş myGov identifikasiya provayderi (OAuth2 Authorization Code + PKCE, OIDC üslubu).

Real myGov inteqrasiyası rəsmi qoşulma (client qeydiyyatı, müqavilə, açarlar) tələb edir — bu modul onun
yerini prototipdə tutur. Əsas tətbiq provayderlə yalnız standart OAuth2/OIDC addımları ilə danışır
(authorize → token → userinfo), ona görə real provayderə keçid konfiqurasiya ilə edilir
(app/services/identity.py, UCE_MYGOV_MODE=oidc).

Bu səhifə real myGov-un dizaynını və loqosunu təqlid etmir; açıq şəkildə "simulyasiya" kimi işarələnib.
Test şəxslərinin bütün məlumatları uydurmadır.
"""
from __future__ import annotations

import base64
import hashlib
import html
import secrets
import time
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

CLIENT_ID = "uce-demo-client"
CLIENT_SECRET = "uce-demo-secret"
CODE_TTL_S = 120
TOKEN_TTL_S = 600
CALLBACK_PATH = "/api/v1/auth/mygov/callback"

# Uydurma test şəxsləri (real şəxslərə aid deyil)
PERSONAS = {
    "p1": {"sub": "mg-0001", "pin": "7ABC1D2", "given_name": "Aysel", "family_name": "Quliyeva", "birthdate": "1988-04-12",
           "phone": "+994 50 000 00 01",
           "disability": {"registered": True, "group": "II", "category": "mobility", "valid_until": "2028-12-31",
                          "certificate_no": "ƏL-2024-000123"}},
    "p2": {"sub": "mg-0002", "pin": "5XYZ9K1", "given_name": "Elvin", "family_name": "Sadıqov", "birthdate": "1995-09-03",
           "phone": "+994 55 000 00 02", "disability": {"registered": False}},
    "p3": {"sub": "mg-0003", "pin": "3LMN4P8", "given_name": "Gülnarə", "family_name": "Həsənova", "birthdate": "1951-01-27",
           "phone": "+994 70 000 00 03",
           "disability": {"registered": True, "group": "I", "category": "mobility", "valid_until": "Müddətsiz",
                          "certificate_no": "ƏL-2019-004567"}},
}


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def create_mock_mygov_app() -> FastAPI:
    app = FastAPI(title="myGov (simulyasiya) — identity provider", docs_url=None, redoc_url=None, openapi_url=None)
    codes: dict[str, dict] = {}
    tokens: dict[str, dict] = {}
    app.state.codes, app.state.tokens = codes, tokens

    def _check_redirect(redirect_uri: str) -> None:
        # Yalnız qeydiyyatdan keçmiş geri qayıdış ünvanı (open redirect-ə qarşı)
        if not redirect_uri.startswith(("http://", "https://")) or not redirect_uri.split("?")[0].endswith(CALLBACK_PATH):
            raise HTTPException(400, "invalid_redirect_uri")

    @app.get("/authorize", response_class=HTMLResponse)
    def authorize(client_id: str, redirect_uri: str, state: str, code_challenge: str,
                  code_challenge_method: str = "S256", scope: str = "openid profile disability"):
        if client_id != CLIENT_ID:
            raise HTTPException(400, "invalid_client")
        if code_challenge_method != "S256" or len(code_challenge) < 43:
            raise HTTPException(400, "pkce_required")
        _check_redirect(redirect_uri)
        hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in
                         (("redirect_uri", redirect_uri), ("state", state), ("code_challenge", code_challenge)))
        cards = ""
        for pid, p in PERSONAS.items():
            dis = p["disability"]
            badge = (f'<span class="b warn">Əlillik: {html.escape(dis["group"])} qrup</span>' if dis["registered"]
                     else '<span class="b">Əlillik qeydi yoxdur</span>')
            cards += (f'<button class="card" type="submit" name="persona" value="{pid}"><b>{html.escape(p["given_name"])} '
                      f'{html.escape(p["family_name"])}</b><span>FİN: {html.escape(p["pin"])} · {html.escape(p["birthdate"])}</span>{badge}</button>')
        page = f"""<!doctype html><html lang="az"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>myGov (simulyasiya) — giriş</title><style>
body{{margin:0;font-family:Segoe UI,system-ui,Arial,sans-serif;background:#EEF1F5;color:#14202E;display:grid;place-items:center;min-height:100vh;padding:16px}}
.box{{background:#fff;border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.1);padding:24px;width:min(440px,100%)}}
.sim{{display:inline-block;background:#FFF4DC;color:#6B4700;font-weight:700;font-size:12px;padding:4px 10px;border-radius:999px;margin-bottom:10px}}
h1{{font-size:20px;margin:0 0 6px}} p{{color:#4A5868;font-size:14px;margin:0 0 16px}}
.card{{display:grid;gap:4px;width:100%;text-align:left;border:1px solid #E1E6EC;background:#fff;border-radius:12px;padding:12px 14px;margin-bottom:10px;cursor:pointer;font:inherit}}
.card:hover{{border-color:#2563EB}} .card span{{font-size:13px;color:#4A5868}}
.b{{display:inline-block;width:max-content;font-size:12px;font-weight:700;padding:2px 8px;border-radius:999px;background:#EEF1F5}}
.b.warn{{background:#EDE7FB;color:#5B21B6}} .scope{{font-size:12px;color:#7B8794;margin-top:12px}}
</style></head><body><form class="box" method="post" action="authorize">
<span class="sim">SİMULYASİYA — real myGov deyil</span><h1>myGov ilə giriş</h1>
<p>«Təhlükəsiz Təxliyə» sistemi sizin şəxsi məlumatlarınıza və əlillik qeydinizə çıxış istəyir. Test şəxsini seçin:</p>
{hidden}{cards}<div class="scope">İcazələr: {html.escape(scope)}</div></form></body></html>"""
        return HTMLResponse(page)

    @app.post("/authorize")
    def approve(persona: str = Form(...), redirect_uri: str = Form(...), state: str = Form(...),
                code_challenge: str = Form(...)):
        if persona not in PERSONAS:
            raise HTTPException(400, "unknown_persona")
        _check_redirect(redirect_uri)
        code = secrets.token_urlsafe(24)
        codes[code] = {"persona": persona, "challenge": code_challenge, "redirect_uri": redirect_uri,
                       "exp": time.time() + CODE_TTL_S}
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(redirect_uri + sep + urlencode({"code": code, "state": state}), status_code=303)

    @app.post("/token")
    async def token(request: Request):
        form = dict(await request.form()) if "form" in request.headers.get("content-type", "") else await request.json()
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        entry = codes.pop(form.get("code", ""), None)  # kod birdəfəlikdir
        if not entry or entry["exp"] < time.time() or entry["redirect_uri"] != form.get("redirect_uri"):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if _s256(form.get("code_verifier", "")) != entry["challenge"]:
            return JSONResponse({"error": "invalid_grant", "error_description": "pkce_mismatch"}, status_code=400)
        access = secrets.token_urlsafe(32)
        tokens[access] = {"persona": entry["persona"], "exp": time.time() + TOKEN_TTL_S}
        return {"access_token": access, "token_type": "Bearer", "expires_in": TOKEN_TTL_S, "scope": "openid profile disability"}

    @app.get("/userinfo")
    def userinfo(authorization: str = Header(default="")):
        t = tokens.get(authorization[7:] if authorization.lower().startswith("bearer ") else "")
        if not t or t["exp"] < time.time():
            raise HTTPException(401, "invalid_token")
        return PERSONAS[t["persona"]]

    return app
