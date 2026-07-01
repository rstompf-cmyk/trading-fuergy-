# -*- coding: utf-8 -*-
"""test_vdt_audit_real_soc.py — VDT-AUDIT-REAL-SOC (#28, 2026-07-01).

VDT otvárací predaj musí rezervovať SOC pre DAM záväzok voči REÁLNEMU SOC, nie
voči plánovej trajektórii od 00:00. Keď je realita teraz nižšia než plán štart,
audit MUSÍ byť prísnejší (menej/žiaden povolený predaj), inak VDT nominuje čo
realita nedodá → večerná odchýlka.

Kill-switch: sim_from_slot/current_soc_pct_at_si=None → seed z plánu (spätná kompat).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core.soc_use_audit as sua


def _state(start_pct, cur_pct, dam):
    return {"ok": True, "batt_kwh": 1000.0, "eff_c": 0.95, "eff_d": 0.95,
            "soc_min_pct": 5.0, "soc_max_pct": 100.0, "soc_reserve_pct": 0.0,
            "current_soc_pct": cur_pct, "start_soc_pct": start_pct,
            "dam_nomination_kwh": dam, "vdt_realized_kwh": [0.0] * 96}


def _dam_evening(total_kwh, n=8, s0=80):
    d = [0.0] * 96
    for i in range(s0, s0 + n):
        d[i] = total_kwh / n
    return d


def test_real_soc_stricter_than_plan_start():
    """Plán štart 50 %, realita 20 %; DAM 420 večer. Real-SOC audit prísnejší."""
    os.environ["VDT_AUDIT_NOWORSEN"] = "1"
    st = _state(50.0, 20.0, _dam_evening(420.0))
    # Bez reálneho SOC (seed z plánu 50 %) — povolí nejaký predaj
    r_plan = sua.audit_action("t", "2026-07-01", 40, "discharge", 100.0,
                              source="vdt", today_state=st)
    # S reálnym SOC 20 % seedom v slote 40
    r_real = sua.audit_action("t", "2026-07-01", 40, "discharge", 100.0,
                              source="vdt", today_state=st,
                              current_soc_pct_at_si=20.0, sim_from_slot=40)
    assert r_real["allowed_kwh"] <= r_plan["allowed_kwh"] + 1e-6, \
        f"real-SOC audit musí byť ≤ plán ({r_real['allowed_kwh']} vs {r_plan['allowed_kwh']})"
    assert r_real["decision"] == "reject", \
        f"z reálnych 20 % má večer padnúť: {r_real['decision']}"


def test_backcompat_no_realsoc_unchanged():
    """sim_from_slot=None → identické so starým správaním (seed v si)."""
    os.environ["VDT_AUDIT_NOWORSEN"] = "1"
    st = _state(50.0, 50.0, _dam_evening(420.0))
    r_old = sua.audit_action("t", "2026-07-01", 40, "discharge", 100.0,
                             source="vdt", today_state=st,
                             current_soc_pct_at_si=50.0)                   # bez sim_from_slot
    r_new = sua.audit_action("t", "2026-07-01", 40, "discharge", 100.0,
                             source="vdt", today_state=st,
                             current_soc_pct_at_si=50.0, sim_from_slot=40)  # seed = si
    assert abs(r_old["allowed_kwh"] - r_new["allowed_kwh"]) < 1e-6, \
        "seed v si (sim_from_slot=si) musí byť identický s default (None)"


if __name__ == "__main__":
    test_real_soc_stricter_than_plan_start()
    test_backcompat_no_realsoc_unchanged()
    print("VDT-AUDIT-REAL-SOC OK")
