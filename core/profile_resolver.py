# -*- coding: utf-8 -*-
"""core/profile_resolver.py — Single source of truth pre aktívny profil.

Bug Q fix (2026-06-06): Pred týmto modulom existovalo 6 paralelných resolverov
ktoré čítali z rôznych zdrojov (env var, per-port active, realio-pinned, atď.).
Výsledok: prepínanie profilov nefungovalo spoľahlivo — klik na VDT zobrazil
iný profil než /profiles chip.

Architektúra:
    get_active(explicit=None) → str
        Vracia meno aktívneho profilu PRE TENTO PORT.
        Priorita:
          1. `explicit` parameter (URL query ?profile=X) — read-only override
          2. profiles.get_active() — per-port persistent storage
          3. DEFAULT_PROFILE konstanta
        ŽIADNY env var, ŽIADNY realio-pinned, ŽIADNY druhý zdroj.

    set_active(name) → None
        Atomický set:
          - profiles.set_active() (per-port JSON + DB)
          - Cleanup environment FTV_PROFILE (legacy, mali by ostávať)
          - Cleanup ui_settings.realio_profile (legacy, Bug Q vyradil)
          - Audit log (stack trace pre debug)

Použitie:
    from core.profile_resolver import get_active, set_active

    # Endpointy (URL query override):
    @app.get("/some")
    def handler(profile: Optional[str] = None):
        prof = get_active(profile)         # 1 zdroj
        ...

    # Setter (cez /profiles/apply):
    set_active("VW_simulacia")             # Atomický + audit

Note: tento modul je iba THIN WRAPPER okolo profiles.py — jeho účel je byť
SINGLE call site pre celú aplikáciu. Všetky existujúce resolver funkcie
(plan_store.resolve_profile, atď.) preexpúšťajú sem.
"""
from __future__ import annotations
import os
from typing import Optional


DEFAULT_PROFILE = "default"


def _safe_name(name: Optional[str]) -> str:
    """Filename-safe: iba alfanum + '_-'. Empty → DEFAULT_PROFILE."""
    if not name:
        return DEFAULT_PROFILE
    import re
    s = re.sub(r"[^A-Za-z0-9_\-]", "_", str(name).strip())
    s = s.strip("_-")
    return s or DEFAULT_PROFILE


def get_active(explicit: Optional[str] = None) -> str:
    """Single source of truth pre aktívny profil PRE TENTO PORT.

    Args:
        explicit: ak nie je None ani prázdny string, použije sa priamo
                    (URL query override — read-only, neprepína persistent stav).

    Returns:
        Meno profilu (safe filename), nikdy None. Default: 'default'.

    Nepoužíva: env var FTV_PROFILE, ui_settings.realio_profile, ani žiadne
    iné per-request storage. Iba profiles.get_active() + explicit param.
    """
    if explicit and str(explicit).strip():
        return _safe_name(explicit)
    try:
        import profiles as _pr
        name = _pr.get_active()
        if name:
            return _safe_name(name)
    except Exception as _e:
        print(f"[profile_resolver.get_active] profiles.get_active() zlyhal: {_e}")
    return DEFAULT_PROFILE


def set_active(name: Optional[str]) -> None:
    """Atomický set aktívneho profilu PRE TENTO PORT.

    Vykoná:
        1. profiles.set_active(name) — per-port JSON + DB write
        2. Cleanup environment FTV_PROFILE (legacy, ak by tam zostalo)
        3. Cleanup ui_settings.realio_profile (Bug Q: zrušený mechanizmus)
        4. Audit log (stack trace pre debug)

    Args:
        name: meno profilu (None = clear active = 'default')
    """
    # 1. Persist cez profiles.set_active (per-port, market-aware)
    try:
        import profiles as _pr
        _pr.set_active(name)
    except Exception as e:
        print(f"[profile_resolver.set_active] profiles.set_active zlyhal: {e}")

    # 2. Cleanup legacy env var (Bug G fix už začal, dokončíme tu)
    if "FTV_PROFILE" in os.environ:
        old = os.environ.pop("FTV_PROFILE")
        print(f"[profile_resolver.set_active] cleanup legacy env FTV_PROFILE={old!r}")

    # 3. Cleanup legacy realio-pinned (Bug Q: tento mechanizmus zrušený)
    try:
        from core.state import _ui_load, _ui_save
        cur = _ui_load("realio_profile", {})
        if cur:
            _ui_save("realio_profile", {})            # vyprázdni
            print(f"[profile_resolver.set_active] cleanup legacy realio_profile={cur}")
    except Exception:
        pass

    # 4. Audit log (kto volal set_active)
    try:
        import traceback as _tb
        _stack = _tb.extract_stack(limit=8)[:-1]
        _caller = " <- ".join(
            f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}"
            for f in _stack[-4:]
        )
        print(f"[profile_resolver.set_active] name={name!r} ✓ | caller: {_caller}")
    except Exception:
        pass


def get_mode(profile: Optional[str] = None) -> str:
    """Vráti mode profilu ('simulation' / 'real' / 'unknown').

    Args:
        profile: ak None, použije get_active(). Inak explicit name.
    """
    name = get_active(profile) if profile else get_active()
    try:
        import profiles as _pr
        p = _pr.load_profile(name)
        if isinstance(p, dict):
            return str(p.get("mode") or "unknown").lower()
    except Exception:
        pass
    return "unknown"


__all__ = ["get_active", "set_active", "get_mode", "DEFAULT_PROFILE"]
