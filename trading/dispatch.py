# -*- coding: utf-8 -*-
"""trading/dispatch.py — most Order → split → control loop.

Spája trading kontrakty s riadením: pre Order bloku načíta dostupnosti batérií
(z fleet DB + posledný SOC zo statusu), agreguje, rozdelí (aggregation.split) a
výsledné Allocation premietne na setpoint PRÍKAZY (fleet.enqueue_command) — to je
existujúce IPC, ktoré control loop konzumuje. Žiadna ekonomika: Order prichádza
zvonku (VDT/RT vrstva ho vyrobí v samostatnej session).

block_id v kontraktoch je string; fleet používa int → konvertujeme.
"""
from __future__ import annotations
from typing import List, Optional

from core.schemas.vpp import Order, AvailabilityReport, BlockAggregate
from aggregation.split import aggregate_block, split_order
from .availability import battery_availability


def build_block_reports(block_id: int, day: str, slot_idx: int, *,
                        dt_h: float = 0.25, soc_default: float = 50.0) -> List[AvailabilityReport]:
    """AvailabilityReport pre všetky AKTÍVNE priradené batérie bloku (SOC zo statusu)."""
    import fleet
    reports: List[AvailabilityReport] = []
    for b in fleet.batteries_in_block(block_id):
        st = fleet.get_status(b["id"])
        soc = (st or {}).get("soc_pct")
        reports.append(battery_availability(
            b, soc if soc is not None else soc_default, day, slot_idx, dt_h=dt_h))
    return reports


def block_aggregate(block_id: int, day: str, slot_idx: int,
                    dt_h: float = 0.25) -> Optional[BlockAggregate]:
    """BlockAggregate (Σ dostupností) — obchodovateľný objem bloku v slote."""
    reports = build_block_reports(block_id, day, slot_idx, dt_h=dt_h)
    if not reports:
        return None
    return aggregate_block(str(block_id), reports)


def dispatch_order(order: Order, *, dt_h: float = 0.25, strategy: Optional[str] = None,
                   enqueue: bool = True, persist: bool = False) -> List:
    """Rozdelí Order bloku na batérie a (voliteľne) zapíše setpoint príkazy / DB.

    Vráti [Allocation].
      • enqueue=True  → každá alokácia → fleet.enqueue_command(setpoint) (control vykoná).
      • persist=True  → Order + Allocation sa zapíšu do DB (trading audit/IPC ledger).
    enqueue=False+persist=False = len výpočet (dry / náhľad)."""
    import fleet
    bid = int(order.block_id)
    reports = build_block_reports(bid, order.day, order.slot_idx, dt_h=dt_h)
    if not reports:
        return []
    blk = fleet.get_block(bid)
    strat = strategy or (blk or {}).get("split_strategy") or "free_capacity"
    allocs = split_order(order, reports, strategy=strat, dt_h=dt_h)
    if persist:
        from . import repository as repo
        repo.save_order(order)
        if allocs:
            repo.save_allocations(allocs)
    if enqueue:
        for a in allocs:
            fleet.enqueue_command(int(a.battery_id), "setpoint", {
                "kw": a.setpoint_kw, "source": a.source,
                "order_id": a.order_id, "slot_idx": a.slot_idx,
            })
    return allocs
