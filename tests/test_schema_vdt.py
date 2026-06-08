# -*- coding: utf-8 -*-
"""tests/test_schema_vdt.py — Smoke test pre Fázu A.3 (VDTTrade + gate).

Testuje:
  • VDTTrade.model_validate na všetkých riadkoch z vdt_paper_trades.csv
  • Round-trip dump↔load
  • Field validators (profile not empty, slot format, action enum, finite floats, SOC range)
  • should_log_vdt_for_profile() gate semantika (Bug UU)
  • validate_paper_trade_row() vracia None pri invalid (žiadny crash)
  • Helper funkcie: is_significant / is_buy / is_sell
"""
import sys
import os
import csv
import glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.schemas.vdt import (
    VDTTrade, ALLOWED_ACTIONS,
    should_log_vdt_for_profile, validate_paper_trade_row,
)


def _paper_trades_csv() -> str:
    """Cesta k existujúcemu CSV (môže byť v CZ alebo SK)."""
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "out", "sk", "vdt_paper_trades.csv"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "out", "cz", "vdt_paper_trades.csv"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return ""


def test_validate_real_csv_rows():
    """Reálne riadky v vdt_paper_trades.csv — majority sa musí validovať."""
    path = _paper_trades_csv()
    if not path:
        print("  (skip — žiadne CSV)")
        return
    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return
    ok = 0; bad_corrupted = 0; bad_other = 0
    for r in rows:
        # Filter corrupted (CSV multiline cells z minulosti)
        if not r.get("action") or r.get("action") in ("0.000", None):
            bad_corrupted += 1
            continue
        try:
            VDTTrade.model_validate(r)
            ok += 1
        except Exception as e:
            bad_other += 1
    # Pomer zdravých riadkov musí byť > 90%
    healthy = ok / (ok + bad_other) if (ok + bad_other) > 0 else 1.0
    assert healthy >= 0.9, f"Iba {healthy:.1%} riadkov healthy (ok={ok}, bad={bad_other})"


def test_validator_empty_profile():
    from pydantic import ValidationError
    try:
        VDTTrade.model_validate({
            "ts": "2026-06-08T12:00:00", "profile": "",
            "slot": "12:00", "action": "charge",
        })
        assert False, "empty profile by mal raise"
    except ValidationError:
        pass


def test_validator_slot_format():
    from pydantic import ValidationError
    # OK: HH:MM
    VDTTrade.model_validate({
        "ts": "2026-06-08T12:00:00", "profile": "Test",
        "slot": "12:00", "action": "charge",
    })
    # OK: HH:MM-HH:MM
    VDTTrade.model_validate({
        "ts": "2026-06-08T12:00:00", "profile": "Test",
        "slot": "12:00-12:15", "action": "charge",
    })
    # FAIL: 25:00
    for bad in ("25:00", "12:60", "12-00", "noon", ""):
        try:
            VDTTrade.model_validate({
                "ts": "2026-06-08T12:00:00", "profile": "Test",
                "slot": bad, "action": "charge",
            })
            assert False, f"slot='{bad}' by mal raise"
        except ValidationError:
            pass


def test_validator_action_enum():
    from pydantic import ValidationError
    for a in ALLOWED_ACTIONS:
        VDTTrade.model_validate({
            "ts": "2026-06-08T12:00:00", "profile": "Test",
            "slot": "12:00", "action": a,
        })
    try:
        VDTTrade.model_validate({
            "ts": "2026-06-08T12:00:00", "profile": "Test",
            "slot": "12:00", "action": "unknown_xyz",
        })
        assert False, "unknown action by mal raise"
    except ValidationError:
        pass


def test_validator_finite_floats():
    from pydantic import ValidationError
    for k, bad in [("kw", float("inf")), ("kwh", float("nan")),
                    ("price_predicted_eur", float("-inf"))]:
        try:
            VDTTrade.model_validate({
                "ts": "2026-06-08T12:00:00", "profile": "Test",
                "slot": "12:00", "action": "charge", k: bad,
            })
            assert False, f"{k}={bad} by mal raise"
        except ValidationError:
            pass


def test_validator_soc_range():
    from pydantic import ValidationError
    # OK: 0, 50, 100
    for v in (0.0, 50.0, 100.0):
        VDTTrade.model_validate({
            "ts": "2026-06-08T12:00:00", "profile": "Test",
            "slot": "12:00", "action": "charge", "soc_before_pct": v,
        })
    # OK: tolerancia ±5%
    VDTTrade.model_validate({
        "ts": "2026-06-08T12:00:00", "profile": "Test",
        "slot": "12:00", "action": "charge", "soc_before_pct": 103.0,
    })
    # FAIL: 150
    try:
        VDTTrade.model_validate({
            "ts": "2026-06-08T12:00:00", "profile": "Test",
            "slot": "12:00", "action": "charge", "soc_before_pct": 150.0,
        })
        assert False, "SOC=150 by mal raise"
    except ValidationError:
        pass


def test_round_trip():
    """model_dump → model_validate musí dať identický výsledok."""
    src = {
        "ts": "2026-06-08T12:30:16", "profile": "Simulacia_Coop",
        "slot": "12:30-12:45", "action": "charge", "kw": 200.0, "kwh": 50.0,
        "price_predicted_eur": 100.0, "soc_before_pct": 61.88,
        "soc_after_pct": 70.0, "soc_source": "realio_db",
        "profit_eur_rest_of_day": 5.5,
    }
    t = VDTTrade.model_validate(src)
    dump = t.model_dump()
    t2 = VDTTrade.model_validate(dump)
    assert t.profile == t2.profile
    assert t.kwh == t2.kwh
    assert t.action == t2.action


def test_helpers():
    """is_significant / is_buy / is_sell."""
    idle = VDTTrade.model_validate({
        "ts": "x", "profile": "T", "slot": "12:00",
        "action": "idle", "kwh": 0.0,
    })
    assert idle.is_significant() is False
    assert idle.is_buy() is False
    assert idle.is_sell() is False

    charge = VDTTrade.model_validate({
        "ts": "x", "profile": "T", "slot": "12:00",
        "action": "charge", "kwh": 50.0,
    })
    assert charge.is_significant() is True
    assert charge.is_buy() is True
    assert charge.is_sell() is False

    discharge = VDTTrade.model_validate({
        "ts": "x", "profile": "T", "slot": "12:00",
        "action": "discharge", "kwh": -50.0,
    })
    assert discharge.is_significant() is True
    assert discharge.is_buy() is False
    assert discharge.is_sell() is True


def test_gate_for_real_profiles():
    """should_log_vdt_for_profile pre reálne profily.

    Profily s explicit use_vdt:false → False
    Legacy profily (žiadny joint_lp) → True (default)
    """
    import profiles as pr
    names = pr.list_profiles() or []
    if not names:
        print("  (skip — žiadne profily)")
        return
    for n in names:
        p = pr.load_profile(n) or {}
        jlp = (p.get("plan") or {}).get("joint_lp") or {}
        use_vdt = jlp.get("use_vdt")
        gate = should_log_vdt_for_profile(n)
        if use_vdt is False:
            assert gate is False, f"{n}: use_vdt=False ale gate=True"
        elif use_vdt is True:
            assert gate is True, f"{n}: use_vdt=True ale gate=False"
        else:
            # Legacy bez joint_lp → default True
            assert gate is True, f"{n}: legacy ale gate=False"


def test_gate_unknown_profile():
    """Neznámy profil → True (back-compat, safer to allow than block)."""
    assert should_log_vdt_for_profile("NonExistent_Profile_XYZ") is True


def test_validate_paper_trade_row_invalid_returns_none():
    """validate_paper_trade_row pre invalid dict vracia None (žiadny crash)."""
    # Invalid: empty profile
    res = validate_paper_trade_row({
        "ts": "x", "profile": "", "slot": "12:00", "action": "charge",
    })
    assert res is None
    # Invalid: bad action
    res = validate_paper_trade_row({
        "ts": "x", "profile": "T", "slot": "12:00", "action": "fly_to_moon",
    })
    assert res is None
    # Valid
    res = validate_paper_trade_row({
        "ts": "x", "profile": "T", "slot": "12:00", "action": "charge",
    })
    assert res is not None
    assert isinstance(res, VDTTrade)


def test_extra_fields_allowed():
    """Forward-compat: neznáme polia musia byť OK."""
    t = VDTTrade.model_validate({
        "ts": "x", "profile": "T", "slot": "12:00", "action": "charge",
        "future_market": "DEX", "future_score": 99.9,
    })
    dump = t.model_dump()
    assert "future_market" in dump
    assert dump["future_market"] == "DEX"


if __name__ == "__main__":
    print("━━━ Fáza A.3 smoke test ━━━\n")
    tests = [t for t in dir() if t.startswith("test_")]
    passed = failed = 0
    for tname in sorted(tests):
        try:
            globals()[tname]()
            print(f"  ✓ {tname}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {tname}: {e}")
            failed += 1
    print(f"\n━━━ {passed} OK, {failed} FAIL ━━━")
    sys.exit(0 if failed == 0 else 1)
