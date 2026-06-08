# -*- coding: utf-8 -*-
"""
auto_control.py — Fáza A.5 paper trading mód.

Každú štvrťhodinu (cez scheduler cron) prečíta D-1 plán pre aktuálny slot,
vyráta plánovaný batt setpoint a buď:
  - SIMULATION mode (DEFAULT): loguje rozhodnutie do CSV; žiadny zápis do Bender
  - REAL mode (HARD-BLOCKED): vyžaduje out/auto_control_unlock.json

Cieľ paper trading módu: nechať systém bežať pár dní, sledovať čo by robil,
overiť konzistenciu plánov vs realio meraniach, BEZ rizika reálneho zápisu.

Storage:
  out/<market>/auto_control_log.csv — 1 riadok per rozhodnutie
  out/auto_control_unlock.json      — manuálny safety unlock pre REAL mode

API:
  compute_setpoint_for_now(profile=None, market=None) -> dict | None
  apply_setpoint(setpoint, dry_run=True) -> dict (log riadok)
  read_log(market=None, n=100) -> list[dict]
  is_real_unlocked() -> bool
  kill_switch_active() -> bool

Vstavané guardy:
  - REAL mode bez unlock súboru → RuntimeError
  - REAL mode + active profile.mode != "real" → RuntimeError
  - Kill switch (out/<market>/auto_control_killed.flag) → skip celého cyklu
  - Setpoint mimo [-batt_kw, +batt_kw] → clip + warning v logu

Read-only voči OKTE — nikdy neposielame na externe trhy.
Real mode = lokálne Bender batt riadenie (Manual_Plan + Param3 W).
"""
from __future__ import annotations
import csv
import datetime as dt
import json
import os
from typing import Dict, Any, Optional, List


MODE_SIM = "simulation"
MODE_REAL = "real"

UNLOCK_FILE = "out/auto_control_unlock.json"
UNLOCK_TOKEN = "I_UNDERSTAND_THIS_WRITES_TO_BENDER"


# ---------------------------------------------------------------------------
# Path resolvers (market-aware)
# ---------------------------------------------------------------------------

def _data_dir(market: Optional[str] = None) -> str:
    """Vráti out/cz alebo out/sk."""
    try:
        import market as _mk
        return _mk.data_dir(market)
    except Exception:
        return "out"


def _log_path(market: Optional[str] = None) -> str:
    return os.path.join(_data_dir(market), "auto_control_log.csv")


def _killswitch_path(market: Optional[str] = None) -> str:
    return os.path.join(_data_dir(market), "auto_control_killed.flag")


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------

def is_real_unlocked() -> bool:
    """REAL mode je odblokovaný iba ak existuje súbor s explicit tokenom."""
    if not os.path.exists(UNLOCK_FILE):
        return False
    try:
        with open(UNLOCK_FILE) as f:
            data = json.load(f)
        return data.get("token") == UNLOCK_TOKEN
    except Exception:
        return False


def kill_switch_active(market: Optional[str] = None) -> bool:
    """Ak existuje flag súbor, scheduler musí preskočiť cyklus."""
    return os.path.exists(_killswitch_path(market))


def set_kill_switch(market: Optional[str] = None) -> None:
    """Aktivuje kill switch — vytvorí flag súbor."""
    p = _killswitch_path(market)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(dt.datetime.now().isoformat(timespec="seconds"))


def clear_kill_switch(market: Optional[str] = None) -> None:
    """Deaktivuje kill switch — zmaže flag súbor."""
    p = _killswitch_path(market)
    try:
        os.remove(p)
    except OSError:
        pass


def _check_profile_is_real(profile: str) -> bool:
    """REAL mode vyžaduje že active profile má mode='real'."""
    try:
        import profiles as _pr
        p = _pr.load_profile(profile)
        if isinstance(p, dict):
            return str(p.get("mode", "")).lower() == "real"
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Compute setpoint
# ---------------------------------------------------------------------------

def _current_slot(now: Optional[dt.datetime] = None) -> int:
    """Aktuálny 15-min slot index 0..95."""
    now = now or dt.datetime.now()
    return (now.hour * 60 + now.minute) // 15


def _get_profile_mode(profile: str) -> str:
    """Vráti mode profilu — 'real' / 'simulation' / 'unknown'."""
    try:
        import profiles as _pr
        p = _pr.load_profile(profile)
        if isinstance(p, dict):
            return str(p.get("mode") or "unknown").lower()
    except Exception:
        pass
    return "unknown"


def _get_current_soc_pct(profile: Optional[str] = None,
                          plan: Optional[Dict[str, Any]] = None,
                          slot_idx: Optional[int] = None
                          ) -> Optional[float]:
    """Vráti aktuálny SOC podľa profile.mode.

    real profile → realio_db.read_recent() (skutočné meranie)
    simulation   → plánovaný soc_pct[slot_idx] z plan_store (D-1 plán)
    unknown      → realio_db ako fallback
    """
    mode = _get_profile_mode(profile) if profile else "unknown"

    # Simulation profile: vezmi plánovaný SOC z plan_store pre aktuálny slot
    if mode == "simulation" and plan is not None and slot_idx is not None:
        try:
            sched = plan.get("schedule")
            if isinstance(sched, dict) and "soc_pct" in sched:
                arr = sched["soc_pct"]
                step_min = int(plan.get("step_min", 15))
                idx = (slot_idx // 4) if step_min == 60 else slot_idx
                if 0 <= idx < len(arr):
                    v = arr[idx]
                    if v is not None:
                        return float(v)
        except Exception:
            pass
        return None   # sim profil bez plánu → žiadne SOC

    # Real profile (alebo neznámy): realio_db meranie
    try:
        import realio as _r
        df = _r.read_recent(n_minutes=60)
        if df is None or df.empty or "batt_soc_pct" not in df.columns:
            return None
        sub = df[df["batt_soc_pct"].notna()]
        if len(sub) == 0:
            return None
        return float(sub["batt_soc_pct"].iloc[-1])
    except Exception:
        return None


def _load_plan_for_today(profile: str) -> Optional[Dict[str, Any]]:
    """Načíta D-1 plán pre dnes z plan_store (cascade dentrh → plan)."""
    try:
        import plan_store as _ps
    except Exception:
        return None
    today_iso = dt.date.today().isoformat()
    # Preferuj dentrh (15-min) — to je natívny formát pre auto-control
    plan = _ps.load_plan_safe(today_iso, step_min=15, kind="dentrh", profile=profile)
    if plan is not None:
        return plan
    # Fallback na 60-min plan
    plan = _ps.load_plan_safe(today_iso, step_min=60, kind="plan", profile=profile)
    return plan


def _extract_batt_kw_for_slot(plan: Dict[str, Any], slot_idx: int,
                                profile: Optional[str] = None) -> Optional[float]:
    """Zo schedule plánu vyrátá net batt setpoint v kW pre 15-min slot.

    Konvencia: kladné = vybíjanie (export), záporné = nabíjanie (import).

    `schedule` v plan_store je dict-of-lists s 24/96 hodnotami per stĺpec:
      schedule["batt_kw"] = list (preferované — uložené priamo)
      schedule["_discharge_kw"] − schedule["_charge_kw"] (fallback ekvivalent)

    Pre 60-min plán slot_idx//4 mapuje 15-min slot na hodinu.

    Bug V (2026-06-07): ak `profile` je dané, pripočíta VDT realized z paper_trades.csv
    pre 15-min slot_idx. Efektívny setpoint = D-1 + Σ VDT. Žiadny LP recalc — len
    čítanie persistovaných paper trades. Konvencia VDT je rovnaká (+ discharge, − charge).
    """
    sched = plan.get("schedule")
    if not isinstance(sched, dict):
        # Ani plán neexistuje — pozri či máme aspoň VDT trade
        dam_v = None
    else:
        step_min = int(plan.get("step_min", 15))
        idx = (slot_idx // 4) if step_min == 60 else slot_idx

        def _arr_at(key):
            arr = sched.get(key)
            if arr is None or idx < 0 or idx >= len(arr):
                return None
            try:
                v = arr[idx]
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        # Priorita: batt_kw priamo
        dam_v = _arr_at("batt_kw")
        if dam_v is None:
            # Fallback: _discharge_kw − _charge_kw
            di = _arr_at("_discharge_kw") or 0.0
            ch = _arr_at("_charge_kw") or 0.0
            if di or ch:
                dam_v = di - ch

    # Bug V: pripočítaj VDT realized pre slot_idx (VDT je vždy 15-min)
    vdt_kw = 0.0
    if profile:
        try:
            import vdt_state as _vs
            vdt_arr = _vs.get_realized_batt_kw(profile, dt_h=0.25)
            if isinstance(vdt_arr, list) and 0 <= slot_idx < len(vdt_arr):
                vdt_kw = float(vdt_arr[slot_idx] or 0.0)
        except Exception:
            pass

    # Ak D-1 plán nedal nič a VDT nedal nič → None (no setpoint)
    if dam_v is None and abs(vdt_kw) < 1e-9:
        return None

    return float(dam_v or 0.0) + vdt_kw


# ---------------------------------------------------------------------------
# Per-profile enable/disable config (paper trading opt-in)
# ---------------------------------------------------------------------------

def _profile_config_path(market: Optional[str] = None) -> str:
    return os.path.join(_data_dir(market), "auto_control_profiles.json")


def _load_profile_config(market: Optional[str] = None) -> Dict[str, Any]:
    p = _profile_config_path(market)
    if not os.path.exists(p):
        return {"enabled": []}
    try:
        with open(p) as f:
            data = json.load(f)
        if not isinstance(data.get("enabled"), list):
            data["enabled"] = []
        return data
    except Exception:
        return {"enabled": []}


def _save_profile_config(cfg: Dict[str, Any], market: Optional[str] = None) -> None:
    p = _profile_config_path(market)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def get_enabled_profiles(market: Optional[str] = None) -> set:
    """Vráti set názvov profilov ktoré majú paper trading zapnutý.

    Default: žiadny — užívateľ musí explicit zapnúť cez UI alebo set_profile_enabled().
    """
    cfg = _load_profile_config(market)
    return set(cfg.get("enabled") or [])


def set_profile_enabled(name: str, enabled: bool,
                          market: Optional[str] = None) -> None:
    """Zapne/vypne paper trading pre konkrétny profil."""
    cfg = _load_profile_config(market)
    en = set(cfg.get("enabled") or [])
    if enabled:
        en.add(name)
    else:
        en.discard(name)
    cfg["enabled"] = sorted(en)
    _save_profile_config(cfg, market)


def compute_setpoint_for_now(profile: Optional[str] = None,
                               market: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Vyráta plánovaný batt setpoint pre aktuálny 15-min slot.

    Vracia dict so všetkými info pre dry-run log alebo None ak nemá plán.
    """
    # Resolve profile
    try:
        import plan_store as _ps
        prof = _ps.resolve_profile(profile)
    except Exception:
        prof = profile or "default"

    # Resolve market
    try:
        import market as _mk
        mk = market or _mk.get_active_market()
    except Exception:
        mk = market or "cz"

    now = dt.datetime.now()
    slot_idx = _current_slot(now)
    h, m = divmod(slot_idx * 15, 60)
    slot_label = f"{h:02d}:{m:02d}"

    # Bug O4: SAFETY CHECK — VDT state musí byť kompletný pred akýmkoľvek setpointom.
    # vdt_state.compute_current_state() vráti data_completeness=False ak chýba D-1 plán
    # alebo iné kľúčové dáta. V tom prípade neexekvovať ani logovať setpoint.
    try:
        import vdt_state as _vs
        _state = _vs.compute_current_state(profile=prof)
        if not _state.get("data_completeness"):
            print(f"[auto_control] {prof}: vdt_state insufficient — "
                  f"missing={_state.get('missing_items', [])}, skipping setpoint")
            return {
                "ts": now.isoformat(timespec="seconds"),
                "market": mk, "profile": prof, "profile_mode": _get_profile_mode(prof),
                "slot_idx": slot_idx, "slot_label": slot_label,
                "setpoint_kw": None,
                "soc_pct": float(_state.get("current_soc_pct", 0.0)),
                "soc_source": "vdt_state (insufficient)",
                "reason": f"vdt_state_insufficient: {','.join(_state.get('missing_items', []))}",
                "executed": False,
                "data_completeness": False,
                "missing_items": _state.get("missing_items", []),
            }
    except Exception as _se:
        print(f"[auto_control] {prof}: vdt_state check zlyhal: {_se} — pokračujem cautiously")

    plan = _load_plan_for_today(prof)
    prof_mode = _get_profile_mode(prof)
    soc_source = "realio_db" if prof_mode == "real" else (
        "plan_soc_pct" if prof_mode == "simulation" else "realio_db_fallback")

    if plan is None:
        return {
            "ts": now.isoformat(timespec="seconds"),
            "market": mk, "profile": prof, "profile_mode": prof_mode,
            "slot_idx": slot_idx, "slot_label": slot_label,
            "setpoint_kw": None,
            "soc_pct": _get_current_soc_pct(profile=prof),
            "soc_source": soc_source,
            "reason": "no_plan_in_store",
            "executed": False,
        }

    # Bug V: pass profile aby setpoint zahŕňal aj VDT realized trades (D-1 + VDT)
    setpoint_kw = _extract_batt_kw_for_slot(plan, slot_idx, profile=prof)
    soc_pct = _get_current_soc_pct(profile=prof, plan=plan, slot_idx=slot_idx)
    # Vyber DAM clearing cenu pre aktuálny slot zo schedule.price_eur
    dam_clearing_price = None
    try:
        _sched = plan.get("schedule") or {}
        _prices = _sched.get("price_eur") or []
        _step_min = int(plan.get("step_min", 15))
        _idx_p = (slot_idx // 4) if _step_min == 60 else slot_idx
        if 0 <= _idx_p < len(_prices) and _prices[_idx_p] is not None:
            dam_clearing_price = float(_prices[_idx_p])
    except Exception:
        pass

    # Bezpečnostné clipping na profile batt_kw limit
    try:
        import profiles as _pr
        p = _pr.load_profile(prof)
        batt_kw_max = float((p or {}).get("batt_kw", 500.0))
    except Exception:
        batt_kw_max = 500.0

    clipped = False
    if setpoint_kw is not None:
        if abs(setpoint_kw) > batt_kw_max:
            setpoint_kw = max(-batt_kw_max, min(batt_kw_max, setpoint_kw))
            clipped = True

    # Smer a kWh — pre normalizovaný "trade-like" záznam v logu
    _direction = "idle"
    _kwh_trade = 0.0
    if setpoint_kw is not None:
        dt_h = (plan.get("step_min", 15)) / 60.0 if plan.get("step_min") else 0.25
        if setpoint_kw > 1.0:
            _direction = "SELL"
            _kwh_trade = float(setpoint_kw) * dt_h
        elif setpoint_kw < -1.0:
            _direction = "BUY"
            _kwh_trade = float(abs(setpoint_kw)) * dt_h

    return {
        "ts": now.isoformat(timespec="seconds"),
        "market": mk, "profile": prof, "profile_mode": prof_mode,
        "slot_idx": slot_idx, "slot_label": slot_label,
        "direction": _direction,
        "kwh": round(_kwh_trade, 2),
        "setpoint_kw": setpoint_kw,
        "price_eur_mwh": (round(dam_clearing_price, 2)
                           if dam_clearing_price is not None else None),
        "dam_clearing_eur_mwh": (round(dam_clearing_price, 2)
                                  if dam_clearing_price is not None else None),
        "soc_pct": soc_pct,
        "soc_source": soc_source,
        "batt_kw_max": batt_kw_max,
        "clipped": clipped,
        "plan_kind": plan.get("kind"),
        "plan_step_min": plan.get("step_min"),
        "reason": "ok" if setpoint_kw is not None else "no_setpoint_in_plan",
        "executed": False,
    }


# ---------------------------------------------------------------------------
# Denný prehľad obchodov pre konkrétny profil
# ---------------------------------------------------------------------------

def get_day_schedule(profile: str) -> Dict[str, Any]:
    """Vráti denný plán pre profil — všetky 24/96 sloty + projektovaný SOC.

    Pre UI dashboard: čo sa kedy ide obchodovať a aký bude SOC.
    Returns:
      {
        "ok": bool, "profile": str, "profile_mode": str,
        "step_min": int, "kind": str,
        "slots": [
          {"slot_idx", "slot_label", "setpoint_kw", "soc_pct",
           "price_eur_mwh", "action": "BUY|SELL|idle"},
          ...
        ],
        "summary": {
          "total_buy_kwh", "total_sell_kwh",
          "soc_start_pct", "soc_end_pct",
          "n_buy_slots", "n_sell_slots",
          "expected_profit_eur"
        }
      }
    """
    plan = _load_plan_for_today(profile)
    if plan is None:
        return {"ok": False, "profile": profile, "error": "no_plan_in_store",
                "slots": [], "summary": {}}
    sched = plan.get("schedule")
    if not isinstance(sched, dict):
        return {"ok": False, "profile": profile, "error": "invalid_schedule",
                "slots": [], "summary": {}}
    step_min = int(plan.get("step_min", 15))
    n_slots = len(sched.get("batt_kw", []) or sched.get("_charge_kw", []) or [])
    if n_slots == 0:
        return {"ok": False, "profile": profile, "error": "empty_schedule",
                "slots": [], "summary": {}}

    batt_arr = sched.get("batt_kw") or []
    ch_arr = sched.get("_charge_kw") or []
    di_arr = sched.get("_discharge_kw") or []
    soc_arr = sched.get("soc_pct") or []
    price_arr = sched.get("price_eur") or []
    ex_arr = sched.get("_export_kwh") or []
    im_arr = sched.get("_import_kwh") or []

    dt_h = step_min / 60.0
    slot_min_per_idx = step_min
    slots_out: List[Dict[str, Any]] = []
    total_buy_kwh = 0.0
    total_sell_kwh = 0.0
    n_buy = 0; n_sell = 0
    revenue = 0.0   # SELL × price/1000
    cost = 0.0      # BUY × (price+fee)/1000 (fee neuvažuje na tomto detaile)

    for i in range(n_slots):
        h, m = divmod(i * slot_min_per_idx, 60)
        h2, m2 = divmod((i + 1) * slot_min_per_idx, 60)
        label = f"{h:02d}:{m:02d}-{h2:02d}:{m2:02d}"
        # batt_kw konvencia: + = vybíjanie (SELL), − = nabíjanie (BUY)
        setp = None
        if i < len(batt_arr):
            try:
                setp = float(batt_arr[i] or 0.0)
            except Exception:
                setp = None
        if setp is None:
            di = float(di_arr[i] or 0.0) if i < len(di_arr) else 0.0
            ch = float(ch_arr[i] or 0.0) if i < len(ch_arr) else 0.0
            setp = di - ch
        soc = None
        if i < len(soc_arr):
            try:
                soc = float(soc_arr[i])
            except Exception:
                soc = None
        price = None
        if i < len(price_arr):
            try:
                price = float(price_arr[i])
            except Exception:
                price = None
        # Akcia
        if setp > 1.0:
            action = "SELL"
            kwh = setp * dt_h
            total_sell_kwh += kwh
            n_sell += 1
            if price is not None:
                revenue += price * kwh / 1000.0
        elif setp < -1.0:
            action = "BUY"
            kwh = abs(setp) * dt_h
            total_buy_kwh += kwh
            n_buy += 1
            if price is not None:
                cost += price * kwh / 1000.0
        else:
            action = "idle"
        slots_out.append({
            "slot_idx": i,
            "slot_label": label,
            "setpoint_kw": round(setp, 1),
            "soc_pct": round(soc, 1) if soc is not None else None,
            "price_eur_mwh": round(price, 2) if price is not None else None,
            "action": action,
        })

    soc_start = slots_out[0]["soc_pct"] if slots_out else None
    soc_end = slots_out[-1]["soc_pct"] if slots_out else None

    return {
        "ok": True, "profile": profile,
        "profile_mode": _get_profile_mode(profile),
        "step_min": step_min,
        "kind": plan.get("kind"),
        "slots": slots_out,
        "summary": {
            "total_buy_kwh": round(total_buy_kwh, 1),
            "total_sell_kwh": round(total_sell_kwh, 1),
            "soc_start_pct": soc_start,
            "soc_end_pct": soc_end,
            "n_buy_slots": n_buy,
            "n_sell_slots": n_sell,
            "expected_profit_eur": round(revenue - cost, 2),
        }
    }


# ---------------------------------------------------------------------------
# Apply setpoint (dry-run = log only; real = Bender write but BLOCKED)
# ---------------------------------------------------------------------------

LOG_HEADERS = ["ts", "market", "profile", "slot_idx", "slot_label",
                "direction", "kwh", "setpoint_kw",
                "price_eur_mwh", "dam_clearing_eur_mwh",
                "soc_pct", "batt_kw_max", "clipped",
                "plan_kind", "plan_step_min", "mode", "executed",
                "reason", "error"]


def _migrate_log_csv(path: str) -> None:
    """Ak existujúci log CSV má staršiu schému, prepíše hlavičku + doplní prázdne stĺpce.

    Idempotent. Po pridaní polí price/kwh/direction (úprava 2026-06-04) treba migrate.
    """
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            rows = list(reader)
        if not rows:
            return
        header = rows[0]
        if all(h in header for h in LOG_HEADERS):
            return   # už migrované
        # Postav new_rows tak aby každý starý riadok mal hodnoty pre LOG_HEADERS
        old_idx = {h: i for i, h in enumerate(header)}
        new_rows = [LOG_HEADERS]
        for r in rows[1:]:
            new_row = []
            for h in LOG_HEADERS:
                if h in old_idx and old_idx[h] < len(r):
                    new_row.append(r[old_idx[h]])
                else:
                    new_row.append("")
            new_rows.append(new_row)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            for r in new_rows:
                w.writerow(r)
        os.replace(tmp, path)
        print(f"[auto_control] migrated log CSV: pridané polia "
              f"direction/kwh/price ({len(new_rows)-1} riadkov)")
    except Exception as e:
        print(f"[auto_control log migration] zlyhalo: {e}")


def _append_log(setpoint_dict: Dict[str, Any]) -> None:
    """Pripoji riadok do CSV log súboru pre aktívny trh."""
    p = _log_path(setpoint_dict.get("market"))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    _migrate_log_csv(p)
    file_exists = os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_HEADERS, extrasaction="ignore")
        if not file_exists:
            w.writeheader()
        # Doplň default kľúče
        row = {k: setpoint_dict.get(k, "") for k in LOG_HEADERS}
        w.writerow(row)
    # DB dual write (Fáza 1.12)
    if os.environ.get("USE_DB", "0").strip() in ("1", "true", "True", "yes"):
        _append_log_db(setpoint_dict)


def _append_log_db(setpoint_dict: Dict[str, Any]) -> None:
    """DB-side log: AutoControlEvent insert (append-only)."""
    try:
        from db import get_session
        from db.models import Profile as _DbProfile, AutoControlEvent as _DbACE
        prof_name = str(setpoint_dict.get("profile") or "")
        ts = str(setpoint_dict.get("ts") or
                  setpoint_dict.get("timestamp") or
                  setpoint_dict.get("datetime") or "")
        if not ts:
            from datetime import datetime as _dt
            ts = _dt.now().isoformat(timespec="seconds")
        market = str(setpoint_dict.get("market") or "cz")

        def _f(k):
            v = setpoint_dict.get(k)
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        def _b(k):
            v = setpoint_dict.get(k)
            if v in (None, ""):
                return None
            s = str(v).strip().lower()
            return True if s in ("true", "1", "yes") else False if s in ("false", "0", "no") else None

        with get_session() as s:
            prof_id = None
            if prof_name:
                p_row = s.query(_DbProfile).filter_by(name=prof_name).one_or_none()
                if p_row:
                    prof_id = p_row.id
            s.add(_DbACE(
                ts=ts, profile_id=prof_id, market=market,
                soc_pct=_f("soc_pct"),
                batt_kw_setpoint=_f("setpoint_kw") or _f("batt_kw_setpoint"),
                mode=str(setpoint_dict.get("mode") or "dry_run"),
                dry_run=bool(setpoint_dict.get("dry_run", True)),
                reason=str(setpoint_dict.get("reason") or "")[:255],
                margin_check=_b("margin_check"),
                soc_terminal_ok=_b("soc_terminal_ok"),
                grid_capacity_ok=_b("grid_capacity_ok"),
                plan_available=_b("plan_available"),
                setpoint_clipped=_b("setpoint_clipped"),
                grid_kw_min=_f("grid_kw_min"),
                grid_kw_max=_f("grid_kw_max"),
                price_eur_mwh=_f("price_eur_mwh"),
                qty_kwh=_f("qty_kwh"),
                notes=str(setpoint_dict.get("notes") or "")[:500],
            ))
    except Exception as e:
        print(f"[auto_control._append_log_db] zlyhal: {e}")


def apply_setpoint(setpoint: Dict[str, Any], dry_run: bool = True) -> Dict[str, Any]:
    """Aplikuje (alebo iba zaloguje) setpoint.

    dry_run=True (DEFAULT) — len log, žiadny zápis do Bender.
    dry_run=False — HARD-BLOCKED bez unlock súboru a real-mode profilu.
    """
    if setpoint is None:
        return {"mode": MODE_SIM, "executed": False, "error": "no_setpoint"}

    out = dict(setpoint)
    out["mode"] = MODE_SIM if dry_run else MODE_REAL

    if dry_run:
        # Simulation: len log
        out["executed"] = False
        out["error"] = ""
        _append_log(out)
        return out

    # REAL mode — guardy
    if not is_real_unlocked():
        err = (f"REAL mode HARD-BLOCKED — chýba {UNLOCK_FILE} s tokenom "
               f"'{UNLOCK_TOKEN}'. Nezapísalo sa nič do Bender.")
        out["executed"] = False
        out["error"] = err
        _append_log(out)
        raise RuntimeError(err)

    if not _check_profile_is_real(setpoint.get("profile", "")):
        err = (f"REAL mode + profile.mode != 'real' — nezhoda, nezapísalo sa "
               f"nič do Bender.")
        out["executed"] = False
        out["error"] = err
        _append_log(out)
        raise RuntimeError(err)

    # OK — povolený REAL zápis (až keď bude užívateľ pripravený)
    sp_kw = setpoint.get("setpoint_kw")
    if sp_kw is None:
        out["executed"] = False
        out["error"] = "no_setpoint_kw"
        _append_log(out)
        return out

    try:
        import realio as _r
        # 15-min plán mode: jediný setpoint, nie celý 96-prvkový array.
        # write_battery_plan_15min očakáva list — pošleme single-element wrapper
        # alebo voláme priamy write API; tu je len placeholder kým fáza A.5 REAL nie je aktívna.
        res = _r.write_battery_plan_15min([sp_kw], source="auto_control")
        out["executed"] = bool(res.get("ok"))
        out["error"] = res.get("error", "") or ""
    except Exception as e:
        out["executed"] = False
        out["error"] = f"realio write zlyhal: {e}"

    _append_log(out)
    return out


# ---------------------------------------------------------------------------
# VDT extras logger (CURTAIL_FTV / LOAD_COVER / BUY / SELL — virtual)
# ---------------------------------------------------------------------------

def log_vdt_extras_for_current_slot(profile: str) -> int:
    """Pre aktuálny 15-min slot zaloguje VDT extras z cache ako trade záznamy.

    Cieľ: užívateľ vidí v /auto_control "normálny záznam" s kWh + cenou pre každý
    simulovaný VDT obchod (BUY/SELL/CURTAIL_FTV/LOAD_COVER) — nielen +0.0 setpoint
    z D-1 plánu. Všetko je virtuálne (mode=SIMULATION), žiadny reálny zápis.

    Vracia počet zaolgovaných záznamov.
    """
    n = 0
    # Bug UU (2026-06-08, refactored Fáza A.3): GATE na use_vdt:false cez centrálny
    # helper v core.schemas.vdt. Ak má profile explicit use_vdt=False, NESMIEME
    # logovať VDT extras trades. Predtým sa logovali aj pre Simulacia_Coop (kde
    # joint_lp.use_vdt=false) → tieto fake VDT trades sa potom skreslili plan_batt_kw
    # cez Bug V/X aggregát → /livesim realita úplne diverged voči plánu.
    try:
        from core.schemas.vdt import should_log_vdt_for_profile as _vdt_gate
        if not _vdt_gate(profile):
            # NEHLÁSIŤ ako warning každú minútu
            return 0
    except Exception as _e_uu:
        print(f"[auto_control.vdt_extras] {profile}: use_vdt gate zlyhal: {_e_uu}")
        # Bezpečnejšie pokračovať (legacy profily bez joint_lp.use_vdt)
    # Bug O4: SAFETY CHECK pred logovaním VDT extras trades — ak vdt_state hovorí
    # že chýbajú dáta, VDT advisor cache je nedôveryhodná → preskočiť.
    try:
        import vdt_state as _vs
        _state = _vs.compute_current_state(profile=profile)
        if not _state.get("data_completeness"):
            print(f"[auto_control.vdt_extras] {profile}: vdt_state insufficient — "
                  f"missing={_state.get('missing_items', [])}, skipping VDT extras")
            return 0
    except Exception as _se:
        print(f"[auto_control.vdt_extras] {profile}: vdt_state check zlyhal: {_se}")
        # Pokračuj cautiously — necháme fallback handle aby cache scenár nebol blocked úplne
    try:
        import vdt_live_advisor as _adv
        cache = _adv.load_cache(profile=profile) or {}
    except Exception:
        return 0
    fp = (cache or {}).get("full_plan") or []
    if not fp:
        return 0
    # Aktuálny slot label "HH:MM"
    try:
        from datetime import datetime, timezone
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("Europe/Bratislava"))
        except Exception:
            now = datetime.now()
        hh = now.hour
        mm = (now.minute // 15) * 15
        slot_label = f"{hh:02d}:{mm:02d}"
        slot_idx = hh * 4 + (mm // 15)
    except Exception:
        return 0

    try:
        mk = (cache.get("market") or "").lower()
    except Exception:
        mk = ""

    for entry in fp:
        try:
            sl = entry.get("slot")
            if not sl or sl != slot_label:
                continue
            action = (entry.get("action") or "").upper()
            kwh = float(entry.get("kwh", 0.0) or 0.0)
            if abs(kwh) < 0.5:
                continue
            price = entry.get("price_eur_mwh", entry.get("price"))
            try:
                price_f = float(price) if price is not None else None
            except Exception:
                price_f = None
            # direction kanonizácia
            dir_map = {
                "BUY": "VDT_BUY", "SELL": "VDT_SELL",
                "CURTAIL_FTV": "CURTAIL_FTV", "LOAD_COVER": "LOAD_COVER",
                "CHARGE": "BUY", "DISCHARGE": "SELL",
            }
            direction = dir_map.get(action, action or "VDT")
            reason = entry.get("reason") or entry.get("comment") or ""
            log_row = {
                "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                "market": mk,
                "profile": profile,
                "slot_idx": slot_idx,
                "slot_label": slot_label,
                "direction": direction,
                "kwh": round(kwh, 2),
                "setpoint_kw": "",
                "price_eur_mwh": (round(price_f, 2) if price_f is not None else ""),
                "dam_clearing_eur_mwh": "",
                "soc_pct": entry.get("soc_after", ""),
                "batt_kw_max": "",
                "clipped": False,
                "plan_kind": "vdt_extras",
                "plan_step_min": 15,
                "mode": MODE_SIM,
                "executed": False,
                "reason": reason or "vdt_extras",
                "error": "",
            }
            _append_log(log_row)
            n += 1
        except Exception:
            continue
    return n


# ---------------------------------------------------------------------------
# Log reader
# ---------------------------------------------------------------------------

def read_log(market: Optional[str] = None, n: int = 100) -> List[Dict[str, Any]]:
    """Vráti posledných n záznamov z auto_control_log.csv."""
    p = _log_path(market)
    if not os.path.exists(p):
        return []
    rows = []
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows[-n:] if n > 0 else rows


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("auto_control.py smoke test (SIMULATION mode)")
    print(f"  REAL mode unlocked: {is_real_unlocked()}")
    print(f"  Kill switch active: {kill_switch_active()}")

    sp = compute_setpoint_for_now()
    if sp is None:
        print("  ✗ compute_setpoint_for_now vrátilo None")
    else:
        print(f"  ✓ Setpoint pre teraz: {sp.get('slot_label')} | "
              f"{sp.get('setpoint_kw')} kW | SOC={sp.get('soc_pct')} | "
              f"reason={sp.get('reason')}")
        # Dry-run apply
        res = apply_setpoint(sp, dry_run=True)
        print(f"  ✓ Dry-run executed: mode={res.get('mode')}, "
              f"logged={res.get('executed') is False}")

    # Test že real mode raise bez unlock súboru
    print("\n  Test: REAL mode bez unlock súboru musí raise")
    try:
        apply_setpoint({"market": "cz", "profile": "default",
                         "setpoint_kw": 100.0}, dry_run=False)
        print("  ✗ REAL mode preslo bez raise — to je BUG")
    except RuntimeError as e:
        print(f"  ✓ REAL mode zablokovaný: {str(e)[:80]}...")

    # Posledné log riadky
    log = read_log(n=3)
    print(f"\n  Posledné log riadky: {len(log)}")
    for r in log[-3:]:
        print(f"    {r.get('ts')} | {r.get('mode')} | "
              f"slot={r.get('slot_label')} | sp={r.get('setpoint_kw')} kW")
