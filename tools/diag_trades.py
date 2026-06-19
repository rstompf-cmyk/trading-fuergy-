# -*- coding: utf-8 -*-
"""
tools/diag_trades.py — DÔVOD NÁKUPOV: rozloží každý nabíjací (nákup) slot na zdroj
(DAM plán / RT odchýlka) + cenu + RT dôvod, a skontroluje VDT párovanie (saldo + spread).

Použitie (v kontajneri na dev/Windows):
    docker exec trading-fuergy-dev python tools/diag_trades.py \
        --profile VW_simulacia_3 --date 2026-06-19 --from-hour 18

Konvencia batérie: + = vybíjanie (predaj), − = nabíjanie (nákup).
"""
from __future__ import annotations
import argparse, os, sys, glob
import pandas as pd, numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_livesim(case, port, profile, day):
    try:
        import livesim as lsim
        if profile:
            os.environ["FTV_PROFILE"] = profile
        df = lsim.load_series(case, port=str(port), day=day, max_points=10**9)
        if df is not None and not df.empty:
            return df
    except Exception as e:
        print(f"(load_series: {e})")
    # fallback: glob CSV priamo
    for pat in [f"out/**/livesim_{case}*{port}*.csv", f"out/**/livesim_{case}*.csv"]:
        for f in sorted(glob.glob(pat, recursive=True)):
            try:
                t = pd.read_csv(f, parse_dates=["time"])
                t = t[t["time"].dt.date.astype(str) == day]
                if not t.empty:
                    print(f"(fallback CSV: {f})")
                    return t
            except Exception:
                continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--case", default="dt_15min")
    ap.add_argument("--port", default=os.environ.get("PORT", os.environ.get("APP_PORT", "8001")))
    ap.add_argument("--from-hour", type=int, default=18, dest="fromh")
    a = ap.parse_args()

    df = _load_livesim(a.case, a.port, a.profile, a.date)
    if df is None or df.empty:
        print(f"Žiadne livesim dáta pre {a.profile} / {a.date} (case={a.case}, port={a.port}).")
        sys.exit(2)

    df = df.copy()
    df["t"] = pd.to_datetime(df["time"])
    df["slot"] = df["t"].dt.floor("15min")
    g = lambda c: pd.to_numeric(df[c], errors="coerce") if c in df.columns else pd.Series(np.nan, index=df.index)
    agg = df.groupby("slot").agg(
        plan_kw=("plan_batt_kw", "mean") if "plan_batt_kw" in df else ("t", "size"),
        real_kw=("batt_kw_realistic", "mean") if "batt_kw_realistic" in df else ("t", "size"),
        dt_eur=("dt_eur", "mean") if "dt_eur" in df else ("t", "size"),
        vdt_eur=("vdt_eur", "mean") if "vdt_eur" in df else ("t", "size"),
        soc=("soc_pct", "last") if "soc_pct" in df else ("t", "size"),
    ).reset_index()
    # rt_reason: posledný neprázdny v slote
    rr = {}
    if "rt_reason" in df.columns:
        for s, sub in df.groupby("slot"):
            vals = [x for x in sub["rt_reason"].astype(str) if x and x != "nan"]
            rr[s] = vals[-1] if vals else ""

    print(f"\n=== NÁKUPY (nabíjanie) {a.profile} / {a.date}, od {a.fromh}:00 ===")
    print(f"{'slot':6} {'plán_kW':>9} {'real_kW':>9} {'RT_kW':>8} {'DT€/MWh':>9} {'VDT€':>7} {'SOC%':>6}  zdroj / RT dôvod")
    tot_buy = 0.0
    for _, r in agg.iterrows():
        ts = pd.Timestamp(r["slot"])
        if ts.hour < a.fromh:
            continue
        plan = float(r["plan_kw"]); real = float(r["real_kw"]); rt = real - plan
        is_charge = real < -0.5    # nabíjanie = nákup
        src = ""
        if is_charge:
            if plan < -0.5 and abs(rt) < 0.5:
                src = "DAM plán (LP arbitráž)"
            elif rt < -0.5 and plan >= -0.5:
                src = "RT odchýlka"
            elif rt < -0.5 and plan < -0.5:
                src = "DAM plán + RT"
            else:
                src = "?"
        tag = ">> NÁKUP" if is_charge else ("predaj" if real > 0.5 else "idle")
        reason = rr.get(r["slot"], "")
        flag = "  <<< DRAHÝ NÁKUP" if (is_charge and float(r["dt_eur"]) > 150) else ""
        print(f"{ts.strftime('%H:%M'):6} {plan:9.0f} {real:9.0f} {rt:8.0f} "
              f"{float(r['dt_eur']):9.1f} {float(r['vdt_eur']):7.0f} {float(r['soc']):6.1f}  "
              f"{tag:8} {src} {reason}{flag}")
        if is_charge:
            tot_buy += abs(real) * 0.25

    # ── VDT párovanie + saldo z paper trades ───────────────────────────────
    print(f"\n=== VDT obchody (párovanie/saldo) {a.profile} / {a.date} ===")
    try:
        import vdt_live_advisor as _vla
        vp = _vla.paper_trades_csv_path(a.profile)
        vt = pd.read_csv(vp)
        vt = vt[(vt["profile"].astype(str) == a.profile)]
        vt = vt[vt["slot"].astype(str).str.len() > 0]
        vt["d"] = pd.to_datetime(vt["ts"], errors="coerce").dt.date.astype(str)
        vt = vt[vt["d"] == a.date]
        if vt.empty:
            print("  (žiadne VDT paper trades pre tento deň — nákupy sú DAM/RT, nie VDT)")
        else:
            chg = vt[vt["action"].astype(str).str.contains("charge|buy", case=False)]
            dis = vt[vt["action"].astype(str).str.contains("discharge|sell", case=False)]
            ch_kwh = pd.to_numeric(chg.get("kwh"), errors="coerce").abs().sum()
            di_kwh = pd.to_numeric(dis.get("kwh"), errors="coerce").abs().sum()
            buy_p = pd.to_numeric(chg.get("price_predicted_eur"), errors="coerce")
            sell_p = pd.to_numeric(dis.get("price_predicted_eur"), errors="coerce")
            print(f"  VDT nákup {ch_kwh:.0f} kWh @ priemer {buy_p.mean():.0f} €/MWh · "
                  f"predaj {di_kwh:.0f} kWh @ {sell_p.mean():.0f} €/MWh")
            print(f"  SALDO (nákup−predaj) = {ch_kwh - di_kwh:+.0f} kWh  "
                  f"({'OK ~0 (spárované)' if abs(ch_kwh-di_kwh) < 0.05*max(ch_kwh,1) else 'NEVYROVNANÉ! nepárový nákup'})")
            if len(buy_p) and len(sell_p) and buy_p.mean() > sell_p.mean():
                print(f"  ⚠ priemerný NÁKUP ({buy_p.mean():.0f}) > priemerný PREDAJ ({sell_p.mean():.0f}) → stratový smer!")
    except Exception as e:
        print(f"  (VDT trades sa nepodarilo načítať: {e})")

    print(f"\nSpolu nakúpené (nabíjanie) od {a.fromh}:00: {tot_buy:.0f} kWh")
    print("Legenda: 'DAM plán' = krížová arbitráž v LP (párované, min_spread). "
          "'RT odchýlka' = reakcia na sys_MW/SOC-lift (NIE párované — pozri rt_reason).")


if __name__ == "__main__":
    main()
