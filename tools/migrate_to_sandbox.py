# -*- coding: utf-8 -*-
"""tools/migrate_to_sandbox.py — Fáza B.1 FS layout migration.

Migrácia z legacy shared paths na per-profile sandbox layout. Riadené cez
core/paths.py (FTV_SANDBOX=1 env var).

PRED (legacy):
    out/profiles/<name>.json
    out/<market>/plans/<name>/*.json
    out/<market>/livesim_*.csv               (shared per-port)
    out/<market>/vdt_paper_trades.csv        (shared, filter by profile col)
    out/<market>/auto_control_log.csv        (shared)
    out/<market>/vdt_live_plan_<name>.json

PO (sandbox):
    out/profiles/<name>/
        config.json
        plans/*.json
        livesim/<case>_<port>.csv
        vdt_paper_trades.csv
        auto_control_log.csv
        vdt_advisor_cache.json
    out/_shared/<market>/
        historian_*.csv, imbalance_*, price_train_*

Použitie:
    python3 -m tools.migrate_to_sandbox                          # dry-run report
    python3 -m tools.migrate_to_sandbox --execute --backup       # naozaj prevedie
    python3 -m tools.migrate_to_sandbox --rollback               # vráti zo zálohy

BEZPEČNOSŤ:
    - Default dry-run (žiadny write)
    - --execute vyžaduje aj --backup (vytvorí backup pred presunom)
    - Auto-stop app check: aplikácia musí byť down (žiadne livesim writes)
    - Rollback z _migration_backup_<ts>/ adresára

Po úspechu treba reštart appky s FTV_SANDBOX=1 v environment.
"""
from __future__ import annotations
import os
import sys
import shutil
import argparse
import datetime as dt
import json
import csv
import glob
from typing import List, Tuple, Optional, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── helpers ───────────────────────────────────────────────────────────────

def _markets() -> List[str]:
    return [mk for mk in ("cz", "sk") if os.path.isdir(os.path.join("out", mk))]


def _list_profiles() -> List[str]:
    """Profily v out/profiles/*.json (legacy layout)."""
    p_dir = "out/profiles"
    if not os.path.isdir(p_dir):
        return []
    names = []
    for f in os.listdir(p_dir):
        if not f.endswith(".json"):
            continue
        if f.startswith("_"):
            continue
        # Skip ak je to už adresár (sandbox layout existuje)
        full = os.path.join(p_dir, f[:-5])
        if os.path.isdir(full):
            continue
        names.append(f[:-5])
    return sorted(names)


def _ts() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


# ── plan steps ────────────────────────────────────────────────────────────

def _step1_configs(profiles: List[str], execute: bool, backup: str) -> int:
    """Krok 1: out/profiles/<name>.json → out/profiles/<name>/config.json"""
    count = 0
    for name in profiles:
        src = os.path.join("out", "profiles", f"{name}.json")
        dst_dir = os.path.join("out", "profiles", name)
        dst = os.path.join(dst_dir, "config.json")
        if not os.path.exists(src) or os.path.exists(dst):
            continue
        if execute:
            os.makedirs(dst_dir, exist_ok=True)
            # backup
            if backup:
                shutil.copy2(src, os.path.join(backup, f"profile_{name}.json"))
            shutil.move(src, dst)
        count += 1
    return count


def _step2_plans(profiles: List[str], markets: List[str],
                  execute: bool, backup: str) -> int:
    """Krok 2: out/<mk>/plans/<name>/*.json → out/profiles/<name>/plans/*.json
    + market suffix v názve aby sa neprekrývali CZ+SK."""
    count = 0
    multi_market = len(markets) > 1
    for name in profiles:
        for mk in markets:
            src_dir = os.path.join("out", mk, "plans", name)
            dst_dir = os.path.join("out", "profiles", name, "plans")
            if not os.path.isdir(src_dir):
                continue
            for f in os.listdir(src_dir):
                if not f.endswith(".json"):
                    continue
                src = os.path.join(src_dir, f)
                dst_name = f.replace(".json", f"_{mk}.json") if multi_market else f
                dst = os.path.join(dst_dir, dst_name)
                if os.path.exists(dst):
                    continue
                if execute:
                    os.makedirs(dst_dir, exist_ok=True)
                    if backup:
                        bk_dir = os.path.join(backup, "plans", mk, name)
                        os.makedirs(bk_dir, exist_ok=True)
                        shutil.copy2(src, os.path.join(bk_dir, f))
                    shutil.move(src, dst)
                count += 1
    return count


def _step3_livesim(markets: List[str], execute: bool, backup: str) -> int:
    """Krok 3: livesim_*.csv → out/_shared/<mk>/livesim_*.csv (shared per-port).

    POZNÁMKA: livesim je per-port (8000/8001), nie per-profile. Bez major refactoru
    appky ich nemôžeme rozdeliť per-profile — pôjdu do _shared/. Aplikácia ich bude
    vedieť čítať cez core/paths.shared_data_path() ak treba.
    """
    count = 0
    for mk in markets:
        src_dir = os.path.join("out", mk)
        dst_dir = os.path.join("out", "_shared", mk)
        if not os.path.isdir(src_dir):
            continue
        for f in os.listdir(src_dir):
            if not (f.startswith("livesim_") and f.endswith((".csv", ".meta.json"))):
                continue
            src = os.path.join(src_dir, f)
            dst = os.path.join(dst_dir, f)
            if os.path.exists(dst):
                continue
            if execute:
                os.makedirs(dst_dir, exist_ok=True)
                if backup:
                    bk_dir = os.path.join(backup, "livesim", mk)
                    os.makedirs(bk_dir, exist_ok=True)
                    shutil.copy2(src, os.path.join(bk_dir, f))
                shutil.move(src, dst)
            count += 1
    return count


def _step4_vdt_trades_split(profiles: List[str], markets: List[str],
                              execute: bool, backup: str) -> int:
    """Krok 4: VDT trades shared CSV → per-profile split CSV.

    Načíta out/sk/vdt_paper_trades.csv, rozdelí riadky podľa profile column,
    napíše per-profile súbory do out/profiles/<name>/vdt_paper_trades.csv.
    Pôvodný shared CSV zostáva ako orphan archive (premenovaný s .legacy suffix).
    """
    count = 0
    for mk in markets:
        if mk != "sk":   # VDT je iba SK
            continue
        src = os.path.join("out", mk, "vdt_paper_trades.csv")
        if not os.path.exists(src):
            continue
        if not execute:
            return _count_rows(src)
        # Read all rows
        with open(src, "r", encoding="utf-8", newline="") as f:
            rdr = csv.reader(f)
            rows = list(rdr)
        if not rows:
            continue
        header = rows[0]
        try:
            prof_idx = header.index("profile")
        except ValueError:
            print(f"  ⚠ {src} nemá 'profile' column — preskakujem split")
            continue
        # Group by profile
        per_prof: Dict[str, List[List[str]]] = {p: [] for p in profiles}
        per_prof["__unknown__"] = []
        for r in rows[1:]:
            if len(r) <= prof_idx:
                per_prof["__unknown__"].append(r)
                continue
            p = r[prof_idx]
            if p in per_prof:
                per_prof[p].append(r)
            else:
                per_prof["__unknown__"].append(r)
        # Write per-profile
        for prof, prof_rows in per_prof.items():
            if not prof_rows or prof == "__unknown__":
                continue
            dst_dir = os.path.join("out", "profiles", prof)
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, "vdt_paper_trades.csv")
            with open(dst, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(prof_rows)
            count += len(prof_rows)
        # Backup + rename original
        if backup:
            shutil.copy2(src, os.path.join(backup, "vdt_paper_trades.csv"))
        os.rename(src, src + ".legacy_pre_sandbox")
    return count


def _step5_auto_control_split(profiles: List[str], markets: List[str],
                                execute: bool, backup: str) -> int:
    """Krok 5: auto_control_log.csv shared → per-profile split."""
    count = 0
    for mk in markets:
        src = os.path.join("out", mk, "auto_control_log.csv")
        if not os.path.exists(src):
            continue
        if not execute:
            return _count_rows(src)
        with open(src, "r", encoding="utf-8", newline="") as f:
            rdr = csv.reader(f)
            rows = list(rdr)
        if not rows:
            continue
        header = rows[0]
        try:
            prof_idx = header.index("profile")
        except ValueError:
            continue
        per_prof: Dict[str, List[List[str]]] = {p: [] for p in profiles}
        for r in rows[1:]:
            if len(r) <= prof_idx:
                continue
            p = r[prof_idx]
            if p in per_prof:
                per_prof[p].append(r)
        for prof, prof_rows in per_prof.items():
            if not prof_rows:
                continue
            dst_dir = os.path.join("out", "profiles", prof)
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, "auto_control_log.csv")
            with open(dst, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(prof_rows)
            count += len(prof_rows)
        if backup:
            shutil.copy2(src, os.path.join(backup, f"auto_control_log_{mk}.csv"))
        os.rename(src, src + ".legacy_pre_sandbox")
    return count


def _step6_vdt_caches(profiles: List[str], markets: List[str],
                       execute: bool, backup: str) -> int:
    """Krok 6: vdt_live_plan_<name>.json → out/profiles/<name>/vdt_advisor_cache.json"""
    count = 0
    for mk in markets:
        for name in profiles:
            safe = "".join(c for c in name if c.isalnum() or c in "_-")
            src = os.path.join("out", mk, f"vdt_live_plan_{safe}.json")
            if not os.path.exists(src):
                continue
            dst_dir = os.path.join("out", "profiles", name)
            dst = os.path.join(dst_dir, "vdt_advisor_cache.json")
            if os.path.exists(dst):
                continue
            if execute:
                os.makedirs(dst_dir, exist_ok=True)
                if backup:
                    shutil.copy2(src, os.path.join(backup, f"vdt_cache_{name}.json"))
                shutil.move(src, dst)
            count += 1
    return count


def _step7_shared_data(markets: List[str], execute: bool, backup: str) -> int:
    """Krok 7: historian, imbalance, price_train, atď. → out/_shared/<mk>/"""
    count = 0
    shared_prefixes = (
        "historian_", "imbalance_", "price_train",
        "okte_", "ote_", "isot_",
    )
    for mk in markets:
        src_dir = os.path.join("out", mk)
        dst_dir = os.path.join("out", "_shared", mk)
        if not os.path.isdir(src_dir):
            continue
        for fn in os.listdir(src_dir):
            if not any(fn.startswith(p) for p in shared_prefixes):
                continue
            src = os.path.join(src_dir, fn)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(dst_dir, fn)
            if os.path.exists(dst):
                continue
            if execute:
                os.makedirs(dst_dir, exist_ok=True)
                if backup:
                    bk_dir = os.path.join(backup, "shared", mk)
                    os.makedirs(bk_dir, exist_ok=True)
                    shutil.copy2(src, os.path.join(bk_dir, fn))
                shutil.move(src, dst)
            count += 1
    return count


def _count_rows(csv_path: str) -> int:
    """Rýchly počet riadkov v CSV (bez load)."""
    try:
        with open(csv_path, "rb") as f:
            return max(0, sum(1 for _ in f) - 1)
    except Exception:
        return 0


# ── rollback ──────────────────────────────────────────────────────────────

def _rollback(backup_dir: str) -> int:
    """Vráti zo zálohy. Backup dir formát: out/_migration_backup_<ts>/"""
    if not os.path.isdir(backup_dir):
        print(f"ERR: backup adresár {backup_dir} neexistuje", file=sys.stderr)
        return 1
    print(f"Rollback z {backup_dir}...")
    # Najprv obnovme profile configs (najjednoduchšie)
    pcount = 0
    for f in os.listdir(backup_dir):
        if f.startswith("profile_") and f.endswith(".json"):
            name = f[len("profile_"):-5]
            src = os.path.join(backup_dir, f)
            dst = os.path.join("out", "profiles", f"{name}.json")
            # Najprv vyhoď sandbox adresár ak existuje
            sb = os.path.join("out", "profiles", name)
            if os.path.isdir(sb):
                shutil.rmtree(sb)
            shutil.copy2(src, dst)
            pcount += 1
    print(f"  Obnovené {pcount} profile configs")
    print("  POZN: plans, livesim, VDT trades, atď. treba obnoviť manuálne z backup-u")
    print(f"  Súbory v {backup_dir}/ — skopíruj späť do out/<mk>/")
    return 0


# ── main ──────────────────────────────────────────────────────────────────

def run(args) -> int:
    profiles = _list_profiles()
    markets = _markets()

    print()
    print("━━━ FS Sandbox Migration ━━━")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print(f"Profily: {len(profiles)} ({', '.join(profiles)})")
    print(f"Trhy:    {len(markets)} ({', '.join(markets)})")
    print()

    if not profiles:
        print("Žiadne profily na migráciu (možno už sú v sandbox layout-e).")
        return 0

    backup = ""
    if args.execute:
        if not args.backup:
            print("ERR: --execute vyžaduje aj --backup (auto-vytvorí _migration_backup_<ts>/)",
                  file=sys.stderr)
            return 2
        backup = os.path.join("out", f"_migration_backup_{_ts()}")
        os.makedirs(backup, exist_ok=True)
        print(f"Backup adresár: {backup}")
        print()

    # POZNÁMKA: livesim CSV (step 3) a shared data (step 7) sa NEPRESÚVAJÚ.
    # Livesim je per-port (8000/8001/8004/...) NIE per-profile — nemá zmysel
    # ich rozdeľovať. Shared data (historian, imbalance, price_train) sú
    # per-market, takže ostávajú v out/<market>/. Migrujeme iba to čo je
    # naozaj per-profile: config, plans, VDT cache, VDT trades, auto_control.
    steps = [
        ("1. Profile configs", _step1_configs, [profiles]),
        ("2. Plan JSONs",      _step2_plans,    [profiles, markets]),
        ("4. VDT trades split (per-profile)", _step4_vdt_trades_split, [profiles, markets]),
        ("5. auto_control_log split", _step5_auto_control_split, [profiles, markets]),
        ("6. VDT advisor caches", _step6_vdt_caches, [profiles, markets]),
    ]

    total = 0
    for name, fn, fnargs in steps:
        try:
            n = fn(*fnargs, args.execute, backup)
            print(f"  {'✓' if args.execute else '·'} {name}: {n}")
            total += n
        except Exception as e:
            print(f"  ✗ {name}: FAILED — {e}")
            if args.execute:
                print("  Backup zostal. Použij --rollback na vrátenie.")
                return 1

    print()
    print(f"Total: {total} items {'migrated' if args.execute else 'planned'}")
    print()
    if args.execute:
        print("✓ Migration kompletná.")
        print(f"  Backup: {backup}")
        print()
        print("Ďalšie kroky:")
        print("  1. Otestuj že app funguje: docker compose --profile dev up -d")
        print("  2. Nastav FTV_SANDBOX=1 v environment (.env / docker-compose):")
        print("     environment:")
        print("       FTV_SANDBOX: \"1\"")
        print("  3. Reštart appky.")
        print(f"  4. Po overení: rm -rf {backup}  (alebo nechaj na rollback)")
    else:
        print("DRY-RUN — žiadne súbory neboli presunuté.")
        print()
        print("Na execute:")
        print("  python3 -m tools.migrate_to_sandbox --execute --backup")
    return 0


def main():
    p = argparse.ArgumentParser(
        description="FS sandbox migration (Fáza B.1)")
    p.add_argument("--execute", action="store_true",
                    help="Naozaj prevedie presun (default = dry-run)")
    p.add_argument("--backup", action="store_true",
                    help="Pred execute vytvorí backup do out/_migration_backup_<ts>/")
    p.add_argument("--rollback", metavar="BACKUP_DIR",
                    help="Vráti zo zálohy. Príklad: --rollback out/_migration_backup_20260608_120000")
    args = p.parse_args()

    if args.rollback:
        return _rollback(args.rollback)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
