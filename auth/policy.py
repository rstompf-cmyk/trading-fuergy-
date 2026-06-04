# -*- coding: utf-8 -*-
"""auth.policy — Centrálna permission policy + middleware gating.

Namiesto rozsypania @require_role po celej app.py máme **jednu deklaráciu**
permission matrix tu, a PolicyMiddleware ju aplikuje automaticky podľa method+path.

Vzory ciest podporujú:
- Presnú zhodu: '/plan'
- Prefix: '/admin/'
- Wildcard: '/realio/*' (zhodí sa s /realio/save, /realio/write, atď.)

Per-pattern definícia obsahuje:
- allow: set rolí ktoré môžu (alebo '*' pre všetkých prihlásených)
- write: True ak je to write akcia (treba can_write na profil-bound endpointoch)
- hw_write: True pre HW write (Bender)
- profile_param: meno query/form parametra ktorý nesie profile name (default None)

Endpointy ktoré nie sú v matrix → default allow (back-compat, nezbiteľný režim).
Bez AUTH_REQUIRED=1 je celý middleware no-op.

VEREJNÉ:
- POLICY_MATRIX (list[dict]) — môžeš ho rozšíriť
- PolicyMiddleware (Starlette)
"""
from __future__ import annotations
import os
import re
from typing import Optional, Iterable
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .permissions import has_profile_access, ROLE_ADMIN, ROLE_OBCHODNIK, ROLE_TRADING, ROLE_ZAKAZNIK


# Symbolický set "všetkých prihlásených"
ALL_AUTH = frozenset({ROLE_ADMIN, ROLE_OBCHODNIK, ROLE_TRADING, ROLE_ZAKAZNIK})
ADMIN_TRADING = frozenset({ROLE_ADMIN, ROLE_TRADING})
ADMIN_TRADING_OBCH = frozenset({ROLE_ADMIN, ROLE_TRADING, ROLE_OBCHODNIK})


def _re_from_pattern(pat: str) -> re.Pattern:
    """Premeň pattern 'a/b/*' alebo 'a/b/' na regex."""
    if pat.endswith("/*"):
        prefix = re.escape(pat[:-1])
        return re.compile(f"^{prefix}.+$")
    if pat.endswith("/"):
        return re.compile(f"^{re.escape(pat)}.*$")
    return re.compile(f"^{re.escape(pat)}$")


# ─────────────────────────────────────────────────────────────────────────────
# POLICY MATRIX — zoznam pravidiel, prvá zhoda vyhrá.
#   methods:     set HTTP metód (None = všetky)
#   pattern:     URL pattern (exact, prefix '/' alebo wildcard '*')
#   allow:       set rolí (frozenset) alebo '*' pre všetkých prihlásených
#   write:       True ak akcia mení dáta (treba can_write na profile-aware)
#   hw_write:    True pre Bender HW write
#   profile_aware: True ak treba per-profile check (use active profile)
# ─────────────────────────────────────────────────────────────────────────────

POLICY_MATRIX = [
    # ── PUBLIC (login, static, health, me) ─────────────────────────────
    {"pattern": "/login",  "allow": "*public*", "methods": None},
    {"pattern": "/logout", "allow": "*public*", "methods": None},
    {"pattern": "/me",     "allow": "*public*", "methods": None},
    {"pattern": "/static/*", "allow": "*public*", "methods": None},
    {"pattern": "/health", "allow": "*public*", "methods": None},
    {"pattern": "/favicon.ico", "allow": "*public*", "methods": None},

    # ── ADMIN ONLY ──────────────────────────────────────────────────────
    {"pattern": "/admin/", "allow": frozenset({ROLE_ADMIN}), "methods": None},
    {"pattern": "/data",   "allow": frozenset({ROLE_ADMIN}), "methods": None},

    # ── REALIO HW write — kritické ──────────────────────────────────────
    {"pattern": "/realio/write",  "allow": ADMIN_TRADING, "methods": {"POST"},
     "hw_write": True, "profile_aware": True},
    {"pattern": "/realio/fve_write",  "allow": ADMIN_TRADING, "methods": {"POST"},
     "hw_write": True, "profile_aware": True},
    {"pattern": "/realio/batt_plan_export/submit", "allow": ADMIN_TRADING,
     "methods": {"POST"}, "hw_write": True, "profile_aware": True},

    # ── REALIO config + diag — admin/trading ────────────────────────────
    {"pattern": "/realio/save", "allow": ADMIN_TRADING, "methods": {"POST"}, "write": True},
    {"pattern": "/realio/test_read", "allow": ADMIN_TRADING, "methods": {"POST"}},
    {"pattern": "/realio/relogin", "allow": ADMIN_TRADING, "methods": {"POST"}, "write": True},
    {"pattern": "/realio/backfill", "allow": ADMIN_TRADING, "methods": {"POST"}, "write": True},
    {"pattern": "/realio/backfill_range", "allow": ADMIN_TRADING, "methods": {"POST"}, "write": True},
    {"pattern": "/realio/cleanup_csv", "allow": frozenset({ROLE_ADMIN}), "methods": {"POST"}, "write": True},
    {"pattern": "/realio/fix_future_timestamps", "allow": frozenset({ROLE_ADMIN}), "methods": {"POST"}, "write": True},
    {"pattern": "/realio/discover_write", "allow": ADMIN_TRADING, "methods": {"POST"}},
    {"pattern": "/realio/disable_control", "allow": ADMIN_TRADING, "methods": {"POST"}},
    {"pattern": "/realio/scan_js", "allow": frozenset({ROLE_ADMIN}), "methods": {"POST"}},
    {"pattern": "/realio/probe_ws", "allow": frozenset({ROLE_ADMIN}), "methods": {"POST"}},
    {"pattern": "/realio/scan_msg_types", "allow": frozenset({ROLE_ADMIN}), "methods": {"POST"}},
    {"pattern": "/realio/ws_listen", "allow": frozenset({ROLE_ADMIN}), "methods": {"POST"}},

    # /realio* GET stránky (vizualizácia + nastavenie + riadenie) — admin/trading/obchodnik read
    {"pattern": "/realio/*", "allow": ADMIN_TRADING_OBCH, "methods": None, "profile_aware": True},
    {"pattern": "/realio", "allow": ADMIN_TRADING_OBCH, "methods": None, "profile_aware": True},
    {"pattern": "/realio/api/latest", "allow": ALL_AUTH, "methods": {"GET"}},
    {"pattern": "/realio/api/soc", "allow": ALL_AUTH, "methods": {"GET"}},

    # ── PROFILES management — admin + obchodnik read-only ───────────────
    {"pattern": "/profiles/save",   "allow": ADMIN_TRADING, "methods": {"POST"}, "write": True},
    {"pattern": "/profiles/delete", "allow": frozenset({ROLE_ADMIN}), "methods": {"POST"}, "write": True},
    {"pattern": "/profiles/apply",  "allow": ADMIN_TRADING_OBCH, "methods": {"POST"}},
    {"pattern": "/profiles/snapshot", "allow": ADMIN_TRADING, "methods": {"POST"}},
    {"pattern": "/profiles/edit",   "allow": ADMIN_TRADING_OBCH, "methods": {"GET"}},
    {"pattern": "/profiles",        "allow": ADMIN_TRADING_OBCH, "methods": None},

    # ── PLAN + DENTRH generovanie — admin + trading ─────────────────────
    {"pattern": "/plan",       "allow": ADMIN_TRADING, "methods": {"POST"}, "write": True},
    {"pattern": "/plan_batch", "allow": ADMIN_TRADING, "methods": {"POST"}, "write": True},
    {"pattern": "/dentrh",     "allow": ADMIN_TRADING, "methods": {"POST"}, "write": True},
    {"pattern": "/plans/delete", "allow": ADMIN_TRADING, "methods": {"POST"}, "write": True},
    # GET stránky pre formuláre — admin/trading aj obchodnik (read-only forma)
    {"pattern": "/dentrh",      "allow": ADMIN_TRADING_OBCH, "methods": {"GET"}},
    {"pattern": "/plan_batch",  "allow": ADMIN_TRADING_OBCH, "methods": {"GET"}},
    {"pattern": "/plan_view",   "allow": ALL_AUTH, "methods": {"GET"}},
    {"pattern": "/plans",       "allow": ALL_AUTH, "methods": {"GET"}},
    {"pattern": "/download",    "allow": ADMIN_TRADING_OBCH, "methods": {"GET"}},

    # ── LIVESIM + RT — všetci prihlásení ────────────────────────────────
    {"pattern": "/livesim/*", "allow": ALL_AUTH, "methods": None},
    {"pattern": "/livesim",   "allow": ALL_AUTH, "methods": None},
    {"pattern": "/livesim_pdf", "allow": ALL_AUTH, "methods": {"GET"}},
    {"pattern": "/rt",        "allow": ALL_AUTH, "methods": {"GET"}},

    # ── LOAD IMPORT — admin + trading ───────────────────────────────────
    {"pattern": "/load_import/*", "allow": ADMIN_TRADING, "methods": None, "write": True},
    {"pattern": "/load_import",   "allow": ADMIN_TRADING, "methods": None, "write": True},

    # ── FTV scenarios — admin + trading ─────────────────────────────────
    {"pattern": "/ftv_scenario", "allow": ADMIN_TRADING, "methods": None, "write": True},

    # ── VDT OKTE — admin/trading/obchodnik read-only ────────────────────
    {"pattern": "/vdt/*", "allow": ADMIN_TRADING_OBCH, "methods": None},
    {"pattern": "/vdt",   "allow": ADMIN_TRADING_OBCH, "methods": None},

    # ── AUTO CONTROL — admin/trading; obchodnik read-only ───────────────
    {"pattern": "/auto_control/kill_switch", "allow": ADMIN_TRADING, "methods": {"POST"}, "write": True},
    {"pattern": "/auto_control/toggle_profile", "allow": ADMIN_TRADING, "methods": {"POST"}, "write": True},
    {"pattern": "/auto_control", "allow": ADMIN_TRADING_OBCH, "methods": {"GET"}},

    # ── SIMULACIA + KALIBRACIA — admin/trading/obchodnik ────────────────
    {"pattern": "/simulacia", "allow": ADMIN_TRADING_OBCH, "methods": None},
    {"pattern": "/kalibracia", "allow": ADMIN_TRADING_OBCH, "methods": None},

    # ── MARKET switcher — admin/trading/obchodnik ───────────────────────
    {"pattern": "/market/set", "allow": ADMIN_TRADING_OBCH, "methods": {"POST"}, "write": True},

    # ── ROOT — všetci prihlásení ────────────────────────────────────────
    {"pattern": "/", "allow": ALL_AUTH, "methods": {"GET"}},
]

# Predkompilovaný regex cache
_COMPILED = [(r.copy(), _re_from_pattern(r["pattern"])) for r in POLICY_MATRIX]


def _auth_required_now() -> bool:
    return os.environ.get("AUTH_REQUIRED", "0").strip() in ("1", "true", "True", "yes")


def _find_rule(method: str, path: str) -> Optional[dict]:
    """Vráti prvý policy rule ktorý matchuje, alebo None."""
    for rule, regex in _COMPILED:
        if rule.get("methods") and method not in rule["methods"]:
            continue
        if regex.match(path):
            return rule
    return None


def _is_json_path(path: str) -> bool:
    return path.startswith("/realio/api/") or path.endswith(".json")


class PolicyMiddleware(BaseHTTPMiddleware):
    """Aplikuje POLICY_MATRIX. AUTH_REQUIRED=0 → no-op.

    Nezachytené paths → defaultne povolené (back-compat).
    """

    async def dispatch(self, request: Request, call_next):
        if not _auth_required_now():
            return await call_next(request)

        path = request.url.path
        method = request.method.upper()

        rule = _find_rule(method, path)
        if rule is None:
            # No-rule → default povolené (back-compat). Hlavný AuthMiddleware už ale
            # zaistil že user je prihlásený (alebo path je public).
            return await call_next(request)

        # Public allowlist (rule.allow=='*public*')
        if rule.get("allow") == "*public*":
            return await call_next(request)

        # Inak musí byť user prihlásený
        user = getattr(request.state, "user", None)
        if user is None:
            # AuthMiddleware mal redirect-nuť — sem sa dostaneme len pri JSON API
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        # Role check
        allow = rule.get("allow")
        if isinstance(allow, frozenset):
            if user.get("role") not in allow:
                return _forbidden(request,
                                     f"Tvoja rola '{user.get('role')}' nemá oprávnenie.")
        # ALL_AUTH → vždy OK (user je prihlásený)

        # Profile-aware check (Trading + Zakaznik musia mať access záznam)
        if rule.get("profile_aware"):
            from plan_store import resolve_profile
            profile_name = resolve_profile()
            if profile_name and not has_profile_access(
                user["id"], profile_name,
                write=bool(rule.get("write")),
                hw_write=bool(rule.get("hw_write")),
            ):
                return _forbidden(request,
                                     f"Nemáš prístup k profilu '{profile_name}'."
                                     + (f" (vyžaduje HW write)" if rule.get("hw_write") else
                                          (" (vyžaduje write)" if rule.get("write") else "")))

        return await call_next(request)


def _forbidden(request: Request, msg: str):
    if _is_json_path(request.url.path):
        return JSONResponse({"error": "forbidden", "detail": msg}, status_code=403)
    body = (
        f'<!doctype html><html lang="sk"><head><meta charset="utf-8">'
        f'<title>403 — Zakázané</title>'
        f'<style>body{{font-family:-apple-system,Arial;max-width:520px;margin:80px auto;'
        f'padding:0 24px;text-align:center;color:#222}}h1{{color:#C62828}}'
        f'.box{{background:#fbeaea;border-radius:10px;padding:24px;margin:24px 0}}'
        f'a{{color:#1F4E78;text-decoration:none}}</style></head>'
        f'<body><h1>🚫 403 — Zakázané</h1>'
        f'<div class="box">{msg}</div>'
        f'<a href="/">← Späť na úvod</a></body></html>'
    )
    return HTMLResponse(body, status_code=403)
