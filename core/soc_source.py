# -*- coding: utf-8 -*-
"""core/soc_source.py — JEDEN zdroj aktuálneho SOC (KROK 2 zjednotenia feasibility, 2026-06-28).

Cieľ (viď IMPLEMENTACNY_PLAN_feasibility_unify.md): skoncovať s 5 nezávislými zdrojmi
„aktuálneho SOC". Tento modul je domov pre kanonický engine-SOC reader + verejný
vstupný bod, ktorý majú volať VŠETKY cesty (VDT advisor, RT audit, auto_control, render).

KROK 2 = ČISTÁ CENTRALIZÁCIA (relokácia), žiadna zmena výpočtu:
  - `current_engine_soc()` = presunutý `vdt_state._get_current_soc_from_livesim_today`
    (kanonický reálny „aktuálny SOC" = posledný non-null soc_pct z DNEŠNÉHO livesim
     traceu <= now; fallback engine meta today_soc_pct). Toto je engine pravda
    (plán + VDT + RT po clipe). vdt_state ho teraz volá odtiaľto (parity).
  - `current_soc_pct()` = verejný vstupný bod = hodnota z vdt_state.compute_current_state
    (ktorá už SOC-UNIFY override používa current_engine_soc). De-facto single source.

Semantické zjednotenie ostatných čítačov (advisor.get_current_soc_pct realio reader,
odstránenie SOC-UNIFY override vetvy) ide v KROKU 3 pod DEV validáciou — tu sa nič
takého nemení, aby bol tento krok bezpečný a reverzibilný.
"""
from __future__ import annotations
import datetime as dt
import os
from typing import Any, Dict, Optional


def current_engine_soc(profile: str, today: dt.date,
                       now: dt.datetime) -> Optional[Dict[str, Any]]:
    """Kanonický REÁLNY „aktuálny SOC" — posledný non-null soc_pct z DNEŠNÉHO livesim
    traceu v čase <= now (engine pravda: plán + VDT + RT po clipe).

    Vracia {"soc_pct", "source"} alebo None ak dnešný trace ešte neexistuje (vtedy
    volajúci spraví fallback na plán projekciu). Bit-exact presun z pôvodného
    vdt_state._get_current_soc_from_livesim_today (KROK 2, žiadna zmena správania).
    """
    try:
        import livesim as _ls
        import pandas as _pd
    except Exception:
        return None
    port = os.environ.get("PORT") or os.environ.get("APP_PORT") or "8000"
    day_iso = today.isoformat()
    _cases = ["dt_15min", "plan_d1"]
    try:
        def _meta_mtime(_c):
            try:
                _, _mp = _ls.paths(_c, port, profile)
                return os.path.getmtime(_mp)
            except OSError:
                return 0.0
        _cases.sort(key=_meta_mtime, reverse=True)
    except Exception:
        pass
    # META-FIRST (2026-06-28): engine meta `today_soc_pct` je AUTORITATÍVNY aktuálny
    # realizovaný SOC (zapisuje livesim.advance s engine SOC vrátane RT). Berie sa PRED
    # trace, lebo `batt_kw_realistic` v trace je riedko vyplnené a posledný taký riadok
    # môže byť zastaraný (napr. 14:44) hoci engine dobehol ďalej (meta ts 17:43). Tým
    # má trade-control vždy posledný ZNÁMY reálny SOC, nie zastaraný riadok ani projekciu.
    try:
        for case in _cases:
            try:
                _, _mp = _ls.paths(case, port, profile)
                _meta = _ls._load_meta(_mp) or {}
            except Exception:
                continue
            _tsoc = _meta.get("today_soc_pct")
            _tts = _meta.get("today_soc_ts")
            if _tsoc is None or not _tts:
                continue
            try:
                _tts_ts = _pd.Timestamp(_tts)
            except Exception:
                continue
            if _tts_ts.date() == today and _tts_ts <= _pd.Timestamp(now):
                return {"soc_pct": float(_tsoc),
                        "source": f"engine meta today_soc_pct ({case}, profile={profile}, ts={str(_tts)[:19]})"}
    except Exception:
        pass
    for case in _cases:
        try:
            df = _ls.load_series(case, port=port, day=day_iso, max_points=10**9, profile=profile)
        except Exception:
            continue
        if (df is None or df.empty or "soc_pct" not in df.columns
                or "time" not in df.columns):
            continue
        sub = df[df["soc_pct"].notna()].copy()
        if sub.empty:
            continue
        # Iba minúty <= now (nie budúcnosť).
        try:
            sub["_t"] = _pd.to_datetime(sub["time"], errors="coerce")
            sub = sub[sub["_t"] <= _pd.Timestamp(now)]
        except Exception:
            sub["_t"] = range(len(sub))
        if sub.empty:
            continue
        # REALIZED-FIRST (2026-06-28, Bug SOC-CURRENT-VS-PROJECTION): „aktuálny SOC" MUSÍ
        # byť z REÁLNE odsimulovaných minút, nie z plánovej projekcie. Trace dnešok obsahuje
        # realizované minúty (batt_kw_realistic != NaN) AJ budúcu/plánovú projekciu
        # (batt_kw_realistic = NaN, nesie plánový SOC napr. 100%). „Posledný <= now" chytal
        # projekciu → trade-control dostal phantom 100% (reálne ~5%). Preto: ak existujú
        # realizované riadky, ber LEN z nich; zoradiť podľa času a vziať POSLEDNÝ realizovaný.
        _realized = sub
        if "batt_kw_realistic" in sub.columns:
            _rz = sub[_pd.to_numeric(sub["batt_kw_realistic"], errors="coerce").notna()]
            if not _rz.empty:
                _realized = _rz
        _realized = _realized.sort_values("_t")
        soc = float(_realized["soc_pct"].iloc[-1])
        ts = str(_realized["time"].iloc[-1])[:19]
        return {"soc_pct": soc,
                "source": f"livesim REALIZED ({case}, profile={profile}, ts={ts})"}
    # Dnešok sa do CSV neukladá (provizórny), ale livesim.advance ukladá engine dnešný
    # SOC (RT+DT+VDT) do meta (today_soc_pct/ts). Prečítaj ho ako jediný zdroj.
    for case in _cases:
        try:
            _, _mp = _ls.paths(case, port, profile)
            _meta = _ls._load_meta(_mp) or {}
        except Exception:
            continue
        _tsoc = _meta.get("today_soc_pct")
        _tts = _meta.get("today_soc_ts")
        if _tsoc is None or not _tts:
            continue
        try:
            _tts_ts = _pd.Timestamp(_tts)
        except Exception:
            continue
        if _tts_ts.date() == today and _tts_ts <= _pd.Timestamp(now):
            return {"soc_pct": float(_tsoc),
                    "source": f"livesim dnešok engine meta ({case}, profile={profile}, ts={str(_tts)[:19]})"}
    return None


def current_soc_pct(profile: str, *, now: Optional[dt.datetime] = None,
                    today: Optional[dt.date] = None,
                    batt_kwh: Optional[float] = None) -> Dict[str, Any]:
    """VEREJNÝ vstupný bod pre aktuálny SOC (%). De-facto single source of truth:
    deleguje na vdt_state.compute_current_state (ktorá kanonický SOC berie cez
    current_engine_soc + fallback plán projekcia).

    Vracia {"ok", "soc_pct", "source", "slot_idx", "state"} (+ "error" ak zlyhá).
    """
    try:
        import vdt_state as _vs
        st = _vs.compute_current_state(profile, today=today, now=now, batt_kwh=batt_kwh)
        return {"ok": True,
                "soc_pct": float(st["current_soc_pct"]),
                "source": st.get("current_soc_source", "compute_current_state"),
                "slot_idx": st.get("current_slot_idx"),
                "state": st}
    except Exception as e:
        return {"ok": False, "soc_pct": None, "source": "error",
                "error": f"current_soc_pct zlyhal: {e}"}


__all__ = ["current_engine_soc", "current_soc_pct"]
