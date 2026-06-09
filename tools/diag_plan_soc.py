# -*- coding: utf-8 -*-
"""diag_plan_soc.py — Bug #642 (2026-06-09)

Offline feasibility check uloženého plánu. Vezme plán z plan_store, sleduje SOC
trajektóriu s presnou eff_c/eff_d dynamikou a identifikuje sloty kde LP nominoval
fyzicky nemožné množstvo (SOC by spadlo pod soc_min + reserve alebo nad soc_max).

Užitočné keď vidíš veľkú odchýlku v /livesim a chceš vedieť či to je vinou
LP plánu (infeasible) alebo iného layer-u (VDT, RT, šum).

Použitie:
    python3 -m tools.diag_plan_soc --profile VW_simulacia_2 --day 2026-06-08
    python3 -m tools.diag_plan_soc --profile X --day Y --step 60 --kind plan
    python3 -m tools.diag_plan_soc --profile X --day Y --json   # JSON output

Príklad výstupu:
    Plán pre 'VW_simulacia_2' dňa 2026-06-08 (kind=plan, step=60)
    Profile: batt=6000 kW / 6000 kWh, soc_min=5% + reserve=5% = eff_min=10%
             eff_c=0.95, eff_d=0.95, soc_init=5%, soc_max=100%, term=5%
    Plán: peak batt_kw=5500, sum nákup=3500 kWh, sum predaj=6750 kWh
    SOC range: 5–100% (eff_min=10%)
    SOC violations: 3 (sloty 19, 20, 21 pod eff_min)
    Hodina 19:00: plán di=5500 kWh, SOC[18]=95%, SOC[19]=−4.8% → INFEASIBLE
        batt potrebuje 5500/0.95 = 5789 kWh z kapacity, dostupné = 5700-300=5400 kWh
        max realne vybitie: 5400 × 0.95 = 5130 kWh = 5130 kW → 370 kW pod planom
"""
from __future__ import annotations
import argparse
import json
import sys
from typing import Optional, Dict, Any


def _load_profile_params(profile: str) -> Dict[str, Any]:
    """Vráti batt parametre profilu."""
    import profiles as _pr
    prof = _pr.load_profile(profile) or {}
    plan = prof.get("plan") or {}
    return {
        "batt_kw": float(plan.get("batt_kw") or 100.0),
        "batt_kwh": float(plan.get("batt_kwh") or 200.0),
        "eff_c": float(plan.get("eff_c") or 0.95),
        "eff_d": float(plan.get("eff_d") or 0.95),
        "soc_min_pct": float(plan.get("soc_min") or plan.get("soc_min_pct") or 5.0),
        "soc_max_pct": float(plan.get("soc_max") or plan.get("soc_max_pct") or 100.0),
        "soc_init_pct": float(plan.get("soc_init") or plan.get("soc_init_pct") or 50.0),
        "terminal_soc_pct": (float(plan.get("terminal_soc")) if plan.get("terminal_soc") is not None
                              else None),
        "soc_reserve_pct": float(plan.get("soc_reserve_pct") or 0.0),
        "grid_kw_export": float(plan.get("grid_kw_export") or plan.get("grid_kw") or 100.0),
        "grid_kw_import": float(plan.get("grid_kw_import") or plan.get("grid_kw") or 100.0),
        "max_export_kwh_day": float(plan.get("max_export_kwh_day") or 0.0) or None,
        "max_import_kwh_day": float(plan.get("max_import_kwh_day") or 0.0) or None,
    }


def _load_plan(profile: str, day: str, kind: str, step_min: int) -> Dict[str, Any]:
    """Vráti dict s schedule arrays."""
    import plan_store as _ps
    p = _ps.load_plan_safe(day, step_min, kind, profile=profile)
    if not p:
        raise RuntimeError(f"Plán nenájdený: profile={profile}, day={day}, kind={kind}, step={step_min}")
    return p


def simulate_feasibility(plan_dict: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """Per-slot simulácia SOC s presnou eff dynamikou.

    Returns dict:
        {
            "soc_path_pct": List[float] (T+1 hodnôt),
            "violations": List[(slot, kind, soc_value, ...details)],
            "summary": {peak_batt, sum_buy_kwh, sum_sell_kwh, soc_min_seen, soc_max_seen, ...}
        }
    """
    sched = plan_dict.get("schedule") or {}
    batt_arr = sched.get("batt_kw") or []
    grid_arr = sched.get("grid_kwh") or []
    T = len(batt_arr)
    step_min = int(plan_dict.get("step_min") or 60)
    dt_h = max(step_min, 1) / 60.0

    cap = params["batt_kwh"]
    eff_c = params["eff_c"]
    eff_d = params["eff_d"]
    soc_min = params["soc_min_pct"]
    soc_max = params["soc_max_pct"]
    reserve = params["soc_reserve_pct"]
    soc_init = params["soc_init_pct"]
    batt_kw_max = params["batt_kw"]
    eff_min = soc_min + reserve
    eff_max = soc_max - reserve

    soc_path = [soc_init]
    cur_soc_kwh = (soc_init / 100.0) * cap
    violations = []
    peak_batt = 0.0
    sum_buy = 0.0
    sum_sell = 0.0

    for t in range(T):
        batt_kw = float(batt_arr[t] or 0.0)        # + vybíja, − nabíja
        # kWh do/zo siete za slot
        if batt_kw >= 0:
            di_kwh = batt_kw * dt_h                 # vybitie do siete
            ch_kwh = 0.0
            sum_sell += di_kwh
        else:
            di_kwh = 0.0
            ch_kwh = -batt_kw * dt_h
            sum_buy += ch_kwh
        peak_batt = max(peak_batt, abs(batt_kw))
        # Δ SOC v kWh: nabíjanie pridá ch * eff_c, vybíjanie odoberie di / eff_d
        delta_kwh = ch_kwh * eff_c - di_kwh / max(0.01, eff_d)
        new_soc_kwh = cur_soc_kwh + delta_kwh
        new_soc_pct = (new_soc_kwh / cap) * 100.0

        # Kontroly
        slot_violations = []
        if batt_kw > 0.5:                           # vybíjací slot
            if new_soc_pct < eff_min - 0.5:
                # batt nemá dosť energie — chcel vybiť di_kwh do siete ale to znamená
                # spotrebovať di_kwh/eff_d z kapacity. Dostupné = cur_soc_kwh - soc_min_kwh
                # (alebo - eff_min_kwh ak chceme buffer)
                avail_for_dis_kwh = max(0.0, cur_soc_kwh - (eff_min/100.0)*cap)
                max_di_into_grid = avail_for_dis_kwh * eff_d
                max_kw_real = max_di_into_grid / dt_h
                slot_violations.append({
                    "kind": "below_eff_min_discharge",
                    "slot": t,
                    "soc_end_pct": round(new_soc_pct, 1),
                    "plan_batt_kw": round(batt_kw, 1),
                    "max_real_kw": round(max_kw_real, 1),
                    "shortfall_kw": round(batt_kw - max_kw_real, 1),
                })
            if abs(batt_kw) > batt_kw_max * 1.01:
                slot_violations.append({
                    "kind": "exceeds_batt_kw_discharge",
                    "slot": t, "plan_batt_kw": round(batt_kw, 1),
                    "batt_kw_max": round(batt_kw_max, 1),
                })
        elif batt_kw < -0.5:                        # nabíjací slot
            if new_soc_pct > eff_max + 0.5:
                avail_for_chg_kwh = max(0.0, (eff_max/100.0)*cap - cur_soc_kwh)
                max_ch_from_grid = avail_for_chg_kwh / eff_c
                max_kw_real = max_ch_from_grid / dt_h
                slot_violations.append({
                    "kind": "above_eff_max_charge",
                    "slot": t,
                    "soc_end_pct": round(new_soc_pct, 1),
                    "plan_batt_kw": round(batt_kw, 1),
                    "max_real_kw": round(max_kw_real, 1),
                    "excess_kw": round(abs(batt_kw) - max_kw_real, 1),
                })
            if abs(batt_kw) > batt_kw_max * 1.01:
                slot_violations.append({
                    "kind": "exceeds_batt_kw_charge",
                    "slot": t, "plan_batt_kw": round(batt_kw, 1),
                    "batt_kw_max": round(batt_kw_max, 1),
                })
        violations.extend(slot_violations)

        # Aktualizuj SOC (s clip na fyzické limity 0-100%)
        cur_soc_kwh = max(0.0, min(cap, new_soc_kwh))
        soc_path.append((cur_soc_kwh / cap) * 100.0)

    return {
        "soc_path_pct": soc_path,
        "violations": violations,
        "summary": {
            "peak_batt_kw": round(peak_batt, 1),
            "sum_buy_kwh": round(sum_buy, 1),
            "sum_sell_kwh": round(sum_sell, 1),
            "net_kwh": round(sum_buy - sum_sell, 1),
            "soc_min_seen": round(min(soc_path), 1),
            "soc_max_seen": round(max(soc_path), 1),
            "soc_end": round(soc_path[-1], 1),
            "violation_count": len(violations),
            "max_dam_export_kwh_day": params.get("max_export_kwh_day"),
            "max_dam_import_kwh_day": params.get("max_import_kwh_day"),
            "max_dam_export_violated": (params.get("max_export_kwh_day") is not None
                                         and sum_sell > params.get("max_export_kwh_day")*1.01),
            "max_dam_import_violated": (params.get("max_import_kwh_day") is not None
                                         and sum_buy > params.get("max_import_kwh_day")*1.01),
        }
    }


def main():
    ap = argparse.ArgumentParser(description="Diag feasibility uloženého plánu")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--day", required=True, help="YYYY-MM-DD")
    ap.add_argument("--kind", default="plan", choices=["plan", "dentrh", "dam_d1"])
    ap.add_argument("--step", type=int, default=60, choices=[15, 60])
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--verbose", "-v", action="store_true", help="Per-slot detail")
    args = ap.parse_args()

    try:
        params = _load_profile_params(args.profile)
        plan_d = _load_plan(args.profile, args.day, args.kind, args.step)
    except Exception as e:
        print(f"ERR: {e}", file=sys.stderr)
        sys.exit(1)

    result = simulate_feasibility(plan_d, params)

    if args.json:
        print(json.dumps({
            "profile": args.profile, "day": args.day, "kind": args.kind, "step": args.step,
            "params": params,
            "result": result,
        }, indent=2))
        return

    # Human-readable summary
    s = result["summary"]
    print(f"━━━ Plán pre '{args.profile}' dňa {args.day} (kind={args.kind}, step={args.step}) ━━━")
    print(f"Profile: batt={params['batt_kw']:.0f} kW / {params['batt_kwh']:.0f} kWh")
    print(f"         soc_min={params['soc_min_pct']:.0f}% + reserve={params['soc_reserve_pct']:.0f}% "
          f"= eff_min={params['soc_min_pct']+params['soc_reserve_pct']:.0f}%")
    print(f"         eff_c={params['eff_c']:.3f}, eff_d={params['eff_d']:.3f}, "
          f"soc_init={params['soc_init_pct']:.0f}%, soc_max={params['soc_max_pct']:.0f}%, "
          f"term={params['terminal_soc_pct']}")
    print(f"         Max DAM export={params['max_export_kwh_day']} kWh/deň, "
          f"import={params['max_import_kwh_day']} kWh/deň")
    print()
    print(f"Plán: peak batt_kw={s['peak_batt_kw']:.0f}, "
          f"nákup={s['sum_buy_kwh']:.0f} kWh, predaj={s['sum_sell_kwh']:.0f} kWh, "
          f"net={s['net_kwh']:+.0f} kWh")
    print(f"SOC trajektória: {s['soc_min_seen']:.0f}–{s['soc_max_seen']:.0f}%, "
          f"koniec={s['soc_end']:.0f}%")
    if s['max_dam_export_violated']:
        print(f"⚠ Max DAM export prekročený: predaj {s['sum_sell_kwh']:.0f} > "
              f"limit {params['max_export_kwh_day']:.0f} kWh/deň")
    if s['max_dam_import_violated']:
        print(f"⚠ Max DAM import prekročený: nákup {s['sum_buy_kwh']:.0f} > "
              f"limit {params['max_import_kwh_day']:.0f} kWh/deň")
    print()
    if s["violation_count"] == 0:
        print("✓ Plán je fyzicky FEASIBLE — žiadny slot nepadol pod soc_min+reserve ani nad soc_max-reserve")
    else:
        print(f"✗ Plán má {s['violation_count']} INFEASIBLE slotov:")
        for v in result["violations"][:20]:
            if v["kind"] == "below_eff_min_discharge":
                print(f"  Slot {v['slot']:2d}: plán vybiť {v['plan_batt_kw']:.0f} kW, "
                      f"SOC by spadlo na {v['soc_end_pct']:.1f}% (< eff_min). "
                      f"Reálne max: {v['max_real_kw']:.0f} kW → shortfall {v['shortfall_kw']:+.0f} kW")
            elif v["kind"] == "above_eff_max_charge":
                print(f"  Slot {v['slot']:2d}: plán nabiť {v['plan_batt_kw']:.0f} kW, "
                      f"SOC by stúplo na {v['soc_end_pct']:.1f}% (> eff_max). "
                      f"Reálne max: {v['max_real_kw']:.0f} kW → excess {v['excess_kw']:+.0f} kW")
            elif v["kind"].startswith("exceeds_batt_kw"):
                print(f"  Slot {v['slot']:2d}: plán {v['plan_batt_kw']:+.0f} kW prekročil "
                      f"batt_kw_max {v['batt_kw_max']:.0f} kW")
        if len(result["violations"]) > 20:
            print(f"  ... a ďalších {len(result['violations']) - 20}")
    if args.verbose:
        print()
        print("Per-slot detail:")
        sched = plan_d.get("schedule") or {}
        batt = sched.get("batt_kw") or []
        for t in range(len(batt)):
            print(f"  {t:2d}h: batt_kw={batt[t]:+8.1f}  "
                  f"soc_start={result['soc_path_pct'][t]:5.1f}%  "
                  f"soc_end={result['soc_path_pct'][t+1]:5.1f}%")


if __name__ == "__main__":
    main()
