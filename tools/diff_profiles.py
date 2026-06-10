#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diff dvoch profilov — porovná každý perzistovaný stav (config, plány,
overrides, VDT cache, capacity ledger, ui_settings, plan_store).

Použitie:
    python3 -m tools.diff_profiles VW_simulacia_2 VW_test_novy

Výstup: side-by-side rozdiely. Identické riadky skryté (--all pre všetko).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_APP = _HERE.parent
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        return f"<ERROR: {e}>"


def _gather(profile: str) -> dict:
    out = {"_profile": profile}

    # 1. Profile config (plan + dentrh + rt + joint_lp + mode)
    import profiles as pr
    p = pr.load_profile(profile) or {}
    out["mode"] = p.get("mode", "?")
    out["plan.soc_init"] = (p.get("plan") or {}).get("soc_init")
    out["plan.soc_min"] = (p.get("plan") or {}).get("soc_min")
    out["plan.soc_max"] = (p.get("plan") or {}).get("soc_max")
    out["plan.soc_reserve_pct"] = (p.get("plan") or {}).get("soc_reserve_pct")
    out["plan.terminal_soc"] = (p.get("plan") or {}).get("terminal_soc")
    out["plan.batt_kw"] = (p.get("plan") or {}).get("batt_kw")
    out["plan.batt_kwh"] = (p.get("plan") or {}).get("batt_kwh")
    out["plan.use_rt"] = (p.get("plan") or {}).get("use_rt")
    out["plan.aggressive_rt"] = (p.get("plan") or {}).get("aggressive_rt")
    out["plan.ftv_balance"] = (p.get("plan") or {}).get("ftv_balance")
    out["plan.joint_lp"] = (p.get("plan") or {}).get("joint_lp")
    out["plan.no_planned_discharge"] = (p.get("plan") or {}).get("no_planned_discharge")
    out["plan.allow_grid_charge"] = (p.get("plan") or {}).get("allow_grid_charge")
    out["plan.allow_curtail"] = (p.get("plan") or {}).get("allow_curtail")
    out["plan.zco_bias_w"] = (p.get("plan") or {}).get("zco_bias_w")
    out["plan.max_export_kwh_day"] = (p.get("plan") or {}).get("max_export_kwh_day")
    out["plan.max_import_kwh_day"] = (p.get("plan") or {}).get("max_import_kwh_day")
    out["dentrh.soc_init"] = (p.get("dentrh") or {}).get("soc_init")
    out["rt.use_rt"] = (p.get("rt") or {}).get("use_rt")

    # 2. plan_store — count of saved plans + dates
    import plan_store as ps
    plans = _safe(lambda: ps.list_plans(profile=profile) or [])
    out["plan_store.count"] = len(plans) if isinstance(plans, list) else plans
    if isinstance(plans, list) and plans:
        dates = sorted({(pl.get("date_iso") or pl.get("date") or "?")
                          for pl in plans if isinstance(pl, dict)})
        out["plan_store.first_date"] = dates[0] if dates else None
        out["plan_store.last_date"] = dates[-1] if dates else None

    # 3. plan_overrides — count
    from core.paths import _data_dir
    try:
        import market as mk
        market = str(mk.get_active_market()).lower()
    except Exception:
        market = "sk"
    over_dir = os.path.join(_data_dir(market), profile, "plan_overrides")
    out["plan_overrides.count"] = _safe(
        lambda: len([f for f in os.listdir(over_dir) if f.endswith(".json")])
        if os.path.exists(over_dir) else 0)

    # 4. VDT cache + paper trades
    vdt_cache = os.path.join(_data_dir(market), f"vdt_live_plan_{profile}.json")
    out["vdt_cache.exists"] = os.path.exists(vdt_cache)
    if out["vdt_cache.exists"]:
        try:
            with open(vdt_cache) as f:
                cache = json.load(f)
            out["vdt_cache.trades_count"] = len(cache.get("trades", []))
        except Exception as e:
            out["vdt_cache.error"] = str(e)

    # 5. Capacity reservation ledger
    try:
        from db.session import SessionFactory
        from db.models import Profile as ProfileM, BattCapacityReservation
        with SessionFactory() as s:
            pid_row = s.query(ProfileM).filter_by(name=profile).first()
            if pid_row:
                cnt = s.query(BattCapacityReservation).filter_by(
                    profile_id=pid_row.id).count()
                out["batt_reservations.count"] = cnt
    except Exception as e:
        out["batt_reservations.error"] = str(e)

    # 6. ui_settings (per-port shared — môže by tieni profile)
    ui_path = os.path.join(_data_dir(market), "ui_settings.json")
    if os.path.exists(ui_path):
        try:
            with open(ui_path) as f:
                ui = json.load(f)
            out["ui_settings.plan.soc_init"] = (ui.get("plan") or {}).get("soc_init")
            out["ui_settings.dentrh.soc_init"] = (ui.get("dentrh") or {}).get("soc_init")
            out["ui_settings.plan.joint_lp"] = (ui.get("plan") or {}).get("joint_lp")
        except Exception as e:
            out["ui_settings.error"] = str(e)

    # 7. ActiveProfile per-port (DB)
    try:
        from db.models import ActiveProfile
        with SessionFactory() as s:
            rows = s.query(ActiveProfile).all()
            out["active_profile.ports"] = {r.port: r.profile_name for r in rows}
    except Exception as e:
        out["active_profile.error"] = str(e)

    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("profile_a", help="Prvý profil (referenčný)")
    p.add_argument("profile_b", help="Druhý profil (porovnávaný)")
    p.add_argument("--all", action="store_true",
                   help="Vypíš aj identické riadky (default len rozdiely)")
    args = p.parse_args()

    a = _gather(args.profile_a)
    b = _gather(args.profile_b)

    keys = sorted(set(a.keys()) | set(b.keys()))
    print(f"{'KEY':<40} | {args.profile_a:<28} | {args.profile_b:<28} | Δ")
    print("─" * 130)
    for k in keys:
        va = a.get(k, "—")
        vb = b.get(k, "—")
        same = (str(va) == str(vb))
        if same and not args.all:
            continue
        marker = "  " if same else "▸▸"
        va_s = str(va)[:28]
        vb_s = str(vb)[:28]
        print(f"{k:<40} | {va_s:<28} | {vb_s:<28} | {marker}")


if __name__ == "__main__":
    main()
