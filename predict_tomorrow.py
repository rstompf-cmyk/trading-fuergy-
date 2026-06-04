# -*- coding: utf-8 -*-
"""predict_tomorrow.py – rýchla predikcia ISOT na zajtra (model s históriou cien).
Lagy berie z out/price_train_2026.csv (posledné dni). python predict_tomorrow.py"""
import datetime as dt
import numpy as np, pandas as pd
import data_sources as ds
from price_model import PriceModel

LAT, LON, KWP, TILT, AZ, EFF = 49.5961, 17.3634, 99.0, 30.0, 0.0, 0.85
day = dt.date.today() + dt.timedelta(days=1)

hist_df = pd.read_csv("out/price_train_2026.csv", parse_dates=["time"])
try:
    pm = PriceModel.load("out/price_model.joblib")
    if pm.reg.n_features_in_ != 13:
        raise ValueError
    print("Model načítaný z out/price_model.joblib")
except Exception:
    pm = PriceModel().fit(hist_df); print("Model dotrénovaný z price_train_2026.csv")

wx = ds.fetch_pv_forecast(LAT, LON, KWP, TILT, AZ, EFF, start=day, end=day)
wx["time"] = pd.to_datetime(wx["time"]); wx = wx[wx.time.dt.date == day].copy()
wx["hour"] = wx.time.dt.hour

hist = hist_df.tail(8*24)[["time", "isot_eur", "gti", "temp", "cloud"]]
wx2 = wx[["time", "gti", "temp", "cloud"]].copy(); wx2["isot_eur"] = np.nan
ctx = pd.concat([hist, wx2], ignore_index=True)
pred = pm.predict(ctx)
pr = wx.merge(pred[pred.time.dt.date == day][["time", "pred_isot", "p_neg"]], on="time").sort_values("hour")

print("\n" + "="*58)
print(f"PREDIKCIA NA {day}  ({['po','ut','st','št','pi','so','ne'][day.weekday()]})")
print(f"Očakávaná výroba FTV: {pr.kw.sum():.0f} kWh/deň")
print("="*58)
print(" hod  GTI[W/m2]  teplota  pred.ISOT[€]  P(zápor)  FTV[kW]")
for _, r in pr.iterrows():
    flag = "  ⬅ lacné" if r.pred_isot < pr.pred_isot.quantile(0.25) else \
           ("  ⬅ drahé" if r.pred_isot > pr.pred_isot.quantile(0.75) else "")
    print(f" {int(r.hour):2d}    {r.gti:5.0f}     {r.temp:5.1f}     {r.pred_isot:7.1f}     {r.p_neg:4.0%}    {r.kw:5.1f}{flag}")
print("-"*58)
print(f"Priemer ISOT: {pr.pred_isot.mean():.1f} €/MWh   "
      f"min {pr.pred_isot.min():.0f}   max {pr.pred_isot.max():.0f}")
print(f"Najlacnejšie (NABÍJAŤ): {sorted(pr.nsmallest(4,'pred_isot').hour.astype(int))}")
print(f"Najdrahšie (VYBÍJAŤ):   {sorted(pr.nlargest(4,'pred_isot').hour.astype(int))}")
print("="*58)
