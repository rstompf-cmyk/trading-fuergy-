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
    # Bug VDT-BREAKEVEN-AUTO (2026-06-11): prah spreadu z REÁLNYCH nákladov obchodu —
    # straty round-trip účinnosti (na cenovej hladine dňa) + 2×fee + cycle_cost.
    # Fixný min_spread ostáva ako minimum. Generické: všetko z parametrov profilu.
    if bool(_pl.get("vdt_breakeven_auto", False)):
        try:
            _eff_rt_be = float(eff_c) * float(eff_d)
            _p_ref_be = None
            try:
                import market as _mk_be
                _df_be = _mk_be.fetch_dam_prices(dt.date.today())
                if _df_be is not None and not _df_be.empty:
                    _p_ref_be = float(pd.to_numeric(_df_be["cena_EUR"],
                                                    errors="coerce").dropna().mean())
            except Exception:
                _p_ref_be = None
            if _p_ref_be and _p_ref_be > 0:
                _auto_be = (_p_ref_be * (1.0 / max(_eff_rt_be, 0.5) - 1.0)
                            + 2.0 * float(grid_fee) + float(cycle_cost))
                if _auto_be > min_spread:
                    print(f"[VDT-BREAKEVEN-AUTO] min_spread {min_spread:.1f} → "
                          f"{_auto_be:.1f} €/MWh (p_ref={_p_ref_be:.0f}, "
                          f"eff_rt={_eff_rt_be:.3f}, fee={grid_fee:g}, cc={cycle_cost:g})")
                    min_spread = _auto_be
        except Exception as _e_be:
            print(f"[VDT-BREAKEVEN-AUTO] zlyhal: {_e_be} → fix min_spread")
    # Bug VDT-SOC-RANGE (2026-06-11, user): VDT advisor pracuje s ROZSAHOM BATÉRIE
    # z profilu (plan.soc_min..plan.soc_max, štandardne 5-100) — žiadny separátny
    # operačný strop 95 ani koniec dňa 20. Predtým plán šiel 5-100 a advisor 20-85
    # → trvalé REJECTy/infeasible. Explicitné UI/API hodnoty majú stále prednosť.
    if soc_min_pct is None:
        soc_min_pct = float((_pl.get("soc_min") if _pl.get("soc_min") is not None
                             else _pl.get("soc_min_pct")) or 5.0)
    if soc_max_pct is None:
        soc_max_pct = float((_pl.get("soc_max") if _pl.get("soc_max") is not None
                             else _pl.get("soc_max_pct")) or 100.0)
    if soc_end_min_pct is None:
        soc_end_min_pct = soc_min_pct
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
        # SOC-SOURCE-LABEL (2026-06-30): ukáž SKUTOČNÝ zdroj SOC (engine meta / REALIZED /
        # carryover / projekcia) z compute_current_state, NIE napevno „kumulatívny". Predtým
        # to maskovalo, či obchodník berie realitu alebo projekciu (mýlilo diagnostiku).
        _real_src = state.get("current_soc_source") or "?"
        soc_source = {"ok": True, "soc_pct": soc_pct,
                      "source": (f"{_real_src} | vdt_state (start={state['start_soc_pct']:.1f}%, "
                                    f"VDT trades={state['vdt_realized_count']}, slot={state['current_slot_idx']})"),
                      "ts": state["now"], "age_minutes": 0.0, "error": "",
                      "start_soc_source": state["start_soc_source"]}
    else:
        soc_pct = float(soc_start_pct)
        soc_source = {"ok": True, "soc_pct": soc_pct, "source": "manual",
                      "ts": dt.datetime.now().isoformat(timespec="seconds"),
                      "error": ""}

    # 2. Snapshot dnes + zajtra. Cross-day obchodovanie je správne a žiaduce — gate NIE je
    # čas (16:00 nie je presný), ale REÁLNA DOSTUPNOSŤ order-book ponúk: slot sa obchoduje
    # IBA ak má reálny BID/ASK (rieši optimizer: slot bez order-booku → max_kwh=0). Žiadny
    # forecast/predbežná cena sa NESMIE použiť na uzavretie obchodu (#30, user 2026-06-18).
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
            # #30-A (user 2026-06-18: "vyhoď každý forecast na základe ktorého uzatváraš
            # obchod; len reálne BID/ASK"): zajtrajšie DAM záväzky použiť LEN ak je zajtrajšie
            # DAM REÁLNE publikované. Inak je zajtrajší D-1 plán forecast-based → VDT by proti
            # nemu cross-day pároval nákup = špekulácia (VW_sim_2). Vtedy vynuluj zajtrajšiu
            # časť záväzkov (gate = REÁLNOSŤ dát, nie čas). Dnešok ostáva (DAM reálne).
            try:
                _tom_iso = (today + dt.timedelta(days=1)).isoformat()
                import seps_sk as _ss_da
                _dam_tom_pub = bool(_ss_da.load_okte_dt_for_day(_tom_iso) or {})
                if not _dam_tom_pub and dam_commits is not None:
                    _snap_dates = [str(snapshot.iloc[_i].get("date", ""))[:10]
                                   for _i in range(len(snapshot))]
                    _n_zero = 0
                    for _i in range(min(len(dam_commits), len(_snap_dates))):
                        if _snap_dates[_i] == _tom_iso:
                            dam_commits[_i] = 0.0
                            if dam_commits_grid is not None and _i < len(dam_commits_grid):
                                dam_commits_grid[_i] = 0.0
                            _n_zero += 1
                    if _n_zero:
                        print(f"[vdt_live_advisor] #30-A: zajtra ({_tom_iso}) DAM nepublikované "
                              f"→ vynulovaných {_n_zero} forecast-based zajtrajších DAM záväzkov "
                              f"(žiadny cross-day forecast pár)")
            except Exception as _e_da:
                print(f"[vdt_live_advisor] #30-A gate zlyhal: {_e_da}")
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

    # VDT-RESIDUAL-SELLOFF wiring (2026-06-19, user: "nenechať batériu zbytočne nabitú; predaj
    # večer drahšie ako boli nabíjania"). Ak profil má flag vdt_residual_selloff, spočítame
    # nákladovú bázu rezidua = váž. priemer DAM nabíjacích cien dňa (fallback: priemer cien dňa,
    # ak žiadne DAM nabíjanie — reziduum z RT/štart SOC) a pošleme do optimizera. Ten potom smie
    # skončiť nižšie (predaj rezidua), ale LEN ak sell > cost_basis + fee + spread (žiadny dump).
    _residual_cb = None
    if bool(_pl.get("vdt_residual_selloff", False)):
        try:
            _prices_cb = [float(p) if p == p else 0.0
                          for p in pd.to_numeric(snapshot["price_eur"], errors="coerce").tolist()]
            _chg_kwh = 0.0; _chg_val = 0.0
            if dam_commits is not None and len(dam_commits) == len(_prices_cb):
                for _i_cb, _c_cb in enumerate(dam_commits):
                    _ck = max(-float(_c_cb or 0.0), 0.0)   # batt-view: − = nabíjanie
                    _chg_kwh += _ck; _chg_val += _ck * _prices_cb[_i_cb]
            if _chg_kwh > 1e-6:
                _residual_cb = _chg_val / _chg_kwh
            else:
                _valid_cb = [p for p in _prices_cb if p > 0]
                _residual_cb = (sum(_valid_cb) / len(_valid_cb)) if _valid_cb else None
            # GUARD (2026-06-19): cost_basis musí byť KLADNÁ reálna cena. cost_basis≤0
            # (žiadne dáta / prázdne ceny) = "predaj čokoľvek nad spread" = nebezpečne
            # agresívne → radšej NEZAPNÚŤ selloff (None = pôvodné soc_neutral správanie).
            if _residual_cb is None or not (_residual_cb > 0):   # None/NaN/≤0 → nezapni
                print(f"[VDT-RESIDUAL-SELLOFF] {active_profile}: cost_basis neplatná "
                      f"({_residual_cb}) → selloff VYPNUTÝ pre tento beh (bezpečne)")
                _residual_cb = None
            else:
                print(f"[VDT-RESIDUAL-SELLOFF] {active_profile}: cost_basis={_residual_cb:.1f} €/MWh "
                      f"(spread={min_spread:.0f}) → povolený ziskový výpredaj rezidua")
        except Exception as _e_cb:
            print(f"[VDT-RESIDUAL-SELLOFF] cost_basis zlyhal: {_e_cb}")
            _residual_cb = None

    # 4. optimize (engine z profilu: "lp" default / "pairs" párový matcher)
    # vdt_engine="pairs" → greedy párové cykly (nákup↔predaj páry so spreadom, žiadne
    # nepárové nákupy); vdt_pair_priority = closest/profit/balanced.
    _vdt_engine = str(_pl.get("vdt_engine", "lp") or "lp").lower()
    _vdt_pair_priority = str(_pl.get("vdt_pair_priority", "closest") or "closest").lower()
    # allow_buyback (user 2026-06-27): default FALSE — KAŽDÝ nákup musí mať NESKORŠÍ predaj.
    # Zakáže „predaj→spätný nákup" (koncové stratové nákupy bez neskoršieho predaja v rámci dňa).
    # Profil môže povoliť späť (vdt_allow_buyback:true) — relevantné až s cez-polnočným horizontom.
    _vdt_allow_buyback = bool(_pl.get("vdt_allow_buyback", False))
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
            residual_cost_basis_eur=_residual_cb,
            engine=_vdt_engine,
            pair_priority=_vdt_pair_priority,
            allow_buyback=_vdt_allow_buyback,
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
                # #28 (2026-06-18, user: "VDT a DT musia brať to saldo SPOLU; VDT predaj
                # nesmie ohroziť DAM záväzok"). PREDTÝM sa pri infeasible zahadzovali
                # dam_commitments (dam=None) → LP predal voľne SOC potrebnú pre DAM záväzok
                # → DAM under-delivery + drahé spätné nákupy (sell 374 / buy-back 463).
                # TERAZ: DAM je posvätný (už zazmluvnený) → ZACHOVÁME dam_commitments a
                # namiesto toho POTLAČÍME dobrovoľný VDT extra (obrovský min_spread →
                # objektív nedovolí žiadny voľný obchod, prejde len vynútený DAM lower-bound)
                # → full_plan = DAM baseline, ŽIADNY over-sell. Ak je LP aj tak infeasible
                # (DAM sa z aktuálnej SOC fyzicky nedá dodať), VDT extra = 0 a DAM odchýlka
                # ide cez ZCO (rieši livesim/RT), NIE cez VDT (netto DT+VDT na SOC).
                _opt2 = _opt.optimize_vdt_day(
                    snapshot=snapshot,
                    batt_kw=batt_kw, batt_kwh=batt_kwh,
                    eff_c=eff_c, eff_d=eff_d,
                    grid_fee=grid_fee, cycle_cost=cycle_cost, min_spread=1e6,
                    soc_min_pct=soc_min_pct, soc_max_pct=soc_max_pct,
                    soc_start_pct=soc_pct,
                    soc_end_min_pct=soc_end_min_pct,
                    max_cycles_per_day=max_cycles_per_day,
                    slot_minutes=15,
                    use_orderbook=use_orderbook,
                    future_only=True,
                    dam_commitments=dam_commits,   # #28: ZACHOVANÉ (DAM posvätný)
                )
                if _opt2.get("ok"):
                    result = _opt2
                    dam_status = ((dam_status + " · ⚠ VDT extra potlačený (infeasible, DAM zachovaný)")
                                   if dam_status else "⚠ VDT extra potlačený (DAM zachovaný)")
                    print(f"[vdt_live_advisor] #28: LP infeasible s extra — DAM zachovaný, "
                          f"VDT extra potlačený · {len(_opt2.get('trades',[]))} trades")
            except Exception as _e_r:
                print(f"[vdt_live_advisor] #28 retry zlyhal: {_e_r}")

    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "?"),
                "soc": soc_source, "orderbook_status": ob_status,
                "dam_status": dam_status, "zco": zco_info,
                "profile": active_profile}

    trades = result["trades"]

    # KROK 3 (2026-06-28): zjednotená feasibility brána je teraz DEFAULT (overené na DEV
    # VW_simulacia_3/4). Reťaz poistiek (clip_extras_to_grid + 2× clip_extras_to_capacity,
    # rôzne baseline = baseline-mismatch) je nahradená JEDNÝM volaním core.feasibility.gate_extras
    # (SOC ∧ grid ∧ výkon naraz, z JEDNÉHO reálneho SOC baseline).
    # KILL-SWITCH: VDT_FEASIBILITY_UNIFIED=0 → návrat k starej reťazi bez redeployu (1 release,
    # potom sa stará vetva zmaže). Default "1".
    _FEAS_UNIFIED = os.environ.get("VDT_FEASIBILITY_UNIFIED", "1").strip() in ("1", "true", "True", "yes")

    # Bug VDT-CAPACITY (2026-06-13, user: "pred uzavretím nákupu a predaja musí
    # prebehnúť simulácia SOC aj s rezervou; ak niekde prekročí, musí sa upraviť
    # a až potom uzavrieť"). LP optimalizátor plánuje future-only z aktuálnej SOC
    # a jeho interný pohľad sa rozchádza s PLNOU dennou trajektóriou DAM plánu →
    # kombinovaná SOC (DAM+VDT) prerážala kapacitu (overené VW_3 06-13: −12 %..+158 %).
    # Tu prebehne pre-commit forward simulácia kombinovanej SOC a každý VDT EXTRA
    # nad DAM sa oreže tak, aby SOC ostala v [soc_min+rezerva, soc_max−rezerva]
    # vo všetkých slotoch. Fail-open: pri chybe dát sa generovanie nezastaví.
    try:
        from core.vdt_capacity_guard import clip_extras_to_capacity as _clip_cap
        import d1_planner as _d1c
        _dam_net = _d1c.get_dam_commitments(today, profile=active_profile, basis="batt")  # 96, +vybíja −nabíja
        print(f"[VDT-CAPACITY/diag] {active_profile}: dam_net={'OK('+str(len(_dam_net))+')' if _dam_net else 'None'} "
              f"trades={len(trades) if trades else 0} soc_now={float(soc_pct):.1f}%")
        if _dam_net and len(_dam_net) == 96 and trades:
            _bk = float(batt_kwh)
            # Bug VDT-CAPACITY-BASELINE (2026-06-13, user: "obchody čo pri simulovanej
            # SOC nemôžu vyjsť, úplne bez zmeny"): predtým poistka štartovala z AKTUÁLNEJ
            # SOC + budúce sloty, ale graf (a teda fyzika) ide z DENNEJ trajektórie
            # plánu od soc_init s PLNÝM DAM. Tie dve sa rozchádzali → poistka nechytila
            # prebitie o 13h (DAM nabíja na 100 % + VDT dokúpi → 154 %). Teraz baseline
            # = soc_init + PLNÝ DAM (presne ako projekcia) → reprodukuje graf.
            try:
                import profiles as _pr_cap
                _pl_cap = (_pr_cap.load_profile(active_profile) or {}).get("plan") or {}
                _soc_init_pct = float(_pl_cap.get("soc_init_pct",
                                      _pl_cap.get("soc_init", soc_pct)) or soc_pct)
            except Exception:
                _soc_init_pct = float(soc_pct)
            _soc0 = _soc_init_pct / 100.0 * _bk            # ŠTART = soc_init plánu (deň)
            _dam_chg = [max(0.0, -float(x)) for x in _dam_net]   # PLNÝ deň, bez zeroingu
            _dam_dis = [max(0.0, float(x)) for x in _dam_net]
            # VDT extra (nad DAM) per absolútny 15-min slot
            _ex = {}
            for _tr in trades:
                _sl = pd.Timestamp(_tr["start_local"])
                _si = int((_sl.hour * 60 + _sl.minute) // 15)
                _net_tr = float(_tr.get("discharge_kwh", 0.0)) - float(_tr.get("charge_kwh", 0.0))
                _extra = _net_tr - float(_dam_net[_si])
                if _extra > 0.5:
                    _ex[_si] = ("SELL", _extra)
                elif _extra < -0.5:
                    _ex[_si] = ("BUY", -_extra)
            _grep = []; _rep2 = []
            if _FEAS_UNIFIED:
                # KROK 3: JEDNA brána (SOC ∧ grid ∧ výkon) z JEDNÉHO reálneho SOC baseline
                # od aktuálneho slotu. Nahrádza clip_extras_to_grid + 2× clip_extras_to_capacity.
                try:
                    from core.feasibility import gate_extras as _gx
                    _now_si = max(0, min(95, int((pd.Timestamp.now().hour * 60
                                                  + pd.Timestamp.now().minute) // 15)))
                    _damkw = [float(_dam_net[i]) / 0.25 for i in range(96)]
                    _nb = None
                    try:
                        _dam_grid = _d1c.get_dam_commitments(today, profile=active_profile, basis="grid")
                        if _dam_grid and len(_dam_grid) == 96:
                            _nb = [(float(_dam_grid[i]) - float(_dam_net[i])) / 0.25 for i in range(96)]
                    except Exception:
                        _nb = None
                    _clipped, _rep = _gx(
                        _damkw, _ex, soc_start_kwh=float(soc_pct) / 100.0 * _bk, batt_kwh=_bk,
                        soc_min_frac=soc_min_pct / 100.0, soc_max_frac=soc_max_pct / 100.0,
                        eff_c=eff_c, eff_d=eff_d, dt_h=0.25, start_slot=_now_si,
                        grid_export_kw=float(_pl.get("grid_kw_export") or _pl.get("grid_kw") or batt_kw),
                        grid_import_kw=float(_pl.get("grid_kw_import") or _pl.get("grid_kw") or batt_kw),
                        net_base_kw=_nb,
                        reserve_frac=float(_pl.get("soc_reserve_pct") or 0.0) / 100.0)
                    if _rep:
                        print(f"[VDT-FEASIBILITY-UNIFIED] {active_profile}: gate_extras orezal "
                              f"{len(_rep)} slotov (SOC∧grid∧vykon, 1 baseline): {_rep[:4]}")
                except Exception as _e_uni:
                    print(f"[VDT-FEASIBILITY-UNIFIED] zlyhalo ({_e_uni}) → bez clipu (matcher je SOC-aware)")
                    _clipped, _rep = dict(_ex), []
            else:
                # Bug VDT-PENALTY (Koreň 2, 2026-06-15): GRID feasibility clip — nominácia nesmie
                # presiahnuť prípojku, inak engine (GRID-LIMIT-REALITY) reálnu dodávku oreže →
                # nominované > dodané → pokuta cez ZCO. DAM grid pozícia (ex−im, + export) už
                # zahŕňa FTV/load; VDT extra ju len posúva o batt delta.
                try:
                    from core.vdt_capacity_guard import clip_extras_to_grid as _clip_grid
                    _dam_grid = _d1c.get_dam_commitments(today, profile=active_profile, basis="grid")
                    if _dam_grid and len(_dam_grid) == 96:
                        _gimp = float(_pl.get("grid_kw_import") or _pl.get("grid_kw") or batt_kw) * 0.25
                        _gexp = float(_pl.get("grid_kw_export") or _pl.get("grid_kw") or batt_kw) * 0.25
                        _ex, _grep = _clip_grid(_ex, _dam_grid, _gimp, _gexp)
                        if _grep:
                            print(f"[VDT-PENALTY/grid] {active_profile}: orezanych {len(_grep)} slotov "
                                  f"na grid limit (imp {_gimp/0.25:.0f}/exp {_gexp/0.25:.0f} kW): {_grep[:4]}")
                except Exception as _e_grid:
                    print(f"[VDT-PENALTY/grid] poistka preskocena ({_e_grid})")
                _clipped, _rep = _clip_cap(_soc0, _dam_chg, _dam_dis, _ex, _bk,
                                           eff_c, eff_d, soc_min_pct, soc_max_pct,
                                           reserve_pct=float(_pl.get("soc_reserve_pct") or 0.0))
                # Bug VDT-PENALTY (SOC-REAL, 2026-06-15): druhý clip z REÁLNEHO aktuálneho SOC cez
                # BUDÚCE sloty. Prvý clip ráta z idealizovaného soc_init+DAM (od 00:00, kvôli zhode
                # s grafom); ak reálny SOC drifol (RT, realizované VDT), budúce nominácie boli
                # SOC-nepokryteľné → "obchod nepokrytý SOC" → pokuta (VW_simulacia_3: grid=batt, čiže
                # úzke hrdlo je SOC nie grid). Forward od reálneho SOC orež extras → vždy dodateľné.
                try:
                    _now_si = max(0, min(95, int((pd.Timestamp.now().hour * 60
                                                  + pd.Timestamp.now().minute) // 15)))
                    _soc_real0 = float(soc_pct) / 100.0 * _bk
                    _fut_ex = {t - _now_si: v for t, v in _clipped.items() if t >= _now_si}
                    _clipped_fut, _rep2 = _clip_cap(_soc_real0, _dam_chg[_now_si:], _dam_dis[_now_si:],
                                                    _fut_ex, _bk, eff_c, eff_d,
                                                    soc_min_pct, soc_max_pct,
                                                    reserve_pct=float(_pl.get("soc_reserve_pct") or 0.0))
                    if _rep2:
                        for _t2 in list(_clipped.keys()):
                            if _t2 >= _now_si:
                                _nv = _clipped_fut.get(_t2 - _now_si)
                                if _nv is None:
                                    _clipped.pop(_t2, None)
                                else:
                                    _clipped[_t2] = _nv
                        print(f"[VDT-PENALTY/soc-real] {active_profile}: orezanych {len(_rep2)} buducich "
                              f"slotov z realneho SOC {float(soc_pct):.1f}%: {_rep2[:4]}")
                except Exception as _e_socr:
                    print(f"[VDT-PENALTY/soc-real] poistka preskocena ({_e_socr})")
            if _rep or _grep or _rep2:
                # zapíš orezané extras späť do trades (trade = DAM + orezaný extra)
                for _tr in trades:
                    _sl = pd.Timestamp(_tr["start_local"])
                    _si = int((_sl.hour * 60 + _sl.minute) // 15)
                    _new_extra = _clipped.get(_si)
                    _ev = (_new_extra[1] if _new_extra and _new_extra[0] == "SELL"
                           else (-_new_extra[1] if _new_extra else 0.0))
                    _net_new = float(_dam_net[_si]) + _ev
                    _tr["discharge_kwh"] = max(0.0, _net_new)
                    _tr["charge_kwh"] = max(0.0, -_net_new)
                    if _tr["charge_kwh"] > 0.01:
                        _tr["action"] = "charge"
                    elif _tr["discharge_kwh"] > 0.01:
                        _tr["action"] = "discharge"
                    else:
                        _tr["action"] = "idle"
                print(f"[VDT-CAPACITY] {active_profile}: orezaných {len(_rep)} slotov "
                      f"(SOC by inak prekročila kapacitu): {_rep[:4]}")
    except Exception as _e_cap:
        print(f"[VDT-CAPACITY] poistka preskočená ({_e_cap})")

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

    # ── TVRDÝ VÝKON+KAPACITA GUARD na UZATVÁRANÝ obchod (2026-06-13, user: "obchody
    # ktoré sa nedajú výkonovo ALEBO kapacitne pokryť sa NESMÚ uzavrieť — na to je tam tá
    # ochrana"). Nezávislé od upstream clip_extras_to_capacity (ten ráta z idealizovaného
    # soc_init+DAM baseline a výkon vôbec netestuje). Tu z REÁLNEHO aktuálneho SOC: ──
    try:
        _bk_g = float(batt_kwh)
        # SOC tolerancia/rezerva od max aj min (user: "máme parameter čo dáva toleranciu
        # od maxima a minima") — tá istá ako RT audit (soc_reserve_pct). Obchod nesmie SOC
        # pretlačiť do rezervného pásma → držíme [soc_min+rez, soc_max−rez].
        _soc_reserve = float(_pl.get("soc_reserve_pct") or 0.0)
        _soc_now_kwh = float(soc_pct) / 100.0 * _bk_g
        _soc_lo_kwh = (float(soc_min_pct) + _soc_reserve) / 100.0 * _bk_g
        _soc_hi_kwh = (float(soc_max_pct) - _soc_reserve) / 100.0 * _bk_g
        _g_reason = None
        # 1) VÝKON: kW obchodu nesmie prekročiť výkon batérie
        if cur_kw > float(batt_kw) + 1e-6:
            _g_reason = f"výkon {cur_kw:.0f} > batt {float(batt_kw):.0f} kW"
            cur_kwh = float(batt_kw) * 0.25
        # 2) KAPACITA: po obchode musí SOC ostať v [soc_min, soc_max] (reálny SOC teraz)
        if cur_action == "charge":
            _soc_after_g = _soc_now_kwh + cur_kwh * float(eff_c)
            if _soc_after_g > _soc_hi_kwh + 1e-6:
                _allow = max(0.0, (_soc_hi_kwh - _soc_now_kwh) / max(float(eff_c), 0.01))
                _g_reason = (f"nabitie by prekročilo {float(soc_max_pct):.0f}% "
                             f"({cur_kwh:.0f}→{_allow:.0f} kWh)")
                cur_kwh = _allow
        elif cur_action == "discharge":
            _soc_after_g = _soc_now_kwh - cur_kwh / max(float(eff_d), 0.01)
            if _soc_after_g < _soc_lo_kwh - 1e-6:
                _allow = max(0.0, (_soc_now_kwh - _soc_lo_kwh) * float(eff_d))
                _g_reason = (f"vybitie by kleslo pod {float(soc_min_pct):.0f}% "
                             f"({cur_kwh:.0f}→{_allow:.0f} kWh)")
                cur_kwh = _allow
        if _g_reason:
            cur_kw = cur_kwh / 0.25 if cur_kwh > 0 else 0.0
            if cur_kwh <= 1e-6:           # neostalo nič pokryteľné → obchod sa NEuzavrie
                cur_action = "idle"; cur_kw = 0.0; cur_kwh = 0.0
                cur["action"] = "idle"
            cur["charge_kwh"] = cur_kwh if cur_action == "charge" else 0.0
            cur["discharge_kwh"] = cur_kwh if cur_action == "discharge" else 0.0
            print(f"[VDT-HARD-GUARD] {active_profile} slot {cur.get('slot')}: {_g_reason} "
                  f"→ {cur_action} {cur_kw:.0f} kW (SOC teraz {float(soc_pct):.1f}%)")
    except Exception as _e_hg:
        print(f"[VDT-HARD-GUARD] preskočené: {_e_hg}")

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

    # ── Bug VDT-HORIZON-AGG (2026-06-16, user VW_simulacia_3) ──────────────
    # LP horizont je dnes+zajtra (snapshot načítava 2 dni kvôli look-ahead na
    # ceny). Preto result-agregáty (dam_committed_export, total_charged/discharged)
    # sumovali OBA dni → pri 6 MWh batérii to vyzeralo ako commit 10830 kWh / ±16830
    # ("obchod uzavretý, nedá sa dodať"). Pre kontrolu deliverability a zobrazenie
    # KLIPUJEME na DNEŠOK (sloty s dátumom == dnes). dam_commits basis=batt: +vybíja −nabíja.
    _today_d = dt.date.today()
    _dc_arr = dam_commits or []
    _td = {"dam_dis": 0.0, "dam_chg": 0.0, "tot_dis": 0.0, "tot_chg": 0.0}
    for _i, _tr in enumerate(result.get("trades", [])):
        try:
            _d = pd.Timestamp(_tr["start_local"]).date()
        except Exception:
            _d = _today_d
        if _d != _today_d:
            continue
        _td["tot_dis"] += float(_tr.get("discharge_kwh", 0.0))
        _td["tot_chg"] += float(_tr.get("charge_kwh", 0.0))
        if _i < len(_dc_arr):
            _v = float(_dc_arr[_i])
            if _v > 0:
                _td["dam_dis"] += _v
            elif _v < 0:
                _td["dam_chg"] += -_v
    _summary_today = dict(result.get("summary", {}))
    _summary_today["total_charged_kwh"] = round(_td["tot_chg"], 1)
    _summary_today["total_discharged_kwh"] = round(_td["tot_dis"], 1)
    if batt_kwh > 0:
        _summary_today["cycles"] = round(_td["tot_dis"] / batt_kwh, 3)
    _summary_today["horizon_total_discharged_kwh"] = round(
        float(result.get("summary", {}).get("total_discharged_kwh", 0.0)), 1)  # diag: 2-dňový horizont

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
        "summary": _summary_today,                    # DNEŠOK-only (viď VDT-HORIZON-AGG)
        "profit_eur": result["profit_eur"],
        "n_slots": result["n_slots"],
        "orderbook_status": ob_status,
        "orderbook_per_slot": orderbook_per_slot,
        "dam_status": dam_status,
        "dam_commits": dam_commits or [],            # BATT účasť (LP lower bound)
        "dam_commits_grid": dam_commits_grid or [],  # FULL grid nominácia (diag)
        "zco": zco_info,                              # ZCO príležitosti (Fáza C1)
        "dam_committed_export_kwh": round(_td["dam_dis"], 1),   # DNEŠOK-only
        "dam_committed_import_kwh": round(_td["dam_chg"], 1),   # DNEŠOK-only
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
                                profit_eur: float = 0.0,
                                dam_clearing_eur_mwh: float = 0.0) -> None:
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
            _plan_au = (_prof_obj.get("plan") or {})
            _batt_kw_max = float(_plan_au.get("batt_kw", 0.0) or 0.0)
            # Bug GRID-AUDIT (2026-06-10): clip aj na grid kapacitu
            _gki_au = _plan_au.get("grid_kw_import")
            _gke_au = _plan_au.get("grid_kw_export")
            _gki_au = float(_gki_au) if _gki_au is not None else None
            _gke_au = float(_gke_au) if _gke_au is not None else None
            if _batt_kw_max > 0 and slot and ":" in str(slot):
                _hh = int(str(slot)[:2]); _mm = int(str(slot)[3:5])
                _slot_idx2 = slot_idx_from_time(_hh, _mm)
                _direction = "discharge" if _act_upper in ("DISCHARGE", "SELL") else "charge"
                _trade_id = f"{profile}_{today}_{slot}_{_act_upper}_extras"
                _ax = audit_vdt_order(profile, today, _slot_idx2, _direction,
                                        abs(float(kwh or 0)), _batt_kw_max, _trade_id,
                                        grid_kw_import=_gki_au, grid_kw_export=_gke_au)
                if _ax["decision"] == "reject":
                    print(f"[append_extra_paper_trade #612] REJECT {profile} slot={slot} "
                          f"{_act_upper}: {_ax['note']}")
                    return   # nezapisujeme
                if _ax["decision"] == "downscale":
                    _sign = -1.0 if kwh < 0 else 1.0
                    kwh = _ax["allowed_kwh"] * _sign
                    print(f"[append_extra_paper_trade #612] DOWNSCALE {profile} slot={slot} "
                          f"{_act_upper}: {_ax['note']}")
                # Bug #637 (2026-06-09): SOC-use audit pre VDT extras (BUY/SELL nad DAM)
                if abs(kwh) > 0:
                    try:
                        from core.soc_use_audit import audit_action as _soc_audit
                        _sa = _soc_audit(profile, today, _slot_idx2, _direction,
                                          abs(kwh), source="vdt_extra")
                        if _sa["decision"] == "reject":
                            print(f"[append_extra_paper_trade #637] REJECT {profile} slot={slot} "
                                  f"{_act_upper}: {_sa['reason']}")
                            return
                        if _sa["decision"] == "downscale":
                            _sign = -1.0 if kwh < 0 else 1.0
                            kwh = _sa["allowed_kwh"] * _sign
                            print(f"[append_extra_paper_trade #637] DOWNSCALE {profile} slot={slot} "
                                  f"{_act_upper}: {_sa['reason']}")
                    except Exception as _e_soc:
                        print(f"[append_extra_paper_trade #637] SOC audit zlyhal: {_e_soc} → pokračujem (fail-open)")
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
                _BATT_ACTS = ("BUY", "SELL", "CHARGE", "DISCHARGE")
                _new_is_batt = str(action or "").upper() in _BATT_ACTS
                for r in rows[1:]:
                    if len(r) <= max(ts_idx, prof_idx, slot_idx, act_idx):
                        existing_rows.append(r)   # nedostatočne dlhý riadok — ponechaj
                        continue
                    ts_v = (r[ts_idx] or "")[:10]   # YYYY-MM-DD
                    same_today = (ts_v == today)
                    same_key = (r[prof_idx] == str(profile or "")
                                 and r[slot_idx] == str(slot or "")
                                 and r[act_idx] == str(action or ""))
                    # VDT-NO-CHURN (2026-06-16): pri batériovom obchode nahraď AKÝKOĽVEK
                    # batériový obchod pre ten istý slot (charge↔discharge flip cez ticky)
                    # → slot má vždy 1 VDT obchod (idle/curtail riadky ostávajú).
                    _same_slot_batt = (_new_is_batt
                                       and r[prof_idx] == str(profile or "")
                                       and r[slot_idx] == str(slot or "")
                                       and str(r[act_idx] or "").upper() in _BATT_ACTS)
                    if same_today and (same_key or _same_slot_batt):
                        continue   # duplikát / starý obchod slotu — vyhoď
                    existing_rows.append(r)
        except Exception:
            existing_rows = []
            header = []
    if not header:
        header = ["ts", "profile", "slot", "action", "kw", "kwh",
                  "price_predicted_eur", "soc_before_pct", "soc_after_pct",
                  "soc_source", "profit_eur_rest_of_day", "dam_clearing_eur_mwh"]
    # VDT-DAM-COL (2026-06-15): doplň stĺpec dam_clearing_eur_mwh do starej hlavičky
    elif "dam_clearing_eur_mwh" not in header:
        header = list(header) + ["dam_clearing_eur_mwh"]

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
        f"{dam_clearing_eur_mwh:.2f}",
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
        dam_clearing=float(dam_clearing_eur_mwh),
    )


def _vdt_price_is_real(px) -> bool:
    """VDT cena je PLATNÁ len ak je reálna: None/0.0/NaN/inf = neplatná (placeholder,
    chýbajúci orderbook alebo DAM commitment bez VDT ceny). Záporná cena je PLATNÁ
    (bežná prax). Pravidlo project-vdt-trading-rules #1 (VDT-ZERO-PRICE)."""
    import math
    try:
        f = float(px)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f != 0.0


def append_paper_trade(result: Dict[str, Any], bypass_audit: bool = False) -> None:
    """Append jednu odporúčanú akciu do paper trading log CSV.

    bypass_audit=True → preskočí #612/#637/DELIVERABLE audit (ručný obchod = override
    rozhodnutý užívateľom). Používa place_manual_trade.

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
    if (not bypass_audit) and _action_upper in ("CHARGE", "DISCHARGE", "BUY", "SELL", "BOTH"):
        # VDT-ZERO-PRICE (writer enforcement, 2026-06-18): VDT obchod sa zapíše LEN s
        # reálnou cenou. None/0.0/NaN/inf = placeholder (chýbajúci orderbook alebo DAM
        # commitment BEZ VDT ceny) → NEZAPÍSAŤ. Inak sa napr. DAM nabíjanie zaloguje ako
        # VDT buy @ 0 → engine ho aplikuje navyše k DAM plánu = DOUBLE-COUNT SOC
        # (porušenie SOC, VW_simulacia_3 13:30). Záporná cena je PLATNÁ. (Pravidlo #1.)
        if not _vdt_price_is_real(cur.get("price_eur_mwh")):
            print(f"[append_paper_trade VDT-ZERO-PRICE] SKIP {profile} slot={slot} "
                  f"{_action_upper}: neplatná cena ({cur.get('price_eur_mwh')}) — VDT len s reálnou cenou")
            return
        try:
            from core.capacity_ledger import audit_vdt_order, slot_idx_from_time
            import profiles as _ps_audit
            _prof_obj = _ps_audit.load_profile(profile) or {}
            _plan_au2 = (_prof_obj.get("plan") or {})
            _batt_kw_max = float(_plan_au2.get("batt_kw", 0.0) or 0.0)
            # Bug GRID-AUDIT (2026-06-10): clip aj na grid kapacitu
            _gki_au2 = _plan_au2.get("grid_kw_import")
            _gke_au2 = _plan_au2.get("grid_kw_export")
            _gki_au2 = float(_gki_au2) if _gki_au2 is not None else None
            _gke_au2 = float(_gke_au2) if _gke_au2 is not None else None
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
                                            abs(_kwh_req), _batt_kw_max, _trade_id,
                                            grid_kw_import=_gki_au2, grid_kw_export=_gke_au2)
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
                        _kwh_req = abs(float(cur.get("kwh_per_slot", 0) or 0))   # update pre #637 audit
                        print(f"[append_paper_trade #612] DOWNSCALE {profile} slot={slot} "
                              f"{_action_upper}: {_ax['note']}")
                    # decision == "accept" → pokračuj bez zmeny
                # Bug #637 (2026-06-09): SOC-use audit — pred zápisom VDT trade-u
                # simuluje 24h SOC trajektóriu vrátane navrhovaného trade-u + všetkých
                # už zazmluvnených commitmentov (D-1 plán + VDT realized). Ak by trade
                # spôsobil SOC violation v ktoromkoľvek budúcom slote → downscale alebo reject.
                if _action_upper in ("CHARGE", "DISCHARGE", "BUY", "SELL") and abs(_kwh_req) > 0:
                    try:
                        from core.soc_use_audit import audit_action as _soc_audit
                        # VDT-AUDIT-REAL-SOC (#28, 2026-07-01): audit MUSÍ rezervovať SOC pre
                        # DAM záväzok voči REÁLNEMU SOC (nie plánu od 00:00). Bez toho VDT
                        # otvárací predaj zožral SOC, ktorý realita nedodá → večerná odchýlka.
                        # Podáme reálny current_soc + aktuálny slot (seed simulácie).
                        # Kill-switch VDT_AUDIT_REAL_SOC=0 → späť na seed z plánu (00:00).
                        _ts_rs = None; _cur_soc_rs = None; _cur_slot_rs = None
                        if os.environ.get("VDT_AUDIT_REAL_SOC", "1") != "0":
                            try:
                                import vdt_state as _vs_rs
                                _ts_rs = _vs_rs.compute_current_state(
                                    profile, today=dt.date.fromisoformat(_today))
                                if _ts_rs and _ts_rs.get("ok"):
                                    _cur_soc_rs = float(_ts_rs.get("current_soc_pct"))
                                    _cur_slot_rs = int(_ts_rs.get("current_slot_idx") or 0)
                            except Exception as _e_rs:
                                print(f"[append_paper_trade VDT-AUDIT-REAL-SOC] {profile}: {_e_rs} → seed z plánu")
                                _ts_rs = None; _cur_soc_rs = None; _cur_slot_rs = None
                        _sa = _soc_audit(profile, _today, _slot_idx, _direction,
                                          abs(_kwh_req), source="vdt",
                                          today_state=_ts_rs,
                                          current_soc_pct_at_si=_cur_soc_rs,
                                          sim_from_slot=_cur_slot_rs)
                        if _sa["decision"] == "reject":
                            print(f"[append_paper_trade #637] REJECT {profile} slot={slot} "
                                  f"{_action_upper}: {_sa['reason']}")
                            return
                        if _sa["decision"] == "downscale":
                            _allowed = _sa["allowed_kwh"]
                            _sign = -1.0 if (cur.get("kwh_per_slot", 0) or 0) < 0 else 1.0
                            cur["kwh_per_slot"] = _allowed * _sign
                            cur["kw"] = _allowed * 4.0 * _sign
                            print(f"[append_paper_trade #637] DOWNSCALE {profile} slot={slot} "
                                  f"{_action_upper}: {_sa['reason']}")
                    except Exception as _e_soc:
                        # Fail-OPEN pre #637 — capacity audit (Bug #612) už prešiel,
                        # nepríjemné je nezapísať trade keby SOC audit padol z dôvodu
                        # nedostupných dát (napr. compute_current_state zlyhalo).
                        print(f"[append_paper_trade #637] SOC audit zlyhal pre {profile}/{slot}: {_e_soc} → pokračujem (fail-open)")
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
                    source: str = "advisor", dam_clearing: float = 0.0) -> bool:
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
            _act_l = str(action or "").lower()
            _BATT_DB = ("buy", "sell", "charge", "discharge")
            # VDT-NO-CHURN (2026-06-16): pri batériovom obchode zmaž iný batériový obchod
            # pre ten istý slot (charge↔discharge flip cez ticky) → slot má 1 VDT obchod.
            if _act_l in _BATT_DB:
                for _opp in s.query(_DbVPT).filter(
                        _DbVPT.profile_id == prof.id, _DbVPT.date == date,
                        _DbVPT.slot == str(slot or "")[:5],
                        _DbVPT.action != _act_l,
                        _DbVPT.action.in_(_BATT_DB)).all():
                    s.delete(_opp)
            existing = s.query(_DbVPT).filter_by(
                profile_id=prof.id, date=date,
                slot=str(slot or "")[:5], action=_act_l
            ).one_or_none()
            if existing:
                existing.kwh = float(kwh)
                existing.price_eur_mwh = float(price)
                existing.dam_clearing_eur_mwh = float(dam_clearing)
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
                    dam_clearing_eur_mwh=float(dam_clearing),
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


def _cleanup_alert_dir() -> str:
    """Zdieľaný adresár pre cleanup alerty (global, naprieč profilmi)."""
    d = os.path.join("out", "_status")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _cleanup_alert_path(profile: str) -> str:
    _safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(profile or ""))
    return os.path.join(_cleanup_alert_dir(), f"cleanup_alert_{_safe}.json")


def _write_cleanup_alert(profile: str, payload: Dict[str, Any]) -> None:
    """Zapíš/prepíš alert pre profil (nedodateľný slot ≤ alert_h, neupratané kvôli max strate)."""
    try:
        import json as _json
        payload = dict(payload or {})
        payload["profile"] = profile
        payload["created"] = dt.datetime.now().isoformat(timespec="seconds")
        with open(_cleanup_alert_path(profile), "w", encoding="utf-8") as f:
            _json.dump(payload, f, ensure_ascii=False)
    except Exception as e:
        print(f"[VDT-CLEANUP-ALERT] zápis zlyhal ({profile}): {e}")


def clear_cleanup_alert(profile: str) -> None:
    """Zmaž alert (problém vyriešený / upratané / už neexistuje)."""
    try:
        p = _cleanup_alert_path(profile)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def read_cleanup_alerts(max_age_min: int = 20) -> list:
    """Vráti VŠETKY aktívne (čerstvé) cleanup alerty naprieč profilmi — pre globálny banner.

    Alert je „čerstvý", ak nie je starší než max_age_min (worker ho každý tick prepíše/zmaže;
    zastaraný súbor = profil sa už nespracúva → nezobrazuj)."""
    import glob as _glob, json as _json
    out = []
    for p in _glob.glob(os.path.join(_cleanup_alert_dir(), "cleanup_alert_*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                a = _json.load(f)
            _c = a.get("created")
            if _c:
                age = (dt.datetime.now() - dt.datetime.fromisoformat(_c)).total_seconds() / 60.0
                if age > max_age_min:
                    continue
            out.append(a)
        except Exception:
            continue
    out.sort(key=lambda x: x.get("tau_h", 99))
    return out


def propose_cleanup(result: Dict[str, Any]) -> Dict[str, Any]:
    """VDT „Upratovanie" (bezpečnostná sieť, task #82). Kill-switch VDT_CLEANUP=1 (DEFAULT OFF).

    Zistí PRVÝ budúci nedodateľný slot (committed nominácia DAM+VDT simulovaná z REÁLNEHO
    SOC vyjde mimo [min,max]) a ak sa oplatí (decide_cleanup s časovou decay), commitne
    korekčný obchod s tagom `vdt_cleanup` (reason). Bezpečné: len zmenšuje odchýlku, cap na
    voľný výkon, ref cena = orderbook@problém, action = orderbook@teraz. FAIL-SAFE: hocijaká
    chyba → nič sa nestane (nikdy nezhodí normálny VDT flow). RT sa NEDOTÝKA.
    """
    out = {"acted": False, "reason": ""}
    try:
        if os.environ.get("VDT_CLEANUP", "0") != "1":
            return out
        if not result or not result.get("ok") or not _is_sk_market():
            return out
        profile = str(result.get("profile") or "")
        if not profile:
            return out
        import vdt_state as _vs
        import core.soc_use_audit as _sua
        import profiles as _pr
        from core.vdt_cleanup import detect_undeliverable_target, decide_cleanup
        _today = (str(result.get("ts") or "")[:10]) or dt.date.today().isoformat()
        st = _vs.compute_current_state(profile, today=dt.date.fromisoformat(_today))
        if not st or not st.get("ok"):
            return out
        cap = float(st.get("batt_kwh") or 0.0)
        if cap <= 0:
            return out
        eff_c = float(st.get("eff_c") or 0.95); eff_d = float(st.get("eff_d") or 0.95)
        soc_min = float(st.get("soc_min_pct") or 5.0); soc_max = float(st.get("soc_max_pct") or 100.0)
        cur_soc = float(st.get("current_soc_pct") or 50.0)
        cur_slot = int(st.get("current_slot_idx") or 0)
        dam = list(st.get("dam_nomination_kwh") or [0.0] * 96)
        vdt = list(st.get("vdt_realized_kwh") or [0.0] * 96)
        while len(dam) < 96: dam.append(0.0)
        while len(vdt) < 96: vdt.append(0.0)
        sched = [float(dam[i]) + float(vdt[i]) for i in range(96)]
        sim = [0.0] * cur_slot + sched[cur_slot:]
        path = _sua.simulate_soc_unclipped(cur_soc, sim, cap, eff_c=eff_c, eff_d=eff_d)
        _pl = (_pr.load_profile(profile) or {}).get("plan") or {}
        batt_kw = float(_pl.get("batt_kw", 0.0) or 0.0)
        tgt = detect_undeliverable_target(path, cur_slot, soc_min_pct=soc_min,
                                          soc_max_pct=soc_max, batt_kwh=cap, batt_kw=batt_kw)
        if not tgt:
            clear_cleanup_alert(profile)      # problém zmizol → zruš prípadný alert
            out["reason"] = "žiadny nedodateľný slot (OK)"
            return out
        obps = result.get("orderbook_per_slot") or []

        def _price(slot_i, side):
            try:
                d = obps[int(slot_i)] or {}
                return float(d.get("bid") if side == "sell" else d.get("ask"))
            except Exception:
                return None
        direction = tgt["direction"]
        action_price = _price(cur_slot, direction)
        ref_price = _price(tgt["problem_slot"], direction)     # cena obchodu v čase problému
        horizon_h = float(_pl.get("cleanup_horizon_h", 6.0) or 6.0)
        deadband_kw = float(_pl.get("cleanup_deadband_kw", 50.0) or 50.0)
        max_loss = float(_pl.get("cleanup_max_loss_eur_mwh", 20.0) or 20.0)
        alert_h = float(_pl.get("cleanup_alert_h", 1.0) or 1.0)
        min_spread = float(_pl.get("min_spread", _pl.get("min_spread_eur", 5.0)) or 5.0)
        max_action_kw = max(0.0, batt_kw - abs(float(sched[cur_slot]))) if batt_kw > 0 else 0.0
        dec = decide_cleanup(tgt["deviation_kw"], tgt["tau_h"], direction=direction,
                             action_price_eur=action_price, ref_price_eur=ref_price,
                             horizon_h=horizon_h, min_spread_eur=min_spread,
                             max_loss_eur=max_loss, deadband_kw=deadband_kw,
                             max_action_kw=max_action_kw)
        out["target"] = tgt; out["decision"] = dec
        # Problémový slot ako HH:MM-HH:MM (pre alert aj log)
        _ps = int(tgt["problem_slot"])
        _ph0, _pm0 = divmod(_ps * 15, 60); _ph1, _pm1 = divmod(_ps * 15 + 15, 60)
        _prob_slot_str = f"{_ph0:02d}:{_pm0:02d}-{_ph1:02d}:{_pm1:02d}"
        if not dec.get("act"):
            out["reason"] = dec.get("reason", "")
            # ALERT: ≤ alert_h do problému + odmietnuté kvôli marži (strata > max) + je cena.
            _mrg = dec.get("margin"); _req = dec.get("req_margin")
            _loss_decline = (_mrg is not None and _req is not None and float(_mrg) < float(_req))
            if (_loss_decline and float(tgt["tau_h"]) <= alert_h
                    and action_price is not None):
                _kw_al = min(abs(float(tgt["deviation_kw"])), abs(float(max_action_kw)))
                _act_al = "discharge" if direction == "sell" else "charge"
                _would_loss = round(float(_mrg) * (_kw_al * 0.25) / 1000.0, 2)  # €/MWh × MWh
                _write_cleanup_alert(profile, {
                    "problem_slot": _prob_slot_str, "tau_h": round(float(tgt["tau_h"]), 2),
                    "direction": direction, "action": _act_al, "kw": round(_kw_al, 1),
                    "best_price_eur_mwh": (round(float(action_price), 2)
                                           if action_price is not None else None),
                    "ref_price_eur_mwh": (round(float(ref_price), 2)
                                          if ref_price is not None else None),
                    "margin_eur_mwh": _mrg, "req_margin_eur_mwh": _req,
                    "would_loss_eur": _would_loss, "kind": tgt.get("kind", ""),
                    "ts": result.get("ts", ""),
                })
                out["alert"] = True
            else:
                clear_cleanup_alert(profile)   # ešte je čas / nie strata → žiadny alert
            return out
        clear_cleanup_alert(profile)           # ideme upratať → alert netreba
        _kw = float(dec["kw"])
        _act = "discharge" if direction == "sell" else "charge"
        _h0, _m0 = divmod(cur_slot * 15, 60)
        _h1, _m1 = divmod(cur_slot * 15 + 15, 60)
        _slot_str = f"{_h0:02d}:{_m0:02d}-{_h1:02d}:{_m1:02d}"
        _res = {"ok": True, "profile": profile, "ts": result.get("ts", ""),
                "current": {"slot": _slot_str, "action": _act, "kw": _kw,
                            "kwh_per_slot": _kw * 0.25, "price_eur_mwh": action_price,
                            "soc_after_pct": cur_soc},
                "soc": {"soc_pct": cur_soc, "source": "vdt_cleanup"},   # TAG → reason stĺpec
                "profit_eur": 0.0}
        append_paper_trade(_res)
        out["acted"] = True; out["reason"] = dec.get("reason", "")
        print(f"[VDT-CLEANUP] {profile} {_slot_str} {_act} {_kw:.0f}kW → {dec.get('reason','')}")
        return out
    except Exception as e:
        out["reason"] = f"cleanup zlyhal (fail-safe): {e}"
        return out


def force_cleanup(profile: str) -> Dict[str, Any]:
    """MANUÁLNY OVERRIDE (užívateľ potvrdil z banneru): uprac nedodateľný slot za NAJLEPŠIU
    dostupnú cenu aj napriek strate (ignoruje max_loss). Vždy len ZMENŠUJE odchýlku (cap na
    voľný výkon + |deviation|), commit tagom `vdt_cleanup`, potom zmaže alert. Fail-safe."""
    out = {"acted": False, "reason": ""}
    try:
        if not profile:
            out["reason"] = "chýba profil"; return out
        import vdt_state as _vs
        import core.soc_use_audit as _sua
        import profiles as _pr
        from core.vdt_cleanup import detect_undeliverable_target
        _today = dt.date.today()
        st = _vs.compute_current_state(profile, today=_today)
        if not st or not st.get("ok"):
            out["reason"] = "nedostupný stav profilu"; return out
        cap = float(st.get("batt_kwh") or 0.0)
        if cap <= 0:
            out["reason"] = "batt_kwh≤0"; return out
        eff_c = float(st.get("eff_c") or 0.95); eff_d = float(st.get("eff_d") or 0.95)
        soc_min = float(st.get("soc_min_pct") or 5.0); soc_max = float(st.get("soc_max_pct") or 100.0)
        cur_soc = float(st.get("current_soc_pct") or 50.0)
        cur_slot = int(st.get("current_slot_idx") or 0)
        dam = list(st.get("dam_nomination_kwh") or [0.0] * 96)
        vdt = list(st.get("vdt_realized_kwh") or [0.0] * 96)
        while len(dam) < 96: dam.append(0.0)
        while len(vdt) < 96: vdt.append(0.0)
        sched = [float(dam[i]) + float(vdt[i]) for i in range(96)]
        sim = [0.0] * cur_slot + sched[cur_slot:]
        path = _sua.simulate_soc_unclipped(cur_soc, sim, cap, eff_c=eff_c, eff_d=eff_d)
        _pl = (_pr.load_profile(profile) or {}).get("plan") or {}
        batt_kw = float(_pl.get("batt_kw", 0.0) or 0.0)
        tgt = detect_undeliverable_target(path, cur_slot, soc_min_pct=soc_min,
                                          soc_max_pct=soc_max, batt_kwh=cap, batt_kw=batt_kw)
        if not tgt:
            clear_cleanup_alert(profile)
            out["reason"] = "problém už neexistuje (nič netreba)"; return out
        direction = tgt["direction"]
        # Najlepšia dostupná cena TERAZ z orderbooku (cez čerstvý cache výsledok).
        best_price = None
        try:
            _cache = load_cache(profile) or {}
            _obps = _cache.get("orderbook_per_slot") or []
            d = _obps[int(cur_slot)] or {}
            best_price = float(d.get("bid") if direction == "sell" else d.get("ask"))
        except Exception:
            best_price = None
        max_action_kw = max(0.0, batt_kw - abs(float(sched[cur_slot]))) if batt_kw > 0 else 0.0
        _kw = min(abs(float(tgt["deviation_kw"])), abs(float(max_action_kw)))
        if _kw < 1e-6:
            out["reason"] = "žiadna voľná kapacita (max_action_kw≈0)"; return out
        _act = "discharge" if direction == "sell" else "charge"
        _h0, _m0 = divmod(cur_slot * 15, 60); _h1, _m1 = divmod(cur_slot * 15 + 15, 60)
        _slot_str = f"{_h0:02d}:{_m0:02d}-{_h1:02d}:{_m1:02d}"
        _res = {"ok": True, "profile": profile, "ts": _today.isoformat(),
                "current": {"slot": _slot_str, "action": _act, "kw": _kw,
                            "kwh_per_slot": _kw * 0.25, "price_eur_mwh": best_price,
                            "soc_after_pct": cur_soc},
                "soc": {"soc_pct": cur_soc, "source": "vdt_cleanup"},
                "profit_eur": 0.0}
        append_paper_trade(_res)
        clear_cleanup_alert(profile)
        out.update(acted=True, kw=round(_kw, 1), price_eur_mwh=best_price,
                   reason=f"OVERRIDE uprataný {_act} {_kw:.0f} kW @ {best_price} €/MWh")
        print(f"[VDT-CLEANUP-FORCE] {profile} {_slot_str} {_act} {_kw:.0f}kW @ {best_price}")
        return out
    except Exception as e:
        out["reason"] = f"force_cleanup zlyhal: {e}"
        return out


def place_manual_trade(profile: str, slot: str, action: str, kw: float,
                        price_eur_mwh: float) -> Dict[str, Any]:
    """RUČNÝ obchod z UI (užívateľ zadá objem, cenu, čas). Override — zapíše sa presne
    ako zadané (bypass audit, tag `vdt_manual`), lebo je to explicitné ľudské rozhodnutie.
    Vráti aj upozornenie z auditu (či by bol nedodateľný), ale NEBLOKUJE. Fail-safe.

    Args: slot = "HH:MM" alebo "HH:MM-HH:MM" (začiatok 15-min slotu), action = buy/sell/
    charge/discharge, kw > 0, price v €/MWh (môže byť aj záporná)."""
    out = {"ok": False, "reason": ""}
    try:
        if not profile:
            out["reason"] = "chýba profil"; return out
        _a = str(action or "").lower().strip()
        if _a in ("buy", "nakup", "nákup"):
            _a = "charge"
        elif _a in ("sell", "predaj"):
            _a = "discharge"
        if _a not in ("charge", "discharge"):
            out["reason"] = f"neznáma akcia: {action!r}"; return out
        _kw = abs(float(kw or 0.0))
        if _kw < 1e-6:
            out["reason"] = "objem (kW) musí byť > 0"; return out
        _px = float(price_eur_mwh)
        _s = str(slot or "").strip()[:5]           # "HH:MM"
        if len(_s) != 5 or ":" not in _s:
            out["reason"] = f"neplatný čas slotu: {slot!r} (očakávam HH:MM)"; return out
        _hh = int(_s[:2]); _mm = int(_s[3:5])
        _si = (_hh * 60 + _mm) // 15
        _h1, _m1 = divmod(_si * 15 + 15, 60)
        _slot_str = f"{_hh:02d}:{(_si*15) % 60:02d}-{_h1:02d}:{_m1:02d}"
        _today = dt.date.today().isoformat()
        # Upozornenie z auditu (nedodateľnosť) — len info, nezastaví zápis.
        _warn = ""
        try:
            from core.soc_use_audit import audit_action as _sa
            import vdt_state as _vs_m
            _ts_m = _vs_m.compute_current_state(profile, today=dt.date.fromisoformat(_today))
            _cs = float(_ts_m.get("current_soc_pct")) if (_ts_m and _ts_m.get("ok")) else None
            _cslot = int(_ts_m.get("current_slot_idx") or 0) if (_ts_m and _ts_m.get("ok")) else None
            _r = _sa(profile, _today, _si, _a, _kw * 0.25, source="vdt",
                     today_state=_ts_m, current_soc_pct_at_si=_cs, sim_from_slot=_cslot)
            if _r.get("decision") != "accept":
                _warn = f"POZOR (audit): {_r.get('reason', '')}"
        except Exception:
            _warn = ""
        _res = {"ok": True, "profile": profile, "ts": _today,
                "current": {"slot": _slot_str, "action": _a, "kw": _kw,
                            "kwh_per_slot": _kw * 0.25, "price_eur_mwh": _px,
                            "soc_after_pct": 0.0},
                "soc": {"soc_pct": 0.0, "source": "vdt_manual"},
                "profit_eur": 0.0}
        append_paper_trade(_res, bypass_audit=True)
        clear_cleanup_alert(profile)               # ručný zásah rieši problém → zruš alert
        out.update(ok=True, slot=_slot_str, action=_a, kw=round(_kw, 1),
                   price_eur_mwh=_px, warn=_warn,
                   reason=f"Zapísaný ručný obchod {_a} {_kw:.0f} kW @ {_px} €/MWh v {_slot_str}")
        print(f"[VDT-MANUAL] {profile} {_slot_str} {_a} {_kw:.0f}kW @ {_px} {('| '+_warn) if _warn else ''}")
        return out
    except Exception as e:
        out["reason"] = f"ručný obchod zlyhal: {e}"
        return out


def run_and_cache(**kwargs) -> Dict[str, Any]:
    """Helper — spustí get_live_recommendation, uloží do cache + paper trade log."""
    res = get_live_recommendation(**kwargs)
    if res.get("ok"):
        save_cache(res)
        append_paper_trade(res)
        # VDT Upratovanie (task #82) — bezpečnostná sieť, DEFAULT OFF (VDT_CLEANUP=1).
        # Fail-safe: nikdy nezhodí normálny flow.
        try:
            propose_cleanup(res)
        except Exception as _e_cl:
            print(f"[VDT-CLEANUP] fail-safe: {_e_cl}")
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
