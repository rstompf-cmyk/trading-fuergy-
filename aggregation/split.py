# -*- coding: utf-8 -*-
"""aggregation/split.py — agregácia bloku + rozdelenie obchodu na batérie.

VPP agregačná vrstva (project-modularizacia-skalovanie):
  • aggregate_block(reports)  → BlockAggregate (Σ voľných dostupností bloku)
  • split_order(order, reports, strategy) → [Allocation] (objem bloku → per-batéria)

Konvencia znamienka (zhodná s vpp kontraktmi): + = vybíja (predaj), − = nabíja (nákup).
Pre SELL order rozdeľujeme medzi voľné VYBÍJANIE batérií, pre BUY medzi voľné NABÍJANIE.

Stratégie delenia (block.split_strategy):
  • free_capacity  — pro-rata podľa voľnej kapacity v danom smere (default)
  • soc_headroom   — pro-rata podľa SOC headroomu (predaj: SOC nad podlahou;
                     nákup: priestor pod stropom)
  • eff            — váženo účinnosťou (preferuj účinnejšie batérie)

Suma alokácií = min(objem, Σ kapacita). Ak objem > Σ kapacita → každá dostane svoj
strop a zvyšok je SHORTFALL (caller vidí sum(share) < objem). Largest-remainder
zaokrúhľovanie → suma presne sedí (žiadny drift), každý podiel ≤ jeho kapacita.

Čisté funkcie — žiadny I/O, žiadny živý kód. Plne testovateľné.
"""
from __future__ import annotations
from typing import List
import datetime as dt

from core.schemas.vpp import AvailabilityReport, BlockAggregate, Order, Allocation


def aggregate_block(block_id: str, reports: List[AvailabilityReport],
                    ts: str = "") -> BlockAggregate:
    """Σ dostupností batérií bloku pre JEDEN slot. Reporty musia byť rovnaký
    deň + slot (caller filtruje). n_batteries = počet hlásiacich batérií."""
    if not reports:
        raise ValueError("aggregate_block: prázdny zoznam reportov")
    day = reports[0].day
    slot = reports[0].slot_idx
    return BlockAggregate(
        block_id=block_id,
        day=day,
        slot_idx=slot,
        agg_free_charge_kw=sum(r.free_charge_kw for r in reports),
        agg_free_discharge_kw=sum(r.free_discharge_kw for r in reports),
        agg_free_kwh=sum(r.free_kwh for r in reports),
        n_batteries=len(reports),
        ts=ts or dt.datetime.now().isoformat(timespec="seconds"),
    )


def _weight(report: AvailabilityReport, side: str, strategy: str) -> float:
    """Váha batérie pre delenie (nezáporná). side: 'sell'|'buy'."""
    # Kapacita v smere obchodu (kW) — tvrdý strop pre danú batériu.
    cap = report.free_discharge_kw if side == "sell" else report.free_charge_kw
    if cap <= 0:
        return 0.0
    if strategy == "soc_headroom":
        # predaj: koľko SOC je nad podlahou (~soc_pct); nákup: priestor pod stropom (~100−soc).
        head = report.soc_pct if side == "sell" else (100.0 - report.soc_pct)
        return max(0.0, head) * cap
    if strategy == "eff":
        return cap * max(0.1, report.eff)
    return cap   # free_capacity (default)


def split_order(order: Order, reports: List[AvailabilityReport],
                strategy: str = "free_capacity", dt_h: float = 0.25) -> List[Allocation]:
    """Rozdelí objem obchodu bloku na jednotlivé batérie podľa stratégie.

    Vracia [Allocation] (len batérie s nenulovým podielom). Σ share_kwh = min(objem,
    Σ kapacita) so znamienkom: +vybíja (sell) / −nabíja (buy). Largest-remainder
    zaokrúhľovanie na 0.001 kWh."""
    side = order.side
    sign = +1.0 if side == "sell" else -1.0
    # kapacita batérie (kWh v slote) v smere obchodu
    caps = []
    for r in reports:
        cap_kw = r.free_discharge_kw if side == "sell" else r.free_charge_kw
        caps.append(max(0.0, cap_kw) * dt_h)
    weights = [_weight(r, side, strategy) for r in reports]
    total_w = sum(weights)
    if total_w <= 0:
        return []   # blok nemá voľnú kapacitu v tomto smere
    total_cap = sum(caps)
    fill = min(float(order.volume_kwh), total_cap)   # shortfall ak objem > kapacita

    # pro-rata podľa váh, ALE strop = kapacita batérie
    raw = []
    for w, cap in zip(weights, caps):
        raw.append(min(cap, fill * (w / total_w)))
    # ak strop niektoré orezal, ostane zvyšok — dorozdeľ proporčne medzi nenasýtené
    assigned = sum(raw)
    remainder = fill - assigned
    for _ in range(4):  # pár iterácií dorovnania
        if remainder <= 1e-6:
            break
        head_w = sum(weights[i] for i in range(len(raw)) if raw[i] < caps[i] - 1e-9)
        if head_w <= 0:
            break
        for i in range(len(raw)):
            if raw[i] < caps[i] - 1e-9:
                add = min(caps[i] - raw[i], remainder * (weights[i] / head_w))
                raw[i] += add
        new_assigned = sum(raw)
        remainder = fill - new_assigned
        if abs(new_assigned - assigned) < 1e-9:
            break
        assigned = new_assigned

    # largest-remainder zaokrúhlenie na 0.001 kWh tak aby suma sedela na `fill`
    shares = _largest_remainder([min(c, x) for x, c in zip(raw, caps)], fill, q=0.001)

    out: List[Allocation] = []
    for r, s in zip(reports, shares):
        if abs(s) < 1e-6:
            continue
        out.append(Allocation(
            battery_id=r.battery_id,
            block_id=order.block_id,
            order_id=order.order_id,
            day=order.day,
            slot_idx=order.slot_idx,
            share_kwh=round(sign * s, 3),
            setpoint_kw=round(sign * s / max(dt_h, 1e-9), 3),
            source=order.source,
            ts=dt.datetime.now().isoformat(timespec="seconds"),
        ))
    return out


def _largest_remainder(values: List[float], target: float, q: float = 0.001) -> List[float]:
    """Zaokrúhli `values` na násobky `q` tak, aby ich súčet bol presne round(target,q)
    (largest-remainder metóda — žiadny drift). values nezáporné."""
    if not values:
        return []
    units_target = int(round(target / q))
    floors = [int(v / q) for v in values]            # zaokrúhli nadol na jednotky q
    used = sum(floors)
    left = units_target - used
    # zvyšky pre prideľovanie zvyšných jednotiek
    rema = sorted(range(len(values)), key=lambda i: (values[i] / q - floors[i]), reverse=True)
    i = 0
    while left > 0 and rema:
        idx = rema[i % len(rema)]
        floors[idx] += 1
        left -= 1
        i += 1
    return [f * q for f in floors]
