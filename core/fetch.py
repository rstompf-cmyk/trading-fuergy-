# -*- coding: utf-8 -*-
"""
core.fetch — vysokoúrovňové data fetch helpery pre /rt poradcu.

Extrahované z app.py (Fáza 1 refactoringu). Závisí na core.caches a core.state.
"""
from __future__ import annotations
import datetime as dt
import pandas as pd

import data_sources as ds
from core.caches import _fetch_ote_cached
from core.state import FX_CZK


def _iv_ts(date, interval: str) -> pd.Timestamp:
    """Konvertuje OTE interval string ('HH:MM-HH:MM') na Timestamp začiatku intervalu."""
    hh, mm = str(interval).split("-")[0].strip().split(":")
    return pd.Timestamp(date) + pd.Timedelta(hours=int(hh), minutes=int(mm))


def _rt_fetch():
    """Stiahne živé dáta pre RT poradcu: DT (dnes), odhad ZCO, sys. odchýlka, aFRR cena.

    Každý zdroj je samostatne ošetrený try/except — pri chybe (napr. OTE 503) vráti
    prázdny DataFrame namiesto vyhodenia výnimky. Volajúci (/rt handler) môže
    skontrolovať či DF nie je prázdny a zobraziť warning banner.

    Vracia okrem dát aj `fetch_errors` — dict {zdroj: chyba_msg} pre UI banner.
    """
    today = dt.date.today()
    now = dt.datetime.now()
    f0 = dt.datetime.combine(today, dt.time(0, 0))
    fetch_errors = {}
    # OTE DT (CZ denný trh) — nie kritické, pre SK trh sa nepoužíva
    try:
        isot = _fetch_ote_cached(today).copy()
        isot["ts"] = isot.interval.map(lambda iv: _iv_ts(today, iv))
    except Exception as _e:
        print(f"[/rt _rt_fetch] OTE DT fetch zlyhal: {_e}")
        fetch_errors["OTE DT (CZ)"] = str(_e)[:200]
        isot = pd.DataFrame(columns=["interval", "ts", "cena_EUR"])
    try:
        est = ds.fetch_ceps_est_price(f0, now).copy()
        est = est[est.interval.astype(str).str.contains("-")]
        est["ts"] = est.interval.map(lambda iv: _iv_ts(today, iv))
        est["est_eur"] = est.zco_est_CZK / FX_CZK
    except Exception as _e:
        fetch_errors["ČEPS ZCO odhad"] = str(_e)[:200]
        est = pd.DataFrame(columns=["ts", "est_eur"])
    try:
        sysd = ds.fetch_ceps_imbalance(f0, now)
    except Exception as _e:
        fetch_errors["ČEPS odchýlka"] = str(_e)[:200]
        sysd = pd.DataFrame(columns=["time", "sys_MW"])
    try:
        afrr = ds.fetch_ceps_re_price(f0, now)
    except Exception as _e:
        fetch_errors["ČEPS aFRR cena"] = str(_e)[:200]
        afrr = pd.DataFrame(columns=["time", "aFRR_EUR"])
    try:
        act = ds.fetch_ceps_activation(f0, now)
    except Exception as _e:
        fetch_errors["ČEPS aktivácia"] = str(_e)[:200]
        act = pd.DataFrame(columns=["time", "aFRR_plus", "aFRR_minus", "mFRR_plus", "mFRR_minus", "mFRR5"])
    try:
        vdt = ds.fetch_ote_intraday(today).copy()             # vnútrodenný trh (čerstvejšia cena DT)
        vdt["ts"] = vdt.interval.map(lambda iv: _iv_ts(today, iv))
    except Exception as _e:
        fetch_errors["OTE VDT (CZ)"] = str(_e)[:200]
        vdt = pd.DataFrame(columns=["ts", "cena_EUR"])
    return today, now, isot, est, sysd, afrr, act, vdt, fetch_errors


def _rt_live_frame(today, isot, sysd, afrr, act):
    """Zloží minútový live frame so stĺpcami, ktoré potrebuje rt_controller (prep/decide)."""
    def strip(df):
        if df is None or df.empty or "time" not in df:
            return pd.DataFrame(columns=["time"])
        x = df.copy(); t = pd.to_datetime(x["time"])
        try:
            if t.dt.tz is not None:
                t = t.dt.tz_localize(None)
        except (TypeError, AttributeError):
            pass
        x["time"] = t
        return x
    # Kontrolovať dostupnosť stĺpcov v already-stripped DataFrame-och (strip() vracia
    # iba ["time"] keď je vstup empty → check proti pôvodným sysd.columns by vyhodil KeyError).
    _S_raw = strip(sysd)
    S = _S_raw[["time"] + [c for c in ["sys_MW"] if c in _S_raw.columns]]
    A = strip(afrr).rename(columns={"aFRR_EUR": "aFRR_eur", "mFRRp_EUR": "mFRRp_eur", "mFRRm_EUR": "mFRRm_eur"})
    Acols = ["time"] + [c for c in ["aFRR_eur", "mFRRp_eur", "mFRRm_eur"] if c in A.columns]
    C = strip(act)
    Ccols = ["time"] + [c for c in ["aFRR_plus", "aFRR_minus", "mFRR_plus", "mFRR_minus", "mFRR5"] if c in C.columns]
    lf = S.merge(A[Acols], on="time", how="outer") if not A.empty else S
    lf = lf.merge(C[Ccols], on="time", how="outer") if not C.empty else lf
    if lf.empty or "time" not in lf:
        return lf
    lf = lf.sort_values("time")
    lf["ts15"] = pd.to_datetime(lf["time"]).dt.floor("15min")
    # CEPS živé dáta môžu vracať aFRR−/mFRR− so záporným znamienkom; rt_controller (a backtest)
    # pracuje s KLADNÝMI veľkosťami → znormalizuj na |x| (smer rozlišuje plus/minus stĺpec).
    for c in ["aFRR_plus", "aFRR_minus", "mFRR_plus", "mFRR_minus", "mFRR5"]:
        if c in lf.columns:
            lf[c] = pd.to_numeric(lf[c], errors="coerce").abs()
    # isot môže byť prázdny po OTE 503 (graceful fallback) — vtedy preskočiť merge.
    if isot is not None and not isot.empty and {"ts", "cena_EUR"}.issubset(isot.columns):
        iso = isot[["ts", "cena_EUR"]].rename(columns={"ts": "ts15", "cena_EUR": "isot_eur"})
        lf = lf.merge(iso, on="ts15", how="left")
    else:
        lf["isot_eur"] = float("nan")
    return lf
