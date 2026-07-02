# -*- coding: utf-8 -*-
"""VDT-AUDIT-DELIVERABLE (2026-07-02) — absolútna dodateľnosť v slote obchodu.

User: „žiadny nákup 3 % pod 100 % a predaj 3 % nad 5 % by nemal byť možný."
Rieši dieru v no-worsen (neclipnutý excess): nabíjanie pri SOC 100 % vytvorí fiktívnu
energiu nad max, ktorá „vykryje" večerný deficit → obchod prejde, hoci realita nedodá.
"""
import core.soc_use_audit as a


def _state(soc_pct, cap=10000.0, evening_kwh=1000.0):
    dam = [0.0] * 96
    for i in range(84, 90):          # večerné vybíjanie → deficit (odhalí no-worsen dieru)
        dam[i] = evening_kwh
    return {"ok": True, "batt_kwh": cap, "eff_c": 0.95, "eff_d": 0.95,
            "soc_min_pct": 5.0, "soc_max_pct": 100.0,
            "dam_nomination_kwh": dam, "vdt_realized_kwh": [0.0] * 96,
            "current_soc_pct": soc_pct, "current_slot_idx": 67,
            "start_soc_pct": soc_pct}


def _audit(soc, direction, kwh, si=67, evening_kwh=1000.0):
    return a.audit_action("X", "2026-07-02", si, direction, kwh, source="vdt",
                          today_state=_state(soc, evening_kwh=evening_kwh),
                          current_soc_pct_at_si=soc, sim_from_slot=si)


def test_charge_at_full_rejected():
    r = _audit(100.0, "charge", 500.0)
    assert r["decision"] == "reject"
    assert "DELIVERABLE" in r["reason"]


def test_charge_within_3pct_of_max_rejected_or_tiny():
    # SOC 98 % > 97 % (max−buffer) → reject (headroom < 0)
    r = _audit(98.0, "charge", 5000.0)
    assert r["decision"] == "reject"


def test_charge_at_96pct_downscaled_to_1pct_headroom():
    # SOC 96 %, buffer 3 % → efekt. max 97 % → headroom 1 % = 100 kWh /eff_c ≈ 105
    r = _audit(96.0, "charge", 5000.0)
    assert r["decision"] == "downscale"
    assert 90.0 <= r["allowed_kwh"] <= 120.0


def test_charge_midrange_accepts_full():
    r = _audit(50.0, "charge", 500.0)
    assert r["decision"] == "accept"
    assert abs(r["allowed_kwh"] - 500.0) < 1e-6


def test_discharge_near_min_rejected():
    # SOC 6 % < 8 % (min+buffer) → predaj nedodateľný
    r = _audit(6.0, "discharge", 500.0)
    assert r["decision"] == "reject"


def test_discharge_midrange_accepts():
    # bez večerného deficitu (feasibilný baseline) → predaj pri 50 % musí prejsť
    r = _audit(50.0, "discharge", 500.0, evening_kwh=0.0)
    assert r["decision"] == "accept"


def test_killswitch_disables(monkeypatch):
    monkeypatch.setenv("VDT_AUDIT_DELIVERABLE", "0")
    r = _audit(100.0, "charge", 500.0)
    # bez deliverable guardu charge@100 % prejde cez no-worsen (nezhoršuje excess)
    assert r["decision"] != "reject" or "DELIVERABLE" not in r["reason"]
