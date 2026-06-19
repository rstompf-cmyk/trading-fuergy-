# -*- coding: utf-8 -*-
"""
tools/diag_trades.py — DÔVOD NÁKUPOV (analýza nominácie): rozloží PLÁN D-1 (DAM) +
uzavreté VDT obchody pre daný profil/deň. Funguje aj pre DNEŠOK/plán (na rozdiel od
livesim CSV, ktorý má len dokončené dni). Zodpovedá hornému grafu „Nominácia".

Použitie (v kontajneri):
    docker exec trading-fuergy-dev python tools/diag_trades.py \
        --profile VW_simulacia_3 --date 2026-06-19 --from-hour 18

Konvencia batérie: + = vybíjanie (PREDAJ/export), − = nabíjanie (NÁKUP/import).
"""
from __future__ import annotations
import argparse, os, sys, glob, json
import pandas as pd, numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _find_plan(profile, date):
    pats = [f"out/**/plans/{profile}/{date}_*15min*.json",
            f"out/**/plans/{profile}/{date}_*.json",
            f"out/plans/{profile}/{date}_*.json"]
    for p in pats:
        hits = sorted(glob.glob(p, recursive=True))
        if hits:
            return hits[0]
    return None


def _find_vdt(profile):
    try:
        import vdt_live_advisor as _vla
        p = _vla.paper_trades_csv_path(profile)
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    hits = sorted(glob.glob("out/**/vdt_paper_trades*.csv", recursive=True))
    return hits[0] if hits else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--from-hour", type=int, default=18, dest="fromh")
    a = ap.parse_args()

    # ── PLÁN D-1 (DAM nominácia) ───────────────────────────────────────────
    pf = _find_plan(a.profile, a.date)
    if not pf:
        print(f"Plán pre {a.profile} / {a.date} nenájdený (out/<trh>/plans/{a.profile}/{a.date}_*.json).")
    else:
        d = json.load(open(pf)); s = d.get("schedule", {})
        bk = np.asarray(s.get("batt_kw", []), float)
        pr = np.asarray(s.get("price_eur", []), float)
        soc = np.asarray(s.get("soc_pct", []), float)
        n = len(bk); step = 15 if n == 96 else 60
        print(f"\n=== PLÁN D-1 (DAM) {a.profile} / {a.date}  [{pf.split('/')[-1]}, {n} slotov] ===")
        print(f"{'slot':6} {'batt_kW':>9} {'cena€/MWh':>10} {'SOC%':>6}  smer")
        buy_kwh = sell_kwh = 0.0; buy_val = sell_val = 0.0
        for i in range(n):
            h = (i * step) // 60; mn = (i * step) % 60
            kw = bk[i] if i < len(bk) else 0.0
            p = pr[i] if i < len(pr) else 0.0
            sc = soc[i] if i < len(soc) else 0.0
            kwh = abs(kw) * (step / 60.0)
            if kw < -0.5:    # nabíjanie = nákup
                buy_kwh += kwh; buy_val += kwh * p
            elif kw > 0.5:
                sell_kwh += kwh; sell_val += kwh * p
            if h >= a.fromh and abs(kw) > 0.5:
                tag = ">> NÁKUP (import)" if kw < 0 else "predaj (export)"
                flag = "  <<< DRAHÝ NÁKUP" if (kw < 0 and p > 150) else ""
                print(f"{h:02d}:{mn:02d}  {kw:9.0f} {p:10.1f} {sc:6.1f}  {tag}{flag}")
        avg_buy = buy_val / buy_kwh if buy_kwh else 0.0
        avg_sell = sell_val / sell_kwh if sell_kwh else 0.0
        print(f"  DAM SPOLU: nákup {buy_kwh:.0f} kWh @ Ø {avg_buy:.0f} €/MWh · "
              f"predaj {sell_kwh:.0f} kWh @ Ø {avg_sell:.0f} €/MWh · netto {sell_kwh-buy_kwh:+.0f} kWh")
        if buy_kwh and sell_kwh:
            print(f"  → krížová arbitráž: {'OK (predaj drahší než nákup)' if avg_sell > avg_buy else '⚠ NÁKUP drahší než predaj = STRATOVÉ!'}"
                  f"  (spread Ø {avg_sell-avg_buy:+.0f} €/MWh)")

    # ── VDT uzavreté obchody (párovanie/saldo) ─────────────────────────────
    vp = _find_vdt(a.profile)
    print(f"\n=== VDT uzavreté obchody {a.profile} / {a.date} ===")
    if not vp:
        print("  vdt_paper_trades.csv nenájdený.")
    else:
        try:
            try:
                vt = pd.read_csv(vp, on_bad_lines="skip")
            except TypeError:
                vt = pd.read_csv(vp, error_bad_lines=False)   # staršia pandas
            vt = vt[vt["profile"].astype(str) == a.profile]
            vt["d"] = pd.to_datetime(vt["ts"], errors="coerce").dt.date.astype(str)
            vt = vt[(vt["d"] == a.date) & (vt["slot"].astype(str).str.contains(":"))]
            if vt.empty:
                print("  (žiadne VDT obchody pre tento deň — večerné nákupy sú DAM plán alebo RT)")
            else:
                vt["pe"] = pd.to_numeric(vt["price_predicted_eur"], errors="coerce")
                vt["kwh_a"] = pd.to_numeric(vt["kwh"], errors="coerce").abs()
                chg = vt[vt["action"].astype(str).str.contains("charge|buy", case=False)]
                dis = vt[vt["action"].astype(str).str.contains("discharge|sell", case=False)]
                ck, dk = chg["kwh_a"].sum(), dis["kwh_a"].sum()
                cb, sp = chg["pe"].mean(), dis["pe"].mean()
                print(f"  VDT nákup {ck:.0f} kWh @ Ø {cb:.0f} €/MWh · predaj {dk:.0f} kWh @ Ø {sp:.0f} €/MWh")
                saldo = ck - dk
                print(f"  SALDO (nákup−predaj) = {saldo:+.0f} kWh  "
                      f"{'→ OK ~0 (spárované, soc-neutral)' if abs(saldo) < 0.05*max(ck,1) else '→ ⚠ NEVYROVNANÉ = nepárový nákup!'}")
                if pd.notna(cb) and pd.notna(sp) and cb > sp:
                    print(f"  ⚠ priemerný VDT NÁKUP ({cb:.0f}) > PREDAJ ({sp:.0f}) → stratový smer!")
                # večerné VDT nákupy
                ev = chg[chg["slot"].astype(str).str[:2].astype(int) >= a.fromh]
                if len(ev):
                    print(f"  Večerné VDT nákupy (od {a.fromh}:00):")
                    for _, r in ev.iterrows():
                        print(f"    {r['slot']}  {r['kwh_a']:.0f} kWh @ {r['pe']:.0f} €/MWh  "
                              f"(profit_rest_of_day {pd.to_numeric(r.get('profit_eur_rest_of_day'), errors='coerce'):+.1f} €)")
        except Exception as e:
            print(f"  (VDT načítanie zlyhalo: {e})")

    print("\nLegenda: DAM nákup = nabíjanie v pláne (krížová arbitráž — má sa predať drahšie). "
          "VDT saldo ~0 = nákupy spárované s predajmi (min_spread). RT/terminál nákupy tu nie sú "
          "(tie sú v živej realite, nie v D-1 pláne) — tie kupujú aj draho bez páru.")


if __name__ == "__main__":
    main()
