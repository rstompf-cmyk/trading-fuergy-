#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_csv_to_db.py — jednorázový migračný script z CSV/JSON do SQLite.

Použitie:
    python tools/migrate_csv_to_db.py [--db <url>] [--dry-run] [--only profiles|plans|...]

Predtým musí byť spustený `alembic upgrade head` aby existovala schéma.

Idempotentnosť:
    Script môžeš spustiť opakovane — používa UPSERT pattern (na duplicitné kľúče
    aktualizuje, nepridáva). Bez `--reset` zachováva existujúce DB záznamy.

Backupy:
    Pred prvou migráciou skopíruj `out/` ako `out/_legacy_backup_<timestamp>/`
    (script to neurobí — radšej manuálne s `cp -r` aby sa nestratilo).

Migruje:
    1. Profiles (out/profiles/*.json + _active.json)
    2. PlanOverrides (out/<market>/plan_overrides/<profile>/_template.json)
    3. Plans (out/<market>/plans/<profile>/<date>_<step>min_<kind>.json)
    4. LoadProfile (out/<market>/load_profile/[<profile>/]profile.json)
    5. FtvScenarios (out/<market>/ftv_scenarios/<date>.json)
    6. VdtPaperTrades (out/<market>/vdt_paper_trades.csv)
    7. AutoControlEvents (out/<market>/auto_control_log.csv)
    8. ActiveMarket (out/_active_market*.json)
    9. ActiveProfile (out/profiles/_active*.json)

NEMIGRUJE:
    - Livesim logs (CSV ostávajú, sú objemné a perf-citlivé)
    - Realio measurements (vlastný realio_measurements.sqlite ostane)
    - Historian CSVs (out/sk/historian_*.csv — read-only externé dáta)
    - Cache (out/cache/* — regenerovateľné)
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import sys
import glob
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

# Pridaj projekt root na path
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db import get_session
from db.models import (
    Profile, ActiveProfile, ActiveMarket,
    Plan, PlanSlot, PlanOverride,
    LoadProfile, FtvScenario, VdtPaperTrade, AutoControlEvent,
)

# ────────────────────────────── HELPERS ─────────────────────────────────

OUT_DIR = os.path.join(_ROOT, "out")


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ⚠ zlyhalo načítanie {path}: {e}")
        return None


class Stats:
    def __init__(self):
        self.created = 0
        self.updated = 0
        self.skipped = 0
        self.errors = 0

    def summary(self, label: str):
        return (f"  {label:30s} created={self.created:4d} updated={self.updated:4d}"
                  f" skipped={self.skipped:4d} errors={self.errors}")


# ────────────────────────────── 1. PROFILES ─────────────────────────────

def migrate_profiles(dry_run: bool = False) -> Stats:
    st = Stats()
    profiles_dir = os.path.join(OUT_DIR, "profiles")
    if not os.path.isdir(profiles_dir):
        print(f"  Skip: {profiles_dir} neexistuje")
        return st
    for fn in sorted(os.listdir(profiles_dir)):
        if fn.startswith("_") or not fn.endswith(".json"):
            continue
        name = fn[:-5]
        data = _load_json(os.path.join(profiles_dir, fn))
        if not data:
            st.errors += 1
            continue
        if dry_run:
            print(f"  [dry] profile {name}")
            st.created += 1
            continue
        try:
            with get_session() as s:
                existing = s.query(Profile).filter_by(name=name).one_or_none()
                if existing:
                    existing.mode = str(data.get("mode") or "simulation")
                    existing.note = str(data.get("note") or "")
                    existing.plan = dict(data.get("plan") or {})
                    existing.dentrh = dict(data.get("dentrh") or {})
                    existing.rt = dict(data.get("rt") or {})
                    existing.distribution = dict(data.get("distribution") or {})
                    existing.mult96 = list(data.get("mult96") or [])
                    existing.rt_on96 = list(data.get("rt_on96") or [])
                    existing.updated_at = _iso_now()
                    st.updated += 1
                else:
                    p = Profile(
                        name=name,
                        mode=str(data.get("mode") or "simulation"),
                        note=str(data.get("note") or ""),
                        created_at=str(data.get("created_at") or _iso_now()),
                        updated_at=str(data.get("updated_at") or _iso_now()),
                        plan=dict(data.get("plan") or {}),
                        dentrh=dict(data.get("dentrh") or {}),
                        rt=dict(data.get("rt") or {}),
                        distribution=dict(data.get("distribution") or {}),
                        mult96=list(data.get("mult96") or []),
                        rt_on96=list(data.get("rt_on96") or []),
                    )
                    s.add(p)
                    st.created += 1
        except Exception as e:
            print(f"  ❌ profile {name}: {e}")
            st.errors += 1
    return st


# ────────────────────────────── 2. PLAN OVERRIDES ──────────────────────

def migrate_plan_overrides(dry_run: bool = False) -> Stats:
    """Migruje out/<market>/plan_overrides/<profile>/_template.json + per-day overrides."""
    st = Stats()
    for market in ("cz", "sk"):
        po_root = os.path.join(OUT_DIR, market, "plan_overrides")
        if not os.path.isdir(po_root):
            continue
        # Per-profile subdirs
        for entry in sorted(os.listdir(po_root)):
            full = os.path.join(po_root, entry)
            if not os.path.isdir(full):
                continue
            # entry je meno profilu
            profile_name = entry
            for fn in sorted(os.listdir(full)):
                if not fn.endswith(".json"):
                    continue
                date = None if fn == "_template.json" else fn.replace(".json", "")
                # Detekcia _dentrh suffix → ignoruj date suffix (legacy)
                if date and "_dentrh" in date:
                    date = date.replace("_dentrh", "")
                data = _load_json(os.path.join(full, fn))
                if not data:
                    continue
                if dry_run:
                    st.created += 1
                    continue
                try:
                    with get_session() as s:
                        prof = s.query(Profile).filter_by(name=profile_name).one_or_none()
                        if not prof:
                            st.skipped += 1
                            continue
                        existing = s.query(PlanOverride).filter_by(
                            profile_id=prof.id, market=market, date=date
                        ).one_or_none()
                        mult96 = list(data.get("mult96") or [])
                        rt_on96 = list(data.get("rt_on96") or [])
                        if existing:
                            existing.mult96 = mult96
                            existing.rt_on96 = rt_on96
                            existing.updated_at = _iso_now()
                            st.updated += 1
                        else:
                            po = PlanOverride(
                                profile_id=prof.id, market=market, date=date,
                                mult96=mult96, rt_on96=rt_on96,
                                updated_at=_iso_now(),
                            )
                            s.add(po)
                            st.created += 1
                except Exception as e:
                    print(f"  ❌ override {profile_name}/{fn}: {e}")
                    st.errors += 1
    return st


# ────────────────────────────── 3. PLANS ─────────────────────────────────

def migrate_plans(dry_run: bool = False) -> Stats:
    """Migruje out/<market>/plans/<profile>/<date>_<step>min_<kind>.json."""
    st = Stats()
    for market in ("cz", "sk"):
        plans_root = os.path.join(OUT_DIR, market, "plans")
        if not os.path.isdir(plans_root):
            continue
        # Plans môžu byť priamo v root (legacy) alebo v podadresároch per profil
        for entry in sorted(os.listdir(plans_root)):
            full = os.path.join(plans_root, entry)
            if os.path.isdir(full):
                profile_name = entry
                for fn in sorted(os.listdir(full)):
                    if fn.endswith(".json"):
                        _migrate_plan_file(market, profile_name,
                                             os.path.join(full, fn), st, dry_run)
            elif entry.endswith(".json"):
                # Legacy: bez profile subdir → použij "default" alebo skip
                # Pre čistú migráciu radšej skip — pôvodný kód má kompatibility
                st.skipped += 1
    return st


def _migrate_plan_file(market: str, profile_name: str, path: str,
                        st: Stats, dry_run: bool):
    fn = os.path.basename(path)
    # parse: YYYY-MM-DD_<step>min_<kind>.json
    base = fn.replace(".json", "")
    parts = base.split("_")
    if len(parts) < 3:
        st.skipped += 1
        return
    date = parts[0]
    try:
        step_min = int(parts[1].replace("min", ""))
    except ValueError:
        st.skipped += 1
        return
    kind = parts[2]
    data = _load_json(path)
    if not data:
        st.errors += 1
        return
    if dry_run:
        st.created += 1
        return
    try:
        with get_session() as s:
            prof = s.query(Profile).filter_by(name=profile_name).one_or_none()
            if not prof:
                st.skipped += 1
                return
            existing = s.query(Plan).filter_by(
                profile_id=prof.id, market=market, date=date, kind=kind, step_min=step_min
            ).one_or_none()
            if existing:
                # Update in place
                existing.params = dict(data.get("params") or {})
                existing.summary = dict(data.get("summary") or {})
                existing.meta = dict(data.get("meta") or {})
                existing.mults = list(data.get("mults") or [])
                existing.rt_mask = list(data.get("rt_mask") or [])
                existing.block_planned_discharge = bool(data.get("block_planned_discharge", False))
                existing.zco_bias_w = float(data.get("zco_bias_w", 0.0))
                existing.rt_freedom = bool(data.get("rt_freedom", True))
                existing.generated_at = str(data.get("generated_at") or _iso_now())
                # Vymaž staré sloty a vlož nové
                s.query(PlanSlot).filter_by(plan_id=existing.id).delete()
                plan_id = existing.id
                st.updated += 1
            else:
                p = Plan(
                    profile_id=prof.id, market=market, date=date, kind=kind,
                    step_min=step_min,
                    generated_at=str(data.get("generated_at") or _iso_now()),
                    params=dict(data.get("params") or {}),
                    summary=dict(data.get("summary") or {}),
                    meta=dict(data.get("meta") or {}),
                    mults=list(data.get("mults") or []),
                    rt_mask=list(data.get("rt_mask") or []),
                    block_planned_discharge=bool(data.get("block_planned_discharge", False)),
                    zco_bias_w=float(data.get("zco_bias_w", 0.0)),
                    rt_freedom=bool(data.get("rt_freedom", True)),
                )
                s.add(p)
                s.flush()
                plan_id = p.id
                st.created += 1
            # Sloty
            sched = data.get("schedule") or {}
            n_slots = max(len(sched.get(k, [])) for k in
                            ("pv_kwh", "batt_kw", "grid_kwh", "soc_pct")) if sched else 0
            for i in range(n_slots):
                ps = PlanSlot(
                    plan_id=plan_id, slot_idx=i,
                    pv_kwh=_safe_float(sched.get("pv_kwh", []), i),
                    load_kwh=_safe_float(sched.get("load_kwh", []), i),
                    price_eur=_safe_float(sched.get("price_eur", []), i),
                    batt_kw=_safe_float(sched.get("batt_kw", []), i),
                    grid_kwh=_safe_float(sched.get("grid_kwh", []), i),
                    order_mwh=_safe_float(sched.get("order_mwh", []), i),
                    curtail_kwh=_safe_float(sched.get("curtail_kwh", []), i),
                    soc_pct=_safe_float(sched.get("soc_pct", []), i),
                    soc_kwh=_safe_float(sched.get("soc_kwh", []), i),
                    charge_kw=_safe_float(sched.get("_charge_kw", []), i),
                    discharge_kw=_safe_float(sched.get("_discharge_kw", []), i),
                    export_kwh=_safe_float(sched.get("_export_kwh", []), i),
                    import_kwh=_safe_float(sched.get("_import_kwh", []), i),
                )
                s.add(ps)
    except Exception as e:
        print(f"  ❌ plan {profile_name}/{fn}: {e}")
        st.errors += 1


def _safe_float(arr, idx, default=0.0):
    try:
        v = arr[idx]
        return float(v) if v is not None else default
    except (IndexError, TypeError, ValueError):
        return default


# ────────────────────────────── 4. LOAD PROFILE ──────────────────────────

def migrate_load_profile(dry_run: bool = False) -> Stats:
    st = Stats()
    for market in ("cz", "sk"):
        lp_root = os.path.join(OUT_DIR, market, "load_profile")
        if not os.path.isdir(lp_root):
            continue
        # Per-profile subdirs
        for entry in sorted(os.listdir(lp_root)):
            full = os.path.join(lp_root, entry)
            profile_name = None
            data = None
            if os.path.isdir(full):
                profile_name = entry
                pj = os.path.join(full, "profile.json")
                if os.path.isfile(pj):
                    data = _load_json(pj)
            elif entry == "profile.json":
                # Default (žiadny per-profil subdir)
                profile_name = "default"
                data = _load_json(full)
            if not data or not profile_name:
                continue
            if dry_run:
                st.created += 1
                continue
            try:
                with get_session() as s:
                    prof = s.query(Profile).filter_by(name=profile_name).one_or_none()
                    if not prof:
                        st.skipped += 1
                        continue
                    existing = s.query(LoadProfile).filter_by(
                        profile_id=prof.id, market=market
                    ).one_or_none()
                    weekday = list(data.get("weekday_kw") or [])
                    weekend = list(data.get("weekend_kw") or [])
                    if existing:
                        existing.weekday_kw = weekday
                        existing.weekend_kw = weekend
                        existing.imported_dates = list(data.get("imported_dates") or [])
                        existing.unit = str(data.get("unit") or "kW")
                        existing.meta = dict(data.get("meta") or {})
                        existing.updated_at = _iso_now()
                        st.updated += 1
                    else:
                        lp = LoadProfile(
                            profile_id=prof.id, market=market,
                            weekday_kw=weekday, weekend_kw=weekend,
                            imported_dates=list(data.get("imported_dates") or []),
                            unit=str(data.get("unit") or "kW"),
                            meta=dict(data.get("meta") or {}),
                            updated_at=_iso_now(),
                        )
                        s.add(lp)
                        st.created += 1
            except Exception as e:
                print(f"  ❌ load_profile {market}/{profile_name}: {e}")
                st.errors += 1
    return st


# ────────────────────────────── 5. FTV SCENARIOS ─────────────────────────

def migrate_ftv_scenarios(dry_run: bool = False) -> Stats:
    st = Stats()
    for market in ("cz", "sk"):
        sc_root = os.path.join(OUT_DIR, market, "ftv_scenarios")
        if not os.path.isdir(sc_root):
            continue
        for fn in sorted(os.listdir(sc_root)):
            if not fn.endswith(".json"):
                continue
            date = fn.replace(".json", "")
            data = _load_json(os.path.join(sc_root, fn))
            if not data:
                continue
            if dry_run:
                st.created += 1
                continue
            try:
                with get_session() as s:
                    existing = s.query(FtvScenario).filter_by(
                        market=market, date=date
                    ).one_or_none()
                    hourly = list(data.get("hourly_kw") or [])
                    if existing:
                        existing.hourly_kw = hourly
                        existing.smooth_sigma = float(data.get("smooth_sigma", 0.0))
                        existing.offset_h = float(data.get("offset_h", 0.0))
                        existing.note = str(data.get("note") or "")
                        existing.saved_at = str(data.get("saved_at") or _iso_now())
                        st.updated += 1
                    else:
                        sc = FtvScenario(
                            market=market, date=date,
                            hourly_kw=hourly,
                            smooth_sigma=float(data.get("smooth_sigma", 0.0)),
                            offset_h=float(data.get("offset_h", 0.0)),
                            note=str(data.get("note") or ""),
                            saved_at=str(data.get("saved_at") or _iso_now()),
                        )
                        s.add(sc)
                        st.created += 1
            except Exception as e:
                print(f"  ❌ ftv_scenario {market}/{date}: {e}")
                st.errors += 1
    return st


# ────────────────────────────── 6. VDT PAPER TRADES ──────────────────────

def migrate_vdt_paper_trades(dry_run: bool = False) -> Stats:
    """Migrácia z out/<market>/vdt_paper_trades.csv.

    CSV header (zistené z dát):
        ts,profile,slot,action,kw,kwh,price_predicted_eur,
        soc_before_pct,soc_after_pct,soc_source,profit_eur_rest_of_day
    """
    st = Stats()
    for market in ("cz", "sk"):
        csv_path = os.path.join(OUT_DIR, market, "vdt_paper_trades.csv")
        if not os.path.isfile(csv_path):
            continue
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                profile_name = (row.get("profile") or "").strip()
                if not profile_name:
                    st.skipped += 1
                    continue
                slot = (row.get("slot") or "").strip()[:5]   # 'HH:MM'
                action = (row.get("action") or "").strip().lower()
                # date z timestampu (ts je ISO formát)
                ts = (row.get("ts") or "").strip()
                date = ts[:10] if len(ts) >= 10 else ""
                if not (slot and action and date):
                    st.skipped += 1
                    continue
                if dry_run:
                    st.created += 1
                    continue
                try:
                    with get_session() as s:
                        prof = s.query(Profile).filter_by(name=profile_name).one_or_none()
                        if not prof:
                            st.skipped += 1
                            continue
                        existing = s.query(VdtPaperTrade).filter_by(
                            profile_id=prof.id, date=date, slot=slot, action=action
                        ).one_or_none()
                        kwh = _safe_float_str(row.get("kwh"))
                        price = _safe_float_str(row.get("price_predicted_eur"))
                        if existing:
                            existing.kwh = kwh
                            existing.price_eur_mwh = price
                            existing.timestamp = ts
                            st.updated += 1
                        else:
                            t = VdtPaperTrade(
                                profile_id=prof.id, market=market, date=date,
                                slot=slot, action=action, kwh=kwh,
                                price_eur_mwh=price,
                                soc_before_pct=_safe_float_str(row.get("soc_before_pct")),
                                soc_after_pct=_safe_float_str(row.get("soc_after_pct")),
                                delta_profit_eur=_safe_float_str(row.get("profit_eur_rest_of_day")),
                                source=str(row.get("soc_source") or "advisor"),
                                timestamp=ts,
                            )
                            s.add(t)
                            st.created += 1
                except Exception as e:
                    print(f"  ❌ vdt_trade {market}/{profile_name}/{slot}: {e}")
                    st.errors += 1
    return st


def _safe_float_str(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ────────────────────────────── 7. AUTO CONTROL LOG ──────────────────────

def migrate_auto_control_log(dry_run: bool = False) -> Stats:
    """Migrácia z out/<market>/auto_control_log.csv (append-only)."""
    st = Stats()
    for market in ("cz", "sk"):
        csv_path = os.path.join(OUT_DIR, market, "auto_control_log.csv")
        if not os.path.isfile(csv_path):
            continue
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = (row.get("timestamp") or row.get("datetime") or "").strip()
                if not ts:
                    st.skipped += 1
                    continue
                if dry_run:
                    st.created += 1
                    continue
                try:
                    with get_session() as s:
                        # Append-only — duplikáty cez (ts, market, profile) sa môžu opakovať
                        # ale netreba UPSERT — len skip ak presný timestamp+market už je
                        prof_name = (row.get("profile") or "").strip()
                        prof_id = None
                        if prof_name:
                            p = s.query(Profile).filter_by(name=prof_name).one_or_none()
                            if p:
                                prof_id = p.id
                        # Check duplicate by (ts, market, profile_id)
                        dup = s.query(AutoControlEvent).filter_by(
                            ts=ts, market=market, profile_id=prof_id
                        ).first()
                        if dup:
                            st.skipped += 1
                            continue
                        ev = AutoControlEvent(
                            ts=ts, market=market, profile_id=prof_id,
                            soc_pct=_safe_float_str(row.get("soc_pct")),
                            batt_kw_setpoint=_safe_float_str(row.get("batt_kw_setpoint")),
                            mode=str(row.get("mode") or "dry_run"),
                            dry_run=str(row.get("dry_run", "True")).lower() in ("true", "1"),
                            reason=str(row.get("reason") or ""),
                            margin_check=_safe_bool(row.get("margin_check")),
                            soc_terminal_ok=_safe_bool(row.get("soc_terminal_ok")),
                            grid_capacity_ok=_safe_bool(row.get("grid_capacity_ok")),
                            plan_available=_safe_bool(row.get("plan_available")),
                            setpoint_clipped=_safe_bool(row.get("setpoint_clipped")),
                            grid_kw_min=_safe_float_str(row.get("grid_kw_min")),
                            grid_kw_max=_safe_float_str(row.get("grid_kw_max")),
                            price_eur_mwh=_safe_float_str(row.get("price_eur_mwh")),
                            qty_kwh=_safe_float_str(row.get("qty_kwh")),
                            notes=str(row.get("notes") or ""),
                        )
                        s.add(ev)
                        st.created += 1
                except Exception as e:
                    print(f"  ❌ auto_control {market}/{ts}: {e}")
                    st.errors += 1
    return st


def _safe_bool(v):
    if v is None or v == "":
        return None
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


# ────────────────────────────── 8. ACTIVE MARKET ────────────────────────

def migrate_active_market(dry_run: bool = False) -> Stats:
    st = Stats()
    pattern = os.path.join(OUT_DIR, "_active_market*.json")
    for path in sorted(glob.glob(pattern)):
        fn = os.path.basename(path)
        port = "8000"
        if "_active_market_" in fn:
            try:
                port = fn.split("_active_market_")[1].replace(".json", "")
            except Exception:
                port = "8000"
        data = _load_json(path)
        if not data:
            continue
        market = str(data.get("market") or "cz")
        if dry_run:
            st.created += 1
            continue
        try:
            with get_session() as s:
                existing = s.query(ActiveMarket).filter_by(port=port).one_or_none()
                if existing:
                    existing.market = market
                    existing.set_at = _iso_now()
                    st.updated += 1
                else:
                    am = ActiveMarket(port=port, market=market, set_at=_iso_now())
                    s.add(am)
                    st.created += 1
        except Exception as e:
            print(f"  ❌ active_market {port}: {e}")
            st.errors += 1
    return st


# ────────────────────────────── 9. ACTIVE PROFILE ────────────────────────

def migrate_active_profile(dry_run: bool = False) -> Stats:
    st = Stats()
    profiles_dir = os.path.join(OUT_DIR, "profiles")
    pattern = os.path.join(profiles_dir, "_active*.json")
    for path in sorted(glob.glob(pattern)):
        fn = os.path.basename(path)
        port = "8000"
        if fn.startswith("_active_"):
            try:
                port = fn.replace("_active_", "").replace(".json", "")
            except Exception:
                port = "8000"
        data = _load_json(path)
        if not data:
            continue
        prof_name = str(data.get("name") or "").strip()
        if not prof_name:
            continue
        if dry_run:
            st.created += 1
            continue
        try:
            with get_session() as s:
                prof = s.query(Profile).filter_by(name=prof_name).one_or_none()
                if not prof:
                    st.skipped += 1
                    continue
                # ActiveProfile má PK (port, market). Iterujeme markety — replicate per market
                for market in ("cz", "sk"):
                    existing = s.query(ActiveProfile).filter_by(port=port, market=market).one_or_none()
                    if existing:
                        existing.profile_id = prof.id
                        existing.set_at = _iso_now()
                        st.updated += 1
                    else:
                        ap = ActiveProfile(port=port, market=market,
                                            profile_id=prof.id, set_at=_iso_now())
                        s.add(ap)
                        st.created += 1
        except Exception as e:
            print(f"  ❌ active_profile {port}: {e}")
            st.errors += 1
    return st


# ────────────────────────────── MAIN ─────────────────────────────────────

ALL_TASKS = {
    "profiles": migrate_profiles,
    "plan_overrides": migrate_plan_overrides,
    "plans": migrate_plans,
    "load_profile": migrate_load_profile,
    "ftv_scenarios": migrate_ftv_scenarios,
    "vdt_paper_trades": migrate_vdt_paper_trades,
    "auto_control": migrate_auto_control_log,
    "active_market": migrate_active_market,
    "active_profile": migrate_active_profile,
}


def main():
    parser = argparse.ArgumentParser(description="Migrácia CSV/JSON → SQLite DB")
    parser.add_argument("--dry-run", action="store_true", help="Iba ukázať čo by sa spravilo")
    parser.add_argument("--only", help="Iba vybranú úlohu (napr. profiles,plans)")
    args = parser.parse_args()

    tasks = list(ALL_TASKS.keys())
    if args.only:
        tasks = [t.strip() for t in args.only.split(",")]
        for t in tasks:
            if t not in ALL_TASKS:
                print(f"⚠ Neznáma úloha: {t}. Dostupné: {list(ALL_TASKS.keys())}")
                sys.exit(1)

    print(f"=== Migrácia CSV/JSON → DB ({'DRY-RUN' if args.dry_run else 'LIVE'}) ===")
    print(f"DB URL: {os.environ.get('DB_URL', 'sqlite:///db/data/app.db (default)')}")
    print(f"Úlohy: {tasks}\n")

    total = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
    for task in tasks:
        fn = ALL_TASKS[task]
        print(f"→ {task}")
        st = fn(dry_run=args.dry_run)
        print(st.summary(task))
        for k in total:
            total[k] += getattr(st, k)
    print(f"\n═══ CELKOM ═══")
    print(f"  created={total['created']} updated={total['updated']} "
          f"skipped={total['skipped']} errors={total['errors']}")
    sys.exit(0 if total["errors"] == 0 else 2)


if __name__ == "__main__":
    main()
