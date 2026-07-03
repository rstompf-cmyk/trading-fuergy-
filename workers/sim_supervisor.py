#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workers/sim_supervisor.py — supervisor pre model PROCES-PER-SIM-PROFIL.

Spustí JEDEN samostatný OS proces na každý bg_enabled sim profil
(`python -m workers.sim_profile_runner --profile <name>`), monitoruje ich a:
  • nový bg_enabled profil → naštartuje,
  • profil už nie bg_enabled / zmizol → SIGTERM (graceful stop),
  • spadnutý proces → reštartuje (exponenciálny backoff).

Analóg workers/fleet_supervisor.py (batérie), len kľúčom je NÁZOV profilu.
Úplná izolácia: pád jednej inštancie sa netýka ostatných ani webu.

Ktoré profily bežia:
  • SIM_PROFILES="Trakany,Simulacia_Coop"  → explicitný zoznam (test/manuál), ALEBO
  • profily s param bg_enabled=True (keď je zoznam prázdny).

Spustenie:
    SIM_PROFILE_CONTROL=1 python -m workers.sim_supervisor
    SIM_PROFILE_CONTROL=1 SIM_RECONCILE_SEC=15 SIM_TICK_SEC=60 python -m workers.sim_supervisor

Docker (dev): samostatný servis vedľa webu (rovnaký image, iný command) →
    reštart webu nezhodí sim behy a naopak.
"""
from __future__ import annotations
import os
import sys
import time
import signal
import subprocess
import datetime as dt
from typing import Dict, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_RUNNING = True
_BACKOFF_BASE = 3.0
_BACKOFF_MAX = 60.0


def _stop(signum, _frame):
    global _RUNNING
    _RUNNING = False
    print(f"[sim-supervisor] signal {signum} → graceful stop", flush=True)


def _ts() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class _Inst:
    __slots__ = ("proc", "started_at", "fails", "next_retry")

    def __init__(self):
        self.proc = None
        self.started_at = 0.0
        self.fails = 0
        self.next_retry = 0.0


def _want_profiles() -> List[str]:
    """Zoznam profilov, ktoré majú bežať: explicitný SIM_PROFILES, inak bg_enabled=True."""
    env = os.environ.get("SIM_PROFILES", "").strip()
    if env:
        return [p.strip() for p in env.split(",") if p.strip()]
    import profiles as _pr
    out: List[str] = []
    for name in (_pr.list_profiles() or []):
        if not name or name == "default":
            continue
        try:
            pd = _pr.load_profile(name) or {}
            if pd.get("bg_enabled"):
                out.append(name)
        except Exception:
            pass
    return out


def _spawn(profile: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["SIM_PROFILE_CONTROL"] = "1"
    env["SIM_PROFILE_NAME"] = profile
    cmd = [sys.executable, "-m", "workers.sim_profile_runner", "--profile", profile]
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return subprocess.Popen(cmd, env=env, cwd=root)


def _terminate(proc: subprocess.Popen, wait_s: float = 10.0) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=wait_s)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run(reconcile_sec: float | None = None, max_cycles: int | None = None) -> None:
    if reconcile_sec is None:
        reconcile_sec = float(os.environ.get("SIM_RECONCILE_SEC", "15"))
    insts: Dict[str, _Inst] = {}

    print(f"[sim-supervisor] štart — reconcile každých {reconcile_sec}s", flush=True)
    cycles = 0
    while _RUNNING:
        try:
            want = set(_want_profiles())
        except Exception as e:
            print(f"[sim-supervisor] {_ts()} zoznam profilov zlyhal (ponechávam bežiace): {e}", flush=True)
            want = set(insts.keys())

        now = time.time()
        # 1) štart nových + reštart spadnutých
        for name in want:
            inst = insts.get(name)
            if inst is None:
                inst = _Inst()
                insts[name] = inst
            alive = inst.proc is not None and inst.proc.poll() is None
            if alive:
                continue
            if inst.proc is not None:  # spadol
                rc = inst.proc.returncode
                inst.fails += 1
                backoff = min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** min(inst.fails - 1, 5)))
                inst.next_retry = now + backoff
                print(f"[sim-supervisor] {_ts()} '{name}' skončil rc={rc} "
                      f"(fail #{inst.fails}) → reštart o {backoff:.0f}s", flush=True)
                inst.proc = None
            if now >= inst.next_retry:
                try:
                    inst.proc = _spawn(name)
                    inst.started_at = now
                    print(f"[sim-supervisor] {_ts()} spustený '{name}' pid={inst.proc.pid}", flush=True)
                except Exception as e:
                    print(f"[sim-supervisor] {_ts()} spawn '{name}' zlyhal: {e}", flush=True)

        # 2) stop tých čo už nemajú byť
        for name, inst in list(insts.items()):
            if name in want:
                continue
            if inst.proc is not None and inst.proc.poll() is None:
                print(f"[sim-supervisor] {_ts()} '{name}' už nie bg_enabled → SIGTERM", flush=True)
                _terminate(inst.proc)
            insts.pop(name, None)

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        slept = 0.0
        while _RUNNING and slept < reconcile_sec:
            time.sleep(min(0.5, reconcile_sec - slept))
            slept += 0.5

    print("[sim-supervisor] zastavujem všetky inštancie…", flush=True)
    for inst in insts.values():
        if inst.proc is not None and inst.proc.poll() is None:
            _terminate(inst.proc)
    print("[sim-supervisor] zastavený", flush=True)


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if os.environ.get("SIM_PROFILE_CONTROL", "0") != "1":
        print("[sim-supervisor] SIM_PROFILE_CONTROL != 1 → idle (nastav =1). Končím.", flush=True)
        return
    run()


if __name__ == "__main__":
    main()
