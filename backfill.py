# -*- coding: utf-8 -*-
"""
backfill.py – doplní LEN CHÝBAJÚCE dni v historických dátach (idempotentné):
  • out/price_train_2026.csv   – DT/ISOT cena (hodinová, tréning modelu) + počasie
  • out/imbalance_history.csv  – odchýlka 15-min (DT, ZCO, aktivácie, sys odchýlka)
  • out/imbalance_minute.csv   – minútové (sys, aktivácie, ceny) pre RT regulátor

Použitie:
  python backfill.py            # doplní všetko chýbajúce (vhodné aj do cronu)
Volá sa aj z appky (tlačidlo „Doplniť chýbajúce dáta") a automaticky pri štarte appky.
Používa rovnaké fetchery a formát ako fetch_history.py / fetch_imbalance_history.py,
takže súbory zostávajú konzistentné (žiadne rozídenie so simuláciou/backtestom).
"""
from __future__ import annotations
import os
import datetime as dt
import time as _t
import pandas as pd
import data_sources as ds
import fetch_imbalance_history as fih   # bezpečný import (kód beží len pod __main__)

PRICE_CSV = "out/price_train_2026.csv"
PRICE_START = dt.date(2026, 1, 1)
LAT, LON, KWP, TILT, AZ, EFF = 49.5961, 17.3634, 99.0, 30.0, 0.0, 0.85


def _existing_dates(csv, col):
    if not os.path.exists(csv):
        return set()
    try:
        df = pd.read_csv(csv, parse_dates=[col])
        return set(pd.to_datetime(df[col]).dt.date)
    except Exception:
        return set()


def _days(start, end):
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


# ---------------- DT / cena (tréning modelu) ----------------
def backfill_prices(end=None, log=print):
    end = end or dt.date.today()
    days = _days(PRICE_START, end)
    have = _existing_dates(PRICE_CSV, "time")
    todo = [d for d in days if d not in have]
    if not todo:
        return dict(dataset="DT cena (tréning)", total=len(days), missing=0, added=0)
    log(f"DT cena: dopĺňam {len(todo)} chýbajúcich dní…")
    rows, ok = [], []
    for d in todo:
        try:
            rows.append(ds.fetch_ote_dayahead(d)); ok.append(d)
        except Exception as e:
            log(f"  ✗ DT {d}: {str(e)[:50]}")
        _t.sleep(0.25)
    if not rows:
        return dict(dataset="DT cena (tréning)", total=len(days), missing=len(todo), added=0)
    isot = pd.concat(rows, ignore_index=True)

    def to_ts(r):
        hh, mm = r["interval"].split("-")[0].split(":")
        return pd.Timestamp(r["date"]) + pd.Timedelta(hours=int(hh), minutes=int(mm))
    isot["time"] = isot.apply(to_ts, axis=1)
    isot_h = (isot.set_index("time")["cena_EUR"].resample("h").mean()
              .rename("isot_eur").reset_index())
    wx = ds.fetch_pv_forecast(LAT, LON, KWP, TILT, AZ, EFF, start=min(ok), end=end)
    wx["time"] = wx["time"].dt.floor("h")
    df = isot_h.merge(wx[["time", "gti", "temp", "cloud", "kw"]], on="time", how="inner").dropna(subset=["isot_eur"])
    df["hour"] = df.time.dt.hour; df["dow"] = df.time.dt.dayofweek; df["month"] = df.time.dt.month
    df = df[["time", "isot_eur", "gti", "temp", "cloud", "kw", "hour", "dow", "month"]]
    old = pd.read_csv(PRICE_CSV, parse_dates=["time"]) if os.path.exists(PRICE_CSV) else None
    full = pd.concat([old, df], ignore_index=True) if old is not None else df
    full = full.drop_duplicates(subset=["time"]).sort_values("time")
    os.makedirs("out", exist_ok=True)
    full.to_csv(PRICE_CSV, index=False)
    log(f"  ✓ DT cena: pridaných {len(ok)} dní")
    return dict(dataset="DT cena (tréning)", total=len(days), missing=len(todo), added=len(ok))


# ---------------- odchýlka 15-min + minútové ----------------
def backfill_imbalance(end=None, log=print):
    end = end or (dt.date.today() - dt.timedelta(days=1))   # ZCO je settled až spätne
    start = fih.START_DATE
    days = _days(start, end)
    have = set()
    if os.path.exists(fih.OUT) and os.path.exists(fih.OUT_MIN):
        try:
            ex = pd.read_csv(fih.OUT, parse_dates=["ts"])
            if all(c in ex.columns for c in fih.EXPECTED_COLS):
                have = set(pd.to_datetime(ex.ts).dt.date)
        except Exception:
            have = set()
    todo = [d for d in days if d not in have]
    if not todo:
        return dict(dataset="odchýlka (15-min + minúty)", total=len(days), missing=0, added=0)
    log(f"Odchýlka: dopĺňam {len(todo)} chýbajúcich dní…")
    added = 0
    # Helper: normalizuj tz na naive (CSV konvencia). Mix tz-aware + tz-naive
    # spadne v sort_values("ts") s "'<' not supported between instances of 'Timestamp'".
    def _strip_tz_col(df, col):
        if df is None or col not in df.columns:
            return df
        try:
            s = pd.to_datetime(df[col], errors="coerce")
            if hasattr(s.dt, "tz") and s.dt.tz is not None:
                s = s.dt.tz_localize(None)
            df = df.copy()
            df[col] = s
        except (TypeError, AttributeError, ValueError):
            pass
        return df
    for d in todo:
        try:
            p15, pmin = fih.fetch_day(d)
            # Normalizuj TZ na strane p15/pmin (fresh fetch) aj old (z disku).
            p15 = _strip_tz_col(p15, "ts")
            pmin = _strip_tz_col(pmin, "time")
            old = pd.read_csv(fih.OUT, parse_dates=["ts"]) if os.path.exists(fih.OUT) else None
            old = _strip_tz_col(old, "ts")
            full = pd.concat([old, p15], ignore_index=True) if old is not None else p15
            full = _strip_tz_col(full, "ts")
            full = full.drop_duplicates(subset=["ts"]).sort_values("ts")
            full.to_csv(fih.OUT, index=False)
            oldm = pd.read_csv(fih.OUT_MIN, parse_dates=["time"]) if os.path.exists(fih.OUT_MIN) else None
            oldm = _strip_tz_col(oldm, "time")
            fm = pd.concat([oldm, pmin], ignore_index=True) if oldm is not None else pmin
            fm = _strip_tz_col(fm, "time")
            fm = fm.drop_duplicates(subset=["time"]).sort_values("time")
            fm.to_csv(fih.OUT_MIN, index=False)
            added += 1; log(f"  ✓ odchýlka {d}  ({added}/{len(todo)})")
        except Exception as e:
            # Rozšírený traceback iba pre prvý fail (aby log nepretiekol pri 97 chybách).
            import traceback as _tb
            _msg = f"{type(e).__name__}: {str(e)[:200]}"
            if added == 0 and d == todo[0]:
                log(f"  ✗ odchýlka {d}: {_msg}")
                log(f"     TRACEBACK:\n{_tb.format_exc()[-1500:]}")
            else:
                log(f"  ✗ odchýlka {d}: {_msg[:80]}")
        _t.sleep(0.2)
    return dict(dataset="odchýlka (15-min + minúty)", total=len(days), missing=len(todo), added=added)


def coverage():
    """Prehľad pokrytia každého súboru: počet dní, od–do, riadkov."""
    rep = []
    for label, csv, col in [("DT cena (tréning, hod.)", PRICE_CSV, "time"),
                            ("Odchýlka 15-min", fih.OUT, "ts"),
                            ("Odchýlka minútové", fih.OUT_MIN, "time")]:
        if os.path.exists(csv):
            try:
                df = pd.read_csv(csv, parse_dates=[col]); dd = pd.to_datetime(df[col]).dt.date
                rep.append(dict(name=label, days=int(dd.nunique()), first=str(dd.min()),
                                last=str(dd.max()), rows=len(df), exists=True))
            except Exception:
                rep.append(dict(name=label, days=0, first="?", last="?", rows=0, exists=True))
        else:
            rep.append(dict(name=label, days=0, first="—", last="—", rows=0, exists=False))
    return rep


def backfill_all(end=None, log=print):
    log("Dopĺňam chýbajúce historické dáta…")
    r1 = backfill_prices(end=end, log=log)
    r2 = backfill_imbalance(end=end, log=log)
    log("Hotovo.")
    return [r1, r2]


if __name__ == "__main__":
    for r in backfill_all():
        print("  →", r)
    print("\nPokrytie:")
    for c in coverage():
        print(f"  {c['name']:26s}: {c['days']:3d} dní  {c['first']} … {c['last']}  ({c['rows']} riadkov)")
