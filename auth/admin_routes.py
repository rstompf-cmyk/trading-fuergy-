# -*- coding: utf-8 -*-
"""auth.admin_routes — Admin UI pre správu užívateľov (Jinja2 verzia).

Endpoints (všetky vyžadujú admin rolu):
    GET  /admin/users              — zoznam užívateľov + akcie
    GET  /admin/users/edit?id=N    — editor (alebo nový ak id=0)
    POST /admin/users/save         — vytvor/aktualizuj
    POST /admin/users/delete       — zmaže usera + jeho sessions
    POST /admin/users/access       — priradenie profilu (Trading/Zákazník)
    GET  /admin/audit_log          — posledné audit záznamy

Integrácia v app.py: register_admin_routes(app) — automaticky volané z routes.py
"""
from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from db import get_session
from db.models import User, Profile, UserProfileAccess, AuthSession, AuditLog
from .passwords import hash_password
from .permissions import require_role, ROLE_ADMIN, VALID_ROLES
from ui.templates import render


def _user_to_dict(u: User) -> Dict[str, Any]:
    return {
        "id": u.id,
        "username": u.username,
        "email": u.email or "",
        "role": u.role,
        "is_active": bool(u.is_active),
        "created_at": u.created_at or "",
        "last_login": u.last_login or "",
    }


def register_admin_routes(app: FastAPI) -> None:
    """Zaregistruje /admin/users CRUD."""

    @app.get("/admin/users", response_class=HTMLResponse)
    @require_role(ROLE_ADMIN)
    def admin_users_list(request: Request):
        with get_session() as s:
            users = [_user_to_dict(u) for u in s.query(User).order_by(User.id).all()]
        return render(request, "admin/users_list.html", users=users)

    @app.get("/admin/users/edit", response_class=HTMLResponse)
    @require_role(ROLE_ADMIN)
    def admin_users_edit(request: Request, id: int = 0):
        is_new = (id == 0)
        target: Dict[str, Any] = {
            "id": 0, "username": "", "email": "", "role": "trading", "is_active": True,
        }
        if not is_new:
            with get_session() as s:
                u = s.query(User).filter_by(id=id).one_or_none()
                if u is None:
                    raise HTTPException(404, "User neexistuje")
                target = _user_to_dict(u)
        return render(request, "admin/users_edit.html",
                       is_new=is_new, target=target, valid_roles=VALID_ROLES)

    @app.post("/admin/users/save", response_class=HTMLResponse)
    @require_role(ROLE_ADMIN)
    def admin_users_save(request: Request,
                          id: int = Form(...),
                          username: str = Form(...),
                          role: str = Form(...),
                          email: str = Form(default=""),
                          is_active: str = Form(default="1"),
                          password: str = Form(default="")):
        if role not in VALID_ROLES:
            raise HTTPException(400, f"Neplatná rola: {role}")
        now_iso = datetime.now().isoformat(timespec="seconds")
        with get_session() as s:
            if id == 0:
                if not password or len(password) < 4:
                    raise HTTPException(400, "Heslo musí mať aspoň 4 znaky")
                if s.query(User).filter_by(username=username).first():
                    raise HTTPException(400, f"User '{username}' už existuje")
                s.add(User(
                    username=username, role=role, email=email or None,
                    password_hash=hash_password(password),
                    is_active=(is_active in ("1", "true")),
                    created_at=now_iso,
                ))
            else:
                target = s.query(User).filter_by(id=id).one_or_none()
                if target is None:
                    raise HTTPException(404, "User neexistuje")
                target.role = role
                target.email = email or None
                target.is_active = (is_active in ("1", "true"))
                if password:
                    if len(password) < 4:
                        raise HTTPException(400, "Heslo musí mať aspoň 4 znaky")
                    target.password_hash = hash_password(password)
                    s.query(AuthSession).filter_by(user_id=target.id).delete()
        return RedirectResponse("/admin/users", status_code=302)

    @app.post("/admin/users/delete", response_class=HTMLResponse)
    @require_role(ROLE_ADMIN)
    def admin_users_delete(request: Request, id: int = Form(...)):
        current_user = getattr(request.state, "user", None) or {}
        if current_user.get("id") == id:
            raise HTTPException(400, "Nemôžeš zmazať sám seba")
        with get_session() as s:
            target = s.query(User).filter_by(id=id).one_or_none()
            if target is None:
                raise HTTPException(404, "User neexistuje")
            s.delete(target)
        return RedirectResponse("/admin/users", status_code=302)

    @app.get("/admin/users/access", response_class=HTMLResponse)
    @require_role(ROLE_ADMIN)
    def admin_user_access_get(request: Request, id: int):
        with get_session() as s:
            u = s.query(User).filter_by(id=id).one_or_none()
            if u is None:
                raise HTTPException(404, "User neexistuje")
            target = _user_to_dict(u)
            access_rows = s.query(UserProfileAccess).filter_by(user_id=id).all()
            accesses: Dict[int, Dict[str, Any]] = {
                a.profile_id: {
                    "can_read": bool(a.can_read),
                    "can_write": bool(a.can_write),
                    "can_write_hw": bool(a.can_write_hw),
                } for a in access_rows
            }
            profiles: List[Dict[str, Any]] = [
                {"id": p.id, "name": p.name, "mode": p.mode or "simulation"}
                for p in s.query(Profile).order_by(Profile.name).all()
            ]
        return render(request, "admin/users_access.html",
                       target=target, profiles=profiles, accesses=accesses)

    @app.get("/admin/audit_log", response_class=HTMLResponse)
    @require_role(ROLE_ADMIN)
    def admin_audit_log(request: Request, limit: int = 200):
        eff_limit = min(max(int(limit or 200), 1), 2000)
        with get_session() as s:
            rows_db = (s.query(AuditLog)
                          .order_by(AuditLog.id.desc())
                          .limit(eff_limit).all())
            user_lookup = {u.id: u.username for u in s.query(User).all()}
            rows: List[Dict[str, Any]] = []
            for r in rows_db:
                d = r.details or {}
                try:
                    status_int = int(d.get("status", 0) or 0)
                except Exception:
                    status_int = 0
                rows.append({
                    "ts": r.ts or "",
                    "username": user_lookup.get(r.user_id, "—"),
                    "method": d.get("method", "?"),
                    "path": d.get("path", ""),
                    "status": d.get("status", "?"),
                    "status_ok": 200 <= status_int < 400,
                    "ip": r.ip_address or "",
                })
        return render(request, "admin/audit_log.html", rows=rows, limit=eff_limit)

    @app.post("/admin/users/access", response_class=HTMLResponse)
    @require_role(ROLE_ADMIN)
    async def admin_user_access_post(request: Request):
        form = await request.form()
        user_id = int(form.get("user_id", 0))
        admin_user = getattr(request.state, "user", None) or {}
        if user_id == 0:
            raise HTTPException(400, "user_id chýba")
        now_iso = datetime.now().isoformat(timespec="seconds")
        with get_session() as s:
            target = s.query(User).filter_by(id=user_id).one_or_none()
            if target is None:
                raise HTTPException(404, "User neexistuje")
            profiles = s.query(Profile).all()
            existing = {a.profile_id: a for a in
                          s.query(UserProfileAccess).filter_by(user_id=user_id).all()}
            for p in profiles:
                has_read = f"read_{p.id}" in form
                has_write = f"write_{p.id}" in form
                has_hw = f"hw_{p.id}" in form
                acc = existing.get(p.id)
                if not (has_read or has_write or has_hw):
                    if acc:
                        s.delete(acc)
                    continue
                if acc:
                    acc.can_read = has_read
                    acc.can_write = has_write
                    acc.can_write_hw = has_hw
                else:
                    s.add(UserProfileAccess(
                        user_id=user_id, profile_id=p.id,
                        can_read=has_read, can_write=has_write, can_write_hw=has_hw,
                        granted_at=now_iso,
                        granted_by=admin_user.get("id"),
                    ))
        return RedirectResponse(f"/admin/users/access?id={user_id}", status_code=302)
