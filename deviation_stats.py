"""
deviation_stats.py — Štatistika a odhad systematickej ODCHÝLKY podľa hodiny dňa, typu dňa a FTV.

Myšlienka: odchýlka (a jej cena ZCO voči dennému trhu DT) nie je čisto náhodná — má denný profil
a závisí od počasia/FTV (slnečno vs zamračené) aj typu dňa (pracovný/víkend). Tento modul:
  1) z histórie (out/imbalance_minute.csv [+ FTV výroba]) postaví PROFIL očakávanej odchýlky,
  2) pre cieľový deň (podľa FTV predikcie a typu dňa) ODHADNE očakávanú odchýlku po periódach,
  3) tento odhad sa dá použiť ako KOREKCIA pri tvorbe D-1 plánu (voliteľne, s výpisom úpravy).

Kľúčová veličina:
  zco_dt = ZCO − DT  [€/MWh]   … "incentív odchýlky"
     > 0  → systém je krátky (chýba energia) → oplatí sa DODAŤ viac než nominácia (vybíjať/exportovať)
     < 0  → systém je dlhý (prebytok)        → oplatí sa ODOBRAŤ (nabíjať/menej exportovať)
  sys_MW … samotná systémová odchýlka (orientačne; znamienko podľa konvencie CEPS).

Profil je čisto z MINULOSTI (žiadny look-ahead) a odhad používa len FTV predikciu známu D-1 → kauzálne.
"""
from __future__ import annotations
import os, json
import numpy as np
import pandas as pd

PROFILE_PATH = "out/deviation_profile.json"
PROFILE_PATH_SK = "out/sk/deviation_profile.json"
MIN_CELL_N = 200          # min. počet minút v bunke, inak fallback na hodinový priemer
MIN_CELL_N_SK = 8         # SK pracuje s 15-min agregátmi (96/deň namiesto 1440 minút) → nižší prah


# ───────────────────────── PV (FTV) denné súčty pre rozdelenie slnečno/zamračené ─────────────────────────
def pv_daily_from_train(price_csv="out/price_train_2026.csv"):
    """Denný súčet FTV výroby [kWh] z trénovacieho CSV (stĺpec 'kw' = hodinový výkon). Len na Macu."""
    try:
        df = pd.read_csv(price_csv, parse_dates=["time"])
    except Exception:
        return None
    if "kw" not in df.columns:
        return None
    df["date"] = df["time"].dt.date
    g = df.groupby("date")["kw"].sum()          # hodinové kW → kWh za deň
    return {d: float(v) for d, v in g.items()}


# ───────────────────────── stavba profilu ─────────────────────────
def build_profile(imb_csv="out/imbalance_minute.csv", pv_daily=None,
                  by_pv=True, by_weekday=True):
    """Postaví profil očakávanej odchýlky podľa hodiny [× PV-bucket × typ dňa].
    pv_daily: {date: pv_kwh} na rozdelenie slnečno/zamračené (medián). None → bez PV-splitu."""
    df = pd.read_csv(imb_csv, parse_dates=["time", "ts15"])
    df = df.dropna(subset=["zco_eur", "isot_eur"]).copy()
    if df.empty:
        raise ValueError("Žiadne dáta s ZCO aj DT v " + imb_csv)
    df["date"] = df["time"].dt.date
    df["hour"] = df["time"].dt.hour
    df["zco_dt"] = df["zco_eur"] - df["isot_eur"]

    pv_threshold = None
    if by_pv and pv_daily:
        vals = [v for v in pv_daily.values() if np.isfinite(v)]
        pv_threshold = float(np.median(vals)) if vals else None
    if pv_threshold is not None:
        df["pv_bucket"] = df["date"].map(
            lambda d: "slnečno" if pv_daily.get(d, pv_threshold) >= pv_threshold else "zamračené")
    else:
        df["pv_bucket"] = "vše"

    if by_weekday:
        df["dtype"] = df["time"].dt.dayofweek.map(lambda x: "víkend" if x >= 5 else "pracovný")
    else:
        df["dtype"] = "vše"

    def _agg(g):
        return pd.Series(dict(zco_dt_mean=g["zco_dt"].mean(), zco_dt_med=g["zco_dt"].median(),
                              zco_mean=g["zco_eur"].mean(), dt_mean=g["isot_eur"].mean(),
                              sys_mw_mean=g["sys_MW"].mean(), n=len(g)))

    cells = df.groupby(["hour", "pv_bucket", "dtype"]).apply(_agg).reset_index()
    hour_only = df.groupby("hour").apply(_agg).reset_index()   # fallback

    prof = {
        "by_pv": bool(pv_threshold is not None),
        "by_weekday": bool(by_weekday),
        "pv_threshold_kwh": pv_threshold,
        "n_days": int(df["date"].nunique()),
        "span": [str(df["date"].min()), str(df["date"].max())],
        "cells": cells.to_dict(orient="records"),
        "hour_only": hour_only.to_dict(orient="records"),
    }
    return prof


def build_profile_sk(zco_csv: str = "out/sk/historian_I_WEB_OKTE_ZCO_15m.csv",
                       dt_csv: str = "out/sk/historian_C_OKTE_ISOT_15m_final.csv",
                       sys_mw_csv: str = None,
                       pv_daily=None, by_pv: bool = True, by_weekday: bool = True,
                       date_from=None, date_to=None):
    """SK variant build_profile — z OKTE 15-min historian CSV (ZCO + DT clearing)
    postaví profil očakávanej odchýlky podľa hodiny × PV-bucket × typu dňa.

    Vstupy:
      zco_csv: historian ZCO ceny per 15-min (stĺpce: time_utc, value)
      dt_csv:  historian DT/DAM ceny per 15-min (rovnaký formát)
      sys_mw_csv: voliteľné — SEPS sys_MW per minútu (pre štatistiku)
      pv_daily: {date → kWh/deň} pre PV-bucket split (None → bez PV split)
      by_weekday: True → rozdeli sa na pracovný/víkend
      date_from/date_to: voliteľný filter (datetime.date alebo string YYYY-MM-DD)

    Vracia rovnaký dict ako build_profile() — kompatibilný s estimate_day().
    """
    if not os.path.exists(zco_csv):
        raise FileNotFoundError(f"ZCO historian CSV neexistuje: {zco_csv}")
    if not os.path.exists(dt_csv):
        raise FileNotFoundError(f"DT historian CSV neexistuje: {dt_csv}")

    df_zco = pd.read_csv(zco_csv, usecols=["time_utc", "value"]).rename(columns={"value": "zco_eur"})
    df_dt = pd.read_csv(dt_csv, usecols=["time_utc", "value"]).rename(columns={"value": "isot_eur"})
    df_zco["time_utc"] = pd.to_datetime(df_zco["time_utc"], utc=True)
    df_dt["time_utc"] = pd.to_datetime(df_dt["time_utc"], utc=True)

    # JOIN na 15-min slot (najbližší match)
    df = pd.merge_asof(
        df_zco.sort_values("time_utc"),
        df_dt.sort_values("time_utc"),
        on="time_utc", direction="nearest",
        tolerance=pd.Timedelta(minutes=7))
    df = df.dropna(subset=["zco_eur", "isot_eur"]).copy()
    if df.empty:
        raise ValueError(f"Žiadne sloty s ZCO + DT v {zco_csv}/{dt_csv}")

    # Konvertuj na lokálny čas pre hodinu/dátum
    df["time"] = df["time_utc"].dt.tz_convert("Europe/Bratislava").dt.tz_localize(None)
    df["date"] = df["time"].dt.date
    df["hour"] = df["time"].dt.hour
    df["zco_dt"] = df["zco_eur"] - df["isot_eur"]

    # Voliteľný date filter
    if date_from is not None:
        d_from = pd.Timestamp(date_from).date() if not hasattr(date_from, "year") else date_from
        df = df[df["date"] >= d_from]
    if date_to is not None:
        d_to = pd.Timestamp(date_to).date() if not hasattr(date_to, "year") else date_to
        df = df[df["date"] <= d_to]
    if df.empty:
        raise ValueError("Žiadne dni v zadanom rozsahu")

    # sys_MW — voliteľne z SEPS minute history (priemer cez 15-min slot)
    df["sys_MW"] = float("nan")
    if sys_mw_csv and os.path.exists(sys_mw_csv):
        try:
            df_sys = pd.read_csv(sys_mw_csv, usecols=["time", "sys_MW"]).copy()
            df_sys["time"] = pd.to_datetime(df_sys["time"])
            df_sys["ts15"] = df_sys["time"].dt.floor("15min")
            sys_15 = df_sys.groupby("ts15")["sys_MW"].mean()
            df["ts15"] = df["time"].dt.floor("15min")
            df = df.merge(sys_15.reset_index(), on="ts15", how="left", suffixes=("", "_sys"))
            if "sys_MW_sys" in df.columns:
                df["sys_MW"] = df["sys_MW_sys"]
                df = df.drop(columns=["sys_MW_sys"])
        except Exception as e:
            print(f"[build_profile_sk] sys_MW load chyba: {e}")

    # PV-bucket
    pv_threshold = None
    if by_pv and pv_daily:
        vals = [v for v in pv_daily.values() if np.isfinite(v)]
        pv_threshold = float(np.median(vals)) if vals else None
    if pv_threshold is not None:
        df["pv_bucket"] = df["date"].map(
            lambda d: "slnečno" if pv_daily.get(d, pv_threshold) >= pv_threshold else "zamračené")
    else:
        df["pv_bucket"] = "vše"

    # Weekday split
    if by_weekday:
        df["dtype"] = df["time"].dt.dayofweek.map(lambda x: "víkend" if x >= 5 else "pracovný")
    else:
        df["dtype"] = "vše"

    def _agg(g):
        return pd.Series(dict(
            zco_dt_mean=float(g["zco_dt"].mean()),
            zco_dt_med=float(g["zco_dt"].median()),
            zco_mean=float(g["zco_eur"].mean()),
            dt_mean=float(g["isot_eur"].mean()),
            sys_mw_mean=float(g["sys_MW"].mean()) if g["sys_MW"].notna().any() else 0.0,
            n=int(len(g))))

    cells = df.groupby(["hour", "pv_bucket", "dtype"]).apply(_agg, include_groups=False).reset_index()
    hour_only = df.groupby("hour").apply(_agg, include_groups=False).reset_index()

    prof = {
        "by_pv": bool(pv_threshold is not None),
        "by_weekday": bool(by_weekday),
        "pv_threshold_kwh": pv_threshold,
        "n_days": int(df["date"].nunique()),
        "span": [str(df["date"].min()), str(df["date"].max())],
        "market": "sk",
        "cells": cells.to_dict(orient="records"),
        "hour_only": hour_only.to_dict(orient="records"),
        "min_cell_n": MIN_CELL_N_SK,
    }
    return prof


def save_profile(prof, path=PROFILE_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(prof, f, ensure_ascii=False, indent=1, default=float)
    return path


def load_profile(path=PROFILE_PATH):
    with open(path) as f:
        return json.load(f)


# ───────────────────────── odhad pre cieľový deň ─────────────────────────
def _hour_only_map(prof, key="zco_dt_mean"):
    return {int(r["hour"]): float(r[key]) for r in prof["hour_only"]}


def estimate_day(prof, date, pv_forecast_kwh=None, step_min=15, key="zco_dt_mean"):
    """Vráti pole očakávanej odchýlky (zco_dt €/MWh) po periódach pre daný deň.
    Vyberie bunku podľa hodiny + (PV-bucket z predikcie) + (typ dňa); fallback na hodinový priemer."""
    date = pd.Timestamp(date)
    bucket = "vše"
    if prof.get("by_pv") and pv_forecast_kwh is not None and prof.get("pv_threshold_kwh") is not None:
        bucket = "slnečno" if pv_forecast_kwh >= prof["pv_threshold_kwh"] else "zamračené"
    dtype = "vše"
    if prof.get("by_weekday"):
        dtype = "víkend" if date.dayofweek >= 5 else "pracovný"

    # index buniek
    cell = {}
    for r in prof["cells"]:
        cell[(int(r["hour"]), r["pv_bucket"], r["dtype"])] = r
    hmap = _hour_only_map(prof, key)

    # min cell n — môže byť per-profil (SK má 15-min vzorky, nižší prah)
    min_n = int(prof.get("min_cell_n", MIN_CELL_N))

    n = int(24*60/step_min)
    out = np.zeros(n)
    for p in range(n):
        hour = int((p*step_min)//60)
        r = cell.get((hour, bucket, dtype))
        if r is not None and r.get("n", 0) >= min_n:
            out[p] = float(r[key])
        else:
            out[p] = hmap.get(hour, 0.0)        # fallback: hodinový priemer
    return out, bucket, dtype


# ───────────────────────── čitateľný report ─────────────────────────
def report(prof):
    lines = []
    sp = prof.get("span", ["?", "?"])
    lines.append(f"PROFIL ODCHÝLKY — {prof['n_days']} dní ({sp[0]} … {sp[1]}), "
                 f"PV-split={'áno' if prof['by_pv'] else 'nie'}"
                 + (f" (prah {prof['pv_threshold_kwh']:.0f} kWh/deň)" if prof['by_pv'] else "")
                 + f", typ dňa={'áno' if prof['by_weekday'] else 'nie'}")
    lines.append("="*72)
    lines.append("Hodinový priemer (cez všetky dni):  zco_dt = ZCO − DT [€/MWh],  sys [MW]")
    lines.append(f"{'hod':>3} {'zco_dt':>9} {'ZCO':>8} {'DT':>8} {'sys MW':>9} {'smer':>14} {'min':>8}")
    for r in sorted(prof["hour_only"], key=lambda x: x["hour"]):
        z = r["zco_dt_mean"]
        smer = "DODAŤ (vybi)" if z > 1 else ("ODOBRAŤ (nabi)" if z < -1 else "~neutrál")
        lines.append(f"{int(r['hour']):>3} {z:>9.1f} {r['zco_mean']:>8.1f} {r['dt_mean']:>8.1f} "
                     f"{r['sys_mw_mean']:>9.0f} {smer:>14} {int(r['n']):>8}")
    if prof["by_pv"]:
        lines.append("-"*72)
        lines.append("Rozdiel slnečno − zamračené (zco_dt €/MWh) po hodinách:")
        c = {}
        for r in prof["cells"]:
            c.setdefault(int(r["hour"]), {})[r["pv_bucket"]] = r
        row = []
        for h in range(24):
            s = c.get(h, {}).get("slnečno", {}).get("zco_dt_mean")
            z = c.get(h, {}).get("zamračené", {}).get("zco_dt_mean")
            if s is not None and z is not None:
                row.append(f"h{h:02d}:{s-z:+.0f}")
        lines.append("  " + "  ".join(row))
    return "\n".join(lines)


# ───────────────────────── použitie v pláne: korekcia cien + výpis úpravy ─────────────────────────
def biased_price(dt_price, expected_zco_dt, weight):
    """Cena pre ROZHODOVANIE plánu = DT + weight·očakávaná_odchýlka. Zúčtovanie ostáva na reálnej DT!
    weight≈0 (vyp) … 1 (plne dôveruj odhadu). Odporúčané malé (0.2–0.5), overiť backtestom."""
    return np.asarray(dt_price, float) + float(weight)*np.asarray(expected_zco_dt, float)


def plan_correction(optimize_fn, pv, dt_price, expected_zco_dt, weight, **kw):
    """Spustí plán BEZ biasu a S biasom, vráti (sch_bias, sch_base, korekcia_kw_po_periodach).
    korekcia = batt_kw(s biasom) − batt_kw(bez biasu) … o koľko odhad odchýlky posunul plán v perióde.
    Zúčtovanie sa robí inde na REÁLNEJ DT cene (bias mení len rozhodnutie, nie príjem)."""
    sch_base, _ = optimize_fn(pv, np.asarray(dt_price, float), **kw)
    adj = biased_price(dt_price, expected_zco_dt, weight)
    sch_bias, _ = optimize_fn(pv, adj, **kw)
    corr = np.asarray(sch_bias["batt_kw"].values, float) - np.asarray(sch_base["batt_kw"].values, float)
    return sch_bias, sch_base, corr


if __name__ == "__main__":
    pv = pv_daily_from_train()
    if pv:
        print(f"FTV denné súčty z price_train: {len(pv)} dní (PV-split zapnutý).")
    else:
        print("price_train_2026.csv nedostupný → profil bez PV-splitu (len hodinový + typ dňa).")
    prof = build_profile(pv_daily=pv, by_pv=bool(pv), by_weekday=True)
    save_profile(prof)
    print(report(prof))
    print(f"\nUložené do {PROFILE_PATH}. Použiteľné v pláne ako korekcia (zco_bias).")
