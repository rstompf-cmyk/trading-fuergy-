#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workers/vdt_trader.py — GLOBÁLNY živý VDT paper trader (10 s) ako SAMOSTATNÝ PROCES.

VDT (SIDC 15-min) je veľmi živé — 15-min scheduler advisor je pomalý. Tento proces
každých VDT_TRADE_SEC (default 10 s) prehodnotí VDT pre VŠETKY profily, ktoré majú VDT
(gate use_vdt), a keď sa nájde ziskový + feasibilný pár, UZAVRIE ho ako PAPER obchod.

Reuse 100 %: volá scheduler.job_vdt_advisor() — tá už iteruje všetky VDT profily v aktívnom
trhu, aplikuje gate (should_log_vdt_for_profile), CZ guard (VDT je SK/OKTE) a per profil spustí
run_and_cache → append_paper_trade (SOC/DAM audit + zápis do lokálneho vdt_paper_trades.csv).

*** PAPER ONLY *** — žiadne reálne ordery. V celom repe NEEXISTUJE kód na odoslanie obchodu
na trh (žiadny place/submit/create order); „uzavretie" = riadok v lokálnom CSV + posun sim SOC.
OKTE sa iba ČÍTA (orderbook). Tento proces nahrádza pomalú 15-min scheduler cadenciu živým 10 s.

FAIL-SAFE: chyba ticku nezhodí proces. Graceful stop: SIGTERM/SIGINT.

Spustenie:
    VDT_TRADE_CONTROL=1 python -m workers.vdt_trader
    VDT_TRADE_SEC=10 ...
POZOR: keď beží tento proces, VYPNI 15-min scheduler vdt_advisor (nech sa VDT nepočíta dvakrát).
"""
from __future__ import annotations
import os
import sys
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
    print(f"[vdt-trader] signal {signum} → graceful stop", flush=True)


def _ts() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _write_status(**kw) -> None:
    try:
        d = os.path.join("out", "_status")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "vdt_trader.json")
        rec = {"ts": _ts(), "pid": os.getpid()}
        rec.update(kw)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass


def run(watch_sec: Optional[float] = None, max_ticks: Optional[int] = None) -> None:
    if watch_sec is None:
        watch_sec = float(os.environ.get("VDT_TRADE_SEC", "10"))
    import scheduler as _sch

    print(f"[vdt-trader] štart — všetky VDT profily každých {watch_sec}s (paper-only)", flush=True)
    _write_status(alive=True, health="starting", watch_sec=watch_sec)
    n = 0
    while _RUNNING:
        t0 = time.time()
        try:
            _sch.job_vdt_advisor()          # loop cez všetky VDT profily + commit paper obchodov
            took = round(time.time() - t0, 2)
            _write_status(alive=True, health="ok", tick=n, took_s=took, watch_sec=watch_sec)
            print(f"[vdt-trader] {_ts()} tick {n}: VDT prehodnotené ({took}s)", flush=True)
        except Exception as e:
            print(f"[vdt-trader] {_ts()} tick {n} zlyhal (pokračujem): {e}", flush=True)
            _write_status(alive=True, health="degraded", error=str(e)[:300], tick=n)
        n += 1
        if max_ticks is not None and n >= max_ticks:
            break
        sleep_s = max(0.0, watch_sec - (time.time() - t0))
        slept = 0.0
        while _RUNNING and slept < sleep_s:
            time.sleep(min(0.5, sleep_s - slept))
            slept += 0.5
    _write_status(alive=False, health="stopped", tick=n)
    print(f"[vdt-trader] zastavený po {n} tickoch", flush=True)


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if os.environ.get("VDT_TRADE_CONTROL", "0") != "1":
        print("[vdt-trader] VDT_TRADE_CONTROL != 1 → idle. Končím.", flush=True)
        return
    run()


if __name__ == "__main__":
    main()
