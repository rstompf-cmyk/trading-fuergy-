#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DB unify F3: backfill efektových tabuliek z existujúcich livesim CSV.

Pre každý profil + market:
1. Načíta `out/<market>/livesim_plan_d1_<port>.csv` (a/alebo dt_15min)
2. Volá `effect_db.upsert_minute_batch` po dňoch
3. Volá `effect_db.upsert_daily` zo `compute_day_totals_from_df` agregátu
4. Validuje: SUM(DB.dt_rev_eur) vs SUM(CSV.dt_rev_min) per profil — toleranca 0.01 €

Použitie:
    python3 -m tools.backfill_effect_db                          # všetky profily, CZ aj SK
    python3 -m tools.backfill_effect_db --profile VW_simulacia   # konkrétny profil
    python3 -m tools.backfill_effect_db --market sk              # len SK
    python3 -m tools.backfill_effect_db --from 2026-05-01 --to 2026-06-09
    python3 -m tools.backfill_effect_db --dry-run                # nezapisuje, len reportuje
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

# Boot aplikačnej cesty
_HERE = Path(__file__).resolve().parent
_APP = _HERE.parent
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

import profiles as pr
from core import effect_db
from core.paths import livesim_csv_path


def _iter_profile_csvs(profile: str, market: str) -> List[Tuple[str, str]]:
    """Vráti [(case, csv_path), ...] dostupné pre profil+market.

    Skúša 3 varianty cesty: per-port (_8000.csv), bez portu (.csv), per-profile
    podadresár — kvôli legacy variantom v rôznych deploy konfiguráciách.
    """
    out = []
    from core.paths import _data_dir
    data_dir = _data_dir(market)
    for case in ("plan_d1", "dt_15min"):
        candidates = [
            os.path.join(data_dir, f"livesim_{case}_8000.csv"),    # per-port
            os.path.join(data_dir, f"livesim_{case}.csv"),         # shared (legacy)
            os.path.join(data_dir, profile, f"livesim_{case}.csv"),# per-profile sandbox
        ]
        for csv in candidates:
            if os.path.exists(csv) and os.path.getsize(csv) > 100:
                out.append((case, csv))
                break  # prvý nájdený stačí
    return out


def _load_csv(csv_path: str, date_from: Optional[str],
                date_to: Optional[str]) -> pd.DataFrame:
    try:
        df = pd.read_csv(csv_path, low_memory=False)
    except Exception as e:
        print(f"   ⚠ načítanie {csv_path} zlyhalo: {e}")
        return pd.DataFrame()
    if df.empty or "time" not in df.columns:
        return pd.DataFrame()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"])
    if date_from:
        df = df[df["time"] >= pd.Timestamp(date_from)]
    if date_to:
        df = df[df["time"] <= pd.Timestamp(date_to) + pd.Timedelta(days=1)]
    return df


def _backfill_profile(profile: str, market: str, date_from: Optional[str],
                        date_to: Optional[str], dry_run: bool) -> dict:
    stats = {"profile": profile, "market": market, "minutes": 0, "days": 0,
              "sum_dt_csv": 0.0, "sum_dt_db_check": 0.0, "files": []}
    csvs = _iter_profile_csvs(profile, market)
    if not csvs:
        return stats

    for case, csv_path in csvs:
        df = _load_csv(csv_path, date_from, date_to)
        if df.empty:
            continue
        stats["files"].append(f"{case}:{os.path.basename(csv_path)}({len(df)})")

        if dry_run:
            stats["minutes"] += len(df)
            stats["days"] += df["time"].dt.date.nunique()
            if "dt_rev_min" in df.columns:
                stats["sum_dt_csv"] += float(
                    pd.to_numeric(df["dt_rev_min"], errors="coerce").fillna(0).sum())
            continue

        # Real zápis — per deň pre upsert_daily granularitu
        df["_date"] = df["time"].dt.date
        for day, day_df in df.groupby("_date"):
            n = effect_db.upsert_minute_batch(profile, market, day_df.drop(columns="_date"))
            totals = effect_db.compute_day_totals_from_df(day_df)
            effect_db.upsert_daily(profile, str(day), market, totals)
            stats["minutes"] += n
            stats["days"] += 1

        if "dt_rev_min" in df.columns:
            stats["sum_dt_csv"] += float(
                pd.to_numeric(df["dt_rev_min"], errors="coerce").fillna(0).sum())

    # Validácia: SUM v DB pre profil
    if not dry_run and date_from and date_to:
        period = effect_db.get_period_effect(profile, date_from, date_to,
                                                joint_flags=None)
        stats["sum_dt_db_check"] = period["dt_eur"]
    return stats


def main():
    p = argparse.ArgumentParser(description="Backfill effect_db z livesim CSV")
    p.add_argument("--profile", help="Konkrétny profil (default: všetky)")
    p.add_argument("--market", choices=("cz", "sk", "both"), default="both")
    p.add_argument("--from", dest="date_from", help="YYYY-MM-DD od")
    p.add_argument("--to", dest="date_to", help="YYYY-MM-DD do")
    p.add_argument("--dry-run", action="store_true",
                   help="Nezapisuje, len reportuje čo by zapísal")
    args = p.parse_args()

    profiles = [args.profile] if args.profile else pr.list_profiles()
    markets = ["cz", "sk"] if args.market == "both" else [args.market]

    print(f"📂 Backfill: profily={len(profiles)} markety={markets} "
          f"od={args.date_from} do={args.date_to} dry_run={args.dry_run}")
    print("─" * 70)

    grand = {"minutes": 0, "days": 0, "files": 0}
    for prof in profiles:
        for mk in markets:
            stats = _backfill_profile(prof, mk, args.date_from, args.date_to,
                                        args.dry_run)
            if not stats["files"]:
                continue
            print(f"  {prof}/{mk}: dní={stats['days']} minút={stats['minutes']} "
                  f"súbory={stats['files']}")
            if stats["sum_dt_csv"] or stats["sum_dt_db_check"]:
                delta = abs(stats["sum_dt_csv"] - stats["sum_dt_db_check"])
                tag = "✓" if delta < 0.01 else "⚠"
                print(f"     {tag} validácia DT: CSV={stats['sum_dt_csv']:.2f} € "
                      f"DB={stats['sum_dt_db_check']:.2f} € Δ={delta:.4f}")
            grand["minutes"] += stats["minutes"]
            grand["days"] += stats["days"]
            grand["files"] += len(stats["files"])

    print("─" * 70)
    print(f"🏁 SPOLU: súborov={grand['files']} dní={grand['days']} "
          f"minút={grand['minutes']}")
    if args.dry_run:
        print("   (dry-run — žiadne dáta v DB sa nezmenili)")


if __name__ == "__main__":
    main()
