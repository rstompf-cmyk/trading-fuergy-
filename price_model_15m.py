# -*- coding: utf-8 -*-
"""
price_model_15m.py — dedikovaný 15-min model VNÚTROHODINOVÉHO TVARU ceny (2026-06-18).

Princíp (validované OOS +24 % vs plochá hodinová): hodinový level berie existujúci
hodinový PriceModel (pred_isot), tento model predikuje len ODCHÝLKU každej štvrťhodiny
od hodinového priemeru (€/MWh), podmienenú pozíciou v hodine + kalendárom + počasím.
Odchýlka sa per-hodina vynúti na mean-0, takže hodinový priemer 15-min predikcie ==
hodinová predikcia (level sa nemení, pridá sa len tvar).

Tréning: 15-min ISOT historian (out/sk/historian_C_OKTE_ISOT_15m_final.csv) + hodinové
počasie (out/price_train_2026.csv). Cieľ = price15 - hodinový_priemer(price15).

API:
    m = PriceModel15().fit(hist15_df, weather_df)      # natrénuje shape regresor
    price96 = m.predict_shape(hourly_price24, date, weather_hourly_df)  # 96 × €/MWh
    m.save("out/price_model_15m.joblib"); PriceModel15.load(...)
"""
from __future__ import annotations
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

FEAT = ["qoh", "qoh_sin", "qoh_cos", "hour", "hour_sin", "hour_cos",
        "dow", "month", "gti", "temp", "cloud"]


def _to_local(series) -> pd.Series:
    """UTC timestampy (historian time_utc) → naive LOKÁLNY SK čas (Europe/Bratislava,
    DST-aware). Bez tohto sa vnútrohodinový tvar aj špic posunú o 1-2h (leto)."""
    try:
        return pd.to_datetime(series, utc=True).dt.tz_convert("Europe/Bratislava").dt.tz_localize(None)
    except Exception:
        return pd.to_datetime(series) + pd.Timedelta(hours=2)   # fallback: fixný letný posun


def _calendar_feats(d: pd.DataFrame) -> pd.DataFrame:
    d["qoh_sin"] = np.sin(2 * np.pi * d["qoh"] / 4)
    d["qoh_cos"] = np.cos(2 * np.pi * d["qoh"] / 4)
    d["hour_sin"] = np.sin(2 * np.pi * d["hour"] / 24)
    d["hour_cos"] = np.cos(2 * np.pi * d["hour"] / 24)
    return d


class PriceModel15:
    def __init__(self):
        self.reg = None

    def fit(self, hist15: pd.DataFrame, weather: pd.DataFrame) -> "PriceModel15":
        """hist15: stĺpce time_utc, value (15-min ISOT). weather: time, gti, temp, cloud (hodinové)."""
        h = hist15.copy()
        h["t"] = _to_local(h["time_utc"]) if "time_utc" in h else pd.to_datetime(h["time"])
        h = h[["t", ("value" if "value" in h else "price15")]].copy()
        h.columns = ["t", "price15"]
        h = h.dropna().sort_values("t")
        # historian label = koniec intervalu → posun na začiatok
        ts = h["t"] - pd.Timedelta(minutes=15)
        h["date"] = ts.dt.date; h["hour"] = ts.dt.hour
        h["qoh"] = (ts.dt.minute // 15).astype(int)
        h["dow"] = ts.dt.dayofweek; h["month"] = ts.dt.month
        hm = h.groupby(["date", "hour"]).price15.mean().reset_index().rename(
            columns={"price15": "hourmean"})
        h = h.merge(hm, on=["date", "hour"])
        h["dev"] = h["price15"] - h["hourmean"]
        w = weather.copy()
        w["time"] = pd.to_datetime(w["time"])
        w["date"] = w["time"].dt.date; w["hour"] = w["time"].dt.hour
        h = h.merge(w[["date", "hour", "gti", "temp", "cloud"]], on=["date", "hour"], how="left")
        for c in ["gti", "temp", "cloud"]:
            h[c] = h[c].fillna(h[c].median())
        h = _calendar_feats(h).dropna(subset=["dev"])
        self.reg = HistGradientBoostingRegressor(
            max_iter=400, learning_rate=0.05, max_leaf_nodes=31,
            l2_regularization=1.0, random_state=0).fit(h[FEAT], h["dev"])
        return self

    def predict_shape(self, hourly_price24, date, weather_hourly: pd.DataFrame) -> np.ndarray:
        """Vráti 96 × €/MWh: hodinový level + naučená vnútrohodinová odchýlka (mean-0 per hodina)."""
        hp = np.asarray(hourly_price24, dtype=float).reshape(-1)[:24]
        if hp.size < 24:
            hp = np.concatenate([hp, np.full(24 - hp.size, hp[-1] if hp.size else 0.0)])
        d = pd.to_datetime(date)
        wk = d.dayofweek; mo = d.month
        # počasie po hodinách (gti/temp/cloud) — ak chýba, neutrálne
        w = weather_hourly.copy() if weather_hourly is not None else pd.DataFrame()
        if len(w):
            w["time"] = pd.to_datetime(w["time"]); w["hour"] = w["time"].dt.hour
            wmap = {int(r.hour): (float(r.gti), float(r.temp), float(r.cloud))
                    for _, r in w.iterrows() if pd.notna(r.get("hour"))}
        else:
            wmap = {}
        rows = []
        for hh in range(24):
            gti, temp, cloud = wmap.get(hh, (0.0, 15.0, 50.0))
            for q in range(4):
                rows.append(dict(qoh=q, hour=hh, dow=wk, month=mo,
                                 gti=gti, temp=temp, cloud=cloud))
        F = _calendar_feats(pd.DataFrame(rows))
        dev = self.reg.predict(F[FEAT]).reshape(24, 4)
        dev = dev - dev.mean(axis=1, keepdims=True)        # mean-0 per hodina → level sa nemení
        out = (hp.reshape(24, 1) + dev).reshape(96)
        return out

    def save(self, path):
        import joblib; joblib.dump({"reg": self.reg, "feat": FEAT}, path)

    @classmethod
    def load(cls, path):
        import joblib; o = cls(); m = joblib.load(path); o.reg = m["reg"]; return o


_M15_CACHE = {}


def load_cached(path: str = "out/price_model_15m.joblib"):
    """Cached load (mtime-invalidovaný) — batch volá per-deň, nenačítavaj joblib zakaždým.
    Vráti None ak model neexistuje (volajúci spraví fallback na flat upsample)."""
    import os
    if not os.path.exists(path):
        return None
    mt = os.path.getmtime(path)
    if _M15_CACHE.get("mt") != mt:
        _M15_CACHE["m"] = PriceModel15.load(path)
        _M15_CACHE["mt"] = mt
    return _M15_CACHE.get("m")


def _oos_eval(hist, weather, test_days=14):
    """Rýchle OOS porovnanie: 15-min model (level+tvar) vs plochá hodinová kópia."""
    from sklearn.metrics import mean_absolute_error
    h = hist.copy(); h["t"] = _to_local(h["time_utc"]);
    ts = h["t"] - pd.Timedelta(minutes=15)
    h["price15"] = h["value"]; h["date"] = ts.dt.date; h["hour"] = ts.dt.hour
    h["qoh"] = (ts.dt.minute // 15).astype(int); h["dow"] = ts.dt.dayofweek; h["month"] = ts.dt.month
    hm = h.groupby(["date","hour"]).price15.mean().reset_index().rename(columns={"price15":"hourmean"})
    h = h.merge(hm, on=["date","hour"]); h["dev"] = h["price15"] - h["hourmean"]
    w = weather.copy(); w["time"] = pd.to_datetime(w["time"]); w["date"]=w["time"].dt.date; w["hour"]=w["time"].dt.hour
    h = h.merge(w[["date","hour","gti","temp","cloud"]], on=["date","hour"], how="left")
    for c in ["gti","temp","cloud"]: h[c] = h[c].fillna(h[c].median())
    h = _calendar_feats(h).dropna(subset=["dev"]).sort_values("t")
    dts = sorted(h.date.unique()); cut = dts[-test_days]
    tr, te = h[h.date < cut], h[h.date >= cut]
    reg = HistGradientBoostingRegressor(max_iter=400, learning_rate=0.05, max_leaf_nodes=31,
                                        l2_regularization=1.0, random_state=0).fit(tr[FEAT], tr.dev)
    pred = reg.predict(te[FEAT])
    mae_m = mean_absolute_error(te.price15, te.hourmean + pred)
    mae_f = mean_absolute_error(te.price15, te.hourmean)
    print(f"  OOS {test_days}d (n={len(te)}): FLAT MAE {mae_f:.2f} → 15-min MODEL MAE {mae_m:.2f} €/MWh "
          f"({(mae_f-mae_m)/mae_f*100:+.1f} %)")


# 15-min ISOT historian — kandidáti v poradí preferencie (najčerstvejší/najúplnejší prvý).
# C_WEB_OKTE_ISOT_15m = live, kompletný 96 slotov/deň (udržiava scheduler historian_extend).
# _final = staršia kurátorská verzia (fallback). Vyberie sa prvý existujúci s najnovším dátumom.
_HIST_CANDIDATES = [
    "out/sk/historian_C_WEB_OKTE_ISOT_15m.csv",
    "out/sk/historian_C_OKTE_ISOT_15m_final.csv",
    "out/cz/historian_C_WEB_OKTE_ISOT_15m.csv",
]


def _load_historian(base: str = "."):
    """Vráti (df, path) najčerstvejšieho dostupného 15-min ISOT historiánu, alebo (None, None)."""
    import os
    best = None
    for rel in _HIST_CANDIDATES:
        p = os.path.join(base, rel)
        if not os.path.exists(p):
            continue
        try:
            df = pd.read_csv(p)
            if "value" not in df.columns:
                # zjednoť názov hodnotového stĺpca
                cand = [c for c in df.columns if c.lower() in ("value", "cena", "price", "isot_eur", "cena_eur")]
                df = df.rename(columns={(cand[0] if cand else df.columns[1]): "value"})
            tcol = "time_utc" if "time_utc" in df.columns else df.columns[0]
            last = pd.to_datetime(df[tcol]).max()
            if best is None or last > best[2]:
                best = (df, p, last)
        except Exception as _e:
            print(f"  (historian {p}: {_e})")
    if best is None:
        return None, None
    print(f"  historian: {best[1]} (do {best[2]})")
    return best[0], best[1]


def retrain(base: str = ".") -> str:
    """Natrénuje + uloží 15-min model z najčerstvejšieho historiánu. Volá scheduler aj CLI.
    Vráti status string. Ak historian/dáta chýbajú, vyhodí výnimku (volajúci ošetrí)."""
    hist, hpath = _load_historian(base)
    if hist is None:
        raise FileNotFoundError("žiadny 15-min ISOT historian nenájdený (out/sk/historian_*ISOT*15m*.csv)")
    weather = pd.read_csv(f"{base}/out/price_train_2026.csv", parse_dates=["time"])
    try:
        _oos_eval(hist, weather)
    except Exception as e:
        print(f"  (OOS eval preskočené: {e})")
    m = PriceModel15().fit(hist, weather)
    outp = f"{base}/out/price_model_15m.joblib"
    m.save(outp)
    msg = f"15-min model uložený: {outp} (n_feat={len(FEAT)}, historian={hpath})"
    print(msg)
    return msg


def main():
    retrain(".")


if __name__ == "__main__":
    main()
