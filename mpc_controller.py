# -*- coding: utf-8 -*-
"""mpc_controller.py — Real-time joint MPC controller.

Bug CC1 (2026-06-07): Rolling MPC kontrolér ktorý každú minútu (alebo na zavolanie)
spočíta optimálny batt setpoint + VDT trade návrhy + RT korekciu pre zostávajúce
sloty dňa, na základe:
  • Aktuálneho SOC (z vdt_state.compute_current_state — single source of truth)
  • Realných FTV/Load hodnôt (1-min cadence pre past, predikcia pre future)
  • DAM cien (z OTE cache) + VDT buy/sell cien (z orderbook)
  • Toggle flags z profile.plan (trade_batt, trade_ftv, trade_load, use_vdt)

Architektúra (užívateľský návrh, 2026-06-07):
  1. Minútový cyklus → update SOC z reality + zobchodované (DAM + VDT realized)
  2. Na základe aktuálneho SOC → joint LP pre zostávajúce sloty
  3. Output → batt setpoint pre AKTUÁLNY slot + VDT návrhy pre future sloty
  4. Cieľ: v každom čase SOC trajectory pokryje uz zobchodovanú energiu

API:
    run_mpc_tick(profile, now=None, market=None) → dict s decisions + diagnostika

Output schema:
    {
        "ok": bool,
        "ts": str (ISO 8601),
        "profile": str,
        "current_slot_idx": int (0..95),
        "current_soc_pct": float,
        "mpc_batt_kw_now": float,         # optimálny batt setpoint TERAZ
        "mpc_full_plan": List[Dict],      # decisions per zostávajúci slot
        "mpc_objective_eur": float,        # LP profit
        "diagnostics": {...}
    }

Žiadna live aplikácia outputov (CC3+). Iba decisions + JSON cache pre debug.
"""
from __future__ import annotations
import os
import json
import datetime as dt
from typing import Optional, Dict, Any, List

import numpy as np


# ────────────────────────── Cache paths ──────────────────────────

def _market_root() -> str:
    """`out/{market}/` cesta pre mpc cache."""
    try:
        import market as _mk
        return _mk.data_dir()   # Bug CC1.1: data_dir, NIE out_root (neexistuje)
    except Exception:
        return "out"


def mpc_cache_path(profile: str) -> str:
    """JSON cache pre posledný MPC tick per profil."""
    safe = profile.replace("/", "_").replace(" ", "_")
    return os.path.join(_market_root(), f"mpc_tick_{safe}.json")


# ────────────────────────── Input loaders ──────────────────────────

def _load_pv_kwh_96(profile: str, today: dt.date,
                     batt_kwp: Optional[float] = None) -> List[float]:
    """Load 96-slot FTV výroba (kWh per 15-min) pre dnes.

    Priorita:
      1. ftv_scenarios (per-day scenár editor)
      2. plan_store schedule.pv_kwh (D-1 plán)
      3. PVF live (data_sources.fetch_pv_forecast)
      4. Default 0 array
    """
    today_iso = today.isoformat()
    # 1. ftv_scenarios
    try:
        import ftv_scenarios as _fs
        sc = _fs.load_scenario(profile, today_iso)
        if sc and "hourly_kw" in sc:
            kw_24 = sc["hourly_kw"]
            # 24h → 96 slotov (1h = 4 sloty)
            return [float(kw_24[h // 4]) * 0.25 for h in range(96)]
    except Exception:
        pass
    # 2. plan_store
    try:
        import plan_store as _ps
        for kind in ("dentrh", "plan"):
            step = 15 if kind == "dentrh" else 60
            if _ps.has_plan(today_iso, step, kind, profile=profile):
                p = _ps.load_plan_safe(today_iso, step, kind, profile=profile)
                if p and "schedule" in p:
                    sch = p["schedule"]
                    pv = sch.get("pv_kwh") or sch.get("pv_kw") or []
                    if pv and len(pv) >= 24:
                        if len(pv) == 24:
                            # 1h → 4× 15-min priemer
                            return [float(pv[h // 4]) / 4.0 for h in range(96)]
                        return [float(x) for x in pv[:96]]
    except Exception:
        pass
    return [0.0] * 96


def _load_load_kwh_96(profile: str, today: dt.date) -> List[float]:
    """Load 96-slot spotreba (kWh per 15-min) pre dnes.

    Priorita:
      1. load_profile per-profil typ deň (weekday/weekend)
      2. plan_store schedule.load_kwh
      3. Default 0
    """
    today_iso = today.isoformat()
    try:
        import load_profile as _lp
        is_weekend = today.weekday() >= 5
        kind = "weekend" if is_weekend else "weekday"
        load_15min = _lp.load_15min(profile, kind=kind)   # 96 kW values
        if load_15min and len(load_15min) >= 96:
            return [float(x) * 0.25 for x in load_15min[:96]]
    except Exception:
        pass
    # Fallback: plan_store
    try:
        import plan_store as _ps
        for k in ("dentrh", "plan"):
            step = 15 if k == "dentrh" else 60
            if _ps.has_plan(today_iso, step, k, profile=profile):
                p = _ps.load_plan_safe(today_iso, step, k, profile=profile)
                if p and "schedule" in p:
                    load = (p["schedule"].get("load_kwh") or
                            p["schedule"].get("load_kw") or [])
                    if load and len(load) >= 24:
                        if len(load) == 24:
                            return [float(load[h // 4]) / 4.0 for h in range(96)]
                        return [float(x) for x in load[:96]]
    except Exception:
        pass
    return [0.0] * 96


def _load_dam_prices_96(today: dt.date) -> List[float]:
    """Load 96-slot DAM ceny (€/MWh) z OTE cache (15-min granularita SK)."""
    try:
        from core.caches import _fetch_ote_cached
        df = _fetch_ote_cached(today)
        if df is not None and not df.empty:
            # DAM má 96 15-min slotov pre SK, 24 hodín pre CZ
            prices = []
            for _, row in df.iterrows():
                try:
                    prices.append(float(row.get("cena_EUR", 0.0)))
                except Exception:
                    prices.append(0.0)
            if len(prices) == 96:
                return prices
            elif len(prices) == 24:
                # expand 24 → 96 (každá hodina = 4× 15-min)
                return [prices[h // 4] for h in range(96)]
    except Exception:
        pass
    return [0.0] * 96


def _load_vdt_prices_96(today: dt.date) -> tuple:
    """Load VDT buy/sell ceny per slot z VDT orderbook (alebo D-1 reference).

    Returns: (buy_prices_96, sell_prices_96) alebo (None, None) ak nedostupné.
    """
    # Pre teraz: pre simplicity = DAM ceny ± malý spread (placeholder)
    # CC1 skeleton — neskôr pripojiť na okte_sk.load_vdt_clearing_prices
    try:
        dam = _load_dam_prices_96(today)
        if dam and any(dam):
            # Approximation: VDT buy ~ DAM + 5%, VDT sell ~ DAM - 5%
            # (Tradenie cez VDT je drahšie ako DAM, preto buy > sell)
            buy = [p * 1.05 for p in dam]
            sell = [p * 0.95 for p in dam]
            return (np.array(buy), np.array(sell))
    except Exception:
        pass
    return (None, None)


# ────────────────────────── MPC tick ──────────────────────────

def run_mpc_tick(profile: str,
                  now: Optional[dt.datetime] = None,
                  market: Optional[str] = None,
                  write_cache: bool = True) -> Dict[str, Any]:
    """Rolling MPC tick pre profil — optimálny plán na zostávajúce sloty dňa.

    Args:
        profile: meno profilu
        now: aktuálny moment (default = teraz)
        market: market override (default = active)
        write_cache: ak True, zapíše output do JSON cache pre debug
    """
    now = now or dt.datetime.now()
    today = now.date()
    ts_iso = now.isoformat(timespec="seconds")

    out: Dict[str, Any] = {
        "ok": False, "ts": ts_iso, "profile": profile,
        "current_slot_idx": 0, "current_soc_pct": 0.0,
        "mpc_batt_kw_now": 0.0, "mpc_full_plan": [],
        "mpc_objective_eur": 0.0,
        "diagnostics": {},
    }

    # 1. Stav (Bug O single source of truth)
    try:
        import vdt_state as _vs
        state = _vs.compute_current_state(profile, today=today, now=now)
    except Exception as e:
        out["reason"] = f"vdt_state failed: {e}"
        return out
    if not state.get("ok"):
        out["reason"] = "vdt_state.data_completeness=False"
        out["diagnostics"]["missing_items"] = state.get("missing_items", [])
        if write_cache:
            _write_cache(profile, out)
        return out

    cur_soc = float(state.get("current_soc_pct", 50.0))
    cur_slot = int(state.get("current_slot_idx", 0))
    batt_kwh_cap = float(state.get("batt_kwh", 200.0))
    out["current_soc_pct"] = round(cur_soc, 2)
    out["current_slot_idx"] = cur_slot

    # 2. Profile params
    try:
        import profiles as _pr
        p = _pr.load_profile(profile) or {}
        plan_params = p.get("plan") or {}
    except Exception:
        plan_params = {}

    # 3. Toggle flags
    trade_batt = bool(plan_params.get("trade_batt", True))
    trade_ftv = bool(plan_params.get("trade_ftv", True))
    trade_load = bool(plan_params.get("trade_load", True))
    use_vdt = bool(plan_params.get("use_vdt", True))
    joint_mpc_enabled = bool(plan_params.get("joint_mpc_enabled", False))

    out["diagnostics"]["flags"] = {
        "trade_batt": trade_batt, "trade_ftv": trade_ftv,
        "trade_load": trade_load, "use_vdt": use_vdt,
        "joint_mpc_enabled": joint_mpc_enabled,
    }

    # 4. Inputs (96-slot full day arrays)
    pv_full = _load_pv_kwh_96(profile, today)
    load_full = _load_load_kwh_96(profile, today)
    dam_full = _load_dam_prices_96(today)
    vdt_buy_full, vdt_sell_full = _load_vdt_prices_96(today)

    out["diagnostics"]["inputs_sum"] = {
        "pv_day_kwh": round(sum(pv_full), 1),
        "load_day_kwh": round(sum(load_full), 1),
        "dam_avg_eur": round(sum(dam_full) / max(1, len(dam_full)), 2),
    }

    # 5. Trim na future sloty (od cur_slot)
    pv_remaining = pv_full[cur_slot:]
    load_remaining = load_full[cur_slot:]
    dam_remaining = dam_full[cur_slot:]
    vdt_buy_remaining = (vdt_buy_full[cur_slot:]
                          if vdt_buy_full is not None else None)
    vdt_sell_remaining = (vdt_sell_full[cur_slot:]
                           if vdt_sell_full is not None else None)
    T = len(pv_remaining)
    out["diagnostics"]["T_remaining"] = T

    if T < 2:
        out["reason"] = "T_remaining < 2 — koniec dňa, nic na optimalizaciu"
        out["ok"] = True
        if write_cache:
            _write_cache(profile, out)
        return out

    # 6. Solve joint LP
    try:
        import joint_lp as _jl
        result = _jl.optimize_joint_day(
            pv_kwh=np.array(pv_remaining),
            load_kwh=np.array(load_remaining),
            dam_price_eur=np.array(dam_remaining),
            batt_kw=float(plan_params.get("batt_kw", 100.0)),
            batt_kwh=batt_kwh_cap,
            eff_c=float(plan_params.get("eff_c", 0.95)),
            eff_d=float(plan_params.get("eff_d", 0.95)),
            soc_min_pct=float(plan_params.get("soc_min", 5.0)),
            soc_max_pct=float(plan_params.get("soc_max", 95.0)),
            soc_init_pct=cur_soc,
            terminal_soc_pct=float(plan_params.get("terminal_soc", 50.0)),
            grid_fee=float(plan_params.get("grid_fee", 22.0)),
            cycle_cost=float(plan_params.get("cycle_cost", 2.0)),
            vdt_buy_price=vdt_buy_remaining,
            vdt_sell_price=vdt_sell_remaining,
            trade_batt=trade_batt,
            trade_ftv=trade_ftv,
            trade_load=trade_load,
            use_vdt=use_vdt,
            dt=0.25,
        )
    except Exception as e:
        out["reason"] = f"joint_lp failed: {e}"
        if write_cache:
            _write_cache(profile, out)
        return out

    # 7. Extract decisions
    batt_kw_arr = result.get("batt_kw", [])
    if len(batt_kw_arr) > 0:
        out["mpc_batt_kw_now"] = float(batt_kw_arr[0])

    # Per-slot full plan
    full_plan = []
    soc_arr = result.get("soc_pct", [])
    ch_arr = result.get("ch_kwh", [])
    di_arr = result.get("di_kwh", [])
    ex_dam = result.get("ex_dam_kwh", [])
    im_dam = result.get("im_dam_kwh", [])
    ex_vdt = result.get("ex_vdt_kwh", [])
    im_vdt = result.get("im_vdt_kwh", [])
    for i in range(T):
        slot_idx = cur_slot + i
        hh = slot_idx // 4
        mm = (slot_idx % 4) * 15
        full_plan.append({
            "slot": f"{hh:02d}:{mm:02d}-{hh:02d}:{mm+15:02d}",
            "slot_idx": slot_idx,
            "batt_kw": float(batt_kw_arr[i]) if i < len(batt_kw_arr) else 0.0,
            "ch_kwh": float(ch_arr[i]) if i < len(ch_arr) else 0.0,
            "di_kwh": float(di_arr[i]) if i < len(di_arr) else 0.0,
            "ex_dam_kwh": float(ex_dam[i]) if i < len(ex_dam) else 0.0,
            "im_dam_kwh": float(im_dam[i]) if i < len(im_dam) else 0.0,
            "ex_vdt_kwh": float(ex_vdt[i]) if i < len(ex_vdt) else 0.0,
            "im_vdt_kwh": float(im_vdt[i]) if i < len(im_vdt) else 0.0,
            "soc_pct": float(soc_arr[i]) if i < len(soc_arr) else 0.0,
        })
    out["mpc_full_plan"] = full_plan
    out["mpc_objective_eur"] = float(result.get("profit_eur", 0.0))
    out["ok"] = True

    if write_cache:
        _write_cache(profile, out)
    return out


def _write_cache(profile: str, data: Dict[str, Any]) -> None:
    path = mpc_cache_path(profile)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        print(f"[mpc_controller] cache zapisany: {path} (ok={data.get('ok')})")
    except Exception as e:
        print(f"[mpc_controller] zapis cache zlyhal: {e} (path={path})")


def load_cache(profile: str) -> Optional[Dict[str, Any]]:
    """Načíta posledný MPC tick z JSON cache."""
    path = mpc_cache_path(profile)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ────────────────────────── CLI smoke test ──────────────────────────

if __name__ == "__main__":
    import sys
    prof = sys.argv[1] if len(sys.argv) > 1 else "VW_simulacia"
    result = run_mpc_tick(prof, write_cache=False)
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("mpc_full_plan",)},
                     indent=2, ensure_ascii=False, default=str))
    if result.get("ok"):
        fp = result.get("mpc_full_plan", [])
        print(f"\nFull plan: {len(fp)} slotov")
        for ent in fp[:5]:
            print(f"  {ent['slot']}: batt={ent['batt_kw']:+.1f}kW · "
                  f"SOC={ent['soc_pct']:.1f}%")
        print(f"  ... ({len(fp)-5} more)" if len(fp) > 5 else "")
