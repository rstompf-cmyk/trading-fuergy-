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


def run_single(battery_id: int, tick_sec: Optional[float] = None,
               max_ticks: Optional[int] = None, dt_h: Optional[float] = None) -> None:
    """Tick LEN jednej batérie — pre model PROCES-PER-BATÉRIA (úplná izolácia).

    Executor sa postaví raz (dlhožijúci — drží SOC v sim) a tickuje sa opakovane;
    setpoint sa drží medzi tickmi (príkaz z DB ho prepíše cez control.loop.tick).
    Ak batéria zmizne / sa vypne (enabled=False), proces sa korektne ukončí —
    supervisor ho už znova nespustí. Chyba ticku NEzhodí proces (fail-safe)."""
    from control.executor import build_executor
    from control.loop import tick
    import fleet

    if tick_sec is None:
        tick_sec = float(os.environ.get("FLEET_TICK_SEC", "60"))
    if dt_h is None:
        dt_h = tick_sec / 3600.0

    b = fleet.get_battery(battery_id)
    if not b:
        print(f"[instance {battery_id}] batéria neexistuje → končím", flush=True)
        return
    st = fleet.get_status(battery_id)
    soc0 = (st or {}).get("soc_pct")
    ex = build_executor(b, soc_pct=soc0 if soc0 is not None else 50.0)
    held = 0.0
    print(f"[instance {battery_id}] štart — {b.get('name')} ({b.get('mode')}/"
          f"{b.get('backend') or 'realio'}), tick={tick_sec}s", flush=True)

    n = 0
    while _RUNNING:
        t0 = time.time()
        try:
            cur = fleet.get_battery(battery_id)
            if not cur or not cur.get("enabled", False):
                print(f"[instance {battery_id}] batéria vypnutá/zmizla → graceful stop", flush=True)
                break
            res = tick(battery_id, ex, current_setpoint_kw=held, dt_h=dt_h)
            held = float(res.get("target_kw", 0.0))
            print(f"[instance {battery_id}] {_ts()} tick {n}: {res.get('health')} "
                  f"sp={held:+.1f}kW soc={res.get('soc_pct')}", flush=True)
        except Exception as e:
            print(f"[instance {battery_id}] {_ts()} tick {n} zlyhal (pokračujem): {e}", flush=True)

        n += 1
        if max_ticks is not None and n >= max_ticks:
            break
        sleep_s = max(0.0, tick_sec - (time.time() - t0))
        slept = 0.0
        while _RUNNING and slept < sleep_s:
            step = min(0.5, sleep_s - slept)
            time.sleep(step)
            slept += step

    print(f"[instance {battery_id}] zastavený po {n} tickoch", flush=True)


def _parse_battery_id() -> Optional[int]:
    """--battery <id> z argv alebo env FLEET_BATTERY_ID."""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a in ("--battery", "-b") and i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                return None
        if a.startswith("--battery="):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                return None
    env = os.environ.get("FLEET_BATTERY_ID")
    if env:
        try:
            return int(env)
        except ValueError:
            return None
    return None


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    # explicitný súhlas: bez FLEET_CONTROL=1 sa netickuje (nech sa proces
    # nespustí omylom v prostredí kde ho nechceme)
    if os.environ.get("FLEET_CONTROL", "0") != "1":
        print("[control_loop] FLEET_CONTROL != 1 → idle (nastav FLEET_CONTROL=1 "
              "pre reálny beh). Končím.", flush=True)
        return
    bid = _parse_battery_id()
    if bid is not None:
        run_single(bid)          # model proces-per-batéria
    else:
        run()                    # legacy: jeden proces tiká celú flotilu


if __name__ == "__main__":
    main()
