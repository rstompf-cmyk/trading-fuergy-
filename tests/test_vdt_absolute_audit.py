# -*- coding: utf-8 -*-
"""test_vdt_absolute_audit.py — VDT-ABSOLUTE-AUDIT (2026-06-30).

Overuje: pre VDT je SOC audit ABSOLÚTNY — VDT nesmie pretlačiť SOC mimo pásma ANI keď je
baseline trajektória (plán + nahromadený VDT) „už pokazená". Pre RT ostáva delta (ignoruj
baseline-spôsobené violácie). Tým VDT nemôže nad-nominovať → realita dodá → žiadna pokuta.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.soc_use_audit as sua


def _state(start_pct, dam0_kwh):
    """today_state s baseline ktorý UŽ prekročí 100% (start vysoký + DAM nabíja v slot 0).
    Konvencia: dam/scheduled kWh +vybíja / −nabíja (charge = záporné)."""
    dam = [0.0] * 96
    dam[0] = dam0_kwh
    return {"ok": True, "batt_kwh": 1000.0, "eff_c": 0.95, "eff_d": 0.95,
            "soc_min_pct": 5.0, "soc_max_pct": 100.0,
            "current_soc_pct": start_pct, "start_soc_pct": start_pct,
            "dam_nomination_kwh": dam, "vdt_realized_kwh": [0.0] * 96}


def test_vdt_charge_into_full_battery_not_accepted():
    # baseline: start 95% + DAM nabíja 300 kWh v slot 0 → SOC vyletí nad 100% (už pokazené)
    st = _state(95.0, -300.0)
    # VDT chce ešte nabíjať 200 kWh v slot 10 → ABSOLÚTNE neprípustné
    r = sua.audit_action("__test__", "2026-06-30", 10, "charge", 200.0,
                         source="vdt", today_state=st)
    assert r["decision"] != "accept", f"VDT nabíjanie do plnej malo byť odmietnuté/orezané: {r['decision']}"
    assert r["allowed_kwh"] < 200.0 - 1e-6, f"allowed_kwh malo byť orezané: {r['allowed_kwh']}"


def test_vdt_feasible_charge_accepted():
    # zdravý baseline (start 30%, žiadne DAM) → VDT nabíjanie 100 kWh je OK
    st = _state(30.0, 0.0)
    r = sua.audit_action("__test__", "2026-06-30", 10, "charge", 100.0,
                         source="vdt", today_state=st)
    assert r["decision"] == "accept", f"feasibilné VDT nabíjanie malo prejsť: {r}"


if __name__ == "__main__":
    test_vdt_charge_into_full_battery_not_accepted()
    test_vdt_feasible_charge_accepted()
    print("VDT-ABSOLUTE-AUDIT OK")
