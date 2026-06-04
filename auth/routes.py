# -*- coding: utf-8 -*-
"""auth.routes — Login + logout endpointy + session middleware.

Integrácia v app.py:
    from auth.routes import register_auth_routes, AuthMiddleware
    register_auth_routes(app)
    app.add_middleware(AuthMiddleware)

Routes:
    GET  /login   — HTML form
    POST /login   — validate credentials, set cookie, redirect na ?next
    GET  /logout  — revoke session, clear cookie, redirect na /login
    GET  /me      — JSON: current user info (debug/profil chip)

Middleware:
    AuthMiddleware:
      - AUTH_REQUIRED=0 (default) → no-op, prepustí všetko
      - AUTH_REQUIRED=1 → cookie → validate → set request.state.user
        - public allowlist (/login, /logout, /static, /health, /favicon.ico) bez auth
        - bez sessions → redirect na /login?next=<url>
"""
from __future__ import annotations
import os
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from db import get_session
from db.models import User
from .passwords import verify_password
from .sessions import (
    COOKIE_NAME, create_session, validate_session, revoke_session,
    SESSION_TTL_DAYS,
)
from . import AUTH_REQUIRED


# Verejné cesty (žiadny auth potrebný)
PUBLIC_PATHS = ("/login", "/logout", "/static", "/health", "/favicon.ico", "/me")


def _is_public(path: str) -> bool:
    """Vráti True ak path je v public allowlist (bez auth)."""
    return any(path.startswith(p) for p in PUBLIC_PATHS)


class AuthMiddleware(BaseHTTPMiddleware):
    """Session middleware. AUTH_REQUIRED=0 → no-op."""

    async def dispatch(self, request: Request, call_next):
        # Vždy nastav user na základe cookie (aj pre public paths — napr. /me potrebuje)
        token = request.cookies.get(COOKIE_NAME)
        user = validate_session(token) if token else None
        request.state.user = user

        # Feature flag check — bez AUTH_REQUIRED=1 prepustíme všetko bez gatingu
        if not _auth_required_now():
            return await call_next(request)

        # Public allowlist (login, logout, static, /me, /health)
        path = request.url.path
        if _is_public(path):
            return await call_next(request)

        # Gating: ak nie je session, redirect na login
        if user is None:
            next_url = str(request.url.path)
            if request.url.query:
                next_url += "?" + request.url.query
            from urllib.parse import quote
            return RedirectResponse(
                f"/login?next={quote(next_url)}", status_code=302
            )

        return await call_next(request)


def _auth_required_now() -> bool:
    """Runtime check (umožňuje switch bez reštartu pri dev/test)."""
    return os.environ.get("AUTH_REQUIRED", "0").strip() in ("1", "true", "True", "yes")


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint handlers
# ─────────────────────────────────────────────────────────────────────────────

def _login_page(error: str = "", next_url: str = "/",
                 request: Optional["Request"] = None) -> HTMLResponse:
    """Render login stránky cez Jinja2 template (Fáza 3 refactor)."""
    from ui.templates import render
    return render(request, "pages/login.html",
                   error=error, next_url=next_url or "/")


def register_auth_routes(app: FastAPI) -> None:
    """Zaregistruje /login, /logout, /me v aplikácii."""

    @app.get("/login", response_class=HTMLResponse)
    def login_get(request: Request, next: str = "/"):
        # Ak už je prihlásený, redirect rovno na next
        if not _auth_required_now():
            return RedirectResponse(next or "/", status_code=302)
        token = request.cookies.get(COOKIE_NAME)
        if token and validate_session(token):
            return RedirectResponse(next or "/", status_code=302)
        return _login_page(next_url=next, request=request)

    @app.post("/login", response_class=HTMLResponse)
    def login_post(request: Request,
                    username: str = Form(...),
                    password: str = Form(...),
                    next: str = Form(default="/")):
        # Lookup user
        with get_session() as s:
            user = s.query(User).filter_by(username=username, is_active=True).one_or_none()
            if user is None or not verify_password(password, user.password_hash):
                return _login_page(error="Nesprávne meno alebo heslo.",
                                      next_url=next, request=request)
            user_id = user.id
            # Update last_login
            user.last_login = datetime.now().isoformat(timespec="seconds")

        # Create session
        ip = request.client.host if request.client else ""
        ua = request.headers.get("user-agent", "")
        token = create_session(user_id, ip_address=ip, user_agent=ua)

        # Set cookie + redirect
        redirect = next or "/"
        if not redirect.startswith("/"):
            redirect = "/"   # safety: nedovoliť absolútne URL (open redirect)
        resp = RedirectResponse(redirect, status_code=302)
        resp.set_cookie(
            COOKIE_NAME, token,
            httponly=True, samesite="strict",
            max_age=SESSION_TTL_DAYS * 86400,
            secure=False,                # nastav True ak HTTPS-only
        )
        return resp

    @app.get("/logout")
    def logout(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if token:
            revoke_session(token)
        resp = RedirectResponse("/login", status_code=302)
        resp.delete_cookie(COOKIE_NAME)
        return resp

    @app.get("/me")
    def me(request: Request):
        """JSON s aktuálnym užívateľom — pre profil chip / debug."""
        user = getattr(request.state, "user", None)
        if user is None:
            return JSONResponse({"authenticated": False, "auth_required": _auth_required_now()})
        return JSONResponse({"authenticated": True, "user": user})

    # /admin/* endpointy
    try:
        from .admin_routes import register_admin_routes
        register_admin_routes(app)
    except Exception as _e:
        print(f"[auth] admin routes init zlyhal: {_e}")
