# -*- coding: utf-8 -*-
"""Testy VDT párového matchera (vdt_pair_matcher.match_pairs)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from vdt_pair_matcher import match_pairs


_COMMON = dict(
    soc0_kwh=0.0, soc_lo_kwh=0.0, soc_hi_kwh=1000.0,
    batt_kwh_per_slot=1000.0, eff_c=1.0, eff_d=1.0,
    cycle_cost=0.0, grid_fee=0.0, min_spread=5.0,
)


def test_basic_arbitrage_buy_low_sell_high():
    # slot0 lacno (10), slot1 draho (100) → 1 cyklus buy@0 sell@1
    r = match_pairs([10.0, 100.0], **_COMMON)
    assert len(r["cycles"]) == 1
    cy = r["cycles"][0]
    assert cy["charge_slot"] == 0 and cy["discharge_slot"] == 1
    assert cy["direction"] == "buy_then_sell"
    assert r["profit_eur"] > 0
    # nabíja v 0 (+), vybíja v 1 (−)
    assert r["vdt_soc_delta"][0] > 0 and r["vdt_soc_delta"][1] < 0


def test_spread_enforced_no_trade_below_threshold():
    # rozdiel 3 €/MWh < min_spread 5 → žiadny obchod
    r = match_pairs([100.0, 103.0], **_COMMON)
    assert r["cycles"] == []
    assert r["profit_eur"] == 0.0


def test_evening_high_buy_has_no_pair_rejected():
    # VW vzor: ráno lacno, večer drahý PEAK na konci dňa bez vyššieho neskoršieho predaja.
    # Ceny: [10, 20, 250, 240, 230]  → peak 250 na slote 2, potom klesá.
    # Nesmie vzniknúť NÁKUP za 230/240 (slot 3/4) — niet kam predať drahšie (+spread).
    # Legitímne: buy@0(10) alebo @1(20) → sell@2(250) atď.
    r = match_pairs([10.0, 20.0, 250.0, 240.0, 230.0], **_COMMON)
    buys = [c["charge_slot"] for c in r["cycles"]]
    # žiadny nákup nesmie byť vo večernom peaku (sloty 2,3,4 — vysoké ceny)
    assert all(b in (0, 1) for b in buys), f"nákup vo vysokej cene: {r['cycles']}"
    # každý pár musí mať kladnú maržu ≥ spread
    assert all(c["margin_eur_mwh"] >= 5.0 for c in r["cycles"])


def test_both_directions_sell_then_buyback():
    # Začni s plnou SOC. Ceny: [200, 10, 50] → predaj draho@0 z SOC, spätný nákup lacno@1.
    r = match_pairs([200.0, 10.0, 50.0],
                    soc0_kwh=1000.0, soc_lo_kwh=0.0, soc_hi_kwh=1000.0,
                    batt_kwh_per_slot=1000.0, eff_c=1.0, eff_d=1.0,
                    cycle_cost=0.0, grid_fee=0.0, min_spread=5.0)
    assert any(c["direction"] == "sell_then_buyback" for c in r["cycles"]), r["cycles"]
    assert r["profit_eur"] > 0


def test_soc_bounds_respected():
    r = match_pairs([10.0, 100.0, 10.0, 100.0],
                    soc0_kwh=0.0, soc_lo_kwh=0.0, soc_hi_kwh=500.0,
                    batt_kwh_per_slot=1000.0, eff_c=1.0, eff_d=1.0,
                    cycle_cost=0.0, grid_fee=0.0, min_spread=5.0)
    soc = np.asarray(r["soc_kwh"])
    assert soc.min() >= -1e-6 and soc.max() <= 500.0 + 1e-6, soc


def test_power_limit_per_slot():
    # batt_kwh_per_slot=100 → jeden cyklus max 100 kWh
    r = match_pairs([10.0, 100.0],
                    soc0_kwh=0.0, soc_lo_kwh=0.0, soc_hi_kwh=1000.0,
                    batt_kwh_per_slot=100.0, eff_c=1.0, eff_d=1.0,
                    cycle_cost=0.0, grid_fee=0.0, min_spread=5.0)
    assert abs(r["vdt_soc_delta"][0] - 100.0) < 1e-6
    assert abs(r["vdt_soc_delta"][1] + 100.0) < 1e-6


def test_efficiency_and_costs_in_margin():
    # eff 0.9/0.9, grid_fee 10, cycle_cost 5. buy@10 sell@100. _thru = 1/0.9 + 0.9 = 2.0111
    # poplatok LEN na nabíjaní: grid_fee/eff_c = 10/0.9 = 11.111
    # m = 0.9*100 − 10/0.9 − 10/0.9 − 5*2.0111/2
    #   = 90 − 11.111 − 11.111 − 5.028 = 62.75
    r = match_pairs([10.0, 100.0],
                    soc0_kwh=0.0, soc_lo_kwh=0.0, soc_hi_kwh=1000.0,
                    batt_kwh_per_slot=1000.0, eff_c=0.9, eff_d=0.9,
                    cycle_cost=5.0, grid_fee=10.0, min_spread=5.0)
    assert len(r["cycles"]) == 1
    assert abs(r["cycles"][0]["margin_eur_mwh"] - 62.75) < 0.05


def test_priority_profit_vs_closest():
    # Ceny: [10, 60, 10, 200]. closest by spáril 0-1 (m=50, dist1). profit by 0-3 alebo 2-3 (m=190).
    r_close = match_pairs([10.0, 60.0, 10.0, 200.0], priority="closest", **_COMMON)
    r_prof = match_pairs([10.0, 60.0, 10.0, 200.0], priority="profit", **_COMMON)
    # profit-prvý cyklus má vyššiu maržu než closest-prvý
    assert r_prof["cycles"][0]["margin_eur_mwh"] >= r_close["cycles"][0]["margin_eur_mwh"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
