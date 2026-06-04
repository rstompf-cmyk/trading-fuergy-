# -*- coding: utf-8 -*-
"""
historian_backfill.py — Stiahne historické dáta z firemného historiana do CSV.

API má limit ~998 bodov per request, takže paginujeme cez time-okná
(default: chunky po 1 dni — pri 3-min tagoch je to ~480 bodov, OK).

Použitie:
  # Default: 4 tagy SK trhu od začiatku 2026 do dnes
  python3 historian_backfill.py

  # Custom rozsah + tagy
  python3 historian_backfill.py --from 2026-01-01 --to 2026-05-28 \\
      --tags I_WEB_DAMAS_ReWithGCC_3m,C_OKTE_ISOT_15m_final

  # Jeden tag, 1 deň (test)
  python3 historian_backfill.py --from 2026-05-27 --to 2026-05-28 \\
      --tags I_WEB_DAMAS_ReWithGCC_3m

  # Custom output adresár
  python3 historian_backfill.py --out out/sk/historian_backfill/

Output:
  Per tag ide CSV → out/sk/historian_<TAG_NAME>.csv
  Schema: time_utc, value, min, max
  Idempotent: pri opakovanom spustení dedup-uje podľa time_utc.
"""
from __future__ import annotations
import argparse
import os
import sys
import datetime as dt
from typing import List

# Auto-load .env aby HISTORIAN_PASSWORD bol dostupný aj bez `export`
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import pandas as pd

import internal_historian as ih


# Default SK tag set
DEFAULT_TAGS = [
    "I_WEB_DAMAS_ReWithGCC_3m",        # SEPS reg.výkon (3 min)
    "C_WEB_OKTE_ISOT_15m",              # OKTE denný trh (15 min, kompletný 96 slotov/deň)
    "I_WEB_OKTE_ZCO_15m",               # Zúčtovacia cena odchýlky (15 min)
    "I_WEB_OKTE_VDT_final_15m",         # VDT finálne (15 min)
    "I_OKTE_ISOT_VDT_15m",              # VDT predbežné (15 min, continuous do konca dňa)
]


def _csv_path_for_tag(tag: str, out_dir: str) -> str:
    """Bezpečný názov súboru pre tag."""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in tag)
    return os.path.join(out_dir, f"historian_{safe}.csv")


def _existing_times(csv_path: str) -> set:
    """Načíta time_utc stĺpec z existujúceho CSV — pre dedup."""
    if not os.path.exists(csv_path):
        return set()
    try:
        df = pd.read_csv(csv_path, usecols=["time_utc"])
        return set(df["time_utc"].astype(str).tolist())
    except Exception:
        return set()


def backfill_tag(h: ih.Historian, tag: str,
                  from_dt: dt.datetime, to_dt: dt.datetime,
                  out_dir: str = "out/sk",
                  chunk_days: int = 1,
                  count_per_chunk: int = 998,
                  verbose: bool = True) -> int:
    """Stiahne 1 tag po chunk-och, deduplikuje, appendne do CSV. Vracia # nových riadkov."""
    csv_path = _csv_path_for_tag(tag, out_dir)
    os.makedirs(out_dir, exist_ok=True)
    existing = _existing_times(csv_path)

    def log(msg):
        if verbose:
            print(f"[backfill/{tag}] {msg}", flush=True)

    total_new = 0
    chunks_total = 0
    chunks_done = 0
    cur = from_dt
    chunk_delta = dt.timedelta(days=chunk_days)

    # Count chunks (cosmetic for progress)
    _cnt = from_dt
    while _cnt < to_dt:
        chunks_total += 1
        _cnt += chunk_delta

    log(f"štart {from_dt:%Y-%m-%d} → {to_dt:%Y-%m-%d}  ({chunks_total} chunky × {chunk_days}d)")

    while cur < to_dt:
        chunk_end = min(cur + chunk_delta, to_dt)
        try:
            df = h.fetch_history([tag], cur, chunk_end, count=count_per_chunk)
        except Exception as e:
            log(f"  ✗ {cur:%Y-%m-%d}: {e}")
            cur = chunk_end
            continue

        if df.empty:
            chunks_done += 1
            cur = chunk_end
            continue

        # Convert to CSV schema + dedup
        df = df[df["tag"] == tag].copy()
        df["time_utc"] = df["time"].dt.strftime("%Y-%m-%d %H:%M:%S")
        df = df[["time_utc", "value", "min", "max"]]
        df = df[~df["time_utc"].isin(existing)]                 # dedup

        if not df.empty:
            mode = "a" if os.path.exists(csv_path) else "w"
            header = not os.path.exists(csv_path)
            df.to_csv(csv_path, mode=mode, header=header, index=False)
            existing.update(df["time_utc"].tolist())
            total_new += len(df)

        chunks_done += 1
        if chunks_done % 10 == 0 or chunks_done == chunks_total:
            log(f"  ... {chunks_done}/{chunks_total} chunkov, total +{total_new} riadkov")
        cur = chunk_end

    log(f"hotovo: +{total_new} nových riadkov → {csv_path}")
    return total_new


def _last_row_time(csv_path: str) -> dt.datetime:
    """Vráti najnovší timestamp v CSV (UTC). None ak CSV chýba/prázdne."""
    if not os.path.exists(csv_path):
        return None
    try:
        # tail-read posledných pár riadkov — efektívnejšie ako pd.read_csv pre veľké súbory
        with open(csv_path, "rb") as f:
            try:
                f.seek(-4096, os.SEEK_END)
            except OSError:
                f.seek(0)
            chunk = f.read().decode("utf-8", errors="ignore")
        lines = [ln for ln in chunk.strip().split("\n") if ln and not ln.startswith("time_utc")]
        if not lines:
            return None
        last_ts_str = lines[-1].split(",")[0].strip()
        return dt.datetime.fromisoformat(last_ts_str).replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def extend_tag_to_now(h: ih.Historian, tag: str, out_dir: str = "out/sk",
                       safety_overlap_min: int = 10, verbose: bool = False,
                       fetch_to_end_of_day: bool = True) -> int:
    """Incremental sync — od posledného riadku v CSV.

    `fetch_to_end_of_day=True` (default): fetchuje až do **konca zajtrajška** UTC.
    Tak zachytíme aj DAM ceny (publikované D-1 a obsahujú celý zajtrajší deň)
    a iné tagy ktoré majú „budúce" hodnoty (VDT predbežné, atď.).

    `safety_overlap_min`: re-fetch posledných ~10 min nech sa pokryjú prípadné
    neskoré samples (dedup ich zahodí).
    """
    csv_path = _csv_path_for_tag(tag, out_dir)
    last_ts = _last_row_time(csv_path)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    if fetch_to_end_of_day:
        # do konca zajtrajška (zachytí DAM publikované D-1)
        tomorrow_end = (now.replace(hour=0, minute=0, second=0)
                        + dt.timedelta(days=2))                         # zajtra 23:59 UTC + 1min buffer
        to_dt = tomorrow_end
    else:
        to_dt = now

    if last_ts is None:
        from_dt = now - dt.timedelta(days=1)
    else:
        from_dt = last_ts - dt.timedelta(minutes=safety_overlap_min)
    if (to_dt - from_dt).total_seconds() < 60:
        return 0
    return backfill_tag(h, tag, from_dt, to_dt, out_dir=out_dir,
                          chunk_days=1, verbose=verbose)


def main():
    ap = argparse.ArgumentParser(description="Backfill historických dát z firemného historiana.")
    ap.add_argument("--from", dest="d_from", default="2026-01-01",
                     help="Začiatok rozsahu (YYYY-MM-DD, default 2026-01-01)")
    ap.add_argument("--to", dest="d_to", default=None,
                     help="Koniec rozsahu (YYYY-MM-DD, default = dnes)")
    ap.add_argument("--tags", default=",".join(DEFAULT_TAGS),
                     help=f"Čiarkou oddelené tagy (default: {len(DEFAULT_TAGS)} SK tagov)")
    ap.add_argument("--out", default="out/sk",
                     help="Output adresár (default out/sk)")
    ap.add_argument("--chunk-days", type=int, default=1,
                     help="Veľkosť chunk-u v dňoch (default 1; 3-min tag = ~480 b/d)")
    args = ap.parse_args()

    d_from = dt.datetime.fromisoformat(args.d_from).replace(tzinfo=dt.timezone.utc)
    if args.d_to:
        d_to = dt.datetime.fromisoformat(args.d_to).replace(tzinfo=dt.timezone.utc)
    else:
        d_to = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    if not tags:
        print("Žiadne tagy. Použi --tags TAG1,TAG2,...", file=sys.stderr)
        sys.exit(1)

    print(f"━━━ Historian backfill {d_from:%Y-%m-%d} → {d_to:%Y-%m-%d}, {len(tags)} tagov ━━━")
    print(f"Tagy: {tags}")
    print(f"Output: {args.out}")
    print()

    h = ih.Historian()
    grand_total = 0
    for tag in tags:
        new = backfill_tag(h, tag, d_from, d_to,
                            out_dir=args.out, chunk_days=args.chunk_days)
        grand_total += new
        print()

    print(f"━━━ Hotovo: +{grand_total} riadkov celkom v {len(tags)} CSV súboroch ━━━")


if __name__ == "__main__":
    main()
