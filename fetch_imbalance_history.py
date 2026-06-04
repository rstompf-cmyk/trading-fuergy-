# -*- coding: utf-8 -*-
"""
fetch_imbalance_history.py – stiahne históriu pre RT/odchýlkovú vrstvu a zanalyzuje
príležitosť: kedy je cena odchýlky (ZCO) vyššia ako denný trh (DT) a ako dobre
sa ZCO dá odhadnúť z minútových CEPS signálov.  python fetch_imbalance_history.py

Výstup: out/imbalance_history.csv (15-min: isot_eur, zco_eur, est_eur, aFRR_eur, sys_MW)
"""
from __future__ import annotations
import os, datetime as dt, time as _t
import numpy as np, pandas as pd
import data_sources as ds

FX = 24.3                          # EUR/CZK
START_DATE = dt.date(2026, 3, 1)   # od kedy sťahovať (uprav podľa potreby)
OUT = "out/imbalance_history.csv"
today = dt.date.today()


def iv_ts(date, interval):
    hh, mm = interval.split("-")[0].split(":")
    return pd.Timestamp(date) + pd.Timedelta(hours=int(hh), minutes=int(mm))


def min15(df, col, name):
    s = df.copy()
    s["time"] = pd.to_datetime(s["time"]).dt.tz_localize(None)   # zhoď tz (+02:00) -> lokálny čas
    return (s.set_index("time")[col].resample("15min").mean()
            .rename(name).reset_index().rename(columns={"time": "ts"}))


def min15_multi(df, cols):
    """Viac stĺpcov naraz: minútový rad → 15-min priemer (tz na lokálny)."""
    s = df.copy(); s["time"] = pd.to_datetime(s["time"]).dt.tz_localize(None)
    cols = [c for c in cols if c in s]
    r = s.set_index("time")[cols].resample("15min").mean().reset_index().rename(columns={"time": "ts"})
    return r


OUT_MIN = "out/imbalance_minute.csv"


def _strip_tz(df):
    x = df.copy(); t = pd.to_datetime(x["time"])
    try:
        if t.dt.tz is not None:
            t = t.dt.tz_localize(None)
    except (TypeError, AttributeError):
        pass
    x["time"] = t
    return x


def fetch_day(d):
    isot = ds.fetch_ote_dayahead(d)[["interval", "cena_EUR"]].rename(columns={"cena_EUR": "isot_eur"})
    isot["ts"] = isot.interval.map(lambda iv: iv_ts(d, iv))
    zco = ds.fetch_ote_imbalance(d)[["interval", "zco_CZK"]]
    zco["ts"] = zco.interval.map(lambda iv: iv_ts(d, iv)); zco["zco_eur"] = zco.zco_CZK / FX
    f0 = dt.datetime.combine(d, dt.time(0, 0)); f1 = dt.datetime.combine(d, dt.time(23, 59))
    re_raw = ds.fetch_ceps_re_price(f0, f1)
    act_raw = ds.fetch_ceps_activation(f0, f1)
    im_raw = ds.fetch_ceps_imbalance(f0, f1)
    est = ds.fetch_ceps_est_price(f0, f1)
    est["ts"] = est.interval.map(lambda iv: iv_ts(d, iv)); est["est_eur"] = est.zco_est_CZK / FX

    # ---------- 15-min frame ----------
    re = min15_multi(re_raw, ["aFRR_EUR", "mFRRp_EUR", "mFRRm_EUR", "mFRR5_EUR"]).rename(
        columns={"aFRR_EUR": "aFRR_eur", "mFRRp_EUR": "mFRRp_eur", "mFRRm_EUR": "mFRRm_eur", "mFRR5_EUR": "mFRR5_eur"})
    act = min15_multi(act_raw, ["aFRR_plus", "aFRR_minus", "mFRR_plus", "mFRR_minus", "mFRR5"])
    im = min15(im_raw, "sys_MW", "sys_MW")
    m15 = (isot[["ts", "isot_eur"]]
           .merge(zco[["ts", "zco_eur"]], on="ts", how="left")
           .merge(est[["ts", "est_eur"]], on="ts", how="left")
           .merge(re, on="ts", how="left").merge(act, on="ts", how="left").merge(im, on="ts", how="left"))
    m15["date"] = d

    # ---------- minútový frame (pre RT regulátor) ----------
    R = _strip_tz(re_raw).rename(columns={"aFRR_EUR": "aFRR_eur", "mFRRp_EUR": "mFRRp_eur",
                                          "mFRRm_EUR": "mFRRm_eur"})
    Rcols = ["time"] + [c for c in ["aFRR_eur", "mFRRp_eur", "mFRRm_eur"] if c in R]
    A = _strip_tz(act_raw)
    Acols = ["time"] + [c for c in ["aFRR_plus", "aFRR_minus", "mFRR_plus", "mFRR_minus", "mFRR5"] if c in A]
    I = _strip_tz(im_raw)[["time", "sys_MW"]]
    mn = (I.merge(R[Rcols], on="time", how="outer")
          .merge(A[Acols], on="time", how="outer").sort_values("time"))
    mn["ts15"] = mn["time"].dt.floor("15min")
    mn = (mn.merge(isot[["ts", "isot_eur"]].rename(columns={"ts": "ts15"}), on="ts15", how="left")
          .merge(zco[["ts", "zco_eur"]].rename(columns={"ts": "ts15"}), on="ts15", how="left"))
    mn["date"] = d
    return m15, mn


EXPECTED_COLS = ["ts", "isot_eur", "zco_eur", "est_eur", "aFRR_eur", "mFRRp_eur", "mFRRm_eur",
                 "aFRR_plus", "aFRR_minus", "mFRR_plus", "mFRR_minus", "mFRR5", "sys_MW"]


def main():
    end = today - dt.timedelta(days=1)
    all_days = [START_DATE + dt.timedelta(days=i) for i in range((end - START_DATE).days + 1)]
    have = set()
    if os.path.exists(OUT) and os.path.exists(OUT_MIN):
        ex = pd.read_csv(OUT, parse_dates=["ts"])
        if all(c in ex.columns for c in EXPECTED_COLS):
            have = set(pd.to_datetime(ex.ts).dt.date)
            print(f"Existujúci súbor: {len(have)} dní už stiahnutých (preskočím ich).")
        else:
            print("Existujúci súbor nemá nové stĺpce → sťahujem nanovo všetko.")
            os.remove(OUT)
    elif os.path.exists(OUT) and not os.path.exists(OUT_MIN):
        print("Chýba minútový súbor → sťahujem nanovo všetko (aj minútové dáta).")
        os.remove(OUT)
    todo = [d for d in all_days if d not in have]
    print(f"Sťahujem {len(todo)} z {len(all_days)} dní ({START_DATE} … {end})…  (môže trvať niekoľko minút)")
    done = 0
    for d in todo:
        try:
            part15, partmin = fetch_day(d)
            old = pd.read_csv(OUT, parse_dates=["ts"]) if os.path.exists(OUT) else None
            full = pd.concat([old, part15], ignore_index=True) if old is not None else part15
            full = full.drop_duplicates(subset=["ts"]).sort_values("ts")
            full = full.drop(columns=[c for c in ["hour"] if c in full.columns])
            full.to_csv(OUT, index=False)
            oldm = pd.read_csv(OUT_MIN, parse_dates=["time"]) if os.path.exists(OUT_MIN) else None
            fullm = pd.concat([oldm, partmin], ignore_index=True) if oldm is not None else partmin
            fullm = fullm.drop_duplicates(subset=["time"]).sort_values("time")
            fullm.to_csv(OUT_MIN, index=False)
            done += 1; print(f"  ✓ {d}   ({done}/{len(todo)})")
        except Exception as e:
            print(f"  ✗ {d}: {str(e)[:60]}")
        _t.sleep(0.2)
    if not os.path.exists(OUT):
        print("Nestiahlo sa nič."); return

    df = pd.read_csv(OUT, parse_dates=["ts"])
    df["hour"] = df.ts.dt.hour
    d = df.dropna(subset=["zco_eur", "isot_eur"])
    print("\n" + "="*60)
    print(f"ANALÝZA ODCHÝLKY  ({len(d)} periód, {d.ts.dt.date.nunique()} dní)")
    print("="*60)
    print("Predpovedateľnosť ZCO (korelácie):")
    for c, lab in [("aFRR_eur", "aFRR cena"), ("est_eur", "odhad CEPS"), ("sys_MW", "sys. odchýlka")]:
        dd = d.dropna(subset=[c])
        if len(dd) > 10:
            print(f"   ZCO ~ {lab:14s}: {dd.zco_eur.corr(dd[c]):+.3f}  (n={len(dd)})")
    spread = d.zco_eur - d.isot_eur
    print(f"\nZCO vs DT (celkovo):  ZCO priemer {d.zco_eur.mean():.1f} €  "
          f"DT {d.isot_eur.mean():.1f} €  | ZCO>DT v {(spread > 0).mean():.0%} periód")
    print(f"Hotovo. Dáta v {OUT}  ({df.ts.dt.date.nunique()} dní).")
    print("="*60)


if __name__ == "__main__":
    main()
