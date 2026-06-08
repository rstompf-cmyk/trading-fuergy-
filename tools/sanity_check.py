# -*- coding: utf-8 -*-
"""tools/sanity_check.py — Cross-profile invariant validator.

Iteruje cez všetky profily a detekuje porušenia invariantov ktoré
zvyčajne signalizujú problém. Output je krátky prehľad — 1 riadok / profil.

Detekuje:
  • Profile: ProfileConfig validation passes
  • VDT-UU consistency: use_vdt=False ale existujú VDT trades (Bug UU rezíduum)
  • Cache freshness: VDT advisor cache staršia ako 60 min pre bg-enabled profil
  • Plan freshness: žiaden plán pre dnes alebo zajtra (pre bg-enabled profil)
  • Real-mode safety: real profil bez explicitne enabled bg/joint_mpc
  • Stale auto_control: enabled ale žiadny event za 24h

Exit code: 0 ak je všetko clean, 1 ak detected >= 1 problém.

Použitie:
    python3 -m tools.sanity_check                 # human-readable
    python3 -m tools.sanity_check --json          # strojový output
    python3 -m tools.sanity_check --strict        # exit 1 aj na warnings
"""
from __future__ import annotations
import os
import sys
import argparse
import datetime as dt
import json
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── ANSI farby ────────────────────────────────────────────────────────────
class C:
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        BOLD = "\033[1m"; DIM = "\033[2m"; OFF = "\033[0m"
        GREEN = "\033[32m"; YELLOW = "\033[33m"; RED = "\033[31m"; BLUE = "\033[34m"
    else:
        BOLD = DIM = OFF = GREEN = YELLOW = RED = BLUE = ""


# ── invariant checks ──────────────────────────────────────────────────────

def _check_profile(name: str) -> Dict[str, Any]:
    """Vráti dict s 'ok' / 'warnings' / 'errors' pre jeden profil."""
    from tools.audit_profile import audit
    data = audit(name)
    out: Dict[str, Any] = {
        "name": name, "ok": [], "warnings": [], "errors": [],
    }

    cfg = data["config"]
    if not cfg.get("exists"):
        out["errors"].append("Profile neexistuje")
        return out
    if not cfg.get("validated"):
        out["errors"].append(
            f"Pydantic validation failed: {cfg.get('validation_error', '?')[:80]}")
        return out

    # ── Invariant 1: VDT-UU consistency ────────────────────────────────────
    vdt = data["vdt"]
    if vdt["total"] > 0 and not vdt["gate_allows"]:
        out["warnings"].append(
            f"Bug UU rezíduum: {vdt['total']} VDT trades v CSV ale use_vdt:false "
            f"→ spusti `python3 -m tools.reset_profile_sim {name} --keep-vdt=false`")
    elif vdt["total"] == 0 and not vdt["gate_allows"]:
        out["ok"].append("VDT gate consistent (use_vdt:false + 0 trades)")
    elif vdt["total"] > 0 and vdt["gate_allows"]:
        out["ok"].append(f"VDT gate consistent ({vdt['total']} trades + use_vdt:true)")

    # ── Invariant 2: Plans count for bg-enabled profiles ───────────────────
    pl = data["plans"]
    if cfg.get("bg_enabled"):
        if pl["total"] == 0:
            out["errors"].append("BG enabled ale 0 plánov v plan_store")
        else:
            # Skontroluj že má aspoň plán pre dnes alebo zajtra
            today = dt.date.today().isoformat()
            tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
            last = pl.get("date_last", "")
            if last < today:
                out["warnings"].append(
                    f"BG enabled ale posledný plán {last} (older than today {today})")
            else:
                out["ok"].append(f"Plans recent ({last})")

    # ── Invariant 3: Cache freshness pre bg+joint_mpc profil ───────────────
    ca = data["caches"]
    if cfg.get("joint_mpc_enabled") and cfg.get("bg_enabled"):
        if ca.get("vdt_advisor_exists") is False:
            out["warnings"].append("Joint MPC enabled ale žiadny VDT advisor cache")
        elif ca.get("vdt_advisor_age_min", 9999) > 60:
            out["warnings"].append(
                f"VDT advisor cache stará {ca.get('vdt_advisor_age_min'):.0f} min (>60)")
        elif ca.get("vdt_advisor_profile_match") is False:
            out["errors"].append(
                "VDT advisor cache existuje ale profile field nematchuje (cross-leak!)")

    # ── Invariant 4: Real-mode safety ──────────────────────────────────────
    if cfg.get("is_real"):
        jlp = cfg.get("joint_lp", {})
        if jlp.get("enabled") is True and not cfg.get("joint_mpc_enabled"):
            # joint_lp enabled bez joint_mpc je OK ale upozorni
            out["warnings"].append(
                "Real mode + joint_lp:enabled ALE joint_mpc_enabled:false "
                "(MPC bude inaktívny)")

    # ── Invariant 5: Auto-control activity ─────────────────────────────────
    ac = data["auto_control"]
    if ac.get("enabled"):
        if ac.get("events_24h", 0) == 0:
            out["warnings"].append(
                "Auto-control enabled ale 0 events za posledných 24h")
        else:
            if ac.get("last_event_age_min", 9999) > 120:
                out["warnings"].append(
                    f"Auto-control posledný event pred "
                    f"{ac.get('last_event_age_min'):.0f} min (>120)")
            else:
                out["ok"].append(f"Auto-control active ({ac['events_24h']} events 24h)")

    return out


def _list_profiles() -> List[str]:
    """Vráti zoznam všetkých profilov v out/profiles/."""
    try:
        import profiles
        return profiles.list_profiles() or []
    except Exception:
        return []


# ── output ────────────────────────────────────────────────────────────────

def _print_report(results: List[Dict[str, Any]]) -> int:
    """Vypíše tabuľku, vráti počet profilov s chybou (>= 1 error)."""
    n_err = sum(1 for r in results if r["errors"])
    n_warn = sum(1 for r in results if r["warnings"])
    n_clean = sum(1 for r in results
                   if not r["errors"] and not r["warnings"])

    print()
    print(f"{C.BOLD}═══ Sanity check: {len(results)} profilov ═══{C.OFF}")
    print(f"  {C.GREEN}clean: {n_clean}{C.OFF}  ·  "
          f"{C.YELLOW}warnings: {n_warn}{C.OFF}  ·  "
          f"{C.RED}errors: {n_err}{C.OFF}")
    print()

    for r in results:
        if r["errors"]:
            badge = f"{C.RED}✗ ERROR{C.OFF}"
        elif r["warnings"]:
            badge = f"{C.YELLOW}⚠ WARN{C.OFF} "
        else:
            badge = f"{C.GREEN}✓ OK{C.OFF}   "
        print(f"{badge}  {C.BOLD}{r['name']}{C.OFF}")
        for e in r["errors"]:
            print(f"    {C.RED}✗{C.OFF} {e}")
        for w in r["warnings"]:
            print(f"    {C.YELLOW}⚠{C.OFF} {w}")
        # OK detaily iba v -v móde — zatial vynech aby output bol stručný

    print()
    return n_err


def main():
    p = argparse.ArgumentParser(
        description="Cross-profile sanity check (read-only)")
    p.add_argument("--json", action="store_true",
                    help="Strojový JSON output")
    p.add_argument("--strict", action="store_true",
                    help="Exit 1 aj keď sú iba warnings (nie iba errors)")
    p.add_argument("--profile", help="Skontrolovať iba jeden profil")
    args = p.parse_args()

    names = [args.profile] if args.profile else _list_profiles()
    if not names:
        print("Žiadne profily v out/profiles/")
        return 2

    results = [_check_profile(n) for n in names]

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False, default=str))
        n_err = sum(1 for r in results if r["errors"])
        n_warn = sum(1 for r in results if r["warnings"])
    else:
        n_err = _print_report(results)
        n_warn = sum(1 for r in results if r["warnings"])

    if n_err > 0:
        return 1
    if args.strict and n_warn > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
