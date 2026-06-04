# -*- coding: utf-8 -*-
"""auth.admin_routes — Admin UI pre správu užívateľov.

Endpoints (všetky vyžadujú admin rolu):
    GET  /admin/users              — zoznam užívateľov + akcie
    GET  /admin/users/edit?id=N    — editor (alebo nový ak id=0)
    POST /admin/users/save         — vytvor/aktualizuj
    POST /admin/users/delete       — zmaže usera + jeho sessions
    POST /admin/users/access       — priradenie profilu (Trading/Zákazník)

Integrácia v app.py: register_admin_routes(app) — automaticky volané z routes.py
"""
from __future__ import annotations
import html as _html
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from db import get_session
from db.models import User, Profile, UserProfileAccess, AuthSession, AuditLog
from .passwords import hash_password
from .permissions import require_role, ROLE_ADMIN, VALID_ROLES


_ADMIN_CSS = """<style>
body{font-family:-apple-system,Segoe UI,Arial;max-width:1280px;margin:24px auto;padding:0 16px;color:#222}
h1{color:#1F4E78;font-size:22px;margin:0 0 6px}
.bar{display:flex;justify-content:space-between;align-items:center;margin:16px 0;flex-wrap:wrap;gap:8px}
.btn{display:inline-block;background:#1F4E78;color:#fff;padding:8px 14px;border-radius:7px;
  text-decoration:none;font-size:14px;border:0;cursor:pointer;font-weight:500}
.btn:hover{background:#16395a}
.btn.sec{background:#fff;color:#1F4E78;border:1px solid #1F4E78}
.btn.red{background:#C62828}
.btn.gr{background:#2E7D32}
table{border-collapse:collapse;width:100%;font-size:14px;background:#fff;border-radius:8px;overflow:hidden;
  box-shadow:0 1px 3px rgba(0,0,0,.06)}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid #eef1f6}
th{background:#1F4E78;color:#fff;font-weight:600;font-size:13px}
tr:hover{background:#f8fafc}
.chip{display:inline-block;background:#e8f4fd;color:#1F4E78;padding:2px 9px;border-radius:12px;
  font-size:11px;font-weight:600}
.chip.admin{background:#fbeaea;color:#7a1810}
.chip.trading{background:#fff3cd;color:#7a5d00}
.chip.obchodnik{background:#e6f4ea;color:#1B5E20}
.chip.zakaznik{background:#e8eaf6;color:#283593}
.muted{color:#888;font-size:12px}
fieldset{border:1px solid #e3e8ef;border-radius:10px;margin:14px 0;padding:14px 18px}
legend{color:#1F4E78;font-weight:600;font-size:14px}
label{display:block;font-size:13px;color:#444;margin:10px 0 4px;font-weight:500}
input,select{width:100%;padding:8px 10px;border:1px solid #ccc;border-radius:6px;font-size:14px;
  box-sizing:border-box;max-width:400px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.warn{background:#fff3cd;border-left:3px solid #f0b80f;padding:8px 14px;border-radius:6px;margin:10px 0;
  color:#7a5c00;font-size:13px}
.ok{background:#e8f5e9;border-left:3px solid #2E7D32;padding:8px 14px;border-radius:6px;margin:10px 0;
  color:#1B5E20;font-size:13px}
.tbl-comp{font-size:13px}
.tbl-comp th,.tbl-comp td{padding:6px 10px}
</style>"""


def _admin_nav(user: dict, current: str = "users") -> str:
    """Mini nav pre admin sekciu."""
    return (
        f'<div style="background:#fff;padding:10px 16px;border-radius:8px;'
        f'box-shadow:0 1px 3px rgba(0,0,0,.06);margin-bottom:16px;'
        f'display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap">'
        f'<div><b style="color:#1F4E78">⚙ Admin</b> · '
        f'<a href="/admin/users" style="margin:0 8px;color:#1F4E78;text-decoration:none">Užívatelia</a> · '
        f'<a href="/admin/audit_log" style="margin:0 8px;color:#1F4E78;text-decoration:none">Audit log</a> · '
        f'<a href="/" style="margin:0 8px;color:#666;text-decoration:none">← Späť na appku</a></div>'
        f'<div style="font-size:13px;color:#666">'
        f'{_html.escape(user.get("username", ""))} '
        f'<span class="chip {user.get("role","")}">{user.get("role","")}</span>'
        f' · <a href="/logout" style="color:#C62828">Odhlásiť</a></div>'
        f'</div>'
    )


def register_admin_routes(app: FastAPI) -> None:
    """Zaregistruje /admin/users CRUD."""

    @app.get("/admin/users", response_class=HTMLResponse)
    @require_role(ROLE_ADMIN)
    def admin_users_list(request: Request):
        user = getattr(request.state, "user", None) or {}
        with get_session() as s:
            users = s.query(User).order_by(User.id).all()
            rows = ""
            for u in users:
                role_html = f'<span class="chip {u.role}">{u.role}</span>'
                active = "✓" if u.is_active else "✗"
                last = u.last_login or "—"
                rows += (
                    f'<tr><td>{u.id}</td>'
                    f'<td><b>{_html.escape(u.username)}</b></td>'
                    f'<td>{role_html}</td>'
                    f'<td>{active}</td>'
                    f'<td class="muted">{_html.escape(u.created_at or "—")}</td>'
                    f'<td class="muted">{_html.escape(last)}</td>'
                    f'<td><a class="btn sec" href="/admin/users/edit?id={u.id}">Upraviť</a>'
                    f' <a class="btn sec" href="/admin/users/access?id={u.id}">Profily</a></td>'
                    f'</tr>'
                )
        body = (
            f'<!doctype html><html lang="sk"><head><meta charset="utf-8">'
            f'<title>Admin · Užívatelia</title>{_ADMIN_CSS}</head><body>'
            f'{_admin_nav(user)}'
            f'<div class="bar"><h1>👥 Užívatelia</h1>'
            f'<a class="btn gr" href="/admin/users/edit?id=0">+ Nový užívateľ</a></div>'
            f'<table><tr><th>ID</th><th>Username</th><th>Rola</th><th>Aktívny</th>'
            f'<th>Vytvorený</th><th>Posledný login</th><th>Akcie</th></tr>'
            f'{rows}</table>'
            f'</body></html>'
        )
        return HTMLResponse(body)

    @app.get("/admin/users/edit", response_class=HTMLResponse)
    @require_role(ROLE_ADMIN)
    def admin_users_edit(request: Request, id: int = 0):
        is_new = (id == 0)
        user = getattr(request.state, "user", None) or {}
        with get_session() as s:
            target = None
            if not is_new:
                target = s.query(User).filter_by(id=id).one_or_none()
                if target is None:
                    raise HTTPException(404, "User neexistuje")
            target_name = target.username if target else ""
            target_role = target.role if target else "trading"
            target_email = (target.email if target else "") or ""
            target_active = target.is_active if target else True
        role_options = "".join(
            f'<option value="{r}"{" selected" if r==target_role else ""}>{r}</option>'
            for r in VALID_ROLES
        )
        body = (
            f'<!doctype html><html lang="sk"><head><meta charset="utf-8">'
            f'<title>Admin · {"Nový" if is_new else "Upraviť"} užívateľ</title>'
            f'{_ADMIN_CSS}</head><body>'
            f'{_admin_nav(user)}'
            f'<h1>{"+ Nový užívateľ" if is_new else f"Upraviť: {_html.escape(target_name)}"}</h1>'
            f'<form method="post" action="/admin/users/save">'
            f'<input type="hidden" name="id" value="{id}">'
            f'<fieldset><legend>Základné údaje</legend>'
            f'<div class="row">'
            f'<div><label>Užívateľské meno</label>'
            f'<input name="username" value="{_html.escape(target_name)}" '
            f'{"" if is_new else "readonly"} required></div>'
            f'<div><label>Rola</label><select name="role">{role_options}</select></div>'
            f'</div>'
            f'<div class="row">'
            f'<div><label>E-mail (voliteľné)</label>'
            f'<input name="email" type="email" value="{_html.escape(target_email)}"></div>'
            f'<div><label>Aktívny</label>'
            f'<select name="is_active"><option value="1"{" selected" if target_active else ""}>✓ Áno</option>'
            f'<option value="0"{" selected" if not target_active else ""}>✗ Nie</option></select></div>'
            f'</div>'
            f'</fieldset>'
            f'<fieldset><legend>Heslo</legend>'
            f'<div class="warn">'
            f'{"Pri novom užívateľovi musí byť heslo zadané." if is_new else "Nechaj prázdne ak nechceš meniť heslo."}'
            f'</div>'
            f'<label>Nové heslo</label>'
            f'<input name="password" type="password" {"required" if is_new else ""}>'
            f'</fieldset>'
            f'<div style="margin-top:18px">'
            f'<button class="btn" type="submit">Uložiť</button> '
            f'<a class="btn sec" href="/admin/users">Zrušiť</a>'
            f'</div></form>'
        )
        if not is_new:
            del_form = (
                f'<form method="post" action="/admin/users/delete" '
                f'style="margin-top:24px" '
                f'onsubmit="return confirm(\'Naozaj zmazať tohto užívateľa?\')">'
                f'<input type="hidden" name="id" value="{id}">'
                f'<button class="btn red" type="submit">🗑 Zmazať užívateľa</button>'
                f'</form>'
            )
            body += del_form
        body += '</body></html>'
        return HTMLResponse(body)

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
                # Nový user
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
                    # Pri zmene hesla revoke všetky sessions
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
            # Sessions + UserProfileAccess sa zmažu cascade
        return RedirectResponse("/admin/users", status_code=302)

    @app.get("/admin/users/access", response_class=HTMLResponse)
    @require_role(ROLE_ADMIN)
    def admin_user_access_get(request: Request, id: int):
        user = getattr(request.state, "user", None) or {}
        with get_session() as s:
            target = s.query(User).filter_by(id=id).one_or_none()
            if target is None:
                raise HTTPException(404, "User neexistuje")
            target_name = target.username
            target_role = target.role
            # Aktuálne priradené profily
            accesses = {a.profile_id: a for a in
                          s.query(UserProfileAccess).filter_by(user_id=id).all()}
            profiles = s.query(Profile).order_by(Profile.name).all()
            rows = ""
            for p in profiles:
                a = accesses.get(p.id)
                cr = "checked" if a and a.can_read else ""
                cw = "checked" if a and a.can_write else ""
                ch = "checked" if a and a.can_write_hw else ""
                mode_chip = (f'<span class="chip {p.mode}">{p.mode}</span>')
                rows += (
                    f'<tr><td><b>{_html.escape(p.name)}</b> {mode_chip}</td>'
                    f'<td style="text-align:center"><input type="checkbox" name="read_{p.id}" {cr}></td>'
                    f'<td style="text-align:center"><input type="checkbox" name="write_{p.id}" {cw}></td>'
                    f'<td style="text-align:center"><input type="checkbox" name="hw_{p.id}" {ch}></td>'
                    f'</tr>'
                )
        info = ""
        if target_role == ROLE_ADMIN:
            info = '<div class="warn">⚠ Admin má prístup ku všetkým profilom automaticky — toto nastavenie sa ignoruje.</div>'
        elif target_role == "obchodnik":
            info = '<div class="warn">⚠ Obchodník vidí všetky profily read-only — toto nastavenie sa ignoruje pre read.</div>'
        body = (
            f'<!doctype html><html lang="sk"><head><meta charset="utf-8">'
            f'<title>Admin · Prístup k profilom</title>{_ADMIN_CSS}</head><body>'
            f'{_admin_nav(user)}'
            f'<h1>Prístup k profilom: {_html.escape(target_name)} '
            f'<span class="chip {target_role}">{target_role}</span></h1>'
            f'{info}'
            f'<form method="post" action="/admin/users/access">'
            f'<input type="hidden" name="user_id" value="{id}">'
            f'<table class="tbl-comp"><tr><th>Profil</th><th>Read</th><th>Write</th>'
            f'<th>HW Write (Bender)</th></tr>{rows}</table>'
            f'<div style="margin-top:18px">'
            f'<button class="btn" type="submit">Uložiť prístupy</button> '
            f'<a class="btn sec" href="/admin/users">← Späť</a></div>'
            f'</form></body></html>'
        )
        return HTMLResponse(body)

    @app.get("/admin/audit_log", response_class=HTMLResponse)
    @require_role(ROLE_ADMIN)
    def admin_audit_log(request: Request, limit: int = 200):
        user = getattr(request.state, "user", None) or {}
        with get_session() as s:
            rows_db = (s.query(AuditLog)
                          .order_by(AuditLog.id.desc())
                          .limit(min(max(limit, 1), 2000)).all())
            user_lookup = {u.id: u.username for u in s.query(User).all()}
            rows = ""
            for r in rows_db:
                uname = user_lookup.get(r.user_id, "—")
                d = r.details or {}
                status = d.get("status", "?")
                method = d.get("method", "?")
                status_color = "#2E7D32" if 200 <= int(status or 0) < 400 else "#C62828"
                rows += (
                    f'<tr>'
                    f'<td class="muted">{_html.escape(r.ts or "")}</td>'
                    f'<td><b>{_html.escape(uname)}</b></td>'
                    f'<td><span style="font-family:monospace;font-size:12px">'
                    f'{_html.escape(method)} {_html.escape(d.get("path",""))}</span></td>'
                    f'<td style="color:{status_color};font-weight:600">{status}</td>'
                    f'<td class="muted">{_html.escape(r.ip_address or "—")}</td>'
                    f'</tr>'
                )
        body = (
            f'<!doctype html><html lang="sk"><head><meta charset="utf-8">'
            f'<title>Admin · Audit log</title>{_ADMIN_CSS}</head><body>'
            f'{_admin_nav(user)}'
            f'<div class="bar"><h1>📋 Audit log</h1>'
            f'<span class="muted">posledných {min(limit, 2000)} záznamov</span></div>'
            f'<table class="tbl-comp"><tr><th>Čas</th><th>User</th><th>Akcia</th>'
            f'<th>Status</th><th>IP</th></tr>{rows}</table>'
            f'</body></html>'
        )
        return HTMLResponse(body)

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
            # Existing accesses
            existing = {a.profile_id: a for a in
                          s.query(UserProfileAccess).filter_by(user_id=user_id).all()}
            for p in profiles:
                has_read = f"read_{p.id}" in form
                has_write = f"write_{p.id}" in form
                has_hw = f"hw_{p.id}" in form
                acc = existing.get(p.id)
                if not (has_read or has_write or has_hw):
                    # Nič → odstránenie ak existuje
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
