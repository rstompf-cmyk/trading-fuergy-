# -*- coding: utf-8 -*-
"""tools/param_sweep.py — parameter sweep D-1 LP ekonomiky profilu (fáza 1).

Cieľ: nájsť kombináciu parametrov maximalizujúcu efekt D-1 vrstvy na REÁLNYCH
OTE cenách (out/cache/ote_dt_*.csv). Bez livesim/RT/VDT — čistý plánovací LP,
takže beh je sekundový a nedotýka sa žiadneho stavu aplikácie.

Dizajn experimentu:
  • `cycle_cost`/`min_spread` v gride = ČO LP VERÍ (formuje rozhodnutia).
  • Vyhodnotenie používa FIXNÚ "skutočnú" degradáciu EVAL_DEG_EUR_MWH na reálne
    pretočenú energiu — fér porovnanie medzi kombináciami.
  • SOC sa REŤAZÍ medzi dňami (koniec dňa N = začiatok N+1); zvyšková energia
    na konci obdobia sa kredituje priemernou cenou × eff_d (inak by vyhrávala
    kombinácia, čo nechá batériu prázdnu).

Použitie:
    python3 tools/param_sweep.py --from 2026-06-01 --to 2026-06-11
    (voliteľne --profile-json out/profiles/VW_simulacia_3.json)
"""
from __future__ import annotations
import argparse
import csv as _csv
import datetime as dt
import itertools
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EVAL_DEG_EUR_MWH = 8.0          # "skutočná" degradácia pre vyhodnotenie

# Base = VW_simulacia_3 (dump 2026-06-11); --profile-json ho prepíše
BASE = dict(batt_kw=6000.0, batt_kwh=6000.0, eff_c=0.95, eff_d=0.95,
            soc_min=5.0, soc_max=100.0, soc_init=5.0, terminal_soc=5.0,
            grid_kw=6000.0, grid_kw_import=6000.0, grid_kw_export=6000.0,
            grid_fee=1.0, cycle_cost=1.0, min_spread=1.0, min_trade=0.0,
            allow_grid_charge=True, allow_curtail=False, block_neg_import=False,
            max_export_kwh_day=4000.0, max_import_kwh_day=4000.0)

JOINT_FLAGS = dict(enabled=True, trade_batt=True, trade_ftv=False,
                   trade_load=False, use_vdt=False, optimize_distribution=False)

GRID = {
    "cycle_cost":        [1.0, 5.0, 10.0],
    "min_spread":        [1.0, 5.0, 15.0],
    "terminal_soc":      [5.0, 30.0],
    "max_kwh_day":       [4000.0, 8000.0, 0.0],     # export aj import naraz; 0 = bez stropu
}


_OKTE_HIST = None


def _load_okte_hist():
    """SK OKTE ISOT 15-min historian (C_WEB_OKTE_ISOT_15m) — UTC → lokálny čas.
    Sme na SK trhu: zdroj DAM cien je OKTE, nie CZ OTE (`ote_dt_*` cache je CZ legacy)."""
    global _OKTE_HIST
    if _OKTE_HIST is None:
        import pandas as pd
        p = os.path.join("out", "sk", "historian_C_WEB_OKTE_ISOT_15m.csv")
        df = pd.read_csv(p)
        t = (pd.to_datetime(df["time_utc"], errors="coerce")
               .dt.tz_localize("UTC").dt.tz_convert("Europe/Bratislava")
               .dt.tz_localize(None))
        # historian timestamp = koniec intervalu → −1 min mapuje do správnej hodiny/dňa
        df["t_loc"] = t - pd.Timedelta(minutes=1)
        df = df.dropna(subset=["t_loc"]).drop_duplicates("t_loc", keep="last")
        _OKTE_HIST = df
    return _OKTE_HIST


def load_prices_hourly(d: dt.date) -> np.ndarray | None:
    df = _load_okte_hist()
    sub = df[df["t_loc"].dt.date == d]
    if len(sub) < 80:
        return None
    grp = sub.groupby(sub["t_loc"].dt.hour)["value"].mean()
    out = np.zeros(24)
    last = float(grp.iloc[0])
    for h in range(24):
        if h in grp.index:
            last = float(grp.loc[h])
        out[h] = last
    return out


def run_combo(days, prices_by_day, fp) -> dict:
    from joint_lp_integration import optimize_day_or_joint
    soc = float(fp["soc_init"])
    rev_eur = 0.0
    cycled_kwh = 0.0
    mex = float(fp["max_export_kwh_day"]) or None
    mim = float(fp["max_import_kwh_day"]) or None
    for d in days:
        pr = prices_by_day[d]
        sch, _ = optimize_day_or_joint(
            np.zeros(24), pr,
            joint_flags=JOINT_FLAGS, profile=None,
            batt_kw=fp["batt_kw"], batt_kwh=fp["batt_kwh"],
            eff_c=fp["eff_c"], eff_d=fp["eff_d"],
            soc_min_pct=fp["soc_min"], soc_max_pct=fp["soc_max"],
            soc_init_pct=soc,
            terminal_soc_pct=fp["terminal_soc"],
            grid_kw=fp["grid_kw"],
            grid_kw_import=fp["grid_kw_import"], grid_kw_export=fp["grid_kw_export"],
            grid_fee=fp["grid_fee"], cycle_cost=fp["cycle_cost"],
            allow_grid_charge=bool(fp["allow_grid_charge"]),
            allow_curtail=bool(fp["allow_curtail"]),
            min_spread_eur=fp["min_spread"], min_trade_mwh=fp["min_trade"],
            block_neg_import=bool(fp["block_neg_import"]),
            max_export_kwh_day=mex, max_import_kwh_day=mim,
            dt=1.0,
        )
        g = np.asarray(sch["grid_kwh"].values, float)
        rev_eur += float(np.sum(np.where(g > 0, g * pr, g * (pr + fp["grid_fee"])))) / 1000.0
        b = np.asarray(sch["batt_kw"].values, float)
        cycled_kwh += float(np.sum(np.abs(b)))          # kW × 1 h = kWh
        soc = float(sch["soc_pct"].values[-1])
    # kredit zvyškovej energie (nad soc_min) priemernou cenou obdobia
    all_p = np.concatenate([prices_by_day[d] for d in days])
    resid_kwh = max(0.0, (soc - fp["soc_min"]) / 100.0 * fp["batt_kwh"])
    resid_eur = resid_kwh * fp["eff_d"] * float(np.mean(all_p)) / 1000.0
    deg_eur = cycled_kwh / 1000.0 * EVAL_DEG_EUR_MWH / 2.0   # /2: cyklus = nabitie+vybitie
    return dict(rev_eur=rev_eur, resid_eur=resid_eur, deg_eur=deg_eur,
                profit_eur=rev_eur + resid_eur - deg_eur,
                cycles=cycled_kwh / 2.0 / fp["batt_kwh"] / len(days),
                soc_end=soc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", default="2026-06-01")
    ap.add_argument("--to", dest="d_to", default="2026-06-11")
    ap.add_argument("--profile-json", dest="pjson", default=None)
    args = ap.parse_args()

    base = dict(BASE)
    if args.pjson and os.path.exists(args.pjson):
        plan = (json.load(open(args.pjson)).get("plan") or {})
        for k in base:
            if plan.get(k) is not None:
                base[k] = plan[k]

    d0 = dt.date.fromisoformat(args.d_from)
    d1 = dt.date.fromisoformat(args.d_to)
    days, prices = [], {}
    d = d0
    while d <= d1:
        pr = load_prices_hourly(d)
        if pr is not None:
            days.append(d); prices[d] = pr
        d += dt.timedelta(days=1)
    if not days:
        print("Žiadne OTE cache dni v rozsahu."); return
    print(f"Sweep: {len(days)} dní ({days[0]}…{days[-1]}), "
          f"eval degradácia {EVAL_DEG_EUR_MWH} €/MWh, batt {base['batt_kw']:.0f}/{base['batt_kwh']:.0f}")

    keys = list(GRID.keys())
    results = []
    for combo in itertools.product(*GRID.values()):
        fp = dict(base)
        cv = dict(zip(keys, combo))
        fp["cycle_cost"] = cv["cycle_cost"]
        fp["min_spread"] = cv["min_spread"]
        fp["terminal_soc"] = cv["terminal_soc"]
        fp["max_export_kwh_day"] = cv["max_kwh_day"]
        fp["max_import_kwh_day"] = cv["max_kwh_day"]
        try:
            r = run_combo(days, prices, fp)
        except Exception as e:
            print(f"  combo {cv} zlyhal: {e}")
            continue
        results.append({**cv, **{k: round(v, 2) for k, v in r.items()}})
        print(f"  cc={cv['cycle_cost']:>4} ms={cv['min_spread']:>4} term={cv['terminal_soc']:>4} "
              f"cap={cv['max_kwh_day']:>6} → profit {r['profit_eur']:>9.1f} € "
              f"(rev {r['rev_eur']:.0f} + resid {r['resid_eur']:.0f} − deg {r['deg_eur']:.0f}) "
              f"· {r['cycles']:.2f} cyk/deň")

    results.sort(key=lambda x: -x["profit_eur"])
    out_p = f"out/sweep_results_{dt.datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    with open(out_p, "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        for r in results:
            w.writerow(r)
    print(f"\nTOP 5 (obdobie {len(days)} dní):")
    for r in results[:5]:
        print(f"  profit {r['profit_eur']:>9.1f} € · cycle_cost={r['cycle_cost']} "
              f"min_spread={r['min_spread']} terminal={r['terminal_soc']} cap/deň={r['max_kwh_day']}")
    print(f"\nVýsledky: {out_p}")


if __name__ == "__main__":
    main()
