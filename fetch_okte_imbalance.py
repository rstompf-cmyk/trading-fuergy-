# -*- coding: utf-8 -*-
"""
fetch_okte_imbalance.py — denný batch fetcher SK imbalance dát z OKTE/ISZO.

Stiahne D-1 (alebo zadaný rozsah dní) z https://iszo.okte.sk/api/v1/SystemImbalance
+ day-ahead ceny z https://isot.okte.sk/api/v1/dam/results, spojí 15-min za deň
a appenduje do out/sk/imbalance_history.csv.

Pokrytie:
  • DT cena (OKTE day-ahead) — 15-min od MTU projektu (2024+)
  • ZCO (system imbalance price) — z evaluationType=preliminarydaily, dostupné ~11:30 D+1
  • Sys odchýlka v MWh za 15-min slot
  • Kladné/záporné zložky odchýlky (pi/ni) + regulačná elektrina (pre/nre)

Použitie
--------
  # default: stiahne včerajšok a pridá do CSV
  python fetch_okte_imbalance.py

  # explicit rozsah:
  python fetch_okte_imbalance.py --from 2026-05-01 --to 2026-05-26
  python fetch_okte_imbalance.py --from 2026-05-01 --to 2026-05-26 --eval final

Cron príklad (každý deň o 11:35 zachytíme včerajšok):
  35 11 * * * cd /cesta/k/Aplikacia && /usr/bin/python3 fetch_okte_imbalance.py >> out/sk/fetch.log 2>&1
"""
from __future__ import annotations
import argparse
import os
import sys
import datetime as dt
import pandas as pd
import numpy as np

import okte_sk

OUT_CSV = "out/sk/imbalance_history.csv"


def _slot_ts(date_iso: str, period: int) -> pd.Timestamp:
    """Period 1 = 00:00, Period 2 = 00:15, …, 96 = 23:45 (15-min sloty)."""
    start_min = (max(1, int(period)) - 1) * 15
    return pd.Timestamp(date_iso) + pd.Timedelta(minutes=start_min)


def fetch_day_combined(date: dt.date, evaluation_type: str = "preliminarydaily") -> pd.DataFrame:
    """Spojí DAM (price) + ISZO imbalance (zco, sys-MWh, RE) za jeden deň → 96 × 15-min riadkov.

    Vracia DataFrame so stĺpcami: ts, date, period, isot_eur (DT cena), zco_eur (= isp),
    sys_MWh, pi_MWh, ni_MWh, pre_MWh, nre_MWh, mppre_EUR, mpnre_EUR, srec_EUR,
    pspre_EUR, psnre_EUR, emergency, evaluation_type, evaluation_date.
    """
    # 1) DAM ceny (sk day-ahead clearing)
    try:
        dam = okte_sk.fetch_okte_dayahead(date)
        # podpora 15-min aj hodinového rozlíšenia (legacy)
        dam = dam[["date", "period", "cena_EUR"]].rename(columns={"cena_EUR": "isot_eur"})
    except Exception as e:
        print(f"[{date}] DAM fetch zlyhal: {e}")
        dam = pd.DataFrame(columns=["date", "period", "isot_eur"])

    # 2) ISZO imbalance
    try:
        ibe = okte_sk.fetch_okte_imbalance(date, date, evaluation_type)
    except Exception as e:
        print(f"[{date}] ISZO imbalance zlyhal: {e}")
        ibe = pd.DataFrame()

    if ibe.empty and dam.empty:
        raise RuntimeError(f"[{date}] obidva endpoints zlyhali")

    # mergeneme po (date, period). DAM ma 96 (alebo 24 legacy), ISZO ma 96.
    base = ibe.copy() if not ibe.empty else dam.copy()
    if not ibe.empty and not dam.empty:
        merged = ibe.merge(dam, on=["date", "period"], how="outer")
    else:
        merged = base
        if "isot_eur" not in merged.columns:
            merged["isot_eur"] = np.nan

    # výsledne mapovanie polí (skratky pre kompatibilitu s livesim/RT controller)
    merged["zco_eur"] = merged.get("isp")                                     # ZCO = isp (system imbalance price)
    merged["sys_MWh"] = merged.get("si")
    merged["pi_MWh"] = merged.get("pi")
    merged["ni_MWh"] = merged.get("ni")
    merged["pre_MWh"] = merged.get("pre")
    merged["nre_MWh"] = merged.get("nre")
    merged["mppre_EUR"] = merged.get("mppre")
    merged["mpnre_EUR"] = merged.get("mpnre")
    merged["srec_EUR"] = merged.get("srec")
    merged["pspre_EUR"] = merged.get("pspre")
    merged["psnre_EUR"] = merged.get("psnre")
    merged["evaluation_type"] = merged.get("evaluationType")
    merged["evaluation_date"] = merged.get("evaluationDate")
    merged["emergency"] = merged.get("emergency", False)

    # ts (lokálny SK čas zo (date, period))
    merged["ts"] = [
        _slot_ts(d, p) for d, p in zip(merged["date"].astype(str), merged["period"].astype(int))
    ]
    keep = ["ts", "date", "period", "isot_eur", "zco_eur", "sys_MWh",
            "pi_MWh", "ni_MWh", "pre_MWh", "nre_MWh",
            "mppre_EUR", "mpnre_EUR", "srec_EUR", "pspre_EUR", "psnre_EUR",
            "emergency", "evaluation_type", "evaluation_date"]
    out = merged[[c for c in keep if c in merged.columns]].sort_values(["date", "period"]).reset_index(drop=True)
    return out


def upsert_csv(new_df: pd.DataFrame, csv_path: str = OUT_CSV) -> tuple[int, int]:
    """Pripojí nové riadky a deduplikuje (podľa ts). Vracia (n_pridanych, n_v_subore)."""
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    if os.path.exists(csv_path):
        existing = pd.read_csv(csv_path)
        # zlúčiť, deduplikovať podľa ts (preferuj nový — najnovšie evaluation)
        existing["ts"] = pd.to_datetime(existing["ts"])
        new_df = new_df.copy()
        new_df["ts"] = pd.to_datetime(new_df["ts"])
        combined = pd.concat([existing, new_df], ignore_index=True)
        # ak ts duplicate, zachovaj NOVŠÍ (drop_duplicates keep='last' po sortovaní podľa ts a evaluation rank)
        rank = {"preliminarydaily": 1, "regulardaily": 2, "decadal": 3, "monthly": 4, "final": 5}
        combined["_rank"] = combined.get("evaluation_type", "preliminarydaily").map(rank).fillna(0)
        combined = (combined.sort_values(["ts", "_rank"], ascending=[True, True])
                            .drop_duplicates(subset=["ts"], keep="last")
                            .drop(columns="_rank")
                            .reset_index(drop=True))
        added = len(combined) - len(existing)
    else:
        combined = new_df
        added = len(combined)
    combined.to_csv(csv_path, index=False)
    return added, len(combined)


def main():
    ap = argparse.ArgumentParser(description="Stiahne SK imbalance dáta z OKTE/ISZO a appendne do CSV.")
    ap.add_argument("--from", dest="d_from", type=str, default=None,
                     help="ISO dátum od (default: včera). Príklad: 2026-05-01")
    ap.add_argument("--to", dest="d_to", type=str, default=None,
                     help="ISO dátum do (default: =from)")
    ap.add_argument("--eval", dest="eval_type", type=str, default="preliminarydaily",
                     choices=["preliminarydaily", "regulardaily", "decadal", "monthly", "final"],
                     help="evaluationType pre ISZO endpoint (default: preliminarydaily — dostupné po ~11:30 D+1)")
    ap.add_argument("--out", dest="out_csv", type=str, default=OUT_CSV,
                     help=f"výstupný CSV (default {OUT_CSV})")
    args = ap.parse_args()

    today = dt.date.today()
    d_from = (dt.date.fromisoformat(args.d_from) if args.d_from
              else today - dt.timedelta(days=1))
    d_to = dt.date.fromisoformat(args.d_to) if args.d_to else d_from
    if d_to < d_from:
        print(f"⚠ --to {d_to} je pred --from {d_from}, vymieňam")
        d_from, d_to = d_to, d_from

    print(f"━━━ Fetch OKTE/ISZO  {d_from} → {d_to}  ({args.eval_type}) ━━━")
    all_rows = []
    for d in pd.date_range(d_from, d_to, freq="D"):
        d_obj = d.date()
        try:
            df = fetch_day_combined(d_obj, args.eval_type)
            if df.empty:
                print(f"  ⊘ {d_obj}: prázdne (ešte nepublikované?)")
                continue
            all_rows.append(df)
            n_isp = df["zco_eur"].notna().sum()
            n_dam = df["isot_eur"].notna().sum()
            print(f"  ✓ {d_obj}: {len(df)} slotov  (DAM cien: {n_dam}, ZCO/isp cien: {n_isp})")
        except Exception as e:
            print(f"  ✗ {d_obj}: {e}")
    if not all_rows:
        print("Žiadny deň sa nestiahol. Možné príčiny: nepublikované, sieť, zlý evaluationType.")
        sys.exit(1)
    full = pd.concat(all_rows, ignore_index=True)
    added, total = upsert_csv(full, args.out_csv)
    print(f"\n━━━ Uložené: {args.out_csv}  (+{added} riadkov, total {total}) ━━━")


if __name__ == "__main__":
    main()
