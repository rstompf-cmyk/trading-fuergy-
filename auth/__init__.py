# -*- coding: utf-8 -*-
"""auth — autentikácia, sessions, role-based permissions.

Verejné API:
    from auth import (
        hash_password, verify_password,
        create_session, validate_session, revoke_session,
        require_role, require_profile_access,
        current_user, AUTH_REQUIRED,
    )

AUTH_REQUIRED env flag (Fáza 2):
    AUTH_REQUIRED=0 (default) — appka beží bez auth, žiadny login (back-compat).
    AUTH_REQUIRED=1            — povinný login, role gating, /admin/users dostupné.

Tento opt-in režim umožňuje:
- main branch (port 8000) beží bez zmeny
- refactor-v2 + USE_DB=1 + AUTH_REQUIRED=0 → DB testing bez auth
- refactor-v2 + USE_DB=1 + AUTH_REQUIRED=1 → produkčný režim (Windows deploy)
"""
from __future__ import annotations
import os

# Hlavný feature flag — vypína celý auth systém ak False
AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "0").strip() in ("1", "true", "True", "yes")

from .passwords import hash_password, verify_password   # noqa: E402
from .sessions import create_session, validate_session, revoke_session, COOKIE_NAME   # noqa: E402
from .permissions import (   # noqa: E402
    require_role, require_profile_access, current_user,
    has_profile_access, ROLE_ADMIN, ROLE_OBCHODNIK, ROLE_TRADING, ROLE_ZAKAZNIK,
)

__all__ = [
    "AUTH_REQUIRED",
    "hash_password", "verify_password",
    "create_session", "validate_session", "revoke_session", "COOKIE_NAME",
    "require_role", "require_profile_access", "current_user",
    "has_profile_access",
    "ROLE_ADMIN", "ROLE_OBCHODNIK", "ROLE_TRADING", "ROLE_ZAKAZNIK",
]
