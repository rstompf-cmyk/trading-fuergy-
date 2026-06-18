# -*- coding: utf-8 -*-
"""trading/availability.py — batéria → AvailabilityReport (čo vie spraviť v slote).

Čistá funkcia (žiadny I/O). Z parametrov batérie + aktuálneho SOC spočíta voľnú
kapacitu nabíjania/vybíjania v danom 15-min slote (limit = výkon batérie aj SOC
pásmo). Konvencia: free_*_kw sú nezáporné (smer rieši trading cez Order.side)."""
from __future__ import annotations
import datetime as dt

from core.schemas.vpp import AvailabilityReport


def battery_availability(battery: dict, soc_pct: float, day: str, slot_idx: int, *,
                         dt_h: float = 0.25, soc_min: float = 5.0,
                         soc_max: float = 95.0) -> AvailabilityReport:
    """AvailabilityReport pre jednu batériu a jeden slot.

    free_discharge_kw = min(výkon, energia nad podlahou / dt_h)
    free_charge_kw    = min(výkon, priestor pod stropom / dt_h)
    Limity SOC (soc_min/max) default 5/95 % — zhodné s executorom."""
    kw = float(battery.get("batt_kw") or 0.0)
    kwh = float(battery.get("batt_kwh") or 0.0)
    soc = max(0.0, min(100.0, float(soc_pct)))
    dt_h = max(float(dt_h), 1e-9)

    dischargeable_kwh = max(0.0, (soc - soc_min) / 100.0 * kwh)
    chargeable_kwh = max(0.0, (soc_max - soc) / 100.0 * kwh)
    free_dis_kw = max(0.0, min(kw, dischargeable_kwh / dt_h))
    free_chg_kw = max(0.0, min(kw, chargeable_kwh / dt_h))

    return AvailabilityReport(
        battery_id=str(battery["id"]),
        day=str(day)[:10],
        slot_idx=int(slot_idx),
        soc_pct=round(soc, 3),
        free_charge_kw=round(free_chg_kw, 3),
        free_discharge_kw=round(free_dis_kw, 3),
        free_kwh=round(dischargeable_kwh, 3),
        eff=float(battery.get("eff") or 0.95),
        limits={"soc_min": soc_min, "soc_max": soc_max, "batt_kw": kw},
        ts=dt.datetime.now().isoformat(timespec="seconds"),
    )
