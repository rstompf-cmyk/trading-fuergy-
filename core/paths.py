# -*- coding: utf-8 -*-
"""core/paths.py — Per-profile FS path resolver (Fáza B.1).

Single source of truth pre cesty k profile-specific dátam. Funguje v 2 módoch:

1. **Legacy (default)** — pôvodná shared layout:
       out/profiles/<name>.json
       out/<market>/plans/<name>/<file>.json
       out/<market>/livesim_*.csv          (shared per-port)
       out/<market>/vdt_paper_trades.csv   (shared, filter by profile col)
       out/<market>/auto_control_log.csv   (shared)
       out/<market>/vdt_live_plan_<name>.json

2. **Sandbox (B.1 nový)** — per-profile podpriečinky, opt-in cez `FTV_SANDBOX=1`:
       out/profiles/<name>/
           config.json
           plans/<file>.json
           livesim/plan_d1_<port>.csv
           livesim/dt_15min_<port>.csv
           vdt_paper_trades.csv
           auto_control_log.csv
           vdt_advisor_cache.json
       out/_shared/<market>/
           historian_*.csv
           imbalance_minute.csv
           price_train_*.csv

Detekcia módu:
    - `FTV_SANDBOX=1` env var → sandbox mode
    - inak → legacy mode (default, back-compat)

Použitie (back-compat — všade existujúci kód volá rovnaké funkcie):
    from core.paths import profile_config_path, plan_path, livesim_csv_path

    cfg = profile_config_path("Simulacia_Coop")
    # legacy: out/profiles/Simulacia_Coop.json
    # sandbox: out/profiles/Simulacia_Coop/config.json
"""
from __future__ import annotations
import os
from typing import Optional


def _is_sandbox() -> bool:
    """True ak je FTV_SANDBOX=1 → sandbox mode."""
    return os.environ.get("FTV_SANDBOX", "").strip() in ("1", "true", "True", "yes")


def _market(default: str = "sk") -> str:
    """Aktuálny trh (cz/sk)."""
    try:
        import market as _mk
        return (_mk.active_market() or default).lower()
    except Exception:
        return default


def _data_dir(market: Optional[str] = None) -> str:
    """Koreň market-specific dát (out/cz alebo out/sk)."""
    mk = market or _market()
    return os.path.join("out", mk)


def _shared_dir(market: Optional[str] = None) -> str:
    """Sandbox-only: out/_shared/<market>/ pre cross-profile dáta."""
    mk = market or _market()
    return os.path.join("out", "_shared", mk)


def _profile_root(name: str) -> str:
    """Sandbox: out/profiles/<name>/ ako koreň profile-u."""
    return os.path.join("out", "profiles", name)


# ── PROFILE CONFIG ─────────────────────────────────────────────────────────

def profile_config_path(name: str) -> str:
    """Cesta k profile config JSONu.

    Legacy: out/profiles/<name>.json
    Sandbox: out/profiles/<name>/config.json
    """
    if _is_sandbox():
        return os.path.join(_profile_root(name), "config.json")
    return os.path.join("out", "profiles", f"{name}.json")


# ── PLANS ──────────────────────────────────────────────────────────────────

def plans_dir(name: str, market: Optional[str] = None) -> str:
    """Adresár pre plány profilu.

    Legacy: out/<market>/plans/<name>/
    Sandbox: out/profiles/<name>/plans/
    """
    if _is_sandbox():
        return os.path.join(_profile_root(name), "plans")
    return os.path.join(_data_dir(market), "plans", name)


def plan_path(name: str, date_iso: str, step_min: int, kind: str,
              market: Optional[str] = None) -> str:
    """Cesta k konkrétnemu plánu."""
    fn = f"{date_iso}_{int(step_min)}min_{kind}.json"
    return os.path.join(plans_dir(name, market), fn)


# ── LIVESIM ────────────────────────────────────────────────────────────────

def livesim_csv_path(case: str, port: str = "8000",
                      profile: Optional[str] = None,
                      market: Optional[str] = None) -> str:
    """Cesta k livesim CSV.

    Legacy: out/<market>/livesim_<case>_<port>.csv (per-port shared)
    Sandbox: out/profiles/<profile>/livesim/<case>_<port>.csv
    """
    if _is_sandbox() and profile:
        return os.path.join(_profile_root(profile), "livesim",
                              f"{case}_{port}.csv")
    return os.path.join(_data_dir(market), f"livesim_{case}_{port}.csv")


def livesim_meta_path(case: str, port: str = "8000",
                       profile: Optional[str] = None,
                       market: Optional[str] = None) -> str:
    return livesim_csv_path(case, port, profile, market).replace(".csv", ".meta.json")


# ── VDT PAPER TRADES ───────────────────────────────────────────────────────

def vdt_trades_csv_path(profile: Optional[str] = None,
                         market: Optional[str] = None) -> str:
    """Cesta k VDT paper trades CSV.

    Legacy: out/<market>/vdt_paper_trades.csv  (shared, filter by profile col)
    Sandbox: out/profiles/<profile>/vdt_paper_trades.csv
    """
    if _is_sandbox() and profile:
        return os.path.join(_profile_root(profile), "vdt_paper_trades.csv")
    mk = market or "sk"   # VDT je iba SK
    return os.path.join("out", mk, "vdt_paper_trades.csv")


# ── AUTO_CONTROL LOG ───────────────────────────────────────────────────────

def auto_control_log_path(profile: Optional[str] = None,
                            market: Optional[str] = None) -> str:
    """Cesta k auto_control_log CSV.

    Legacy: out/<market>/auto_control_log.csv (shared, filter by profile col)
    Sandbox: out/profiles/<profile>/auto_control_log.csv
    """
    if _is_sandbox() and profile:
        return os.path.join(_profile_root(profile), "auto_control_log.csv")
    return os.path.join(_data_dir(market), "auto_control_log.csv")


# ── VDT ADVISOR CACHE ──────────────────────────────────────────────────────

def vdt_advisor_cache_path(profile: Optional[str] = None,
                             market: Optional[str] = None) -> str:
    """Cesta k VDT advisor JSON cache.

    Legacy: out/<market>/vdt_live_plan_<profile>.json (per-profile už máme)
    Sandbox: out/profiles/<profile>/vdt_advisor_cache.json
    """
    if _is_sandbox() and profile:
        return os.path.join(_profile_root(profile), "vdt_advisor_cache.json")
    mk = market or "sk"
    if profile and profile != "default":
        safe = "".join(c for c in profile if c.isalnum() or c in "_-")
        return os.path.join("out", mk, f"vdt_live_plan_{safe}.json")
    return os.path.join("out", mk, "vdt_live_plan.json")


# ── SHARED DATA (sandbox-aware) ────────────────────────────────────────────

def shared_data_path(filename: str, market: Optional[str] = None) -> str:
    """Cesta k shared dáta (historian, imbalance, price_train).

    Legacy: out/<market>/<filename>
    Sandbox: out/_shared/<market>/<filename>
    """
    if _is_sandbox():
        return os.path.join(_shared_dir(market), filename)
    return os.path.join(_data_dir(market), filename)


# ── helpers ────────────────────────────────────────────────────────────────

def ensure_profile_dirs(name: str) -> None:
    """Sandbox-only: vytvor všetky podadresáre pre profile."""
    if not _is_sandbox():
        return
    root = _profile_root(name)
    for sub in ("plans", "livesim"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)


def is_sandbox_mode() -> bool:
    """Public API — pre log/debug."""
    return _is_sandbox()


__all__ = [
    "is_sandbox_mode",
    "profile_config_path",
    "plans_dir", "plan_path",
    "livesim_csv_path", "livesim_meta_path",
    "vdt_trades_csv_path",
    "auto_control_log_path",
    "vdt_advisor_cache_path",
    "shared_data_path",
    "ensure_profile_dirs",
]
