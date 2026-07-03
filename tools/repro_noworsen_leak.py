# -*- coding: utf-8 -*-
"""repro_noworsen_leak.py — hypotéza: no-worsen (celkový excess) pustí obchod,
ktorý SVOJ vlastný slot / neskorší slot pretlačí MIMO hraníc SOC, lebo inde excess
klesne a súčet „nie je horší". = „obchod mimo hranice SOC".

Scenár: baseline je NAD max skoro ráno (excess hore). VDT PREDAJ (discharge) skoro
ráno zníži SOC → zmenší above-max excess, ALE večer to isté vybíjanie pretlačí SOC
POD min (nová below-min violácia). Ak no-worsen súčet nevzrastie → ACCEPT (diera).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core.soc_use_audit as sua


def _state(cur_pct, dam):
    return {"ok": True, "batt_kwh": 1000.0, "eff_c": 0.95, "eff_d": 0.95,
            "soc_min_pct": 5.0, "soc_max_pct": 100.0, "soc_reserve_pct": 0.0,
            "current_soc_pct": cur_pct, "start_soc_pct": cur_pct,
            "dam_nomination_kwh": dam, "vdt_realized_kwh": [0.0] * 96}


def main():
    # baseline: štart 95%, DAM NABÍJA ešte skoro ráno (nad 100 = above-max excess),
    # potom večer VYBÍJA na dno. Navrhneme VDT PREDAJ o 08:00 (slot 32).
    dam = [0.0] * 96
    for i in range(4, 12):      # 01:00-03:00 nabíja → nad max
        dam[i] = -200.0
    for i in range(80, 92):     # večer vybíja
        dam[i] = 250.0
    st = _state(95.0, dam)

    for mode in ("noworsen", "absolute"):
        os.environ["VDT_AUDIT_NOWORSEN"] = "1" if mode == "noworsen" else "0"
        r = sua.audit_action("t", "2026-07-01", 32, "discharge", 300.0,
                             source="vdt", today_state=st,
                             current_soc_pct_at_si=95.0, sim_from_slot=32)
        # skontroluj či VÝSLEDNÁ trajektória má slot mimo [5,100]
        path = r.get("soc_path_proposed") or []
        below = [(i, round(v, 1)) for i, v in enumerate(path) if v < 5.0 - 1e-6]
        above = [(i, round(v, 1)) for i, v in enumerate(path) if v > 100.0 + 1e-6]
        print(f"[{mode}] decision={r['decision']} allowed={r.get('allowed_kwh')}  "
              f"below_min_slots={len(below)} above_max_slots={len(above)}")
        if r["decision"] != "reject" and (below or above):
            print(f"    ⚠ DIERA: obchod prešiel, ale trajektória je MIMO hraníc "
                  f"(below={below[:3]}, above={above[:3]})")


if __name__ == "__main__":
    main()
