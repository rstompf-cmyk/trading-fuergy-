# -*- coding: utf-8 -*-
"""
settlement.py — Market-aware abstrakcia pre real-price settlement.

Pre minulé/dokončené dni potrebujeme **reálne clearing ceny** (DT) a
**reálne zúčtovacie ceny odchýlky** (ZCO) na prepočet profitu — namiesto
predikcie ktorá sa použila pri vstupe do trhu.

Pre CZ:
  - DT real: out/price_train_2026.csv (hodinové isot_eur)
  - ZCO real: imbalance_minute.csv (per-minute z ČEPS, riešený upstream)

Pre SK:
  - DT real: out/sk/historian_C_OKTE_ISOT_15m_final.csv (cez seps_sk.load_okte_dt_for_day)
  - ZCO real: out/sk/historian_I_WEB_OKTE_ZCO_15m.csv (cez seps_sk.load_okte_zco_for_day)

Volajúce moduly: livesim.py, combined_backtest.py.

Funkcie:
  get_dt_real_hourly(date_iso, market=None) -> np.ndarray|None
     24-element array hodinových reálnych DT clearing cien (€/MWh).
     None ak dáta nie sú dostupné pre daný deň/trh.

  get_zco_for_day(date_iso, market=None) -> Dict[str, float]
     Dict {ts_iso: zco_eur_mwh} pre 96 × 15-min slotov dňa.
     Prázdny dict ak dáta nie sú dostupné.

Backward compatibility:
  market=None → použije market.get_active_market() (zachová existing behavior).
"""
from __future__ import annotations
import os
from typing import Dict, Optional
import numpy as np
import pandas as pd


_CZ_PRICE_CSV = "out/price_train_2026.csv"
_PRICE_CACHE = {"mtime": None, "by_date": None}


def _active_market(market: Optional[str]) -> str:
    """Resolve aktívny trh — argument, alebo z market.py."""
    if market:
        return market.lower()
    try:
        import market as _mk
        return _mk.get_active_market()
    except Exception:
        return "cz"


# ---------------------------------------------------------------------------
# DT real (clearing) hourly array
# ---------------------------------------------------------------------------

def _dt_real_cz(date_iso: str) -> Optional[np.ndarray]:
    """CZ: čítaj 24 hodinových cien z price_train_2026.csv (isot_eur)."""
    try:
        mt = os.path.getmtime(_CZ_PRICE_CSV)
    except OSError:
        return None
    if _PRICE_CACHE["mtime"] != mt or _PRICE_CACHE["by_date"] is None:
        try:
            df = pd.read_csv(_CZ_PRICE_CSV, parse_dates=["time"])
        except Exception:
            return None
        df["date_iso"] = df["time"].dt.date.astype(str)
        by_date = {}
        for diso, g in df.groupby("date_iso"):
            g = g.sort_values("time")
            if len(g) == 24:
                by_date[diso] = g["isot_eur"].values.astype(float)
        _PRICE_CACHE["mtime"] = mt
        _PRICE_CACHE["by_date"] = by_date
    return _PRICE_CACHE["by_date"].get(str(date_iso))


def _dt_real_sk(date_iso: str) -> Optional[np.ndarray]:
    """SK: čítaj 15-min DT clearing zo seps_sk historian, agreguj na 24h priemer."""
    try:
        import seps_sk as _seps
    except Exception:
        return None
    try:
        dt_map = _seps.load_okte_dt_for_day(date_iso) or {}
    except Exception:
        return None
    if not dt_map:
        return None
    # dt_map: dict { "YYYY-MM-DD HH:MM:SS": eur_mwh } — 96 slotov 15-min
    hourly_buckets = [[] for _ in range(24)]
    for ts_str, val in dt_map.items():
        try:
            s = str(ts_str)
            h = int(s[11:13])
            if 0 <= h < 24 and val is not None:
                hourly_buckets[h].append(float(val))
        except Exception:
            continue
    arr = np.full(24, np.nan, dtype=float)
    for h in range(24):
        if hourly_buckets[h]:
            arr[h] = float(np.mean(hourly_buckets[h]))
    # Skontroluj že máme aspoň 75% hodín — inak vráť None (neúplné dáta)
    if np.isfinite(arr).sum() < 18:
        return None
    return arr


def get_dt_real_hourly(date_iso: str,
                        market: Optional[str] = None) -> Optional[np.ndarray]:
    """Vráti 24-prvkový rad reálnych hodinových DT clearing cien (€/MWh).

    Returns None ak dáta nie sú dostupné pre daný deň/trh.
    """
    mk = _active_market(market)
    if mk == "sk":
        return _dt_real_sk(date_iso)
    # CZ default
    return _dt_real_cz(date_iso)


# ---------------------------------------------------------------------------
# DT real (clearing) 15-min array (pre 15-min livesim mode)
# ---------------------------------------------------------------------------

def get_dt_real_quarterly(date_iso: str,
                           market: Optional[str] = None) -> Optional[np.ndarray]:
    """Vráti 96-prvkový rad reálnych 15-min DT clearing cien (€/MWh).

    Pre CZ: rozšíri 24h ceny na 4× 15-min (rovnaká cena vo všetkých quarter-och hodiny).
    Pre SK: priamo z 15-min historian (autentickejšie).
    Returns None ak dáta nie sú dostupné.
    """
    mk = _active_market(market)
    if mk == "sk":
        try:
            import seps_sk as _seps
            dt_map = _seps.load_okte_dt_for_day(date_iso) or {}
        except Exception:
            return None
        if not dt_map:
            return None
        arr = np.full(96, np.nan, dtype=float)
        for ts_str, val in dt_map.items():
            try:
                s = str(ts_str)
                h = int(s[11:13])
                m = int(s[14:16])
                idx = (h * 60 + m) // 15
                if 0 <= idx < 96 and val is not None:
                    arr[idx] = float(val)
            except Exception:
                continue
        if np.isfinite(arr).sum() < 72:   # <75% slotov → None
            return None
        return arr
    # CZ — upsample z hodinového
    h24 = _dt_real_cz(date_iso)
    if h24 is None:
        return None
    return np.repeat(h24, 4)


# ---------------------------------------------------------------------------
# ZCO real per day
# ---------------------------------------------------------------------------

def get_zco_for_day(date_iso: str,
                     market: Optional[str] = None) -> Dict[str, float]:
    """Vráti dict {ts_iso: zco_eur_mwh} pre 15-min sloty dňa.

    Pre CZ: aktuálne nie je samostatný source (ZCO sa rieši cez imbalance_minute
            ktorý už načítava livesim.advance() upstream).
    Pre SK: zo seps_sk.load_okte_zco_for_day historian.
    """
    mk = _active_market(market)
    if mk == "sk":
        try:
            import seps_sk as _seps
            return _seps.load_okte_zco_for_day(date_iso) or {}
        except Exception:
            return {}
    # CZ — pre teraz nemáme samostatný settlement source (rieši to imbalance_minute upstream)
    return {}


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import datetime as _dt
    print("settlement.py smoke test")
    yest = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    print(f"\nVčera ({yest}):")
    for mk in ("cz", "sk"):
        h = get_dt_real_hourly(yest, market=mk)
        q = get_dt_real_quarterly(yest, market=mk)
        z = get_zco_for_day(yest, market=mk)
        if h is not None:
            print(f"  {mk.upper()}: DT hourly = {len(h)} hodín, "
                  f"priemer {np.nanmean(h):.2f} €/MWh, "
                  f"min={np.nanmin(h):.2f}, max={np.nanmax(h):.2f}")
        else:
            print(f"  {mk.upper()}: DT hourly = N/A")
        if q is not None:
            print(f"  {mk.upper()}: DT quarterly = {len(q)} slotov")
        if z:
            zvals = list(z.values())
            print(f"  {mk.upper()}: ZCO = {len(z)} slotov, "
                  f"priemer {np.mean(zvals):.2f}, "
                  f"min={min(zvals):.2f}, max={max(zvals):.2f}")
        else:
            print(f"  {mk.upper()}: ZCO = N/A")
