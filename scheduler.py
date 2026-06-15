# -*- coding: utf-8 -*-
"""
scheduler.py — APScheduler-based background scheduler pre server deployment.

Beží paralelne s livesim bg loopom (ten je v app.py @ lifespan). Tento modul
rieši DENNÉ batch úlohy ktoré majú zmysel spustiť v konkrétny čas:

  • ~06:30 — PVF fetch (počasie + FTV predikcia na zajtra)
  • ~13:30 — DAM clearing (CZ OTE + SK OKTE day-ahead ceny pre zajtra)
  • ~11:35 — SK imbalance fetch (OKTE ISZO, preliminarydaily za D-1)
  • ~14:30 — Auto D-1 plán generovanie pre všetky aktívne profily

Joby sú registrované cez `start()` ktorý vracia bežiacu inštanciu schedulera.
Lifespan v app.py ho štartuje pri boote a zastaví pri shutdowne.

Konfigurácia cez ENV vars:
  SCHEDULER=0                → vypne scheduler úplne (užitočné pre dev)
  SCHEDULER_TZ=Europe/Bratislava  → časová zóna (default Bratislava)
  SCHED_PVF_CRON="30 6 * * *"     → override PVF schedule
  SCHED_DAM_CRON="30 13 * * *"    → override DAM schedule
  SCHED_IMBALANCE_CRON="35 11 * * *"  → override imbalance schedule
  SCHED_AUTOPLAN_CRON="30 14 * * *"   → override auto-plán schedule
  SCHED_JOB_DISABLE=pvf,dam       → comma-sep zoznam jobov ktoré vypnúť

Job ID-čka (pre `sched.modify_job()` / `pause_job()` z UI):
  pvf_fetch, dam_fetch_cz, dam_fetch_sk, imbalance_fetch_sk, autoplan_d1
"""
from __future__ import annotations
import os
import datetime as dt
import traceback
from typing import Optional

# APScheduler je optional — ak nie je nainštalovaný, lifespan to zachytí cez ImportError
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
except ImportError as e:
    raise ImportError(
        f"APScheduler nie je nainštalovaný ({e}). Nainštaluj: pip install apscheduler"
    )


# ─── Defaults (cron syntax: minute hour day-of-month month day-of-week) ──────
_DEFAULT_CRONS = {
    "pvf_fetch":           "30 6 * * *",        # 06:30 — PVF predikcia
    "dam_fetch_cz":        "30 13 * * *",       # 13:30 — OTE CZ
    "dam_fetch_sk":        "35 13 * * *",       # 13:35 — OKTE SK
    "imbalance_fetch_sk":  "35 11 * * *",       # 11:35 — OKTE ISZO D-1
    "autoplan_d1":         "0 14 * * *",        # 14:00 — auto D-1 plán (po SK/CZ DAM clearance)
    "seps_cookies":        "*/25 * * * *",      # každých 25 min — obnov SEPS cookies/XSRF
    "seps_realtime_log":   "* * * * *",         # každú minútu — log SEPS okamžitých hodnôt do CSV
    "historian_login":     "*/45 * * * *",      # každých 45 min — relogin firemný historian (1h session timeout)
    "historian_extend":    "*/5 * * * *",       # každých 5 min — incremental sync SK tagov do CSV
    "realio_poll":         "* * * * *",         # každú minútu — poll real-time FTV/batt z DAMSU + log do CSV
    "realio_relogin":      "*/25 * * * *",      # každých 25 min — Playwright relogin do dashboardu (cookies expirujú ~30 min)
    "vdt_advisor":         "*/15 * * * *",      # každých 15 min — rolling MPC re-optimization pre VDT (cache do JSON)
    "joint_mpc_tick":      "* * * * *",         # každú min — joint MPC kontrolér (Bug CC2): SOC + DAM + VDT + FTV + Load → joint LP
    "zco_profile_rebuild": "0 2 * * 0",         # nedeľa 02:00 — prebuilduj SK deviation_profile (PV+weekday split)
    "auto_control_apply":  "0,15,30,45 * * * *", # každú 15-minútovku — paper trading sim apply D-1 plánu (Fáza A.5)
}
_DEFAULT_TZ = "Europe/Bratislava"


def _log(job_id: str, msg: str, *, level: str = "info"):
    """Stručný log — neskôr môže ísť do structlog/loguru."""
    prefix = "✓" if level == "info" else ("⚠" if level == "warn" else "✗")
    print(f"[sched/{job_id}] {prefix} {msg}", flush=True)


def _safe(job_id: str):
    """Decorator — chytá výnimky v job-e aby pád jedného nevyhodil scheduler."""
    def deco(fn):
        def wrapper():
            try:
                fn()
            except Exception as e:
                _log(job_id, f"ZLYHANIE: {e}\n{traceback.format_exc()}", level="error")
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


# ─── Jednotlivé joby ─────────────────────────────────────────────────────────
# Každý job je SAMOSTATNE callable funkcia (nie HTTP handler) — to je dôležité
# pre testovateľnosť a manuálne spustenie z REPL.

def _resolve_pv_location() -> Optional[dict]:
    """Vráti {lat, lon, kwp, tilt, azimuth, eff} pre auto PVF fetch.

    Priorita: ENV vars (PV_LAT, PV_LON, PV_KWP, ...) → posledný uložený plán
    v plan_store → None (vtedy auto-fetch preskočíme s warning-om).

    TODO: keď profiles.py bude ukladať aj koordináty (lat/lon/kwp), preferovať
    aktívny profil. Momentálne formulár /plan má lat/lon ako Form fields ale
    profil ich neperzistuje.
    """
    env_lat = os.environ.get("PV_LAT")
    env_lon = os.environ.get("PV_LON")
    if env_lat and env_lon:
        try:
            return {
                "lat": float(env_lat), "lon": float(env_lon),
                "kwp": float(os.environ.get("PV_KWP", "99.0")),
                "tilt": float(os.environ.get("PV_TILT", "30.0")),
                "azimuth": float(os.environ.get("PV_AZIMUTH", "0.0")),
                "eff": float(os.environ.get("PV_EFF", "0.85")),
            }
        except ValueError as e:
            _log("pvf_fetch", f"ENV vars zle naparsovateľné: {e}", level="warn")
    # TODO: try plan_store last-known coordinates
    return None


@_safe("pvf_fetch")
def job_pvf_fetch():
    """Stiahne PVF predikciu na zajtra (Open-Meteo) pre konfigurované miesto.

    Koordináty: ENV vars PV_LAT/PV_LON/PV_KWP/... — pre teraz najjednoduchšie.
    Neskôr, keď profile budú ukladať aj geo, zoberie sa z aktívneho profilu.
    """
    import data_sources as ds
    loc = _resolve_pv_location()
    if loc is None:
        _log("pvf_fetch", "preskakujem — chýbajú PV_LAT/PV_LON env vars", level="warn")
        return
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    _log("pvf_fetch", f"štart pre {tomorrow} @ ({loc['lat']:.4f}, {loc['lon']:.4f})")
    df = ds.fetch_pv_forecast(loc["lat"], loc["lon"], loc["kwp"], loc["tilt"],
                              loc["azimuth"], loc["eff"],
                              start=tomorrow, end=tomorrow)
    _log("pvf_fetch", f"OK — {len(df)} riadkov")


@_safe("dam_fetch_cz")
def job_dam_fetch_cz():
    """Stiahne OTE CZ day-ahead clearing na zajtra (publikované ~13:00)."""
    import data_sources as ds
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    _log("dam_fetch_cz", f"štart pre {tomorrow}")
    df = ds.fetch_ote_dayahead(tomorrow)
    _log("dam_fetch_cz", f"OK — {len(df)} period")


@_safe("dam_fetch_sk")
def job_dam_fetch_sk():
    """Stiahne OKTE SK day-ahead clearing na zajtra."""
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    _log("dam_fetch_sk", f"štart pre {tomorrow}")
    try:
        import okte_sk
        df = okte_sk.fetch_okte_dayahead(tomorrow)
        _log("dam_fetch_sk", f"OK — {len(df)} period")
    except ImportError:
        _log("dam_fetch_sk", "okte_sk modul nedostupný", level="warn")
    except Exception as e:
        _log("dam_fetch_sk", f"fetch zlyhal: {e}", level="warn")


@_safe("imbalance_fetch_sk")
def job_imbalance_fetch_sk():
    """Stiahne SK imbalance (ZCO) za D-1 z OKTE ISZO a appendne do CSV.

    Delegát na fetch_okte_imbalance.py logiku (rovnaké ako CLI script).
    """
    import fetch_okte_imbalance as foi
    yesterday = dt.date.today() - dt.timedelta(days=1)
    _log("imbalance_fetch_sk", f"štart pre {yesterday} (preliminarydaily)")
    try:
        df = foi.fetch_day_combined(yesterday, "preliminarydaily")
        added, total = foi.upsert_csv(df, foi.OUT_CSV)
        _log("imbalance_fetch_sk", f"OK — +{added} riadkov, total {total} (v {foi.OUT_CSV})")
    except Exception as e:
        _log("imbalance_fetch_sk", f"fetch zlyhal: {e}", level="warn")


@_safe("historian_extend")
def job_historian_extend():
    """Incremental sync SK tagov do CSV — udržiava out/sk/historian_*.csv čerstvé.

    Každých 5 min stiahne nové body od posledného CSV riadku po teraz.
    Tagy: SEPS reg.výkon (3-min), OKTE DAM, ZCO, VDT (15-min každý).
    """
    import historian_backfill as hb
    import internal_historian as ih
    h = ih.Historian()
    total = 0
    for tag in hb.DEFAULT_TAGS:
        try:
            n = hb.extend_tag_to_now(h, tag, verbose=False)
            total += n
        except Exception as e:
            _log("historian_extend", f"{tag} zlyhalo: {e}", level="warn")
    if total > 0:
        _log("historian_extend", f"+{total} nových bodov v {len(hb.DEFAULT_TAGS)} CSV")


@_safe("historian_login")
def job_historian_login():
    """Pravidelný relogin do interného firemného historiana (Bender / Express.js).

    Session typicky vyprší za ~1h. Refreshujeme každých 45 min defenzívne.
    Vyžaduje HISTORIAN_PASSWORD env var (príp. HISTORIAN_USER, default 'admin').
    """
    import historian_login as hl
    ok, msg = hl.login_once(verbose=False)
    if ok:
        _log("historian_login", f"OK — {msg}")
    else:
        _log("historian_login", f"refresh zlyhal: {msg}", level="warn")


@_safe("seps_realtime_log")
def job_seps_realtime_log():
    """Každú minútu fetchne SEPS realtime + appendne do CSV (s dedup podľa updated_at).

    SEPS updateuje hodnoty každé ~3 min, takže väčšina volaní bude no-op (skip
    duplicate). Cieľ: kontinuálne logovať pre back-test SEPS-based RT enginu.
    """
    import seps_sk
    path = seps_sk.log_realtime()
    if path:
        # Vypisuj len keď sa naozaj zapísal nový riadok (1×/3min priemerne)
        _log("seps_realtime_log", f"+1 riadok → {path}")


@_safe("realio_relogin")
def job_realio_relogin():
    """Periodický Playwright login do dashboardu — udržuje cookies čerstvé.
    Beží len keď je realio modul enabled."""
    try:
        import realio as _rio
        cfg = _rio.load_config()
    except ImportError:
        return
    if not cfg.get("enabled"):
        return
    import realio_login
    ok, msg = realio_login.login_once(verbose=False)
    if ok:
        _log("realio_relogin", f"OK — {msg}")
    else:
        _log("realio_relogin", f"zlyhalo — {msg}", level="warn")


@_safe("vdt_advisor")
def job_vdt_advisor():
    """Rolling MPC re-optimization pre VDT obchodovanie.

    Iteruje cez VŠETKY profily v aktívnom trhu (real aj simulation) a pre každý
    spustí get_live_recommendation s tým profile-om → vlastný per-profile cache.
    Tým pádom chPlan v /livesim aj /vdt/live_advisor zobrazujú VDT plán pre
    akýkoľvek aktívny profil bez ohľadu na poradie scheduler runu.

    Read-only — žiadne objednávky.
    """
    try:
        import vdt_live_advisor as _adv
        import vdt_arbitrage as _arb
        import profiles as _pr
        import market as _mk
    except ImportError:
        return

    # CZ guard — VDT je SK trh (OKTE). Na CZ nemáme prístup k českému VDT trhu,
    # takže žiadne paper trades ani plánovanie tu nesmie vznikať.
    try:
        active_mk = (_mk.active_market() or "").lower()
    except Exception:
        active_mk = ""
    if active_mk == "cz":
        _log("vdt_advisor",
             "CZ trh — VDT advisor sa preskakuje (nemáme prístup k českému VDT)")
        return

    profs = []
    try:
        profs = _pr.list_profiles() or []
    except Exception:
        pass
    if not profs:
        _log("vdt_advisor", "žiadne profily v aktívnom trhu", level="warn")
        return

    # Bug #605: Bug UU gate filter - preskoc profily s use_vdt:False.
    # Inak scheduler spusti LP pre Simulacia_Coop / Trakany_real a vyhodi
    # 'LP infeasible' warning v kazdom 5-min tiku.
    try:
        from core.schemas.vdt import should_log_vdt_for_profile as _gate
    except Exception:
        _gate = None

    n_ok = 0; n_fail = 0; n_skipped = 0
    for prof in profs:
        prof_name = prof if isinstance(prof, str) else (
            prof.get("name") if isinstance(prof, dict) else None)
        if not prof_name:
            continue
        # Bug UU: preskoc ak gate=False (use_vdt:False alebo bg_off bez VDT).
        if _gate is not None:
            try:
                if not _gate(prof_name):
                    n_skipped += 1
                    continue
            except Exception:
                pass   # gate fail -> nech LP rozhodne
        # Defaults from profile (batt geometry, fees, ...)
        try:
            defaults = _arb.get_default_params_from_profile(profile=prof_name)
        except Exception as e:
            _log("vdt_advisor", f"{prof_name}: defaults zlyhali · {e}", level="warn")
            n_fail += 1
            continue
        try:
            res = _adv.run_and_cache(
                batt_kw=defaults.get("batt_kw", 500.0),
                batt_kwh=defaults.get("batt_kwh", 800.0),
                eff_c=defaults.get("eff_c", 0.95),
                eff_d=defaults.get("eff_d", 0.95),
                grid_fee=defaults.get("grid_fee", 22.0),
                cycle_cost=defaults.get("cycle_cost", 2.0),
                min_spread=defaults.get("min_spread", 5.0),
                soc_start_pct=None,
                soc_end_min_pct=None,   # Bug VDT-SOC-RANGE: None → zdedí plan.soc_min z profilu
                max_cycles_per_day=None,  # None → zdedí plan.max_cycles_per_day z profilu
                use_orderbook=True,
                use_dam_commitments=True,
                profile=prof_name,
            )
            if res.get("ok"):
                cur = res.get("current", {})
                _log("vdt_advisor",
                     f"{prof_name}: OK · {cur.get('action','?')} "
                     f"{cur.get('kw',0):.0f} kW · profit {res.get('profit_eur',0):+.2f}€")
                n_ok += 1
                # Zapíš aj CURTAIL_FTV / LOAD_COVER / BUY / SELL extras do paper log
                # — tým sa zobrazia v chPlan ako "VDT realizované".
                try:
                    import vdt_extras as _vex_sched
                    import datetime as _dt_sched
                    _today_iso = _dt_sched.date.today().isoformat()
                    # Bug #618: pred novým behom VDT advisora zmaž **predchádzajúce VDT
                    # rezervácie** v capacity ledger pre tento profil + dnes. Inak
                    # stale rezervácie z minulých 15-min cron tickov blokujú nové
                    # návrhy — VDT advisor potom dostáva "REJECT ziadna voľná kapacita"
                    # aj keď D-1 plán nebráni novému zásahu.
                    # Trade_id má deterministický formát {profile}_{day}_{slot}_{action}_extras,
                    # ale rôzny "action" medzi behmi (charge↔discharge) by vytváral
                    # duplicitné rezervácie ktoré sa nikdy nemažú. release() vyrieši.
                    try:
                        from core.capacity_ledger import release as _release_vdt
                        _n_rel = _release_vdt(prof_name, _today_iso, source="vdt")
                        if _n_rel > 0:
                            _log("vdt_advisor",
                                 f"{prof_name}: reset {_n_rel} starých VDT rezervácií pred novým behom",
                                 level="info")
                    except Exception as _e_rel:
                        pass   # fail-safe — pokračuj aj keď reset zlyhá
                    # Spustí greedy s tými istými parametrami (low cost — len 1 fn call)
                    _ob_df_full = res.get("snapshot_df")   # ak by sme to ukladali
                    # Snapshot nie je v cache, takže pre extras logging použijeme
                    # iba dáta z full_plan / dam_commits z cache (best-effort).
                    # Tu len LOG signál "stalo sa to" — nebudeme prepočítavať LP.
                    fp = res.get("full_plan", []) or []
                    # Bug VDT-EXTRA-ONLY (Koreň 1, 2026-06-15): NEloguj CELÝ plán ako VDT obchody!
                    # Pôvodne sa KAŽDÝ slot full_planu (DAM+VDT) zalogoval ako VDT paper trade
                    # s order-book cenou (0/200/147 pre nelikvidné sloty) → "VDT obchody" obsahovali
                    # DAM nomináciu za nezmyselné ceny → falošná strata (user: "nákup za 200/300 je
                    # mimo"). Loguj LEN VDT EXTRA nad DAM nomináciou (= reálna arbitráž) pri jej VDT
                    # cene. DAM časť patrí do "z toho DT", nie do VDT.
                    try:
                        import d1_planner as _d1c_sx
                        import datetime as _dt_sx
                        _dam_sx = _d1c_sx.get_dam_commitments(_dt_sx.date.today(),
                                                              profile=prof_name, basis="batt")
                    except Exception:
                        _dam_sx = None
                    _last_idx_l = -1
                    for entry in fp:
                        sl = str(entry.get("slot", ""))
                        if "-" not in sl or len(sl) < 5:
                            continue
                        try:
                            _h = int(sl[:2]); _m = int(sl[3:5])
                            _idx = (_h * 60 + _m) // 15
                        except Exception:
                            continue
                        if not (0 <= _idx < 96):
                            continue
                        if _last_idx_l >= 0 and _idx < _last_idx_l:
                            break
                        _last_idx_l = _idx
                        kwh_e = float(entry.get("kwh", 0) or 0)
                        action_e = entry.get("action", "")
                        # net plánu v slote (+vybíja, −nabíja)
                        _net_e = (kwh_e if action_e == "discharge"
                                  else (-kwh_e if action_e == "charge" else 0.0))
                        # VDT extra = plán − DAM nominácia (basis batt: +di −ch). DAM=DT, nie VDT.
                        _dam_e = float(_dam_sx[_idx]) if (_dam_sx and _idx < len(_dam_sx)) else 0.0
                        _extra_e = _net_e - _dam_e
                        if abs(_extra_e) <= 0.5:
                            continue                      # čisto DAM slot → žiadny VDT obchod
                        _act_x = "discharge" if _extra_e > 0 else "charge"
                        _px = float((entry.get("sell_price") if _extra_e > 0
                                     else entry.get("buy_price")) or 0)
                        _adv.append_extra_paper_trade(
                            profile=prof_name,
                            slot=sl,
                            action=_act_x,
                            kwh=abs(_extra_e),
                            price_eur_mwh=_px,
                            reason=f"vdt_extra_{_act_x}",
                            soc_pct=float(entry.get("soc_after_pct", 0) or 0),
                        )
                except Exception as _e_sx:
                    pass   # logger zlyhal, ale advisor cache je OK — nezastavujeme
            else:
                _log("vdt_advisor",
                     f"{prof_name}: zlyhalo · {res.get('error','?')}", level="warn")
                n_fail += 1
        except Exception as e:
            _log("vdt_advisor", f"{prof_name}: exception · {e}", level="warn")
            n_fail += 1

    _log("vdt_advisor",
         f"hotovo · {n_ok}/{len(profs)} OK · {n_fail} zlyhalo · {n_skipped} preskocene (use_vdt:False)")


@_safe("joint_mpc_tick")
def job_joint_mpc_tick():
    """Bug CC2 (2026-06-07): Joint MPC kontrolér — beží každú minútu.

    Pre KAŽDÝ profil s joint_mpc_enabled=True v profile.plan:
      1. Volá mpc_controller.run_mpc_tick(profile)
      2. Ten cez vdt_state.compute_current_state získa aktuálny SOC + DAM + VDT
      3. Plus FTV/Load/DAM/VDT ceny per zostávajúce sloty
      4. Volá joint_lp.optimize_joint_day → optimálny batt setpoint + plán
      5. Zapíše JSON cache out/{market}/mpc_tick_{profile}.json (read-only debug)
      6. CC3: aplikuje paper log (sim) alebo Bender setpoint (real) — gating
         podľa profile.mode + safety gates v auto_control.

    Read-only ak joint_mpc_enabled=False alebo profil bg-OFF.
    """
    try:
        import mpc_controller as _mpc
        import profiles as _pr
        import auto_control as _ac
        import market as _mk
    except ImportError:
        return

    # CZ guard — joint MPC primarily pre SK trh (cez OKTE VDT/DAM ceny)
    try:
        active_mk = (_mk.active_market() or "").lower()
    except Exception:
        active_mk = ""
    if active_mk == "cz":
        return   # CZ mimo VDT scope pre teraz

    # Načítaj profile zoznam + bg-enabled
    try:
        profs = _pr.list_profiles() or []
    except Exception:
        return
    try:
        bg_enabled = _ac.get_enabled_profiles()
    except Exception:
        bg_enabled = set()

    n_ok = 0
    n_fail = 0
    n_skip = 0
    for prof_name in profs:
        if prof_name not in bg_enabled:
            n_skip += 1
            continue
        try:
            p = _pr.load_profile(prof_name) or {}
            plan = p.get("plan") or {}
            if not bool(plan.get("joint_mpc_enabled", False)):
                n_skip += 1
                continue
        except Exception:
            n_skip += 1
            continue

        try:
            result = _mpc.run_mpc_tick(prof_name, write_cache=True)
            if result.get("ok"):
                n_ok += 1
                # CC3: aplikácia outputu (sim aj real cez auto_control safety gates)
                try:
                    import mpc_apply as _mpc_a
                    _mpc_a.apply_mpc_output(prof_name, result, profile_mode=p.get("mode", "simulation"))
                except Exception as _e_app:
                    _log("joint_mpc_tick", f"{prof_name}: apply zlyhalo · {_e_app}",
                         level="warn")
            else:
                n_fail += 1
                _log("joint_mpc_tick", f"{prof_name}: {result.get('reason', '?')}",
                     level="warn")
        except Exception as e:
            n_fail += 1
            _log("joint_mpc_tick", f"{prof_name}: exception · {e}", level="warn")
    if n_ok or n_fail:
        _log("joint_mpc_tick", f"OK {n_ok} · skip {n_skip} · fail {n_fail}")


@_safe("realio_poll")
def job_realio_poll():
    """Periodický poll real-time meraní (FTV/batt/SOC/grid) cez DAMSU tagy.

    Nepokladá nič ak modul nie je enabled (config flag). Inak fetch + append do
    out/realio_measurements.csv. Žiadny error = nepíše do logu (aby to neflood-lo).
    """
    try:
        import realio as _rio
    except ImportError:
        return
    cfg = _rio.load_config()
    if not cfg.get("enabled"):
        return
    vals = _rio.poll_and_log()
    if vals:
        nn = sum(1 for k, v in vals.items() if k != "_ts" and v is not None)
        _log("realio_poll", f"OK ({nn} tagov)")


@_safe("seps_cookies")
def job_seps_cookies():
    """Obnoví SEPS DAE cookies + XSRF cez headless Chromium (Playwright).

    DAE má ~30 min session timeout, takže refreshneme každých 25 min defensively.
    """
    import seps_cookies_refresh
    ok, msg = seps_cookies_refresh.refresh_once(verbose=False)
    if ok:
        _log("seps_cookies", f"OK — {msg}")
    else:
        _log("seps_cookies", f"refresh zlyhal: {msg}", level="warn")


@_safe("autoplan_d1")
def job_autoplan_d1():
    """Auto-generovanie D-1 plánu na zajtra pre všetky uložené profile.

    Beží po DAM cleare (14:30). Pre každý profil zavolá `compute_d1_plan`
    ktorý uloží plán ako `kind='dentrh'` (15-min) do plan_store.
    /dentrh stránka, /vdt/d1 viewer a VDT live advisor čítajú TEN ISTÝ plán
    cez cascade `dentrh → plan` — guarantuje konzistenciu naprieč UI.
    """
    tomorrow = dt.date.today() + dt.timedelta(days=1)
    _log("autoplan_d1", f"štart pre {tomorrow}")
    try:
        import profiles as pr
    except ImportError:
        _log("autoplan_d1", "profiles.py nedostupný — preskakujem", level="warn")
        return

    profile_names = []
    try:
        profile_names = [p.get("name") if isinstance(p, dict) else p
                         for p in pr.list_profiles()]
    except Exception as e:
        _log("autoplan_d1", f"list_profiles zlyhalo: {e}", level="warn")
        return

    if not profile_names:
        _log("autoplan_d1", "žiadne uložené profile — preskakujem", level="warn")
        return

    # Pre každý profil v aktívnom markete spusti D-1 plán cez d1_planner.
    # Plán sa uloží do plan_store, ostatné komponenty (VDT live advisor,
    # /vdt/d1 stránka) ho automaticky využijú.
    try:
        import d1_planner as _d1p
        import market as _mk
    except ImportError as e:
        _log("autoplan_d1", f"d1_planner/market import zlyhal: {e}", level="warn")
        return

    active_market = _mk.get_active_market()
    ok_count = fail_count = 0
    for prof in profile_names:
        try:
            res = _d1p.compute_d1_plan(tomorrow, market=active_market,
                                          profile=prof, save_to_store=True)
            if res.get("ok"):
                zisk = res.get("summary", {}).get("ZISK_EUR", 0)
                _log("autoplan_d1", f"  {prof}: OK · ZISK {zisk:+.2f} €")
                ok_count += 1
            else:
                _log("autoplan_d1", f"  {prof}: ZLYHALO · {res.get('error','?')}", level="warn")
                fail_count += 1
        except Exception as e:
            _log("autoplan_d1", f"  {prof}: exception · {e}", level="warn")
            fail_count += 1
    _log("autoplan_d1", f"hotovo · {ok_count} OK · {fail_count} zlyhalo · trh={active_market}")


@_safe("zco_profile_rebuild")
def job_zco_profile_rebuild():
    """Prebuilduje SK deviation_profile.json zo všetkých dostupných historian dát.

    Zdroje:
      - out/sk/historian_I_WEB_OKTE_ZCO_15m.csv (ZCO)
      - out/sk/historian_C_OKTE_ISOT_15m_final.csv (DT clearing)
    Output: out/sk/deviation_profile.json (PV-bucket + weekday split).

    Beží v nedeľu o 02:00 — za týždeň pribudli ~672 nových 15-min slotov,
    čo posunie štatistiku. ZCO predikcia v Live advisor/D-1 bias/backtest
    automaticky preberá nový profile (load_profile() pri každom volaní).
    """
    _log("zco_profile_rebuild", "štart")
    try:
        import deviation_stats as _ds
    except ImportError as e:
        _log("zco_profile_rebuild", f"deviation_stats import zlyhal: {e}", level="warn")
        return
    try:
        prof = _ds.build_profile_sk(by_pv=False, by_weekday=True)
        _ds.save_profile(prof, _ds.PROFILE_PATH_SK)
        n_days = prof.get("n_days", 0)
        span = prof.get("span", ["?", "?"])
        n_cells = len(prof.get("cells", []))
        _log("zco_profile_rebuild",
             f"hotovo · {n_days} dní ({span[0]} → {span[1]}) · {n_cells} buniek · "
             f"uložené do {_ds.PROFILE_PATH_SK}")
    except Exception as e:
        _log("zco_profile_rebuild", f"build_profile_sk zlyhal: {e}", level="warn")


@_safe("auto_control_apply")
def job_auto_control_apply():
    """Fáza A.5 paper trading — každú štvrťhodinu prečíta D-1 plán
    pre aktuálny slot a aplikuje setpoint v SIMULATION móde.

    Iteruje cez všetky profily v aktívnom trhu, pre každý vyráta setpoint
    a zaloguje do out/<market>/auto_control_log.csv. REAL mode je BLOCKED
    (vyžaduje out/auto_control_unlock.json).
    """
    _log("auto_control_apply", "štart")
    try:
        import auto_control as _ac
        import market as _mk
    except ImportError as e:
        _log("auto_control_apply", f"auto_control import zlyhal: {e}", level="warn")
        return

    # CZ guard — VDT paper trading je SK trh (OKTE). Na CZ nemáme prístup k českému
    # VDT, takže auto_control sa preskakuje aj pre SIMULATION profily na CZ.
    try:
        active_mk = (_mk.active_market() or "").lower()
    except Exception:
        active_mk = ""
    if active_mk == "cz":
        _log("auto_control_apply",
             "CZ trh — auto_control sa preskakuje (nemáme prístup k českému VDT)")
        return

    # Kill switch check
    try:
        if _ac.kill_switch_active():
            _log("auto_control_apply", "kill switch aktívny — preskakujem", level="warn")
            return
    except Exception:
        pass

    # Pre každý profil v aktívnom trhu
    try:
        import profiles as _pr
        profs = _pr.list_profiles() or []
    except Exception as e:
        _log("auto_control_apply", f"profiles list zlyhal: {e}", level="warn")
        return

    if not profs:
        _log("auto_control_apply", "žiadne profily v aktívnom trhu")
        return

    # Filter len ENABLED profily (per-profile opt-in)
    try:
        enabled = _ac.get_enabled_profiles()
    except Exception:
        enabled = set()
    if not enabled:
        _log("auto_control_apply",
             "žiadny profil nemá paper trading zapnutý — preskakujem "
             "(zapni v UI /auto_control)")
        return

    n_logged = 0
    n_no_plan = 0
    n_iter = 0
    n_vdt_extras = 0
    for p in profs:
        # list_profiles() vracia list stringov; tolerantný k zmene API
        name = p if isinstance(p, str) else (p.get("name") if isinstance(p, dict) else None)
        if not name or name not in enabled:
            continue
        n_iter += 1
        try:
            sp = _ac.compute_setpoint_for_now(profile=name)
            if sp is None:
                continue
            res = _ac.apply_setpoint(sp, dry_run=True)   # vždy SIM mode
            if sp.get("setpoint_kw") is not None:
                n_logged += 1
            else:
                n_no_plan += 1
        except Exception as e:
            _log("auto_control_apply", f"{name}: zlyhalo · {e}", level="warn")
        # VDT extras (BUY/SELL/CURTAIL_FTV/LOAD_COVER) z cache → log ako "trade záznam"
        try:
            n_vdt_extras += _ac.log_vdt_extras_for_current_slot(profile=name)
        except Exception as e:
            _log("auto_control_apply", f"{name}: vdt_extras log zlyhal · {e}",
                 level="warn")

    _log("auto_control_apply",
         f"hotovo · {n_logged} setpointov zaolgovaných · "
         f"{n_no_plan} bez plánu · {n_vdt_extras} VDT extras · "
         f"{n_iter}/{len(profs)} profilov enabled · "
         f"mode=SIMULATION (real-write blocked)")


# ─── Public API ──────────────────────────────────────────────────────────────

def _cron_for(job_id: str) -> str:
    """Načíta cron z ENV var (SCHED_<JOB>_CRON) alebo vráti default."""
    env_key = f"SCHED_{job_id.upper()}_CRON"
    return os.environ.get(env_key, _DEFAULT_CRONS[job_id])


def _disabled_jobs() -> set[str]:
    raw = os.environ.get("SCHED_JOB_DISABLE", "").strip()
    if not raw:
        return set()
    return {j.strip() for j in raw.split(",") if j.strip()}


def start() -> BackgroundScheduler:
    """Vytvorí BackgroundScheduler, registruje joby a spustí. Vracia inštanciu.

    Lifespan v app.py drží referenciu a pri shutdowne zavolá .shutdown().
    """
    tz = os.environ.get("SCHEDULER_TZ", _DEFAULT_TZ)
    sched = BackgroundScheduler(timezone=tz)

    disabled = _disabled_jobs()
    jobs = [
        ("pvf_fetch",          job_pvf_fetch,          "PVF predikcia"),
        ("dam_fetch_cz",       job_dam_fetch_cz,       "OTE CZ DAM"),
        ("dam_fetch_sk",       job_dam_fetch_sk,       "OKTE SK DAM"),
        ("imbalance_fetch_sk", job_imbalance_fetch_sk, "OKTE ISZO D-1"),
        ("autoplan_d1",        job_autoplan_d1,        "Auto D-1 plán"),
        ("seps_cookies",       job_seps_cookies,       "SEPS cookies refresh"),
        ("seps_realtime_log",  job_seps_realtime_log,  "SEPS realtime CSV log"),
        ("historian_login",    job_historian_login,    "Historian relogin"),
        ("historian_extend",   job_historian_extend,   "Historian incremental sync"),
        ("realio_poll",        job_realio_poll,        "Realtime FTV/batt poll"),
        ("realio_relogin",     job_realio_relogin,     "Realio Playwright relogin"),
        ("vdt_advisor",        job_vdt_advisor,        "VDT rolling MPC advisor (15-min)"),
        ("joint_mpc_tick",     job_joint_mpc_tick,     "Joint MPC kontroler (1-min, Bug CC2)"),
        ("zco_profile_rebuild",job_zco_profile_rebuild,"SK ZCO deviation profile rebuild (nedeľa 02:00)"),
        ("auto_control_apply", job_auto_control_apply, "Fáza A.5 paper trading apply (SIMULATION)"),
    ]

    for job_id, fn, label in jobs:
        if job_id in disabled:
            print(f"[scheduler] ⊘ {job_id} ({label}) — vypnutý cez SCHED_JOB_DISABLE")
            continue
        cron_str = _cron_for(job_id)
        try:
            trigger = CronTrigger.from_crontab(cron_str, timezone=tz)
        except Exception as e:
            print(f"[scheduler] ⊘ {job_id}: neplatný cron '{cron_str}': {e}")
            continue
        sched.add_job(fn, trigger=trigger, id=job_id, name=label,
                      misfire_grace_time=600,    # 10 min — ak server bol down, ešte stihneme
                      coalesce=True,              # ak chýba viac runov, urob len 1
                      max_instances=1)            # neprekrývať
        print(f"[scheduler] + {job_id} ({label}) → cron '{cron_str}'")

    sched.start()
    return sched


def run_now(job_id: str):
    """Manuálne spustenie joba (pre debug / UI 'Run now' tlačidlo).

    Použitie: `python -c "import scheduler; scheduler.run_now('imbalance_fetch_sk')"`
    """
    funcs = {
        "pvf_fetch":          job_pvf_fetch,
        "dam_fetch_cz":       job_dam_fetch_cz,
        "dam_fetch_sk":       job_dam_fetch_sk,
        "imbalance_fetch_sk": job_imbalance_fetch_sk,
        "autoplan_d1":        job_autoplan_d1,
        "seps_cookies":       job_seps_cookies,
        "seps_realtime_log":  job_seps_realtime_log,
        "vdt_advisor":        job_vdt_advisor,
        "joint_mpc_tick":     job_joint_mpc_tick,
        "zco_profile_rebuild":job_zco_profile_rebuild,
        "auto_control_apply": job_auto_control_apply,
        "historian_login":    job_historian_login,
        "historian_extend":   job_historian_extend,
        "realio_poll":        job_realio_poll,
        "realio_relogin":     job_realio_relogin,
    }
    fn = funcs.get(job_id)
    if fn is None:
        raise ValueError(f"neznámy job: {job_id} (dostupné: {list(funcs)})")
    print(f"[scheduler] manuálny run: {job_id}")
    fn()


if __name__ == "__main__":
    # Test/debug entry point — spustí ľubovoľný job z command line.
    import sys
    if len(sys.argv) < 2:
        print("Použitie: python scheduler.py <job_id>")
        print(f"Dostupné: {list(_DEFAULT_CRONS.keys())}")
        sys.exit(1)
    run_now(sys.argv[1])
