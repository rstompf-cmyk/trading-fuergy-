#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workers/fleet_supervisor.py — supervisor pre model PROCES-PER-BATÉRIA.

Spustí JEDEN samostatný OS proces na každú ENABLED batériu
(`python -m workers.control_loop --battery <id>`), monitoruje ich a:
  • novú enabled batériu → naštartuje,
  • batériu už nie enabled / zmizla → pošle SIGTERM (graceful stop),
  • spadnutý proces → reštartuje (s jednoduchým backoffom).

Úplná izolácia: pád/reštart jednej inštancie sa netýka ostatných ani webu.
IPC stav (instance_status) píše každá inštancia sama cez control.loop.tick.

Spustenie:
    FLEET_CONTROL=1 python -m workers.fleet_supervisor
    FLEET_CONTROL=1 FLEET_TICK_SEC=60 FLEET_RECONCILE_SEC=15 python -m workers.fleet_supervisor

Vypnutie: SIGTERM / SIGINT → graceful stop všetkých inštancií.

Docker (dev): samostatný servis vedľa webu, rovnaký image, command
    ["python","-m","workers.fleet_supervisor"], env FLEET_CONTROL=1 (+ FLEET_REAL_WRITE
    len keď naozaj chceme zápis na HW).
"""
from __future__ import annotations
import os
import sys
import time
import signal
import subprocess
import datetime as dt
from typing import Dict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_RUNNING = True
_BACKOFF_BASE = 3.0      # s — počiatočný backoff po páde
_BACKOFF_MAX = 60.0      # s — strop


def _stop(signum, _frame):
    global _RUNNING
    _RUNNING = False
    print(f"[supervisor] signal {signum} → graceful stop", flush=True)


def _ts() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class _Inst:
    __slots__ = ("proc", "name", "started_at", "fails", "next_retry")

    def __init__(self):
        self.proc = None
        self.name = ""
        self.started_at = 0.0
        self.fails = 0
        self.next_retry = 0.0


def _spawn(battery_id: int) -> subprocess.Popen:
    """Spustí inštanciu pre danú batériu (dedí env vrátane FLEET_CONTROL/REAL_WRITE)."""
    env = dict(os.environ)
    env["FLEET_BATTERY_ID"] = str(battery_id)
    # control_loop.main vyžaduje FLEET_CONTROL=1 — supervisor beží len ak ho má,
    # takže ho deťom odovzdáme.
    env["FLEET_CONTROL"] = "1"
    cmd = [sys.executable, "-m", "workers.control_loop", "--battery", str(battery_id)]
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return subprocess.Popen(cmd, env=env, cwd=root)


def run(reconcile_sec: float | None = None, max_cycles: int | None = None) -> None:
    import fleet

    if reconcile_sec is None:
        reconcile_sec = float(os.environ.get("FLEET_RECONCILE_SEC", "15"))
    insts: Dict[int, _Inst] = {}

    print(f"[supervisor] štart — reconcile každých {reconcile_sec}s", flush=True)
    cycles = 0
    while _RUNNING:
        try:
            want = {b["id"]: b for b in fleet.list_batteries(enabled_only=True)}
        except Exception as e:
            print(f"[supervisor] {_ts()} DB chyba (skúsim znova): {e}", flush=True)
            want = {wid: insts[wid] for wid in insts}  # ponechaj bežiace, nereaguj

        # 1) štart nových + reštart spadnutých
        now = time.time()
        for bid, b in want.items():
            inst = insts.get(bid)
            if inst is None:
                inst = _Inst()
                insts[bid] = inst
            alive = inst.proc is not None and inst.proc.poll() is None
            if alive:
                continue
            if inst.proc is not None:  # spadol
                rc = inst.proc.returncode
                inst.fails += 1
                backoff = min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** min(inst.fails - 1, 5)))
                inst.next_retry = now + backoff
                print(f"[supervisor] {_ts()} inštancia bat {bid} skončila rc={rc} "
                      f"(fail #{inst.fails}) → reštart o {backoff:.0f}s", flush=True)
                inst.proc = None
            if now >= inst.next_retry:
                try:
                    inst.proc = _spawn(bid)
                    inst.name = (b.get("name") if isinstance(b, dict) else "") or str(bid)
                    inst.started_at = now
                    print(f"[supervisor] {_ts()} spustená inštancia bat {bid} "
                          f"({inst.name}) pid={inst.proc.pid}", flush=True)
                except Exception as e:
                    print(f"[supervisor] {_ts()} spawn bat {bid} zlyhal: {e}", flush=True)

        # 2) stop tých čo už nemajú byť (disabled / zmizli)
        for bid, inst in list(insts.items()):
            if bid in want:
                continue
            if inst.proc is not None and inst.proc.poll() is None:
                print(f"[supervisor] {_ts()} bat {bid} už nie enabled → SIGTERM", flush=True)
                _terminate(inst.proc)
            insts.pop(bid, None)

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        slept = 0.0
        while _RUNNING and slept < reconcile_sec:
            time.sleep(min(0.5, reconcile_sec - slept))
            slept += 0.5

    # graceful stop všetkých
    print("[supervisor] zastavujem všetky inštancie…", flush=True)
    for inst in insts.values():
        if inst.proc is not None and inst.proc.poll() is None:
            _terminate(inst.proc)
    print("[supervisor] zastavený", flush=True)


def _terminate(proc: subprocess.Popen, wait_s: float = 10.0) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=wait_s)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if os.environ.get("FLEET_CONTROL", "0") != "1":
        print("[supervisor] FLEET_CONTROL != 1 → idle (nastav FLEET_CONTROL=1). Končím.",
              flush=True)
        return
    run()


if __name__ == "__main__":
    main()
