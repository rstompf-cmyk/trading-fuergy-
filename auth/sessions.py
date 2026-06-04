# -*- coding: utf-8 -*-
"""auth.sessions — server-side sessions cez DB tabuľku AuthSession.

Cookie obsahuje iba random token; server-side validuje cez DB lookup.
Tokeny sú bezpečné random 32-byte hex (64 znakov).

Lifecycle:
    create_session(user_id, ip, ua) → token (zapíše do DB, vráti pre cookie)
    validate_session(token) → User alebo None (renews last_seen)
    revoke_session(token) → odstráni z DB (logout)

Expiry:
    SESSION_TTL_DAYS=7 (configurable env). Po expiry validate_session vráti None.
"""
from __future__ import annotations
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional


COOKIE_NAME = "ftv_session"
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "7"))


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _expires_iso(days: int = SESSION_TTL_DAYS) -> str:
    return (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")


def create_session(user_id: int, ip_address: str = "", user_agent: str = "") -> str:
    """Vytvor novú session, vráti token (zapíš do cookie).

    Args:
        user_id: ID prihláseného užívateľa
        ip_address: client IP (optional, pre audit log)
        user_agent: client browser/CLI string

    Returns:
        token: 64-znakový hex string (zapíš do cookie ako COOKIE_NAME)
    """
    from db import get_session
    from db.models import AuthSession
    token = secrets.token_hex(32)   # 64 hex chars = 256 bits entropy
    with get_session() as s:
        s.add(AuthSession(
            user_id=user_id, token=token,
            created_at=_now_iso(),
            expires_at=_expires_iso(),
            last_seen=_now_iso(),
            ip_address=ip_address or None,
            user_agent=(user_agent or None) and user_agent[:1000],
        ))
    return token


def validate_session(token: Optional[str]) -> Optional[dict]:
    """Validuj session token. Pri úspechu vráti dict s user info, inak None.

    Renew last_seen pri každom validate (sliding session).

    Returns:
        dict {id, username, role, email, is_active} alebo None
    """
    if not token or len(token) != 64:
        return None
    from db import get_session
    from db.models import AuthSession, User
    now = datetime.now()
    with get_session() as s:
        sess = s.query(AuthSession).filter_by(token=token).one_or_none()
        if sess is None:
            return None
        # Expiry check
        try:
            expires = datetime.fromisoformat(sess.expires_at)
            if expires < now:
                # Auto-cleanup expired session
                s.delete(sess)
                return None
        except (ValueError, TypeError):
            return None
        # Lookup user
        user = s.query(User).filter_by(id=sess.user_id, is_active=True).one_or_none()
        if user is None:
            return None
        # Renew last_seen
        sess.last_seen = _now_iso()
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "email": user.email,
            "is_active": user.is_active,
        }


def revoke_session(token: Optional[str]) -> bool:
    """Odstráň session (logout). True ak existovala."""
    if not token:
        return False
    from db import get_session
    from db.models import AuthSession
    with get_session() as s:
        sess = s.query(AuthSession).filter_by(token=token).one_or_none()
        if sess is None:
            return False
        s.delete(sess)
    return True


def cleanup_expired_sessions() -> int:
    """Hromadne zmaž všetky expirated sessions. Vráti počet zmazaných.

    Volaj periodicky (napr. raz denne cez APScheduler).
    """
    from db import get_session
    from db.models import AuthSession
    now_iso = _now_iso()
    with get_session() as s:
        rows = s.query(AuthSession).filter(AuthSession.expires_at < now_iso).all()
        n = len(rows)
        for r in rows:
            s.delete(r)
    return n
