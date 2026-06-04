# -*- coding: utf-8 -*-
"""
plan_overrides.py — ručné násobitele nad výstupom optimizéra (D-1 plán batérie).

Idea:
- Globálna šablóna (96 hodnôt × 15-min) + per-day prepis → kombinovaný "effective" profil.
- Hodnota slotu = multiplikátor proposed batt_kw z optimizéra (1.0 = bez zmeny, 0.0 = blokovať,
  záporné = obrátený smer, >1.0 = posilniť).
- Pri rozhodnutí ako sa kombinujú: per-day má prednosť pred šablónou, šablóna pred default=1.0.
  Per-day hodnota je "zapnutá" ak je explicitne uložená (NaN znamená "necháva sa default").
- Granularita úložiska: vždy 96 (15-min). UI v hodinovom režime broadcastuje hodinovú hodnotu na 4×15-min.

Soubory:
  out/plan_overrides/_template.json   → globálna šablóna {"mult96": [...]}  (NaN = nezadané)
  out/plan_overrides/<YYYY-MM-DD>.json → per-day prepis  {"mult96": [...]} (NaN = nepoužiť prepis)
"""
from __future__ import annotations
import os, json
import numpy as np
from typing import Optional, Tuple

DEFAULT_PROFILE = "default"
N96 = 96


def _root() -> str:
    """Koreň plan_overrides. Market-aware (out/cz/plan_overrides alebo out/sk/plan_overrides)."""
    env = os.environ.get("PLAN_OVERRIDES_DIR")
    if env:
        return env
    try:
        import market as _mk
        return os.path.join(_mk.data_dir(), "plan_overrides")
    except Exception:
        return "out/cz/plan_overrides"


# back-compat (niekde sa importuje)
DIR = _root()
TEMPLATE_PATH = os.path.join(DIR, "_template.json")


def _resolve_profile(profile: Optional[str] = None) -> str:
    """Vráti aktívny profil. Default = 'default' (legacy bez podadresára).
    Priorita: parameter → plan_store.resolve_profile() → 'default'."""
    if profile:
        return profile
    try:
        import plan_store as _ps
        return _ps.resolve_profile()
    except Exception:
        return DEFAULT_PROFILE


def _dir_for(profile: Optional[str] = None) -> str:
    """Adresár pre overrides daného profilu. 'default' = priamo root (legacy)."""
    p = _resolve_profile(profile)
    root = _root()
    return root if p == DEFAULT_PROFILE else os.path.join(root, p)


def _kind_suffix(kind: str) -> str:
    """'plan' (default, /plan 60-min) → '' (legacy bez sufixu).
    'dentrh' (/dentrh 15-min) → '_dentrh'. Iné vetvy môžu pridať vlastný suffix v budúcnosti."""
    if kind == "dentrh":
        return "_dentrh"
    return ""


def _template_path_for(kind: str, profile: Optional[str] = None) -> str:
    """Cesta k template súboru per kind + profil. Legacy 'plan'+'default' = _template.json."""
    suf = _kind_suffix(kind)
    return os.path.join(_dir_for(profile), f"_template{suf}.json")


def _day_path_for(date_iso: str, kind: str, profile: Optional[str] = None) -> str:
    suf = _kind_suffix(kind)
    return os.path.join(_dir_for(profile), f"{date_iso}{suf}.json")


def _ensure_dir(profile: Optional[str] = None) -> None:
    os.makedirs(_dir_for(profile), exist_ok=True)


def _safe_load_raw(path: str) -> Optional[dict]:
    """Načíta surový JSON dict. None ak súbor chýba/neplatný."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _parse_mult96(data: Optional[dict]) -> Optional[np.ndarray]:
    """Extrahuje mult96 z JSON dict ako float96 (NaN tam, kde bola null/None)."""
    if not data:
        return None
    arr = data.get("mult96")
    if arr is None or len(arr) != N96:
        return None
    return np.array([(float(x) if x is not None else float("nan")) for x in arr], dtype=float)


def _parse_rt_on96(data: Optional[dict]) -> Optional[np.ndarray]:
    """Extrahuje rt_on96 z JSON dict ako float96 (1.0/0.0; NaN = "nezadané"). Default = NaN (padá ďalej)."""
    if not data:
        return None
    arr = data.get("rt_on96")
    if arr is None or len(arr) != N96:
        return None
    return np.array([(float(x) if x is not None else float("nan")) for x in arr], dtype=float)


def _safe_load(path: str) -> Optional[np.ndarray]:
    """Spätná kompatibilita: vracia len mult96 časť."""
    return _parse_mult96(_safe_load_raw(path))


def _safe_save_both(path: str, mult96: Optional[np.ndarray] = None,
                     rt_on96: Optional[np.ndarray] = None, *, note: str = "") -> None:
    """Uloží JSON so súčasným mult96 a rt_on96. Ak je len jedno nastavené, druhé ostane z disku (alebo NaN)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    existing = _safe_load_raw(path) or {}
    body = dict(existing)
    if mult96 is not None:
        arr = np.asarray(mult96, dtype=float).reshape(-1)
        if arr.size != N96:
            raise ValueError(f"mult96: očakávam {N96} hodnôt, dostal som {arr.size}")
        body["mult96"] = [(None if not np.isfinite(x) else float(x)) for x in arr]
    if rt_on96 is not None:
        arr = np.asarray(rt_on96, dtype=float).reshape(-1)
        if arr.size != N96:
            raise ValueError(f"rt_on96: očakávam {N96} hodnôt, dostal som {arr.size}")
        body["rt_on96"] = [(None if not np.isfinite(x) else float(x)) for x in arr]
    if note:
        body["note"] = note
    with open(path, "w") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)


def _safe_save(path: str, arr96: np.ndarray, *, note: str = "") -> None:
    """Spätná kompatibilita: uloží len mult96."""
    _safe_save_both(path, mult96=arr96, note=note)


# ─── globálna šablóna ───────────────────────────────────────────────────────

def load_template(kind: str = "plan", profile: Optional[str] = None) -> np.ndarray:
    """Vracia 96 hodnôt mult96 šablóny per kind + profil; NaN tam kde šablóna mlčí (default 1.0).
    kind='plan'/'dentrh', profile=None → aktívny (cez plan_store.resolve_profile)."""
    arr = _parse_mult96(_safe_load_raw(_template_path_for(kind, profile)))
    return arr if arr is not None else np.full(N96, np.nan, dtype=float)


def save_template(mult96, kind: str = "plan", profile: Optional[str] = None) -> None:
    _safe_save_both(_template_path_for(kind, profile), mult96=mult96,
                     note=f"globálna šablóna násobiteľov ({kind}/{_resolve_profile(profile)})")


def load_template_rt(kind: str = "plan", profile: Optional[str] = None) -> np.ndarray:
    """RT mask šablóna; NaN = "nezadané", 1 = RT povolené, 0 = RT zablokované (drží plán)."""
    arr = _parse_rt_on96(_safe_load_raw(_template_path_for(kind, profile)))
    return arr if arr is not None else np.full(N96, np.nan, dtype=float)


def save_template_rt(rt_on96, kind: str = "plan", profile: Optional[str] = None) -> None:
    _safe_save_both(_template_path_for(kind, profile), rt_on96=rt_on96,
                     note=f"globálna šablóna násobiteľov + RT ({kind}/{_resolve_profile(profile)})")


# ─── per-day ────────────────────────────────────────────────────────────────

def _day_path(date_iso: str, kind: str = "plan", profile: Optional[str] = None) -> str:
    return _day_path_for(date_iso, kind, profile)


def load_day(date_iso: str, kind: str = "plan", profile: Optional[str] = None) -> np.ndarray:
    """Vracia 96 hodnôt per-day prepisu mult96. NaN tam kde sa per-day nevyjadruje."""
    arr = _parse_mult96(_safe_load_raw(_day_path(date_iso, kind, profile)))
    return arr if arr is not None else np.full(N96, np.nan, dtype=float)


def save_day(date_iso: str, mult96, kind: str = "plan", profile: Optional[str] = None) -> None:
    _safe_save_both(_day_path(date_iso, kind, profile), mult96=mult96,
                     note=f"per-day override {date_iso} ({kind}/{_resolve_profile(profile)})")


def load_day_rt(date_iso: str, kind: str = "plan", profile: Optional[str] = None) -> np.ndarray:
    """Per-day RT mask; NaN = "nezadané", 1 = RT povolené, 0 = RT zablokované."""
    arr = _parse_rt_on96(_safe_load_raw(_day_path(date_iso, kind, profile)))
    return arr if arr is not None else np.full(N96, np.nan, dtype=float)


def save_day_rt(date_iso: str, rt_on96, kind: str = "plan", profile: Optional[str] = None) -> None:
    _safe_save_both(_day_path(date_iso, kind, profile), rt_on96=rt_on96,
                     note=f"per-day override {date_iso} ({kind}/{_resolve_profile(profile)})")


def clear_day(date_iso: str, kind: str = "plan", profile: Optional[str] = None) -> None:
    """Vymaže ÚPLNE per-day prepis (aj mult, aj rt) pre daný profil."""
    p = _day_path(date_iso, kind, profile)
    if os.path.exists(p):
        try:
            os.remove(p)
        except OSError:
            # ak nemôžeme delete, prepíšeme všetko na NaN (no-op effective)
            nan96 = np.full(N96, np.nan)
            _safe_save_both(p, mult96=nan96, rt_on96=nan96, note=f"cleared {date_iso}")


# ─── kombinácia + projekcia ─────────────────────────────────────────────────

def load_effective(date_iso: str, kind: str = "plan", profile: Optional[str] = None) -> np.ndarray:
    """Effective 96-array mult per kind + profil: per-day prepíše šablónu, šablóna prepisuje default 1.0."""
    tmpl = load_template(kind, profile)
    day = load_day(date_iso, kind, profile)
    out = np.where(np.isfinite(day), day, tmpl)
    out = np.where(np.isfinite(out), out, 1.0)
    return out.astype(float)


def load_effective_rt(date_iso: str, kind: str = "plan", profile: Optional[str] = None) -> np.ndarray:
    """Effective 96-array RT mask per kind + profil."""
    tmpl = load_template_rt(kind, profile)
    day = load_day_rt(date_iso, kind, profile)
    out = np.where(np.isfinite(day), day, tmpl)
    out = np.where(np.isfinite(out), out, 1.0)
    return (out > 0.5).astype(float)


def effective_for_step(date_iso: str, step_min: int, kind: str = None,
                        profile: Optional[str] = None) -> np.ndarray:
    """Effective násobitele zarovnané na krok plánu (15→96, 60→24).
    Default kind: 'dentrh' pre step=15, 'plan' pre step=60 (samostatné šablóny pre obe vetvy)."""
    if kind is None:
        kind = "dentrh" if int(step_min) == 15 else "plan"
    eff = load_effective(date_iso, kind, profile)
    if int(step_min) == 15:
        return eff
    if int(step_min) == 60:
        return eff.reshape(24, 4).mean(axis=1)
    raise ValueError(f"step_min musí byť 15 alebo 60, dostal som {step_min}")


def effective_rt_for_step(date_iso: str, step_min: int, kind: str = None,
                           profile: Optional[str] = None) -> np.ndarray:
    """Effective RT mask zarovnaná na krok plánu.
    Default kind: 'dentrh' pre step=15, 'plan' pre step=60."""
    if kind is None:
        kind = "dentrh" if int(step_min) == 15 else "plan"
    eff = load_effective_rt(date_iso, kind, profile)
    if int(step_min) == 15:
        return eff
    if int(step_min) == 60:
        return (eff.reshape(24, 4).min(axis=1) > 0.5).astype(float)
    raise ValueError(f"step_min musí byť 15 alebo 60, dostal som {step_min}")


def broadcast_to_96(mult_step, step_min: int) -> np.ndarray:
    """UI ↔ úložisko: hodnoty pri kroku 60 sa rozkopírujú na 4×15-min (každú hodinu rovnaké)."""
    arr = np.asarray(mult_step, dtype=float).reshape(-1)
    if int(step_min) == 15:
        if arr.size != 96:
            raise ValueError(f"15-min vstup musí mať 96 hodnôt, dostal som {arr.size}")
        return arr.copy()
    if int(step_min) == 60:
        if arr.size != 24:
            raise ValueError(f"60-min vstup musí mať 24 hodnôt, dostal som {arr.size}")
        return np.repeat(arr, 4)
    raise ValueError(f"step_min musí byť 15 alebo 60, dostal som {step_min}")


# ─── podpis pre settings_sig (livesim auto-reset) ──────────────────────────

def signature(date_iso: str, kind: str = "plan", profile: Optional[str] = None) -> str:
    """Kompaktný string pre settings_sig: 96 floatov + 96 RT bit. NaN sa zlučuje k 1.0."""
    eff = load_effective(date_iso, kind, profile)
    rt = load_effective_rt(date_iso, kind, profile)
    rnd = np.where(np.isfinite(eff), np.round(eff, 3), 1.0)
    return "mult:" + ",".join(f"{x:g}" for x in rnd) + ";rt:" + "".join("1" if v > 0.5 else "0" for v in rt)


def has_any_active(date_iso: str, kind: str = "plan", profile: Optional[str] = None) -> bool:
    """True ak je v effective profile aspoň jeden slot ≠ 1.0 (mult) ALEBO aspoň jeden RT zablokovaný."""
    eff = load_effective(date_iso, kind, profile)
    rt = load_effective_rt(date_iso, kind, profile)
    return bool(np.any(np.abs(eff - 1.0) > 1e-6) or np.any(rt < 0.5))
