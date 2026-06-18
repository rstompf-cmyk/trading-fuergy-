# -*- coding: utf-8 -*-
"""fleet — VPP register flotily (batérie/bloky/účty) + IPC (status/príkazy).

Verejné API (plain dicts, žiadne ORM objekty von):
    fleet.register_battery(...), list_batteries(), get_battery(), set_enabled()
    fleet.active_assignment(), batteries_in_block()
    fleet.write_status(), get_status(), fleet_status()          # IPC inštancia→jadro
    fleet.enqueue_command(), pending_commands(), mark_command_consumed()  # jadro→inštancia
"""
from .repository import (
    register_battery, list_batteries, get_battery, set_enabled,
    active_assignment, batteries_in_block,
    write_status, get_status, fleet_status,
    enqueue_command, pending_commands, mark_command_consumed,
)

__all__ = [
    "register_battery", "list_batteries", "get_battery", "set_enabled",
    "active_assignment", "batteries_in_block",
    "write_status", "get_status", "fleet_status",
    "enqueue_command", "pending_commands", "mark_command_consumed",
]
