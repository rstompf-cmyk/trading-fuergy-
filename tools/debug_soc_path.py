# -*- coding: utf-8 -*-
"""tools/debug_soc_path.py — Diagnostika SOC trajektórie krok po kroku.

Bug S1: User videl že 20:15-20:30 idle slot mal SOC stúpajúci o +12% bez akcie.
Tento script vypíše každý slot s:
  - VDT action (z full_plan cache)
  - DAM nominácia (z vdt_state.dam_nomination_kwh)
  - Σ batt kWh action (VDT + DAM)
  - SOC pred / SOC po (z full_plan cache vs kumulatívny výpočet)
  - Anomália highlight: idle slot kde SOC ≠ predchádzajúci

Použitie:
    docker compose exec trading-fuergy python3 -m tools.debug_soc_path VW_simulacia

Spustenie bez profile argument použije aktívny.
"""
from __future__ import annotations
import sys
import os


def main():
    sys.path.insert(0, "/app")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    profile = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        from core.profile_resolver import get_active as _ga
        profile = profile or _ga()
    except Exception:
        profile = profile or "default"

    print(f"=== SOC trajektória debug pre profil: {profile} ===\n")

    # 1. Načítaj VDT cache
    try:
        import vdt_live_advisor as _adv
        cache = _adv.load_cache(profile=profile)
    except Exception as e:
        print(f"FAIL: cannot load VDT cache: {e}")
        return 1
    if cache is None:
        print("FAIL: vdt_advisor cache neexistuje — beží scheduler?")
        return 1
    full_plan = cache.get("full_plan") or []
    if not full_plan:
        print("FAIL: full_plan je prázdny v cache")
        return 1
    print(f"Cache OK: {len(full_plan)} slotov, ts={cache.get('ts', '?')}")

    # 2. Načítaj vdt_state pre kumulatívny výpočet
    try:
        import vdt_state as _vs
        state = _vs.compute_current_state(profile)
    except Exception as e:
        print(f"WARN: vdt_state zlyhal: {e}")
        state = None

    # 3. Načítaj profile params (eff_c, eff_d, batt_kwh)
    try:
        import profiles as _pr
        p = _pr.load_profile(profile) or {}
        plan = p.get("plan") or {}
        batt_kwh = float(plan.get("batt_kwh", 800))
        eff_c = float(plan.get("eff_c", 0.95))
        eff_d = float(plan.get("eff_d", 0.95))
    except Exception:
        batt_kwh, eff_c, eff_d = 800.0, 0.95, 0.95

    dam_kwh = (state or {}).get("dam_nomination_kwh", [0.0] * 96)
    vdt_kwh = (state or {}).get("vdt_realized_kwh", [0.0] * 96)
    soc_path = (state or {}).get("soc_path_pct", [])

    print(f"Profile params: batt={batt_kwh:.0f} kWh, eff_c={eff_c}, eff_d={eff_d}")
    print(f"vdt_state: dam slots={len(dam_kwh)}, vdt slots={len(vdt_kwh)}, soc_path={len(soc_path)}")
    print()

    # 4. Header
    print(f"{'SLOT':<14} {'VDT_ACT':<10} {'VDT_kWh':>9} {'DAM_kWh':>9} {'Σbatt':>8} "
              f"{'SOC_AD':>8} {'SOC_VS':>8} {'Δ':>6} {'ANOMÁLIA'}")
    print("-" * 110)

    # 5. Iterate cez full_plan + porovnaj s vdt_state kumulatívnym výpočtom
    prev_soc_ad = None
    anomalies = 0
    for i, p_slot in enumerate(full_plan):
        slot = str(p_slot.get("slot", "?"))
        action = str(p_slot.get("action", "?"))
        vdt_kwh_slot = float(p_slot.get("kwh", 0) or 0)
        soc_after_pct = float(p_slot.get("soc_after_pct", 0) or 0)

        # DAM nominácia pre slot
        try:
            dam_slot = float(dam_kwh[i]) if i < len(dam_kwh) else 0.0
        except Exception:
            dam_slot = 0.0

        # Σ batt akcia (VDT signed + DAM signed)
        # VDT: charge → +kWh charge, discharge → +kWh discharge (positive),
        #      ale pre batt signed: charge=-, discharge=+
        vdt_signed = (-vdt_kwh_slot if action == "charge"
                          else vdt_kwh_slot if action == "discharge" else 0.0)
        # DAM (z vdt_state.dam_nomination_kwh): + = discharge, - = charge
        total_batt = dam_slot + vdt_signed

        # SOC z vdt_state soc_path
        soc_vs = float(soc_path[i + 1]) if (soc_path and i + 1 < len(soc_path)) else 0.0

        # Anomália: idle s rozdielom SOC > 0.1%
        delta = (soc_after_pct - prev_soc_ad) if prev_soc_ad is not None else 0.0
        anomaly = ""
        if action == "idle" and abs(delta) > 0.1 and abs(total_batt) < 0.5:
            anomaly = "⚠ IDLE ALE SOC SA MENÍ"
            anomalies += 1

        print(f"{slot:<14} {action:<10} {vdt_kwh_slot:>9.1f} {dam_slot:>+9.1f} "
                  f"{total_batt:>+8.1f} {soc_after_pct:>8.2f} {soc_vs:>8.2f} "
                  f"{delta:>+6.2f} {anomaly}")
        prev_soc_ad = soc_after_pct

    print()
    print(f"Anomálií detegovaných: {anomalies}")
    if anomalies > 0:
        print("\n⚠ AKCIA: skontroluj prečo `idle` sloty majú SOC zmenu.")
        print("Možné príčiny:")
        print("  1. VDT optimizer nezahrňuje DAM commits do SOC trajectory")
        print("  2. soc_after_pct sa zapisuje pred aplikáciou akcie (off-by-one)")
        print("  3. Cache obsahuje stale data zo zlého optimizer behu")
    return 0


if __name__ == "__main__":
    sys.exit(main())
