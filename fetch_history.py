# -*- coding: utf-8 -*-
"""
fetch_history.py – stiahne históriu ISOT z OTE od 1.1.2026 + počasie z Open-Meteo,
spáruje ich do tréningovej sady pre cenový model.  python fetch_history.py
Výstupy:  out/isot_2026.csv  (surové 15-min ISOT)
          out/price_train_2026.csv  (hodinové: isot_eur + gti, temp, cloud, hour, dow, month)
"""
import datetime as dt, time, os
import pandas as pd
import data_sources as ds

# poloha FTV (kvôli slnku/teplote pre cenový model)
LAT, LON, KWP, TILT, AZ, EFF = 49.5961, 17.3634, 99.0, 30.0, 0.0, 0.85
START = dt.date(2026, 1, 1)
today = dt.date.today()
os.makedirs("out", exist_ok=True)

# ---------- 1) ISOT z OTE deň po dni ----------
print(f"Sťahujem ISOT z OTE {START} … {today}  (chvíľu to potrvá)")
rows, fails = [], []
d, i, n = START, 0, (today - START).days + 1
while d <= today:
    i += 1
    try:
        rows.append(ds.fetch_ote_dayahead(d))
    except Exception as e:
        fails.append((d.isoformat(), str(e)[:50]))
    if i % 20 == 0:
        print(f"   ...{i}/{n} dní")
    time.sleep(0.25)            # slušnosť voči serveru
    d += dt.timedelta(days=1)

isot = pd.concat(rows, ignore_index=True)
isot.to_csv("out/isot_2026.csv", index=False)
print(f"ISOT hotovo: {len(isot)} riadkov, {isot['date'].nunique()} dní, zlyhalo {len(fails)} dní")
if fails:
    print("   zlyhané dni (prvých 5):", fails[:5])

# ---------- 2) ISOT -> hodinový priemer ----------
def to_ts(r):
    hh, mm = r["interval"].split("-")[0].split(":")
    return pd.Timestamp(r["date"]) + pd.Timedelta(hours=int(hh), minutes=int(mm))
isot["time"] = isot.apply(to_ts, axis=1)
isot_h = (isot.set_index("time")["cena_EUR"].resample("h").mean()
          .rename("isot_eur").reset_index())

# ---------- 3) počasie z Open-Meteo za celé obdobie ----------
print("Sťahujem slnko/teplotu z Open-Meteo …")
wx = ds.fetch_pv_forecast(LAT, LON, KWP, TILT, AZ, EFF, start=START, end=today)
wx["time"] = wx["time"].dt.floor("h")
wx_h = wx[["time", "gti", "temp", "cloud", "kw"]]

# ---------- 4) spojenie ----------
df = isot_h.merge(wx_h, on="time", how="inner").dropna(subset=["isot_eur"])
df["hour"] = df.time.dt.hour
df["dow"] = df.time.dt.dayofweek
df["month"] = df.time.dt.month
df = df[["time", "isot_eur", "gti", "temp", "cloud", "kw", "hour", "dow", "month"]]
df.to_csv("out/price_train_2026.csv", index=False)

# ---------- 5) súhrn ----------
neg = (df.isot_eur < 0).mean()
print("\n" + "="*60)
print(f"TRÉNINGOVÁ SADA: {len(df)} hodín, {df.time.dt.date.nunique()} dní "
      f"({df.time.min()} … {df.time.max()})")
print(f"  ISOT [EUR/MWh]:  priemer {df.isot_eur.mean():.1f}  "
      f"min {df.isot_eur.min():.0f}  max {df.isot_eur.max():.0f}  "
      f"podiel záporných {neg:.1%}")
print(f"  korelácia ISOT~GTI:  {df.isot_eur.corr(df.gti):.3f}")
print(f"  korelácia ISOT~teplota: {df.isot_eur.corr(df.temp):.3f}")
print("  priemerný ISOT podľa pásma GTI [W/m2]:")
for lo, hi in [(0,1),(1,200),(200,400),(400,600),(600,1500)]:
    g = df[(df.gti>=lo) & (df.gti<hi)]
    if len(g):
        print(f"     {lo:4d}-{hi:<4d}: {g.isot_eur.mean():7.1f} EUR  (n={len(g)})")
print("="*60)
print("Uložené: out/isot_2026.csv, out/price_train_2026.csv")
print("Pošli mi tento súhrn – podľa neho postavím price_model.py")
