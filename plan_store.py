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


# ── Dual storage prepínač (Fáza 1.10 migrácie) ─────────────────────────────
# USE_DB=1 → čítame z DB ako zdroj pravdy, write je dual (DB + JSON).
# USE_DB=0 (default) → pôvodný JSON-only režim.
_USE_DB = os.environ.get("USE_DB", "0").strip() in ("1", "true", "True", "yes")


def _db_available() -> bool:
    if not _USE_DB:
        return False
    try:
        from db import get_session   # noqa: F401
        return True
    except Exception:
        return False


def _current_market() -> str:
    try:
        import market as _mk
        return str(_mk.active_market() or "cz")
    except Exception:
        return "cz"


# Mapovanie schedule kľúč → PlanSlot column (tie ktoré chceme v DB stĺpcoch)
_SLOT_COLUMNS = {
    "pv_kwh": "pv_kwh",
    "load_kwh": "load_kwh",
    "price_eur": "price_eur",
    "batt_kw": "batt_kw",
    "grid_kwh": "grid_kwh",
    "order_mwh": "order_mwh",
    "curtail_kwh": "curtail_kwh",
    "soc_pct": "soc_pct",
    "soc_kwh": "soc_kwh",
    "_charge_kw": "charge_kw",
    "_discharge_kw": "discharge_kw",
    "_export_kwh": "export_kwh",
    "_import_kwh": "import_kwh",
}
# Reverzne pre rekonštrukciu schedule pri load (DB col → schedule key)
_REVERSE_SLOT_COLUMNS = {v: k for k, v in _SLOT_COLUMNS.items()}


def _safe_profile_name(name: str) -> str:
    """Bezpečný názov priečinka. Iba alfanum + '_-'."""
    import re
    s = re.sub(r"[^A-Za-z0-9_\-]", "_", str(name).strip())
    s = s.strip("_-")
    return s or DEFAULT_PROFILE


def resolve_profile(profile: Optional[str] = None) -> str:
    """Resolve aktívneho profilu (na riadenie kde plán načítať/uložiť).

    Bug Q (2026-06-06): preexpúšťa cez core.profile_resolver — single source of truth.
    Žiadny env var FTV_PROFILE (Bug Q ho úplne zrušil). Iba explicit param + per-port
    profiles.get_active().
    """
    try:
        from core.profile_resolver import get_active as _resolve_get
        return _safe_profile_name(_resolve_get(profile))
    except Exception:
        # Hard fail-safe: priamy fallback na profiles.get_active
        if profile:
            return _safe_profile_name(profile)
        try:
            import profiles as _pr
            a = _pr.get_active()
            if a:
                return _safe_profile_name(a)
        except Exception:
            pass
        return DEFAULT_PROFILE


def _dir_for(profile: Optional[str] = None) -> str:
    """Adresár pre plány daného profilu.

    Fáza B.1: ak je FTV_SANDBOX=1, vráti `out/profiles/<name>/plans/`
    namiesto `out/<market>/plans/<name>/`. Inak (default) → legacy.
    """
    p = resolve_profile(profile)
    # Fáza B.1: sandbox layout
    try:
        from core.paths import is_sandbox_mode, plans_dir as _pd
        if is_sandbox_mode() and p != DEFAULT_PROFILE:
            return _pd(p)
    except Exception:
        pass
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
    if _db_available() and _db_has_plan(date_iso, step_min, kind, profile):
        return True
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
    # JSON write (vždy — back-compat)
    with open(p, "w") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)
    # DB write (dual storage)
    if _db_available():
        _db_save_plan(date_iso, step_min, kind, profile, body)
    # Bug #611: rezervuj D-1 batt v capacity ledger.
    # Tým VDT/RT vidia kapacitu zarezervovanú D-1 plánom a nemôžu ju prebiť.
    # Iba pre kind='plan' (D-1) — dentrh a dam_d1 sú samostatné a delia kapacitu.
    if kind == "plan":
        try:
            from core.capacity_ledger import (
                reserve as _ledger_reserve, clear_day,
                save_d1_trajectory, compute_soc_trajectory, clear_day_trajectory,
            )
            _prof_for_ledger = resolve_profile(profile)
            if _prof_for_ledger:
                # Reset existujúce D-1 rezervácie pre tento deň (re-uloženie plánu)
                clear_day(_prof_for_ledger, date_iso)
                # Bug #614: aj SOC trajektóriu reset + nová
                clear_day_trajectory(_prof_for_ledger, date_iso)
                # Vyrátaj D-1 SOC trajektóriu a ulož do d1_soc_trajectory
                try:
                    _batt_arr_traj = schedule.get("batt_kw") or schedule.get("batt") or []
                    _soc_init = float((params or {}).get("soc_init", 0.0) or 0.0)
                    # soc_init môže byť v % alebo v kWh — preferuj %
                    _batt_kwh_traj = float((params or {}).get("batt_kwh", 0.0) or 0.0)
                    if _soc_init > 1.5 and _batt_kwh_traj > 0:
                        # _soc_init je v % (typicky 5..100)
                        _soc_init_pct = _soc_init if _soc_init <= 100 else 100.0
                    elif _batt_kwh_traj > 0:
                        # legacy: soc_init je fraction (0..1) — prepočítaj na %
                        _soc_init_pct = _soc_init * 100.0
                    else:
                        _soc_init_pct = 0.0
                    _soc_traj = compute_soc_trajectory(
                        _batt_arr_traj, _soc_init_pct, _batt_kwh_traj, int(step_min))
                    if _soc_traj:
                        _n_saved = save_d1_trajectory(_prof_for_ledger, date_iso,
                                                       _soc_traj, tolerance_pct=10.0)
                        print(f"[plan_store #614] D-1 SOC trajectory saved: {_n_saved} slots "
                              f"(soc_init={_soc_init_pct:.1f}%, batt={_batt_kwh_traj:.0f}kWh)")
                except Exception as _e_traj:
                    print(f"[plan_store #614] SOC trajectory zlyhal: {_e_traj}")
                # Schedule batt_kw môže byť pod kľúčom 'batt_kw' alebo 'batt'
                _batt_arr = schedule.get("batt_kw") or schedule.get("batt") or []
                _step = int(step_min)
                _slots_per_15 = max(1, 15 // _step) if _step <= 15 else 1
                for _idx, _b_val in enumerate(_batt_arr):
                    if _b_val is None:
                        continue
                    _bk = float(_b_val)
                    if _bk == 0:
                        continue
                    # Mapuj index plánu na 15-min slot
                    if _step == 15:
                        _slot_idx = _idx
                    elif _step == 60:
                        # 1 hodina = 4 sloty (každý dostane rovnakú časť)
                        for _q in range(4):
                            _slot_idx_q = _idx * 4 + _q
                            if _slot_idx_q > 95:
                                break
                            _direction = "discharge" if _bk > 0 else "charge"
                            _ledger_reserve(_prof_for_ledger, date_iso, _slot_idx_q,
                                              source="d1", direction=_direction,
                                              kw=abs(_bk), trade_id=None,
                                              note=f"D-1 plán (step={_step}min)")
                        continue
                    else:
                        _slot_idx = (_idx * _step) // 15
                        if _slot_idx > 95:
                            continue
                    _direction = "discharge" if _bk > 0 else "charge"
                    _ledger_reserve(_prof_for_ledger, date_iso, _slot_idx,
                                      source="d1", direction=_direction,
                                      kw=abs(_bk), trade_id=None,
                                      note=f"D-1 plán (step={_step}min)")
        except Exception as _e_ledger:
            print(f"[plan_store.save_plan #611] ledger reserve zlyhal: {_e_ledger}")
    return p


def load_plan(date_iso: str, step_min: int, kind: str = "plan",
              profile: Optional[str] = None) -> Dict[str, Any]:
    """Načíta plán. Hodí PlanMissingError ak neexistuje."""
    # DB read prvé (zdroj pravdy v USE_DB režime)
    if _db_available():
        db_plan = _db_load_plan(date_iso, step_min, kind, profile)
        if db_plan is not None:
            return db_plan
    # JSON fallback
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


# ════════════════════════════════════════════════════════════════════════════
# Fáza A.2 — Pydantic-validated API (additive, neporušuje legacy)
# ════════════════════════════════════════════════════════════════════════════

def load_plan_validated(date_iso: str, step_min: int, kind: str = "plan",
                        profile: Optional[str] = None):
    """Načíta plán a vráti `StoredPlan` (Pydantic). None ak chýba / je poškodený.

    Použitie:
        from plan_store import load_plan_validated
        sp = load_plan_validated("2026-06-08", 60, "plan", "Simulacia_Coop")
        if sp is not None:
            batt = sp.get_array("batt_kw")   # list[float], dĺžka 24
            n = sp.expected_slots()
    """
    raw = load_plan_safe(date_iso, step_min, kind, profile)
    if raw is None:
        return None
    try:
        from core.schemas import StoredPlan
        return StoredPlan.model_validate(raw)
    except Exception as e:
        print(f"[plan_store.load_plan_validated {date_iso} {step_min}min {kind}] "
              f"profile={profile or 'active'} — validation zlyhal: {e}")
        return None


def validate_plan_dict(raw: Dict[str, Any]):
    """Validuje raw dict cez `StoredPlan`. Vracia `StoredPlan` alebo raise.

    Hodí sa pre check-pred-save: ak by save_plan dostal nekonzistentné dáta,
    skôr to chytíme tu ako až pri load.
    """
    from core.schemas import StoredPlan
    return StoredPlan.model_validate(raw)


def list_plans(kind: Optional[str] = None, profile: Optional[str] = None) -> List[Dict[str, Any]]:
    """Vráti zoznam dostupných plánov v danom profile (alebo aktívnom)."""
    if _db_available():
        db_list = _db_list_plans(kind, profile)
        if db_list:
            return db_list
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
    # Fáza B.1: sandbox layout — out/profiles/<name>/plans/*.json
    try:
        from core.paths import is_sandbox_mode
        if is_sandbox_mode():
            p_root = os.path.join("out", "profiles")
            if not os.path.isdir(p_root):
                return []
            for name in os.listdir(p_root):
                if name.startswith("_"):
                    continue
                plans_d = os.path.join(p_root, name, "plans")
                if os.path.isdir(plans_d) and any(
                    f.endswith(".json") for f in os.listdir(plans_d)
                ):
                    out.add(name)
            return sorted(out)
    except Exception:
        pass
    # Legacy layout
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
    found = False
    # DB delete (dual storage)
    if _db_available():
        try:
            from db import get_session
            from db.models import Profile as _DbProfile, Plan as _DbPlan
            prof_name = resolve_profile(profile)
            mkt = _current_market()
            with get_session() as s:
                p_prof = s.query(_DbProfile).filter_by(name=prof_name).one_or_none()
                if p_prof:
                    p_plan = s.query(_DbPlan).filter_by(
                        profile_id=p_prof.id, market=mkt,
                        date=str(date_iso), kind=str(kind), step_min=int(step_min)
                    ).one_or_none()
                    if p_plan:
                        s.delete(p_plan)
                        found = True
        except Exception as e:
            print(f"[plan_store.delete_plan {date_iso}] DB delete zlyhal: {e}")
    # JSON delete (vždy)
    p = _path(date_iso, step_min, kind, profile)
    if os.path.exists(p):
        try:
            os.remove(p)
            found = True
        except OSError:
            pass
    return found


# ════════════════════════════════════════════════════════════════════════════
# DB helpers (Fáza 1.10)
# ════════════════════════════════════════════════════════════════════════════

def _db_get_profile_id(prof_name: str) -> Optional[int]:
    """Resolve profile.id z mena. Vráti None ak profil neexistuje v DB."""
    try:
        from db import get_session
        from db.models import Profile as _DbProfile
        with get_session() as s:
            p = s.query(_DbProfile).filter_by(name=prof_name).one_or_none()
            return p.id if p else None
    except Exception:
        return None


def _db_load_plan(date_iso: str, step_min: int, kind: str,
                   profile: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Načíta plán z DB a zostaví dict v rovnakom tvare ako JSON."""
    try:
        from db import get_session
        from db.models import Plan as _DbPlan, PlanSlot as _DbPlanSlot
        prof_name = resolve_profile(profile)
        mkt = _current_market()
        pid = _db_get_profile_id(prof_name)
        if pid is None:
            return None
        with get_session() as s:
            p = s.query(_DbPlan).filter_by(
                profile_id=pid, market=mkt,
                date=str(date_iso), kind=str(kind), step_min=int(step_min)
            ).one_or_none()
            if p is None:
                return None
            slots = (s.query(_DbPlanSlot).filter_by(plan_id=p.id)
                       .order_by(_DbPlanSlot.slot_idx).all())
            # Rekonštrukcia schedule dict
            n = len(slots)
            schedule = {sch_key: [0.0] * n for sch_key in _SLOT_COLUMNS.keys()}
            for slot in slots:
                i = slot.slot_idx
                for db_col, sch_key in _REVERSE_SLOT_COLUMNS.items():
                    val = getattr(slot, db_col, 0.0)
                    schedule[sch_key][i] = float(val) if val is not None else 0.0
            return {
                "date": p.date, "step_min": p.step_min, "kind": p.kind,
                "profile": prof_name,
                "generated_at": p.generated_at,
                "params": dict(p.params or {}),
                "block_planned_discharge": bool(p.block_planned_discharge),
                "zco_bias_w": float(p.zco_bias_w or 0.0),
                "rt_freedom": bool(p.rt_freedom),
                "mults": list(p.mults or []) or None,
                "rt_mask": list(p.rt_mask or []) or None,
                "schedule": schedule,
                "summary": dict(p.summary or {}),
                "meta": dict(p.meta or {}),
            }
    except Exception as e:
        print(f"[plan_store._db_load_plan {date_iso}] zlyhal: {e}")
        return None


def _db_save_plan(date_iso: str, step_min: int, kind: str,
                   profile: Optional[str], body: Dict[str, Any]) -> bool:
    """Upsert plánu do DB (Plan + PlanSlot riadky). Vracia True pri úspechu."""
    try:
        from db import get_session
        from db.models import Plan as _DbPlan, PlanSlot as _DbPlanSlot
        prof_name = body.get("profile") or resolve_profile(profile)
        mkt = _current_market()
        pid = _db_get_profile_id(prof_name)
        if pid is None:
            print(f"[plan_store._db_save_plan] profil '{prof_name}' nie je v DB → skip")
            return False
        with get_session() as s:
            existing = s.query(_DbPlan).filter_by(
                profile_id=pid, market=mkt,
                date=str(date_iso), kind=str(kind), step_min=int(step_min)
            ).one_or_none()
            if existing:
                existing.generated_at = body["generated_at"]
                existing.params = body.get("params", {})
                existing.summary = body.get("summary", {})
                existing.meta = body.get("meta", {})
                existing.mults = body.get("mults") or []
                existing.rt_mask = body.get("rt_mask") or []
                existing.block_planned_discharge = bool(body.get("block_planned_discharge", False))
                existing.zco_bias_w = float(body.get("zco_bias_w") or 0.0)
                existing.rt_freedom = bool(body.get("rt_freedom", True))
                # Vymaž staré sloty
                s.query(_DbPlanSlot).filter_by(plan_id=existing.id).delete()
                plan_id = existing.id
            else:
                p = _DbPlan(
                    profile_id=pid, market=mkt,
                    date=str(date_iso), kind=str(kind), step_min=int(step_min),
                    generated_at=body["generated_at"],
                    params=body.get("params", {}), summary=body.get("summary", {}),
                    meta=body.get("meta", {}),
                    mults=body.get("mults") or [], rt_mask=body.get("rt_mask") or [],
                    block_planned_discharge=bool(body.get("block_planned_discharge", False)),
                    zco_bias_w=float(body.get("zco_bias_w") or 0.0),
                    rt_freedom=bool(body.get("rt_freedom", True)),
                )
                s.add(p)
                s.flush()
                plan_id = p.id
            # Vlož sloty
            schedule = body.get("schedule") or {}
            n = max((len(schedule.get(k, [])) for k in _SLOT_COLUMNS), default=0)
            for i in range(n):
                slot_kwargs = {"plan_id": plan_id, "slot_idx": i}
                for sch_key, db_col in _SLOT_COLUMNS.items():
                    arr = schedule.get(sch_key, [])
                    try:
                        slot_kwargs[db_col] = float(arr[i]) if arr[i] is not None else 0.0
                    except (IndexError, TypeError, ValueError):
                        slot_kwargs[db_col] = 0.0
                s.add(_DbPlanSlot(**slot_kwargs))
        return True
    except Exception as e:
        print(f"[plan_store._db_save_plan {date_iso}] zlyhal: {e}")
        return False


def _db_has_plan(date_iso: str, step_min: int, kind: str,
                  profile: Optional[str] = None) -> bool:
    try:
        from db import get_session
        from db.models import Plan as _DbPlan
        prof_name = resolve_profile(profile)
        mkt = _current_market()
        pid = _db_get_profile_id(prof_name)
        if pid is None:
            return False
        with get_session() as s:
            return s.query(_DbPlan).filter_by(
                profile_id=pid, market=mkt,
                date=str(date_iso), kind=str(kind), step_min=int(step_min)
            ).first() is not None
    except Exception:
        return False


def _db_list_plans(kind: Optional[str] = None,
                    profile: Optional[str] = None) -> List[Dict[str, Any]]:
    try:
        from db import get_session
        from db.models import Plan as _DbPlan
        prof_name = resolve_profile(profile)
        mkt = _current_market()
        pid = _db_get_profile_id(prof_name)
        if pid is None:
            return []
        out = []
        with get_session() as s:
            q = s.query(_DbPlan).filter_by(profile_id=pid, market=mkt)
            if kind is not None:
                q = q.filter_by(kind=str(kind))
            for p in q.order_by(_DbPlan.date, _DbPlan.step_min, _DbPlan.kind).all():
                out.append({
                    "date": p.date, "step_min": p.step_min, "kind": p.kind,
                    "path": f"db://plan/{p.id}",
                    "generated_at": p.generated_at,
                    "profile": prof_name,
                })
        return out
    except Exception as e:
        print(f"[plan_store._db_list_plans] zlyhal: {e}")
        return []
