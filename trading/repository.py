# -*- coding: utf-8 -*-
"""trading/repository.py — perzistencia VPP trading kontraktov (DB = IPC + audit).

Trading proces zapíše Order + Allocation; control/monitor ich číta. Vracia plain
dicty (oddelené od ORM). Konvencia setpointu: +vybíja / −nabíja.
"""
from __future__ import annotations
from typing import Optional, List, Dict
import datetime as dt

from db import get_session
from db import models as m


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


# ── Order ──────────────────────────────────────────────────────────────────
def save_order(order) -> int:
    """UPSERT Order (pydantic alebo dict) podľa order_id. Vráti id."""
    o = order.model_dump() if hasattr(order, "model_dump") else dict(order)
    now = _now()
    with get_session() as s:
        row = s.query(m.TradeOrder).filter_by(order_id=o["order_id"]).one_or_none()
        if row is None:
            row = m.TradeOrder(order_id=o["order_id"], created_at=now)
            s.add(row)
        row.account_id = o["account_id"]
        row.block_id = str(o["block_id"])
        row.country = o["country"]
        row.day = str(o["day"])[:10]
        row.slot_idx = int(o["slot_idx"])
        row.side = o["side"]
        row.volume_kwh = float(o["volume_kwh"])
        row.price_eur_mwh = float(o["price_eur_mwh"])
        row.source = o["source"]
        row.status = o.get("status", "planned")
        row.submitted_at = o.get("submitted_at")
        s.flush()
        return row.id


def list_orders(day: Optional[str] = None, block_id: Optional[str] = None,
                status: Optional[str] = None) -> List[Dict]:
    with get_session() as s:
        q = s.query(m.TradeOrder)
        if day:
            q = q.filter(m.TradeOrder.day == str(day)[:10])
        if block_id is not None:
            q = q.filter(m.TradeOrder.block_id == str(block_id))
        if status:
            q = q.filter(m.TradeOrder.status == status)
        return [{"id": r.id, "order_id": r.order_id, "account_id": r.account_id,
                 "block_id": r.block_id, "country": r.country, "day": r.day,
                 "slot_idx": r.slot_idx, "side": r.side, "volume_kwh": r.volume_kwh,
                 "price_eur_mwh": r.price_eur_mwh, "source": r.source,
                 "status": r.status, "submitted_at": r.submitted_at}
                for r in q.order_by(m.TradeOrder.id).all()]


# ── Allocation ───────────────────────────────────────────────────────────────
def save_allocations(allocs: List) -> List[int]:
    """Zapíše zoznam Allocation (pydantic/dict). Vráti ids. battery_id → int."""
    now = _now()
    ids: List[int] = []
    with get_session() as s:
        for a in allocs:
            d = a.model_dump() if hasattr(a, "model_dump") else dict(a)
            row = m.AllocationRow(
                battery_id=int(d["battery_id"]),
                block_id=int(d["block_id"]) if str(d.get("block_id") or "").lstrip("-").isdigit() else None,
                order_id=d.get("order_id"),
                day=str(d["day"])[:10],
                slot_idx=int(d["slot_idx"]),
                share_kwh=float(d["share_kwh"]),
                setpoint_kw=float(d["setpoint_kw"]),
                source=d["source"],
                created_at=now,
            )
            s.add(row)
            s.flush()
            ids.append(row.id)
    return ids


def pending_allocations(battery_id: Optional[int] = None, day: Optional[str] = None,
                        slot_idx: Optional[int] = None) -> List[Dict]:
    """Alokácie, ktoré ešte neboli vykonané (applied_at IS NULL)."""
    with get_session() as s:
        q = s.query(m.AllocationRow).filter(m.AllocationRow.applied_at.is_(None))
        if battery_id is not None:
            q = q.filter(m.AllocationRow.battery_id == battery_id)
        if day:
            q = q.filter(m.AllocationRow.day == str(day)[:10])
        if slot_idx is not None:
            q = q.filter(m.AllocationRow.slot_idx == slot_idx)
        return [{"id": r.id, "battery_id": r.battery_id, "block_id": r.block_id,
                 "order_id": r.order_id, "day": r.day, "slot_idx": r.slot_idx,
                 "share_kwh": r.share_kwh, "setpoint_kw": r.setpoint_kw,
                 "source": r.source}
                for r in q.order_by(m.AllocationRow.id).all()]


def mark_allocation_applied(alloc_id: int) -> None:
    with get_session() as s:
        r = s.get(m.AllocationRow, alloc_id)
        if r and r.applied_at is None:
            r.applied_at = _now()


# ── AvailabilityReport ───────────────────────────────────────────────────────
def save_availability(report) -> int:
    d = report.model_dump() if hasattr(report, "model_dump") else dict(report)
    now = _now()
    with get_session() as s:
        row = m.AvailabilityReportRow(
            battery_id=int(d["battery_id"]), day=str(d["day"])[:10],
            slot_idx=int(d["slot_idx"]), soc_pct=float(d["soc_pct"]),
            free_charge_kw=float(d["free_charge_kw"]),
            free_discharge_kw=float(d["free_discharge_kw"]),
            free_kwh=float(d["free_kwh"]), eff=float(d.get("eff", 0.95)),
            created_at=now,
        )
        s.add(row)
        s.flush()
        return row.id
