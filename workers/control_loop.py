#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workers/control_loop.py — VPP control loop ako SAMOSTATNÝ PROCES.

Real-time tick flotily (default 60 s = minútový tick), IZOLOVANÝ od webu: web
reštart/pád nezhodí riadenie a naopak. Každý tick:
  • build_fleet_executors() — enabled batérie z DB (SOC sa nesie cez instance_status),
  • tick_fleet(...) s perzistovaným setpointom per batéria (príkaz z DB ho prepíše,
    inak sa drží → plynulé riadenie, nie one-shot),
  • stav per batéria → instance_status (ts = heartbeat; /fleet ho zobrazí).

BEZPEČNOSŤ: real batérie bez realio wiringu → RealExecutor raise → fail-safe
degraded (NEvykoná nič), takže spustenie je bezpečné aj pred dokončením real
wiringu. SIM batérie sa tickajú normálne.

Spustenie:
    python -m workers.control_loop
    FLEET_TICK_SEC=60 python -m workers.control_loop      # cadence
    FLEET_CONTROL=1 ...                                    # explicitný súhlas (inak idle)

Vypnutie: SIGTERM / SIGINT (Ctrl-C) → graceful stop (dokončí bežiaci tick).

Docker (dev, oddelený proces vedľa web servisu) — pridať do docker-compose.yml:
    control-loop-dev:
      image: trading-fuergy:dev
      command: ["python", "-m", "workers.control_loop"]
      environment: [ "FLEET_CONTROL=1", "FLEET_TICK_SEC=60", "DB_URL=..." ]
      volumes: [ ... rovnaké mounty / DB ako trading-fuergy-dev ... ]
      restart: unless-stopped
  (rovnaký image, iný command → reštart webu nezhodí riadenie a naopak.)
"""
from __future__ import annotations
import os
import sys
import time
import signal
import datetime as dt
from typing import Dict, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_RUNNING = True


def _stop(signum, _frame):
    global _RUNNING
    _RUNNING = False
    print(f"[control_loop] signal {signum} → graceful stop", flush=True)


def _ts() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def run(tick_sec: Optional[float] = None, max_ticks: Optional[int] = None,
        dt_h: Optional[float] = None) -> None:
    """Hlavná slučka. `max_ticks` (test) = po koľkých tickoch skončiť.
    `dt_h` (test) = energetický krok per tick; default = tick_sec/3600 (reálne =
    dĺžka intervalu). Override len pre rýchle testy (veľký dt_h, malá cadencia)."""
    from control.runner import build_fleet_executors, tick_fleet

    if tick_sec is None:
        tick_sec = float(os.environ.get("FLEET_TICK_SEC", "60"))
    if dt_h is None:
        dt_h = tick_sec / 3600.0
    last_setpoint: Dict[int, float] = {}   # perzistencia setpointu medzi tickmi

    print(f"[control_loop] štart — tick={tick_sec}s, dt_h={dt_h:.5f}", flush=True)
    n = 0
    while _RUNNING:
        t0 = time.time()
        try:
            execs = build_fleet_executors()
            if execs:
                # zahoď setpointy batérií ktoré už nie sú vo flotile
                last_setpoint = {b: kw for b, kw in last_setpoint.items() if b in execs}
                res = tick_fleet(execs, dt_h=dt_h, setpoints=last_setpoint)
                # prenes držaný setpoint do ďalšieho ticku
                last_setpoint = {b: float(r.get("target_kw", 0.0)) for b, r in res.items()}
                ok = sum(1 for r in res.values() if r.get("health") == "ok")
                deg = sum(1 for r in res.values() if r.get("health") == "degraded")
                print(f"[control_loop] {_ts()} tick {n}: {ok} ok, {deg} degraded "
                      f"({len(res)} batérií)", flush=True)
            else:
                print(f"[control_loop] {_ts()} tick {n}: žiadne enabled batérie", flush=True)
        except Exception as e:
            # outer poistka — slučka NIKDY nespadne na jednom ticku
            print(f"[control_loop] {_ts()} tick {n} zlyhal (pokračujem): {e}", flush=True)

        n += 1
        if max_ticks is not None and n >= max_ticks:
            break

        # presná cadence + prerušiteľný spánok (rýchla reakcia na SIGTERM)
        sleep_s = max(0.0, tick_sec - (time.time() - t0))
        slept = 0.0
        while _RUNNING and slept < sleep_s:
            step = min(0.5, sleep_s - slept)
            time.sleep(step)
            slept += step

    print(f"[control_loop] zastavený po {n} tickoch", flush=True)


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    # explicitný súhlas: bez FLEET_CONTROL=1 sa netickuje (nech sa proces
    # nespustí omylom v prostredí kde ho nechceme)
    if os.environ.get("FLEET_CONTROL", "0") != "1":
        print("[control_loop] FLEET_CONTROL != 1 → idle (nastav FLEET_CONTROL=1 "
              "pre reálny beh). Končím.", flush=True)
        return
    run()


if __name__ == "__main__":
    main()
