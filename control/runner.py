# -*- coding: utf-8 -*-
"""control/runner.py — fleet orchestrácia control loopu (sim cesta end-to-end).

build_fleet_executors(): pre enabled batérie z DB postaví executor (sim/real),
  prenesie posledný SOC zo statusu.
tick_fleet(executors): spustí control.tick pre každú batériu — IZOLOVANE
  (chyba/výnimka jednej batérie nezhodí ostatné; real bez wiringu → fail-safe
  degraded, neblokuje fleet).

Toto je orchestračný helper. control_loop ako samostatný PROCES + supervisor +
real-time scheduling (minútový tick) je ďalší integračný krok (infraštruktúra).
Executory sú dlhožijúce (držia SOC stav) — postav raz, tickaj opakovane.
"""
from __future__ import annotations
from typing import Dict

from .executor import Executor, build_executor
from .loop import tick


def build_fleet_executors(soc_default: float = 50.0) -> Dict[int, Executor]:
    """Postaví executor pre každú ENABLED batériu (z DB). SOC prenesie zo statusu
    ak existuje, inak soc_default."""
    import fleet
    execs: Dict[int, Executor] = {}
    for b in fleet.list_batteries(enabled_only=True):
        st = fleet.get_status(b["id"])
        soc = (st or {}).get("soc_pct")
        execs[b["id"]] = build_executor(b, soc_pct=soc if soc is not None else soc_default)
    return execs


def tick_fleet(executors: Dict[int, Executor], dt_h: float = 1.0 / 60.0) -> Dict[int, dict]:
    """Jeden control tick naprieč flotilou. Každá batéria izolovane — chyba jednej
    (vrátane real bez wiringu → fail-safe) NEzhodí ostatné. Vráti {battery_id: result}."""
    results: Dict[int, dict] = {}
    for bid, ex in executors.items():
        try:
            results[bid] = tick(bid, ex, dt_h=dt_h)
        except Exception as e:
            # tick() sám nehádže, ale poistka pre istotu — izolácia per batéria
            results[bid] = {"target_kw": 0.0, "soc_pct": None, "health": "degraded",
                            "stopped": False, "applied": False, "error": str(e)}
    return results


__all__ = ["build_fleet_executors", "tick_fleet"]
