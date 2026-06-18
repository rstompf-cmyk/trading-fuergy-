# -*- coding: utf-8 -*-
"""control/ — VPP riadenie inštancie batérie (per-batéria control loop + executor).

tick(battery_id, executor): jeden control tick (príkazy → setpoint → status, fail-safe).
Executor: pluggable vykonávač (SimExecutor / RealExecutor; DummyExecutor pre testy).
build_executor(battery): postaví executor podľa módu batérie.
"""
from .executor import Executor, DummyExecutor, SimExecutor, RealExecutor, build_executor
from .loop import tick
from .runner import build_fleet_executors, tick_fleet

__all__ = [
    "Executor", "DummyExecutor", "SimExecutor", "RealExecutor", "build_executor",
    "tick", "build_fleet_executors", "tick_fleet",
]
