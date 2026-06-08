# -*- coding: utf-8 -*-
"""tools/migrate_to_sandbox.py — Fáza B.1 FS layout migration (dry-run by default).

Cieľ: presunúť dáta z dnešnej shared štruktúry do per-profile sandboxov.

PRED:
    out/<market>/plans/<profile>/                   ← už per-profile
    out/<market>/livesim_plan_d1.csv                ← shared per-port
    out/<market>/livesim_dt_15min_<port>.csv        ← shared per-port
    out/<market>/vdt_paper_trades.csv               ← shared (filter by profile col)
    out/<market>/auto_control_log.csv               ← shared (filter by profile col)
    out/<market>/vdt_live_plan_<profile>.json       ← už per-profile

PO:
    out/profiles/<profile>/
        config.json                                  ← presun z out/profiles/<profile>.json
        plans/<date>_<step>min_<kind>.json
        livesim/plan_d1.csv
        livesim/dt_15min_<port>.csv
        vdt_paper_trades.csv                         ← filtered subset
        auto_control_log.csv                         ← filtered subset
        vdt_advisor_cache.json
    out/_shared/<market>/
        historian_*.csv
        imbalance_minute.csv
        price_train_*.csv
        audit/<date>.jsonl

Použitie:
    python3 -m tools.migrate_to_sandbox            # dry-run (default)
    python3 -m tools.migrate_to_sandbox --execute  # naozaj presunie
    python3 -m tools.migrate_to_sandbox --rollback # vráti zo zálohy

BEZPEČNOSŤ:
    - Default = dry-run (žiadny write)
    - Pred execute treba `--execute --i-have-backup` (dva flagy = ochrana proti omylu)
    - Auto backup do out/_migration_backup_<ts>/ pred presunom
    - Rollback môže byť spustený kým existuje backup adresár

STATUS: pripravené, ale **NEVOLAJ NA PRODUKCII bez review**. Aplikácia musí byť
zastavená pri presune (riziko race condition s background pollerom).
"""
from __future__ import annotations
import os
import sys
import shutil
import argparse
import datetime as dt
import json
import csv
from typing import List, Tuple, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── helpers ───────────────────────────────────────────────────────────────

def _markets() -> List[str]:
    """Vráti zoznam trhov ktoré majú dáta (cz/sk)."""
    out = []
    for mk in ("cz", "sk"):
        if os.path.isdir(os.path.join("out", mk)):
            out.append(mk)
    return out


def _list_profiles() -> List[str]:
    """Profily v out/profiles/*.json (existing format pred migration)."""
    p_dir = "out/profiles"
    if not os.path.isdir(p_dir):
        return []
    names = []
    for f in os.listdir(p_dir):
        if not f.endswith(".json"):
            continue
        if f.startswith("_"):    # _active.json, _active_8000.json
            continue
        names.append(f[:-5])    # strip .json
    return sorted(names)


def _backup_dir() -> str:
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join("out", f"_migration_backup_{ts}")


def _profile_sandbox(name: str) -> str:
    """Cieľová cesta pre profile sandbox."""
    return os.path.join("out", "profiles", name)


def _shared_dir(mk: str) -> str:
    return os.path.join("out", "_shared", mk)


# ── plan steps (každý vracia list of (src, dst) prácí) ────────────────────

def _plan_config(profiles: List[str]) -> List[Tuple[str, str]]:
    """Step 1: out/profiles/<name>.json → out/profiles/<name>/config.json"""
    out = []
    for name in profiles:
        src = os.path.join("out", "profiles", f"{name}.json")
        dst_dir = _profile_sandbox(name)
        dst = os.path.join(dst_dir, "config.json")
        if os.path.exists(src) and not os.path.exists(dst):
            out.append((src, dst))
    return out


def _plan_plans(profiles: List[str], markets: List[str]) -> List[Tuple[str, str]]:
    """Step 2: out/<mk>/plans/<name>/*.json → out/profiles/<name>/plans/*.json"""
    out = []
    for name in profiles:
        for mk in markets:
            src_dir = os.path.join("out", mk, "plans", name)
            dst_dir = os.path.join(_profile_sandbox(name), "plans")
            if not os.path.isdir(src_dir):
                continue
            for f in os.listdir(src_dir):
                if f.endswith(".json"):
                    src = os.path.join(src_dir, f)
                    # Pridaj market suffix aby sa neprekrývali CZ+SK plány
                    if len(markets) > 1:
                        dst_name = f.replace(".json", f"_{mk}.json")
                    else:
                        dst_name = f
                    dst = os.path.join(dst_dir, dst_name)
                    if not os.path.exists(dst):
                        out.append((src, dst))
    return out


def _plan_livesim(markets: List[str]) -> List[Tuple[str, str]]:
    """Step 3: livesim CSVs sú per-port shared → potreba split podľa profilu.

    Pretože livesim CSV v aktuálnom stave je per-port (nie per-profil),
    a aplikácia ich na tomto bode používa, je TOTO najťažší krok migrácie.
    Tu len identifikujeme — actual split CSV-y robí samostatný handler
    (najprv extrahovať per-profile riadky cez CSV reader, potom zapísať).

    Pre dry-run len zoznam súborov pre report.
    """
    out = []
    for mk in markets:
        for fn in os.listdir(os.path.join("out", mk)):
            if fn.startswith("livesim_") and fn.endswith((".csv", ".meta.json")):
                src = os.path.join("out", mk, fn)
                # Plánovaný dst: out/_shared/<mk>/<fn>  (zatiaľ ako shared)
                dst = os.path.join(_shared_dir(mk), fn)
                out.append((src, dst))
    return out


def _plan_vdt_trades(profiles: List[str], markets: List[str]) -> List[Tuple[str, str]]:
    """Step 4: vdt_paper_trades.csv shared → per-profile filtered split.

    Toto vyžaduje SPECIAL handler (filter rows), nie iba file copy.
    """
    out = []
    for mk in markets:
        src = os.path.join("out", mk, "vdt_paper_trades.csv")
        if os.path.exists(src):
            # Generic destination — actual split = handler
            out.append((src, f"split:per-profile/{mk}"))
    return out


def _plan_shared_data(markets: List[str]) -> List[Tuple[str, str]]:
    """Step 5: historian, imbalance, price_train → out/_shared/<mk>/"""
    out = []
    shared_patterns = (
        "historian_", "imbalance_", "price_train", "ftv_scenarios",
        "load_profile", "auto_control_profiles.json", "okte_",
    )
    for mk in markets:
        src_dir = os.path.join("out", mk)
        dst_dir = _shared_dir(mk)
        if not os.path.isdir(src_dir):
            continue
        for fn in os.listdir(src_dir):
            if any(fn.startswith(p) or fn == p.rstrip("/") for p in shared_patterns):
                src = os.path.join(src_dir, fn)
                if os.path.isfile(src):
                    dst = os.path.join(dst_dir, fn)
                    if not os.path.exists(dst):
                        out.append((src, dst))
    return out


# ── dry-run report ─────────────────────────────────────────────────────────

def report(args) -> int:
    profiles = _list_profiles()
    markets = _markets()

    print()
    print("━━━ FS Sandbox Migration — dry-run plán ━━━")
    print()
    print(f"Profily: {len(profiles)} ({', '.join(profiles)})")
    print(f"Trhy:    {len(markets)} ({', '.join(markets)})")
    print()

    steps = [
        ("1. Profile configs", _plan_config(profiles)),
        ("2. Plan JSONs", _plan_plans(profiles, markets)),
        ("3. Livesim CSV (shared → _shared/)", _plan_livesim(markets)),
        ("4. VDT trades (SPLIT per-profile)",
            _plan_vdt_trades(profiles, markets)),
        ("5. Shared data (historian, imbalance, ...)",
            _plan_shared_data(markets)),
    ]
    total_files = 0
    for name, items in steps:
        print(f"● {name}")
        print(f"    files: {len(items)}")
        for src, dst in items[:3]:
            print(f"      {src}")
            print(f"        → {dst}")
        if len(items) > 3:
            print(f"      ... +{len(items) - 3} ďalších")
        total_files += len(items)
        print()

    print(f"Total files to migrate: {total_files}")
    print()

    if not args.execute:
        print("DRY-RUN ONLY — žiadne súbory neboli presunuté.")
        print()
        print("Na execute spusti:")
        print("  python3 -m tools.migrate_to_sandbox --execute --i-have-backup")
        print()
        print("ALEBO najprv backup celej out/ + odstavenie aplikácie:")
        print("  docker compose down")
        print("  cp -r out out.bak")
        print("  python3 -m tools.migrate_to_sandbox --execute --i-have-backup")
        print("  docker compose up -d")
        return 0
    else:
        print("✗ EXECUTE NIE JE V TOMTO SKRIPTE IMPLEMENTOVANÝ.")
        print("Toto je dry-run-only nástroj — actual migration vyžaduje")
        print("samostatnú reviewovanú PR + downtime window.")
        print()
        print("Reason: livesim CSV split + VDT trades split = nontrivial logic,")
        print("ktorá by sa mala spúšťať len pod manual supervízou.")
        return 2


def main():
    p = argparse.ArgumentParser(
        description="FS sandbox migration plánovač (B.1 Architecture refactor)")
    p.add_argument("--execute", action="store_true",
                    help="(NOT IMPLEMENTED) ozaj prevedie presun. Vyžaduje --i-have-backup.")
    p.add_argument("--i-have-backup", action="store_true",
                    help="Potvrď že máš backup out/ pred execute")
    p.add_argument("--rollback", action="store_true",
                    help="(TODO) restore z najnovšieho _migration_backup_*")
    args = p.parse_args()

    if args.execute and not args.i_have_backup:
        print("ERR: --execute vyžaduje aj --i-have-backup (potvrdenie backupu)",
              file=sys.stderr)
        return 2
    if args.rollback:
        print("Rollback TODO — manuálne: rm -rf out && mv out.bak out")
        return 0
    return report(args)


if __name__ == "__main__":
    sys.exit(main())
