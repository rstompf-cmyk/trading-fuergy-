"""Bug #614 backfill: dopočítaj SOC trajektóriu pre staré uložené D-1 plány.

Použitie:
    python tools/backfill_d1_soc_trajectory.py --profile VW_simulacia --from 2026-06-01 --to 2026-06-09
    python tools/backfill_d1_soc_trajectory.py --all                       # všetky profily, všetky uložené dni
    python tools/backfill_d1_soc_trajectory.py --profile X --day 2026-06-03
"""
import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta

from plan_store import load_plan_safe, list_plans
from core.capacity_ledger import (
    compute_soc_trajectory, save_d1_trajectory, clear_day_trajectory,
)
from profiles import list_profiles, load_profile


def _resolve_step_min(plan_meta: dict) -> int:
    """Z meta zisti či bol plán 15-min alebo 60-min."""
    step = plan_meta.get("step_min")
    if step:
        return int(step)
    # Fallback: ak má 96 slotov → 15-min, ak 24 → 60-min
    sched = plan_meta.get("schedule", {})
    arr = sched.get("batt_kw") or sched.get("batt") or []
    n = len(arr)
    if n >= 90:
        return 15
    if n >= 20:
        return 60
    return 60


def backfill_one(profile: str, day: str, tolerance_pct: float = 10.0) -> int:
    """Backfill 1 deň. Vráti počet uložených slotov (0 = preskočené)."""
    p = load_plan_safe(profile, day, kind="plan")
    if not p:
        return 0
    sched = p.get("schedule", {})
    params = p.get("params", {})
    batt_arr = sched.get("batt_kw") or sched.get("batt") or []
    if not batt_arr:
        return 0
    soc_init = float(params.get("soc_init", 0.0) or 0.0)
    batt_kwh = float(params.get("batt_kwh", 0.0) or 0.0)
    if batt_kwh <= 0:
        # fallback z profilu
        prof = load_profile(profile)
        if prof:
            batt_kwh = float((prof.get("plan") or {}).get("batt_kwh", 0.0) or 0.0)
            if soc_init <= 0:
                soc_init = float((prof.get("plan") or {}).get("soc_init", 0.0) or 0.0)
    if batt_kwh <= 0:
        print(f"  ⚠ {day}: batt_kwh=0 → preskakujem")
        return 0
    # soc_init v % alebo fraction
    if soc_init > 1.5:
        soc_init_pct = min(100.0, soc_init)
    else:
        soc_init_pct = soc_init * 100.0
    step = _resolve_step_min(p)
    traj = compute_soc_trajectory(batt_arr, soc_init_pct, batt_kwh, step)
    if not traj:
        return 0
    clear_day_trajectory(profile, day)
    n = save_d1_trajectory(profile, day, traj, tolerance_pct)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", type=str, help="meno profilu")
    ap.add_argument("--all", action="store_true", help="všetky profily")
    ap.add_argument("--day", type=str, help="konkrétny deň YYYY-MM-DD")
    ap.add_argument("--from", dest="d_from", type=str, help="rozsah od YYYY-MM-DD")
    ap.add_argument("--to", dest="d_to", type=str, help="rozsah do YYYY-MM-DD")
    ap.add_argument("--tolerance", type=float, default=10.0, help="tolerance %% (default 10)")
    args = ap.parse_args()

    profiles_to_do = []
    if args.all:
        raw = list_profiles()
        profiles_to_do = [p if isinstance(p, str) else (p.get("name") or p.get("profile")) for p in raw]
        profiles_to_do = [p for p in profiles_to_do if p]
    elif args.profile:
        profiles_to_do = [args.profile]
    else:
        print("Použi --profile <name> alebo --all")
        sys.exit(1)

    total_days = 0
    total_slots = 0
    for prof in profiles_to_do:
        print(f"\n=== {prof} ===")
        if args.day:
            days = [args.day]
        elif args.d_from and args.d_to:
            d1 = date.fromisoformat(args.d_from)
            d2 = date.fromisoformat(args.d_to)
            days = [(d1 + timedelta(days=i)).isoformat()
                      for i in range((d2 - d1).days + 1)]
        else:
            # Z plan_store listing
            try:
                items = list_plans(kind="plan", profile=prof)
                days = sorted({it.get("date") for it in items if it.get("date")})
            except Exception as e:
                print(f"  ⚠ list_plans zlyhal: {e}")
                continue
        for day in days:
            n = backfill_one(prof, day, args.tolerance)
            if n > 0:
                print(f"  ✓ {day}: {n} slotov")
                total_days += 1
                total_slots += n
            elif n == 0:
                pass   # silently skip (no plan)

    print(f"\nHotovo: {total_days} dní, {total_slots} slotov uložených.")


if __name__ == "__main__":
    main()
