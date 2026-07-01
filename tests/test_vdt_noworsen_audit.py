# -*- coding: utf-8 -*-
"""test_vdt_noworsen_audit.py — VDT-AUDIT-NOWORSEN (2026-07-01).

Keď je baseline SOC UŽ infeasibilný (D-1/nahromadený VDT plán mimo limitov), VDT audit
NESMIE zamietať KAŽDÝ obchod (to blokovalo WV_3/4). No-worsen: obchod prejde, ak NEZVÝŠI
celkový SOC excess; nákup pri 100 % / predaj pri 0 % ho zvýšia → padnú (fyzika drží).
Kill-switch VDT_AUDIT_NOWORSEN=0 → späť na absolútny.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core.soc_use_audit as sua


def _state(start_pct, dam):
    return {"ok": True, "batt_kwh": 1000.0, "eff_c": 0.95, "eff_d": 0.95,
            "soc_min_pct": 5.0, "soc_max_pct": 100.0,
            "current_soc_pct": start_pct, "start_soc_pct": start_pct,
            "dam_nomination_kwh": dam, "vdt_realized_kwh": [0.0] * 96}


def _dam(**slots):
    d = [0.0] * 96
    for k, v in slots.items():
        d[int(k[1:])] = v      # k="s10" → slot 10
    return d


def test_noworsen_charge_into_full_still_rejected():
    """Nákup pri ~plnej batérii ZVÝŠI excess → musí padnúť aj v no-worsen (fyzika)."""
    os.environ["VDT_AUDIT_NOWORSEN"] = "1"
    st = _state(98.0, _dam(s0=-300.0))          # start 98% + DAM nabíja → nad 100
    r = sua.audit_action("t", "2026-07-01", 10, "charge", 300.0, source="vdt", today_state=st)
    assert r["decision"] != "accept", f"nákup do plnej mal padnúť: {r['decision']}"


def test_noworsen_helpful_trade_passes_when_baseline_broken():
    """Baseline infeasibilný (DAM vybíja pod dno neskôr); skorší nákup NEZHORŠÍ (pomáha) →
    no-worsen ACCEPT, absolute REJECT."""
    st = _state(20.0, _dam(s10=300.0, s11=300.0, s12=300.0, s14=300.0))  # pod dno v neskorších
    os.environ["VDT_AUDIT_NOWORSEN"] = "1"
    r_nw = sua.audit_action("t", "2026-07-01", 5, "charge", 150.0, source="vdt", today_state=st)
    os.environ["VDT_AUDIT_NOWORSEN"] = "0"
    r_abs = sua.audit_action("t", "2026-07-01", 5, "charge", 150.0, source="vdt", today_state=st)
    os.environ["VDT_AUDIT_NOWORSEN"] = "1"
    assert r_nw["decision"] == "accept", f"no-worsen mal prijať pomáhajúci obchod: {r_nw['decision']}"
    assert r_abs["decision"] != "accept", f"absolute mal odmietnuť (kontrola): {r_abs['decision']}"


def test_noworsen_feasible_on_clean_baseline_accepted():
    """Zdravý baseline + feasibilný obchod → accept (žiadna regresia)."""
    os.environ["VDT_AUDIT_NOWORSEN"] = "1"
    st = _state(50.0, _dam())
    r = sua.audit_action("t", "2026-07-01", 10, "charge", 100.0, source="vdt", today_state=st)
    assert r["decision"] == "accept", f"feasibilný na čistom baseline mal prejsť: {r}"


if __name__ == "__main__":
    test_noworsen_charge_into_full_still_rejected()
    test_noworsen_helpful_trade_passes_when_baseline_broken()
    test_noworsen_feasible_on_clean_baseline_accepted()
    print("VDT-AUDIT-NOWORSEN OK")
