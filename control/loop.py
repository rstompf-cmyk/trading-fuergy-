# -*- coding: utf-8 -*-
"""control/loop.py — per-batéria control loop (jeden tick).

Srdce modelu „inštancia per batéria": izolovaný, len VYKONÁVA a HLÁSI.
Jeden tick:
  1. načíta čakajúce príkazy z DB (fleet repo) → aplikuje (setpoint / stop / mode)
  2. vykoná cieľový setpoint cez Executor (sim/real)
  3. zapíše status (alive/health/soc/last_setpoint) cez fleet repo
  4. FAIL-SAFE: pri chybe executora nespadne — drží bezpečný setpoint (default 0 =
     idle, NIE náhodná hodnota), označí health='degraded', zaloguje error.

Príkazy spracúva idempotentne (mark_command_consumed). Cieľový setpoint berie z
posledného 'setpoint' príkazu v dávke; 'stop' → 0 + stopped; 'mode' → zmena módu.
(Allocation z trading vrstvy sa napojí ako zdroj príkazov v integračnej fáze.)
"""
from __future__ import annotations
from typing import Optional


def tick(battery_id: int, executor, *,
         current_setpoint_kw: float = 0.0, dt_h: float = 1.0 / 60.0,
         fail_safe_kw: float = 0.0) -> dict:
    """Spracuje jeden control tick batérie. Vráti súhrn {target_kw, soc_pct,
    health, stopped, applied}. NIKDY nehádže — chyby idú do fail-safe + status."""
    import fleet

    target_kw = float(current_setpoint_kw)
    stopped = False
    mode: Optional[str] = None

    # 1. príkazy z jadra (FIFO) → aplikuj posledný relevantný
    try:
        for cmd in fleet.pending_commands(battery_id):
            t = str(cmd.get("type", "")).lower()
            payload = cmd.get("payload") or {}
            if t == "setpoint":
                target_kw = float(payload.get("kw", target_kw))
            elif t == "stop":
                target_kw = 0.0
                stopped = True
            elif t == "mode":
                mode = str(payload.get("mode", "")) or None
            # start/restart = no-op pre setpoint (rieši supervisor)
            fleet.mark_command_consumed(int(cmd["id"]))
    except Exception as e:
        # príkazy zlyhali — pokračuj s current_setpoint (nezhadzuj loop)
        _safe_status(battery_id, health="degraded", setpoint=fail_safe_kw,
                     error=f"command read zlyhal: {e}", mode=mode)
        return {"target_kw": fail_safe_kw, "soc_pct": None, "health": "degraded",
                "stopped": stopped, "applied": False}

    # 2. vykonaj setpoint cez executor
    try:
        res = executor.apply_setpoint(battery_id, target_kw, dt_h=dt_h)
        soc = float(res.get("soc_pct")) if res.get("soc_pct") is not None else None
        applied_kw = float(res.get("applied_kw", target_kw))
        _safe_status(battery_id, health="ok", setpoint=applied_kw, soc=soc, mode=mode)
        return {"target_kw": target_kw, "soc_pct": soc, "health": "ok",
                "stopped": stopped, "applied": True}
    except Exception as e:
        # 4. FAIL-SAFE: drž bezpečný setpoint, NEpadni
        soc = None
        try:
            soc = float(executor.read_soc(battery_id))
        except Exception:
            pass
        _safe_status(battery_id, health="degraded", setpoint=fail_safe_kw, soc=soc,
                     error=f"executor zlyhal: {e}", mode=mode)
        return {"target_kw": fail_safe_kw, "soc_pct": soc, "health": "degraded",
                "stopped": stopped, "applied": False}


def _safe_status(battery_id: int, *, health: str, setpoint: float,
                 soc: Optional[float] = None, error: Optional[str] = None,
                 mode: Optional[str] = None) -> None:
    """Zápis statusu, ktorý sám nesmie zhodiť loop."""
    try:
        import fleet
        fleet.write_status(battery_id, alive=True, health=health, soc_pct=soc,
                           last_setpoint_kw=setpoint, error=error, mode=mode)
    except Exception:
        pass
