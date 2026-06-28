# -*- coding: utf-8 -*-
"""test_soc_source.py — KROK 2 (2026-06-28). Smoke test core/soc_source.

Plná validácia SOC I/O (realio_db, livesim trace) sa robí na DEV (sandbox nemá runtime
dáta). Tu len kontrakt: import OK, current_engine_soc bez traceu → None (graceful),
current_soc_pct vždy vráti dict s kľúčom 'ok'.
"""
import os, sys, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.soc_source as ss


def test_import_and_api():
    assert hasattr(ss, "current_engine_soc")
    assert hasattr(ss, "current_soc_pct")


def test_engine_soc_no_trace_returns_none():
    # neexistujúci profil → žiadny trace ani meta → None (fallback na plán projekciu)
    r = ss.current_engine_soc("__neexistujuci_profil__", dt.date.today(), dt.datetime.now())
    assert r is None


def test_current_soc_pct_returns_dict():
    r = ss.current_soc_pct("__neexistujuci_profil__")
    assert isinstance(r, dict) and "ok" in r


if __name__ == "__main__":
    test_import_and_api()
    test_engine_soc_no_trace_returns_none()
    test_current_soc_pct_returns_dict()
    print("soc_source smoke OK")
