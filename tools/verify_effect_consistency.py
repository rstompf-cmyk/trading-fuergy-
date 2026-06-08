#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/verify_effect_consistency.py — verifikácia konzistencie výpočtu efektu.

Pre daný livesim CSV + profil + deň overí že **všetky zobrazenia ukazujú
rovnaké číslo** pre DT, RT a VDT arbitráž. Volá:

  1. core.effect.compute_effect_totals(df)              — agregát (Excel / karta)
  2. core.effect.get_rt_eur_series(df).sum()            — graf "Po dňoch"
  3. priamy súčet stĺpcov                                — sanity
  4. core.effect.compute_effect_cumulative(df)["cum_rt"].iloc[-1]
                                                          — kumulatívny graf

Ak všetky 4 čísla nesedia → zlyhanie s diagnostikou.

Použitie:
    python tools/verify_effect_consistency.py
    python tools/verify_effect_consistency.py --day 2026-06-03 --profile VW_simulacia
    python tools/verify_effect_consistency.py --csv /app/out/sk/livesim_plan_d1.csv
"""
from __future__ import annotations
import argparse
import sys
import os
import pandas as pd

# Importuj z parent (cesta z tools/)
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.effect import (
    resolve_rt_col, get_rt_eur_series, get_vdt_arb_series,
    compute_effect_totals, compute_effect_cumulative,
)


TOL = 0.01   # €  — tolerancia floating point pri porovnaní


def verify(csv_path: str, profile: str, day: str | None = None) -> int:
    """Vráti exit code (0 = OK, 1 = mismatch)."""
    if not os.path.exists(csv_path):
        print(f"[ERR] CSV neexistuje: {csv_path}")
        return 2

    df = pd.read_csv(csv_path, low_memory=False)
    if "time" not in df.columns:
        print(f"[ERR] CSV nemá stĺpec 'time' — moze byt headerless legacy format")
        return 2
    df["_t"] = pd.to_datetime(df["time"], errors="coerce")

    # Filter na deň
    if day:
        df = df[df["_t"].dt.date.astype(str) == day]
        if len(df) == 0:
            print(f"[ERR] Žiadne dáta pre {day}")
            return 2

    print(f"=== Verifikácia: profil={profile}, day={day or 'celé CSV'}, rows={len(df)} ===\n")

    # 1) Centrálna metóda (Excel + UI karta)
    totals = compute_effect_totals(df, profile=profile, day=day)
    dt_central = totals["dt_eur"]
    rt_central = totals["rt_eur"]
    vdt_central = totals["vdt_arb_eur"]
    total_central = totals["total_eur"]

    print(f"[A] core.effect.compute_effect_totals():")
    print(f"    dt_eur:       {dt_central:>12.2f} EUR")
    print(f"    rt_eur:       {rt_central:>12.2f} EUR")
    print(f"    vdt_arb_eur:  {vdt_central:>12.2f} EUR")
    print(f"    total_eur:    {total_central:>12.2f} EUR")
    print(f"    rt_col_used:  {totals['rt_col_used']}")
    print()

    # 2) Priamy súčet cez get_rt_eur_series
    rt_series = float(get_rt_eur_series(df).sum())
    print(f"[B] get_rt_eur_series().sum():")
    print(f"    rt_eur:       {rt_series:>12.2f} EUR")
    print()

    # 3) Sanity: priamy súčet kolumny ktorá je resolved
    rt_col = resolve_rt_col(df)
    rt_direct = float(pd.to_numeric(df[rt_col], errors="coerce").fillna(0).sum())
    print(f"[C] df['{rt_col}'].sum():")
    print(f"    rt_eur:       {rt_direct:>12.2f} EUR")
    print()

    # 4) Cumulative path
    cum = compute_effect_cumulative(df, profile=profile)
    rt_cum = float(cum["cum_rt"].iloc[-1]) if len(cum) else 0.0
    dt_cum = float(cum["cum_dt"].iloc[-1]) if len(cum) else 0.0
    total_cum = float(cum["cum_total"].iloc[-1]) if len(cum) else 0.0
    print(f"[D] compute_effect_cumulative()['cum_*'].iloc[-1]:")
    print(f"    cum_dt:       {dt_cum:>12.2f} EUR")
    print(f"    cum_rt:       {rt_cum:>12.2f} EUR")
    print(f"    cum_total:    {total_cum:>12.2f} EUR")
    print()

    # ── Porovnanie ────────────────────────────────────────────────────
    errors = []
    if abs(rt_central - rt_series) > TOL:
        errors.append(f"  [A] vs [B] RT mismatch: {rt_central:.4f} vs {rt_series:.4f}")
    if abs(rt_central - rt_direct) > TOL:
        errors.append(f"  [A] vs [C] RT mismatch: {rt_central:.4f} vs {rt_direct:.4f}")
    if abs(rt_central - rt_cum) > TOL:
        errors.append(f"  [A] vs [D] RT mismatch: {rt_central:.4f} vs {rt_cum:.4f}")
    if abs(dt_central - dt_cum) > TOL:
        errors.append(f"  [A] vs [D] DT mismatch: {dt_central:.4f} vs {dt_cum:.4f}")
    if abs(total_central - total_cum) > TOL:
        errors.append(f"  [A] vs [D] TOTAL mismatch: {total_central:.4f} vs {total_cum:.4f}")

    if errors:
        print("❌ MISMATCH:")
        for e in errors:
            print(e)
        return 1

    print("✅ KONZISTENCIA OK — všetky 4 zobrazenia ukazujú rovnaké čísla.")
    print()
    print("Záver:")
    print(f"  Zisk SPOLU za vybrané obdobie: {total_central:+.2f} EUR")
    print(f"    z toho DT:                   {dt_central:+.2f} EUR")
    print(f"    z toho odchýlka (RT):        {rt_central:+.2f} EUR  (cez {totals['rt_col_used']})")
    print(f"    z toho VDT arbitráž:         {vdt_central:+.2f} EUR")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default="/app/out/sk/livesim_plan_d1.csv",
                    help="cesta k livesim CSV")
    p.add_argument("--profile", default="VW_simulacia",
                    help="profil pre VDT arbitráž lookup")
    p.add_argument("--day", default=None,
                    help="deň YYYY-MM-DD (default: celé CSV)")
    args = p.parse_args()
    sys.exit(verify(args.csv, args.profile, args.day))


if __name__ == "__main__":
    main()
