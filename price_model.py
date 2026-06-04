# -*- coding: utf-8 -*-
"""
price_model.py – odhad ISOT zo slnka/teploty/hodiny + HISTÓRIE CIEN (lag 1/2/7 dní,
rolling priemer, víkend). Backtest ukázal zachytenie ~82 % stropu (vs 54 % bez histórie).

API:
    pm = PriceModel().fit(df_hist)              # df_hist: time, isot_eur, gti, temp, cloud, ...
    out = pm.predict(df_context)                # context = história(s cenami) + cieľové riadky
    # out má stĺpce time, pred_isot, p_neg pre VŠETKY riadky kontextu (cieľový deň si vyfiltruj)
    pm.save("out/price_model.joblib"); PriceModel.load(...)

Pozn.: lag/rolling príznaky sa počítajú zo súvislého hodinového radu cien; pri živej
predikcii na zajtra treba do kontextu pridať posledných ~8 dní skutočných cien (z OTE).
"""
from __future__ import annotations
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor, HistGradientBoostingClassifier
from sklearn.metrics import mean_absolute_error, r2_score, roc_auc_score

FEATURES = ["gti", "temp", "cloud", "hour", "dow", "month", "hour_sin", "hour_cos",
            "is_weekend", "lag1d", "lag2d", "lag7d", "roll7d"]


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Zo súvislého hodinového radu vytvorí príznaky vrátane histórie cien."""
    d = df.copy()
    d["time"] = pd.to_datetime(d["time"])
    d = d.sort_values("time").set_index("time").asfreq("h")
    for c in ["gti", "cloud"]:
        d[c] = d[c].fillna(0) if c in d else 0
    d["temp"] = (d["temp"].ffill().bfill() if "temp" in d else 15.0)
    if "isot_eur" not in d:
        d["isot_eur"] = np.nan
    d["lag1d"] = d["isot_eur"].shift(24)
    d["lag2d"] = d["isot_eur"].shift(48)
    d["lag7d"] = d["isot_eur"].shift(168)
    d["roll7d"] = d["isot_eur"].shift(24).rolling(168, min_periods=24).mean()
    d["hour"] = d.index.hour; d["dow"] = d.index.dayofweek; d["month"] = d.index.month
    d["is_weekend"] = (d.index.dayofweek >= 5).astype(int)
    d["hour_sin"] = np.sin(2*np.pi*d["hour"]/24); d["hour_cos"] = np.cos(2*np.pi*d["hour"]/24)
    return d.reset_index()


class PriceModel:
    def __init__(self):
        self.reg = None; self.clf = None

    def fit(self, df: pd.DataFrame) -> "PriceModel":
        F = build_features(df)
        m = F["isot_eur"].notna()
        X, y = F.loc[m, FEATURES], F.loc[m, "isot_eur"].values
        self.reg = HistGradientBoostingRegressor(max_iter=400, learning_rate=0.05,
                   max_leaf_nodes=31, l2_regularization=1.0, random_state=0).fit(X, y)
        yneg = (y < 0).astype(int)
        if yneg.sum() >= 10:
            w = np.where(yneg == 1, (len(yneg)-yneg.sum())/max(yneg.sum(), 1), 1.0)
            self.clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05,
                       random_state=0).fit(X, yneg, sample_weight=w)
        return self

    def predict(self, df_context: pd.DataFrame) -> pd.DataFrame:
        F = build_features(df_context)
        F["pred_isot"] = self.reg.predict(F[FEATURES])
        F["p_neg"] = self.clf.predict_proba(F[FEATURES])[:, 1] if self.clf else 0.0
        return F

    def save(self, path):
        import joblib; joblib.dump({"reg": self.reg, "clf": self.clf}, path)

    @classmethod
    def load(cls, path):
        import joblib; o = cls(); m = joblib.load(path); o.reg, o.clf = m["reg"], m["clf"]; return o


def evaluate(df, test_days=14):
    F = build_features(df); F["date"] = F.time.dt.date
    F = F.dropna(subset=["isot_eur"])
    cut = sorted(F.date.unique())[-test_days]
    tr, te = F[F.date < cut], F[F.date >= cut]
    reg = HistGradientBoostingRegressor(max_iter=400, learning_rate=0.05,
          max_leaf_nodes=31, l2_regularization=1.0, random_state=0).fit(tr[FEATURES], tr.isot_eur)
    pred = reg.predict(te[FEATURES])
    mae = mean_absolute_error(te.isot_eur, pred); r2 = r2_score(te.isot_eur, pred)
    hm = tr.groupby("hour").isot_eur.mean()
    base = mean_absolute_error(te.isot_eur, te.hour.map(hm).fillna(tr.isot_eur.mean()))
    print(f"  OUT-OF-SAMPLE (posledných {test_days} dní, n={len(te)}):")
    print(f"    model    MAE {mae:6.1f} €/MWh   R² {r2:5.2f}")
    print(f"    baseline MAE {base:6.1f} €/MWh   (priemer podľa hodiny)")
    if (te.isot_eur < 0).sum() >= 3:
        try:
            cl = HistGradientBoostingClassifier(max_iter=300, random_state=0).fit(
                 tr[FEATURES], (tr.isot_eur < 0).astype(int))
            auc = roc_auc_score((te.isot_eur < 0).astype(int), cl.predict_proba(te[FEATURES])[:, 1])
            print(f"    P(záporná cena) AUC {auc:4.2f}")
        except Exception:
            pass


def main():
    df = pd.read_csv("out/price_train_2026.csv", parse_dates=["time"])
    print(f"Načítané {len(df)} hodín ({df.time.min()} … {df.time.max()})")
    evaluate(df)
    PriceModel().fit(df).save("out/price_model.joblib")
    print("\nModel (s históriou cien) uložený: out/price_model.joblib")


if __name__ == "__main__":
    main()
