#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workers/profile_runner.py — PROFIL (sim ALEBO reál) ako SAMOSTATNÝ PROCES.

Model PROCES-PER-PROFIL pre livesim/monitoring: každý profil má vlastný OS proces,
ktorý v slučke posúva livesim (advance) a persistuje výsledok — prehliadač je len
read-only monitor (číta z DB/disk cache). Generické: funguje pre sim aj reálne profily
(pre reálny profil advance počíta livesim z REÁLNYCH meraní; nič neposiela na HW — to
robí samostatná vrstva HW riadenia workers/fleet_supervisor + workers/control_loop).

Každý tick:
  • načíta parametre profilu (plan/rt),
  • pod SÚBOROVÝM zámkom per profil (fcntl — serializuje voči web GET workerovi v inom
    procese, kde threading.Lock neplatí) zavolá app._livesim_bg_tick_one() (tá istá overená
    advance+persist cesta ako doterajší bg loop),
  • zapíše heartbeat status (out/_status/prof_<profil>.json) — monitor ho zobrazí.

FAIL-SAFE: chyba ticku NEzhodí proces. Graceful stop: SIGTERM/SIGINT → dobehne tick a skončí.

Spustenie:
    PROFILE_BG_CONTROL=1 python -m workers.profile_runner --profile "Trakany_real"
    PROFILE_TICK_SEC=60 ...
Supervisor (workers/profile_supervisor.py) spúšťa 1 proces na každý bg_enabled profil.
"""
from __future__ import annotations
import os
import sys
import re
import time
import json
import signal
import datetime as dt
from typing import Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_RUNNING = True


def _stop(signum, _frame):
    global _RUNNING
    _RUNNING = False
    print(f"[profile-runner] signal {signum} → graceful stop", flush=True)


def _ts() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "profil"))


def _lock_path(profile: str) -> str:
    d = os.path.join("out", "_locks")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"prof_{_safe(profile)}.lock")


def _status_path(profile: str) -> str:
    d = os.path.join("out", "_status")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"prof_{_safe(profile)}.json")


def _write_status(profile: str, **kw) -> None:
    """Heartbeat status per profil (atomický zápis). Monitor ho číta.
    (DB tabuľka profile_bg_status je ďalší krok; zatiaľ JSON heartbeat = bez migrácie.)"""
    try:
        rec = {"profile": profile, "ts": _ts(), "pid": os.getpid()}
        rec.update(kw)
        p = _status_path(profile)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass


def run_single_profile(profile_name: str, tick_sec: Optional[float] = None,
                       max_ticks: Optional[int] = None) -> None:
    """Slučka advance+persist pre JEDEN profil (sim aj reál). Lazy import `app` až tu,
    nech modul importne aj bez app (test arg-parse/lock/status)."""
    import fcntl
    import app as _app
    import profiles as _pr

    if tick_sec is None:
        tick_sec = float(os.environ.get("PROFILE_TICK_SEC", "60"))

    print(f"[profile {profile_name}] štart — tick={tick_sec}s pid={os.getpid()}", flush=True)
    _write_status(profile_name, alive=True, health="starting")

    n = 0
    while _RUNNING:
        t0 = time.time()
        try:
            pdata = _pr.load_profile(profile_name)
            if not pdata:
                print(f"[profile {profile_name}] profil zmizol → graceful stop", flush=True)
                break
            if "bg_enabled" in pdata and not pdata.get("bg_enabled"):
                print(f"[profile {profile_name}] bg_enabled=False → graceful stop", flush=True)
                break

            MODES = _app._livesim_modes()
            case = "dt_15min" if "dt_15min" in MODES else next(iter(MODES))
            _lbl, _bc, _st = MODES[case]
            start = os.environ.get("PROFILE_START") or (dt.date.today() - dt.timedelta(days=7)).isoformat()
            live = _app._livesim_live_minutes()

            lf = open(_lock_path(profile_name), "w")
            try:
                fcntl.flock(lf, fcntl.LOCK_EX)
                appended = _app._livesim_bg_tick_one(case, start, _bc, _st, live, profile_name)
            finally:
                try:
                    fcntl.flock(lf, fcntl.LOCK_UN)
                finally:
                    lf.close()

            _write_status(profile_name, alive=True, health="ok",
                          mode=pdata.get("mode"), appended=int(appended or 0), tick=n,
                          took_s=round(time.time() - t0, 2))
            print(f"[profile {profile_name}] {_ts()} tick {n}: +{int(appended or 0)} min "
                  f"({round(time.time()-t0,1)}s)", flush=True)
        except Exception as e:
            print(f"[profile {profile_name}] {_ts()} tick {n} zlyhal (pokračujem): {e}", flush=True)
            _write_status(profile_name, alive=True, health="degraded", error=str(e)[:300], tick=n)

        n += 1
        if max_ticks is not None and n >= max_ticks:
            break
        sleep_s = max(0.0, tick_sec - (time.time() - t0))
        slept = 0.0
        while _RUNNING and slept < sleep_s:
            time.sleep(min(0.5, sleep_s - slept))
            slept += 0.5

    _write_status(profile_name, alive=False, health="stopped", tick=n)
    print(f"[profile {profile_name}] zastavený po {n} tickoch", flush=True)


def _parse_profile() -> Optional[str]:
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a in ("--profile", "-p") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--profile="):
            return a.split("=", 1)[1]
    return os.environ.get("PROFILE_NAME")


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if os.environ.get("PROFILE_BG_CONTROL", "0") != "1":
        print("[profile-runner] PROFILE_BG_CONTROL != 1 → idle. Končím.", flush=True)
        return
    prof = _parse_profile()
    if not prof:
        print("[profile-runner] chýba --profile <name>. Končím.", flush=True)
        return
    run_single_profile(prof)


if __name__ == "__main__":
    main()
