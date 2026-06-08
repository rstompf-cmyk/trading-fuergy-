# -*- coding: utf-8 -*-
"""reset_profile_sim.py — Vyčisti livesim CSV + VDT paper trades pre profil.

Použitie:
    python3 -m tools.reset_profile_sim Simulacia_Coop
    python3 -m tools.reset_profile_sim Simulacia_Coop --dry-run
    python3 -m tools.reset_profile_sim Trakany_real --keep-vdt   # iba livesim reset

Čo robí:
  1. Vymaže livesim_*.csv + meta.json pre VŠETKY porty (8000-8005)
     v out/{market}/ pre profil ktorý je aktuálny pre daný port.
  2. Odstráni riadky z out/{market}/vdt_paper_trades.csv pre tento profil
     (zachová riadky pre iné profily).
  3. Vymaže VDT live advisor cache pre tento profil.
  4. Vymaže MPC cache pre tento profil.

POZOR: dáta sú nevratné. Po reset musí scheduler znovu generovať od zaciatku.
Plán (D-1) z plan_store ostáva — to je zámer (kúpiš si plán raz a hoď ho
mnoho-krát).
"""
from __future__ import annotations
import argparse
import os
import sys
import glob
import shutil
from datetime import datetime


def _market_data_dirs():
    """Vráti list adresárov out/cz/ a out/sk/ ak existujú."""
    out = []
    for sub in ["cz", "sk"]:
        d = os.path.join("out", sub)
        if os.path.isdir(d):
            out.append(d)
    if not out:
        # fallback ak nie sú podpriečinky (single-market setup)
        if os.path.isdir("out"):
            out.append("out")
    return out


def _backup_dir() -> str:
    """Vráti časovo-stamped backup adresár (vytvorí ak treba)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    d = os.path.join("out", f"_reset_backup_{ts}")
    os.makedirs(d, exist_ok=True)
    return d


def _move_to_backup(path: str, backup_root: str, verbose: bool = True):
    """Presunie súbor do backup adresára (zachová relatívnu cestu)."""
    if not os.path.exists(path):
        return
    rel = os.path.relpath(path, "out")
    dst = os.path.join(backup_root, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(path, dst)
    if verbose:
        print(f"  → backup: {path}")


def reset_livesim(profile: str, dry_run: bool, backup: str) -> int:
    """Vyčistí livesim_*.csv + meta.json pre PORTY ktoré majú profile=daný profile."""
    removed = 0
    for md in _market_data_dirs():
        # Pre každý port skontroluj _active_<port>.json
        # Pôjdeme cez všetky livesim_*.csv a meta — VYMAŽEME LEN AK aktívny profil
        # pre ten port je náš target.
        for port_file in sorted(glob.glob(os.path.join(md, "profiles", "_active*.json"))):
            try:
                import json
                with open(port_file) as f:
                    active = json.load(f)
                active_name = active.get("name") or active.get("active") or ""
            except Exception:
                continue
            if active_name != profile:
                continue
            # Port suffix: _active_8001.json → 8001, _active.json → 8000 (default)
            base = os.path.basename(port_file).replace(".json", "").replace("_active", "")
            port = base.lstrip("_") if base.lstrip("_") else "8000"
            # Vymaž všetky livesim_<case>_<port>.csv + meta
            patterns = [
                os.path.join(md, f"livesim_*_{port}.csv"),
                os.path.join(md, f"livesim_*_{port}.meta.json"),
                os.path.join(md, f"livesim_*_{port}_meta.json"),
            ]
            for pattern in patterns:
                for p in sorted(glob.glob(pattern)):
                    if dry_run:
                        print(f"  [dry-run] would delete: {p}")
                    else:
                        _move_to_backup(p, backup)
                    removed += 1
    return removed


def reset_vdt_trades(profile: str, dry_run: bool, backup: str) -> int:
    """Odstráni VDT paper trades pre profil (zachová iné profily)."""
    import csv
    removed = 0
    for md in _market_data_dirs():
        csv_path = os.path.join(md, "vdt_paper_trades.csv")
        if not os.path.exists(csv_path):
            continue
        try:
            with open(csv_path, newline="") as f:
                reader = csv.reader(f)
                rows = list(reader)
        except Exception as e:
            print(f"  ✗ read failed {csv_path}: {e}")
            continue
        if not rows:
            continue
        header = rows[0]
        try:
            prof_idx = header.index("profile")
        except ValueError:
            print(f"  ✗ {csv_path}: chýba stĺpec 'profile' — preskakujem")
            continue
        kept = [header]
        for r in rows[1:]:
            if len(r) > prof_idx and r[prof_idx] == profile:
                removed += 1
                continue
            kept.append(r)
        if removed > 0:
            if dry_run:
                print(f"  [dry-run] would remove {removed} rows from {csv_path}")
            else:
                # Backup pôvodný
                _move_to_backup(csv_path, backup, verbose=False)
                # Zapíš nový
                with open(csv_path, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerows(kept)
                print(f"  ✓ {csv_path}: -{removed} riadkov pre {profile}")
    return removed


def reset_vdt_cache(profile: str, dry_run: bool, backup: str) -> int:
    """Vymaže VDT live advisor + MPC cache pre profil."""
    removed = 0
    safe_name = profile.replace("/", "_").replace("\\", "_")
    patterns = []
    for md in _market_data_dirs():
        patterns.extend([
            os.path.join(md, f"vdt_advisor_{safe_name}.json"),
            os.path.join(md, f"mpc_tick_{safe_name}.json"),
            os.path.join(md, f"mpc_last_setpoint_{safe_name}.json"),
        ])
    for p in patterns:
        if not os.path.exists(p):
            continue
        if dry_run:
            print(f"  [dry-run] would delete: {p}")
        else:
            _move_to_backup(p, backup)
        removed += 1
    return removed


def main():
    ap = argparse.ArgumentParser(description="Reset livesim + VDT trades + cache pre profil")
    ap.add_argument("profile", help="Meno profilu (napr. Simulacia_Coop)")
    ap.add_argument("--dry-run", action="store_true",
                     help="Iba ukáž čo by sa zmazalo — bez zápisu")
    ap.add_argument("--keep-vdt", action="store_true",
                     help="Iba livesim reset (zachová VDT trades + cache)")
    ap.add_argument("--vdt-only", action="store_true",
                     help="Iba VDT trades + cache reset (zachová livesim) — "
                          "užitočné pri Bug UU residual cleanup")
    args = ap.parse_args()
    if args.keep_vdt and args.vdt_only:
        print("ERR: --keep-vdt a --vdt-only sú navzájom vylúčiteľné",
              file=sys.stderr)
        sys.exit(2)

    profile = args.profile.strip()
    if not profile:
        print("ERR: profile name prázdny", file=sys.stderr)
        sys.exit(1)

    print(f"━━━ Reset profile {profile} (dry-run={args.dry_run}) ━━━")
    backup = "" if args.dry_run else _backup_dir()
    if backup:
        print(f"Backup: {backup}")

    if not args.vdt_only:
        print("\n[1] Livesim CSV + meta:")
        n_ls = reset_livesim(profile, args.dry_run, backup)
        print(f"    {'(dry-run) ' if args.dry_run else ''}{n_ls} súborov")
    else:
        print("\n[1] Livesim CSV + meta: preskakuje sa (--vdt-only)")

    if not args.keep_vdt:
        print("\n[2] VDT paper trades:")
        n_vdt = reset_vdt_trades(profile, args.dry_run, backup)
        print(f"    {'(dry-run) ' if args.dry_run else ''}{n_vdt} riadkov")

        print("\n[3] VDT/MPC cache:")
        n_cache = reset_vdt_cache(profile, args.dry_run, backup)
        print(f"    {'(dry-run) ' if args.dry_run else ''}{n_cache} cache súborov")
    else:
        print("\n[2-3] VDT trades + cache: preskakuje sa (--keep-vdt)")

    print("\n━━━ Hotovo. Po reset spustí scheduler znova od začiatku. ━━━")
    if not args.dry_run:
        print(f"Backup pre prípad ROLLBACK: {backup}")


if __name__ == "__main__":
    main()
