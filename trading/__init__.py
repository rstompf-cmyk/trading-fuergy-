# -*- coding: utf-8 -*-
"""trading/ — VPP trading plumbing na kontraktoch (asset ↔ agregácia ↔ control).

NEOBSAHUJE ekonomiku (VDT/RT rozhodovanie ostáva v existujúcich moduloch a napojí
sa na Order v samostatnej golden-chránenej session). Tu len PREPOJENIE:
  • availability.battery_availability — batéria → AvailabilityReport (čo vie spraviť),
  • dispatch.dispatch_order — Order bloku → split na batérie → enqueue setpoint
    príkazov (most na control loop cez existujúce IPC instance_command).
"""
from .availability import battery_availability
from .dispatch import build_block_reports, block_aggregate, dispatch_order
from .economics_bridge import vdt_trades_to_orders, dispatch_vdt_trades
from .repository import (save_order, list_orders, save_allocations,
                         pending_allocations, mark_allocation_applied, save_availability)

__all__ = [
    "battery_availability", "build_block_reports", "block_aggregate", "dispatch_order",
    "vdt_trades_to_orders", "dispatch_vdt_trades",
    "save_order", "list_orders", "save_allocations", "pending_allocations",
    "mark_allocation_applied", "save_availability",
]
