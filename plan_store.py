# -*- coding: utf-8 -*-
"""
plan_store.py — perzistencia D-1 plánov.

Plán = JSON s kompletným rozvrhom + všetkými parametrami ktoré ho vygenerovali.
Cesta: out/plans/<date>_<step>min_<kind>.json

`kind`:
  - "plan"   = hodinový D-1 z /plan (predikované ceny cez PriceModel)
  - "dentrh" = 15-min z /dentrh (reálne OTE day-ahead ceny)

Livesim ich číta v strict-mode: ak plán pre deň + krok chýba, deň sa preskočí
s warningom a používateľ ho musí prst vygenerovať cez /plan, /dentrh, alebo
batch /plan_batch.
"""
from __future__ import annotations
import os, json
from datetime import datetime
from typing import Optional, List, Dict, Any
import numpy as np

DEFAULT_PROFILE = "default"                    # legacy = súbory priamo v DIR


def _dir_root() -> str:
    """Koreň plans/. Rešpektuje aktívny trh (out/cz/plans alebo out/sk/plans).
    Env var PLAN_STORE_DIR ostane podporovaný pre back-compat testov."""
    env = os.environ.get("PLAN_STORE_DIR")
    if env:
        return env
    try:
        import market as _mk
        return os.path.join(_mk.data_dir(), "plans")
    except Exception:
        return "out/cz/plans"


# Spätná kompatibilita — niektoré moduly importujú DIR. Resolvujeme dynamicky pri každom volaní cez _dir_root().
DIR = _dir_root()


def _safe_profile_name(name: str) -> str:
    """Bezpečný názov priečinka. Iba alfanum + '_-'."""
    import re
    s = re.sub(r"[^A-Za-z0-9_\-]", "_", str(name).strip())
    s = s.strip("_-")
    return s or DEFAULT_PROFILE


def resolve_profile(profile: Optional[str] = None) -> str:
    """Resolve aktívneho profilu (na riadenie kde plán načítať/uložiť).
    Priorita: explicit param → env var FTV_PROFILE → profiles.get_active() → 'default'."""
    if profile:
        return _safe_profile_name(profile)
    env = os.environ.get("FTV_PROFILE")
    if env:
        return _safe_profile_name(env)
    try:
        import profiles as _pr
        a = _pr.get_active()
        if a:
            return _safe_profile_name(a)
    except Exception:
        pass
    return DEFAULT_PROFILE


def _dir_for(profile: Optional[str] = None) -> str:
    """Adresár pre plány daného profilu. 'default' = priamo root (legacy)."""
    p = resolve_profile(profile)
    root = _dir_root()
    return root if p == DEFAULT_PROFILE else os.path.join(root, p)


class PlanMissingError(Exception):
    """Vyhodené v strict režime keď plán pre dátum + krok neexistuje na disku."""
    def __init__(self, date_iso: str, step_min: int, kind: str = "plan", profile: str = None):
        self.date_iso = date_iso
        self.step_min = int(step_min)
        self.kind = kind
        self.profile = profile or resolve_profile()
        super().__init__(f"Plán pre {date_iso} ({step_min}-min, {kind}, profil={self.profile}) neexistuje. "
                         f"Vygeneruj cez /plan, /dentrh alebo /plan_batch.")


def _ensure_dir(profile: Optional[str] = None) -> None:
    os.makedirs(_dir_for(profile), exist_ok=True)


def _path(date_iso: str, step_min: int, kind: str, profile: Optional[str] = None) -> str:
    safe_date = "".join(c for c in str(date_iso) if c.isalnum() or c in ("-", "_"))
    safe_kind = "".join(c for c in str(kind) if c.isalnum() or c in ("_",)) or "plan"
    return os.path.join(_dir_for(profile), f"{safe_date}_{int(step_min)}min_{safe_kind}.json")


def has_plan(date_iso: str, step_min: int, kind: str = "plan", profile: Optional[str] = None) -> bool:
    return os.path.exists(_path(date_iso, step_min, kind, profile))


def _to_jsonable(obj: Any) -> Any:
    """Konvertuje numpy/pandas hodnoty na JSON-friendly typy."""
    if obj is None:
        return None
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.integer, int)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if not np.isfinite(f) else f
    if isinstance(obj, (np.ndarray, list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    return obj                                              # str a iné


def save_plan(date_iso: str, step_min: int, kind: str, *,
              params: Dict[str, Any],
              schedule: Dict[str, list],
              summary: Dict[str, Any] = None,
              mults: Optional[list] = None,
              rt_mask: Optional[list] = None,
              block_planned_discharge: bool = False,
              zco_bias_w: float = 0.0,
              rt_freedom: bool = True,
              meta: Dict[str, Any] = None,
              profile: Optional[str] = None) -> str:
    """Uloží kompletný plán pre daný deň + krok + kind + profil. Vracia cestu k súboru.
    profile=None → aktívny profil (cez resolve_profile). default profil = legacy out/plans/."""
    _ensure_dir(profile)
    expected_n = 96 if int(step_min) == 15 else 24
    # validácia základných polí
    for k, v in schedule.items():
        if len(v) != expected_n:
            raise ValueError(f"schedule[{k}] má dĺžku {len(v)}, očakávam {expected_n}")
    body = {
        "date": str(date_iso),
        "step_min": int(step_min),
        "kind": str(kind),
        "profile": resolve_profile(profile),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "params": _to_jsonable(params or {}),
        "block_planned_discharge": bool(block_planned_discharge),
        "zco_bias_w": float(zco_bias_w or 0.0),
        "rt_freedom": bool(rt_freedom),
        "mults": _to_jsonable(mults) if mults is not None else None,
        "rt_mask": _to_jsonable(rt_mask) if rt_mask is not None else None,
        "schedule": {k: _to_jsonable(v) for k, v in schedule.items()},
        "summary": _to_jsonable(summary or {}),
        "meta": _to_jsonable(meta or {}),
    }
    p = _path(date_iso, step_min, kind, profile)
    with open(p, "w") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)
    return p


def load_plan(date_iso: str, step_min: int, kind: str = "plan",
              profile: Optional[str] = None) -> Dict[str, Any]:
    """Načíta plán. Hodí PlanMissingError ak neexistuje."""
    p = _path(date_iso, step_min, kind, profile)
    if not os.path.exists(p):
        raise PlanMissingError(date_iso, step_min, kind, profile)
    with open(p) as f:
        return json.load(f)


def load_plan_safe(date_iso: str, step_min: int, kind: str = "plan",
                   profile: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Načíta plán alebo None ak neexistuje / je poškodený."""
    try:
        return load_plan(date_iso, step_min, kind, profile)
    except (PlanMissingError, OSError, ValueError, json.JSONDecodeError):
        return None


def list_plans(kind: Optional[str] = None, profile: Optional[str] = None) -> List[Dict[str, Any]]:
    """Vráti zoznam dostupných plánov v danom profile (alebo aktívnom)."""
    d = _dir_for(profile)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        if not fn.endswith(".json"):
            continue
        full = os.path.join(d, fn)
        if not os.path.isfile(full):                # skip subdirectories (other profiles)
            continue
        try:
            with open(full) as f:
                pd = json.load(f)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        item = dict(date=pd.get("date"), step_min=int(pd.get("step_min", 60)),
                    kind=pd.get("kind", "plan"), path=full,
                    generated_at=pd.get("generated_at"),
                    profile=pd.get("profile", resolve_profile(profile)))
        if kind is not None and item["kind"] != kind:
            continue
        out.append(item)
    return sorted(out, key=lambda x: (x["date"], x["step_min"], x["kind"]))


def list_profiles_with_plans() -> List[str]:
    """Vráti zoznam profilov ktoré majú nejaké plány (vrátane 'default' ak existujú legacy)."""
    out = set()
    _root = _dir_root()                                                # market-aware
    if not os.path.isdir(_root):
        return []
    # legacy plány v root = default profil
    for fn in os.listdir(_root):
        full = os.path.join(_root, fn)
        if os.path.isfile(full) and fn.endswith(".json"):
            out.add(DEFAULT_PROFILE)
        elif os.path.isdir(full) and not fn.startswith("_"):
            # podadresár = ďalší profil
            if any(f.endswith(".json") for f in os.listdir(full)):
                out.add(fn)
    return sorted(out)


def missing_plans(date_start_iso: str, date_end_iso: str, step_min: int,
                  kind: str = "plan", profile: Optional[str] = None) -> List[str]:
    """Vráti zoznam ISO dátumov v rozsahu [start, end] (inkluzívne) pre ktoré plán chýba."""
    import pandas as _pd
    rng = _pd.date_range(date_start_iso, date_end_iso, freq="D")
    return [str(d.date()) for d in rng if not has_plan(str(d.date()), step_min, kind, profile)]


def delete_plan(date_iso: str, step_min: int, kind: str = "plan",
                profile: Optional[str] = None) -> bool:
    """Zmaže plán; True ak existoval a zmazal sa, False inak."""
    p = _path(date_iso, step_min, kind, profile)
    if not os.path.exists(p):
        return False
    try:
        os.remove(p)
        return True
    except OSError:
        return False
