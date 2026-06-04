# -*- coding: utf-8 -*-
"""auth.audit — middleware loguje POST/PUT/DELETE akcie do AuditLog tabuľky.

Zachytené:
    - method (POST, PUT, DELETE, PATCH)
    - path (URL bez query)
    - user_id (z request.state.user)
    - resource (heuristika: profile param, id param, atď.)
    - details (JSON: status_code, query, ip)
    - timestamp + ip

Audit log je read-only z UI cez /admin/audit_log (Admin only).

Pri AUTH_REQUIRED=0 middleware no-op.
"""
from __future__ import annotations
import os
import json
from datetime import datetime
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware


_LOG_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
# Endpoint-y ktoré preskočíme (log noise — login attempts apod.)
_SKIP_PATHS = {"/login", "/logout"}


def _auth_required_now() -> bool:
    return os.environ.get("AUTH_REQUIRED", "0").strip() in ("1", "true", "True", "yes")


class AuditLogMiddleware(BaseHTTPMiddleware):
    """Loguj write actions do DB. No-op ak AUTH_REQUIRED=0."""

    async def dispatch(self, request: Request, call_next):
        method = request.method.upper()
        path = request.url.path

        # Skip non-write metódy alebo blacklisted paths
        if method not in _LOG_METHODS or path in _SKIP_PATHS:
            return await call_next(request)
        if not _auth_required_now():
            return await call_next(request)

        # Process request
        response = await call_next(request)

        # Po response: vytvor log entry (best-effort, nezhodíme app pri chybe)
        try:
            self._write_log(request, response.status_code)
        except Exception as e:
            print(f"[AuditLogMiddleware] log write zlyhal: {e}")
        return response

    def _write_log(self, request: Request, status_code: int):
        from db import get_session
        from db.models import AuditLog
        user = getattr(request.state, "user", None)
        if not user:
            return   # neautorizovaní sa logovať netreba (žiadny user_id)
        # Heuristika resource: posledný path segment alebo query param
        path = request.url.path
        resource = path.split("/")[-1] if "/" in path else path
        # Pre /admin/users/save → resource = 'save', detail obsahuje úplnú cestu
        details = {
            "method": request.method,
            "path": path,
            "status": status_code,
            "query": dict(request.query_params) if request.query_params else {},
        }
        ip = request.client.host if request.client else ""
        action = f"{request.method} {path}"
        with get_session() as s:
            s.add(AuditLog(
                ts=datetime.now().isoformat(timespec="seconds"),
                user_id=user.get("id"),
                action=action[:64],
                resource=resource[:255],
                details=details,
                ip_address=ip[:64] or None,
            ))
