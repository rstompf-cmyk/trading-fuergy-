# -*- coding: utf-8 -*-
"""test_vdt_cleanup.py — VDT Upratovanie (bezpečnostná sieť) jadro (2026-07-02)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.vdt_cleanup import required_margin_eur, decide_cleanup, detect_undeliverable_target


def test_margin_decay():
    # ďaleko (τ=horizont) → +min_spread ; blízko (τ=0) → −max_loss ; stred → priemer
    assert abs(required_margin_eur(6, 6, 5, 20) - 5.0) < 1e-6
    assert abs(required_margin_eur(0, 6, 5, 20) - (-20.0)) < 1e-6
    assert abs(required_margin_eur(3, 6, 5, 20) - (-7.5)) < 1e-6      # stred: (5 + -20)/2
    assert abs(required_margin_eur(12, 6, 5, 20) - 5.0) < 1e-6        # nad horizont = clip


def test_far_profitable_acts():
    # ďaleko (6h), predaj za 150 vs ref 130 → marža +20 ≥ req +5 → zasiahni
    r = decide_cleanup(1000, 6, direction="sell", action_price_eur=150, ref_price_eur=130,
                       horizon_h=6, min_spread_eur=5, max_loss_eur=20, deadband_kw=50, max_action_kw=2000)
    assert r["act"] and r["kw"] == 1000


def test_far_unprofitable_waits():
    # ďaleko (6h), predaj za 132 vs ref 130 → marža +2 < req +5 → počkaj
    r = decide_cleanup(1000, 6, direction="sell", action_price_eur=132, ref_price_eur=130,
                       horizon_h=6, min_spread_eur=5, max_loss_eur=20, deadband_kw=50, max_action_kw=2000)
    assert not r["act"]


def test_near_accepts_small_loss():
    # blízko (0.5h), predaj za 120 vs ref 130 → marža −10; req pri τ=0.5 ≈ −17.9 → −10 ≥ −17.9 → zasiahni
    r = decide_cleanup(1000, 0.5, direction="sell", action_price_eur=120, ref_price_eur=130,
                       horizon_h=6, min_spread_eur=5, max_loss_eur=20, deadband_kw=50, max_action_kw=2000)
    assert r["act"], r


def test_near_rejects_big_loss():
    # blízko (0h), predaj za 105 vs ref 130 → marža −25 < req −20 (strop straty) → nezasahuj
    r = decide_cleanup(1000, 0.0, direction="sell", action_price_eur=105, ref_price_eur=130,
                       horizon_h=6, min_spread_eur=5, max_loss_eur=20, deadband_kw=50, max_action_kw=2000)
    assert not r["act"], r


def test_deadband_and_cap_and_noref():
    # pod deadband → nič
    assert not decide_cleanup(10, 6, direction="sell", action_price_eur=150, ref_price_eur=130,
                              horizon_h=6, min_spread_eur=5, max_loss_eur=20, deadband_kw=50, max_action_kw=2000)["act"]
    # cap: deviation 5000, max_action 2000 → kw=2000
    r = decide_cleanup(5000, 6, direction="sell", action_price_eur=150, ref_price_eur=130,
                       horizon_h=6, min_spread_eur=5, max_loss_eur=20, deadband_kw=50, max_action_kw=2000)
    assert r["act"] and r["kw"] == 2000
    # bez referencie → nezasahuj
    assert not decide_cleanup(1000, 6, direction="sell", action_price_eur=None, ref_price_eur=130,
                              horizon_h=6, min_spread_eur=5, max_loss_eur=20, deadband_kw=50, max_action_kw=2000)["act"]


def test_buy_direction():
    # nákup chce cenu POD referenciou: buy za 110 vs ref 130 → marža +20 ≥ req +5 → zasiahni
    r = decide_cleanup(1000, 6, direction="buy", action_price_eur=110, ref_price_eur=130,
                       horizon_h=6, min_spread_eur=5, max_loss_eur=20, deadband_kw=50, max_action_kw=2000)
    assert r["act"] and r["kw"] == 1000


def test_detect_over_charge():
    # SOC path prekroci 100 v slote 40 (index) → over_charge → sell teraz
    path = [50.0] * 97
    path[41] = 108.0        # koniec slotu 40 = 108 % → nad max
    t = detect_undeliverable_target(path, 30, soc_min_pct=5, soc_max_pct=100,
                                    batt_kwh=6000, batt_kw=6000)
    assert t and t["problem_slot"] == 40 and t["direction"] == "sell"
    assert abs(t["tau_h"] - (40 - 30) * 0.25) < 1e-6


def test_detect_over_discharge():
    path = [50.0] * 97
    path[21] = 2.0          # koniec slotu 20 = 2 % → pod min
    t = detect_undeliverable_target(path, 10, soc_min_pct=5, soc_max_pct=100,
                                    batt_kwh=6000, batt_kw=6000)
    assert t and t["problem_slot"] == 20 and t["direction"] == "buy"


def test_detect_none_when_feasible():
    path = [50.0] * 97
    assert detect_undeliverable_target(path, 0, soc_min_pct=5, soc_max_pct=100,
                                       batt_kwh=6000, batt_kw=6000) is None


if __name__ == "__main__":
    for _n, _f in list(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _f(); print(f"OK {_n}")
    print("VDT-CLEANUP OK")
