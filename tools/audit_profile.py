# -*- coding: utf-8 -*-
"""tools/audit_profile.py — Per-profile diagnostický CLI.

Vypíše kompletný stav jedného profilu: config + plans + livesim + VDT + cache +
auto_control log. Žiadny zápis, žiadne side-effecty — len čítanie z disku.

Použitie:
    python3 -m tools.audit_profile Simulacia_Coop
    python3 -m tools.audit_profile Trakany_real --json     # strojový output

Fáza B.3 architektúry. Používa Pydantic schemy z core.schemas pre validáciu
počas auditu — keď niečo nie je v očakávanom tvare, vypíše to ako warning.
"""
from __future__ import annotations
import os
import sys
import csv
import json
import glob
import argparse
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

# Pridaj root do sys.path aby fungovalo `python3 -m tools.audit_profile`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── farby + formátovanie ──────────────────────────────────────────────────
class C:
    """ANSI escape codes — vypnuté ak nie je TTY."""
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        BOLD = "\033[1m"; DIM = "\033[2m"; OFF = "\033[0m"
        GREEN = "\033[32m"; YELLOW = "\033[33m"; RED = "\033[31m"; BLUE = "\033[34m"
    else:
        BOLD = DIM = OFF = GREEN = YELLOW = RED = BLUE = ""


def _ok(s: str) -> str: return f"{C.GREEN}✓{C.OFF} {s}"
def _warn(s: str) -> str: return f"{C.YELLOW}⚠{C.OFF} {s}"
def _err(s: str) -> str: return f"{C.RED}✗{C.OFF} {s}"
def _head(s: str) -> str: return f"{C.BOLD}{s}{C.OFF}"


# ── data collectors (každý vracia dict ktorý pôjde do JSON output-u) ──────

def _audit_config(name: str) -> Dict[str, Any]:
    """Validuje ProfileConfig + extrahuje kľúčové polia."""
    import profiles
    out: Dict[str, Any] = {"name": name, "exists": False}
    try:
        raw = profiles.load_profile(name)
        if not raw:
            out["error"] = "neexistuje v out/profiles/"
            return out
        out["exists"] = True
        out["raw_keys"] = sorted(raw.keys())
    except Exception as e:
        out["error"] = f"load_profile zlyhal: {e}"
        return out

    try:
        cfg = profiles.load_profile_validated(name)
        if cfg is None:
            out["validated"] = False
            out["validation_error"] = "load_profile_validated vrátil None"
            return out
        out["validated"] = True
        out["mode"] = cfg.mode
        out["kwp"] = cfg.plan.kwp
        out["batt_kw"] = cfg.plan.batt_kw
        out["batt_kwh"] = cfg.plan.batt_kwh
        out["soc_min"] = cfg.plan.soc_min
        out["soc_max"] = cfg.plan.soc_max
        out["bg_enabled"] = bool(getattr(cfg.plan, "bg_enabled", False))
        out["joint_mpc_enabled"] = bool(getattr(cfg.plan, "joint_mpc_enabled", False))
        out["joint_lp"] = {
            "enabled": cfg.plan.joint_lp.enabled,
            "trade_batt": cfg.plan.joint_lp.trade_batt,
            "trade_ftv": cfg.plan.joint_lp.trade_ftv,
            "trade_load": cfg.plan.joint_lp.trade_load,
            "use_vdt": cfg.plan.joint_lp.use_vdt,
            "optimize_distribution": cfg.plan.joint_lp.optimize_distribution,
        }
        out["uses_vdt"] = cfg.uses_vdt()
        out["uses_tou"] = cfg.uses_tou()
        out["is_real"] = cfg.is_real()
    except Exception as e:
        out["validated"] = False
        out["validation_error"] = str(e)
    return out


def _audit_plans(name: str) -> Dict[str, Any]:
    """Spočíta plány v plan_store + validuje."""
    from core.schemas import StoredPlan
    out: Dict[str, Any] = {"total": 0, "by_kind": {}, "invalid": []}
    try:
        import plan_store
        # market-aware root
        try:
            import market as _mk
            root = _mk.data_dir()
        except Exception:
            root = "out/cz"
        plan_dir = os.path.join(root, "plans", name)
        if not os.path.isdir(plan_dir):
            # legacy default v root-e
            plan_dir = os.path.join(root, "plans")
        files = []
        if os.path.isdir(plan_dir):
            files = [f for f in glob.glob(os.path.join(plan_dir, "*.json"))
                     if os.path.isfile(f)]
        out["dir"] = plan_dir
        dates = []
        for f in files:
            try:
                with open(f) as fh:
                    d = json.load(fh)
                sp = StoredPlan.model_validate(d)
                # Filter — niektoré subdirectory plány môžu mať iný profile interne
                if sp.profile and sp.profile != name and sp.profile != "default":
                    continue
                key = f"{sp.kind}_{sp.step_min}min"
                out["by_kind"][key] = out["by_kind"].get(key, 0) + 1
                out["total"] += 1
                dates.append(sp.date)
            except Exception as e:
                out["invalid"].append({"file": os.path.basename(f),
                                         "error": str(e)[:120]})
        if dates:
            dates.sort()
            out["date_first"] = dates[0]
            out["date_last"] = dates[-1]
    except Exception as e:
        out["error"] = str(e)
    return out


def _audit_livesim() -> Dict[str, Any]:
    """Livesim CSV stats — globálne (per-port, nie per-profile zatiaľ)."""
    out: Dict[str, Any] = {"files": []}
    try:
        import market as _mk
        root = _mk.data_dir()
    except Exception:
        root = "out/sk"
    for pattern in ("livesim_plan_d1*.csv", "livesim_dt_15min_*.csv"):
        for f in glob.glob(os.path.join(root, pattern)):
            try:
                size = os.path.getsize(f)
                # Count lines (rows ≈ lines - 1)
                with open(f, "rb") as fh:
                    rows = sum(1 for _ in fh) - 1
                # Last mtime
                mtime = dt.datetime.fromtimestamp(os.path.getmtime(f))
                meta_file = f.replace(".csv", ".meta.json")
                meta = {}
                if os.path.exists(meta_file):
                    try:
                        with open(meta_file) as mh:
                            meta = json.load(mh)
                    except Exception:
                        pass
                out["files"].append({
                    "path": os.path.basename(f),
                    "size_kb": round(size / 1024, 1),
                    "rows": max(0, rows),
                    "mtime": mtime.strftime("%Y-%m-%d %H:%M"),
                    "case": meta.get("case", "?"),
                    "settings_sig": (meta.get("settings_sig") or "")[:12],
                    "start_date": meta.get("start_date", ""),
                    "end_date": meta.get("end_date", ""),
                })
            except Exception:
                pass
    return out


def _audit_vdt_trades(name: str) -> Dict[str, Any]:
    """Spočíta VDT paper trades pre tento profil + validuje."""
    from core.schemas.vdt import VDTTrade, should_log_vdt_for_profile
    out: Dict[str, Any] = {"total": 0, "valid": 0, "invalid": 0, "by_action": {}}
    try:
        import market as _mk
        root = os.path.dirname(_mk.data_dir().rstrip("/").rstrip(os.sep)) or "out"
    except Exception:
        root = "out"
    csv_path = os.path.join(root, "sk", "vdt_paper_trades.csv")
    out["csv_exists"] = os.path.exists(csv_path)
    out["gate_allows"] = should_log_vdt_for_profile(name)
    if not out["csv_exists"]:
        return out
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("profile") or "") != name:
                    continue
                out["total"] += 1
                try:
                    t = VDTTrade.model_validate(row)
                    out["valid"] += 1
                    out["by_action"][t.action] = out["by_action"].get(t.action, 0) + 1
                except Exception:
                    out["invalid"] += 1
    except Exception as e:
        out["error"] = str(e)

    # Sanity check: ak gate False ale trades existujú → varovanie (Bug UU)
    if out["total"] > 0 and not out["gate_allows"]:
        out["warning"] = (f"{out['total']} VDT trades v CSV ale use_vdt:false — "
                          f"pre-Bug UU stav, treba reset_profile_sim alebo gate refresh")
    return out


def _audit_caches(name: str) -> Dict[str, Any]:
    """VDT advisor cache + MPC cache."""
    out: Dict[str, Any] = {}
    try:
        import vdt_live_advisor as vla
        cache_p = vla.cache_path(name)
        out["vdt_advisor_path"] = cache_p
        if os.path.exists(cache_p):
            mtime = os.path.getmtime(cache_p)
            out["vdt_advisor_mtime"] = dt.datetime.fromtimestamp(mtime).strftime(
                "%Y-%m-%d %H:%M")
            age_min = (dt.datetime.now().timestamp() - mtime) / 60
            out["vdt_advisor_age_min"] = round(age_min, 1)
            with open(cache_p) as f:
                cd = json.load(f)
            out["vdt_advisor_has_full_plan"] = bool(cd.get("full_plan"))
            out["vdt_advisor_profile_match"] = (cd.get("profile") == name)
        else:
            out["vdt_advisor_exists"] = False
    except Exception as e:
        out["vdt_advisor_error"] = str(e)
    return out


def _audit_auto_control(name: str) -> Dict[str, Any]:
    """auto_control_log.csv — počty eventov pre tento profil za posledných 24h."""
    out: Dict[str, Any] = {"enabled": False, "events_24h": 0, "events_7d": 0,
                            "last_event": None}
    try:
        import market as _mk
        root = os.path.dirname(_mk.data_dir().rstrip("/").rstrip(os.sep)) or "out"
    except Exception:
        root = "out"
    cfg_path = os.path.join(root, "sk", "auto_control_profiles.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
            out["enabled"] = name in (cfg.get("enabled") or [])
        except Exception:
            pass
    log_path = os.path.join(root, "sk", "auto_control_log.csv")
    if not os.path.exists(log_path):
        return out
    try:
        now = dt.datetime.now()
        cutoff_24h = now - dt.timedelta(hours=24)
        cutoff_7d = now - dt.timedelta(days=7)
        last_ts = None
        with open(log_path) as f:
            for row in csv.DictReader(f):
                if (row.get("profile") or "") != name:
                    continue
                ts_raw = row.get("ts") or ""
                try:
                    ts = dt.datetime.fromisoformat(ts_raw[:19])
                except Exception:
                    continue
                if ts > cutoff_24h:
                    out["events_24h"] += 1
                if ts > cutoff_7d:
                    out["events_7d"] += 1
                if last_ts is None or ts > last_ts:
                    last_ts = ts
        if last_ts:
            out["last_event"] = last_ts.strftime("%Y-%m-%d %H:%M")
            age_min = (now - last_ts).total_seconds() / 60
            out["last_event_age_min"] = round(age_min, 1)
    except Exception as e:
        out["error"] = str(e)
    return out


# ── pretty printer ─────────────────────────────────────────────────────────

def _print_report(name: str, data: Dict[str, Any]) -> None:
    cfg = data["config"]
    print()
    print(_head(f"═══ Audit profilu: {name} ═══"))
    print()

    # Config
    print(_head("● Config"))
    if not cfg.get("exists"):
        print(_err(f"profile neexistuje: {cfg.get('error', '?')}"))
        return
    if not cfg.get("validated"):
        print(_warn(f"Pydantic validation FAILED: {cfg.get('validation_error', '?')}"))
    else:
        print(f"  Mode:    {C.BOLD}{cfg['mode']}{C.OFF}"
              f" {'(REAL hardware!)' if cfg['is_real'] else ''}")
        print(f"  kWp:     {cfg['kwp']:.0f}")
        print(f"  Batt:    {cfg['batt_kw']:.0f} kW / {cfg['batt_kwh']:.0f} kWh"
              f"  ·  SOC {cfg['soc_min']:.0f}-{cfg['soc_max']:.0f}%")
        bg = "ON" if cfg.get('bg_enabled') else "OFF"
        bg_color = C.GREEN if cfg.get('bg_enabled') else C.DIM
        print(f"  BG:      {bg_color}{bg}{C.OFF}"
              f"  ·  Joint MPC: {'ON' if cfg.get('joint_mpc_enabled') else 'OFF'}")
        jlp = cfg["joint_lp"]
        jlp_flags = " ".join(f"{C.GREEN if v else C.DIM}{k}{C.OFF}"
                              for k, v in [("batt", jlp["trade_batt"]),
                                            ("ftv", jlp["trade_ftv"]),
                                            ("load", jlp["trade_load"]),
                                            ("vdt", jlp["use_vdt"]),
                                            ("dist", jlp["optimize_distribution"])])
        print(f"  Joint LP: {'enabled' if jlp['enabled'] else 'disabled'}  ·  {jlp_flags}")
        print(f"  uses_vdt()={cfg['uses_vdt']}  ·  uses_tou()={cfg['uses_tou']}")

    # Plans
    print()
    print(_head("● Plans"))
    pl = data["plans"]
    if pl.get("error"):
        print(_err(pl["error"]))
    else:
        print(f"  Total:   {pl['total']} plánov"
              f"  ·  dir: {C.DIM}{pl.get('dir', '?')}{C.OFF}")
        if pl["total"] > 0:
            kinds = ", ".join(f"{k}={v}" for k, v in sorted(pl["by_kind"].items()))
            print(f"  By kind: {kinds}")
            print(f"  Range:   {pl.get('date_first', '?')} → {pl.get('date_last', '?')}")
        if pl["invalid"]:
            print(_warn(f"{len(pl['invalid'])} invalid plánov (zlyhanie schema validation)"))
            for inv in pl["invalid"][:3]:
                print(f"    - {inv['file']}: {inv['error']}")

    # Livesim
    print()
    print(_head("● Livesim CSV"))
    lv = data["livesim"]
    if not lv["files"]:
        print(_warn("Žiadne livesim CSV — nemáš spustený žiadny port"))
    for f in lv["files"]:
        print(f"  {C.BOLD}{f['path']}{C.OFF}  "
              f"({f['size_kb']} kB · {f['rows']} riadkov)")
        print(f"    case={f['case']}  sig={f['settings_sig']}  "
              f"range={f['start_date']}→{f['end_date']}  mtime={f['mtime']}")

    # VDT trades
    print()
    print(_head("● VDT paper trades"))
    vdt = data["vdt"]
    gate_color = C.GREEN if vdt["gate_allows"] else C.RED
    print(f"  Gate (use_vdt): {gate_color}{vdt['gate_allows']}{C.OFF}")
    if not vdt["csv_exists"]:
        print(_warn("CSV neexistuje — žiadne VDT trades"))
    else:
        print(f"  Pre profil:    {vdt['total']} riadkov "
              f"({vdt['valid']} valid · {vdt['invalid']} invalid)")
        if vdt["by_action"]:
            acts = ", ".join(f"{a}={c}" for a, c in sorted(vdt["by_action"].items()))
            print(f"  By action:     {acts}")
        if vdt.get("warning"):
            print(_warn(vdt["warning"]))
        if vdt["total"] == 0 and not vdt["gate_allows"]:
            print(_ok("Gate False + 0 trades — Bug UU správanie OK"))

    # Caches
    print()
    print(_head("● Caches"))
    ca = data["caches"]
    if ca.get("vdt_advisor_exists") is False:
        print(_warn("VDT advisor cache: neexistuje"))
    elif "vdt_advisor_mtime" in ca:
        prof_match = ca.get("vdt_advisor_profile_match", True)
        match_str = _ok("profile match") if prof_match else _err("profile MISMATCH!")
        print(f"  VDT advisor:   {ca['vdt_advisor_mtime']} "
              f"({ca['vdt_advisor_age_min']} min stará)  ·  {match_str}")
        if ca.get("vdt_advisor_has_full_plan"):
            print(f"    full_plan: present")

    # Auto-control
    print()
    print(_head("● Auto-control"))
    ac = data["auto_control"]
    en_color = C.GREEN if ac.get("enabled") else C.DIM
    print(f"  Enabled: {en_color}{ac.get('enabled', False)}{C.OFF}")
    print(f"  Events:  24h={ac.get('events_24h', 0)}  ·  7d={ac.get('events_7d', 0)}")
    if ac.get("last_event"):
        age = ac.get("last_event_age_min", 0)
        age_color = C.GREEN if age < 30 else (C.YELLOW if age < 120 else C.RED)
        print(f"  Last:    {ac['last_event']}  ·  {age_color}{age:.0f} min ago{C.OFF}")

    print()


# ── main ───────────────────────────────────────────────────────────────────

def audit(name: str) -> Dict[str, Any]:
    """Zbiera všetky audit dáta. Hlavná funkcia ktorú volá CLI aj testy."""
    return {
        "name": name,
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "config": _audit_config(name),
        "plans": _audit_plans(name),
        "livesim": _audit_livesim(),
        "vdt": _audit_vdt_trades(name),
        "caches": _audit_caches(name),
        "auto_control": _audit_auto_control(name),
    }


def main():
    p = argparse.ArgumentParser(
        description="Per-profile diagnostický audit (read-only)")
    p.add_argument("profile", help="Meno profilu (napr. Simulacia_Coop)")
    p.add_argument("--json", action="store_true",
                    help="Strojový JSON output namiesto pretty print")
    args = p.parse_args()

    data = audit(args.profile)

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        _print_report(args.profile, data)

    # Exit code: 0 ak je všetko OK, 1 ak nájde anomálie
    cfg_ok = data["config"].get("validated", False)
    vdt_ok = not data["vdt"].get("warning")
    return 0 if (cfg_ok and vdt_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
