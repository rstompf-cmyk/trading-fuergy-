# -*- coding: utf-8 -*-
"""
vdt_live_advisor.py — Rolling MPC (Model Predictive Control) advisor pre VDT.

Princíp:
  1. Vezmi aktuálny SOC batérie (z Realio merania alebo manuálne)
  2. Vezmi aktuálny live orderbook (best bid/ask per slot z OKTE)
  3. Spusti LP optimizer s aktuálnym SOC ako počiatočný stav
  4. Vráť odporúčanú akciu pre najbližší 15-min slot + plán na zvyšok dňa

Reaguje na zmenu cien automaticky — pri ďalšom volaní použije fresh orderbook.

Read-only: výstup je iba odporúčanie. Užívateľ ho manuálne aplikuje do Bender
(alebo neskôr cez "Aplikovať" tlačidlo so safety potvrdením).

JSON cache: výsledok sa ukladá do out/sk/vdt_live_plan.json pre rýchle UI loady
medzi scheduler runs.
"""
from __future__ import annotations
import datetime as dt
import json
import os
from typing import Dict, Any, Optional


def cache_path(profile: Optional[str] = None) -> str:
    """Cesta k JSON cache pre posledný advisor result — PER-PROFILE.

    Fáza B.1: deleguje na core/paths.vdt_advisor_cache_path() ktorá vie
    obe — legacy aj sandbox layout cez FTV_SANDBOX env var.
    """
    try:
        from core.paths import vdt_advisor_cache_path
        return vdt_advisor_cache_path(profile)
    except Exception:
        pass
    # Legacy fallback (ak core/paths nedostupné)
    try:
        import market as _mk
        root = os.path.dirname(_mk.data_dir().rstrip("/").rstrip(os.sep)) or "out"
    except Exception:
        root = "out"
    if profile and profile != "default":
        safe = "".join(c for c in str(profile) if c.isalnum() or c in "_-")
        if safe:
            return os.path.join(root, "sk", f"vdt_live_plan_{safe}.json")
    return os.path.join(root, "sk", "vdt_live_plan.json")


def _get_active_profile_mode(profile: Optional[str] = None) -> str:
    """Zistí mode profilu — 'real' / 'simulation' / 'unknown'.

    Ak `profile` je zadané, ide o ten konkrétny profil. Inak fallback na globálny
    active (cez plan_store.resolve_profile).
    """
    try:
        import plan_store as _ps
        import profiles as _pr
        prof = profile if profile else _ps.resolve_profile()
        if not prof or prof == "default":
            return "unknown"
        p = _pr.load_profile(prof)
        if isinstance(p, dict):
            return str(p.get("mode") or "unknown").lower()
    except Exception:
        pass
    return "unknown"


def _soc_from_livesim_trace() -> Optional[Dict[str, Any]]:
    """Pre simulation profil načíta posledný `soc_pct` zo dnešného livesim CSV
    (živá simulácia).

    Vracia None ak žiadne dáta nie sú.
    """
    try:
        import livesim as _ls
        import pandas as _pd
        import datetime as _dt
        port = os.environ.get("APP_PORT") or os.environ.get("PORT") or "8000"
        today_iso = _dt.date.today().isoformat()
        for case in ("dt_15min", "plan_d1"):
            try:
                df = _ls.load_series(case, port=port, day=today_iso, max_points=10**9)
            except Exception:
                continue
            if df is None or df.empty or "soc_pct" not in df.columns:
                continue
            sub = df[df["soc_pct"].notna()]
            if len(sub) == 0:
                continue
            soc = float(sub["soc_pct"].iloc[-1])
            ts_raw = sub["time"].iloc[-1] if "time" in sub.columns else None
            ts_str = str(ts_raw)[:19] if ts_raw is not None else ""
            age_min = 0.0
            if ts_raw is not None:
                try:
                    ts_dt = _pd.Timestamp(ts_raw).to_pydatetime()
                    if ts_dt.tzinfo is not None:
                        ts_dt = ts_dt.replace(tzinfo=None)
                    age_min = abs((_dt.datetime.now() - ts_dt).total_seconds()) / 60.0
                except Exception:
                    pass
            return {"ok": True, "soc_pct": soc,
                    "source": f"livesim {case} (simulation, vek: {age_min:.0f} min)",
                    "ts": ts_str, "age_minutes": age_min, "error": ""}
    except Exception:
        return None
    return None


def get_current_soc_pct(batt_kwh: Optional[float] = None,
                          fallback_soc_pct: Optional[float] = None,
                          max_age_minutes: int = 1440,
                          profile: Optional[str] = None) -> Dict[str, Any]:
    """Načíta aktuálny SOC podľa profile.mode:
      - **real** → realio_db (SQLite z Trakany Bender pollingu)
      - **simulation** → livesim CSV (posledný soc_pct z live trace), inak fallback

    Pre simulation profile NEKONTAMINOVAŤ z realio_db — to by viedlo k zlému SOC
    (napr. simulation profile by dostal SOC 100% z reálnej batérie). Bug 2026-06-04.

    Args:
        profile: meno konkrétneho profilu pre mode lookup. Ak None, fallback na
                 globálny active profile (zachová staré správanie).

    Vracia:
        {"ok": bool, "soc_pct": float, "source": str, "ts": str,
         "age_minutes": float, "error": str}
    """
    # Bug P: žiadne hard-coded defaults — resolve z profile.plan ak chýba arg
    if batt_kwh is None or fallback_soc_pct is None:
        try:
            import profiles as _pr
            _p = _pr.load_profile(profile) if profile else {}
            _pl = (_p or {}).get("plan") or {}
            if batt_kwh is None:
                batt_kwh = float(_pl.get("batt_kwh") or 800.0)
            if fallback_soc_pct is None:
                fallback_soc_pct = float(_pl.get("fallback_soc_pct") or 50.0)
        except Exception:
            if batt_kwh is None: batt_kwh = 800.0
            if fallback_soc_pct is None: fallback_soc_pct = 50.0

    mode = _get_active_profile_mode(profile=profile)

    # SIMULATION profile — čítaj zo simulácie, NIE z realio_db
    if mode == "simulation":
        sim = _soc_from_livesim_trace()
        if sim is not None:
            return sim
        return {"ok": True, "soc_pct": float(fallback_soc_pct),
                "source": f"fallback {fallback_soc_pct:.0f}% (simulation, žiadny "
                          f"livesim trace pre dnes)",
                "ts": "", "age_minutes": -1, "error": ""}

    # REAL profile — z realio_db (SQLite)
    try:
        import realio as _r
        import datetime as _dt
        import pandas as _pd
        df = _r.read_recent(n_minutes=max_age_minutes)
        if (df is not None and not df.empty
                and "batt_soc_pct" in df.columns and "time" in df.columns):
            sub = df[df["batt_soc_pct"].notna()]
            if len(sub) > 0:
                soc = float(sub["batt_soc_pct"].iloc[-1])
                ts_raw = sub["time"].iloc[-1]
                ts_str = str(ts_raw)[:19]
                age_min = 0.0
                try:
                    ts_dt = _pd.Timestamp(ts_raw).to_pydatetime()
                    if ts_dt.tzinfo is not None:
                        ts_dt = ts_dt.replace(tzinfo=None)
                    age_min = abs((_dt.datetime.now() - ts_dt).total_seconds()) / 60.0
                except Exception:
                    pass
                if age_min <= max_age_minutes:
                    return {"ok": True, "soc_pct": soc,
                            "source": f"realio_db (profile: {mode}, vek: {age_min:.0f} min)",
                            "ts": ts_str, "age_minutes": age_min, "error": ""}
    except Exception as e:
        return {"ok": True, "soc_pct": float(fallback_soc_pct),
                "source": "fallback (realio chyba)",
                "ts": "", "age_minutes": -1,
                "error": f"realio chyba: {e}"}

    # Real profile bez realio dát → skús D-1 plán pred konečným fallback
    d1 = _soc_from_d1_plan(profile=profile)
    if d1 is not None:
        return d1
    return {"ok": True, "soc_pct": float(fallback_soc_pct),
            "source": f"fallback {fallback_soc_pct:.0f}% (žiadne čerstvé realio dáta ani D-1 plán, profile: {mode})",
            "ts": "", "age_minutes": -1, "error": ""}


def _load_dam_commitments_for_snapshot(snapshot, today_date,
                                         basis: str = "batt",
                                         profile: Optional[str] = None) -> Optional[list]:
    """Načíta DAM plán pre dnes (a zajtra ak je v snapshot) a namatchuje na sloty snapshotu.

    Args:
        basis: "batt" → di_kwh − ch_kwh (batt arbitráž, pre LP lower bound).
               "grid" → ex_kwh − im_kwh (full grid nominácia, pre diagnostiku).
        profile: ktorý profile použiť pre lookup v plan_store (musí sa zhodovať
                 s profilom, pod ktorým bol D-1 plán uložený cez /vdt/d1).

    Vracia list dĺžky `len(snapshot)` s kWh per slot.
    None ak žiadny DAM plán nie je dostupný.
    """
    try:
        import d1_planner as _d1p
        import pandas as _pd
    except Exception:
        return None

    # Zbieram unique dni v snapshote
    days_in_snap = set()
    for _, r in snapshot.iterrows():
        d = r.get("date") or str(r["start_local"])[:10]
        days_in_snap.add(d)

    # Pre každý deň skús načítať DAM plán
    dam_maps = {}  # date_iso → {slot_idx: kWh}
    for d_iso in days_in_snap:
        try:
            import datetime as _dt
            d_obj = _dt.date.fromisoformat(d_iso)
            commits = _d1p.get_dam_commitments(d_obj, basis=basis,
                                                  profile=profile)
            if commits and len(commits) == 96:
                dam_maps[d_iso] = commits
        except Exception:
            pass

    if not dam_maps:
        return None

    # Namatchuj na snapshot sloty
    out = []
    has_any = False
    for _, r in snapshot.iterrows():
        d_iso = r.get("date") or str(r["start_local"])[:10]
        if d_iso in dam_maps:
            try:
                start = r["start_local"]
                if hasattr(start, "to_pydatetime"):
                    start = start.to_pydatetime()
                slot_idx = (start.hour * 60 + start.minute) // 15
                val = float(dam_maps[d_iso][slot_idx]) if 0 <= slot_idx < 96 else 0.0
                out.append(val)
                if abs(val) > 0.01:
                    has_any = True
            except Exception:
                out.append(0.0)
        else:
            out.append(0.0)
    return out if has_any else None


def get_live_recommendation(*,
                              batt_kw: Optional[float] = None,
                              batt_kwh: Optional[float] = None,
                              eff_c: Optional[float] = None,
                              eff_d: Optional[float] = None,
                              grid_fee: Optional[float] = None,
                              cycle_cost: Optional[float] = None,
                              min_spread: Optional[float] = None,
                              soc_min_pct: Optional[float] = None,
                              soc_max_pct: Optional[float] = None,
                              soc_start_pct: Optional[float] = None,
                              soc_end_min_pct: Optional[float] = None,
                              max_cycles_per_day: Optional[float] = None,
                              use_orderbook: bool = True,
                              use_dam_commitments: bool = True,
                              profile: Optional[str] = None,
                              ) -> Dict[str, Any]:
    """Spustí rolling MPC re-optimization a vráti odporúčanie.

    Args:
        soc_start_pct: ak None → načíta sa z Realio (real-time meranie).
                       Inak ručný override pre what-if scenár.
        max_cycles_per_day: typicky vyšší než pri static (povolíme viac cyklov
                            keď LP reaguje na intra-day price moves).

    Vracia:
        {
          "ok": bool,
          "ts": ISO timestamp simulácie,
          "soc": {"pct", "source", "ts"},
          "current": {"slot", "action", "kw", "kwh_per_slot", "price_eur_mwh",
                      "reason"},
          "preview": [{slot, action, kw, kwh, price, soc_after_pct}, ... next ~16 slotov],
          "summary": {profit_eur, cycles, n_charge, n_discharge},
          "orderbook_status": "✓ N ponúk" | "✗ ..."
        }
    """
    import pandas as pd
    try:
        import vdt_arbitrage as _arb
        import vdt_optimizer as _opt
        import okte_vdt as _vdt
        import plan_store as _ps
    except Exception as e:
        return {"ok": False, "error": f"modul nedostupný: {e}"}

    # 0. Resolve profile EXPLICITNE — všetky vnútorné volania budú používať
    # ten istý profile name (consistent s /vdt/d1 ktorá tiež resolveduje).
    # Volajúci môže poslať explicit name (UI z formulára), inak fallback na active.
    try:
        active_profile = _ps.resolve_profile(profile)
    except Exception:
        active_profile = profile or "default"

    # 0a. PROFILE PARAMS — Bug P: žiadne hard-coded defaults. Všetky chýbajúce args
    # sa naplnia z profile.plan (ktorý profiles.load_profile auto-doplnil VDT defaults
    # cez _ensure_plan_vdt_defaults). Volajúci môže override-nuť ktorýkoľvek arg.
    try:
        import profiles as _pr
        _prof_data = _pr.load_profile(active_profile) or {}
        _pl = _prof_data.get("plan") or {}
    except Exception:
        _pl = {}
    if batt_kw is None:
        batt_kw = float(_pl.get("batt_kw") or 500.0)
    if batt_kwh is None:
        batt_kwh = float(_pl.get("batt_kwh") or 800.0)
    if eff_c is None:
        eff_c = float(_pl.get("eff_c") or 0.95)
    if eff_d is None:
        eff_d = float(_pl.get("eff_d") or 0.95)
    if grid_fee is None:
        grid_fee = float(_pl.get("grid_fee_vdt") or _pl.get("grid_fee") or 22.0)
    if cycle_cost is None:
        cycle_cost = float(_pl.get("cycle_cost_vdt") or _pl.get("cycle_cost") or 2.0)
    if min_spread is None:
        min_spread = float(_pl.get("min_spread_eur") or 5.0)
    if soc_min_pct is None:
        soc_min_pct = float(_pl.get("soc_min_pct") or 5.0)
    if soc_max_pct is None:
        # VDT advisor používa OPERAČNÝ strop (95%), nie fyzikálny (100%) zo plan.soc_max_pct
        soc_max_pct = float(_pl.get("soc_max_pct_operational") or 95.0)
    if soc_end_min_pct is None:
        soc_end_min_pct = float(_pl.get("soc_end_min_pct") or 20.0)
    if max_cycles_per_day is None:
        max_cycles_per_day = float(_pl.get("max_cycles_per_day") or 3.0)

    # 0b. SINGLE SOURCE OF TRUTH (Bug O fix) — vždy pred LP zavolaj vdt_state
    # ktorý dá kompletný kontext: kumulatívny SOC od 00:00, DAM nominácia z D-1 plánu,
    # všetky realizované VDT trades z paper_trades CSV. Ak chýba čokoľvek z toho,
    # VDT NESMIE obchodovať — vrátime error result s warnings + missing_items.
    state = None
    try:
        import vdt_state as _vs
        state = _vs.compute_current_state(profile=active_profile,
                                              batt_kwh=batt_kwh)
        if not state.get("data_completeness"):
            return {"ok": False,
                    "error": ("VDT NEMÔŽE OBCHODOVAŤ — chýba kontext: "
                              + ", ".join(state.get("missing_items", []))),
                    "data_completeness": False,
                    "missing_items": state.get("missing_items", []),
                    "warnings": state.get("warnings", []),
                    "state": state,
                    "profile": active_profile}
    except Exception as e:
        return {"ok": False,
                "error": f"vdt_state.compute_current_state zlyhal: {e}",
                "data_completeness": False,
                "missing_items": ["state_module_error"],
                "warnings": [f"Modul vdt_state nedostupný alebo chyba: {e}"],
                "profile": active_profile}

    # 1. SOC — z state (kumulatívny výpočet) alebo manuálny override
    if soc_start_pct is None:
        soc_pct = float(state["current_soc_pct"])
        soc_source = {"ok": True, "soc_pct": soc_pct,
                      "source": (f"vdt_state kumulatívny (start={state['start_soc_pct']:.1f}%, "
                                    f"VDT trades={state['vdt_realized_count']}, slot={state['current_slot_idx']})"),
                      "ts": state["now"], "age_minutes": 0.0, "error": "",
                      "start_soc_source": state["start_soc_source"]}
    else:
        soc_pct = float(soc_start_pct)
        soc_source = {"ok": True, "soc_pct": soc_pct, "source": "manual",
                      "ts": dt.datetime.now().isoformat(timespec="seconds"),
                      "error": ""}

    # 2. Snapshot dnes + zajtra
    today = dt.date.today()
    try:
        snapshot = _arb.get_market_snapshot(today, days_ahead=1, from_current_slot=True)
    except Exception as e:
        return {"ok": False, "error": f"snapshot zlyhal: {e}", "soc": soc_source}

    # 3. Orderbook overlay (live bid/ask + likvidita)
    ob_status = "(nedostupné)"
    if use_orderbook:
        try:
            ob_res = _vdt.get_orderbook(delivery_duration=15)
            snapshot = _arb.add_orderbook(snapshot, ob_res)
            if ob_res.get("ok"):
                ob_status = f"✓ {ob_res.get('stats',{}).get('trades_parsed',0)} ponúk"
            else:
                ob_status = f"✗ {str(ob_res.get('error',''))[:60]}"
        except Exception as e:
            ob_status = f"✗ exception: {str(e)[:60]}"

    # 3b. DAM commitments — načítaj plán z plan_store pre dni v snapshote.
    # Pre LP používame BATT basis (di_kwh − ch_kwh) — to je nominácia ktorú musí
    # batt fyzicky pokryť. Grid basis (ex_kwh − im_kwh) zahrňuje FTV cestu
    # ktorú VDT LP nemodeluje — preto je len pre diagnostiku.
    dam_commits = None              # batt arbitráž → posunie sa do LP
    dam_commits_grid = None         # full grid nominácia → diagnostika UI
    dam_status = "(žiadny D-1 plán)"
    if use_dam_commitments:
        try:
            # Sondovanie: ktorý kind cascade vyberie?
            dam_source_kind = None
            try:
                import d1_planner as _d1p
                import plan_store as _ps
                for _k in ("dentrh", "plan"):
                    _step = 60 if _k == "plan" else 15
                    if _ps.has_plan(today.isoformat(), step_min=_step,
                                       kind=_k, profile=active_profile):
                        dam_source_kind = _k
                        break
            except Exception:
                pass

            dam_commits = _load_dam_commitments_for_snapshot(snapshot, today,
                                                                basis="batt",
                                                                profile=active_profile)
            dam_commits_grid = _load_dam_commitments_for_snapshot(snapshot, today,
                                                                    basis="grid",
                                                                    profile=active_profile)
            if dam_commits:
                batt_total = sum(abs(v) for v in dam_commits)
                grid_total = sum(abs(v) for v in (dam_commits_grid or []))
                src = f" zdroj={dam_source_kind}" if dam_source_kind else ""
                if grid_total > batt_total + 1:
                    dam_status = (f"✓ D-1 [{active_profile}{src}]: batt ±{batt_total:.0f} kWh "
                                  f"(z grid noms ±{grid_total:.0f} kWh, "
                                  f"rozdiel = FTV cesta)")
                else:
                    dam_status = f"✓ D-1 [{active_profile}{src}]: batt ±{batt_total:.0f} kWh"
            else:
                dam_status = (f"(žiadny D-1 plán pre profile '{active_profile}' "
                              f"v plan_store — skús /plan, /dentrh alebo /vdt/d1)")
        except Exception as e:
            dam_status = f"✗ DAM error: {str(e)[:50]}"

    # 3c. ZCO príležitosti (Fáza C1) — POČÍTA SA NEZÁVISLE OD LP, aby fungovalo
    # aj keď je LP infeasible. Používa snapshot + dam_commits + DAM/VDT ceny.
    zco_info = {"ok": False, "opportunities": [], "total_profit_eur": 0.0}
    if dam_commits:
        try:
            import zco_advisor as _zco
            # Pre ZCO potrebujeme proxy full_plan s buy/sell cenami z snapshotu
            _fp_proxy = []
            for _idx in range(len(snapshot)):
                _r = snapshot.iloc[_idx]
                _fp_proxy.append({
                    "slot": str(_r.get("period", "")),
                    "buy_price": _r.get("ob_best_ask_eur_mwh"),
                    "sell_price": _r.get("ob_best_bid_eur_mwh"),
                    "price": _r.get("price_eur"),
                })
            zco_info = _zco.compute_zco_opportunities(
                snapshot=snapshot,
                dam_commits=dam_commits,
                full_plan=_fp_proxy,
                date_iso=today.isoformat(),
                grid_fee=grid_fee,
                days_window=7,
                top_n=8,
            )
        except Exception as e:
            zco_info = {"ok": False, "error": str(e)[:120],
                          "opportunities": [], "total_profit_eur": 0.0}

    # 4. LP optimize
    try:
        result = _opt.optimize_vdt_day(
            snapshot,
            batt_kw=batt_kw, batt_kwh=batt_kwh,
            eff_c=eff_c, eff_d=eff_d,
            grid_fee=grid_fee, cycle_cost=cycle_cost,
            min_spread=min_spread,
            soc_min_pct=soc_min_pct, soc_max_pct=soc_max_pct,
            soc_start_pct=soc_pct,
            soc_end_min_pct=soc_end_min_pct,
            max_cycles_per_day=max_cycles_per_day,
            slot_minutes=15,
            use_orderbook=use_orderbook,
            future_only=True,
            dam_commitments=dam_commits,
        )
    except Exception as e:
        return {"ok": False, "error": f"optimizer zlyhal: {e}",
                "soc": soc_source, "orderbook_status": ob_status,
                "dam_status": dam_status, "zco": zco_info,
                "profile": active_profile}

    # Auto-retry pri Infeasible: najčastejšia príčina je SOC na limite + DAM commitment
    # ktorý núti charge/discharge bez možnosti realizácie. Skúsime LP znova bez
    # dam_commitments a vrátime výsledok s warning (DAM odchýlka pôjde cez ZCO).
    if not result.get("ok") and dam_commits is not None:
        err1 = str(result.get("error", ""))
        if "infeasible" in err1.lower() or "HiGHS Status 8" in err1:
            try:
                import vdt_optimizer as _opt
                # Bug #602: funkcia bola premenovana na optimize_vdt_day.
                # compute_optimal_trades neexistuje -> AttributeError -> retry path
                # vzdy zlyhal a infeasible LP s DAM commits sa nikdy nezachranil.
                _opt2 = _opt.optimize_vdt_day(
                    snapshot=snapshot,
                    batt_kw=batt_kw, batt_kwh=batt_kwh,
                    eff_c=eff_c, eff_d=eff_d,
                    grid_fee=grid_fee, cycle_cost=cycle_cost, min_spread=min_spread,
                    soc_min_pct=soc_min_pct, soc_max_pct=soc_max_pct,
                    soc_start_pct=soc_pct,
                    soc_end_min_pct=soc_end_min_pct,
                    max_cycles_per_day=max_cycles_per_day,
                    slot_minutes=15,
                    use_orderbook=use_orderbook,
                    future_only=True,
                    dam_commitments=None,   # ⚠ vyhadzujeme DAM commitments
                )
                if _opt2.get("ok"):
                    result = _opt2
                    dam_status = (dam_status + " · ⚠ relaxed (LP infeasible s commits)"
                                   if dam_status else "⚠ relaxed (LP infeasible)")
                    print(f"[vdt_live_advisor] LP infeasible s DAM — retry bez "
                          f"commits: OK · {len(_opt2.get('trades',[]))} trades")
            except Exception as _e_r:
                print(f"[vdt_live_advisor] retry zlyhal: {_e_r}")

    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "?"),
                "soc": soc_source, "orderbook_status": ob_status,
                "dam_status": dam_status, "zco": zco_info,
                "profile": active_profile}

    trades = result["trades"]
    if not trades:
        return {"ok": True, "ts": dt.datetime.now().isoformat(timespec="seconds"),
                "soc": soc_source,
                "current": {"slot": "—", "action": "idle", "kw": 0.0,
                              "kwh_per_slot": 0.0, "price_eur_mwh": None,
                              "reason": "Žiadne sloty v LP horizonte"},
                "preview": [],
                "summary": result["summary"],
                "orderbook_status": ob_status,
                "dam_status": dam_status,
                "dam_committed_export_kwh": result.get("dam_committed_export_kwh", 0.0),
                "dam_committed_import_kwh": result.get("dam_committed_import_kwh", 0.0),
                "vdt_extra_discharge_kwh": result.get("vdt_extra_discharge_kwh", 0.0),
                "vdt_extra_charge_kwh": result.get("vdt_extra_charge_kwh", 0.0),
                "profit_eur": result["profit_eur"]}

    # Current slot = prvý trade
    cur = trades[0]
    cur_action = cur["action"]
    cur_kwh = cur["charge_kwh"] if cur_action == "charge" else (
        cur["discharge_kwh"] if cur_action == "discharge" else 0.0)
    cur_kw = cur_kwh / 0.25 if cur_kwh > 0 else 0.0   # kW priemer cez 15 min
    cur_price = (cur.get("buy_price_eur_mwh") if cur_action == "charge"
                 else cur.get("sell_price_eur_mwh"))

    # Reason — krátky text čo robí
    if cur_action == "charge":
        reason = (f"Lacná elektrina @ {cur_price:.1f} €/MWh — nabíjam batériu. "
                  f"Plán predať v drahšom slote neskôr.")
    elif cur_action == "discharge":
        reason = (f"Drahá elektrina @ {cur_price:.1f} €/MWh — vybíjam batériu "
                  f"a predávam na sieť.")
    else:
        reason = "Žiadny profitný pár — batéria zostáva nečinná."

    current = {
        "slot": cur["slot"],
        "action": cur_action,
        "kw": cur_kw,
        "kwh_per_slot": cur_kwh,
        "price_eur_mwh": cur_price,
        "soc_after_kwh": cur["soc_after_kwh"],
        "soc_after_pct": cur["soc_after_pct"],
        "reason": reason,
    }

    # Preview — nasledujúcich 16 slotov (4h)
    preview = []
    for t in trades[1:17]:
        a = t["action"]
        kwh = t["charge_kwh"] if a == "charge" else (
            t["discharge_kwh"] if a == "discharge" else 0.0)
        price = (t.get("buy_price_eur_mwh") if a == "charge"
                 else t.get("sell_price_eur_mwh"))
        preview.append({
            "slot": t["slot"],
            "action": a,
            "kw": kwh / 0.25 if kwh > 0 else 0.0,
            "kwh": kwh,
            "price": price,
            "soc_after_pct": t["soc_after_pct"],
        })

    # Full plan (pre graf)
    full_plan = []
    for t in trades:
        a = t["action"]
        kwh = t["charge_kwh"] if a == "charge" else (
            t["discharge_kwh"] if a == "discharge" else 0.0)
        full_plan.append({
            "slot": t["slot"],
            "action": a,
            "kwh": kwh,
            "buy_price": t.get("buy_price_eur_mwh"),
            "sell_price": t.get("sell_price_eur_mwh"),
            "soc_after_pct": t["soc_after_pct"],
        })

    # orderbook_per_slot — pre vdt_extras (extra obchody nad DAM)
    orderbook_per_slot: Dict[int, Dict[str, float]] = {}
    try:
        for _, _row in snapshot.iterrows():
            try:
                _si = int(_row.get("slot_idx", -1))
            except Exception:
                continue
            if _si < 0 or _si >= 96:
                continue
            _be = _row.get("ob_best_bid_eur")
            _ae = _row.get("ob_best_ask_eur")
            _rec: Dict[str, float] = {}
            if _be is not None:
                try:
                    _rec["bid_eur"] = float(_be)
                    _rec["bid_mw"] = float(_row.get("ob_best_bid_mw") or 0.0)
                except Exception:
                    pass
            if _ae is not None:
                try:
                    _rec["ask_eur"] = float(_ae)
                    _rec["ask_mw"] = float(_row.get("ob_best_ask_mw") or 0.0)
                except Exception:
                    pass
            if _rec:
                orderbook_per_slot[_si] = _rec
    except Exception:
        pass

    out = {
        "ok": True,
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "profile": active_profile,
        "soc": soc_source,
        "current": current,
        "preview": preview,
        "full_plan": full_plan,
        # Bug O: single source of truth state diagnostika pre UI
        "state": state if state is not None else {"data_completeness": False,
                                                          "missing_items": ["state_not_computed"]},
        "data_completeness": (state.get("data_completeness", False) if state else False),
        "summary": result["summary"],
        "profit_eur": result["profit_eur"],
        "n_slots": result["n_slots"],
        "orderbook_status": ob_status,
        "orderbook_per_slot": orderbook_per_slot,
        "dam_status": dam_status,
        "dam_commits": dam_commits or [],            # BATT účasť (LP lower bound)
        "dam_commits_grid": dam_commits_grid or [],  # FULL grid nominácia (diag)
        "zco": zco_info,                              # ZCO príležitosti (Fáza C1)
        "dam_committed_export_kwh": result.get("dam_committed_export_kwh", 0.0),
        "dam_committed_import_kwh": result.get("dam_committed_import_kwh", 0.0),
        "vdt_extra_discharge_kwh": result.get("vdt_extra_discharge_kwh", 0.0),
        "vdt_extra_charge_kwh": result.get("vdt_extra_charge_kwh", 0.0),
        "params": {
            "batt_kw": batt_kw, "batt_kwh": batt_kwh,
            "eff_c": eff_c, "eff_d": eff_d,
            "grid_fee": grid_fee, "cycle_cost": cycle_cost,
            "min_spread": min_spread,
            "max_cycles_per_day": max_cycles_per_day,
            "soc_min_pct": soc_min_pct, "soc_max_pct": soc_max_pct,
            "soc_end_min_pct": soc_end_min_pct,
        },
    }
    return out


def save_cache(result: Dict[str, Any]) -> None:
    """Uloží advisor result do JSON cache (pre rýchly UI load).

    Profile-aware: ak result obsahuje 'profile', uloží sa do per-profile súboru,
    inak fallback na shared cache.
    """
    prof = str(result.get("profile") or "") if isinstance(result, dict) else ""
    p = cache_path(prof or None)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    try:
        with open(p, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        print(f"[vdt_live_advisor.save_cache] zlyhalo: {e}")


def paper_trades_csv_path(profile: Optional[str] = None) -> str:
    """Cesta k paper trading log CSV.

    Fáza B.1: deleguje na core/paths.vdt_trades_csv_path() pre dual-mode
    (legacy shared CSV vs sandbox per-profile CSV).

    Legacy režim (FTV_SANDBOX nezapnutý) ignoruje `profile` parameter — vracia
    shared CSV (filtrovanie podľa profile column).
    """
    try:
        from core.paths import vdt_trades_csv_path
        return vdt_trades_csv_path(profile)
    except Exception:
        pass
    # Legacy fallback
    try:
        import market as _mk
        root = os.path.dirname(_mk.data_dir().rstrip("/").rstrip(os.sep)) or "out"
    except Exception:
        root = "out"
    return os.path.join(root, "sk", "vdt_paper_trades.csv")


def _migrate_paper_trades_csv_add_profile(path: str) -> None:
    """Ak existujúci CSV nemá 'profile' column, prepíše ho s prázdnou hodnotou.

    Idempotent. Best-effort priradenie pre staré riadky:
      - soc_source obsahuje "profile: simulation" → '' (filter ich vyhodí)
      - soc_source obsahuje "profile: real" → meno aktívneho REAL profilu z profiles.py
        (predpoklad: na real mode máš zvyčajne jeden profile)
      - inak → ''
    """
    if not os.path.exists(path):
        return
    import csv
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            rows = list(reader)
        if not rows:
            return
        header = rows[0]
        if "profile" in header:
            return   # už migrované

        # Nájdi index soc_source v starej hlavičke (zvyčajne index 8)
        try:
            src_idx = header.index("soc_source")
        except ValueError:
            src_idx = -1

        # Best-effort: nájdi prvý real-mode profil
        real_prof = ""
        try:
            import profiles as _pr
            for _name in (_pr.list_profiles() or []):
                _p = _pr.load_profile(_name) if isinstance(_name, str) else None
                if isinstance(_p, dict) and str(_p.get("mode", "")).lower() == "real":
                    real_prof = _name
                    break
        except Exception:
            pass

        new_header = [header[0], "profile"] + header[1:]
        new_rows = [new_header]
        n_real = 0; n_sim = 0; n_unk = 0
        for r in rows[1:]:
            if len(r) < len(header):
                continue
            src = r[src_idx] if 0 <= src_idx < len(r) else ""
            if "profile: real" in src and real_prof:
                prof = real_prof; n_real += 1
            elif "profile: simulation" in src:
                prof = ""; n_sim += 1   # sim trades sa pri renderingu odfiltrujú
            else:
                prof = ""; n_unk += 1
            new_rows.append([r[0], prof] + r[1:])

        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            for r in new_rows:
                w.writerow(r)
        os.replace(tmp_path, path)
        print(f"[vdt_paper_trades] migrated CSV: pridaná 'profile' column, "
              f"{len(new_rows)-1} riadkov · real={n_real}({real_prof or '—'}) · "
              f"sim={n_sim} · unknown={n_unk}")
    except Exception as e:
        print(f"[vdt_paper_trades migration] zlyhalo: {e}")


def _is_sk_market() -> bool:
    """VDT obchodovanie je SK trh (OKTE). Na CZ nemáme prístup k českému VDT,
    takže paper trades writers tu musia byť no-op."""
    try:
        import market as _mk
        return (_mk.active_market() or "").lower() == "sk"
    except Exception:
        return True   # fallback: nepoznáme market — radšej dovoľ (default beh)


def append_extra_paper_trade(profile: str, slot: str, action: str,
                                kwh: float, price_eur_mwh: float,
                                reason: str, soc_pct: float = 0.0,
                                profit_eur: float = 0.0) -> None:
    """UPSERT jednej extra (CURTAIL_FTV / LOAD_COVER / BUY / SELL) akcie do paper trading CSV.

    Plánované VDT extras sa zapisujú pri každom advisor cron-tick (5-min) pre celý
    full_plan (96 slotov × N profilov). Bez UPSERT vznikajú duplikáty, ktoré
    pri agregácii v chPlan grafe nafukujú výkony 2-3×.

    Preto pred zápisom odstránime existujúce riadky pre rovnakú kombináciu
    (profile, slot, action) v dnešnom dni a zapíšeme len najnovší.

    Pre minulé dni sa nič nemení (immutable archive).

    CZ guard: ak aktívny trh je "cz", nepíšeme nič (nemáme prístup k českému VDT).

    Bug UU built-in (Fáza A.3, 2026-06-08): pred zápisom volá centrálny gate
    `should_log_vdt_for_profile(profile)` — ak profile.plan.joint_lp.use_vdt:false,
    NEPÍŠEME nič. Predtým bol gate iba v auto_control.log_vdt_extras_for_current_slot
    a iné writers cez tento helper mohli omylom napísať fake VDT trade.
    """
    if not _is_sk_market():
        return
    # Bug UU gate — centralizovaný v core.schemas.vdt
    try:
        from core.schemas.vdt import should_log_vdt_for_profile as _vdt_gate
        if not _vdt_gate(profile):
            return
    except Exception as _e_gate:
        print(f"[append_extra_paper_trade] use_vdt gate zlyhal pre {profile}: {_e_gate}")
        # Pokračuj cautiously (legacy fallback)
    import csv
    # Fáza B.1: sandbox vyžaduje profile (per-profile CSV); legacy ho ignoruje
    path = paper_trades_csv_path(profile)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _migrate_paper_trades_csv_add_profile(path)
    new_file = not os.path.exists(path)
    today = dt.date.today().isoformat()
    ts_now = dt.datetime.now().isoformat(timespec="seconds")

    # Bug #612: capacity audit pre extras (BUY/SELL/CHARGE/DISCHARGE).
    # CURTAIL_FTV a LOAD_COVER nepoužívajú batt kapacitu → preskočia audit.
    _act_upper = str(action or "").upper()
    if _act_upper in ("CHARGE", "DISCHARGE", "BUY", "SELL"):
        try:
            from core.capacity_ledger import audit_vdt_order, slot_idx_from_time
            import profiles as _ps_audit
            _prof_obj = _ps_audit.load_profile(profile) or {}
            _batt_kw_max = float((_prof_obj.get("plan") or {}).get("batt_kw", 0.0) or 0.0)
            if _batt_kw_max > 0 and slot and ":" in str(slot):
                _hh = int(str(slot)[:2]); _mm = int(str(slot)[3:5])
                _slot_idx2 = slot_idx_from_time(_hh, _mm)
                _direction = "discharge" if _act_upper in ("DISCHARGE", "SELL") else "charge"
                _trade_id = f"{profile}_{today}_{slot}_{_act_upper}_extras"
                _ax = audit_vdt_order(profile, today, _slot_idx2, _direction,
                                        abs(float(kwh or 0)), _batt_kw_max, _trade_id)
                if _ax["decision"] == "reject":
                    print(f"[append_extra_paper_trade #612] REJECT {profile} slot={slot} "
                          f"{_act_upper}: {_ax['note']}")
                    return   # nezapisujeme
                if _ax["decision"] == "downscale":
                    _sign = -1.0 if kwh < 0 else 1.0
                    kwh = _ax["allowed_kwh"] * _sign
                    print(f"[append_extra_paper_trade #612] DOWNSCALE {profile} slot={slot} "
                          f"{_act_upper}: {_ax['note']}")
        except Exception as _e_audit:
            # Bug #625-C (2026-06-09): fail-CLOSED — REJECT pri chybe auditu.
            print(f"[append_extra_paper_trade #612+#625-C] audit zlyhal pre {profile}/{slot}: {_e_audit} → REJECT (fail-closed)")
            return

    # 1) Načítaj existujúce riadky a odfiltruj duplikáty pre dnes
    existing_rows: list = []
    header: list = []
    if not new_file:
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                rdr = csv.reader(f)
                rows = list(rdr)
            if rows:
                header = rows[0]
                ts_idx = header.index("ts") if "ts" in header else 0
                prof_idx = header.index("profile") if "profile" in header else 1
                slot_idx = header.index("slot") if "slot" in header else 2
                act_idx = header.index("action") if "action" in header else 3
                for r in rows[1:]:
                    if len(r) <= max(ts_idx, prof_idx, slot_idx, act_idx):
                        existing_rows.append(r)   # nedostatočne dlhý riadok — ponechaj
                        continue
                    ts_v = (r[ts_idx] or "")[:10]   # YYYY-MM-DD
                    same_today = (ts_v == today)
                    same_key = (r[prof_idx] == str(profile or "")
                                 and r[slot_idx] == str(slot or "")
                                 and r[act_idx] == str(action or ""))
                    if same_today and same_key:
                        continue   # duplikát — vyhoď
                    existing_rows.append(r)
        except Exception:
            existing_rows = []
            header = []
    if not header:
        header = ["ts", "profile", "slot", "action", "kw", "kwh",
                  "price_predicted_eur", "soc_before_pct", "soc_after_pct",
                  "soc_source", "profit_eur_rest_of_day"]

    # 2) Pripoj nový (najnovší) riadok
    dt_h = 0.25
    kw_val = kwh / dt_h if dt_h > 0 else kwh
    new_row = [
        ts_now,
        str(profile or ""),
        str(slot or ""),
        str(action or ""),
        f"{kw_val:.2f}",
        f"{kwh:.2f}",
        f"{price_eur_mwh:.2f}",
        f"{soc_pct:.2f}",
        f"{soc_pct:.2f}",
        str(reason or "")[:60],
        f"{profit_eur:.3f}",
    ]

    # 3) Atomický prepis CSV
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            for r in existing_rows:
                w.writerow(r)
            w.writerow(new_row)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[append_extra_paper_trade] zlyhalo: {e}")
    # DB dual write
    _vdt_db_upsert(
        profile=profile, slot=slot, action=action, ts=ts_now,
        kwh=float(kwh), price=float(price_eur_mwh),
        soc_before=float(soc_pct), soc_after=float(soc_pct),
        delta_profit=float(profit_eur), source=str(reason)[:60],
    )


def append_paper_trade(result: Dict[str, Any]) -> None:
    """Append jednu odporúčanú akciu do paper trading log CSV.

    Cieľ: zaznamenať odporúčanie aby sme ho mohli neskôr porovnať s realitou
    (čo sa naozaj zobchodovalo na VDT/clearing) a získať trust v presnosť MPC.

    Schema (CSV):
        ts            — kedy bol advisor spustený
        profile       — meno profilu (pridané fix #441 pre filter v chPlan)
        slot          — pre ktorý 15-min slot (current)
        action        — charge/discharge/idle/both
        kw, kwh       — odporúčaná akcia
        price_predicted_eur — ask (charge) alebo bid (discharge) z orderbooku
        soc_before_pct, soc_after_pct
        soc_source    — realio_db / manual / fallback
        profit_eur    — očakávaný profit za zvyšok dňa
    """
    if not result or not result.get("ok"):
        return
    # CZ guard — nepíš nič na CZ trhu (nemáme prístup k českému VDT).
    if not _is_sk_market():
        return
    profile = str(result.get("profile", "") or "")
    # Bug UU gate (Fáza A.3) — explicit joint_lp.use_vdt:false → block
    try:
        from core.schemas.vdt import should_log_vdt_for_profile as _vdt_gate
        if profile and not _vdt_gate(profile):
            return
    except Exception as _e_gate:
        print(f"[append_paper_trade] use_vdt gate zlyhal pre {profile}: {_e_gate}")
    import csv
    cur = result.get("current") or {}
    soc = result.get("soc") or {}
    slot = str(cur.get("slot", "") or "")
    action = str(cur.get("action", "") or "")

    # Bug #612: capacity audit gate. VDT order musí prejsť cez ledger pred zápisom.
    # Ak voľná kapacita (= batt_kw_max − Σ rezervácie) nestačí → downscale alebo reject.
    # IDLE akcie sa nepasujú cez audit (žiadna rezervácia kapacity).
    _action_upper = action.upper()
    if _action_upper in ("CHARGE", "DISCHARGE", "BUY", "SELL", "BOTH"):
        try:
            from core.capacity_ledger import audit_vdt_order, slot_idx_from_time
            import profiles as _ps_audit
            _prof_obj = _ps_audit.load_profile(profile) or {}
            _batt_kw_max = float((_prof_obj.get("plan") or {}).get("batt_kw", 0.0) or 0.0)
            if _batt_kw_max > 0 and slot and ":" in slot:
                _hh = int(slot[:2]); _mm = int(slot[3:5])
                _slot_idx = slot_idx_from_time(_hh, _mm)
                _today = dt.date.today().isoformat()
                _kwh_req = float(cur.get("kwh_per_slot", 0) or 0)
                _direction = "discharge" if _action_upper in ("DISCHARGE", "SELL") else "charge"
                if _action_upper == "BOTH":
                    # BOTH = nabíja aj vybíja v rovnakom slote — preskočíme audit (zložité)
                    pass
                elif abs(_kwh_req) > 0:
                    _trade_id = f"{profile}_{_today}_{slot}_{_action_upper}"
                    _ax = audit_vdt_order(profile, _today, _slot_idx, _direction,
                                            abs(_kwh_req), _batt_kw_max, _trade_id)
                    if _ax["decision"] == "reject":
                        print(f"[append_paper_trade #612] REJECT {profile} slot={slot} "
                              f"{_action_upper}: {_ax['note']}")
                        return   # nezapisujeme nič
                    if _ax["decision"] == "downscale":
                        # Uprav kwh + kw na povolenú časť
                        _allowed = _ax["allowed_kwh"]
                        _sign = -1.0 if _kwh_req < 0 else 1.0
                        cur["kwh_per_slot"] = _allowed * _sign
                        cur["kw"] = _allowed * 4.0 * _sign   # 15-min → kW
                        print(f"[append_paper_trade #612] DOWNSCALE {profile} slot={slot} "
                              f"{_action_upper}: {_kwh_req:.2f}→{_allowed:.2f} kWh ({_ax['note']})")
                    # decision == "accept" → pokračuj bez zmeny
        except Exception as _e_audit:
            # Bug #625-C (2026-06-09): fail-CLOSED. Pri chybe auditu REJECT trade.
            # Plán nesmie obsahovať nominácie ktoré neprešli kapacitnou kontrolou,
            # inak vznikne pretek nad batt_kw_max → odchýlka voči realite = pokuta.
            print(f"[append_paper_trade #612+#625-C] audit zlyhal pre {profile}/{slot}: {_e_audit} → REJECT (fail-closed)")
            return
    # Fáza B.1: sandbox vyžaduje profile (per-profile CSV)
    path = paper_trades_csv_path(profile)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _migrate_paper_trades_csv_add_profile(path)
    new_file = not os.path.exists(path)
    today = dt.date.today().isoformat()

    # UPSERT: pre dnes a rovnakú kombináciu (profile, slot, action) odstráň existujúce
    existing_rows: list = []
    header: list = []
    if not new_file:
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                rdr = csv.reader(f)
                rows = list(rdr)
            if rows:
                header = rows[0]
                ts_idx = header.index("ts") if "ts" in header else 0
                prof_idx = header.index("profile") if "profile" in header else 1
                slot_idx = header.index("slot") if "slot" in header else 2
                act_idx = header.index("action") if "action" in header else 3
                for r in rows[1:]:
                    if len(r) <= max(ts_idx, prof_idx, slot_idx, act_idx):
                        existing_rows.append(r)
                        continue
                    ts_v = (r[ts_idx] or "")[:10]
                    same_today = (ts_v == today)
                    same_key = (r[prof_idx] == profile
                                 and r[slot_idx] == slot
                                 and r[act_idx] == action)
                    if same_today and same_key:
                        continue
                    existing_rows.append(r)
        except Exception:
            existing_rows = []
            header = []
    if not header:
        header = ["ts", "profile", "slot", "action", "kw", "kwh",
                  "price_predicted_eur", "soc_before_pct", "soc_after_pct",
                  "soc_source", "profit_eur_rest_of_day"]

    new_row = [
        result.get("ts", ""),
        profile,
        slot,
        action,
        f"{cur.get('kw', 0):.2f}",
        f"{cur.get('kwh_per_slot', 0):.2f}",
        f"{cur.get('price_eur_mwh') or 0:.2f}",
        f"{soc.get('soc_pct', 0):.2f}",
        f"{cur.get('soc_after_pct', 0):.2f}",
        str(soc.get("source", "?"))[:60],
        f"{result.get('profit_eur', 0):.3f}",
    ]

    try:
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            for r in existing_rows:
                w.writerow(r)
            w.writerow(new_row)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[vdt_live_advisor.append_paper_trade] zlyhalo: {e}")
    # DB dual write
    _vdt_db_upsert(
        profile=profile, slot=slot, action=action,
        ts=result.get("ts", ""), kwh=float(cur.get("kwh_per_slot", 0) or 0),
        price=float(cur.get("price_eur_mwh", 0) or 0),
        soc_before=float(soc.get("soc_pct", 0) or 0),
        soc_after=float(cur.get("soc_after_pct", 0) or 0),
        delta_profit=float(result.get("profit_eur", 0) or 0),
        source=str(soc.get("source", "?"))[:60],
    )


# ── DB dual storage helper (Fáza 1.12 migrácie) ─────────────────────────────
def _vdt_use_db() -> bool:
    if os.environ.get("USE_DB", "0").strip() not in ("1", "true", "True", "yes"):
        return False
    try:
        from db import get_session   # noqa: F401
        return True
    except Exception:
        return False


def _vdt_db_upsert(profile: str, slot: str, action: str, ts: str,
                    kwh: float, price: float, soc_before: float = 0.0,
                    soc_after: float = 0.0, delta_profit: float = 0.0,
                    source: str = "advisor") -> bool:
    """UPSERT VdtPaperTrade do DB (profile_id, date, slot, action)."""
    if not _vdt_use_db():
        return False
    try:
        from db import get_session
        from db.models import Profile as _DbProfile, VdtPaperTrade as _DbVPT
        # CZ guard: VDT je iba SK trh
        try:
            import market as _mk
            mkt = str(_mk.active_market() or "cz")
        except Exception:
            mkt = "sk"
        if mkt != "sk":
            return False
        date = (ts or dt.date.today().isoformat())[:10]
        with get_session() as s:
            prof = s.query(_DbProfile).filter_by(name=str(profile or "")).one_or_none()
            if prof is None:
                return False
            existing = s.query(_DbVPT).filter_by(
                profile_id=prof.id, date=date,
                slot=str(slot or "")[:5], action=str(action or "").lower()
            ).one_or_none()
            if existing:
                existing.kwh = float(kwh)
                existing.price_eur_mwh = float(price)
                existing.soc_before_pct = float(soc_before)
                existing.soc_after_pct = float(soc_after)
                existing.delta_profit_eur = float(delta_profit)
                existing.timestamp = ts or dt.datetime.now().isoformat(timespec="seconds")
                existing.source = source
            else:
                s.add(_DbVPT(
                    profile_id=prof.id, market=mkt, date=date,
                    slot=str(slot or "")[:5], action=str(action or "").lower(),
                    kwh=float(kwh), price_eur_mwh=float(price),
                    soc_before_pct=float(soc_before), soc_after_pct=float(soc_after),
                    delta_profit_eur=float(delta_profit),
                    source=source,
                    timestamp=ts or dt.datetime.now().isoformat(timespec="seconds"),
                ))
        return True
    except Exception as e:
        print(f"[vdt_live_advisor._vdt_db_upsert] zlyhal: {e}")
        return False


def load_cache(profile: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Načíta posledný JSON cache pre konkrétny profil (alebo None ak neexistuje).

    Ak profile=None, fallback poradie:
      1. Skús resolve aktívny profil cez plan_store.resolve_profile() → per-profile cache
      2. Inak legacy shared cache
    """
    if profile is None:
        try:
            import plan_store as _ps
            profile = _ps.resolve_profile() or None
        except Exception:
            pass
    p = cache_path(profile)
    if not os.path.exists(p):
        # Fallback: legacy shared cache iba ak per-profile chýba
        if profile:
            p_legacy = cache_path(None)
            if os.path.exists(p_legacy):
                # Použiť legacy iba ak jeho profile pole sedí
                try:
                    with open(p_legacy) as f:
                        d = json.load(f)
                    if str(d.get("profile") or "") == profile:
                        return d
                except Exception:
                    pass
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception as e:
        print(f"[vdt_live_advisor.load_cache] zlyhalo: {e}")
        return None


def run_and_cache(**kwargs) -> Dict[str, Any]:
    """Helper — spustí get_live_recommendation, uloží do cache + paper trade log."""
    res = get_live_recommendation(**kwargs)
    if res.get("ok"):
        save_cache(res)
        append_paper_trade(res)
    return res


if __name__ == "__main__":
    # Smoke test — manuálny SOC 50% (lebo realio nemusí byť dostupné)
    r = get_live_recommendation(soc_start_pct=50.0, max_cycles_per_day=3.0)
    print(f"ok={r.get('ok')}")
    if r.get("ok"):
        c = r["current"]
        print(f"TERAZ: {c['slot']} {c['action']} {c['kw']:.0f} kW @ {c.get('price_eur_mwh','—')}")
        print(f"REASON: {c['reason']}")
        print(f"Profit za zvyšok dňa: {r['profit_eur']:+.2f} €")
        print(f"SOC po teraz: {c['soc_after_pct']:.0f} %")
        print(f"Preview ({len(r['preview'])} slotov):")
        for p in r["preview"][:8]:
            print(f"  {p['slot']} {p['action']:9} {p['kw']:6.0f} kW @ {p.get('price','—'):.0f if isinstance(p.get('price'),(int,float)) else ''} SOC={p['soc_after_pct']:.0f}%")
    else:
        print(f"FAIL: {r.get('error')}")
