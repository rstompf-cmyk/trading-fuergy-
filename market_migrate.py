# -*- coding: utf-8 -*-
"""
market_migrate.py — jednorazová migrácia legacy dátového layoutu (out/plans/, out/plan_overrides/, …)
na nový multi-market layout out/cz/plans/, out/cz/plan_overrides/, …

Beh
---
Pri importe market.py voláme `market_migrate.ensure_migrated()` ktorý:
  1. Skontroluje či existujú legacy priečinky priamo v out/ (out/plans/, atď.)
  2. Ak áno, presunie ich do out/cz/<sub>/.
  3. Vytvori marker súbor out/_migrated_to_market.json aby sa migrácia spustila len raz.
  4. Vytvori out/sk/ ako prázdnu šablónu (so štandardnými podpriečinkami).

Migrácia je IDEMPOTENT — opakovaný beh je no-op (kontroluje marker + existenciu).
"""
from __future__ import annotations
import os, json, shutil
from datetime import datetime


MARKER = "out/_migrated_to_market.json"
PROFILES_SHARED_MARKER = "out/_migrated_profiles_to_shared.json"
LIVESIM_MARKER = "out/_migrated_livesim_to_market.json"
LEGACY_DIRS = [
    "plans", "plan_overrides", "profiles", "load_profile", "ftv_scenarios"
]
# Pre cases (out/cases/*.json) — nemigrujeme, ostáva globálne (model je rovnaký).
# imbalance_*.csv / livesim_*.csv / price_train_*.csv — tiež globálne pre teraz,
# pretože sú CZ-specific (ČEPS). SK má vlastné feedy → migrujeme neskôr explicitne.


def is_migrated() -> bool:
    return os.path.exists(MARKER)


def _move_dir(src: str, dst: str) -> bool:
    """Bezpečne presunie src → dst (ak src existuje a dst neexistuje)."""
    if not os.path.isdir(src) or os.path.isdir(dst):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)
    return True


def ensure_migrated() -> dict:
    """Spustí migráciu ak ešte nebola. Vracia summary dict."""
    if is_migrated():
        return {"already": True, "moved": []}
    os.makedirs("out", exist_ok=True)
    moved = []
    # 1) Presunúť legacy priečinky out/<sub>/ → out/cz/<sub>/
    for sub in LEGACY_DIRS:
        src = os.path.join("out", sub)
        dst = os.path.join("out", "cz", sub)
        if _move_dir(src, dst):
            moved.append((src, dst))
    # 2) Vytvor out/cz/ a out/sk/ s podpriečinkami (ak chýbajú)
    for mk in ("cz", "sk"):
        for sub in LEGACY_DIRS:
            os.makedirs(os.path.join("out", mk, sub), exist_ok=True)
    # 3) Marker
    with open(MARKER, "w") as fh:
        json.dump({"migrated_at": datetime.now().isoformat(timespec="seconds"),
                   "moved": [{"src": s, "dst": d} for s, d in moved]}, fh, indent=1)
    return {"already": False, "moved": moved}


def ensure_profiles_shared() -> dict:
    """Migrácia profilov: out/cz/profiles/ + out/sk/profiles/ → out/profiles/ (shared).

    Profile = šablóna použiteľná pre CZ aj SK trh. Computations (plány, livesim,
    plan_overrides) ostávajú per-market.

    Idempotent — pri opakovanom behu kontroluje marker.
    """
    if os.path.exists(PROFILES_SHARED_MARKER):
        return {"already": True, "copied": []}
    shared_dir = os.path.join("out", "profiles")
    os.makedirs(shared_dir, exist_ok=True)
    copied = []
    for mk in ("cz", "sk"):
        src_dir = os.path.join("out", mk, "profiles")
        if not os.path.isdir(src_dir):
            continue
        for fn in os.listdir(src_dir):
            src = os.path.join(src_dir, fn)
            if not os.path.isfile(src):
                continue
            # _active*.json je per-port/per-market state, nemigrovať — generovaný runtime
            if fn.startswith("_"):
                continue
            dst = os.path.join(shared_dir, fn)
            # Nezruš existujúci shared profile ak by user-already-vytvoril
            if os.path.exists(dst):
                continue
            try:
                shutil.copy2(src, dst)
                copied.append((src, dst))
            except Exception:
                continue
    with open(PROFILES_SHARED_MARKER, "w") as fh:
        json.dump({"migrated_at": datetime.now().isoformat(timespec="seconds"),
                   "copied": [{"src": s, "dst": d} for s, d in copied]}, fh, indent=1)
    return {"already": False, "copied": copied}


def ensure_livesim_market() -> dict:
    """Migrácia livesim CSV/meta: out/livesim_<case>*.csv → out/cz/livesim_<case>*.csv.

    Legacy livesim súbory boli globálne (out/livesim_*.csv). Pre multi-market
    je nutné per-market izolovanie — CZ data zostávajú v out/cz/, pre SK
    sa vytvorí čistá kópia (alebo prázdny štart) v out/sk/.

    Idempotent: pri opakovaní no-op.
    """
    if os.path.exists(LIVESIM_MARKER):
        return {"already": True, "moved": []}
    moved = []
    cz_dir = os.path.join("out", "cz")
    sk_dir = os.path.join("out", "sk")
    os.makedirs(cz_dir, exist_ok=True)
    os.makedirs(sk_dir, exist_ok=True)
    if os.path.isdir("out"):
        for fn in os.listdir("out"):
            if not (fn.startswith("livesim_") and (fn.endswith(".csv") or fn.endswith(".meta.json"))):
                continue
            src = os.path.join("out", fn)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(cz_dir, fn)
            if os.path.exists(dst):
                continue
            try:
                shutil.move(src, dst)
                moved.append((src, dst))
            except Exception:
                continue
    with open(LIVESIM_MARKER, "w") as fh:
        json.dump({"migrated_at": datetime.now().isoformat(timespec="seconds"),
                   "moved": [{"src": s, "dst": d} for s, d in moved]}, fh, indent=1)
    return {"already": False, "moved": moved}


# spustí migráciu pri prvom importe (idempotent)
try:
    ensure_migrated()
    ensure_profiles_shared()
    ensure_livesim_market()
except Exception as _e:
    # ak migrácia zlyhá, appka pokračuje — pôvodný layout sa použije
    # (path helpery v moduloch musia byť robust voči chýbajúcim out/cz/)
    pass
