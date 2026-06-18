# -*- coding: utf-8 -*-
"""tests/test_vpp.py — golden sieť pre VPP backbone (kontrakty + agregácia/split).

Zamyká správanie core/schemas/vpp.py + aggregation/split.py — chráni proti
regresii pri ďalšej VPP prestavbe (project-modularizacia-skalovanie).
"""
import math
import pytest

from core.schemas.vpp import AvailabilityReport, BlockAggregate, Order, Allocation, ControlTick
from aggregation import aggregate_block, split_order


# ── kontrakty: validačné pravidlá ────────────────────────────────────────────
def test_availability_valid():
    a = AvailabilityReport(battery_id="b1", day="2026-06-18", slot_idx=80, soc_pct=37.0,
                           free_charge_kw=500, free_discharge_kw=300, free_kwh=125, eff=0.95)
    assert a.battery_id == "b1" and a.slot_idx == 80


def test_availability_rejects_bad_slot_and_day():
    with pytest.raises(Exception):
        AvailabilityReport(battery_id="b", day="2026-06-18", slot_idx=200, soc_pct=50,
                           free_charge_kw=0, free_discharge_kw=0, free_kwh=0)
    with pytest.raises(Exception):
        AvailabilityReport(battery_id="b", day="18.6.2026", slot_idx=1, soc_pct=50,
                           free_charge_kw=0, free_discharge_kw=0, free_kwh=0)


def test_order_rejects_zero_price_accepts_negative():
    # cena presne 0 = placeholder/neplatná (len reálne BID/ASK)
    with pytest.raises(Exception):
        Order(order_id="o", account_id="a", block_id="b", country="sk", day="2026-06-18",
              slot_idx=1, side="buy", volume_kwh=10, price_eur_mwh=0.0, source="vdt")
    # záporná cena je platná (prebytok na trhu)
    o = Order(order_id="o", account_id="a", block_id="b", country="sk", day="2026-06-18",
              slot_idx=1, side="buy", volume_kwh=10, price_eur_mwh=-12.5, source="vdt")
    assert o.price_eur_mwh == -12.5


def test_controltick_defaults():
    ct = ControlTick(battery_id="b1", ts="2026-06-18T20:31:00", setpoint_kw=-188.0)
    assert ct.source == "rt" and ct.ttl_sec > 0


# ── agregácia + split ────────────────────────────────────────────────────────
def _reports_sell():
    return [
        AvailabilityReport(battery_id="b1", day="2026-06-18", slot_idx=80, soc_pct=80,
                           free_charge_kw=0, free_discharge_kw=3000, free_kwh=750),
        AvailabilityReport(battery_id="b2", day="2026-06-18", slot_idx=80, soc_pct=60,
                           free_charge_kw=0, free_discharge_kw=1000, free_kwh=250),
        AvailabilityReport(battery_id="b3", day="2026-06-18", slot_idx=80, soc_pct=40,
                           free_charge_kw=0, free_discharge_kw=500, free_kwh=125),
    ]


def test_aggregate_block():
    agg = aggregate_block("blk1", _reports_sell())
    assert agg.n_batteries == 3
    assert abs(agg.agg_free_discharge_kw - 4500) < 1e-6


def _order_sell(vol):
    return Order(order_id="o", account_id="a", block_id="blk1", country="sk",
                 day="2026-06-18", slot_idx=80, side="sell", volume_kwh=vol,
                 price_eur_mwh=180, source="vdt")


def test_split_sum_exact_and_sign():
    al = split_order(_order_sell(600), _reports_sell(), "free_capacity")
    total = sum(a.share_kwh for a in al)
    assert abs(total - 600.0) < 1e-3          # largest-remainder → presná suma
    assert all(a.share_kwh > 0 for a in al)   # SELL → vybíja (+)
    assert all(a.source == "vdt" for a in al)


def test_split_respects_capacity_and_shortfall():
    # objem > Σ kapacita (1125 kWh = 0.25h × (3000+1000+500))
    al = split_order(_order_sell(2000), _reports_sell(), "free_capacity")
    total = sum(a.share_kwh for a in al)
    assert abs(total - 1125.0) < 1e-3          # naplnené na kapacitu, zvyšok shortfall
    by_id = {a.battery_id: a.share_kwh for a in al}
    assert by_id["b1"] <= 750.0 + 1e-3
    assert by_id["b2"] <= 250.0 + 1e-3
    assert by_id["b3"] <= 125.0 + 1e-3


def test_split_buy_is_negative():
    rb = [AvailabilityReport(battery_id="b1", day="2026-06-18", slot_idx=10, soc_pct=20,
                             free_charge_kw=2000, free_discharge_kw=0, free_kwh=500)]
    ob = Order(order_id="o", account_id="a", block_id="blk1", country="sk",
               day="2026-06-18", slot_idx=10, side="buy", volume_kwh=300,
               price_eur_mwh=60, source="vdt")
    al = split_order(ob, rb, "free_capacity")
    assert al and al[0].share_kwh < 0          # BUY → nabíja (−)
    assert abs(al[0].share_kwh + 300.0) < 1e-3


def test_split_no_capacity_returns_empty():
    # SELL ale batérie nemajú voľné vybíjanie
    r = [AvailabilityReport(battery_id="b1", day="2026-06-18", slot_idx=5, soc_pct=5,
                            free_charge_kw=1000, free_discharge_kw=0, free_kwh=0)]
    assert split_order(_order_sell(100), r, "free_capacity") == []
