# -*- coding: utf-8 -*-
"""auth.permissions — role-based access control + per-profile access.

Role:
    admin     — full access
    obchodnik — vidí všetko read-only, žiadne write do externých systémov
    trading   — generuje plány, pracuje s priradenými profilmi (real ak má unlock)
    zakaznik  — read-only, vidí iba priradené profily, žiadne nastavenia

Use:
    @app.post("/plan")
    @require_role("admin", "trading")
    def plan(...): ...

    @app.post("/realio/write")
    @require_role("admin", "trading")
    @require_profile_access(hw_write=True)
    def realio_write(profile: str, ...): ...

current_user() vracia dict z request.state alebo None (ak nie je auth alebo unset).
"""
from __future__ import annotations
import os
from functools import wraps
from typing import Optional, Callable
from fastapi import HTTPException, Request


# Konstanty rolí — keep in sync s db/models.User CheckConstraint
ROLE_ADMIN = "admin"
ROLE_OBCHODNIK = "obchodnik"
ROLE_TRADING = "trading"
ROLE_ZAKAZNIK = "zakaznik"

VALID_ROLES = (ROLE_ADMIN, ROLE_OBCHODNIK, ROLE_TRADING, ROLE_ZAKAZNIK)


def _auth_required() -> bool:
    """Vráti True ak AUTH_REQUIRED=1 v env. Inak všetky decorators sú no-op."""
    return os.environ.get("AUTH_REQUIRED", "0").strip() in ("1", "true", "True", "yes")


def current_user(request: Optional[Request] = None) -> Optional[dict]:
    """Vráti aktuálneho užívateľa z request.state alebo None.

    Volaj iba v rámci request handleru. Mimo handlera (cron, CLI) vráti None.
    """
    if request is None:
        # Pokús sa nájsť request cez stack inspection (FastAPI dependency)
        return None
    user = getattr(request.state, "user", None)
    return user if isinstance(user, dict) else None


def require_role(*roles: str) -> Callable:
    """Decorator: vyžaduje že prihlásený user má jednu z `roles`.

    Pri AUTH_REQUIRED=0 je decorator no-op (back-compat).
    Pri AUTH_REQUIRED=1 a chýbajúcom user-ovi → 401, pri zlej roli → 403.
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not _auth_required():
                return fn(*args, **kwargs)
            # Hľadaj Request v kwargs alebo args
            req = kwargs.get("request")
            if req is None:
                for a in args:
                    if isinstance(a, Request):
                        req = a
                        break
            user = current_user(req)
            if user is None:
                raise HTTPException(status_code=401, detail="Neprihlásený")
            if user.get("role") not in roles:
                raise HTTPException(
                    status_code=403,
                    detail=f"Tvoja rola '{user.get('role')}' nemá oprávnenie na túto akciu."
                )
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def has_profile_access(user_id: int, profile_name: str,
                         write: bool = False, hw_write: bool = False) -> bool:
    """Vráti True ak user má prístup k profilu (s requested flagmi).

    Admin: vždy True.
    Obchodnik: vidí všetky profily read-only (True ak write=False a hw_write=False).
    Trading + Zakaznik: musia mať UserProfileAccess záznam.
    """
    if not _auth_required():
        return True
    from db import get_session
    from db.models import User, Profile, UserProfileAccess
    with get_session() as s:
        user = s.query(User).filter_by(id=user_id, is_active=True).one_or_none()
        if user is None:
            return False
        # Admin bypass
        if user.role == ROLE_ADMIN:
            return True
        # Obchodnik: read-only access ku všetkým profilom
        if user.role == ROLE_OBCHODNIK:
            return not (write or hw_write)
        # Trading + Zakaznik: per-profile check
        prof = s.query(Profile).filter_by(name=profile_name).one_or_none()
        if prof is None:
            return False
        access = s.query(UserProfileAccess).filter_by(
            user_id=user.id, profile_id=prof.id
        ).one_or_none()
        if access is None:
            return False
        if not access.can_read:
            return False
        if write and not access.can_write:
            return False
        if hw_write and not access.can_write_hw:
            return False
        # Zákazník nemôže writeovať nikdy (defense in depth)
        if user.role == ROLE_ZAKAZNIK and (write or hw_write):
            return False
        return True


def require_profile_access(*, write: bool = False, hw_write: bool = False,
                              profile_arg: str = "profile") -> Callable:
    """Decorator: vyžaduje že user má prístup k profilu (z form/query parametra).

    Args:
        write: True ak akcia mení profil/jeho data
        hw_write: True ak akcia píše do HW (realio Bender, OKTE order)
        profile_arg: meno parametra v handleri ktorý nesie meno profilu

    Pri AUTH_REQUIRED=0 je no-op.
    Pri AUTH_REQUIRED=1: kontroluje cez has_profile_access().
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not _auth_required():
                return fn(*args, **kwargs)
            req = kwargs.get("request")
            if req is None:
                for a in args:
                    if isinstance(a, Request):
                        req = a
                        break
            user = current_user(req)
            if user is None:
                raise HTTPException(status_code=401, detail="Neprihlásený")
            profile_name = kwargs.get(profile_arg, "")
            if not profile_name:
                # Skús resolve cez plan_store
                try:
                    import plan_store as _ps
                    profile_name = _ps.resolve_profile()
                except Exception:
                    pass
            if not has_profile_access(user["id"], profile_name,
                                         write=write, hw_write=hw_write):
                raise HTTPException(
                    status_code=403,
                    detail=f"Nemáš prístup k profilu '{profile_name}'."
                )
            return fn(*args, **kwargs)
        return wrapper
    return decorator
