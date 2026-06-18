# -*- coding: utf-8 -*-
"""
app.py – jednoduché webové rozhranie pre plán D-1 (FTV + batéria).
Spustenie:  python app.py     →  otvor http://127.0.0.1:8000
Závislosti: fastapi, uvicorn  (+ už máš: pandas, scikit-learn, scipy, openpyxl, requests, lxml)
"""
from __future__ import annotations
import datetime as dt, os, io, json

# Auto-load .env súboru ak existuje (credentials pre historian, atď.) —
# musí byť PRED importom modulov ktoré čítajú env vars pri loade.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import numpy as np
import pandas as pd
from fastapi import FastAPI, Form, UploadFile, File, Request
from fastapi.responses import HTMLResponse, FileResponse, Response, StreamingResponse, RedirectResponse

import data_sources as ds
import livesim as lsim
import case_config as cc
from ui.templates import render_legacy_body
from price_model import PriceModel, FEATURES
from optimizer import optimize_day
from report import build_plan_excel
from combined_backtest import run_combined
import rt_controller as rtc
try:
    import plan_overrides as po                           # ručné násobitele D-1 plánu (per-day + globálna šablóna)
except ImportError:
    po = None
try:
    import plan_store as ps                                # perzistencia D-1 plánov; livesim ich číta v strict mode
except ImportError:
    ps = None
try:
    import profiles as pr                                  # pomenované profile so všetkými parametrami
except ImportError:
    pr = None
try:
    import ftv_scenarios as fs                             # per-dátum override hodinového FTV priebehu
except ImportError:
    fs = None
try:
    import load_profile as lp                              # spotreba zákazníka per-profil (15-min CSV import)
except ImportError:
    lp = None
try:
    import baseline_calc as bc                             # výpočet baseline (bez batérie, bez plánu)
except ImportError:
    bc = None
try:
    import market as mk                                    # trh (CZ/SK) — per-port state, dátové cesty
except ImportError:
    mk = None

# ─── Refactored helpery (Fáza 1) ────────────────────────────────────────────
# State + UI nastavenia
from core.state import _PORT, UI_PATH, _ui_load, _ui_save, DEF, FX_CZK
# Cache + cached fetchers (OTE DT, PV, ISOT history, model)
from core.caches import (_MODEL_CACHE, _OTE_CACHE, _PVF_CACHE, _ISOT_HIST_CACHE,
                          _LIVE_FETCH_CACHE, _OTE_TTL_FUTURE, _PVF_TTL_FUTURE,
                          _ISOT_HIST_TTL, _LIVE_FETCH_TTL,
                          _model, _ote_cache_csv, _fetch_ote_cached,
                          _isot_history, _fetch_pv_cached)
# RT poradca fetch helpery
from core.fetch import _iv_ts, _rt_fetch, _rt_live_frame
# HTML helpery (pure)
from ui.html import _RECO_COL, _field, _active_profile_badge, _market_badge, _nav

# ─── Background runtime (lifespan, scheduled tasks, graceful shutdown) ────
# Toto je server-ready scaffolding: appka beží na pozadí (livesim loop, denné
# fetche, auto D-1 plán) aj keď nikto nemá otvorený prehliadač. Pri Docker
# stop / SIGTERM threadu sa korektne zastavia (stop_event + join).
import threading as _thr
from contextlib import asynccontextmanager

_bg_runtime = {
    "stop_event": None,
    "livesim_thread": None,
    "startup_backfill_thread": None,
    "scheduler": None,
    "started": False,                                        # guard proti duplicitnému štartu lifespan
}


def _bg_livesim_loop(stop_event: _thr.Event):
    """Livesim background loop — každých 60s vyhodnotí odchýlku + raz za hodinu backfill.
    Reaguje na stop_event pre graceful shutdown (Docker SIGTERM)."""
    import time
    last_bf = 0.0
    # úvodné čakanie aby appka nabehla — prerušiteľné cez stop_event
    for _ in range(8):
        if stop_event.is_set():
            return
        time.sleep(1)
    interval = int(os.environ.get("LIVESIM_BG_SEC", "60"))
    while not stop_event.is_set():
        try:
            now = time.time()
            if now - last_bf > 3600 and os.environ.get("NO_BACKFILL") != "1":
                try:
                    import backfill as bf
                    bf.backfill_all(log=lambda m: None)
                except Exception as e:
                    print("[livesim-bg backfill]", e)
                last_bf = now
            n = _livesim_bg_tick()
            if n and n > 0:
                print(f"[livesim-bg] +{n} min")
        except Exception as e:
            print("[livesim-bg loop]", e)
        # interrupt-able sleep — wait sa preruší keď stop_event.set()
        stop_event.wait(interval)


def _bg_startup_backfill():
    """Jednorázový backfill na štarte (dorovnať históriu)."""
    try:
        import backfill as bf
        bf.backfill_all(log=lambda m: print("[backfill]", m))
    except Exception as e:
        print("[backfill] preskočené:", e)


@asynccontextmanager
async def lifespan(app):
    """FastAPI lifespan — štartuje background úlohy a korektne ich zastaví pri shutdowne.

    Funguje rovnako či app pustíš cez `python app.py` alebo cez `uvicorn app:app`
    (čo robí Docker). Predtým bg loop bežal len v `if __name__ == "__main__"` —
    pod uvicornom sa vôbec nespustil.

    Premenné prostredia:
      LIVESIM_BG=0      → vypne livesim background loop
      LIVESIM_BG_SEC=60 → interval livesim ticku v sekundách
      NO_BACKFILL=1     → vypne backfill (jednorázový aj hodinový)
      SCHEDULER=0       → vypne APScheduler (denné fetche + auto D-1 plán)
    """
    # guard proti duplicitnému štartu (napr. pri uvicorn --reload)
    if _bg_runtime["started"]:
        print("[lifespan] background runtime už beží — preskakujem štart")
        yield
        return
    _bg_runtime["started"] = True

    stop_event = _thr.Event()
    _bg_runtime["stop_event"] = stop_event

    # 1) Štartový backfill (raz, v threadu — nech nezdržuje boot)
    if os.environ.get("NO_BACKFILL") != "1":
        t = _thr.Thread(target=_bg_startup_backfill, daemon=True, name="startup-backfill")
        t.start()
        _bg_runtime["startup_backfill_thread"] = t

    # 2) Livesim continuous bg loop
    if os.environ.get("LIVESIM_BG") != "0":
        t = _thr.Thread(target=_bg_livesim_loop, args=(stop_event,),
                        daemon=True, name="livesim-bg")
        t.start()
        _bg_runtime["livesim_thread"] = t
        print("[livesim-bg] beží na pozadí (vypneš: LIVESIM_BG=0)")

    # 3) APScheduler — denné fetche (PVF/DAM/imbalance) + auto D-1 plán
    if os.environ.get("SCHEDULER") != "0":
        try:
            import scheduler as sch
            sched = sch.start()
            _bg_runtime["scheduler"] = sched
            try:
                n_jobs = len(sched.get_jobs())
            except Exception:
                n_jobs = "?"
            print(f"[scheduler] beží — {n_jobs} jobov (vypneš: SCHEDULER=0)")
        except ImportError as e:
            print(f"[scheduler] modul/dependency nedostupné ({e}) — `pip install apscheduler` ak chceš denné fetche")
        except Exception as e:
            print(f"[scheduler] štart zlyhal: {e}")

    try:
        yield                                                # ←─── app beží
    finally:
        # ─── SHUTDOWN (SIGTERM, Ctrl-C, reload) ───
        print("[lifespan] shutdown: zastavujem background úlohy...")
        stop_event.set()                                      # signál livesim loopu

        sched = _bg_runtime.get("scheduler")
        if sched is not None:
            try:
                sched.shutdown(wait=False)
                print("[scheduler] zastavený")
            except Exception as e:
                print(f"[scheduler] chyba pri shutdown: {e}")

        t = _bg_runtime.get("livesim_thread")
        if t is not None:
            t.join(timeout=5)
            print("[livesim-bg] " + ("zastavený" if not t.is_alive() else "thread sa nestihol zastaviť (5s timeout)"))


from core.state import APP_NAME as _APP_NAME
app = FastAPI(title=_APP_NAME, lifespan=lifespan)
os.makedirs("out", exist_ok=True)

# Static files (CSS, JS, obrázky) — Fáza 3 Jinja2 refactor
try:
    from fastapi.staticfiles import StaticFiles
    if os.path.isdir("static"):
        app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception as _e_static:
    print(f"[static] mount zlyhal: {_e_static}")

# ── Auth (Fáza 2) — opt-in cez AUTH_REQUIRED env flag ────────────────────────
# AUTH_REQUIRED=0 (default) → middleware aj routes sú no-op, žiadna zmena správania.
# AUTH_REQUIRED=1 → session cookie validácia + /login redirect pre neautorizovaných.
try:
    from auth.routes import register_auth_routes, AuthMiddleware
    from auth.policy import PolicyMiddleware
    from auth.audit import AuditLogMiddleware
    register_auth_routes(app)
    # Pozor: middlewares aplikujú sa v reverznom poradí.
    # Add Audit prvé → exec NAPOSLEDY (zachytí status_code aj user z policy).
    # Policy ďalšie → check rolí
    # Auth posledné → set request.state.user (exec PRVÝ)
    app.add_middleware(AuditLogMiddleware)
    app.add_middleware(PolicyMiddleware)
    app.add_middleware(AuthMiddleware)
except Exception as _e_auth:
    print(f"[auth] init zlyhal — beh bez auth: {_e_auth}")


def _handle_mult_action(date_iso: str, step_min: int, mult_arr, mult_action: str,
                          rt_arr_indices=None):
    """Vykoná akciu nad násobiteľmi/RT-maskou a vráti (info_msg, effective_mult, effective_rt_mask).
    Šablóny sú ODDELENÉ podľa kindu — step=60 → kind='plan', step=15 → kind='dentrh'."""
    expected_n = 96 if int(step_min) == 15 else 24
    kind = "dentrh" if int(step_min) == 15 else "plan"
    if po is None:
        return ("plan_overrides modul nie je dostupný", np.ones(expected_n), np.ones(expected_n))
    # postaviť rt_arr z indexov (default 1 = RT povolené; index v rt_arr_indices = povolené)
    rt_arr_step = None
    if rt_arr_indices is not None:
        idxs = {int(i) for i in rt_arr_indices if str(i).isdigit() or (isinstance(i, int))}
        rt_arr_step = np.array([1.0 if i in idxs else 0.0 for i in range(expected_n)], dtype=float)
    msg = ""
    try:
        if mult_action in ("save_day", "save_both") and mult_arr and len(mult_arr) == expected_n:
            m96 = po.broadcast_to_96(np.asarray(mult_arr, dtype=float), step_min)
            rt96 = po.broadcast_to_96(rt_arr_step, step_min) if rt_arr_step is not None else None
            po.save_day(date_iso, m96, kind=kind)
            if rt96 is not None:
                po.save_day_rt(date_iso, rt96, kind=kind)
            if mult_action == "save_both":
                po.save_template(m96, kind=kind)
                if rt96 is not None:
                    po.save_template_rt(rt96, kind=kind)
                msg = (f"✓ Uložené pre <b>{date_iso}</b> AJ ako <b>globálna šablóna</b> ({kind}). "
                       f"Šablóna teraz platí pre každý ďalší deň, kde nebude vlastný per-day prepis.")
            else:
                msg = (f"✓ Uložené pre <b>{date_iso}</b> ako <b>per-day</b> prepis ({kind}). "
                       f"<i>Platí len pre tento dátum.</i> Pre globálne pravidlo použi tlačidlo „Uložiť ako šablónu\".")
        elif mult_action == "save_template" and mult_arr and len(mult_arr) == expected_n:
            m96 = po.broadcast_to_96(np.asarray(mult_arr, dtype=float), step_min)
            rt96 = po.broadcast_to_96(rt_arr_step, step_min) if rt_arr_step is not None else None
            po.save_template(m96, kind=kind)
            if rt96 is not None:
                po.save_template_rt(rt96, kind=kind)
            # spočítaj existujúce per-day prepisy ktoré budú prepisovať šablónu
            import glob
            # per-day suffix podľa kindu (legacy '' pre plan, '_dentrh' pre dentrh)
            _suf = "_dentrh" if kind == "dentrh" else ""
            per_day_files = sorted(glob.glob(os.path.join(po.DIR, f"[0-9]*-[0-9]*-[0-9]*{_suf}.json")))
            conflict_days = []
            for pf in per_day_files:
                try:
                    base = os.path.splitext(os.path.basename(pf))[0]
                    d_iso = base[:-len(_suf)] if _suf and base.endswith(_suf) else base
                    pday = po.load_day(d_iso, kind=kind)
                    if any(np.isfinite(v) for v in pday):
                        conflict_days.append(d_iso)
                except Exception:
                    pass
            warn_html = ""
            if conflict_days:
                _list = ", ".join(conflict_days[:8]) + (f" … (+{len(conflict_days)-8})" if len(conflict_days) > 8 else "")
                warn_html = (f"<br><b>⚠ Pozor:</b> existuje <b>{len(conflict_days)}</b> per-day prepisov ktoré "
                             f"<b>PREPISUJÚ šablónu</b> pre dni: {_list}.<br>"
                             f"Pre tieto dni sa šablóna NEPOUŽIJE. "
                             f"Klikni nižšie 'Vyčistiť per-day' pre vymazanie všetkých per-day prepisov "
                             f"(šablóna potom platí univerzálne).")
            msg = (f"✓ Uložené ako <b>globálna šablóna</b> ({kind}). Platí pre každý generovaný plán — vrátane "
                   f"budúcich dní bez vlastného per-day prepisu. "
                   f"<i>(Pre tento konkrétny deň <b>{date_iso}</b> sa neuloží osobitný prepis — len šablóna.)</i>"
                   f"{warn_html}")
        elif mult_action == "clear_day":
            po.clear_day(date_iso, kind=kind)
            msg = f"✓ Per-day prepis pre <b>{date_iso}</b> ({kind}) vymazaný (padá späť na globálnu šablónu)."
        elif mult_action == "clear_template":
            po.save_template(np.full(po.N96, np.nan), kind=kind)
            po.save_template_rt(np.full(po.N96, np.nan), kind=kind)
            msg = f"✓ Globálna šablóna ({kind}) vymazaná. Všetky dni bez per-day prepisu teraz padajú na default."
        elif mult_action == "clear_all_per_day":
            import glob
            _suf = "_dentrh" if kind == "dentrh" else ""
            per_day_files = sorted(glob.glob(os.path.join(po.DIR, f"[0-9]*-[0-9]*-[0-9]*{_suf}.json")))
            cleared = []
            for pf in per_day_files:
                try:
                    base = os.path.splitext(os.path.basename(pf))[0]
                    d_iso = base[:-len(_suf)] if _suf and base.endswith(_suf) else base
                    po.clear_day(d_iso, kind=kind)
                    cleared.append(d_iso)
                except Exception:
                    pass
            if cleared:
                _list = ", ".join(cleared[:8]) + (f" … (+{len(cleared)-8})" if len(cleared) > 8 else "")
                msg = (f"✓ Vymazaných <b>{len(cleared)}</b> per-day prepisov: {_list}. "
                       f"Teraz pre všetky dni platí globálna šablóna. "
                       f"<b>Existujúce plány v plan_store ostávajú nezmenené</b> — pre ich aplikáciu "
                       f"re-generuj cez batch.")
            else:
                msg = "(Žiadne per-day prepisy na vyčistenie — šablóna už platí univerzálne.)"
        elif mult_action:
            msg = f"⚠ Neznáma akcia '{mult_action}' alebo zlý počet hodnôt ({len(mult_arr) if mult_arr else 0}/{expected_n})."
    except (ValueError, TypeError, OSError) as e:
        msg = f"⚠ Chyba pri uložení: {e}"
    return (msg,
             po.effective_for_step(date_iso, step_min, kind=kind),
             po.effective_rt_for_step(date_iso, step_min, kind=kind))


def _overrides_status(date_iso: str = "", kind: str = "plan") -> str:
    """Indikátor stavu uložených overridov (mult + RT mask) — z disku, nezávisle od posledného save.
    Ak date_iso je prázdny string, ukáže len stav šablóny (form_page kontext).
    kind='plan' (default, 60-min /plan) | 'dentrh' (15-min /dentrh) — šablóny sú oddelené."""
    if po is None:
        return ""
    try:
        tmpl_mult = po.load_template(kind)
        tmpl_rt = po.load_template_rt(kind)
        tmpl_act = int(np.sum(np.isfinite(tmpl_mult) & (np.abs(tmpl_mult - 1.0) > 1e-6)))
        tmpl_rt_off = int(np.sum(np.isfinite(tmpl_rt) & (tmpl_rt < 0.5)))
        if not date_iso:                                       # form_page: len šablóna
            if tmpl_act == 0 and tmpl_rt_off == 0:
                return ""
            return (f"<div style='background:#eef5e0;border-left:4px solid #2E7D32;padding:6px 10px;"
                    f"margin:6px 0 12px;font-size:13px;border-radius:4px'>"
                    f"📋 <b>Globálna šablóna overridov ({kind}) je aktívna:</b> "
                    f"{tmpl_act} slotov s násobiteľom ≠ 1.00"
                    + (f", <b>{tmpl_rt_off}</b> 15-min slotov má RT zablokované" if tmpl_rt_off else "")
                    + f". <span style='color:#666;font-size:12px'>(Použije sa na každý generovaný plán; per-day prepis sa nastavuje na výslednej stránke plánu.)</span></div>")
        day_mult = po.load_day(date_iso, kind)
        day_rt = po.load_day_rt(date_iso, kind)
        day_act = int(np.sum(np.isfinite(day_mult) & (np.abs(day_mult - 1.0) > 1e-6)))
        day_rt_off = int(np.sum(np.isfinite(day_rt) & (day_rt < 0.5)))
        eff_mult = po.load_effective(date_iso, kind)
        eff_rt = po.load_effective_rt(date_iso, kind)
        eff_act = int(np.sum(np.abs(eff_mult - 1.0) > 1e-6))
        eff_rt_off = int(np.sum(eff_rt < 0.5))
        if eff_act == 0 and eff_rt_off == 0:
            return (f"<div style='background:#f5f5f5;border-left:4px solid #999;padding:6px 10px;"
                    f"margin:6px 0;font-size:12px;color:#666;border-radius:4px'>"
                    f"📋 Pre {date_iso}: žiadne aktívne overridy (mult=1.00, RT povolená).</div>")
        parts = [f"<b>{eff_act}</b> 15-min slotov s násobiteľom ≠ 1.00"]
        if eff_rt_off > 0:
            parts.append(f"<b>{eff_rt_off}</b> 15-min slotov má RT zablokované")
        src = []
        if day_act or day_rt_off:
            src.append(f"per-day pre {date_iso}: {day_act} mult + {day_rt_off} RT")
        if tmpl_act or tmpl_rt_off:
            src.append(f"šablóna: {tmpl_act} mult + {tmpl_rt_off} RT")
        return (f"<div style='background:#eef5e0;border-left:4px solid #2E7D32;padding:6px 10px;"
                f"margin:6px 0;font-size:13px;border-radius:4px'>"
                f"📋 <b>Aktívne overridy (z disku):</b> {' &nbsp;•&nbsp; '.join(parts)}."
                f"<br><span style='color:#666;font-size:12px'>Zdroj: {' &nbsp;|&nbsp; '.join(src)}</span></div>")
    except Exception as e:
        return f"<div style='color:#999;font-size:12px'>(stav overridov nedostupný: {e})</div>"


def _carried_soc_banner(date: str, soc_init_pct: float, case: str = "plan_d1") -> str:
    """Banner ktorý informuje že livesim používa CARRIED SOC, nie soc_init z formulára.
    Ukáže rozdiel a navrhne hodnotu pre 1:1 porovnanie."""
    try:
        info = lsim.carried_soc_for_date(case, port=_PORT, date=date)
    except Exception:
        return ""
    if not info:
        return ""
    carried_pct = float(info["soc_pct"])
    diff = abs(carried_pct - float(soc_init_pct))
    if diff < 0.5:
        return (f"<div style='background:#e8f5e9;border-left:4px solid #2E7D32;padding:6px 10px;"
                f"margin:6px 0;font-size:13px;border-radius:4px'>"
                f"🔋 <b>SOC začiatok dňa ({date}) sedí s livesim:</b> {soc_init_pct:.1f} % vo formulári, "
                f"livesim carried {carried_pct:.1f} % (z {info['as_of_date']}). Plán sa bude zhodovať s livesim.</div>")
    return (f"<div style='background:#fff3cd;border-left:4px solid #f0b80f;padding:8px 12px;"
            f"margin:6px 0;font-size:13px;border-radius:4px;color:#7a5c00'>"
            f"⚠ <b>SOC začiatok sa LÍŠI od livesim:</b> formulár má <b>{soc_init_pct:.1f} %</b>, "
            f"livesim by použil <b>{carried_pct:.1f} %</b> (z {info['as_of_date']}). "
            f"Plán pre tento deň sa preto bude líšiť od toho čo uvidíš v živej simulácii — "
            f"optimizer s rôznym SOC robí iné rozhodnutia. "
            f"<br><i>Pre 1:1 porovnanie s livesim: nastav <b>SOC začiatok = {carried_pct:.1f}</b> a klikni Generovať.</i></div>")


def _stale_plans_banner(case: str = "plan_d1") -> str:
    """Bug SOC-CONT-V3 (2026-06-11): warning na /plan keď existujúce LP plány pre budúce
    dni majú soc_init nekonzistentný s meta.soc_after_done (> 1 %). Normálne ich auto-regen
    opraví sám pri najbližšom livesim ticku — banner zachytí stav medzi tým / pri zlyhaní."""
    try:
        found = _find_stale_future_plans(case)
    except Exception:
        return ""
    if not found or not found["stale"]:
        return ""
    items = " &nbsp;•&nbsp; ".join(
        f"<b>{d}</b>: plán soc_init <b>{s0:.1f} %</b>" for d, s0 in found["stale"])
    return (f"<div style='background:#fff3cd;border-left:4px solid #f0b80f;padding:8px 12px;"
            f"margin:6px 0;font-size:13px;border-radius:4px;color:#7a5c00'>"
            f"⚠ <b>Zastarané LP plány</b> (livesim po {found['done_through']} skončil na "
            f"<b>{found['carried_pct']:.1f} %</b>): {items}. "
            f"Auto-regen ich opraví pri najbližšom livesim ticku; ručne cez "
            f"<a href='/plan_batch'>📦 Batch</a>.</div>")


def _vdt_trade_stats(profile: str, day: str = None) -> dict:
    """Bug VDT-EFEKTIVITA (2026-06-11): agregát VDT paper trades pre UI karty —
    objemy + vážené priemerné ceny nákup/predaj + hrubý cash (predaj − nákup).
    day=None → celá história profilu; day='YYYY-MM-DD' → len ten deň."""
    out = dict(n=0, buy_kwh=0.0, buy_avg=0.0, sell_kwh=0.0, sell_avg=0.0,
               cash_eur=0.0)
    try:
        import vdt_live_advisor as _vla
        p = _vla.paper_trades_csv_path(profile)
        if not p or not os.path.exists(p):
            return out
        import csv as _csv
        b_pw = b_w = s_pw = s_w = 0.0
        with open(p, encoding="utf-8", newline="") as f:
            for row in _csv.DictReader(f):
                if str(row.get("profile") or "") != profile:
                    continue
                if day and str(row.get("ts", ""))[:10] != str(day)[:10]:
                    continue
                act = str(row.get("action", "")).upper()
                if act not in ("BUY", "CHARGE", "SELL", "DISCHARGE"):
                    continue
                try:
                    kwh = abs(float(row.get("kwh") or 0))
                    pr_t = float(row.get("price_predicted_eur") or 0)
                except (TypeError, ValueError):
                    continue
                if kwh <= 0:
                    continue
                out["n"] += 1
                if act in ("BUY", "CHARGE"):
                    b_w += kwh; b_pw += kwh * pr_t
                else:
                    s_w += kwh; s_pw += kwh * pr_t
        out["buy_kwh"] = b_w; out["sell_kwh"] = s_w
        out["buy_avg"] = (b_pw / b_w) if b_w > 0 else 0.0
        out["sell_avg"] = (s_pw / s_w) if s_w > 0 else 0.0
        out["cash_eur"] = (s_pw - b_pw) / 1000.0
    except Exception as _e:
        print(f"[_vdt_trade_stats] {profile}/{day}: {_e}")
    return out


def _vdt_price_accuracy(profile: str, day: str, dview) -> dict:
    """Bug VDT-EFEKTIVITA: vážená odchýlka EXEKUČNEJ ceny obchodu (reálny orderbook
    bid/ask v momente obchodu) vs finálny OKTE VDT clearing (`vdt_eur` v trace).
    Kladná hodnota = obchodovali sme nad clearingom (predaj výhodne / nákup draho).
    Returns: {n, werr_eur_mwh} — n=0 ak nie sú dáta."""
    out = dict(n=0, werr=None)
    try:
        if dview is None or "vdt_eur" not in getattr(dview, "columns", []):
            return out
        _t = pd.to_datetime(dview["time"], errors="coerce")
        _cl = pd.to_numeric(dview["vdt_eur"], errors="coerce")
        _cmap = {}
        for _ts, _v in zip(_t, _cl):
            if _v == _v and _ts == _ts:
                _cmap.setdefault(int((_ts.hour * 60 + _ts.minute) // 15), []).append(float(_v))
        _cavg = {k: sum(v) / len(v) for k, v in _cmap.items()}
        if not _cavg:
            return out
        import vdt_live_advisor as _vla
        import csv as _csv
        p = _vla.paper_trades_csv_path(profile)
        if not p or not os.path.exists(p):
            return out
        sw = se = 0.0
        n = 0
        with open(p, encoding="utf-8", newline="") as f:
            for row in _csv.DictReader(f):
                if str(row.get("profile") or "") != profile:
                    continue
                if str(row.get("ts", ""))[:10] != str(day)[:10]:
                    continue
                if str(row.get("action", "")).upper() not in ("BUY", "CHARGE", "SELL", "DISCHARGE"):
                    continue
                try:
                    kwh = abs(float(row.get("kwh") or 0))
                    pr_t = float(row.get("price_predicted_eur") or 0)
                    _sl = str(row.get("slot", ""))
                    idx = (int(_sl[:2]) * 60 + int(_sl[3:5])) // 15
                except (TypeError, ValueError):
                    continue
                if kwh <= 0 or idx not in _cavg:
                    continue
                se += (pr_t - _cavg[idx]) * kwh
                sw += kwh
                n += 1
        if sw > 0:
            out = dict(n=n, werr=se / sw)
    except Exception as _e:
        print(f"[_vdt_price_accuracy] {profile}/{day}: {_e}")
    return out


def _rt_eff_stats(df) -> dict:
    """RT efektivita (bod 2 RT v2 plánu): ex-post vyhodnotenie zásahov z trace.
    hit = smer zásahu sa zhodol so znamienkom realizovaného spreadu (zco − dt)."""
    out = dict(n=0, hit_pct=None, rev_eur=0.0, avg_spread=None, v2_share=None)
    try:
        if df is None or "rt_dir" not in getattr(df, "columns", []):
            return out
        _dir = pd.to_numeric(df["rt_dir"], errors="coerce").fillna(0)
        _act = _dir != 0
        n = int(_act.sum())
        out["n"] = n
        try:
            out["rev_eur"] = float(pd.to_numeric(
                df.get("rt_rev_realistic_min"), errors="coerce").fillna(0).sum())
        except Exception:
            pass
        if n == 0:
            return out
        _zco = pd.to_numeric(df.get("zco_eur"), errors="coerce")
        _dt = pd.to_numeric(df.get("dt_real_eur", df.get("dt_eur")), errors="coerce")
        _spread = (_zco - _dt)
        _ok = _act & _spread.notna()
        if int(_ok.sum()) > 0:
            _d_ok = _dir[_ok]
            _s_ok = _spread[_ok]
            out["hit_pct"] = float(((_d_ok * _s_ok) > 0).mean() * 100.0)
            out["avg_spread"] = float((_d_ok * _s_ok).mean())   # + = zásahy v smere spreadu
        if "rt_reason" in df.columns:
            _rs = df.loc[_act, "rt_reason"].astype(str)
            out["v2_share"] = float(_rs.str.startswith("v2:").mean() * 100.0)
            # RT-EFF-V3-LABEL: dominantný engine z reasons (v3 karta ukazovala "v1")
            _eng = _rs.str.extract(r"^(v\d)")[0]
            out["engine"] = (_eng.mode().iloc[0]
                             if _eng.notna().any() else "v1")
    except Exception as _e:
        print(f"[_rt_eff_stats] {_e}")
    return out


def _vdt_eff_decorate(agg_df):
    """Bug VDT-EFEKTIVITA: doplní do per-deň agregátu stĺpce VDT nákup/predaj
    (kWh + vážená cena) z paper trades. No-op ak chýba stĺpec 'date'."""
    if agg_df is None or "date" not in getattr(agg_df, "columns", []):
        return agg_df
    try:
        _prof_eff = pr.get_active() if pr is not None else None
        if not _prof_eff:
            return agg_df
        _stats = [_vdt_trade_stats(_prof_eff, day=str(_dy)) for _dy in agg_df["date"]]
        agg_df["vdt_buy_kwh"] = [s["buy_kwh"] for s in _stats]
        agg_df["vdt_buy_avg"] = [s["buy_avg"] for s in _stats]
        agg_df["vdt_sell_kwh"] = [s["sell_kwh"] for s in _stats]
        agg_df["vdt_sell_avg"] = [s["sell_avg"] for s in _stats]
    except Exception as _e:
        print(f"[VDT-EFEKTIVITA agg] {_e}")
    return agg_df


def _resolve_soc_init_carryover(date_iso: str, fp: dict,
                                   case: str = "plan_d1") -> tuple:
    """Bug #622 + SOC-CONT: SOC kontinuita cez dni.

    Užívateľ: "soc nemoze kazdy den zacat od nuli alebo nastavenej hodnoty
    ale pokracovat. delenie na dni je len logicka vec".

    Priorita zdrojov SOC pre začiatok dňa N:
      1. **plan_store**: posledný `soc_pct` plánu pre deň N-1 (deterministický
         LP výsledok z D-1 plánu). Použije sa AJ keď livesim ešte nezbehol.
      2. **livesim trace**: `lsim.carried_soc_for_date` — koniec realizácie
         dňa N-1 po RT zásahoch. Použije sa ak je plan_store fallback.
      3. **manual** (`fp["soc_init"]`): IBA pre prvý deň simulácie, alebo
         keď nič iné nie je k dispozícii (žiadny plán + žiadne livesim CSV).

    Returns:
        (soc_pct, source) — source ∈ {"plan_store", "carried", "manual_fallback",
                                         "manual_clamped"}
    """
    manual_soc = float(fp.get("soc_init", DEF["soc_init"]))
    soc_min = float(fp.get("soc_min", DEF["soc_min"]))
    soc_max = float(fp.get("soc_max", DEF["soc_max"]))
    # Bug SOC-CONT-V2 (2026-06-10): poradie zmenené na carried → plan_store → manual.
    # Predtým bolo plan_store prvé, ale ten vracia LP-nominovaný terminal_soc
    # (typicky 50%), nie reálny SOC po RT zásahoch včera. Tým každý deň začínal
    # od 50% napriek tomu že realita včera skončila inde.
    # Carried (livesim CSV koniec dňa N-1) = pravdivá história po RT.
    # Plan_store fallback len ak livesim ešte nezbehol pre N-1 (= prvý deň).
    # Krok 1: livesim trace carried — REÁLNY koniec dňa N-1
    try:
        info = lsim.carried_soc_for_date(case, port=_PORT, date=date_iso)
    except Exception:
        info = None
    if info:
        try:
            carried_pct = float(info["soc_pct"])
            if soc_min <= carried_pct <= soc_max:
                print(f"[SOC-CONT-V2] {date_iso}: carried={carried_pct:.1f}% "
                      f"({info.get('note','')})")
                return (carried_pct, "carried")
            else:
                print(f"[SOC-CONT-V2] {date_iso}: carried={carried_pct:.1f}% mimo "
                      f"[{soc_min:.0f}, {soc_max:.0f}], skúsim plan_store fallback")
        except (KeyError, TypeError, ValueError):
            pass
    # Krok 2: plan_store fallback — posledný soc_pct plánu pre deň N-1
    try:
        import plan_store as _ps_carry
        import datetime as _dt
        _prev_day = (_dt.date.fromisoformat(date_iso) - _dt.timedelta(days=1)).isoformat()
        _step_min = 60 if case == "plan_d1" else 15
        _kind = "plan" if case == "plan_d1" else "dentrh"
        _prev_plan = _ps_carry.load_plan_safe(_prev_day, _step_min, kind=_kind)
        if _prev_plan and isinstance(_prev_plan, dict):
            _slots = _prev_plan.get("slots") or _prev_plan.get("plan") or []
            if _slots and isinstance(_slots, list):
                _last = _slots[-1]
                if isinstance(_last, dict) and "soc_pct" in _last:
                    _last_soc = float(_last["soc_pct"])
                    if soc_min <= _last_soc <= soc_max:
                        print(f"[SOC-CONT-V2] {date_iso}: plan_store fallback "
                              f"slots[-1].soc_pct={_last_soc:.1f}%")
                        return (_last_soc, "plan_store")
    except Exception as _e_ps:
        print(f"[SOC-CONT-V2 plan_store] {date_iso}: {_e_ps}")
    # Krok 3: manual fallback (prvý deň, žiadny livesim CSV, žiadny D-1 plán)
    print(f"[SOC-CONT-V2] {date_iso}: manual fallback {manual_soc:.1f}%")
    return (manual_soc, "manual_fallback")


def _resolve_terminal_soc(date_iso: str, fp: dict, price_arr_today) -> float:
    """Bug TERMINAL-SOC-MODE (2026-06-11): terminál dňa podľa zajtrajších cien.

    mode="fixed" (default) → fp["terminal_soc"] ako doteraz.
    mode="next_day_price"  → ak zajtrajšie ráno (06-10 h) je drahšie než dnešný
    večer (17-22 h) + breakeven nákladov round-tripu, oplatí sa energiu PODRŽAŤ
    cez polnoc → terminál = vysoký (min(soc_max, 90)). Inak fixný terminál.
    Zajtrajšie ceny: reálny DAM (market.fetch_dam_prices) ak je publikovaný,
    inak None → fixed. Generické pre ľubovoľný profil (všetko z fp)."""
    mode = str(fp.get("terminal_soc_mode") or "fixed")
    # #30 (user 2026-06-18): koniec dňa sa NEVYNUCUJE — „proste ako to vyjde z ekonomiky".
    # Bug TBB-TERMINAL-INFEASIBLE (2026-06-18): base_term = soc_min bolo ZLE — keď štart SOC
    # < soc_min (nesené nízke SOC), joint_lp si per-slot podlahu auto-zníži na štart, ALE
    # terminál ≥ soc_min vynútil dobiť späť → pri load/grid obmedzeniach LP infeasible
    # (TBB 171/171). Preto base_term = 0: terminál nezáväzný, koniec drží LEN fyzická per-slot
    # podlaha (soc_min, auto-adjust) → SOC smie skončiť kdekoľvek ≥ podlaha podľa ekonomiky,
    # bez núteného recharge. Vyššie ho dvihne LEN ekonomika cez next_day_price.
    base_term = 0.0
    if mode != "next_day_price":
        return base_term
    try:
        import market as _mk_t
        _d_next = dt.date.fromisoformat(date_iso) + dt.timedelta(days=1)
        _df_n = _mk_t.fetch_dam_prices(_d_next)
        if _df_n is None or _df_n.empty:
            return base_term
        _pn = pd.to_numeric(_df_n["cena_EUR"], errors="coerce").dropna().values
        _hours_n = np.array([int(str(iv)[:2]) for iv in _df_n["interval"].astype(str)])[:len(_pn)]
        _morning_next = float(np.mean(_pn[(_hours_n >= 6) & (_hours_n < 10)]))
        _p_today = np.asarray(price_arr_today, float)
        _evening_today = float(np.mean(_p_today[17:23])) if len(_p_today) >= 23 else float(np.mean(_p_today))
        _eff_rt = float(fp.get("eff_c", DEF["eff_c"])) * float(fp.get("eff_d", DEF["eff_d"]))
        _breakeven = (_evening_today * (1.0 / max(_eff_rt, 0.5) - 1.0)
                      + float(fp.get("cycle_cost", DEF["cycle_cost"]))
                      + 2.0 * float(fp.get("grid_fee", DEF["grid_fee"])))
        if _morning_next > _evening_today + _breakeven:
            _term_hi = min(float(fp.get("soc_max", DEF["soc_max"])), 90.0)
            print(f"[TERMINAL-SOC-MODE] {date_iso}: zajtra ráno {_morning_next:.1f} > "
                  f"dnes večer {_evening_today:.1f} + breakeven {_breakeven:.1f} "
                  f"→ terminál {_term_hi:.0f}% (podrž energiu)")
            return _term_hi
        print(f"[TERMINAL-SOC-MODE] {date_iso}: zajtra ráno {_morning_next:.1f} ≤ "
              f"večer {_evening_today:.1f} + breakeven {_breakeven:.1f} → fixed {base_term:.0f}%")
        return base_term
    except Exception as _e_t:
        print(f"[TERMINAL-SOC-MODE] {date_iso}: forecast nedostupný ({_e_t}) → fixed")
        return base_term


def _gen_one_plan(date_iso: str, step_min: int, kind: str, fp: dict) -> str:
    """Internal helper pre /plan a /plan_batch. Spustí kompletnú generáciu pre jeden deň + uloží plán.
    Vracia cestu k uloženému súboru. Pri chybe hodí výnimku."""
    if ps is None:
        raise RuntimeError("plan_store modul nie je dostupný")
    d = dt.date.fromisoformat(date_iso)
    # Bug #641 diag: vstup _gen_one_plan — vidíme či sa volá a aké parametre dostane
    print(f"[_gen_one_plan] day={date_iso} step={step_min} kind={kind} "
          f"batt_kw={fp.get('batt_kw')} batt_kwh={fp.get('batt_kwh')} "
          f"soc_init={fp.get('soc_init')} soc_min={fp.get('soc_min')} "
          f"soc_reserve_pct={fp.get('soc_reserve_pct', 0.0)} "
          f"grid_im={fp.get('grid_kw_import') or fp.get('grid_kw')} "
          f"grid_ex={fp.get('grid_kw_export') or fp.get('grid_kw')} "
          f"max_dam_im={fp.get('max_import_kwh_day')} "
          f"max_dam_ex={fp.get('max_export_kwh_day')} "
          f"joint_lp={(fp.get('joint_lp') or {}).get('enabled', False)}")
    if int(step_min) == 60 and kind == "plan":
        # Ak profil nemá FTV (kwp=0), netreba volať PVF — pv_arr = 0 array.
        # Cena sa berie zo ISOT predikcie nezávisle od počasia (model nemá GTI keď nie je PV).
        _kwp = float(fp.get("kwp", DEF["kwp"]))
        _has_pv = _kwp > 0.01
        if _has_pv:
            wx = _fetch_pv_cached(float(fp.get("lat", DEF["lat"])), float(fp.get("lon", DEF["lon"])),
                                        _kwp, float(fp.get("tilt", DEF["tilt"])),
                                        float(fp.get("azimuth", DEF["azimuth"])), float(fp.get("eff", DEF["eff"])),
                                        start=d, end=d)
            wx["time"] = pd.to_datetime(wx["time"]); wx = wx[wx.time.dt.date == d].copy()
            if wx.empty:
                raise RuntimeError(f"PV forecast nedostupný pre {d}")
        else:
            # No-FTV profil: vytvor prázdny wx grid s 24 hodinami (00..23) pre daný dátum
            wx = pd.DataFrame({
                "time": pd.date_range(pd.Timestamp(d), periods=24, freq="h"),
                "kw": np.zeros(24), "gti": np.zeros(24),
                "temp": np.full(24, 15.0), "cloud": np.full(24, 50.0),
            })
        hist = _isot_history(d, days=8)
        wx2 = wx[["time", "gti", "temp", "cloud"]].copy(); wx2["isot_eur"] = np.nan
        h2 = hist.copy()
        for c in ["gti", "temp", "cloud"]:
            h2[c] = np.nan
        ctx = pd.concat([h2[["time", "isot_eur", "gti", "temp", "cloud"]], wx2], ignore_index=True)
        pred = _model().predict(ctx)
        dayp = pred[pred.time.dt.date == d][["time", "pred_isot", "p_neg"]]
        day = wx.merge(dayp, on="time").sort_values("time")
        if len(day) < 24:
            raise RuntimeError(f"predikcia neúplná pre {d}")
        cal = _cal_for(d)
        pv_arr = day.kw.values * cal * float(fp.get("pv_scale", 1.0))
        price_arr = day.pred_isot.values * float(fp.get("price_scale", 1.0))
        zbw = float(fp.get("zco_bias_w", 0.0))
        decision_price = lsim._apply_zco_bias(price_arr, d, float(pv_arr.sum()), 60, zbw)
        npd = bool(fp.get("no_planned_discharge", False))
        rtf = bool(fp.get("rt_freedom", True))
        # šablóna mults + rt_mask z plan_overrides (per-day prepíše šablónu)
        mult24 = po.effective_for_step(date_iso, 60) if po is not None else np.ones(24)
        rt_mask24 = po.effective_rt_for_step(date_iso, 60) if po is not None else np.ones(24)
        # ── LOAD (predikovaná spotreba zákazníka) z naimportovaného profilu pre tento dátum ──
        load24 = None
        if lp is not None and lp.has_data():
            try:
                _l96_kw = lp.load_for_date(date_iso)                                # 96 × kW
                load24 = _l96_kw.reshape(24, 4).mean(axis=1)                        # 24 × kWh/hod (priemerné kW)
            except Exception:
                load24 = None
        _mex = float(fp.get("max_export_kwh_day", 0) or 0)
        _mim = float(fp.get("max_import_kwh_day", 0) or 0)
        # Joint LP flags z aktívneho profilu (parita s /plan handlerom — fix bug B).
        # Bez tohto by batch volal čistý optimize_day a výsledky by sa líšili od single /plan.
        from joint_lp_integration import (optimize_day_or_joint as _od_or_joint_batch,
                                          get_flags_from_profile as _gjlp_batch)
        try:
            import plan_store as _ps_jlb
            _jb_prof = _ps_jlb.resolve_profile() or "default"
        except Exception:
            _jb_prof = "default"
        _joint_flags_b = _gjlp_batch(_jb_prof if _jb_prof != "default" else None)
        # Bug #622: SOC carryover pre 60-min plan (Krok A)
        _soc_init_use, _soc_init_src = _resolve_soc_init_carryover(date_iso, fp, case="plan_d1")
        print(f"[#622 _gen_one_plan 60min] {date_iso}: soc_init={_soc_init_use:.1f}% "
              f"({_soc_init_src})")
        # Bug LP-VDT-BOUNDS: uzavreté VDT obchody dňa = smerové stropy pre LP
        from joint_lp_integration import vdt_committed_kw_for_day as _vdtb
        _vdt_committed = _vdtb(_jb_prof, date_iso, T=24, step_min=60)
        # Bug VDT-CAP-RESERVE-HIST (2026-06-12, user): rezerva pre intraday platí LEN
        # pre dnešok/budúcnosť — pri spätnom prepočte minulých dní žiadne VDT obchody
        # nevzniknú a rezerva by históriu len hendikepovala (skreslenie efektu).
        _vcr_eff = (float(fp.get("vdt_capacity_reserve_kw", 0) or 0)
                    if d >= dt.date.today() else 0.0)
        sch, summ = _od_or_joint_batch(
            pv_arr, decision_price,
            joint_flags=_joint_flags_b, profile=_jb_prof,
            vdt_committed_kw=_vdt_committed,
            vdt_capacity_reserve_kw=_vcr_eff,
            settle_price=price_arr,
            batt_kw=float(fp.get("batt_kw", DEF["batt_kw"])), batt_kwh=float(fp.get("batt_kwh", DEF["batt_kwh"])),
            eff_c=float(fp.get("eff_c", DEF["eff_c"])), eff_d=float(fp.get("eff_d", DEF["eff_d"])),
            soc_min_pct=float(fp.get("soc_min", DEF["soc_min"])),
            soc_max_pct=float(fp.get("soc_max", DEF["soc_max"])),
            soc_init_pct=_soc_init_use,
            soc_reserve_pct=float(fp.get("soc_reserve_pct", 0.0) or 0.0),
            rt_grid_reserve_pct=float(fp.get("rt_grid_reserve_pct", 0.0) or 0.0),
            terminal_soc_pct=_resolve_terminal_soc(date_iso, fp, price_arr),
            grid_kw=float(fp.get("grid_kw", DEF["grid_kw"])),
            # Bug GRID-LIMIT-BATCH (2026-06-14): batch generátor neposielal grid_kw_import/
            # export → joint_lp spadol na fallback grid_kw → plán importoval nad limit a
            # guard #625-C ho zhodil (CHYBA). Posielame asymetrické limity z profilu
            # (rovnako ako single /plan) → batéria sa korektne obmedzí na dodávku/odber.
            grid_kw_import=(float(fp.get("grid_kw_import")) if fp.get("grid_kw_import") not in (None, "") else None),
            grid_kw_export=(float(fp.get("grid_kw_export")) if fp.get("grid_kw_export") not in (None, "") else None),
            grid_fee=float(fp.get("grid_fee", DEF["grid_fee"])),
            cycle_cost=float(fp.get("cycle_cost", DEF["cycle_cost"])),
            allow_grid_charge=bool(fp.get("allow_grid_charge", True)),
            allow_curtail=bool(fp.get("allow_curtail", DEF["allow_curtail"])),
            min_spread_eur=float(fp.get("min_spread", DEF["min_spread"])),
            min_trade_mwh=float(fp.get("min_trade", DEF["min_trade"])),
            block_neg_import=bool(fp.get("block_neg_import", False)),
            block_planned_discharge=npd,
            batt_kw_override=mult24,
            load_kwh=load24,
            max_export_kwh_day=_mex if _mex > 0 else None,
            max_import_kwh_day=_mim if _mim > 0 else None)
        params = {**fp, "date": date_iso}
        sched_cols = ["batt_kw","grid_kwh","pv_kwh","price_eur","curtail_kwh","soc_pct","order_mwh",
                      "_charge_kw","_discharge_kw","_export_kwh","_import_kwh","soc_kwh","load_kwh"]
        sched = {c: sch[c].tolist() for c in sched_cols if c in sch.columns}
        bk = np.asarray(sch["batt_kw"].values, float)
        # rt_mask: ak rt_freedom=False, RT iba kde plán aktívne pracuje
        rtm = np.asarray(rt_mask24, float)
        if not rtf:
            rtm = np.where(np.abs(bk) > 0.5, rtm, 0.0)
        return ps.save_plan(date_iso, 60, "plan", params=params, schedule=sched, summary=summ,
                            mults=list(map(float, np.asarray(mult24).reshape(-1))),
                            rt_mask=list(map(float, rtm)),
                            block_planned_discharge=npd, zco_bias_w=zbw, rt_freedom=rtf,
                            meta=dict(source="batch", price_kind="predicted"))
    elif int(step_min) == 15 and kind == "dentrh":
        ote = _fetch_ote_cached(d)
        price15 = _dt15_from_ote(ote)
        # Ak profil nemá FTV (kwp=0), netreba volať PVF
        _kwp15 = float(fp.get("kwp", DEF["kwp"]))
        if _kwp15 > 0.01:
            wx = _fetch_pv_cached(float(fp.get("lat", DEF["lat"])), float(fp.get("lon", DEF["lon"])),
                                        _kwp15, float(fp.get("tilt", DEF["tilt"])),
                                        float(fp.get("azimuth", DEF["azimuth"])), float(fp.get("eff", DEF["eff"])),
                                        start=d, end=d)
            wx["time"] = pd.to_datetime(wx["time"]); wx = wx[wx.time.dt.date == d].sort_values("time")
            if wx.empty:
                raise RuntimeError(f"PV forecast nedostupný pre {d}")
            cal = _cal_for(d)
            pv_h = wx.kw.values * cal
            if len(pv_h) < 24:
                pv_h = np.concatenate([pv_h, np.zeros(24 - len(pv_h))])
        else:
            # No-FTV profil: 24 hodín × 0 kW
            pv_h = np.zeros(24)
        pv15 = np.repeat(pv_h[:24], 4) / 4.0
        n = min(len(pv15), len(price15))
        npd = bool(fp.get("no_planned_discharge", False))
        rtf = bool(fp.get("rt_freedom", True))
        # šablóna mults + rt_mask z plan_overrides
        mult96_full = po.effective_for_step(date_iso, 15) if po is not None else np.ones(96)
        rt_mask96_full = po.effective_rt_for_step(date_iso, 15) if po is not None else np.ones(96)
        mult_use = np.asarray(mult96_full, float)[:n]
        rt_use = np.asarray(rt_mask96_full, float)[:n]
        # ── LOAD pre 15-min (96 × kWh za 15min = kW × 0.25) ──
        load96 = None
        if lp is not None and lp.has_data():
            try:
                _l96_kw = lp.load_for_date(date_iso)                                # 96 × kW
                load96 = (_l96_kw * 0.25)[:n]                                       # kWh za 15-min slot
            except Exception:
                load96 = None
        _mex15 = float(fp.get("max_export_kwh_day", 0) or 0)
        _mim15 = float(fp.get("max_import_kwh_day", 0) or 0)
        # Joint LP flags z aktívneho profilu (parita s /dentrh handlerom — fix bug B 15-min)
        from joint_lp_integration import (optimize_day_or_joint as _od_or_joint_batch15,
                                          get_flags_from_profile as _gjlp_batch15)
        try:
            import plan_store as _ps_jlb15
            _jb_prof15 = _ps_jlb15.resolve_profile() or "default"
        except Exception:
            _jb_prof15 = "default"
        _joint_flags_b15 = _gjlp_batch15(_jb_prof15 if _jb_prof15 != "default" else None)
        # Bug #622: SOC carryover pre 15-min dentrh
        _soc_init_use15, _soc_init_src15 = _resolve_soc_init_carryover(date_iso, fp, case="dentrh")
        print(f"[#622 _gen_one_plan 15min] {date_iso}: soc_init={_soc_init_use15:.1f}% "
              f"({_soc_init_src15})")
        # Bug LP-VDT-BOUNDS: uzavreté VDT obchody dňa = smerové stropy pre LP
        from joint_lp_integration import vdt_committed_kw_for_day as _vdtb15
        _vdt_committed15 = _vdtb15(_jb_prof15, date_iso, T=n, step_min=15)
        # Bug VDT-CAP-RESERVE-HIST: rezerva len pre dnešok/budúcnosť (viď 60-min vetva)
        _vcr_eff15 = (float(fp.get("vdt_capacity_reserve_kw", 0) or 0)
                      if d >= dt.date.today() else 0.0)
        sch, summ = _od_or_joint_batch15(pv15[:n], price15[:n], dt=0.25,
                                  joint_flags=_joint_flags_b15, profile=_jb_prof15,
                                  vdt_committed_kw=_vdt_committed15,
                                  vdt_capacity_reserve_kw=_vcr_eff15,
                                  batt_kw=float(fp.get("batt_kw", DEF["batt_kw"])),
                                  batt_kwh=float(fp.get("batt_kwh", DEF["batt_kwh"])),
                                  eff_c=float(fp.get("eff_c", DEF["eff_c"])), eff_d=float(fp.get("eff_d", DEF["eff_d"])),
                                  soc_min_pct=float(fp.get("soc_min", DEF["soc_min"])),
                                  soc_max_pct=float(fp.get("soc_max", DEF["soc_max"])),
                                  soc_init_pct=_soc_init_use15,
                                  soc_reserve_pct=float(fp.get("soc_reserve_pct", 0.0) or 0.0),
                                  rt_grid_reserve_pct=float(fp.get("rt_grid_reserve_pct", 0.0) or 0.0),
                                  terminal_soc_pct=_resolve_terminal_soc(
                                      date_iso, fp,
                                      (np.asarray(price15[:96], float).reshape(24, 4).mean(axis=1)
                                       if n >= 96 else np.asarray(price15[:n], float))),
                                  grid_kw=float(fp.get("grid_kw", DEF["grid_kw"])),
                                  # Bug GRID-LIMIT-BATCH: rovnako pre 15-min batch
                                  grid_kw_import=(float(fp.get("grid_kw_import")) if fp.get("grid_kw_import") not in (None, "") else None),
                                  grid_kw_export=(float(fp.get("grid_kw_export")) if fp.get("grid_kw_export") not in (None, "") else None),
                                  grid_fee=float(fp.get("grid_fee", DEF["grid_fee"])),
                                  cycle_cost=float(fp.get("cycle_cost", DEF["cycle_cost"])),
                                  allow_grid_charge=bool(fp.get("allow_grid_charge", True)),
                                  allow_curtail=bool(fp.get("allow_curtail", True)),
                                  min_spread_eur=float(fp.get("min_spread", DEF["min_spread"])),
                                  block_neg_import=bool(fp.get("block_neg_import", False)),
                                  block_planned_discharge=npd,
                                  batt_kw_override=mult_use,
                                  load_kwh=load96,
                                  max_export_kwh_day=_mex15 if _mex15 > 0 else None,
                                  max_import_kwh_day=_mim15 if _mim15 > 0 else None)
        params = {**fp, "date": date_iso}
        sched_cols = ["batt_kw","grid_kwh","pv_kwh","price_eur","curtail_kwh","soc_pct","order_mwh",
                      "_charge_kw","_discharge_kw","_export_kwh","_import_kwh","soc_kwh","load_kwh"]
        sched = {c: sch[c].tolist() for c in sched_cols if c in sch.columns}
        bk = np.asarray(sch["batt_kw"].values, float)
        rtm = np.asarray(rt_use, float)
        if not rtf:
            rtm = np.where(np.abs(bk) > 0.5, rtm, 0.0)
        return ps.save_plan(date_iso, 15, "dentrh", params=params, schedule=sched, summary=summ,
                            mults=list(map(float, mult_use)),
                            rt_mask=list(map(float, rtm)),
                            block_planned_discharge=npd, zco_bias_w=0.0, rt_freedom=rtf,
                            meta=dict(source="batch", price_kind="real_ote"))
    else:
        raise ValueError(f"nesúlad step_min={step_min} a kind={kind}")


@app.get("/version")
def version_endpoint():
    """Vráti git commit hash + build time z Docker image. Vždy ukáže ktorá
    verzia kódu reálne beží v kontajneri (riešenie opakovaného problému že
    docker build nevyzdvihne najnovší commit)."""
    import os
    return {
        "git_commit": os.environ.get("GIT_COMMIT", "unknown"),
        "build_time": os.environ.get("BUILD_TIME", "unknown"),
        "started_at": _APP_START_TIME if "_APP_START_TIME" in globals() else None,
    }


@app.get("/plan_batch", response_class=HTMLResponse)
def plan_batch_form(from_date: str = None, to_date: str = None, step_min: int = 60,
                    kind: str = "plan"):
    """Samostatná stránka pre hromadné generovanie plánov za rozsah dátumov.
    Voliteľné query params (`from_date`, `to_date`, `step_min`, `kind`) pre-vyplnia formulár —
    napríklad keď príde redirect zo `/simulacia` pri chýbajúcich plánoch."""
    today = dt.date.today()
    default_from = from_date or (today - dt.timedelta(days=7)).isoformat()
    default_to = to_date or (today + dt.timedelta(days=1)).isoformat()
    sel_step = int(step_min) if int(step_min) in (15, 60) else 60
    sel_kind = kind if kind in ("plan", "dentrh") else "plan"
    # výpis existujúcich plánov pre prehľad
    existing = ps.list_plans() if ps is not None else []
    by_step = {60: [], 15: []}
    for it in existing:
        by_step.setdefault(int(it["step_min"]), []).append(it)
    def _rows(items):
        if not items:
            return "<tr><td colspan='3' style='color:#999;text-align:center'>(žiadne)</td></tr>"
        out = []
        for it in sorted(items, key=lambda x: x["date"], reverse=True)[:30]:
            d, s, k = it["date"], int(it["step_min"]), it["kind"]
            out.append(f"<tr><td><a href='/plan_view?date={d}&step={s}&kind={k}'>{d}</a></td>"
                       f"<td>{k}</td>"
                       f"<td style='color:#666;font-size:11px'>{it.get('generated_at','')}</td></tr>")
        return "".join(out)
    body = f"""<style>
fieldset{{border:1px solid #e0e0e0;border-radius:10px;margin:12px 0;padding:12px 16px}}
legend{{color:#2E75B6;font-weight:600}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
</style>
<h1>📦 Batch generovanie D-1 plánov</h1>
<p class="muted">Vygeneruje plány pre rozsah dátumov a uloží ich do <code>out/plans/</code>.
Livesim ich potom v <b>strict mode</b> načíta — bez disku žiaden plán nebeží.
Nastavenia (lokácia, batéria, ekonomika, násobitele, RT-freedom, bias…) sa berú z aktuálnych
hodnôt vo formulári <a href="/">/Plán D-1</a> a <a href="/dentrh">/Denný trh 15-min</a>.</p>

<form method="post" action="/plan_batch">
<fieldset><legend>Rozsah a typ</legend>
<label><span>Od (vrátane)</span><input name="from_date" type="date" value="{default_from}" required></label>
<label><span>Do (vrátane)</span><input name="to_date" type="date" value="{default_to}" required></label>
<label><span>Krok plánu</span>
  <select name="step_min">
    <option value="60"{" selected" if sel_step == 60 else ""}>60 min (D-1 hodinový — kind=plan, predikované ceny)</option>
    <option value="15"{" selected" if sel_step == 15 else ""}>15 min (Denný trh 15-min — kind=dentrh, reálne OTE day-ahead)</option>
  </select></label>
<label><span>Typ plánu (kind)</span>
  <select name="kind">
    <option value="plan"{" selected" if sel_kind == "plan" else ""}>plan (z /plan POST, predikované ceny)</option>
    <option value="dentrh"{" selected" if sel_kind == "dentrh" else ""}>dentrh (z /dentrh POST, reálne ceny)</option>
  </select></label>
<p style="color:#666;font-size:12px;margin:6px 0">Tip: pri kroku 60 použi <b>kind=plan</b>, pri kroku 15 použi <b>kind=dentrh</b>. (Inak livesim plán nenájde.)</p>
</fieldset>
<fieldset style="background:#fff3e0;border-left:4px solid #FB8C00">
<legend style="color:#E65100">Pred regeneráciou</legend>
<div style="margin-bottom:10px">
  <div style="display:flex;align-items:flex-start;gap:8px">
    <input type="checkbox" name="purge_history" value="1" id="cb_purge" checked style="margin-top:3px;width:18px;height:18px">
    <label for="cb_purge" style="display:block;cursor:pointer;font-weight:600;color:#222">
      Zmazať históriu DT plánov + VDT trades v rozsahu
    </label>
  </div>
  <div style="color:#666;font-size:12px;margin:4px 0 0 26px;line-height:1.5">
    Doporučené pri zmene parametrov (Max DAM, batt_kw, soc_reserve_pct…).
    Inak livesim merguje staré VDT obchody do nového plánu → drift → pokuta za odchýlku.
    Maže: plán JSON+DB, VDT paper_trades, VDT cache, auto_control eventy,
    capacity_ledger pre <b>vybraný rozsah</b>.
  </div>
</div>
<div style="background:#ffebee;padding:10px 12px;border-radius:6px;border:2px solid #C62828">
  <div style="display:flex;align-items:flex-start;gap:8px">
    <input type="checkbox" name="full_reset" value="1" id="cb_full" style="margin-top:3px;width:18px;height:18px">
    <label for="cb_full" style="display:block;cursor:pointer;font-weight:700;color:#C62828">
      🔥 ÚPLNÝ reset profilu (ako nový profil)
    </label>
  </div>
  <div style="color:#444;font-size:12px;margin:4px 0 0 26px;line-height:1.5">
    Pri tejto možnosti rozsah dátumov sa <b>ignoruje</b> — vyčistí sa <b>všetko</b>:
    všetky plány (nielen v rozsahu), <b>livesim CSV trace</b>, plan_overrides
    (× šablóny), MPC cache, VDT cache, paper trades, ledger, auto_control eventy.
    Profile config (batt_kw, kwp, lat/lon…) sa zachová.
    Použi keď je drift voči realite hocikedy v minulosti a chceš čistý štart.
  </div>
</div>
</fieldset>
<button type="submit">Spustiť generovanie</button>
</form>

<div class="cols" style="margin-top:18px">
<div>
<h2>Uložené plány (60 min, kind=plan)</h2>
<table><tr><th>Dátum</th><th>kind</th><th>Vygenerované</th></tr>{_rows(by_step.get(60, []))}</table>
</div>
<div>
<h2>Uložené plány (15 min, kind=dentrh)</h2>
<table><tr><th>Dátum</th><th>kind</th><th>Vygenerované</th></tr>{_rows(by_step.get(15, []))}</table>
</div>
</div>
"""
    return render_legacy_body(None, "Batch plánovanie", body)


@app.post("/plan_batch", response_class=HTMLResponse)
def plan_batch(from_date: str = Form(...), to_date: str = Form(...),
                step_min: int = Form(default=60), kind: str = Form(default="plan"),
                # voliteľné: ak pošle main /plan alebo /dentrh form spolu s rozsahom, prepíšeme ui_settings
                lat: float = Form(default=None), lon: float = Form(default=None),
                kwp: float = Form(default=None), tilt: float = Form(default=None),
                azimuth: float = Form(default=None), eff: float = Form(default=None),
                batt_kw: float = Form(default=None), batt_kwh: float = Form(default=None),
                eff_c: float = Form(default=None), eff_d: float = Form(default=None),
                soc_min: float = Form(default=None), soc_max: float = Form(default=None),
                soc_init: float = Form(default=None), terminal_soc: float = Form(default=None),
                grid_kw: float = Form(default=None), grid_fee: float = Form(default=None),
                cycle_cost: float = Form(default=None),
                min_spread: float = Form(default=None), min_trade: float = Form(default=None),
                price_scale: float = Form(default=None), pv_scale: float = Form(default=None),
                allow_grid_charge: str = Form(default=None), allow_curtail: str = Form(default=None),
                block_neg_import: str = Form(default=None),
                no_planned_discharge: str = Form(default=None),
                zco_bias_w: float = Form(default=None),
                max_export_kwh_day: float = Form(default=None),
                max_import_kwh_day: float = Form(default=None),
                soc_reserve_pct: float = Form(default=None),
                rt_grid_reserve_pct: float = Form(default=None),
                rt_freedom: str = Form(default=None),
                purge_history: str = Form(default=None),
                full_reset: str = Form(default=None),
                # Bug BATCH-NEW-PARAMS (2026-06-12): nové polia musia prísť aj cez
                # BATCH submit (rovnaký formulár, iné tlačidlo) — inak sa pri uložení
                # cez BATCH zahodili (user: "neukladá sa, stále 60").
                terminal_soc_mode: str = Form(default=None),
                vdt_breakeven_auto: str = Form(default=None),
                vdt_capacity_reserve_kw: float = Form(default=None),
                rt_engine: str = Form(default=None),
                rt2_margin_min_eur: float = Form(default=None),
                rt2_margin_full_eur: float = Form(default=None),
                rt2_zco_k: float = Form(default=None),
                rt2_margin_min_chg_eur: str = Form(default=None)):
    """Hromadné generovanie plánov pre rozsah dátumov.
    Pre každý deň v [from, to] (inkluzívne) spustí internú generáciu a uloží do plan_store.
    Ak sú v requeste prítomné aj štandardné form polia (z /plan alebo /dentrh formulára),
    najprv ich uložíme do ui_settings — batch tým pádom pracuje s aktuálne odoslanými hodnotami."""
    if ps is None:
        return "<p>plan_store modul nedostupný.</p>"
    try:
        dates = pd.date_range(from_date, to_date, freq="D")
    except Exception as e:
        return f"<p>Zlý rozsah dátumov: {e}</p>"
    # ak sú v requeste form polia, prepíšeme ui_settings ešte pred batch
    _overrides = {k: v for k, v in dict(
        lat=lat, lon=lon, kwp=kwp, tilt=tilt, azimuth=azimuth, eff=eff,
        batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
        soc_min=soc_min, soc_max=soc_max, soc_init=soc_init, terminal_soc=terminal_soc,
        grid_kw=grid_kw, grid_fee=grid_fee, cycle_cost=cycle_cost,
        min_spread=min_spread, min_trade=min_trade,
        price_scale=price_scale, pv_scale=pv_scale, zco_bias_w=zco_bias_w,
        max_export_kwh_day=max_export_kwh_day,
        max_import_kwh_day=max_import_kwh_day,
        soc_reserve_pct=soc_reserve_pct,
        rt_grid_reserve_pct=rt_grid_reserve_pct,
        vdt_capacity_reserve_kw=vdt_capacity_reserve_kw,
    ).items() if v is not None}
    # Bug BATCH-NEW-PARAMS: string/checkbox polia
    if terminal_soc_mode is not None:
        _overrides["terminal_soc_mode"] = ("next_day_price"
                                           if str(terminal_soc_mode) == "next_day_price" else "fixed")
    if vdt_breakeven_auto is not None:
        _overrides["vdt_breakeven_auto"] = bool(vdt_breakeven_auto)
    # RT poradca 2.0: voľba enginu + v2 parametre do PROFILU rt sekcie (ako /plan POST)
    try:
        import profiles as _pr_rteB
        _prof_rteB = ps.resolve_profile() if ps is not None else None
        if _prof_rteB and _prof_rteB != "default" and rt_engine is not None:
            _pobj_b = _pr_rteB.load_profile(_prof_rteB) or {}
            _rt_sec_b = _pobj_b.get("rt") or {}
            _rt2_new_b = dict(engine=(str(rt_engine) if str(rt_engine) in ("v2", "v3") else "v1"))
            if rt2_margin_min_eur is not None:
                _rt2_new_b["rt2_margin_min_eur"] = max(0.0, float(rt2_margin_min_eur))
            if rt2_margin_full_eur is not None:
                _rt2_new_b["rt2_margin_full_eur"] = max(1.0, float(rt2_margin_full_eur))
            if rt2_zco_k is not None:
                _rt2_new_b["rt2_zco_k"] = max(0.0, float(rt2_zco_k))
            if rt2_margin_min_chg_eur is not None:
                try:
                    _rt2_new_b["rt2_margin_min_chg_eur"] = (
                        float(rt2_margin_min_chg_eur)
                        if str(rt2_margin_min_chg_eur).strip() else None)
                except (TypeError, ValueError):
                    pass
            if any(_rt_sec_b.get(k) != v for k, v in _rt2_new_b.items()):
                _rt_sec_b.update(_rt2_new_b)
                _pobj_b["rt"] = _rt_sec_b
                _pr_rteB.save_profile(_prof_rteB, _pobj_b)
                print(f"[RT-ENGINE batch] profil {_prof_rteB}: {_rt2_new_b}")
    except Exception as _e_rteB:
        print(f"[RT-ENGINE batch] uloženie zlyhalo: {_e_rteB}")
    # checkbox-y: prítomné v requeste len ak sú zaškrtnuté
    if allow_grid_charge is not None: _overrides["allow_grid_charge"] = bool(allow_grid_charge)
    if allow_curtail is not None:     _overrides["allow_curtail"] = bool(allow_curtail)
    if block_neg_import is not None:  _overrides["block_neg_import"] = bool(block_neg_import)
    if no_planned_discharge is not None: _overrides["no_planned_discharge"] = bool(no_planned_discharge)
    if rt_freedom is not None:        _overrides["rt_freedom"] = bool(rt_freedom)
    ui_key = "plan" if int(step_min) == 60 else "dentrh"
    fp = _ui_load(ui_key, DEF)
    main_form_sent = any(k in _overrides for k in ("lat", "kwp", "batt_kw"))
    if _overrides:
        fp = {**fp, **_overrides}
        for cb in ("allow_grid_charge", "allow_curtail", "block_neg_import",
                   "no_planned_discharge", "rt_freedom"):
            if cb not in _overrides and main_form_sent:
                fp[cb] = False
        _ui_save(ui_key, fp)
    # ── streaming progress + paralelný worker pool ─────────────────────────
    from concurrent.futures import ThreadPoolExecutor, as_completed
    MAX_PARALLEL = 1                                       # sériovo (Open-Meteo veľmi striktný rate limit ~10 req/min)

    _do_purge = bool(purge_history)
    _do_full = bool(full_reset)
    def _stream():
        # PRE-flight: ktoré dni už majú plán na disku
        already = [d.date().isoformat() for d in dates
                    if ps.has_plan(d.date().isoformat(), int(step_min), str(kind))]
        total = len(dates)
        # Bug #631/#634: zmazať históriu pred regeneráciou
        # full_reset má prednosť pred purge_history (úplný wipe ignoruje date range)
        purge_counts = {"plans": 0, "vdt_trades": 0, "ledger_rows": 0}
        if _do_full:
            try:
                purge_counts = ps.purge_full_profile()
            except Exception as _e_purge:
                print(f"[plan_batch #634] purge_full_profile zlyhal: {_e_purge}")
        elif _do_purge and total > 0:
            try:
                purge_counts = ps.purge_history_range(
                    str(dates[0].date()), str(dates[-1].date()),
                    step_min=int(step_min), kind=str(kind))
            except Exception as _e_purge:
                print(f"[plan_batch #631] purge_history_range zlyhal: {_e_purge}")
        yield ("<!doctype html><html lang='sk'><head><meta charset='utf-8'>"
                f"<title>Batch ({from_date}→{to_date})</title>"
                "<style>body{font-family:-apple-system,Segoe UI,Arial;max-width:1100px;margin:24px auto;padding:0 16px;color:#222}"
                "h1,h2{color:#1F4E78} table{border-collapse:collapse;width:100%;font-size:14px}"
                "th,td{border:1px solid #e3e3e3;padding:6px 10px;text-align:left} th{background:#1F4E78;color:#fff}"
                ".prog{background:#eef1f5;border-radius:6px;height:10px;overflow:hidden;margin:8px 0}"
                ".prog>div{background:linear-gradient(90deg,#1F4E78,#2E7D32);height:100%;transition:width .3s}"
                "code{font-size:12px;color:#666} a{color:#1F4E78}</style></head><body>")
        yield (f"<h1>📦 Batch plánovanie</h1>"
                f"<p>Rozsah <b>{from_date} → {to_date}</b> ({total} dní), krok <b>{step_min} min</b>, kind <b>{kind}</b>. "
                f"<i>Paralelizácia: {MAX_PARALLEL} workers.</i></p>")
        if _do_full:
            yield (f"<div style='background:#ffebee;border-left:4px solid #C62828;"
                    f"padding:10px 14px;border-radius:6px;margin:8px 0'>"
                    f"🔥 <b>ÚPLNÝ reset profilu hotový:</b> zmazaných "
                    f"<b>{purge_counts.get('plans', 0)}</b> plánov, "
                    f"<b>{purge_counts.get('vdt_trades', 0)}</b> VDT trade-ov, "
                    f"<b>{purge_counts.get('vdt_cache', 0)}</b> VDT/MPC cache súborov, "
                    f"<b>{purge_counts.get('auto_control_events', 0)}</b> auto_control eventov, "
                    f"<b>{purge_counts.get('plan_overrides', 0)}</b> plan_override súborov, "
                    f"<b>{purge_counts.get('livesim_files', 0)}</b> livesim súborov (CSV+meta), "
                    f"<b>{purge_counts.get('effect_minute', 0)}+{purge_counts.get('effect_daily', 0)}</b> effect riadkov (min+daily), "
                    f"<b>{purge_counts.get('livesim_files', 0)}</b> livesim CSV/meta súborov, "
                    f"<b>{purge_counts.get('ledger_rows', 0)}</b> ledger rezervácií.<br>"
                    f"<i>Profile config (batt_kw, kwp, lat/lon, …) zostáva nedotknutý.</i></div>")
        elif _do_purge:
            yield (f"<div style='background:#fff3e0;border-left:4px solid #FB8C00;"
                    f"padding:8px 12px;border-radius:6px;margin:8px 0'>"
                    f"🗑️ <b>Pred-cleanup hotový:</b> zmazaných "
                    f"<b>{purge_counts.get('plans', 0)}</b> plánov, "
                    f"<b>{purge_counts.get('vdt_trades', 0)}</b> VDT trade-ov, "
                    f"<b>{purge_counts.get('vdt_cache', 0)}</b> VDT cache súborov, "
                    f"<b>{purge_counts.get('auto_control_events', 0)}</b> auto_control eventov, "
                    f"<b>{purge_counts.get('ledger_rows', 0)}</b> ledger rezervácií</div>")
        if already:
            ul = "".join(
                f"<li><a href='/plan_view?date={d}&step={int(step_min)}&kind={kind}'>{d}</a></li>"
                for d in already)
            yield (f"<details open style='background:#eef5e0;padding:8px 12px;border-left:4px solid #2E7D32;"
                    f"border-radius:6px;margin:8px 0'>"
                    f"<summary><b>Už hotových {len(already)} / {total}</b> "
                    f"(prepíšu sa novými parametrami z formulára)</summary>"
                    f"<ul style='margin:6px 0'>{ul}</ul></details>")
        yield (f"<div class='prog' id='pb'><div id='pf' style='width:0%'></div></div>"
                f"<table><tr><th>#</th><th>Dátum</th><th>Stav</th><th>Detail</th><th>Náhľad</th></tr>")
        # 1024 B padding pre flush v niektorých proxy / browser cache
        yield " " * 1024 + "\n"
        ok_cnt = 0; fail_cnt = 0
        date_list = [d.date().isoformat() for d in dates]
        # Pri MAX_PARALLEL=1 ide sériový beh s 1.5 s pauzou medzi dňami
        # (rate-limit-friendly pre Open-Meteo ~10 req/min).
        import time as _time_batch
        _throttle_per_day = 1.5 if MAX_PARALLEL == 1 else 0.0
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            # Pri sériovom režime submit-uj s pauzou — inak všetky futures vyletia naraz
            # do ThreadPoolu a v queue čakajú, ale fetch_pv sa volá hneď ako worker prebrali
            # → namiesto submitovania všetkých naraz, ich predávame sekvenčne.
            if MAX_PARALLEL == 1:
                futures = {}
                for d_iso in date_list:
                    futures[pool.submit(_gen_one_plan, d_iso, int(step_min), str(kind), fp)] = d_iso
                    _time_batch.sleep(_throttle_per_day)
            else:
                futures = {pool.submit(_gen_one_plan, d_iso, int(step_min), str(kind), fp): d_iso
                           for d_iso in date_list}
            i = 0
            for fut in as_completed(futures):
                d_iso = futures[fut]
                i += 1
                pct = int(i / total * 100)
                try:
                    path = fut.result()
                    ok_cnt += 1
                    # Bug GRID-LIMIT-SOFT: ak plán vznikol s obmedzením (clip na sieťové
                    # limity), save_plan zapísal meta.plan_warnings → ukáž ORANŽOVÝ riadok
                    # (plán existuje, ale bol obmedzený), nie zelené OK ani červenú chybu.
                    _pw = []
                    try:
                        _pl_chk = ps.load_plan(d_iso, int(step_min), str(kind))
                        _pw = ((_pl_chk or {}).get("meta") or {}).get("plan_warnings") or []
                    except Exception:
                        _pw = []
                    if _pw:
                        _wmsg = ("; ".join(str(x) for x in _pw)).replace("<", "&lt;").replace(">", "&gt;")
                        yield (f"<tr><td>{i}/{total}</td>"
                                f"<td style='color:#E67E22;font-weight:600'>⚠ {d_iso}</td>"
                                f"<td style='color:#E67E22;font-weight:600'>OBMEDZENÝ</td>"
                                f"<td style='color:#B9770E'>{_wmsg}<br><code>{path}</code></td>"
                                f"<td><a href='/plan_view?date={d_iso}&step={int(step_min)}&kind={kind}' target='_blank'>zobraziť →</a></td></tr>"
                                f"<script>document.getElementById('pf').style.width='{pct}%';</script>")
                    else:
                        yield (f"<tr><td>{i}/{total}</td>"
                                f"<td style='color:#2E7D32;font-weight:600'>✓ {d_iso}</td>"
                                f"<td>OK</td><td><code>{path}</code></td>"
                                f"<td><a href='/plan_view?date={d_iso}&step={int(step_min)}&kind={kind}' target='_blank'>zobraziť →</a></td></tr>"
                                f"<script>document.getElementById('pf').style.width='{pct}%';</script>")
                except Exception as ex:
                    fail_cnt += 1
                    msg = str(ex)[:200].replace("<", "&lt;").replace(">", "&gt;")
                    yield (f"<tr><td>{i}/{total}</td>"
                            f"<td style='color:#C0392B;font-weight:600'>✗ {d_iso}</td>"
                            f"<td>CHYBA</td><td colspan='2' style='color:#C0392B'>{msg}</td></tr>"
                            f"<script>document.getElementById('pf').style.width='{pct}%';</script>")
                yield " " * 256 + "\n"
        yield (f"</table>"
                f"<h2>Súhrn</h2>"
                f"<p>✓ Úspech: <b>{ok_cnt}</b> &nbsp;•&nbsp; ✗ Zlyhanie: <b>{fail_cnt}</b> &nbsp;z&nbsp; <b>{total}</b> dní.</p>"
                f"<p><a href='/livesim'>← Späť do živej simulácie</a> &nbsp;|&nbsp; "
                f"<a href='/plan_batch'>📦 Batch plán</a> &nbsp;|&nbsp; "
                f"<a href='/'>Hlavné menu</a></p>"
                f"</body></html>")
    return StreamingResponse(_stream(), media_type="text/html; charset=utf-8",
                              headers={"X-Accel-Buffering": "no"})


@app.get("/profiles", response_class=HTMLResponse)
def profiles_browse(request: Request):
    """Profile manager: zoznam, aktívny profile, akcie (apply, edit, delete, snapshot)."""
    from ui.templates import render
    if pr is None:
        return HTMLResponse("<p>profiles modul nedostupný.</p>", status_code=503)
    profiles_list = pr.list_profiles()
    active = pr.get_active()
    # Per-market enabled set (scheduler background flag) — pre indikátor v zozname
    try:
        import auto_control as _ac
        bg_set = _ac.get_enabled_profiles()
    except Exception:
        bg_set = set()
    rows = []
    active_mode = "simulation"
    for n in profiles_list:
        p = pr.load_profile(n)
        if not p:
            continue
        m96 = p.get("mult96") or []
        rt96 = p.get("rt_on96") or []
        mn = sum(1 for x in m96 if x is not None and abs(float(x) - 1.0) > 1e-6)
        rt_off = sum(1 for x in rt96 if x is not None and float(x) < 0.5)
        prof_mode = (p.get("mode") or "simulation").lower()
        if n == active:
            active_mode = prof_mode
        rows.append({
            "name": n,
            "active": (n == active),
            "mode": prof_mode,
            "bg_enabled": (n in bg_set),
            "updated_at": p.get("updated_at", ""),
            "mults_changed": mn,
            "rt_off_count": rt_off,
            "kdis": p.get("rt", {}).get("kdis", "—"),
            "kchg": p.get("rt", {}).get("kchg", "—"),
            "no_planned_discharge": p.get("plan", {}).get("no_planned_discharge", False),
            "zco_bias_w": p.get("plan", {}).get("zco_bias_w", 0),
            "note": p.get("note", "") or "",
        })
    return render(request, "pages/profiles_list.html",
                   rows=rows, active=active, active_mode=active_mode)


@app.get("/profiles/edit", response_class=HTMLResponse)
def profiles_edit(request: Request, name: str = "__new__",
                    defaults: str = "", preset_mode: str = ""):
    """Form na vytvorenie nového alebo editáciu existujúceho profilu.
    `defaults=1` pri name=__new__ → pre-fill z továrenských DEF hodnôt (NIE z aktuálnych UI).
    `preset_mode=real` → pri novom profile zaškrtne radio "🔴 Reálny chod" namiesto
                          default Simulácia. Použité v linke z /realio?tab=riadenie."""
    from ui.templates import render
    if pr is None:
        return HTMLResponse("<p>profiles modul nedostupný.</p>", status_code=503)
    is_new = (name == "__new__")
    if is_new:
        if defaults:
            # továrenské defaulty
            data = {"plan": dict(DEF), "dentrh": dict(DEF),
                    "rt": {"kdis": 1.0, "kchg": 1.0, "dtk": None, "rboost": 0.0},
                    "mult96": [], "rt_on96": [], "note": "Vytvorené ako defaultný profil"}
            title = "Vytvoriť nový profile (továrenské defaulty)"
        else:
            # pre-fill z aktuálnych UI hodnôt
            data = {"plan": _ui_load("plan", DEF), "dentrh": _ui_load("dentrh", DEF),
                    "rt": _ui_load("rt", {}), "mult96": [], "rt_on96": [], "note": ""}
            title = "Vytvoriť nový profile (z aktuálneho UI)"
    else:
        data = pr.load_profile(name)
        if data is None:
            return HTMLResponse(
                f"<p>Profile <b>{name}</b> neexistuje. <a href='/profiles'>← Späť</a></p>",
                status_code=404)
        title = f"Edit profile: {name}"

    plan_json = json.dumps(data.get("plan", {}), ensure_ascii=False, indent=2)
    dentrh_json = json.dumps(data.get("dentrh", {}), ensure_ascii=False, indent=2)
    rt_json = json.dumps(data.get("rt", {}), ensure_ascii=False, indent=2)

    # Distribučný config (TOU sadzby) — F3 Joint LP
    dist_presets_list = []
    dist_presets_json = "{}"
    try:
        import distribution_cost as _dc_edit
        _dist_data = data.get("distribution") or _dc_edit.default_config()
        dist_presets_list = _dc_edit.list_presets()
        dist_presets_json = json.dumps(_dc_edit.TARIFF_PRESETS, ensure_ascii=False)
    except Exception:
        _dist_data = data.get("distribution", {})
    distribution_json = json.dumps(_dist_data, ensure_ascii=False, indent=2)

    note_val = data.get("note", "")
    profile_mode = (data.get("mode") or "simulation").lower()
    if profile_mode not in ("simulation", "real"):
        profile_mode = "simulation"

    # Background scheduler enabled flag (per aktuálny market) — toggle "Beží na pozadí"
    bg_enabled = False
    try:
        import auto_control as _ac
        bg_enabled = (not is_new) and (name in _ac.get_enabled_profiles())
    except Exception:
        bg_enabled = False

    return render(request, "pages/profiles_edit.html",
                   title=title,
                   is_new=is_new,
                   name=("" if is_new else name),
                   preset_mode=preset_mode,
                   profile_mode=profile_mode,
                   note_val=note_val,
                   bg_enabled=bg_enabled,
                   plan_json=plan_json,
                   dentrh_json=dentrh_json,
                   rt_json=rt_json,
                   distribution_json=distribution_json,
                   dist_presets_list=dist_presets_list,
                   dist_presets_json=dist_presets_json)




@app.post("/profiles/save", response_class=HTMLResponse)
def profiles_save(name: str = Form(...), note: str = Form(default=""),
                   plan_json: str = Form(default="{}"), dentrh_json: str = Form(default="{}"),
                   rt_json: str = Form(default="{}"),
                   distribution_json: str = Form(default="{}"),
                   mode: str = Form(default="simulation"),
                   bg_enabled: str = Form(default="")):
    """Uloží profile zo zaslaných JSON polí. Pri novom profile sa zapamätá mode
    (simulation/real), pri existujúcom je ignorovaný (mode je immutable v save_profile)."""
    if pr is None:
        return "<p>profiles modul nedostupný.</p>"
    try:
        plan_d = json.loads(plan_json) if plan_json.strip() else {}
        dentrh_d = json.loads(dentrh_json) if dentrh_json.strip() else {}
        rt_d = json.loads(rt_json) if rt_json.strip() else {}
        # Distribučný config — normalize cez distribution_cost (vyplní missing defaults)
        try:
            import distribution_cost as _dc_save
            dist_raw = json.loads(distribution_json) if distribution_json.strip() else {}
            dist_d = _dc_save.normalize_config(dist_raw)
        except Exception:
            dist_d = {}
    except json.JSONDecodeError as e:
        return f"<p>JSON parse error: {e}</p><a href='/profiles'>← Späť</a>"
    # mult96 + rt_on96 zo súčasnej šablóny (snapshot)
    mult96, rt_on96 = [], []
    if po is not None:
        try:
            import numpy as _np
            m = po.load_template()
            rt = po.load_template_rt()
            mult96 = [None if not _np.isfinite(x) else float(x) for x in m]
            rt_on96 = [None if not _np.isfinite(x) else float(x) for x in rt]
        except Exception:
            pass
    path = pr.save_profile(name, {"plan": plan_d, "dentrh": dentrh_d, "rt": rt_d,
                                    "mult96": mult96, "rt_on96": rt_on96,
                                    "note": note, "mode": mode,
                                    "distribution": dist_d})
    # Background scheduler enabled toggle (per aktuálny market) — best-effort, neblokujeme save
    bg_was_on = False
    try:
        import auto_control as _ac
        bg_was_on = (name in _ac.get_enabled_profiles())
        _ac.set_profile_enabled(name, bool(bg_enabled))
    except Exception as _bg_e:
        print(f"[profiles_save] set_profile_enabled({name}, {bool(bg_enabled)}) zlyhal: {_bg_e}")

    # Trigger immediate D-1 plán generation ak sa bg_enabled toggleol OFF→ON
    # (nečakaj na nasledujúci 14:00 autoplan_d1 cron). Beží v thread, neblokuje response.
    if bool(bg_enabled) and not bg_was_on:
        try:
            import threading, datetime as _dt
            def _autoplan_now(_n=name):
                try:
                    import d1_planner as _dp
                    import market as _mk
                    tomorrow = _dt.date.today() + _dt.timedelta(days=1)
                    res = _dp.compute_d1_plan(tomorrow, market=_mk.active_market(), profile=_n)
                    print(f"[profiles_save] immediate autoplan({_n}, {tomorrow}) → "
                          f"{res.get('status','?')} · ZISK {res.get('zisk_eur', 0):+.2f}")
                except Exception as _ape:
                    print(f"[profiles_save] immediate autoplan({_n}) zlyhal: {_ape}")
            threading.Thread(target=_autoplan_now, daemon=True).start()
        except Exception as _te:
            print(f"[profiles_save] autoplan thread spawn zlyhal: {_te}")
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
             f"<meta http-equiv='refresh' content='1;url=/profiles'></head><body>"
             f"<p>✓ Profile <b>{name}</b> uložený do <code>{path}</code>. Redirect na /profiles…</p></body></html>")


@app.post("/profiles/apply", response_class=HTMLResponse)
def profiles_apply(name: str = Form(...), redirect_to: str = Form(default="")):
    """Aplikuje profile: prepíše ui_settings.plan/dentrh/rt + plan_overrides template.
    Plus auto-detect: ak profil má v plan_store iný kind (15-min vs hodinový),
    automaticky nastaví ui_settings.livesim.case aby livesim STRICT nepadal.

    Bug Q: `redirect_to` param pre návrat na pôvodnú stránku (napr. /realio?tab=riadenie)
    po zmene profilu cez quick switcher dropdown. Default = /profiles."""
    if pr is None:
        return "<p>profiles modul nedostupný.</p>"
    try:
        summary = pr.apply_to_ui_and_overrides(name, _ui_save, po)
    except FileNotFoundError as e:
        return f"<p>Profile <b>{name}</b> nenájdený: {e}</p><a href='/profiles'>← Späť</a>"

    # Bug CACHE-WIPE-ON-SWITCH (2026-06-15): NEmaž cache pri prepnutí profilu! Pôvodné
    # _livesim_cache_invalidate() (clear ALL) zmazalo cache VŠETKÝCH profilov pri KAŽDOM
    # prepnutí (apply) → n_cache=0 → SWITCH-INSTANT vždy MISS → každé otvorenie prepočet
    # ("akoby DB ani nebola"). Cache je per-profil a konzistentná s parametrami každého
    # profilu (bg ráta z profilu); reálnu zmenu nastavení zachytí meta mtime / settings_sig.
    # Preto pri prepnutí cache NEčistíme → profily ostanú teplé → prepnutie = cache hit.
    pass

    # Auto-detect: zisti ktorý kind plánov profil najviac používa (dentrh vs plan)
    # a nastav ui_settings.livesim.case zhodne — tým sa user vyhne STRICT chybe
    # keď prepne profil ktorý plánuje len 15-min, alebo naopak.
    livesim_case_changed = ""
    try:
        if ps is not None:
            plans_all = ps.list_plans(profile=name) or []
            n_dentrh = sum(1 for p in plans_all if (p.get("kind") or "") == "dentrh")
            n_plan = sum(1 for p in plans_all if (p.get("kind") or "") == "plan")
            # Preferuj kind ktorý má aspoň nejaké plány; pri rovnosti uprednosti dentrh (mode='real' default)
            if n_dentrh > 0 or n_plan > 0:
                target_case = "dt_15min" if n_dentrh >= n_plan else "plan_d1"
                cur_ls = _ui_load("livesim", {"case": "plan_d1", "start": "", "to": "",
                                                 "rt_kdis": 1.5, "rt_kchg": 2.5, "use_rt": True})
                # Reset livesim UI parametre pre nový profil — predchádzajúci profil mohol mať
                # úplne iné start/use_rt nastavenia (napr. start=2026-01-01 čo by trvalo prepočítať).
                # User po prepnutí vie, s akým nastavením livesim beží na pozadí.
                ls_changes = []
                # 1. case (auto-detect z dostupných plánov)
                if cur_ls.get("case") != target_case:
                    cur_ls["case"] = target_case
                    ls_changes.append(f"case → <b>{target_case}</b>")
                # 2. start = dátum najstaršieho plánu pre nový profil (alebo today-7d ak žiadne)
                try:
                    dates_sorted = sorted({p.get("date") for p in plans_all if p.get("date")})
                    new_start = dates_sorted[0] if dates_sorted else (dt.date.today() - dt.timedelta(days=7)).isoformat()
                except Exception:
                    new_start = (dt.date.today() - dt.timedelta(days=7)).isoformat()
                if cur_ls.get("start") != new_start:
                    cur_ls["start"] = new_start
                    ls_changes.append(f"start → <b>{new_start}</b>")
                # 3. use_rt = default True (per memo #130 sa pre 15-min stejne force-uje False v dt_15min)
                if cur_ls.get("use_rt") is not True:
                    cur_ls["use_rt"] = True
                    ls_changes.append("use_rt → <b>True</b>")
                if ls_changes:
                    _ui_save("livesim", cur_ls)
                    livesim_case_changed = (
                        f" · livesim: " + ", ".join(ls_changes) +
                        f" (profil má {n_dentrh}× dentrh, {n_plan}× plan)")
    except Exception as _ae:
        print(f"[profiles_apply] auto-detect livesim case zlyhal: {_ae}")

    # Bug R3: Auto-enable bg + spusti tick ak bol bg-OFF
    # Princíp: klik na chip = profil je aktívny a bežiaci hneď. User nemusí manual
    # zapnúť toggle "Beží na pozadí" v /profiles/edit.
    auto_started_msg = ""
    try:
        import auto_control as _ac
        was_off = name not in _ac.get_enabled_profiles()
        if was_off:
            _ac.set_profile_enabled(name, True)
            auto_started_msg = " · bg auto-zapnutý"
            print(f"[/profiles/apply] {name}: bg-OFF → auto-enabled (klik na chip)")
            # Spusti VDT advisor cache refresh + autoplan_d1 ak chýba dnešný plán
            try:
                import vdt_live_advisor as _adv
                import threading as _th
                # Async tick aby nezablokoval response (advisor LP môže trvať pár sekúnd)
                def _tick():
                    try:
                        _adv.get_live_recommendation(profile=name)
                        print(f"[/profiles/apply] {name}: VDT advisor tick OK")
                    except Exception as _te:
                        print(f"[/profiles/apply] {name}: VDT tick zlyhal: {_te}")
                _th.Thread(target=_tick, daemon=True).start()
                auto_started_msg += " + VDT tick"
            except Exception as _ve:
                print(f"[/profiles/apply] {name}: VDT tick init zlyhal: {_ve}")
    except Exception as _ae:
        print(f"[/profiles/apply] {name}: auto-enable bg zlyhal: {_ae}")

    _clear_livesim_logs()           # iný profil = iné nastavenia + iná šablóna → fresh log
    upd = ", ".join(summary.get("updated", []))
    livesim_case_changed = livesim_case_changed + auto_started_msg
    # Bug Q1: redirect_to podporuje návrat na pôvodnú stránku (napr. /realio?tab=riadenie)
    # po quick switcheri profilu. Validácia: musí začínať /, žiadne URL injekcie.
    _target = "/profiles"
    if redirect_to and redirect_to.startswith("/") and not redirect_to.startswith("//"):
        _target = redirect_to
    # HTML escape pre meta refresh attribute (URL query string môže obsahovať & / =)
    _target_esc = _target.replace('"', "&quot;").replace("'", "&#39;")
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
             f"<meta http-equiv='refresh' content='1;url={_target_esc}'></head><body>"
             f"<p>✓ Profile <b>{name}</b> aplikovaný. Aktualizované: <code>{upd}</code>{livesim_case_changed}. "
             f"Livesim log vyresetovaný. Redirect na <code>{_target_esc}</code>…</p></body></html>")


@app.post("/market/set", response_class=HTMLResponse)
def market_set(market: str = Form(...)):
    """Prepne aktívny trh (cz/sk). Po prepnutí sa všetky čítania presunú do out/<market>/.
    Livesim log vyresetuje (lebo dáta sú per-market). Profile sa pri prepnutí trhu môže meniť
    (defaultne sa nemení — ostáva taký aký bol; ale data spojený s ním sa hľadá v novom trhu)."""
    if mk is None:
        return "<p>market modul nedostupný.</p>"
    try:
        prev = mk.get_active_market()
        mk.set_active_market(market)
        cur = mk.get_active_market()
        _clear_livesim_logs()                                    # iné dáta → fresh log
        return (f"<!doctype html><html><head><meta charset='utf-8'>"
                f"<meta http-equiv='refresh' content='1;url=/'></head><body style='font-family:Arial;text-align:center;margin-top:40px'>"
                f"<h1 style='color:#2E7D32'>✓ Trh prepnutý</h1>"
                f"<p>{mk.label_for(prev)} → <b>{mk.label_for(cur)}</b><br>"
                f"Data dir: <code>{mk.data_dir()}</code></p>"
                f"<p>Livesim log vyresetovaný. Presmerovávam na úvod…</p></body></html>")
    except Exception as e:
        return f"<p>Chyba pri prepnutí trhu: {e}</p>"


@app.post("/profiles/delete", response_class=HTMLResponse)
def profiles_delete(name: str = Form(...)):
    if pr is None:
        return "<p>profiles modul nedostupný.</p>"
    try:
        deleted = pr.delete_profile(name)
    except Exception as e:
        # Bug #640: error v delete (FK constraint, IntegrityError) sa už nepotláča
        return (f"<!doctype html><html><head><meta charset='utf-8'></head><body style='font-family:Arial;padding:20px'>"
                 f"<h2 style='color:#C62828'>✗ Mazanie profilu zlyhalo</h2>"
                 f"<p>Profil <b>{name}</b> sa nepodarilo zmazať. Detail chyby:</p>"
                 f"<pre style='background:#ffebee;padding:12px;border-radius:6px;overflow:auto'>"
                 f"{type(e).__name__}: {e}</pre>"
                 f"<p><a href='/profiles'>← Späť na zoznam profilov</a></p></body></html>")
    msg = "✓ Zmazané" if deleted else "⚠ Neexistovalo"
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
             f"<meta http-equiv='refresh' content='1;url=/profiles'></head><body>"
             f"<p>{msg}: <b>{name}</b>. Redirect…</p></body></html>")


@app.post("/profiles/snapshot", response_class=HTMLResponse)
def profiles_snapshot(name: str = Form(...), mode: str = Form(default="simulation")):
    """Snímka súčasných UI hodnôt + šablóny → nový profile.
    Mode (simulation/real) je fixovaný pri vzniku."""
    if pr is None:
        return "<p>profiles modul nedostupný.</p>"
    try:
        path = pr.snapshot_current(name, _ui_load, po,
                                     note=f"Snímka {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
                                     mode=mode)
    except Exception as e:
        return f"<p>Chyba: {e}</p><a href='/profiles'>← Späť</a>"
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
             f"<meta http-equiv='refresh' content='1;url=/profiles'></head><body>"
             f"<p>📸 Snímka profile <b>{name}</b> uložená do <code>{path}</code>. Redirect…</p></body></html>")


# ═══════════════════════════════════════════════════════════════════
# FTV SCENÁRE — per-dátum override hodinového FTV priebehu
# ═══════════════════════════════════════════════════════════════════
def _ftv_scenario_default_hours(date_iso: str):
    """Vráti (24 hodín, is_saved, note, time_shift_min) pre daný deň — zo scenára alebo PVF fallback."""
    if fs is not None and fs.has_scenario(date_iso):
        sc = fs.load_scenario(date_iso)
        if sc is not None:
            return ([float(x) for x in sc["hourly_kw"]], True, sc.get("note", ""),
                    int(sc.get("time_shift_min", 0)))
    # fallback: skús plan_store (60-min plán pre tento deň má pv_kwh v 24-prvkovej schéme)
    if ps is not None:
        sch_disk = ps.load_plan_safe(date_iso, 60, "plan") if hasattr(ps, "load_plan_safe") else None
        if sch_disk is not None:
            try:
                pv = sch_disk["schedule"].get("pv_kwh", [])
                if len(pv) == 24:
                    return [float(x) for x in pv], False, "", 0
            except Exception:
                pass
    # úplný fallback: prázdne (24 núl)
    return [0.0] * 24, False, "", 0


@app.get("/ftv_scenario", response_class=HTMLResponse)
def ftv_scenario_get(request: Request, date: str = None, msg: str = ""):
    """Editor hodinového FTV scenára pre konkrétny dátum.
    Interaktívne SVG s 24 ťahafnými ručkami + Gauss smoothing susedov + Save/Delete."""
    from ui.templates import render
    if fs is None:
        return HTMLResponse("<p>ftv_scenarios modul nedostupný.</p>", status_code=503)
    d_iso = date or dt.date.today().isoformat()
    try:
        d_iso = dt.date.fromisoformat(d_iso).isoformat()
    except (ValueError, TypeError):
        d_iso = dt.date.today().isoformat()
    hours, is_saved, note, time_shift_min = _ftv_scenario_default_hours(d_iso)
    saved_at = ""
    if is_saved:
        sc = fs.load_scenario(d_iso)
        if sc:
            saved_at = sc.get("saved_at", "")
    scenarios = (fs.list_scenarios() or [])[:30]
    return render(request, "pages/ftv_scenario.html",
                   d_iso=d_iso, msg=msg, is_saved=is_saved, saved_at=saved_at,
                   note=note, time_shift_min=time_shift_min,
                   js_hours=json.dumps(hours), scenarios=scenarios)




@app.post("/ftv_scenario", response_class=HTMLResponse)
def ftv_scenario_post(date: str = Form(...), hourly_json: str = Form(...),
                      note: str = Form(default=""), action: str = Form(default="save"),
                      time_shift_min: int = Form(default=0)):
    """Uloží/zmaže per-dátum FTV scenár (vrátane časového posunu reality)."""
    if fs is None:
        return "<p>ftv_scenarios modul nedostupný.</p>"
    try:
        d_iso = dt.date.fromisoformat(date).isoformat()
    except (ValueError, TypeError):
        return ftv_scenario_get(date=None, msg="Neplatný formát dátumu.")
    if action == "delete":
        ok = fs.delete_scenario(d_iso)
        return ftv_scenario_get(date=d_iso, msg=("✓ Scenár zmazaný." if ok else "Scenár nebol nájdený."))
    try:
        hours = json.loads(hourly_json)
        # clip shift na rozumný rozsah ±180 min
        _shift = max(-180, min(180, int(time_shift_min)))
        path = fs.save_scenario(d_iso, hours, smooth_sigma=1.0, note=note,
                                 time_shift_min=_shift)
    except (ValueError, json.JSONDecodeError) as e:
        return ftv_scenario_get(date=d_iso, msg=f"Chyba pri ukladaní: {e}")
    _shift_info = f" (posun reality {_shift:+d} min)" if _shift else ""
    return ftv_scenario_get(date=d_iso,
                            msg=f"✓ Scenár pre {d_iso} uložený do {os.path.basename(path)}{_shift_info}. Livesim ho pri tomto dni preberie.")


@app.get("/plans", response_class=HTMLResponse)
def plans_browse(request: Request, date_from: str = None, date_to: str = None,
                  kind: str = "all", step: str = "all", profile: str = None):
    """Browser uložených plánov v plan_store s filtrami a akciami (zobraziť/zmazať).
    profile=None → aktívny profil. Cez query param vie zobraziť plány aj z iného profilu."""
    from ui.templates import render
    if ps is None:
        return HTMLResponse("<p>plan_store modul nedostupný.</p>", status_code=503)
    today = dt.date.today()
    df = date_from or (today - dt.timedelta(days=30)).isoformat()
    dt_ = date_to or (today + dt.timedelta(days=7)).isoformat()
    active_profile = ps.resolve_profile(profile)
    all_profiles = ps.list_profiles_with_plans() or [active_profile]
    all_plans = ps.list_plans(profile=active_profile)
    fk = (kind or "all").lower()
    fs_step = str(step or "all").lower()
    rows_data = []
    for it in all_plans:
        d_iso = it["date"]
        try:
            d_obj = dt.date.fromisoformat(d_iso)
        except Exception:
            continue
        if d_obj < dt.date.fromisoformat(df) or d_obj > dt.date.fromisoformat(dt_):
            continue
        if fk != "all" and it["kind"] != fk:
            continue
        if fs_step != "all" and int(it["step_min"]) != int(fs_step):
            continue
        p = ps.load_plan_safe(d_iso, int(it["step_min"]), it["kind"])
        if not p:
            continue
        summary = p.get("summary", {})
        mults = p.get("mults") or []
        rt = p.get("rt_mask") or []
        zisk_v = summary.get("ZISK_EUR")
        zisk_num = float(zisk_v) if isinstance(zisk_v, (int, float)) else None
        rows_data.append(dict(
            date=d_iso, step=int(it["step_min"]), kind=it["kind"],
            generated_at=p.get("generated_at", ""),
            zisk_num=zisk_num,
            zisk_str=(f"{zisk_num:+.1f} €" if zisk_num is not None else "—"),
            cycles=round(float(summary.get("nabite_kWh", 0) + summary.get("vybite_kWh", 0)) / 2.0
                         / float(p.get("params", {}).get("batt_kwh", 200)), 2),
            mult_act=sum(1 for x in mults if x is not None and abs(float(x) - 1.0) > 1e-6),
            rt_off=sum(1 for x in rt if x is not None and float(x) < 0.5),
            npd=bool(p.get("block_planned_discharge", False)),
            zbw=float(p.get("zco_bias_w", 0)),
            rt_freedom=bool(p.get("rt_freedom", True)),
            source=p.get("meta", {}).get("source", "?"),
        ))
    rows_data.sort(key=lambda r: (r["date"], r["step"], r["kind"]), reverse=True)
    return render(request, "pages/plans_list.html",
                   rows=rows_data, profiles=all_profiles,
                   active_profile_name=active_profile,
                   date_from=df, date_to=dt_,
                   kind_filter=fk, step_filter=fs_step)


@app.post("/plans/delete", response_class=HTMLResponse)
def plans_delete(date: str = Form(...), step: int = Form(...), kind: str = Form(...),
                  profile: str = Form(default=None)):
    """Zmaže jeden uložený plán z daného profilu (alebo aktívneho) a redirectne na /plans."""
    if ps is None:
        return "<p>plan_store modul nedostupný.</p>"
    deleted = ps.delete_plan(date, int(step), kind, profile=profile)
    msg = ("✓ Zmazané" if deleted else "⚠ Plán neexistoval")
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
             f"<meta http-equiv='refresh' content='1;url=/plans'></head><body>"
             f"<p>{msg}: <b>{date}</b> ({step}m, {kind}). Redirect na /plans…</p></body></html>")


@app.get("/plan_view", response_class=HTMLResponse)
def plan_view(date: str, step: int = 60, kind: str = "plan"):
    """Zobrazí uložený plán z plan_store. Tabuľka rozvrhu + súhrn parametrov."""
    if ps is None:
        return "<p>plan_store modul nedostupný.</p>"
    plan = ps.load_plan_safe(date, int(step), kind)
    if plan is None:
        return (f"<!doctype html><html lang='sk'><head><meta charset='utf-8'><title>Plán nenájdený</title></head>"
                f"<body style='font-family:-apple-system,Segoe UI,Arial;max-width:900px;margin:24px auto;padding:0 16px'>"
                f"<h1 style='color:#C0392B'>Plán nenájdený</h1>"
                f"<p>Plán pre {date} ({step}-min, {kind}) neexistuje. Vygeneruj ho cez "
                f"<a href='/'>/plan</a>, <a href='/dentrh'>/dentrh</a> alebo "
                f"<a href='/plan_batch'>/plan_batch</a>.</p></body></html>")
    sched = plan.get("schedule", {})
    n = int(plan.get("step_min", step))
    n_rows = 96 if n == 15 else 24
    # časové popisky
    times = [f"{i//4:02d}:{(i%4)*15:02d}" if n == 15 else f"{i:02d}:00" for i in range(n_rows)]
    cols = [("Čas", times),
            ("FTV kWh", sched.get("pv_kwh", [0]*n_rows)),
            ("Cena €/MWh", sched.get("price_eur", [0]*n_rows)),
            ("Batéria kW", sched.get("batt_kw", [0]*n_rows)),
            ("Sieť kWh", sched.get("grid_kwh", [0]*n_rows)),
            ("Orezané kWh", sched.get("curtail_kwh", [0]*n_rows)),
            ("SOC %", sched.get("soc_pct", [50]*n_rows)),
            ("Obchod MWh", sched.get("order_mwh", [0]*n_rows))]
    rows = []
    for i in range(min(n_rows, len(times))):
        rs = []
        for j, (lab, arr) in enumerate(cols):
            v = arr[i] if i < len(arr) else 0
            if j == 0:
                rs.append(f"<td>{v}</td>")
            elif lab == "Batéria kW":
                col = "#2E7D32" if float(v) > 0.5 else ("#C49000" if float(v) < -0.5 else "#999")
                rs.append(f"<td style='color:{col};font-weight:600'>{float(v):+.1f}</td>")
            else:
                rs.append(f"<td>{float(v):+.2f}</td>" if isinstance(v, (int, float)) else f"<td>{v}</td>")
        rows.append("<tr>" + "".join(rs) + "</tr>")
    # parametre + summary
    params = plan.get("params", {})
    summary = plan.get("summary", {})
    param_rows = "".join(
        f"<tr><td>{k}</td><td><code>{v}</code></td></tr>"
        for k, v in sorted(params.items()) if not k.startswith("_"))
    summary_rows = "".join(
        f"<tr><td>{k}</td><td><b>{v}</b></td></tr>"
        for k, v in summary.items() if not isinstance(v, (list, dict)))
    # mults / rt_mask
    mult_active = sum(1 for x in (plan.get("mults") or []) if x is not None and abs(float(x) - 1.0) > 1e-6)
    rt_off = sum(1 for x in (plan.get("rt_mask") or []) if x is not None and float(x) < 0.5)
    body = f"""<style>
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
.box{{background:#f3f6fb;padding:10px 14px;border-radius:8px;margin:8px 0;font-size:13px}}
</style>
<h1>📋 Plán {date}</h1>
<div class="box">
<b>Krok:</b> {step} min &nbsp;•&nbsp; <b>Kind:</b> {kind} &nbsp;•&nbsp;
<b>Vygenerované:</b> {plan.get('generated_at','?')} &nbsp;•&nbsp;
<b>Zdroj:</b> {plan.get('meta',{}).get('source','?')} &nbsp;•&nbsp;
<b>Ceny:</b> {plan.get('meta',{}).get('price_kind','?')}<br>
<b>Aktívne overridy:</b> {mult_active} slotov × ≠ 1.00, {rt_off} 15-min slotov má RT OFF &nbsp;•&nbsp;
<b>Iba nabíjanie:</b> {plan.get('block_planned_discharge', False)} &nbsp;•&nbsp;
<b>Bias ZCO:</b> {plan.get('zco_bias_w', 0)} &nbsp;•&nbsp;
<b>RT freedom:</b> {plan.get('rt_freedom', True)}
</div>
<h2>Rozvrh</h2>
<div style="max-height:480px;overflow:auto"><table class="tbl-compact"><tr>{''.join(f'<th>{c[0]}</th>' for c in cols)}</tr>{''.join(rows)}</table></div>
<div class="cols">
<div><h2>Súhrn (ekonomika)</h2><table class="tbl-compact">{summary_rows or '<tr><td>(prázdne)</td></tr>'}</table></div>
<div><h2>Parametre plánu</h2><table class="tbl-compact">{param_rows or '<tr><td>(prázdne)</td></tr>'}</table></div>
</div>
<p style="margin-top:14px"><a href="/plan_batch">← Späť na batch</a> &nbsp;|&nbsp; <a href="/livesim">Živá simulácia</a></p>
"""
    return render_legacy_body(None, f"Plán {date} ({step}m, {kind})", body)


def _render_mult_warnings(summ) -> str:
    """Zobrazí pásik s warningmi z optimize_day (override_warnings) ak existujú."""
    warns = summ.get("override_warnings") if isinstance(summ, dict) else None
    active = summ.get("override_active") if isinstance(summ, dict) else 0
    if not warns and not active:
        return ""
    if not warns:
        return (f"<div style='background:#e8f5e9;border-radius:8px;padding:8px 12px;margin:8px 0;"
                f"color:#1B5E20;font-size:13px'>✓ Aplikovaných {active} ručných násobiteľov bez SOC konfliktu.</div>")
    items = "".join(f"<li>{w.get('msg','')}</li>" for w in warns[:12])
    more = f"<li>… a ďalších {len(warns)-12}</li>" if len(warns) > 12 else ""
    return (f"<div style='background:#fff3cd;border:1px solid #ffeaa7;border-radius:8px;padding:8px 12px;"
            f"margin:8px 0;color:#856404;font-size:13px'>"
            f"<b>⚠ Ručné násobitele narazili na SOC/grid limity ({len(warns)}× — pri reálnom behu sa to "
            f"prejaví ako RT odchýlka):</b><ul style='margin:6px 0 0 20px'>{items}{more}</ul></div>")


# Bug R5 (2026-06-06): Spinner overlay ZRUŠENÝ podľa žiadosti používateľa.
# Modul ostáva ako placeholder pre prípadný comeback — _OVERLAY_HTML konstanta
# zostáva (nevyužitá), ale @app.middleware NIE JE registrované.
# Pre dlhé operácie (Excel export, plan_batch) sú streaming responses a inline
# progress bars (napríklad /plan_batch streaming progress).
_OVERLAY_HTML = """
<style>
#_busy_ov{position:fixed;inset:0;background:rgba(255,255,255,.85);display:none;z-index:99999;align-items:center;justify-content:center;backdrop-filter:blur(2px)}
#_busy_ov.on{display:flex}
#_busy_ov .b{background:#fff;border:1px solid #d8dee5;border-radius:14px;padding:22px 30px;box-shadow:0 10px 32px rgba(31,78,120,.15);text-align:center;min-width:300px;max-width:90vw}
#_busy_ov .t{font-size:17px;color:#1F4E78;font-weight:600;margin-bottom:6px}
#_busy_ov .h{font-size:13px;color:#666;margin-bottom:14px;line-height:1.5}
#_busy_ov .bar{height:6px;background:#eef1f5;border-radius:3px;overflow:hidden}
#_busy_ov .bar .f{height:100%;background:linear-gradient(90deg,#1F4E78 0%,#2E7D32 50%,#1F4E78 100%);background-size:200% 100%;animation:_bp 1.4s linear infinite;width:100%}
@keyframes _bp{0%{background-position:200% 0}100%{background-position:-200% 0}}
#_busy_ov .e{font-size:11px;color:#999;margin-top:10px}
</style>
<div id="_busy_ov" role="status" aria-live="polite">
 <div class="b">
  <div class="t" id="_busy_t">Pracujem…</div>
  <div class="h" id="_busy_h">Trvá to obvykle pár sekúnd.</div>
  <div class="bar"><div class="f"></div></div>
  <div class="e">Ak to trvá viac ako minútu, môže to byť pomalý fetch z OTE/ČEPS. Skús stránku obnoviť (F5).</div>
 </div>
</div>
<script>
(function(){
 const ov=document.getElementById('_busy_ov'),T=document.getElementById('_busy_t'),H=document.getElementById('_busy_h');
 const show=(t,h)=>{T.textContent=t;H.textContent=h||'';ov.classList.add('on')};
 const hide=()=>{ov.classList.remove('on')};
 const lbl=u=>{
  if(!u)return['Pracujem…',''];
  u=String(u);
  // sub-action signály z form submit buttonov (?save_day, formaction atď.)
  if(u.indexOf('save_both')>=0)return['Ukladám a prepočítavam plán…','Per-day pre tento dátum + globálna šablóna. LP optimalizácia + post-process.'];
  if(u.indexOf('save_template')>=0||u.indexOf('save_day')>=0)return['Aplikujem násobiteľ a prepočítavam plán…','LP optimalizácia + post-process s SOC orezom.'];
  if(u.indexOf('clear_day')>=0||u.indexOf('clear_template')>=0)return['Mažem ručné nastavenia…',''];
  if(u.indexOf('/predict_tomorrow')>=0)return['Predikujem ceny na zajtra…','Tréning/predikcia modelu cien.'];
  if(u.indexOf('/sim_run')>=0)return['Bežím simuláciu (backtest)…','Pre každý deň plán + RT vrstva. Môže trvať 30 s – 3 min podľa rozsahu.'];
  if(u.indexOf('/livesim')>=0)return['Načítavam livesim…','Live fetch OTE/ČEPS + advance trace.'];
  if(u.indexOf('/dentrh')>=0)return['Počítam denný trh 15-min…','OTE day-ahead + LP. Obvykle 3–10 s.'];
  if(u.indexOf('/backfill')>=0)return['Backfill histórie…','Sťahujem OTE/ČEPS minútovú históriu. Môže trvať desiatky sekúnd.'];
  if(u.indexOf('/download')>=0)return['Pripravujem Excel…',''];
  if(u.indexOf('/case_')>=0)return['Ukladám prípad…',''];
  if(u.indexOf('/sim')>=0)return['Otváram simuláciu…',''];
  if(u.indexOf('/plan')>=0)return['Generujem plán D-1…','Predikcia FTV + ceny + LP optimalizácia. Trvá obvykle 5–15 s.'];
  if(u.indexOf('/rt')>=0)return['Načítavam RT poradcu…','Live signál + odporúčanie.'];
  return['Pracujem…','Trvá to obvykle pár sekúnd.'];
 };
 document.addEventListener('submit',e=>{
  const f=e.target;if(f.tagName!=='FORM')return;
  const act=f.getAttribute('action')||location.pathname;
  // ak je submit button s vlastným formaction, použiť ten
  const sb=document.activeElement;
  const url=(sb&&sb.formAction)?sb.formAction:act;
  const [t,h]=lbl(url+(sb&&sb.value?'?'+sb.value:''));show(t,h);
 });
 document.addEventListener('click',e=>{
  const a=e.target.closest('a');if(!a)return;
  const href=a.getAttribute('href');
  if(!href||href.startsWith('#')||href.startsWith('javascript:'))return;
  if(a.target==='_blank'||a.hasAttribute('download'))return;
  const [t,h]=lbl(href);show(t,h);
 });
 window.addEventListener('pageshow',hide);  // back/forward cache → skry
 window.addEventListener('error',hide);
})();
</script>
"""


# Bug R5: _inject_overlay middleware ZRUŠENÝ. Spinner sa už nezobrazuje.
# Pôvodný middleware injektoval _OVERLAY_HTML pred </body> každej HTML response.
# Užívateľ pripomenul že popup vyrušuje — pre dlhé operácie sú stačí streaming
# responses (/plan_batch) a inline progress bars (Excel export).
# Ak by sa middleware mal vrátit, pridať @app.middleware("http") nad funkciu.

                                          # (DEF, cache dicty, _model, _ote_cache_csv,
                                          #  _fetch_ote_cached, _isot_history, _fetch_pv_cached
                                          #  — vyrezané do core/caches.py — Fáza 1)


CAL_PATH = "out/pv_calibration.json"


def _calibration():
    """Načíta kalibráciu: dict s 'factor' (celkový) a 'by_month'. Prázdne ak neexistuje."""
    try:
        with open(CAL_PATH) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _cal_for(date):
    """Kalibračný faktor výroby pre daný dátum (mesačný, fallback celkový, inak 1.0)."""
    c = _calibration()
    if not c:
        return 1.0
    mth = date.strftime("%Y-%m")
    return float(c.get("by_month", {}).get(mth, c.get("factor", 1.0)))


def _calibration_factor():
    return float(_calibration().get("factor", 1.0))


# Port (env PORT, default 8000) → umožní bežať viac inštancií naraz.
# UI-stav (naposledy zadané hodnoty) je per-port, aby si sa inštancie navzájom neprepisovali;
# default (8000) ostáva pôvodný súbor kvôli spätnej kompatibilite.


def _autosave_active_profile():
    """Po submit /plan alebo /dentrh: ak je aktívny profil, prepíš ho aktuálnymi ui_settings.
    Týmto sa zmeny v /plan a /dentrh ihneď premietnu aj do profilu — žiadny manuálny snapshot."""
    try:
        import profiles as _pr
        active = _pr.get_active()
        if not active:
            return
        _pr.snapshot_current(active, _ui_load, po,
                              note=f"Auto-save po /plan|/dentrh {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    except Exception:
        pass


def _clear_livesim_logs():
    """Zmaže VŠETKY livesim_*.csv a .meta.json — pri zmene šablóny / load profile / parametrov
    sa živá simulácia auto-prepočíta od štartu. Bez tohto by čakal na settings_sig diff, čo užívateľ nevidí.

    Pokrýva legacy layout (`out/livesim_*`) aj multi-market (`out/<market>/livesim_*`)
    aj per-port súbory (`livesim_<case>_<port>.{csv,meta.json}`)."""
    import glob as _glob
    patterns = ["out/livesim_*", "out/*/livesim_*"]
    n = 0
    for pat in patterns:
        for _p in _glob.glob(pat):
            try:
                os.remove(_p)
                n += 1
            except OSError:
                pass
    # invalidate in-memory cache aby nasledujúce _read_csv prečítalo prázdny / nový súbor
    try:
        import livesim as _lsim
        _lsim._LIVESIM_CSV_CACHE.clear()
    except Exception:
        pass
    return n


def _read_production(raw: bytes, filename: str, unit: str):
    """Načíta nahraný súbor s 15-min výrobou → hodinová výroba [kWh]. Vráti (df, dtcol, valcol)."""
    if filename.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(raw))
    else:
        txt = raw.decode("utf-8", errors="replace")
        df = pd.read_csv(io.StringIO(txt), sep=None, engine="python")
    dtcol = None
    for c in df.columns:
        sample = df[c].dropna().astype(str).head(20)
        if sample.empty:
            continue
        iso = sample.str.match(r"^\s*\d{4}-\d{1,2}-\d{1,2}").mean() > 0.5   # YYYY-MM-DD?
        parsed = pd.to_datetime(df[c], errors="coerce", dayfirst=not iso)
        if parsed.notna().mean() > 0.8:
            dtcol = c; df[c] = parsed; break
    if dtcol is None:
        raise ValueError("Nenašiel som časový stĺpec (dátum a čas).")
    cand = [c for c in df.columns if c != dtcol]
    valcol = None
    for c in cand:
        if any(k in str(c).lower() for k in ["výrob", "vyrob", "produc", "kwh", "kw", "power", "výkon", "vykon"]):
            valcol = c; break
    if valcol is None:
        for c in cand:
            if pd.to_numeric(df[c].astype(str).str.replace(",", ".").str.replace(" ", ""),
                             errors="coerce").notna().mean() > 0.8:
                valcol = c; break
    if valcol is None:
        raise ValueError("Nenašiel som stĺpec výroby.")
    val = pd.to_numeric(df[valcol].astype(str).str.replace("\xa0", "").str.replace(" ", "").str.replace(",", "."),
                        errors="coerce")
    s = pd.DataFrame({"time": df[dtcol], "val": val}).dropna().set_index("time").sort_index()
    hourly = (s["val"].resample("h").mean() if unit == "kw"      # priemerný kW ≈ kWh/h
              else s["val"].resample("h").sum())                  # kWh za 15-min → súčet
    return hourly.rename("real_kwh").reset_index(), str(dtcol), str(valcol)


def _build_template_editor_html_plan():
    """Vykreslí 24-hodinový × a RT editor (= globálna šablóna pre aktívny profil).
    Pri každom submite /plan sa hodnoty uložia ako template. Per-day prepis tým zaniká."""
    try:
        m96 = po.load_template(kind="plan") if po is not None else np.full(96, np.nan)
        rt96 = po.load_template_rt(kind="plan") if po is not None else np.full(96, np.nan)
    except Exception:
        m96 = np.full(96, np.nan); rt96 = np.full(96, np.nan)
    m24 = []
    for h in range(24):
        seg = m96[h*4:(h+1)*4]
        seg_fin = seg[np.isfinite(seg)]
        m24.append(float(seg_fin.mean()) if len(seg_fin) > 0 else 1.0)
    rt24 = []
    for h in range(24):
        seg = rt96[h*4:(h+1)*4]
        rt24.append(all((not np.isfinite(v) or v > 0.5) for v in seg))
    cells = []
    for h in range(24):
        mv = m24[h]; ron = rt24[h]
        m_bg = "#fff7e6" if abs(mv - 1.0) > 1e-6 else "#fff"
        rt_bg = "#ffe2e2" if not ron else "#fff"
        cells.append(
            f"<div style='display:flex;gap:6px;align-items:center;padding:3px 6px;border:1px solid #ddd;border-radius:5px;background:#fafafa'>"
            f"<span style='font-weight:600;color:#1F4E78;min-width:46px;font-size:13px'>{h:02d}:00</span>"
            f"<input type='number' name='mult_arr' value='{mv:.2f}' step='0.05' min='-3' max='3' "
            f"style='width:58px;padding:2px;border:1px solid #ccc;border-radius:4px;text-align:right;background:{m_bg};font-size:13px'>"
            f"<label style='display:flex;gap:3px;align-items:center;font-size:12px;color:#555;background:{rt_bg};padding:1px 4px;border-radius:3px'>"
            f"RT<input type='checkbox' name='rt_arr' value='{h}'{' checked' if ron else ''}></label>"
            f"</div>"
        )
    grid = ("<div style='display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:8px 0'>"
            + "".join(cells) + "</div>")
    bulk = ("<div style='margin:8px 0;padding:6px 10px;background:#fff3cd;border:1px solid #ffe399;border-radius:6px;display:flex;gap:10px;flex-wrap:wrap;align-items:center'>"
            "<b style='color:#7a5d00;font-size:13px'>⚡ Hromadne:</b>"
            "<label style='font-size:13px'>× = "
            "<input type='number' id='tpl_bulk_mult' value='1.00' step='0.05' min='-3' max='3' style='width:60px;padding:2px;border:1px solid #ccc;border-radius:4px'></label>"
            "<button type='button' onclick=\"document.querySelectorAll('fieldset.tpl-editor input[name=mult_arr]').forEach(i=>{i.value=document.getElementById('tpl_bulk_mult').value;i.style.background='#fff7e6'});return false\" "
            "style='background:#1F4E78;color:#fff;border:0;padding:4px 10px;border-radius:5px;cursor:pointer;font-size:12px'>Aplikuj ×</button>"
            "<span style='color:#ccc'>│</span>"
            "<button type='button' onclick=\"document.querySelectorAll('fieldset.tpl-editor input[name=rt_arr]').forEach(i=>{i.checked=true;i.parentElement.style.background='#fff'});return false\" "
            "style='background:#2E7D32;color:#fff;border:0;padding:4px 10px;border-radius:5px;cursor:pointer;font-size:12px'>RT všetky ✓</button>"
            "<button type='button' onclick=\"document.querySelectorAll('fieldset.tpl-editor input[name=rt_arr]').forEach(i=>{i.checked=false;i.parentElement.style.background='#ffe2e2'});return false\" "
            "style='background:#C0392B;color:#fff;border:0;padding:4px 10px;border-radius:5px;cursor:pointer;font-size:12px'>RT všetky ✗</button>"
            "</div>")
    return bulk + grid


def form_page(msg=""):
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    f = _ui_load("plan", DEF)
    # Joint LP flags (per-profil) — defaults DEF + override z profilu aktívneho
    try:
        import joint_lp_integration as _jli
        import plan_store as _ps_jli
        _active_prof_jli = _ps_jli.resolve_profile() or "default"
        _jl_flags = _jli.get_flags_from_profile(_active_prof_jli)
    except Exception:
        _jl_flags = {"enabled": False, "trade_batt": True, "trade_ftv": True,
                     "trade_load": True, "use_vdt": False,
                     "optimize_distribution": False}
    # RT poradca 2.0: aktuálny engine + v2 parametre z profilu rt sekcie (pre formu)
    try:
        import profiles as _pr_rteF
        _rt_sec_F = ((_pr_rteF.load_profile(_active_prof_jli) or {}).get("rt") or {})
        _rt_engine_cur = str(_rt_sec_F.get("engine", "v1") or "v1")
        _rt2_mmin_cur = float(_rt_sec_F.get("rt2_margin_min_eur", 10.0) or 10.0)
        _rt2_mfull_cur = float(_rt_sec_F.get("rt2_margin_full_eur", 60.0) or 60.0)
        _rt2_zcok_cur = float(_rt_sec_F.get("rt2_zco_k", 0.6) or 0.6)
        _rt2_mchg_raw = _rt_sec_F.get("rt2_margin_min_chg_eur")
        _rt2_mchg_cur = "" if _rt2_mchg_raw is None else f"{float(_rt2_mchg_raw):g}"
    except Exception:
        _rt_engine_cur = "v1"
        _rt2_mmin_cur, _rt2_mfull_cur, _rt2_zcok_cur = 10.0, 60.0, 0.6
        _rt2_mchg_cur = ""
    # Distribučný poplatok — single source of truth.
    # Pole sa zobrazí 3 spôsobmi podľa stavu distribúcie v profile:
    #   • _dist_enabled=True  → readonly, vypočítaná hodnota (master toggle ON)
    #   • _dist_configured=True (ale OFF) → editovateľné + info banner s náhľadom
    #   • inak → klasické editovateľné pole s f['grid_fee']
    _dist_avg = None
    _dist_enabled = False
    _dist_configured = False     # config existuje (TPS/SS/OZE > 0) ale enabled=False
    _dist_profile_name = _active_prof_jli
    try:
        import distribution_cost as _dc_form
        _dc_cfg = _dc_form.get_config(_active_prof_jli)
        # avg fungujem iba pre enabled=True (default helper)
        if _dc_cfg.get("enabled"):
            _dist_avg = _dc_form.avg_eur_per_mwh(_dc_cfg)
            _dist_enabled = True
        else:
            # Skús vypočítať hodnotu aj pre vypnutý config (náhľad) — donútime enabled=True dočasne
            try:
                _tmp = dict(_dc_cfg); _tmp["enabled"] = True
                _avg_preview = _dc_form.avg_eur_per_mwh(_tmp)
                if _avg_preview and _avg_preview > 0.01:
                    _dist_avg = _avg_preview
                    _dist_configured = True
            except Exception:
                pass
    except Exception:
        pass
    def _chk(v):
        return "checked" if bool(v) else ""
    return f"""<!doctype html><html lang="sk"><head><meta charset="utf-8">
<title>{_APP_NAME} — Plán D-1</title><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin="">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
<style>body{{font-family:-apple-system,Segoe UI,Arial;max-width:1680px;margin:24px auto;padding:0 16px;color:#222}}
h1{{color:#1F4E78}} fieldset{{border:1px solid #e0e0e0;border-radius:10px;margin:12px 0;padding:12px 16px}}
legend{{color:#2E75B6;font-weight:600}} .cols{{display:grid;grid-template-columns:1fr 1fr;gap:0 24px}}
.cols3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0 20px}}
button{{background:#1F4E78;color:#fff;border:0;padding:10px 18px;border-radius:8px;font-size:15px;cursor:pointer}}
.msg{{color:#C00000}} #map{{height:340px;border-radius:8px;border:1px solid #ccc}}
.wx-card{{background:#f3f6fb;border-radius:8px;padding:10px 12px;font-size:13px}}
.wx-card b{{color:#1F4E78}} .wx-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:6px}}
.wx-day{{background:#fff;border:1px solid #e0e0e0;border-radius:6px;padding:6px;text-align:center;font-size:11px}}
.wx-day .d{{font-weight:600;color:#1F4E78}} .wx-day .t{{font-size:18px;color:#2E75B6;margin:2px 0}}
.wx-day.today{{border:2px solid #2E7D32;background:#eef7ee}}</style></head><body>
<h1>⚡ {_APP_NAME} — Plán D-1</h1>
{_nav("/")}
<p style="color:#666">Predpoveď počasia → odhad ISOT → optimálny rozvrh batérie a obchodná pozícia → Excel.</p>
{_overrides_status(tomorrow)}
{_stale_plans_banner()}
<p class="msg">{msg}</p>
<form method="post" action="/plan">
<fieldset><legend>Deň, lokácia a elektráreň</legend>
<div style="display:grid;grid-template-columns:1fr 1.4fr;gap:20px">
<div>
<div class="cols">
<label style="display:flex;justify-content:space-between;margin:4px 0;grid-column:1/-1"><span>Dátum</span>
<input id="plan_date" name="date" value="{tomorrow}" type="date" style="padding:4px;border:1px solid #ccc;border-radius:6px"></label>
{_field("Šírka (lat)","lat",f['lat'])}{_field("Dĺžka (lon)","lon",f['lon'])}
{_field("Výkon FTV [kWp]","kwp",f['kwp'])}{_field("Sklon [°]","tilt",f['tilt'])}
{_field("Azimut [° ,0=juh]","azimuth",f['azimuth'])}{_field("Účinnosť FTV","eff",f['eff'])}
</div>
<p style="color:#666;font-size:12px;margin:6px 0 0">💡 Kliknutím na mapu sa <b>lat/lon</b> vyplní automaticky. Po zmene polohy alebo dátumu sa aktualizuje aj prognóza počasia.</p>
</div>
<div>
<div style="display:flex;gap:6px;margin-bottom:6px;align-items:center;position:relative">
  <input id="map_search" type="text" placeholder="🔍 Vyhľadaj mesto / obec / adresu (napr. Olomouc, Praha 5, Bratislava)…"
    style="flex:1;padding:6px 10px;border:1px solid #ccc;border-radius:6px;font-size:14px" autocomplete="off">
  <button type="button" id="map_search_btn" style="padding:6px 14px;background:#1F4E78;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:13px">Hľadaj</button>
  <div id="map_search_results" style="position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid #ccc;border-radius:6px;max-height:220px;overflow:auto;display:none;z-index:1000;box-shadow:0 4px 10px rgba(0,0,0,.1)"></div>
</div>
<div id="map"></div>
<div id="wx_box" class="wx-card" style="margin-top:8px">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <b id="wx_now_title">Načítavam počasie…</b>
    <span id="wx_now_t" style="font-size:22px;color:#1F4E78"></span>
  </div>
  <div id="wx_now_detail" style="font-size:12px;color:#666;margin-top:2px"></div>
  <div class="wx-grid" id="wx_forecast"></div>
</div>
</div>
</div>
</fieldset>
<script>
(function(){{
  function getLat(){{ return parseFloat(document.querySelector('input[name=lat]').value)||49.0; }}
  function getLon(){{ return parseFloat(document.querySelector('input[name=lon]').value)||17.0; }}
  const map = L.map('map').setView([getLat(), getLon()], 9);
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom:18,attribution:'© OpenStreetMap'}}).addTo(map);
  let marker = L.marker([getLat(), getLon()], {{draggable:true}}).addTo(map);
  function setLatLon(lat, lon){{
    document.querySelector('input[name=lat]').value = lat.toFixed(4);
    document.querySelector('input[name=lon]').value = lon.toFixed(4);
    fetchWeather();
  }}
  map.on('click', e => {{
    marker.setLatLng(e.latlng);
    setLatLon(e.latlng.lat, e.latlng.lng);
  }});
  marker.on('dragend', () => {{
    const p = marker.getLatLng();
    setLatLon(p.lat, p.lng);
  }});
  // pri ručnom editovaní lat/lon input → presuň marker
  ['lat','lon'].forEach(n => {{
    const el = document.querySelector('input[name='+n+']');
    el.addEventListener('change', () => {{
      const lat = getLat(), lon = getLon();
      marker.setLatLng([lat,lon]);
      map.panTo([lat,lon]);
      fetchWeather();
    }});
  }});
  document.getElementById('plan_date').addEventListener('change', fetchWeather);

  // ── Nominatim search (OpenStreetMap geocoder, free) ──
  const searchInput = document.getElementById('map_search');
  const searchBtn = document.getElementById('map_search_btn');
  const resultsBox = document.getElementById('map_search_results');
  let searchAbort = null;
  async function doSearch(q){{
    q = (q||'').trim();
    if(!q){{ resultsBox.style.display='none'; return; }}
    if(searchAbort) searchAbort.abort();
    searchAbort = new AbortController();
    try {{
      const url = 'https://nominatim.openstreetmap.org/search?q=' + encodeURIComponent(q)
                + '&format=json&limit=6&accept-language=sk,cs,en&countrycodes=cz,sk,at,pl,de,hu';
      const r = await fetch(url, {{signal: searchAbort.signal, headers: {{'User-Agent':'FTV-planner-app'}}}});
      if(!r.ok) throw new Error('HTTP '+r.status);
      const items = await r.json();
      resultsBox.innerHTML = '';
      if(!items.length){{
        resultsBox.innerHTML = '<div style="padding:8px;color:#999">Žiadne výsledky</div>';
      }} else {{
        items.forEach(it => {{
          const div = document.createElement('div');
          div.style.cssText = 'padding:8px 12px;border-bottom:1px solid #eee;cursor:pointer;font-size:13px';
          div.innerHTML = '<b>'+(it.name||it.display_name.split(',')[0])+'</b><br>'
                        + '<span style="color:#666;font-size:11px">'+it.display_name+'</span><br>'
                        + '<span style="color:#999;font-size:10px">'+parseFloat(it.lat).toFixed(4)+', '+parseFloat(it.lon).toFixed(4)+' · '+(it.type||'')+'</span>';
          div.onmouseover = () => div.style.background='#eef';
          div.onmouseout = () => div.style.background='#fff';
          div.onclick = () => {{
            const lat = parseFloat(it.lat), lon = parseFloat(it.lon);
            marker.setLatLng([lat, lon]);
            map.setView([lat, lon], 12);
            setLatLon(lat, lon);
            resultsBox.style.display='none';
            searchInput.value = it.display_name.split(',').slice(0,2).join(', ');
          }};
          resultsBox.appendChild(div);
        }});
      }}
      resultsBox.style.display='block';
    }} catch(e){{
      if(e.name !== 'AbortError'){{
        resultsBox.innerHTML = '<div style="padding:8px;color:#c00">Chyba: '+e.message+'</div>';
        resultsBox.style.display='block';
      }}
    }}
  }}
  // debounce on input
  let debTimer = null;
  searchInput.addEventListener('input', () => {{
    if(debTimer) clearTimeout(debTimer);
    debTimer = setTimeout(() => doSearch(searchInput.value), 450);
  }});
  searchInput.addEventListener('keydown', e => {{
    if(e.key === 'Enter'){{ e.preventDefault(); doSearch(searchInput.value); }}
    if(e.key === 'Escape'){{ resultsBox.style.display='none'; }}
  }});
  searchBtn.addEventListener('click', () => doSearch(searchInput.value));
  // klik mimo → zatvor results
  document.addEventListener('click', e => {{
    if(!resultsBox.contains(e.target) && e.target !== searchInput && e.target !== searchBtn){{
      resultsBox.style.display='none';
    }}
  }});

  // ── Open-meteo počasie (current + 7-day forecast) ──
  function wxIcon(code){{
    if(code===0) return '☀️ jasno';
    if(code<=3) return '🌤 čiastočne oblačno';
    if(code<=48) return '🌫 hmla';
    if(code<=57) return '🌦 mrholenie';
    if(code<=67) return '🌧 dážď';
    if(code<=77) return '❄️ sneh';
    if(code<=82) return '🌧 prehánky';
    if(code<=99) return '⛈ búrka';
    return '?';
  }}
  async function fetchWeather(){{
    const lat = getLat(), lon = getLon();
    const sel = document.getElementById('plan_date').value;
    const url = 'https://api.open-meteo.com/v1/forecast'
      + '?latitude='+lat+'&longitude='+lon
      + '&current=temperature_2m,relative_humidity_2m,weather_code,cloud_cover,wind_speed_10m'
      + '&daily=weather_code,temperature_2m_max,temperature_2m_min,sunshine_duration,precipitation_sum,cloud_cover_mean'
      + '&forecast_days=7&past_days=2&timezone=Europe%2FBratislava';
    try {{
      const r = await fetch(url);
      if(!r.ok) throw new Error('HTTP '+r.status);
      const j = await r.json();
      const c = j.current||{{}};
      document.getElementById('wx_now_title').textContent = 'Aktuálne: '+wxIcon(c.weather_code);
      document.getElementById('wx_now_t').textContent = (c.temperature_2m||0).toFixed(1)+' °C';
      document.getElementById('wx_now_detail').textContent =
        'vlhkosť '+(c.relative_humidity_2m||0)+' % · oblačnosť '+(c.cloud_cover||0)+' % · vietor '+(c.wind_speed_10m||0).toFixed(1)+' m/s · ('+lat.toFixed(3)+', '+lon.toFixed(3)+')';
      const d = j.daily||{{}};
      const fc = document.getElementById('wx_forecast');
      fc.innerHTML = '';
      const today = new Date().toISOString().slice(0,10);
      for(let i=0;i<(d.time||[]).length;i++){{
        const dt = d.time[i];
        const div = document.createElement('div');
        div.className = 'wx-day' + (dt===sel ? ' today' : '');
        const sun_h = Math.round((d.sunshine_duration?.[i]||0)/3600);
        div.innerHTML = '<div class="d">'+dt.slice(5)+(dt===today?' (dnes)':'')+(dt===sel?' ★':'')+'</div>'
          + '<div class="t">'+(d.temperature_2m_max?.[i]||0).toFixed(0)+'°/'+(d.temperature_2m_min?.[i]||0).toFixed(0)+'°</div>'
          + '<div>'+wxIcon(d.weather_code?.[i])+'</div>'
          + '<div style="color:#666">☁ '+(d.cloud_cover_mean?.[i]||0).toFixed(0)+'% · ☀ '+sun_h+'h · 💧 '+(d.precipitation_sum?.[i]||0).toFixed(1)+'mm</div>';
        fc.appendChild(div);
      }}
    }} catch(e) {{
      document.getElementById('wx_now_title').textContent = 'Počasie sa nedá načítať: '+e.message;
    }}
  }}
  fetchWeather();
}})();
</script>
<fieldset><legend>Batéria a sieť</legend><div class="cols">
{_field("Výkon batérie [kW]","batt_kw",f['batt_kw'])}{_field("Kapacita [kWh]","batt_kwh",f['batt_kwh'])}
{_field("Účinnosť nabíjania","eff_c",f['eff_c'])}{_field("Účinnosť vybíjania","eff_d",f['eff_d'])}
{_field("SOC min [%]","soc_min",f['soc_min'])}{_field("SOC max [%]","soc_max",f['soc_max'])}
{_field("SOC začiatok [%]","soc_init",f['soc_init'])}{_field("SOC koniec [%]","terminal_soc",f['terminal_soc'])}{_field("SOC rezerva [%]","soc_reserve_pct",f.get('soc_reserve_pct', 0.0))}{_field("Sieť rezerva pre RT [%]","rt_grid_reserve_pct",f.get('rt_grid_reserve_pct', 0.0))}
{_field("Limit dodávky do siete [kW]","grid_kw_export",f.get('grid_kw_export', f['grid_kw']))}{_field("Limit odberu zo siete [kW]","grid_kw_import",f.get('grid_kw_import', f['grid_kw']))}
{(
  f'<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0">'
  f'<span>Poplatok odber [€/MWh] '
  f'<span style="background:#e6f4ea;color:#1B5E20;padding:1px 6px;border-radius:4px;font-size:11px;margin-left:4px">'
  f'auto z distribúcie ✓</span></span>'
  f'<input name="grid_fee" type="number" step="0.1" value="{_dist_avg:.2f}" readonly '
  f'style="padding:4px 8px;border:1px solid #ccc;border-radius:6px;width:120px;'
  f'background:#f5f5f5;color:#555;cursor:not-allowed" '
  f'title="Vypočítané z distribučnej konfigurácie (TOU+TPS+SS+OZE). '
  f'Edituj cez Profil → Distribučné tarify."></label>'
) if _dist_enabled else (
  f'<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0">'
  f'<span>Poplatok odber [€/MWh] '
  f'<span style="background:#fff3cd;color:#7a5d00;padding:1px 6px;border-radius:4px;font-size:11px;margin-left:4px" '
  f'title="Distribučné tarify sú v profile nastavené (priemer {_dist_avg:.2f} €/MWh) ale OFF master toggle">'
  f'⚠ distribúcia vypnutá</span></span>'
  f'<input name="grid_fee" type="number" step="0.1" value="{float(f["grid_fee"]):.2f}" '
  f'style="padding:4px 8px;border:1px solid #ccc;border-radius:6px;width:120px"></label>'
  f'<div style="margin:2px 0 8px;padding:6px 10px;background:#fff8e1;border-left:3px solid #f9a825;border-radius:4px;font-size:12px;color:#7a5d00">'
  f'💡 Máš nastavené distribučné tarify — priemer = <b>{_dist_avg:.2f} €/MWh</b>. '
  f'<a href="/profiles/edit?name={_dist_profile_name}" style="color:#1F4E78">Zapni v profile</a> '
  f'aby sa použili automaticky (single source of truth namiesto ručného poplatku).</div>'
) if _dist_configured else _field("Poplatok odber [€/MWh]","grid_fee",f['grid_fee'])}<input type="hidden" name="grid_kw" value="{max(float(f.get('grid_kw_import', f['grid_kw'])), float(f.get('grid_kw_export', f['grid_kw'])))}">
{_field("Náklad cyklu [€/MWh]","cycle_cost",f['cycle_cost'])}
{_field("Max DAM export [kWh/deň, 0=bez stropu]","max_export_kwh_day",f.get('max_export_kwh_day', 0))}{_field("Max DAM import [kWh/deň, 0=bez stropu]","max_import_kwh_day",f.get('max_import_kwh_day', 0))}
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0">
<span>Nabíjať zo siete</span><input name="allow_grid_charge" type="checkbox" checked></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0">
<span title="VYPNUTÉ (default) = LP voľne arbitrážuje pri DT &lt; 0 (zarobí na odbere zo siete). ZAPNUTÉ = LP nikdy neimportuje pri DT &lt; 0 (môže nechať zisk na stole — odporúča sa nechať vypnuté)">Blokovať nákup pri zápornej cene <span style="color:#888">(odporúča sa vypnuté)</span></span><input name="block_neg_import" type="checkbox" {"checked" if f.get("block_neg_import", False) else ""}></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0">
<span>Orezať/vypnúť FTV pri nevýhodnej cene</span><input name="allow_curtail" type="checkbox" {"checked" if f.get("allow_curtail", True) else ""}></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#1F4E78;background:#eef5e0;padding:4px 8px;border-radius:6px" title="D-1 plán bude IBA nabíjať batériu. Vybíjanie ostáva otvorené pre RT odchýlku — batéria sa vybije len keď ČEPS signál vyhodnotí výhodný okamih.">
<span><b>Iba D-1 nabíjanie</b> (vybíjanie len cez RT)</span><input name="no_planned_discharge" type="checkbox" {"checked" if f.get("no_planned_discharge", False) else ""}></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0" title="RT smie reagovať na sys_MW signál aj v slotoch kde D-1 plán = 0. Vypnutím obmedzíš RT iba na slotmi, kde plán aktívne nabíja/vybíja.">
<span>RT slobodná aj mimo D-1 slotov</span><input name="rt_freedom" type="checkbox" {"checked" if f.get("rt_freedom", True) else ""}></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#7a3500;background:#ffe8d0;padding:4px 8px;border-radius:6px" title="Vypne cycle budget pre RT odchýlku — RT je obmedzená iba SOC limitmi a max výkonom. Použiteľné keď chceš RT plný výkon na každý silný signál (max financny efekt).">
<span><b>⚡ Agresívne RT</b> (bez cycle budget — RT obmedzená len SOC a výkonom)</span><input name="aggressive_rt" type="checkbox" {"checked" if f.get("aggressive_rt", False) else ""}></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#1B5E20;background:#e6f4ea;padding:4px 8px;border-radius:6px" title="V slotoch s RT=✓: batéria sa snaží neutralizovať odchýlku prahu voči obchodnému plánu (FTV minútová realita ≠ hodinový plán). Pridáva sa na vrch existujúceho MW-signál RT enginu.">
<span><b>🌿 FTV balansovanie</b> (batéria kompenzuje threshold odchýlku vs obchod)</span><input name="ftv_balance" type="checkbox" {"checked" if f.get("ftv_balance", True) else ""}></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#1B5E20" title="Keď ON (default): FTV-balance pri VÝZNAMNEJ pre_dev (nad deadband) potlačí MW engine a drží plán. Pri šume pod deadband ide MW engine bežne (môže arbitrážovať). Keď OFF: FTV-balance fire iba pri opačných znakoch (= iba keď hrozí pokuta), bez ohľadu na veľkosť.">
<span>🎯 Striktné dodržanie plánu (pri významnej FTV odchýlke)</span><input name="ftv_strict_plan" type="checkbox" {"checked" if f.get("ftv_strict_plan", True) else ""}></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#1B5E20" title="Prah šumu pre strict_plan: |pre_dev| musí prekročiť túto hodnotu, aby strict potlačil MW engine. Malé šumové fluktuácie pod prah neblokujú arbitráž. Default 5 kW.">
<span>⛅ Deadband FTV odchýlky pre strict [kW]</span><input name="ftv_strict_deadband_kw" type="number" step="0.5" min="0" max="100" value="{f.get('ftv_strict_deadband_kw', 5.0)}" style="width:90px;padding:4px;border:1px solid #ccc;border-radius:6px"></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#1B5E20" title="Celý RT zásah (MW signal + FTV balance) pozrie N hodín dopredu na plán batérie. Plánované nabíjanie zmenší možnosť RT nabíjania teraz (rezerva kapacity), plánované vybíjanie zmenší RT vybíjanie (rezerva SOC). 0 = vypnúť lookahead. Default 4 h pokrýva typický D-1 plán arbitráže.">
<span>⏱ RT lookahead na plán [hodín]</span><input name="ftv_lookahead_h" type="number" step="0.5" min="0" max="12" value="{f.get('ftv_lookahead_h', 4.0)}" style="width:90px;padding:4px;border:1px solid #ccc;border-radius:6px"></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#1B5E20;background:#e6f4ea;padding:4px 8px;border-radius:6px" title="Audit RT cez SOC kapacitu pozrie N hodín dopredu na plán — ak v okolí je plánované nabíjanie, RT nabíjanie sa orezáva (chráni soc_max); ak je plánované vybíjanie, RT vybíjanie sa orezáva (chráni soc_min). Default 1.0 h = audit zachytí len najbližšiu hodinu plánu. Pre konzervatívnejšie nastavenie: 2–4 h. 0 = vypnuté (RT len cez okamžitú SOC kapacitu).">
<span>🛂 <b>Audit horizon [hodín]</b> (kapacita pre RT cez plán)</span><input name="rt_audit_horizon_h" type="number" step="0.5" min="0" max="12" value="{f.get('rt_audit_horizon_h', 1.0)}" style="width:90px;padding:4px;border:1px solid #ccc;border-radius:6px"></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#1B5E20" title="fixed = terminál dňa podľa poľa 'SOC koniec'. next_day_price = ak je zajtrajšie ráno (06-10 h, reálny DAM) drahšie než dnešný večer + breakeven round-tripu, terminál sa zdvihne na min(soc_max, 90 %) — energia sa podrží cez polnoc. LP je inak cez polnoc myopický.">
<span>🌅 <b>Terminál podľa zajtrajška</b> (terminal_soc_mode)</span><select name="terminal_soc_mode" style="padding:4px;border:1px solid #ccc;border-radius:6px">
<option value="fixed" {"selected" if f.get("terminal_soc_mode", "fixed") != "next_day_price" else ""}>fixed</option>
<option value="next_day_price" {"selected" if f.get("terminal_soc_mode") == "next_day_price" else ""}>next_day_price</option>
</select></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#1B5E20" title="VDT advisor si prah spreadu počíta z reálnych nákladov obchodu: cena × (1/η−1) + 2×fee + cycle_cost. Fixný 'min spread VDT' ostáva ako minimum. Bráni obchodom ziskovým len na papieri (bez strát účinnosti).">
<span>⚖️ <b>VDT breakeven auto</b> (prah z účinnosti + fees)</span><input name="vdt_breakeven_auto" type="checkbox" {"checked" if f.get("vdt_breakeven_auto", False) else ""}></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#1B5E20" title="Explicitná rezerva výkonu batérie pre VDT/RT: D-1 LP nominuje max (batt_kw − rezerva) v oboch smeroch. Nahrádza denný kWh strop ako nástroj delenia kapacity DAM vs intraday. 0 = bez rezervy.">
<span>🪫 <b>VDT kapacitná rezerva [kW]</b> (headroom pre intraday)</span><input name="vdt_capacity_reserve_kw" type="number" step="any" min="0" value="{f.get('vdt_capacity_reserve_kw', 0.0)}" style="width:90px;padding:4px;border:1px solid #ccc;border-radius:6px"></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#1B5E20" title="LEN PRE HISTÓRIU: vo zvolenom rozsahu dní sa VDT obchody ocenia reálnymi OKTE VDT UZAVRETÝMI cenami (value pre nákup aj predaj, len cross-slot arbitráž), namiesto živých paper trades. Reálny dnešok + budúcnosť ostávajú na klasickej živej simulácii. Prázdne = vypnuté.">
<span>📅 <b>VDT uzavreté ceny — rozsah dní</b> (len história)</span><span style="display:flex;gap:6px"><input name="vdt_closed_from" type="date" value="{f.get('vdt_closed_from','')}" style="padding:4px;border:1px solid #ccc;border-radius:6px"><input name="vdt_closed_to" type="date" value="{f.get('vdt_closed_to','')}" style="padding:4px;border:1px solid #ccc;border-radius:6px"></span></label>
<div style="margin:10px 0;padding:10px 12px;border:2px solid #5b7fb5;border-radius:10px;background:#f4f8ff">
<div style="font-weight:700;color:#1F4E78;margin-bottom:6px" title="Voľba RT enginu + jeho parametre na jednom mieste. Ukladá sa do rt sekcie profilu.">🤖 RT poradca — engine a parametre</div>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#1B5E20" title="v1 = signálová heuristika (kdis/kchg/margin/dtk). v2 = ekonomický: E[ZCO] z kalibrovaného spreadu vs prahy marže. v3 = marginálna hodnota energie: predaj/nákup teraz len ak E[ZCO] prekoná najlepšiu BUDÚCU alternatívu (committed plán, voľné okná zvyšku dňa, RT maska) — výkon vyplynie z objemu energie s kladnou maržou. Fyzické ochrany (SOC/grid/audit) platia pre všetky.">
<span><b>Engine</b></span><select name="rt_engine" style="padding:4px;border:1px solid #ccc;border-radius:6px">
<option value="v1" {"selected" if _rt_engine_cur not in ("v2", "v3") else ""}>v1 — signálový</option>
<option value="v2" {"selected" if _rt_engine_cur == "v2" else ""}>v2 — ekonomický</option>
<option value="v3" {"selected" if _rt_engine_cur == "v3" else ""}>v3 — hodnota energie (nový)</option>
</select></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#33506e" title="v2+v3: minimálna čistá marža €/MWh, pod ktorou RT nezasahuje. Vyššie = konzervatívnejšie. SK šum spreadu je ~±26 € → odporúčané 15-25.">
<span>↳ prah marže [€/MWh]</span><input name="rt2_margin_min_eur" type="number" step="any" min="0" value="{_rt2_mmin_cur:g}" style="width:90px;padding:4px;border:1px solid #ccc;border-radius:6px"></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#33506e" title="v2+v3: samostatný (vyšší) prah pre NABÍJANIE. Prázdne = použije sa spoločný prah. Nabíjacie zásahy majú menšiu istotu (plán sa intradenne nereoptimalizuje) — odporúčané 30-50.">
<span>↳ prah nabíjania [€/MWh] <span style="color:#888">(prázdne = spoločný)</span></span><input name="rt2_margin_min_chg_eur" type="number" step="any" min="0" value="{_rt2_mchg_cur}" placeholder="—" style="width:90px;padding:4px;border:1px solid #ccc;border-radius:6px"></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#33506e" title="LEN v2: marža €/MWh pri ktorej ide RT na plný výkon (lineárna rampa od prahu). v3 výkon neodvodzuje z marže, ale z objemu energie s kladnou maržou — toto pole ignoruje.">
<span>↳ v2: plný výkon pri marži [€/MWh]</span><input name="rt2_margin_full_eur" type="number" step="any" min="1" value="{_rt2_mfull_cur:g}" style="width:90px;padding:4px;border:1px solid #ccc;border-radius:6px"></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#33506e" title="v2+v3: fallback citlivosť €/MWh za MW signálu — použije sa LEN keď kalibrácia z imbalance_history nie je dostupná (inak kalibrovaný spread).">
<span>↳ fallback slope [€/MWh za MW]</span><input name="rt2_zco_k" type="number" step="any" min="0" value="{_rt2_zcok_cur:g}" style="width:90px;padding:4px;border:1px solid #ccc;border-radius:6px"></label>
</div>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#1B5E20" title="Keď systémový signál pretrváva v jednom smere (príležitostí je veľa), agresivita FTV-balance sa zníži (~50 % pri silnej persistencii). Nechá priestor pre plán a iné zásahy.">
<span>🌊 Persistencia signálu throttle</span><input name="ftv_persistence_throttle" type="checkbox" {"checked" if f.get("ftv_persistence_throttle", True) else ""}></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#1B5E20;background:#e6f4ea;padding:4px 8px;border-radius:6px" title="RT zásah (MW signal + FTV balance) nesmie nikdy zhoršiť threshold odchýlku voči obchodnému plánu. Keď FTV nedoposlúchne plán (under-deliver, pre_dev<0), RT nesmie batériu nabíjať navyše; keď FTV preteká (over-deliver), RT nesmie ďalej vybíjať. Plán adherence má prednosť pred MW signal arbitrážou.">
<span>🛡 <b>RT nesmie zhoršovať threshold odchýlku</b> (plán má prednosť pred arbitrážou)</span><input name="rt_no_worsen_dev" type="checkbox" {"checked" if f.get("rt_no_worsen_dev", True) else ""}></label>
</div></fieldset>
<fieldset class="tpl-editor"><legend>× a RT šablóna (pre celý profil)</legend>
<p style="color:#666;font-size:13px;margin:0 0 6px">Hodnoty per hodinu sa uložia ako <b>globálna šablóna pre aktívny profil</b> pri každom <b>Generuj plán D-1</b>. Šablóna platí pre VŠETKY dni — nie je rozdielna v rôznych dňoch.<br>
<b>×</b> = násobiteľ návrhu optimizéra (1.00 = bez zmeny, 0 = zablokovať slot, 0.5 = polovičný výkon, 1.5 = posilniť 50 %). <b>RT</b> = ✓ povolí odchýlkovú regulácia v slote, ✗ ju zablokuje.</p>
{_build_template_editor_html_plan()}
</fieldset>
<fieldset><legend>Plánovanie a korekcie</legend><div class="cols">
{_field("Min. cenový rozdiel [€/MWh]","min_spread",f['min_spread'])}
{_field("Min. veľkosť obchodu [MWh]","min_trade",f['min_trade'])}
{_field("Korekcia ceny (×)","price_scale",f['price_scale'])}
{_field("Korekcia výroby (×)","pv_scale",f['pv_scale'])}
{_field("Bias plánu o ZCO (váha 0–1)","zco_bias_w",f.get('zco_bias_w', 0.0))}
</div>
<p style="color:#666;font-size:13px;margin:6px 0 0">Min. cenový rozdiel = batéria cykluje len keď arbitráž prekročí tento prah. Korekcie = vynásobenie predikovanej ceny/výroby (na doladenie podľa reality). <b>Bias plánu o ZCO</b> = pri rozhodovaní upraví cenu o očakávanú odchýlku z deviation_profile.json (0 = vypnuté, 0,5 = optimizer vie že RT bude vybíjať v deficite a neplánuje tam vybíjanie). Zúčtovanie ostáva na reálnej DT.</p></fieldset>
<fieldset><legend>Baseline (bez batérie a plánu)</legend>
<p style="color:#666;font-size:13px;margin:0 0 6px">Pre porovnanie sa spočíta scenár <b>bez batérie a bez plánu</b> (FTV najprv pokryje load, zostatok obchoduje za zvolenú cenu). Rozdiel oproti reálnemu výsledku ukáže <b>prínos batérie + plánovania</b>.</p>
<div class="cols">
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;gap:8px">
  <span>Odber (import zo siete)</span>
  <span style="display:flex;gap:4px">
    <select name="baseline_im_mode" style="padding:4px;border:1px solid #ccc;border-radius:5px">
      <option value="dt_x"{' selected' if f.get('baseline_im_mode', 'dt_x') == 'dt_x' else ''}>DT × multiplier</option>
      <option value="fix"{' selected' if f.get('baseline_im_mode', 'dt_x') == 'fix' else ''}>Pevná cena €/MWh</option>
    </select>
    <input name="baseline_im_value" type="number" step="0.01" value="{f.get('baseline_im_value', 1.0)}" style="width:80px;padding:4px;border:1px solid #ccc;border-radius:5px;text-align:right">
  </span>
</label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;gap:8px">
  <span>Dodávka (export do siete)</span>
  <span style="display:flex;gap:4px">
    <select name="baseline_ex_mode" style="padding:4px;border:1px solid #ccc;border-radius:5px">
      <option value="dt_x"{' selected' if f.get('baseline_ex_mode', 'dt_x') == 'dt_x' else ''}>DT × multiplier</option>
      <option value="fix"{' selected' if f.get('baseline_ex_mode', 'dt_x') == 'fix' else ''}>Pevná cena €/MWh</option>
    </select>
    <input name="baseline_ex_value" type="number" step="0.01" value="{f.get('baseline_ex_value', 1.0)}" style="width:80px;padding:4px;border:1px solid #ccc;border-radius:5px;text-align:right">
  </span>
</label>
</div>
<p style="color:#666;font-size:13px;margin:6px 0 0">Príklady: <code>DT × 1.05</code> = clearing + 5 % markup dodávateľa; <code>DT × 1.0</code> = čistá trhová cena; <code>Pevná 90 €/MWh</code> = fixná tarifa nezávislá od DT.</p>
</fieldset>
<fieldset><legend>Batch generovanie (voliteľne)</legend>
<div class="cols">
<label style="display:flex;justify-content:space-between;gap:8px;margin:4px 0"><span>Od (vrátane)</span>
<input name="from_date" value="{dt.date.today().isoformat()}" type="date" style="padding:4px;border:1px solid #ccc;border-radius:6px"></label>
<label style="display:flex;justify-content:space-between;gap:8px;margin:4px 0"><span>Do (vrátane)</span>
<input name="to_date" value="{(dt.date.today() + dt.timedelta(days=2)).isoformat()}" type="date" style="padding:4px;border:1px solid #ccc;border-radius:6px"></label>
</div>
<input type="hidden" name="step_min" value="60">
<input type="hidden" name="kind" value="plan">
<p style="color:#666;font-size:13px;margin:6px 0 0">Tlačidlo <b>"Generovať BATCH"</b> uloží vyššie zadané nastavenia AJ vygeneruje plán pre každý deň v rozsahu od–do. Použije rovnaké hodnoty ako pre jednodenný plán.</p>
</fieldset>

<fieldset style="background:#fff8e1;border:2px solid #f9a825">
<legend style="color:#e65100">🧠 Joint LP (experimentálne) — koherentné plánovanie</legend>
<p style="color:#666;font-size:13px;margin:0 0 8px">
Po zapnutí <b>Joint LP</b> sa namiesto pôvodného `optimize_day` použije jeden veľký LP, ktorý plánuje FTV+Batt+Load+DAM(+VDT) <b>spoločne</b>. Toggle-y určujú, čo sa zapája do obchodu.<br>
<i style="color:#999">Vypnuté = pôvodný optimizer (default, otestované).</i>
</p>
<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:6px 18px;margin-top:4px">
<label style="display:flex;align-items:center;gap:6px;font-weight:600;color:#e65100">
  <input type="checkbox" name="joint_lp_enabled" value="1" {_chk(_jl_flags.get('enabled'))}>
  🔵 Zapnúť Joint LP (master toggle)
</label>
<label style="display:flex;align-items:center;gap:6px;color:#555">
  <input type="checkbox" name="joint_trade_batt" value="1" {_chk(_jl_flags.get('trade_batt'))}>
  🔋 Obchodovať batériu (charge/discharge cez trh)
</label>
<label style="display:flex;align-items:center;gap:6px;color:#555">
  <input type="checkbox" name="joint_trade_ftv" value="1" {_chk(_jl_flags.get('trade_ftv'))}>
  ☀️ Obchodovať FTV (predávať export do siete)
</label>
<label style="display:flex;align-items:center;gap:6px;color:#555">
  <input type="checkbox" name="joint_trade_load" value="1" {_chk(_jl_flags.get('trade_load'))}>
  🏭 Obchodovať spotrebu (nakupovať na load)
</label>
<label style="display:flex;align-items:center;gap:6px;color:#555">
  <input type="checkbox" name="joint_use_vdt" value="1" {_chk(_jl_flags.get('use_vdt'))}>
  💱 VDT extras (BUY/SELL nad DAM)
</label>
<label style="display:flex;align-items:center;gap:6px;color:#555">
  <input type="checkbox" name="joint_optimize_dist" value="1" {_chk(_jl_flags.get('optimize_distribution'))}>
  ⚡ Optimalizovať distribučné náklady (TOU)
</label>
</div>
<p style="color:#888;font-size:11px;margin:8px 0 0;font-style:italic">
Distribučné tarify (TOU sadzby €/MWh) sa pridajú v F3. Zatiaľ ich treba mať v profile manuálne.
</p>
</fieldset>
<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:8px">
<button type="submit">Generuj plán D-1 (jeden deň)</button>
<button type="submit" name="save_only" value="1" style="background:#2E7D32" title="Uloží ui_settings + × a RT šablónu do aktívneho profilu, BEZ generovania plánu. Použité keď chceš len zmeniť parametre profilu.">💾 Uložiť do profilu (bez generovania)</button>
<button type="submit" formaction="/plan_batch" style="background:#5E35B1">📦 Generovať BATCH (od–do)</button>
</div>
</form></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return form_page()


def _mpc_section_for_profile(profile: str, plan_params: dict) -> str:
    """Bug CC7: MPC tick info pre Manager dashboard card.

    Ak joint_mpc_enabled=True, načíta out/{market}/mpc_tick_{profile}.json a
    zobrazí: batt setpoint TERAZ + objective € pre zostávajúce sloty + last ts.
    Inak: ikona "MPC vypnuté".
    """
    enabled = bool(plan_params.get("joint_mpc_enabled", False))
    if not enabled:
        return (
            '<div style="font-size:11px;color:#999;text-align:center;'
            'background:#fafafa;border-radius:6px;padding:4px 8px;margin-bottom:8px">'
            '⏸ Joint MPC vypnuté (toggle v profile.plan)</div>'
        )
    try:
        import mpc_controller as _mpc
        cache = _mpc.load_cache(profile) or {}
    except Exception:
        cache = {}
    if not cache or not cache.get("ok"):
        return (
            '<div style="font-size:11px;color:#F57F17;background:#FFF8E1;'
            'border-left:3px solid #F57F17;border-radius:4px;padding:5px 8px;margin-bottom:8px">'
            '⏳ MPC: čaká na prvý tick alebo failed</div>'
        )
    sp_kw = float(cache.get("mpc_batt_kw_now", 0.0) or 0.0)
    obj_eur = float(cache.get("mpc_objective_eur", 0.0) or 0.0)
    cur_slot = int(cache.get("current_slot_idx", 0))
    ts = str(cache.get("ts", ""))[11:16]
    flags = cache.get("diagnostics", {}).get("flags", {})
    # Direction label
    if sp_kw > 1.0:
        dir_label = "VYBÍJAŤ"
        dir_color = "#2E7D32"
    elif sp_kw < -1.0:
        dir_label = "NABÍJAŤ"
        dir_color = "#1976D2"
    else:
        dir_label = "idle"
        dir_color = "#999"
    flag_chips = []
    for k, label in (("trade_batt", "B"), ("trade_ftv", "F"),
                     ("trade_load", "L"), ("use_vdt", "V")):
        on = bool(flags.get(k, True))
        flag_chips.append(
            f'<span style="background:{("#2E7D32" if on else "#999")};color:#fff;'
            f'padding:1px 4px;border-radius:3px;font-size:9px;font-weight:600;'
            f'margin-right:2px" title="{k}">{label}</span>'
        )
    return (
        '<div style="background:#E8F5E9;border-left:3px solid #2E7D32;'
        'border-radius:5px;padding:6px 10px;margin-bottom:8px">'
        '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px">'
        f'<span style="font-size:11px;color:#1B5E20;font-weight:600">🤖 Joint MPC</span>'
        f'<span style="font-size:10px;color:#666">{"".join(flag_chips)}</span>'
        f'<span style="font-size:10px;color:#999;font-family:monospace">{ts}</span>'
        '</div>'
        '<div style="display:flex;justify-content:space-between;margin-top:3px">'
        f'<span style="font-size:12px;color:#444">Setpoint: '
        f'<b style="color:{dir_color}">{sp_kw:+.0f} kW {dir_label}</b></span>'
        f'<span style="font-size:12px;color:#444">Zvyšok dňa: '
        f'<b style="color:#2E7D32">{obj_eur:+.0f} €</b></span>'
        '</div></div>'
    )


@app.get("/manager", response_class=HTMLResponse)
def manager_dashboard():
    try:
        return _manager_dashboard_impl()
    except Exception as ex:
        import traceback as _tb
        err = _tb.format_exc()
        body = (
            f'<div style="max-width:900px;margin:24px auto;padding:20px;background:#fff3e0;'
            f'border-left:4px solid #C62828;border-radius:8px;font-family:monospace">'
            f'<h2 style="color:#C62828">Manager dashboard chyba</h2>'
            f'<p>Render zlyhal s výnimkou. Stack trace nižšie môžeš poslať developerovi.</p>'
            f'<pre style="background:#fff;padding:12px;overflow:auto;font-size:11px">{err}</pre>'
            f'<p><a href="/manager">↻ Skús znova</a> · <a href="/profiles">⚙ Profily</a></p>'
            f'</div>'
        )
        return render_legacy_body(None, "Manager — chyba", body)


def _manager_dashboard_impl():
    """Bug T1: Manager dashboard — fleet command center.

    Layout:
      - HORE: 2 zdieľané grafy (MT signál + DT ceny — raz pre celý fleet)
      - PER BG-ON PROFIL: card s
          * Header (názov + mode + bg + link "Detail →")
          * KPI tile riadok (Zisk SPOLU, FTV teraz, Batt reálna, SOC, Zisk dnes)
          * Mini-graf "Riadenie batérie" (plán + realita + SOC)

    Mobile responsive: CSS grid auto-fit minmax(460px, 1fr).
    Klik na kartu (header) → /dashboard?profile=X.
    Auto-refresh 60 s.
    """
    import html as _html
    import datetime as _dt
    import json as _json
    try:
        import profiles as _pr
        all_profs = _pr.list_profiles() or []
    except Exception:
        all_profs = []
    try:
        import auto_control as _ac
        bg_enabled = _ac.get_enabled_profiles()
    except Exception:
        bg_enabled = set()
    try:
        import vdt_live_advisor as _adv
    except Exception:
        _adv = None
    try:
        import plan_store as _ps
    except Exception:
        _ps = None
    try:
        import realio as _rio
    except Exception:
        _rio = None
    # Cache realio latest values pre celu fleet (1 call namiesto N — realio nema per-profile rozlisenie)
    realio_latest = None
    if _rio:
        try:
            realio_latest = _rio.fetch_latest_all() or {}
        except Exception:
            realio_latest = None

    today = _dt.date.today()
    today_iso = today.isoformat()
    now_hh = _dt.datetime.now().hour + _dt.datetime.now().minute / 60.0

    # === 1. Zdieľané dáta hore: DT ceny + MT signál ===
    # DT ceny CZ — z OTE cache (DataFrame s columns date/interval/cena_EUR)
    dt_labels: list = []
    dt_cz_vals: list = []
    try:
        from core.caches import _fetch_ote_cached
        dt_df = _fetch_ote_cached(today)
        if dt_df is not None and not dt_df.empty:
            for _, row in dt_df.iterrows():
                try:
                    iv = str(row.get("interval", ""))
                    eur = float(row.get("cena_EUR", 0.0))
                    # interval typu "00:00-00:15" — vezmi začiatok
                    start = iv.split("-")[0].strip() if "-" in iv else iv
                    dt_labels.append(start)
                    dt_cz_vals.append(eur)
                except Exception:
                    pass
    except Exception:
        pass

    # MT signál — SEPS (SK) recent reg.výkon
    mt_labels: list = []
    mt_vals_sys: list = []
    try:
        from seps_sk import load_seps_mw_for_day as _seps
        mt_df = _seps(today_iso)
        if mt_df is not None and not mt_df.empty and "mw" in mt_df.columns:
            # decimácia: každá 5. minúta (288 bodov / deň)
            step = max(1, len(mt_df) // 288)
            sample = mt_df.iloc[::step]
            for _, row in sample.iterrows():
                try:
                    ts = row.get("ts_local")
                    if hasattr(ts, "strftime"):
                        mt_labels.append(ts.strftime("%H:%M"))
                    else:
                        mt_labels.append(str(ts)[11:16])
                    mw = row.get("mw")
                    if mw is None or (isinstance(mw, float) and (mw != mw)):  # NaN
                        mt_vals_sys.append(None)
                    else:
                        mt_vals_sys.append(float(mw))
                except Exception:
                    pass
    except Exception:
        pass

    # === 2. Per-profil dáta — IBA BG ON (live) ===
    profile_cards = []
    bg_count = len(bg_enabled)
    total_vdt_eur = 0.0
    live_profs = sorted([n for n in all_profs if n in bg_enabled])

    for name in live_profs:
        try:
            p_data = _pr.load_profile(name) or {}
        except Exception:
            p_data = {}
        mode = (p_data.get("mode") or "simulation").lower()
        plan_params = p_data.get("plan") or {}
        batt_kwh = float(plan_params.get("batt_kwh", 800.0))
        bg_on = (name in bg_enabled)
        if bg_on:
            bg_count += 1

        # VDT cache
        soc_now = None
        vdt_eur = 0.0
        adv_ts = ""
        full_plan = []
        if _adv:
            try:
                cache = _adv.load_cache(profile=name) or {}
                state = (cache.get("state") or {})
                soc_now = state.get("current_soc_pct")
                vdt_eur = float(state.get("vdt_realized_eur", 0.0) or 0.0)
                adv_ts = str(cache.get("ts", ""))[:16]
                full_plan = cache.get("full_plan") or []
            except Exception:
                pass
        total_vdt_eur += vdt_eur

        # Realio meranie pre real profile (zdielany cache pre vsetky)
        ftv_now_kw = None
        batt_real_kw = None
        if mode == "real" and realio_latest:
            try:
                ftv_now_kw = realio_latest.get("FTV_kW") or realio_latest.get("FVE1_C_Power")
                batt_real_kw = realio_latest.get("ESS1_C_Power") or realio_latest.get("BAT_kW")
                if soc_now is None:
                    soc_now = realio_latest.get("SOC_pct") or realio_latest.get("ESS1_C_SOC")
            except Exception:
                pass

        # Mini-graf data: SOC trajectory + batt action
        soc_path = []
        batt_path = []
        slot_labels = []
        for p_slot in (full_plan[:96] if full_plan else []):
            try:
                slot_labels.append(str(p_slot.get("slot", ""))[:5])
                soc_path.append(float(p_slot.get("soc_after_pct", 0) or 0))
                kwh = float(p_slot.get("kwh", 0) or 0)
                act = str(p_slot.get("action", "idle"))
                # convert kWh / 0.25 h = kW; charge = negative (input), discharge = positive
                kw = (kwh / 0.25)
                if act == "charge":
                    batt_path.append(-kw)
                elif act == "discharge":
                    batt_path.append(kw)
                else:
                    batt_path.append(0.0)
            except Exception:
                batt_path.append(0.0)
                soc_path.append(0.0)

        # Plán pre dnes?
        has_plan_today = False
        if _ps:
            try:
                for step, kind in ((60, "plan"), (15, "dentrh")):
                    if _ps.has_plan(today_iso, step, kind, profile=name):
                        has_plan_today = True
                        break
            except Exception:
                pass

        # === Render card ===
        canvas_id = f"chart_{name.replace('-', '_').replace(' ', '_')}"
        mode_chip = ('<span style="background:#C62828;color:#fff;padding:3px 9px;'
                     'border-radius:6px;font-size:11px;font-weight:600">🔴 real</span>'
                     if mode == "real" else
                     '<span style="background:#2E7D32;color:#fff;padding:3px 9px;'
                     'border-radius:6px;font-size:11px;font-weight:600">🟢 sim</span>')
        bg_chip = ('<span style="background:#1F88E5;color:#fff;padding:3px 9px;border-radius:6px;'
                   'font-size:11px;font-weight:600">🔵 BG ON</span>'
                   if bg_on else
                   '<span style="background:#bbb;color:#fff;padding:3px 9px;border-radius:6px;'
                   'font-size:11px;font-weight:600">⚪ BG OFF</span>')
        plan_dot = ('<span style="color:#2E7D32" title="Plán pre dnes existuje">●</span>'
                    if has_plan_today else
                    '<span style="color:#C62828" title="Plán chýba">●</span>')

        def _fmt(v, suffix="", decimals=1):
            if v is None:
                return '<span style="color:#999">—</span>'
            try:
                return f'{float(v):.{decimals}f}{suffix}'
            except Exception:
                return '<span style="color:#999">—</span>'

        soc_color = ("#2E7D32" if (soc_now is not None and 20 <= soc_now <= 80)
                     else "#C62828" if (soc_now is not None and (soc_now <= 5 or soc_now >= 95))
                     else "#F57F17")
        eur_color = "#2E7D32" if vdt_eur >= 0 else "#C62828"

        profile_cards.append(
            f'<div class="profile-card" style="background:#fff;border-radius:12px;padding:14px 16px;'
            f'box-shadow:0 2px 6px rgba(0,0,0,.07);border-top:3px solid {"#C62828" if mode == "real" else "#2E7D32"}">'
            # Header
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:8px;flex-wrap:wrap">'
            f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
            f'<a href="/dashboard?profile={name}" style="font-size:16px;font-weight:700;color:#1F4E78;text-decoration:none">{_html.escape(name)} →</a>'
            f'{mode_chip}{bg_chip}{plan_dot}'
            f'</div>'
            f'<span style="font-size:10px;color:#999;font-family:monospace">{_html.escape(adv_ts) if adv_ts else "—"}</span>'
            f'</div>'
            # KPI tiles
            f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(85px,1fr));gap:6px;margin-bottom:10px">'
            f'<div style="background:#f5f7fb;padding:6px 8px;border-radius:6px"><div style="font-size:10px;color:#777">SOC</div>'
            f'<div style="font-size:18px;font-weight:700;color:{soc_color}">{_fmt(soc_now, "%", 1)}</div></div>'
            f'<div style="background:#f5f7fb;padding:6px 8px;border-radius:6px"><div style="font-size:10px;color:#777">Zisk dnes</div>'
            f'<div style="font-size:18px;font-weight:700;color:{eur_color}">{vdt_eur:+.1f} €</div></div>'
            f'<div style="background:#f5f7fb;padding:6px 8px;border-radius:6px"><div style="font-size:10px;color:#777">FTV teraz</div>'
            f'<div style="font-size:18px;font-weight:700;color:#1F4E78">{_fmt(ftv_now_kw, " kW", 0)}</div></div>'
            f'<div style="background:#f5f7fb;padding:6px 8px;border-radius:6px"><div style="font-size:10px;color:#777">Batt reálna</div>'
            f'<div style="font-size:18px;font-weight:700;color:#1F4E78">{_fmt(batt_real_kw, " kW", 0)}</div></div>'
            f'<div style="background:#f5f7fb;padding:6px 8px;border-radius:6px"><div style="font-size:10px;color:#777">Kapacita</div>'
            f'<div style="font-size:18px;font-weight:700;color:#666">{batt_kwh:.0f} kWh</div></div>'
            f'</div>'
            # Bug CC7: MPC sekcia — ak je joint_mpc_enabled pre profil, načítaj mpc_tick.json
            f'{_mpc_section_for_profile(name, plan_params)}'
            # Mini chart canvas
            f'<div style="height:160px;position:relative"><canvas id="{canvas_id}"></canvas></div>'
            # Data inline (skript ich vyzbiera nižšie)
            f'<script type="application/json" id="data_{canvas_id}">'
            f'{_json.dumps({"labels": slot_labels, "batt": batt_path, "soc": soc_path, "now_hh": now_hh})}'
            f'</script>'
            f'</div>'
        )

    n_total = len(all_profs)
    n_active = len(live_profs)

    # === 3. HTML body ===
    body = (
        f'<style>'
        f'.container{{max-width:100%;margin:0 auto;padding:16px 20px}}'
        f'.shared-charts{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}}'
        f'.shared-chart-card{{background:#fff;border-radius:12px;padding:14px;box-shadow:0 2px 6px rgba(0,0,0,.07);height:240px;position:relative}}'
        f'.kpi-row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:18px}}'
        f'.kpi-card{{background:#fff;border-radius:10px;padding:12px 14px;border-left:4px solid #1F4E78}}'
        f'.kpi-card .label{{font-size:11px;color:#888;text-transform:uppercase}}'
        f'.kpi-card .value{{font-size:26px;font-weight:700;color:#1F4E78;margin:3px 0}}'
        f'.profile-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(460px,1fr));gap:14px}}'
        f'@media(max-width:768px){{.shared-charts{{grid-template-columns:1fr}}.profile-grid{{grid-template-columns:1fr}}.container{{padding:10px}}}}'
        f'</style>'

        f'<div class="container">'
        f'<h1 style="margin:0 0 6px;color:#1F4E78">🛰 Manager — fleet command center</h1>'
        f'<p style="color:#666;margin:0 0 18px;font-size:13px">'
        f'{n_active} BG ON (z {n_total} celkom) · '
        f'<span style="color:{"#2E7D32" if total_vdt_eur >= 0 else "#C62828"};font-weight:700">{total_vdt_eur:+.0f} € VDT dnes spolu</span> · '
        f'auto-refresh 60 s · '
        f'<a href="/profiles" style="color:#1F4E78">⚙ Spravovať profily</a></p>'

        # Zdieľané grafy hore
        f'<div class="shared-charts">'
        f'<div class="shared-chart-card">'
        f'<div style="font-size:13px;color:#1F4E78;font-weight:600;margin-bottom:4px">DT ceny dnes ({today_iso})</div>'
        f'<canvas id="ch_dt"></canvas>'
        f'</div>'
        f'<div class="shared-chart-card">'
        f'<div style="font-size:13px;color:#1F4E78;font-weight:600;margin-bottom:4px">MT signál (SEPS reg. výkon, MW)</div>'
        f'<canvas id="ch_mt"></canvas>'
        f'</div>'
        f'</div>'

        # Per-profil karty
        f'<h2 style="color:#1F4E78;font-size:18px;margin:14px 0 10px">Bežiace profily ({n_active})</h2>'
        f'<div class="profile-grid">'
        f'{"".join(profile_cards) if profile_cards else f"<div style=padding:20px;text-align:center;color:#888;background:#fff;border-radius:10px;grid-column:1/-1>Žiadny profil nebeží na pozadí. Zapni cez <a href=/profiles style=color:#1F4E78>⚙ Profily</a> (toggle Bg ON)</div>"}'
        f'</div>'

        f'<p style="color:#888;font-size:12px;margin:18px 0 0;font-style:italic">'
        f'Dáta z VDT advisor cache (state) + Realio DB (real profile) + OTE/SEPS (zdieľané grafy). Klik na názov profilu → detail dashboard.'
        f'</p>'
        f'</div>'

        # === Chart.js render ===
        f'<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>'
        f'<script>'
        f'document.addEventListener("DOMContentLoaded", () => {{'

        # Zdieľaný DT graf
        f'const dt_labels = {_json.dumps(dt_labels)};'
        f'const dt_cz = {_json.dumps(dt_cz_vals)};'
        f'if (dt_labels.length > 0) {{'
        f'  new Chart(document.getElementById("ch_dt"), {{type:"line",'
        f'    data:{{labels:dt_labels,datasets:['
        f'      {{label:"DT €/MWh",data:dt_cz,borderColor:"#1F4E78",backgroundColor:"rgba(31,78,120,.1)",fill:true,tension:0.2,pointRadius:0}}'
        f'    ]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{labels:{{font:{{size:11}}}}}}}},'
        f'      scales:{{x:{{ticks:{{font:{{size:9}},maxRotation:0,autoSkip:true,maxTicksLimit:12}}}},y:{{ticks:{{font:{{size:10}}}}}}}}}}}});'
        f'}}'

        # Zdieľaný MT graf
        f'const mt_labels = {_json.dumps(mt_labels)};'
        f'const mt_sys = {_json.dumps(mt_vals_sys)};'
        f'if (mt_labels.length > 0) {{'
        f'  new Chart(document.getElementById("ch_mt"), {{type:"line",'
        f'    data:{{labels:mt_labels,datasets:['
        f'      {{label:"SEPS sys MW",data:mt_sys,borderColor:"#F57F17",backgroundColor:"rgba(245,127,23,.1)",fill:true,tension:0.1,pointRadius:0}}'
        f'    ]}},options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{labels:{{font:{{size:11}}}}}}}}}}}});'
        f'}}'

        # Per-profil mini grafy — collect všetky canvas a vyrenderuj
        f'document.querySelectorAll(\'script[type="application/json"][id^="data_chart_"]\').forEach(scriptEl => {{'
        f'  const data = JSON.parse(scriptEl.textContent);'
        f'  const canvasId = scriptEl.id.replace("data_", "");'
        f'  const canvas = document.getElementById(canvasId);'
        f'  if (!canvas || !data.labels || data.labels.length === 0) return;'
        f'  new Chart(canvas, {{type:"line",'
        f'    data:{{labels:data.labels,datasets:['
        f'      {{label:"Batt kW",data:data.batt,borderColor:"#2E7D32",backgroundColor:"rgba(46,125,50,.15)",fill:true,tension:0.0,pointRadius:0,yAxisID:"y"}},'
        f'      {{label:"SOC %",data:data.soc,borderColor:"#F57F17",borderDash:[4,2],fill:false,tension:0.0,pointRadius:0,yAxisID:"y1"}}'
        f'    ]}},'
        f'    options:{{responsive:true,maintainAspectRatio:false,'
        f'      scales:{{'
        f'        y:{{position:"left",ticks:{{font:{{size:9}}}}}},'
        f'        y1:{{position:"right",ticks:{{font:{{size:9}}}},grid:{{drawOnChartArea:false}},min:0,max:100}},'
        f'        x:{{ticks:{{font:{{size:9}},maxRotation:0,autoSkip:true,maxTicksLimit:8}}}}'
        f'      }},'
        f'      plugins:{{legend:{{labels:{{font:{{size:10}},boxWidth:10}}}}}}'
        f'    }}'
        f'  }});'
        f'}});'
        f'setTimeout(() => location.reload(), 60000);'
        f'}});'
        f'</script>'
    )
    return render_legacy_body(None, "Manager", body)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(profile: str = ""):
    """Bug R2: Per-profile landing page.

    KPI dlaždice: SOC teraz, dnes zisk, FTV výroba, plán status.
    State diagnostic z vdt_state.compute_current_state.
    Bg toggle + last activity timestamp.
    Sub-nav linky na ďalšie pages.

    `?profile=X` = read-only override (nezmení active). Bez profile = aktívny.
    """
    import html as _html
    # Resolve profile (URL override → active)
    try:
        from core.profile_resolver import get_active as _ga, get_mode as _gm
        prof = _ga(profile)
        prof_mode = _gm(profile) if profile else _gm()
    except Exception:
        prof = profile or "default"
        prof_mode = "unknown"

    # Bg enabled status
    try:
        import auto_control as _ac
        bg_enabled = prof in _ac.get_enabled_profiles()
    except Exception:
        bg_enabled = False

    # Profile detail (kwp, batt, atď.)
    try:
        import profiles as _pr
        p_data = _pr.load_profile(prof) or {}
    except Exception:
        p_data = {}
    plan = p_data.get("plan") or {}
    kwp = float(plan.get("kwp", 0) or 0)
    batt_kw = float(plan.get("batt_kw", 0) or 0)
    batt_kwh = float(plan.get("batt_kwh", 0) or 0)

    # VDT state (kumulatívny SOC + DAM)
    try:
        import vdt_state as _vs
        state = _vs.compute_current_state(prof)
    except Exception as _e:
        state = {"data_completeness": False, "missing_items": ["state_module_error"],
                 "current_soc_pct": 0.0, "vdt_realized_count": 0, "vdt_realized_eur": 0.0,
                 "start_soc_pct": 0.0, "start_soc_source": str(_e)}

    cur_soc = float(state.get("current_soc_pct", 0.0))
    vdt_n = int(state.get("vdt_realized_count", 0))
    vdt_eur = float(state.get("vdt_realized_eur", 0.0))
    data_ok = bool(state.get("data_completeness", False))

    # Mode color
    if prof_mode == "real":
        chip_bg = "#C62828"
        chip_icon = "🔴"
    else:
        chip_bg = "#2E7D32"
        chip_icon = "🟢"
    bg_chip = "🔵 bg ON" if bg_enabled else "⚪ bg OFF"

    # KPI dlaždice
    soc_color = ("#2E7D32" if 20 <= cur_soc <= 80 else
                 "#C62828" if cur_soc <= 5 or cur_soc >= 95 else "#F57F17")
    profit_color = "#2E7D32" if vdt_eur >= 0 else "#C62828"
    data_chip = ("<span style='background:#2E7D32;color:#fff;padding:4px 10px;border-radius:6px;"
                 "font-size:12px;font-weight:600'>✓ Kompletný kontext</span>" if data_ok else
                 "<span style='background:#C62828;color:#fff;padding:4px 10px;border-radius:6px;"
                 "font-size:12px;font-weight:600'>⚠ Insufficient data</span>")

    # Sub-nav pages (rovnaké ako v _nav() pásme 3)
    pages = [("/", "🗓 Plán D-1"), ("/dentrh", "⚡ Denný trh 15-min"),
             ("/plan_batch", "📦 Batch plán"), ("/plans", "📋 Plány"),
             ("/livesim", "🟢 Živá simulácia"), ("/load_import", "🏠 Spotreba"),
             ("/auto_control", "🤖 Paper trading"), ("/vdt/live_advisor", "💹 OKTE VDT")]
    if prof_mode == "real":
        pages.insert(5, ("/realio", "🔌 Reálne meranie"))
    page_links = "".join(
        f'<a href="{href}?profile={_html.escape(prof)}" target="_top" '
        f'style="display:inline-flex;flex-direction:column;align-items:center;justify-content:center;'
        f'min-width:140px;padding:18px 12px;background:#fff;border:1px solid #d8e0eb;'
        f'border-radius:10px;text-decoration:none;color:#1F4E78;font-weight:600;'
        f'transition:transform .12s,box-shadow .12s" '
        f'onmouseover="this.style.transform=\'translateY(-2px)\';this.style.boxShadow=\'0 4px 12px rgba(31,78,120,.15)\'" '
        f'onmouseout="this.style.transform=\'\';this.style.boxShadow=\'\'">'
        f'<span style="font-size:24px;line-height:1">{lab.split()[0]}</span>'
        f'<span style="font-size:12px;margin-top:6px">{lab.split(maxsplit=1)[1] if len(lab.split())>1 else lab}</span>'
        f'</a>'
        for href, lab in pages)

    body = (
        f'<div style="max-width:1280px;margin:18px auto;padding:0 16px">'
        f'<h1 style="margin:0 0 14px;display:flex;align-items:center;gap:12px;color:#1F4E78">'
        f'<span style="background:{chip_bg};color:#fff;padding:6px 18px;border-radius:10px;font-size:20px">'
        f'{chip_icon} {_html.escape(prof)}</span>'
        f'<span style="font-size:14px;color:#666;font-weight:400">{bg_chip} · {prof_mode}</span>'
        f'</h1>'
        f'<p style="color:#666;margin:0 0 16px;font-size:13px">'
        f'Dashboard tohto profilu — KPI súhrn + rýchly prístup ku všetkým stránkam profilu.</p>'

        # KPI dlaždice (4 v rade)
        f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:0 0 18px">'
        f'<div style="background:#fff;border-radius:10px;padding:14px;border-left:4px solid {soc_color}">'
        f'<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px">SOC teraz</div>'
        f'<div style="font-size:28px;font-weight:700;color:{soc_color};margin:4px 0">{cur_soc:.1f}%</div>'
        f'<div style="font-size:11px;color:#999">kumulatívny výpočet od 00:00</div>'
        f'</div>'
        f'<div style="background:#fff;border-radius:10px;padding:14px;border-left:4px solid {profit_color}">'
        f'<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px">VDT zisk dnes</div>'
        f'<div style="font-size:28px;font-weight:700;color:{profit_color};margin:4px 0">{vdt_eur:+.2f} €</div>'
        f'<div style="font-size:11px;color:#999">{vdt_n} paper trades</div>'
        f'</div>'
        f'<div style="background:#fff;border-radius:10px;padding:14px;border-left:4px solid #1F4E78">'
        f'<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px">Batéria</div>'
        f'<div style="font-size:28px;font-weight:700;color:#1F4E78;margin:4px 0">{batt_kw:.0f} kW</div>'
        f'<div style="font-size:11px;color:#999">{batt_kwh:.0f} kWh kapacita</div>'
        f'</div>'
        f'<div style="background:#fff;border-radius:10px;padding:14px;border-left:4px solid #1F4E78">'
        f'<div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px">FTV inštalácia</div>'
        f'<div style="font-size:28px;font-weight:700;color:#1F4E78;margin:4px 0">{kwp:.0f} kWp</div>'
        f'<div style="font-size:11px;color:#999">{"FTV inštalovaná" if kwp > 0 else "batt-only profil"}</div>'
        f'</div>'
        f'</div>'

        # State diagnostic banner
        f'<div style="background:#fff;border-radius:10px;padding:14px 16px;margin:0 0 18px;'
        f'border-left:4px solid {"#2E7D32" if data_ok else "#C62828"}">'
        f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">'
        f'<b style="font-size:14px">Stav dát</b> {data_chip}</div>'
        f'<div style="font-size:12px;color:#555;line-height:1.6">'
        f'<b>Start SOC:</b> {state.get("start_soc_pct", 0):.1f}% '
        f'<span style="color:#888">({_html.escape(str(state.get("start_soc_source", "?")))})</span><br>'
        f'<b>Aktuálny SOC:</b> {state.get("current_soc_pct", 0):.1f}% '
        f'<span style="color:#888">({_html.escape(str(state.get("current_soc_source", "?")))})</span><br>'
        f'<b>DAM kind:</b> {_html.escape(str(state.get("dam_kind", "?")))} · '
        f'<b>VDT realized:</b> {vdt_n} trades<br>'
        f'{("<b>Missing:</b> " + ", ".join(state.get("missing_items", []))) if not data_ok else ""}'
        f'</div></div>'

        # Sub-nav dlaždice (pages profilu)
        f'<h2 style="margin:18px 0 12px;color:#1F4E78;font-size:18px">Stránky profilu</h2>'
        f'<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:18px">'
        f'{page_links}'
        f'</div>'

        f'<p style="color:#888;font-size:12px;margin-top:24px">'
        f'<i>Auto-refresh každých 60 s.</i> · '
        f'<a href="/profiles" style="color:#1F4E78">⚙ Správa profilov</a> · '
        f'<a href="/rt" style="color:#1F4E78">🔴 RT poradca (market-wide)</a>'
        f'</p>'
        f'</div>'
        # Auto-refresh meta
        f'<script>setTimeout(()=>location.reload(),60000);</script>'
    )
    return render_legacy_body(None, f"Dashboard — {prof}", body)


# ───────────────────────── DENNÝ TRH 15-min (čistá cenová arbitráž) ─────────────────────────
def _slot_of(iv):
    """index 15-min slotu (0..95) zo začiatku intervalu 'H:MM-H:MM'."""
    s = str(iv).split("-")[0]
    h, m = s.split(":")
    return int(h)*4 + int(m)//15


def _dt15_from_ote(df):
    """96 cien 15-min DT zarovnaných 00:00..23:45 (z fetch_ote_dayahead)."""
    arr = np.full(96, np.nan)
    for _, r in df.iterrows():
        try:
            arr[_slot_of(r["interval"])] = float(r["cena_EUR"])
        except (ValueError, IndexError):
            continue
    s = pd.Series(arr).ffill().bfill()
    return s.values


def _build_template_editor_html_dentrh():
    """Vykreslí 96 × 15-min editor (= globálna šablóna pre /dentrh aktívneho profilu)."""
    try:
        m96 = po.load_template(kind="dentrh") if po is not None else np.full(96, np.nan)
        rt96 = po.load_template_rt(kind="dentrh") if po is not None else np.full(96, np.nan)
    except Exception:
        m96 = np.full(96, np.nan); rt96 = np.full(96, np.nan)
    m96 = np.where(np.isfinite(m96), m96, 1.0)
    rt96 = np.array([(not np.isfinite(v) or v > 0.5) for v in rt96], dtype=bool)
    cells = []
    for i in range(96):
        h = i // 4; mm = (i % 4) * 15
        mv = float(m96[i]); ron = bool(rt96[i])
        m_bg = "#fff7e6" if abs(mv - 1.0) > 1e-6 else "#fff"
        rt_bg = "#ffe2e2" if not ron else "#fff"
        cells.append(
            f"<div style='display:flex;gap:4px;align-items:center;padding:2px 4px;border:1px solid #ddd;border-radius:4px;background:#fafafa;font-size:11px'>"
            f"<span style='font-weight:600;color:#1F4E78;min-width:38px'>{h:02d}:{mm:02d}</span>"
            f"<input type='number' name='mult_arr' value='{mv:.2f}' step='0.05' min='-3' max='3' "
            f"style='width:46px;padding:1px;border:1px solid #ccc;border-radius:3px;text-align:right;background:{m_bg};font-size:11px'>"
            f"<label style='display:flex;gap:1px;align-items:center;background:{rt_bg};padding:0 3px;border-radius:3px'>"
            f"<input type='checkbox' name='rt_arr' value='{i}'{' checked' if ron else ''}></label>"
            f"</div>"
        )
    grid = ("<div style='display:grid;grid-template-columns:repeat(8,1fr);gap:3px;margin:8px 0'>"
            + "".join(cells) + "</div>")
    bulk = ("<div style='margin:8px 0;padding:6px 10px;background:#fff3cd;border:1px solid #ffe399;border-radius:6px;display:flex;gap:10px;flex-wrap:wrap;align-items:center'>"
            "<b style='color:#7a5d00;font-size:13px'>⚡ Hromadne:</b>"
            "<label style='font-size:13px'>× = "
            "<input type='number' id='tpl_bulk_mult_dt' value='1.00' step='0.05' min='-3' max='3' style='width:60px;padding:2px;border:1px solid #ccc;border-radius:4px'></label>"
            "<button type='button' onclick=\"document.querySelectorAll('fieldset.tpl-editor input[name=mult_arr]').forEach(i=>{i.value=document.getElementById('tpl_bulk_mult_dt').value;i.style.background='#fff7e6'});return false\" "
            "style='background:#1F4E78;color:#fff;border:0;padding:4px 10px;border-radius:5px;cursor:pointer;font-size:12px'>Aplikuj ×</button>"
            "<span style='color:#ccc'>│</span>"
            "<button type='button' onclick=\"document.querySelectorAll('fieldset.tpl-editor input[name=rt_arr]').forEach(i=>{i.checked=true;i.parentElement.style.background='#fff'});return false\" "
            "style='background:#2E7D32;color:#fff;border:0;padding:4px 10px;border-radius:5px;cursor:pointer;font-size:12px'>RT všetky ✓</button>"
            "<button type='button' onclick=\"document.querySelectorAll('fieldset.tpl-editor input[name=rt_arr]').forEach(i=>{i.checked=false;i.parentElement.style.background='#ffe2e2'});return false\" "
            "style='background:#C0392B;color:#fff;border:0;padding:4px 10px;border-radius:5px;cursor:pointer;font-size:12px'>RT všetky ✗</button>"
            "</div>")
    return bulk + grid


def _dentrh_form(msg=""):
    today = dt.date.today().isoformat()
    f = _ui_load("dentrh", DEF)
    # FTV geometria + batéria + sieť SOC patria do PROFILU (jedného zdroja pravdy). ui_settings.plan
    # je vždy aktuálne (uložil sa pri poslednej aktivácii profilu alebo zmene v /plan), takže ho
    # použijeme ako prioritný zdroj pre tieto polia. Bez toho mali /plan a /dentrh nesynchronizované
    # hodnoty: napr. Trakany v /plan = 1200 kWp, ale /dentrh.kwp = 99 (stale defaulty) → FTV
    # predikcia v /dentrh ignorovala kWp z profilu.
    _plan = _ui_load("plan", {}) or {}
    _SHARED = ("lat", "lon", "kwp", "tilt", "azimuth", "eff",
               "batt_kw", "batt_kwh", "eff_c", "eff_d",
               "soc_min", "soc_max", "soc_init", "terminal_soc",
               "grid_kw", "grid_kw_import", "grid_kw_export",
               "grid_fee", "cycle_cost", "min_spread",
               "max_export_kwh_day", "max_import_kwh_day",
               "zco_bias_w")
    for _k in _SHARED:
        if _k in _plan and _plan[_k] not in (None, ""):
            f[_k] = _plan[_k]
    # Distribučný poplatok — single source of truth (rovnaké ako form_page)
    _dist_avg = None
    _dist_enabled = False
    _dist_configured = False
    _dist_profile_name = "default"
    try:
        import plan_store as _ps_d
        import distribution_cost as _dc_d
        _active_prof_d = _ps_d.resolve_profile() or "default"
        _dist_profile_name = _active_prof_d
        _dc_cfg_d = _dc_d.get_config(_active_prof_d)
        if _dc_cfg_d.get("enabled"):
            _dist_avg = _dc_d.avg_eur_per_mwh(_dc_cfg_d)
            _dist_enabled = True
        else:
            try:
                _tmp_d = dict(_dc_cfg_d); _tmp_d["enabled"] = True
                _avg_preview_d = _dc_d.avg_eur_per_mwh(_tmp_d)
                if _avg_preview_d and _avg_preview_d > 0.01:
                    _dist_avg = _avg_preview_d
                    _dist_configured = True
            except Exception:
                pass
    except Exception:
        pass
    return f"""<!doctype html><html lang="sk"><head><meta charset="utf-8">
<title>Denný trh 15-min</title><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin="">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
<style>body{{font-family:-apple-system,Segoe UI,Arial;max-width:1680px;margin:24px auto;padding:0 16px;color:#222}}
h1{{color:#1F4E78}} fieldset{{border:1px solid #e0e0e0;border-radius:10px;margin:12px 0;padding:12px 16px}}
legend{{color:#2E75B6;font-weight:600}} .cols{{display:grid;grid-template-columns:1fr 1fr;gap:0 24px}}
button{{background:#1F4E78;color:#fff;border:0;padding:10px 18px;border-radius:8px;font-size:15px;cursor:pointer}}
.msg{{color:#C00000}} #map_dentrh{{height:340px;border-radius:8px;border:1px solid #ccc}}
.wx-card{{background:#f3f6fb;border-radius:8px;padding:10px 12px;font-size:13px}}
.wx-card b{{color:#1F4E78}} .wx-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:6px}}
.wx-day{{background:#fff;border:1px solid #e0e0e0;border-radius:6px;padding:6px;text-align:center;font-size:11px}}
.wx-day .d{{font-weight:600;color:#1F4E78}} .wx-day .t{{font-size:18px;color:#2E75B6;margin:2px 0}}
.wx-day.today{{border:2px solid #2E7D32;background:#eef7ee}}</style></head><body>
<h1>⚡ Denný trh 15-min — cenová arbitráž</h1>
{_nav("/dentrh")}
{_overrides_status(today, kind="dentrh")}
<p style="color:#666">Ceny denného trhu na zvolený deň <b>už poznáš</b> — model nabíja v lacných 15-min slotoch
(obed) a vybíja v drahých (večer/noc). Žiadne plánovanie s neistotou, čistá arbitráž + výroba FTV.
Ak zvolíš <b>dnešný deň</b>, dole uvidíš aj odporúčanie pre aktuálny 15-min interval.</p>
<p class="msg">{msg}</p>
<form method="post" action="/dentrh">
<fieldset><legend>Deň, lokácia a elektráreň</legend>
<div style="display:grid;grid-template-columns:1fr 1.4fr;gap:20px">
<div>
<div class="cols">
<label style="display:flex;justify-content:space-between;margin:4px 0;grid-column:1/-1"><span>Dátum</span>
<input id="dentrh_date" name="date" value="{today}" type="date" style="padding:4px;border:1px solid #ccc;border-radius:6px"></label>
{_field("Šírka (lat)","lat",f['lat'])}{_field("Dĺžka (lon)","lon",f['lon'])}
{_field("Výkon FTV [kWp]","kwp",f['kwp'])}{_field("Sklon [°]","tilt",f['tilt'])}
{_field("Azimut [° ,0=juh]","azimuth",f['azimuth'])}{_field("Účinnosť FTV","eff",f['eff'])}
</div>
<p style="color:#666;font-size:12px;margin:6px 0 0">💡 Kliknutím na mapu sa <b>lat/lon</b> vyplní automaticky. Po zmene polohy alebo dátumu sa aktualizuje aj prognóza počasia.</p>
</div>
<div>
<div style="display:flex;gap:6px;margin-bottom:6px;align-items:center;position:relative">
  <input id="map_search_dentrh" type="text" placeholder="🔍 Vyhľadaj mesto / obec / adresu (napr. Trakany, Bratislava, Olomouc)…"
    style="flex:1;padding:6px 10px;border:1px solid #ccc;border-radius:6px;font-size:14px" autocomplete="off">
  <button type="button" id="map_search_btn_dentrh" style="padding:6px 14px;background:#1F4E78;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:13px">Hľadaj</button>
  <div id="map_search_results_dentrh" style="position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid #ccc;border-radius:6px;max-height:220px;overflow:auto;display:none;z-index:1000;box-shadow:0 4px 10px rgba(0,0,0,.1)"></div>
</div>
<div id="map_dentrh"></div>
<div id="wx_box_dentrh" class="wx-card" style="margin-top:8px">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <b id="wx_now_title_dentrh">Načítavam počasie…</b>
    <span id="wx_now_t_dentrh" style="font-size:22px;color:#1F4E78"></span>
  </div>
  <div id="wx_now_detail_dentrh" style="font-size:12px;color:#666;margin-top:2px"></div>
  <div class="wx-grid" id="wx_forecast_dentrh"></div>
</div>
</div>
</div>
</fieldset>
<script>
(function(){{
  function getLat(){{ return parseFloat(document.querySelector('input[name=lat]').value)||49.0; }}
  function getLon(){{ return parseFloat(document.querySelector('input[name=lon]').value)||17.0; }}
  const map = L.map('map_dentrh').setView([getLat(), getLon()], 9);
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom:18,attribution:'© OpenStreetMap'}}).addTo(map);
  let marker = L.marker([getLat(), getLon()], {{draggable:true}}).addTo(map);
  function setLatLon(lat, lon){{
    document.querySelector('input[name=lat]').value = lat.toFixed(4);
    document.querySelector('input[name=lon]').value = lon.toFixed(4);
    fetchWeather();
  }}
  map.on('click', e => {{
    marker.setLatLng(e.latlng);
    setLatLon(e.latlng.lat, e.latlng.lng);
  }});
  marker.on('dragend', () => {{
    const p = marker.getLatLng();
    setLatLon(p.lat, p.lng);
  }});
  ['lat','lon'].forEach(n => {{
    const el = document.querySelector('input[name='+n+']');
    el.addEventListener('change', () => {{
      const lat = getLat(), lon = getLon();
      marker.setLatLng([lat,lon]);
      map.panTo([lat,lon]);
      fetchWeather();
    }});
  }});
  document.getElementById('dentrh_date').addEventListener('change', fetchWeather);

  // ── Nominatim search (OpenStreetMap geocoder, free) ──
  const searchInput = document.getElementById('map_search_dentrh');
  const searchBtn = document.getElementById('map_search_btn_dentrh');
  const resultsBox = document.getElementById('map_search_results_dentrh');
  let searchAbort = null;
  async function doSearch(q){{
    q = (q||'').trim();
    if(!q){{ resultsBox.style.display='none'; return; }}
    if(searchAbort) searchAbort.abort();
    searchAbort = new AbortController();
    try {{
      const url = 'https://nominatim.openstreetmap.org/search?q=' + encodeURIComponent(q)
                + '&format=json&limit=6&accept-language=sk,cs,en&countrycodes=cz,sk,at,pl,de,hu';
      const r = await fetch(url, {{signal: searchAbort.signal, headers: {{'User-Agent':'FTV-planner-app'}}}});
      if(!r.ok) throw new Error('HTTP '+r.status);
      const items = await r.json();
      resultsBox.innerHTML = '';
      if(!items.length){{
        resultsBox.innerHTML = '<div style="padding:8px;color:#999">Žiadne výsledky</div>';
      }} else {{
        items.forEach(it => {{
          const div = document.createElement('div');
          div.style.cssText = 'padding:8px 12px;border-bottom:1px solid #eee;cursor:pointer;font-size:13px';
          div.innerHTML = '<b>'+(it.name||it.display_name.split(',')[0])+'</b><br>'
                        + '<span style="color:#666;font-size:11px">'+it.display_name+'</span><br>'
                        + '<span style="color:#999;font-size:10px">'+parseFloat(it.lat).toFixed(4)+', '+parseFloat(it.lon).toFixed(4)+' · '+(it.type||'')+'</span>';
          div.onmouseover = () => div.style.background='#eef';
          div.onmouseout = () => div.style.background='#fff';
          div.onclick = () => {{
            const lat = parseFloat(it.lat), lon = parseFloat(it.lon);
            marker.setLatLng([lat, lon]);
            map.setView([lat, lon], 12);
            setLatLon(lat, lon);
            resultsBox.style.display='none';
            searchInput.value = it.display_name.split(',').slice(0,2).join(', ');
          }};
          resultsBox.appendChild(div);
        }});
      }}
      resultsBox.style.display='block';
    }} catch(e){{
      if(e.name !== 'AbortError'){{
        resultsBox.innerHTML = '<div style="padding:8px;color:#c00">Chyba: '+e.message+'</div>';
        resultsBox.style.display='block';
      }}
    }}
  }}
  let debTimer = null;
  searchInput.addEventListener('input', () => {{
    if(debTimer) clearTimeout(debTimer);
    debTimer = setTimeout(() => doSearch(searchInput.value), 450);
  }});
  searchInput.addEventListener('keydown', e => {{
    if(e.key === 'Enter'){{ e.preventDefault(); doSearch(searchInput.value); }}
    if(e.key === 'Escape'){{ resultsBox.style.display='none'; }}
  }});
  searchBtn.addEventListener('click', () => doSearch(searchInput.value));
  document.addEventListener('click', e => {{
    if(!resultsBox.contains(e.target) && e.target !== searchInput && e.target !== searchBtn){{
      resultsBox.style.display='none';
    }}
  }});

  // ── Open-meteo počasie (current + 7-day forecast) ──
  function wxIcon(code){{
    if(code===0) return '☀️ jasno';
    if(code<=3) return '🌤 čiastočne oblačno';
    if(code<=48) return '🌫 hmla';
    if(code<=57) return '🌦 mrholenie';
    if(code<=67) return '🌧 dážď';
    if(code<=77) return '❄️ sneh';
    if(code<=82) return '🌧 prehánky';
    if(code<=99) return '⛈ búrka';
    return '?';
  }}
  async function fetchWeather(){{
    const lat = getLat(), lon = getLon();
    const sel = document.getElementById('dentrh_date').value;
    const url = 'https://api.open-meteo.com/v1/forecast'
      + '?latitude='+lat+'&longitude='+lon
      + '&current=temperature_2m,relative_humidity_2m,weather_code,cloud_cover,wind_speed_10m'
      + '&daily=weather_code,temperature_2m_max,temperature_2m_min,sunshine_duration,precipitation_sum,cloud_cover_mean'
      + '&forecast_days=7&past_days=2&timezone=Europe%2FBratislava';
    try {{
      const r = await fetch(url);
      if(!r.ok) throw new Error('HTTP '+r.status);
      const j = await r.json();
      const c = j.current||{{}};
      document.getElementById('wx_now_title_dentrh').textContent = 'Aktuálne: '+wxIcon(c.weather_code);
      document.getElementById('wx_now_t_dentrh').textContent = (c.temperature_2m||0).toFixed(1)+' °C';
      document.getElementById('wx_now_detail_dentrh').textContent =
        'vlhkosť '+(c.relative_humidity_2m||0)+' % · oblačnosť '+(c.cloud_cover||0)+' % · vietor '+(c.wind_speed_10m||0).toFixed(1)+' m/s · ('+lat.toFixed(3)+', '+lon.toFixed(3)+')';
      const d = j.daily||{{}};
      const fc = document.getElementById('wx_forecast_dentrh');
      fc.innerHTML = '';
      const today = new Date().toISOString().slice(0,10);
      for(let i=0;i<(d.time||[]).length;i++){{
        const dt = d.time[i];
        const div = document.createElement('div');
        div.className = 'wx-day' + (dt===sel ? ' today' : '');
        const sun_h = Math.round((d.sunshine_duration?.[i]||0)/3600);
        div.innerHTML = '<div class="d">'+dt.slice(5)+(dt===today?' (dnes)':'')+(dt===sel?' ★':'')+'</div>'
          + '<div class="t">'+(d.temperature_2m_max?.[i]||0).toFixed(0)+'°/'+(d.temperature_2m_min?.[i]||0).toFixed(0)+'°</div>'
          + '<div>'+wxIcon(d.weather_code?.[i])+'</div>'
          + '<div style="color:#666">☁ '+(d.cloud_cover_mean?.[i]||0).toFixed(0)+'% · ☀ '+sun_h+'h · 💧 '+(d.precipitation_sum?.[i]||0).toFixed(1)+'mm</div>';
        fc.appendChild(div);
      }}
    }} catch(e) {{
      document.getElementById('wx_now_title_dentrh').textContent = 'Počasie sa nedá načítať: '+e.message;
    }}
  }}
  fetchWeather();
}})();
</script>
<fieldset><legend>Batéria a sieť</legend><div class="cols">
{_field("Výkon batérie [kW]","batt_kw",f['batt_kw'])}{_field("Kapacita [kWh]","batt_kwh",f['batt_kwh'])}
{_field("Účinnosť nabíjania","eff_c",f['eff_c'])}{_field("Účinnosť vybíjania","eff_d",f['eff_d'])}
{_field("SOC min [%]","soc_min",f['soc_min'])}{_field("SOC max [%]","soc_max",f['soc_max'])}
{_field("SOC začiatok [%]","soc_init",f['soc_init'])}{_field("SOC koniec [%]","terminal_soc",f['terminal_soc'])}{_field("SOC rezerva [%]","soc_reserve_pct",f.get('soc_reserve_pct', 0.0))}{_field("Sieť rezerva pre RT [%]","rt_grid_reserve_pct",f.get('rt_grid_reserve_pct', 0.0))}
{_field("Limit dodávky do siete [kW]","grid_kw_export",f.get('grid_kw_export', f['grid_kw']))}{_field("Limit odberu zo siete [kW]","grid_kw_import",f.get('grid_kw_import', f['grid_kw']))}
{(
  f'<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0">'
  f'<span>Poplatok odber [€/MWh] '
  f'<span style="background:#e6f4ea;color:#1B5E20;padding:1px 6px;border-radius:4px;font-size:11px;margin-left:4px">'
  f'auto z distribúcie ✓</span></span>'
  f'<input name="grid_fee" type="number" step="0.1" value="{_dist_avg:.2f}" readonly '
  f'style="padding:4px 8px;border:1px solid #ccc;border-radius:6px;width:120px;'
  f'background:#f5f5f5;color:#555;cursor:not-allowed" '
  f'title="Vypočítané z distribučnej konfigurácie (TOU+TPS+SS+OZE). '
  f'Edituj cez Profil → Distribučné tarify."></label>'
) if _dist_enabled else (
  f'<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0">'
  f'<span>Poplatok odber [€/MWh] '
  f'<span style="background:#fff3cd;color:#7a5d00;padding:1px 6px;border-radius:4px;font-size:11px;margin-left:4px" '
  f'title="Distribučné tarify sú v profile nastavené (priemer {_dist_avg:.2f} €/MWh) ale OFF master toggle">'
  f'⚠ distribúcia vypnutá</span></span>'
  f'<input name="grid_fee" type="number" step="0.1" value="{float(f["grid_fee"]):.2f}" '
  f'style="padding:4px 8px;border:1px solid #ccc;border-radius:6px;width:120px"></label>'
  f'<div style="margin:2px 0 8px;padding:6px 10px;background:#fff8e1;border-left:3px solid #f9a825;border-radius:4px;font-size:12px;color:#7a5d00">'
  f'💡 Máš nastavené distribučné tarify — priemer = <b>{_dist_avg:.2f} €/MWh</b>. '
  f'<a href="/profiles/edit?name={_dist_profile_name}" style="color:#1F4E78">Zapni v profile</a> '
  f'aby sa použili automaticky (single source of truth namiesto ručného poplatku).</div>'
) if _dist_configured else _field("Poplatok odber [€/MWh]","grid_fee",f['grid_fee'])}<input type="hidden" name="grid_kw" value="{max(float(f.get('grid_kw_import', f['grid_kw'])), float(f.get('grid_kw_export', f['grid_kw'])))}">
{_field("Náklad cyklu [€/MWh]","cycle_cost",f['cycle_cost'])}{_field("Min. cenový rozdiel [€/MWh]","min_spread",f['min_spread'])}
{_field("Max DAM export [kWh/deň, 0=bez stropu]","max_export_kwh_day",f.get('max_export_kwh_day', 0))}{_field("Max DAM import [kWh/deň, 0=bez stropu]","max_import_kwh_day",f.get('max_import_kwh_day', 0))}
{_field("Bias plánu o ZCO (váha 0–1)","zco_bias_w",f.get('zco_bias_w', 0.0))}
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0">
<span>Nabíjať zo siete</span><input name="allow_grid_charge" type="checkbox" checked></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0">
<span title="VYPNUTÉ (default) = LP voľne arbitrážuje pri DT &lt; 0 (zarobí na odbere zo siete). ZAPNUTÉ = LP nikdy neimportuje pri DT &lt; 0 (môže nechať zisk na stole — odporúča sa nechať vypnuté)">Blokovať nákup pri zápornej cene <span style="color:#888">(odporúča sa vypnuté)</span></span><input name="block_neg_import" type="checkbox" {"checked" if f.get("block_neg_import", False) else ""}></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0">
<span>Orezať/vypnúť FTV pri nevýhodnej cene</span><input name="allow_curtail" type="checkbox" checked></label>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0;color:#1F4E78;background:#eef5e0;padding:4px 8px;border-radius:6px" title="D-1 plán bude IBA nabíjať batériu. Vybíjanie cez RT odchýlku.">
<span><b>Iba D-1 nabíjanie</b> (vybíjanie len cez RT)</span><input name="no_planned_discharge" type="checkbox" {"checked" if f.get("no_planned_discharge", False) else ""}></label>
</div></fieldset>
<fieldset class="tpl-editor"><legend>× a RT šablóna (pre celý profil, 96 × 15-min)</legend>
<p style="color:#666;font-size:13px;margin:0 0 6px">Hodnoty per 15-min slot sa uložia ako <b>globálna šablóna pre aktívny profil</b> pri každom submite. Šablóna platí pre VŠETKY dni rovnako.</p>
{_build_template_editor_html_dentrh()}
</fieldset>
<fieldset><legend>Batch generovanie (voliteľne)</legend>
<div class="cols">
<label style="display:flex;justify-content:space-between;gap:8px;margin:4px 0"><span>Od (vrátane)</span>
<input name="from_date" value="{today}" type="date" style="padding:4px;border:1px solid #ccc;border-radius:6px"></label>
<label style="display:flex;justify-content:space-between;gap:8px;margin:4px 0"><span>Do (vrátane)</span>
<input name="to_date" value="{(dt.date.today() + dt.timedelta(days=1)).isoformat()}" type="date" style="padding:4px;border:1px solid #ccc;border-radius:6px"></label>
</div>
<input type="hidden" name="step_min" value="15">
<input type="hidden" name="kind" value="dentrh">
<p style="color:#666;font-size:13px;margin:6px 0 0">"Generovať BATCH" uloží nastavenia AJ vygeneruje 15-min plán pre každý deň v rozsahu od–do.</p>
</fieldset>
<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:8px">
<button type="submit">Spočítaj 15-min rozvrh</button>
<button type="submit" name="save_only" value="1" style="background:#2E7D32" title="Uloží ui_settings + × a RT šablónu do aktívneho profilu, BEZ výpočtu plánu.">💾 Uložiť do profilu (bez generovania)</button>
<button type="submit" formaction="/plan_batch" style="background:#5E35B1">📦 Generovať BATCH (od–do)</button>
</div>
</form></body></html>"""


@app.get("/dentrh", response_class=HTMLResponse)
def dentrh_get():
    return _dentrh_form()


@app.post("/dentrh", response_class=HTMLResponse)
def dentrh(date: str = Form(...), lat: float = Form(...), lon: float = Form(...),
           kwp: float = Form(...), tilt: float = Form(...), azimuth: float = Form(...), eff: float = Form(...),
           batt_kw: float = Form(...), batt_kwh: float = Form(...), eff_c: float = Form(...), eff_d: float = Form(...),
           soc_min: float = Form(...), soc_max: float = Form(...), soc_init: float = Form(...),
           soc_reserve_pct: float = Form(default=0.0),
           rt_grid_reserve_pct: float = Form(default=0.0),
           terminal_soc: float = Form(...), grid_kw: float = Form(...),
           grid_kw_import: float = Form(default=None), grid_kw_export: float = Form(default=None),
           grid_fee: float = Form(...),
           cycle_cost: float = Form(...), min_spread: float = Form(default=30.0),
           allow_grid_charge: str = Form(default=""), allow_curtail: str = Form(default=""),
           block_neg_import: str = Form(default=""),
           no_planned_discharge: str = Form(default=""),
           max_export_kwh_day: float = Form(default=0.0),
           max_import_kwh_day: float = Form(default=0.0),
           zco_bias_w: float = Form(default=0.0),
           mult_action: str = Form(default=""),
           mult_arr: list[float] = Form(default=[]),
           rt_arr: list[str] = Form(default=[]),
           save_only: str = Form(default="")):
    d = dt.date.fromisoformat(date)
    agc = bool(allow_grid_charge)
    acu = bool(allow_curtail)
    bni = bool(block_neg_import)
    npd = bool(no_planned_discharge)
    _mex = float(max_export_kwh_day) if max_export_kwh_day and max_export_kwh_day > 0 else None
    _mim = float(max_import_kwh_day) if max_import_kwh_day and max_import_kwh_day > 0 else None
    zbw = float(zco_bias_w or 0.0)
    # Bug #622 (Krok A): SOC carryover z livesim trace pre /dentrh.
    # Override user-vstupu `soc_init` reálnym SOC po predošlom dni — D-1 plán
    # nesmie predpokladať ideálnu trajektóriu, lebo večerné nominácie potom
    # nedosiahne (= odchýlka voči trhu = pokuta).
    _soc_init_carry, _soc_init_src = _resolve_soc_init_carryover(
        date, {"soc_init": soc_init, "soc_min": soc_min, "soc_max": soc_max}, case="dentrh")
    soc_init_user = soc_init   # zachovaj manual pre ui_settings/profile zápis
    if _soc_init_src == "carried":
        print(f"[#622 /dentrh POST] {date}: soc_init={soc_init:.1f}% → "
              f"carried {_soc_init_carry:.1f}% (LP only, profile zostáva {soc_init_user:.1f}%)")
        soc_init = _soc_init_carry
    # asymetrické limity siete: ak prázdne, použiť grid_kw (backward compat)
    gki = float(grid_kw_import) if grid_kw_import is not None else float(grid_kw)
    gke = float(grid_kw_export) if grid_kw_export is not None else float(grid_kw)
    # ── ručné násobitele + RT mask (uloženie/vyčistenie + načítanie effective) ──
    # KAŽDÝ submit /dentrh ukladá × a RT z formulára ako globálnu šablónu pre profil (kind=dentrh).
    if not mult_action and mult_arr and len(mult_arr) == 96:
        mult_action = "save_template"
    mult_msg, mult96, rt_mask96 = _handle_mult_action(date, 15, mult_arr, mult_action, rt_arr)
    # ui_settings.dentrh = polia z /dentrh formulára (bez FTV-balance — tie sú globálne v ui_settings.plan)
    # Bug SOC-INIT-PERSIST-V2 (2026-06-10): používa soc_init_user (= user formulár),
    # nie soc_init (= carried prepísaný v riadku 3226). Predtým sa do ui_settings.dentrh
    # ukladal carried 16.86% namiesto user 5% → ďalšie otvorenie formuláru ho zobrazil ako 16.86.
    _ui_save("dentrh", dict(lat=lat, lon=lon, kwp=kwp, tilt=tilt, azimuth=azimuth, eff=eff,
                            batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
                            soc_min=soc_min, soc_max=soc_max, soc_init=soc_init_user, terminal_soc=terminal_soc,
                            soc_reserve_pct=float(soc_reserve_pct or 0.0),
                            rt_grid_reserve_pct=float(rt_grid_reserve_pct or 0.0),
                            grid_kw=grid_kw, grid_kw_import=gki, grid_kw_export=gke,
                            grid_fee=grid_fee, cycle_cost=cycle_cost, min_spread=min_spread,
                            allow_grid_charge=agc, allow_curtail=acu, block_neg_import=bni,
                            no_planned_discharge=npd,
                            max_export_kwh_day=float(max_export_kwh_day or 0),
                            max_import_kwh_day=float(max_import_kwh_day or 0),
                            zco_bias_w=zbw))
    # SYNC: shared FTV/batt/sieť parametre tiež do ui_settings.plan — aby boli /plan a /dentrh
    # vždy konzistentné. Bez tohto sync-u by /dentrh forma pri reloade prepísala uloženú dentrh
    # hodnotu starou hodnotou z .plan (lebo _dentrh_form má .plan > .dentrh priority).
    _SHARED_SYNC = dict(lat=lat, lon=lon, kwp=kwp, tilt=tilt, azimuth=azimuth, eff=eff,
                          batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
                          soc_min=soc_min, soc_max=soc_max, soc_init=soc_init_user, terminal_soc=terminal_soc,
                          soc_reserve_pct=float(soc_reserve_pct or 0.0),
                          rt_grid_reserve_pct=float(rt_grid_reserve_pct or 0.0),
                          grid_kw=grid_kw, grid_kw_import=gki, grid_kw_export=gke,
                          grid_fee=grid_fee, cycle_cost=cycle_cost, min_spread=min_spread,
                          max_export_kwh_day=float(max_export_kwh_day or 0),
                          max_import_kwh_day=float(max_import_kwh_day or 0),
                          zco_bias_w=zbw)
    _plan_existing = _ui_load("plan", {}) or {}
    _plan_existing.update(_SHARED_SYNC)
    _ui_save("plan", _plan_existing)
    _autosave_active_profile()      # zmeny v /dentrh ihneď premietnuť do aktívneho profilu (ak je)
    _clear_livesim_logs()           # invalidate cached livesim log — fresh prepočet pri ďalšom otvorení /livesim
    # ── Save-only režim: skončiť tu, vrátiť potvrdzovaciu stránku (plán SA NEGENERUJE) ──
    if save_only:
        try:
            import profiles as _pr
            _active = _pr.get_active() or "—"
        except Exception:
            _active = "—"
        return f"""<!doctype html><html lang="sk"><head><meta charset="utf-8"><title>Uložené</title>
<meta http-equiv="refresh" content="2;url=/dentrh">
<style>body{{font-family:-apple-system,Segoe UI,Arial;max-width:680px;margin:48px auto;padding:0 16px;color:#222;text-align:center}}
.ok{{background:#e8f5e9;border-left:4px solid #2E7D32;border-radius:8px;padding:18px;margin:20px 0;color:#1B5E20;text-align:left}}
a{{color:#1F4E78}}</style></head><body>
<h1 style="color:#2E7D32">✓ Uložené do profilu</h1>
<div class="ok"><b>Uložené:</b><br>
• Parametre /dentrh (ui_settings.dentrh)<br>
• × a RT šablóna pre profil <b>{_active}</b> (out/plan_overrides/{_active}/_template_dentrh.json)<br>
• Snapshot profilu (out/profiles/{_active}.json)<br><br>
{mult_msg if mult_msg else ""}</div>
<p>15-min rozvrh som <b>NEVYGENEROVAL</b>. Vráť sa na <a href="/dentrh">/dentrh</a> a klikni „Spočítaj 15-min rozvrh" keď chceš výpočet.</p>
<p style="color:#666;font-size:13px">(Auto-redirect za 2 s na /dentrh…)</p>
</body></html>"""
    try:
        ote = _fetch_ote_cached(d)                        # reálne 15-min DT ceny (známe vopred)
        price15 = _dt15_from_ote(ote)
        # Pre batt-only profily (kwp=0, napr. Trakany_real) PVF fetch nedáva zmysel.
        # Vytvoríme prázdny pv_h aby plan bežal s 0 FTV (čistá batt arbitráž).
        if float(kwp or 0) > 0.01:
            wx = _fetch_pv_cached(lat, lon, kwp, tilt, azimuth, eff, start=d, end=d)
            wx["time"] = pd.to_datetime(wx["time"]); wx = wx[wx.time.dt.date == d].sort_values("time")
            if wx.empty:
                return _dentrh_form(f"Pre {d} nie sú dostupné dáta výroby FTV.")
            cal = _cal_for(d)
            pv_h = wx.kw.values * cal                          # hodinová výroba (kalibrovaná)
            if len(pv_h) < 24:
                pv_h = np.concatenate([pv_h, np.zeros(24 - len(pv_h))])
        else:
            pv_h = np.zeros(24)                                # batt-only: žiadny FTV
        pv15 = np.repeat(pv_h[:24], 4) / 4.0                  # → 96 × 15-min slotov
        n = min(len(pv15), len(price15))
        mult96_use = np.asarray(mult96, dtype=float)[:n]
        # ── load profile pre tento dátum (96 × kW → 96 × kWh za 15min = kW × 0.25) ──
        load96 = None
        if lp is not None and lp.has_data():
            try:
                _l96_kw = lp.load_for_date(d.isoformat())                       # 96 × kW
                load96 = _l96_kw * 0.25                                          # kWh za 15-min slot
                load96 = load96[:n]
            except Exception:
                load96 = None
        # ZCO bias — ak zbw>0, LP rozhoduje na biased cene; settle stále na pôvodnej
        import livesim as _lsim
        decision_price15 = _lsim._apply_zco_bias(price15[:n], d, float(pv15[:n].sum()), 15, zbw)
        # baseline (NÁVRH) bez overridu
        sch_base, summ_base = optimize_day(pv15[:n], decision_price15, dt=0.25,
                                 settle_price=price15[:n],
                                 batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
                                 soc_min_pct=soc_min, soc_max_pct=soc_max, soc_init_pct=soc_init,
                                 soc_reserve_pct=float(soc_reserve_pct or 0.0),
                                 rt_grid_reserve_pct=float(rt_grid_reserve_pct or 0.0),
                                 grid_kw=grid_kw, grid_kw_import=gki, grid_kw_export=gke,
                                 grid_fee=grid_fee, cycle_cost=cycle_cost,
                                 allow_grid_charge=agc, terminal_soc_pct=terminal_soc,
                                 allow_curtail=acu,
                                 min_spread_eur=min_spread, block_neg_import=bni,
                                 block_planned_discharge=npd,
                                 load_kwh=load96,
                                 max_export_kwh_day=_mex, max_import_kwh_day=_mim)
        if np.allclose(mult96_use, 1.0):
            sch, summ = sch_base, summ_base
        else:
            sch, summ = optimize_day(pv15[:n], decision_price15, dt=0.25,
                                 settle_price=price15[:n],
                                 batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
                                 soc_min_pct=soc_min, soc_max_pct=soc_max, soc_init_pct=soc_init,
                                 soc_reserve_pct=float(soc_reserve_pct or 0.0),
                                 rt_grid_reserve_pct=float(rt_grid_reserve_pct or 0.0),
                                 grid_kw=grid_kw, grid_kw_import=gki, grid_kw_export=gke,
                                 grid_fee=grid_fee, cycle_cost=cycle_cost,
                                 allow_grid_charge=agc, terminal_soc_pct=terminal_soc,
                                 allow_curtail=acu,
                                 load_kwh=load96,
                                 min_spread_eur=min_spread, block_neg_import=bni,
                                 batt_kw_override=mult96_use,
                                 block_planned_discharge=npd,
                                 max_export_kwh_day=_mex, max_import_kwh_day=_mim)
    except Exception as e:
        return _dentrh_form(f"Chyba pri výpočte: {e}")

    # uloženie 15-min plánu (kind='dentrh') do plan_store — livesim ho odtiaľto číta
    plan_saved_path = None
    if ps is not None:
        try:
            sched_cols = ["batt_kw", "grid_kwh", "pv_kwh", "price_eur", "curtail_kwh",
                          "soc_pct", "order_mwh", "_charge_kw", "_discharge_kw",
                          "_export_kwh", "_import_kwh", "soc_kwh", "load_kwh"]
            sched_dict = {c: sch[c].tolist() for c in sched_cols if c in sch.columns}
            # FTV-balance flags sú globálne v ui_settings.plan (formulár /dentrh ich nemá) — pridáme pre audit
            _pui_plan = _ui_load("plan", {}) or {}
            dentrh_params = dict(date=date, lat=lat, lon=lon, kwp=kwp, tilt=tilt, azimuth=azimuth, eff=eff,
                                  batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
                                  soc_min_pct=soc_min, soc_max_pct=soc_max, soc_init_pct=soc_init,
                                  soc_reserve_pct=float(soc_reserve_pct or 0.0),
                                  rt_grid_reserve_pct=float(rt_grid_reserve_pct or 0.0),
                                  terminal_soc_pct=terminal_soc, grid_kw=grid_kw,
                                  grid_kw_import=gki, grid_kw_export=gke,
                                  grid_fee=grid_fee, cycle_cost=cycle_cost, min_spread_eur=min_spread,
                                  allow_grid_charge=agc, allow_curtail=acu, block_neg_import=bni,
                                  no_planned_discharge=npd,
                                  aggressive_rt=bool(_pui_plan.get("aggressive_rt", False)),
                                  ftv_balance=bool(_pui_plan.get("ftv_balance", True)),
                                  ftv_lookahead_h=float(_pui_plan.get("ftv_lookahead_h", 4.0)),
                                  rt_audit_horizon_h=float(_pui_plan.get("rt_audit_horizon_h", 1.0)),
                                  ftv_persistence_throttle=bool(_pui_plan.get("ftv_persistence_throttle", True)),
                                  rt_no_worsen_dev=bool(_pui_plan.get("rt_no_worsen_dev", True)),
                                  ftv_strict_plan=bool(_pui_plan.get("ftv_strict_plan", True)),
                                  ftv_strict_deadband_kw=float(_pui_plan.get("ftv_strict_deadband_kw", 5.0)))
            plan_saved_path = ps.save_plan(
                date, 15, "dentrh", params=dentrh_params, schedule=sched_dict, summary=summ,
                mults=list(map(float, np.asarray(mult96_use).reshape(-1))),
                rt_mask=list(map(float, np.asarray(rt_mask96).reshape(-1))),
                block_planned_discharge=npd, zco_bias_w=0.0, rt_freedom=True,
                meta=dict(source="/dentrh", price_kind="real_ote"))
        except Exception as _e:
            plan_saved_path = f"ERR: {_e}"
    times = [f"{i//4:02d}:{(i%4)*15:02d}" for i in range(len(sch))]
    batt = sch["batt_kw"].tolist(); price = sch["price_eur"].tolist()
    socp = sch["soc_pct"].tolist(); pvv = sch["pv_kwh"].tolist()
    cycles = (sch["_charge_kw"].sum() + sch["_discharge_kw"].sum())/2/batt_kwh

    # ── ODPORÚČANIE TERAZ (len ak je zvolený dnešný deň) ──
    now_card = ""
    if d == dt.date.today():
        now = dt.datetime.now(); sl = now.hour*4 + now.minute//15
        if 0 <= sl < len(sch):
            kw = batt[sl]
            if kw > 0.5:
                act, col, ic = f"VYBÍJAJ {kw:.0f} kW", "#2E7D32", "🔋→"
            elif kw < -0.5:
                act, col, ic = f"NABÍJAJ {abs(kw):.0f} kW", "#C0392B", "→🔋"
            else:
                act, col, ic = "DRŽ (nič nerob)", "#777", "⏸"
            now_card = (
                f"<div style='background:{col};color:#fff;border-radius:12px;padding:16px 20px;margin:14px 0'>"
                f"<div style='font-size:13px;opacity:.85'>ODPORÚČANIE TERAZ — interval {times[sl]} "
                f"(cena {price[sl]:.0f} €/MWh, SOC {socp[sl]:.0f} %)</div>"
                f"<div style='font-size:26px;font-weight:700'>{ic} {act}</div></div>")
        else:
            now_card = ""
    elif d > dt.date.today():
        now_card = ("<div style='background:#eef3f9;border-radius:10px;padding:10px 14px;margin:12px 0;"
                    "font-size:14px;color:#1F4E78'>Plán na budúci deň — odporúčanie pre aktuálny interval "
                    "sa zobrazí, keď zvolíš dnešný dátum.</div>")

    # ── BASELINE (bez batérie + bez plánu) — pre /dentrh kind = 15-min, ceny sú reálne OTE DT ──
    _bl_dentrh = ""
    if bc is not None:
        try:
            _pui_plan = _ui_load("plan", {}) or {}
            _bp = bc.parse_baseline_params(_pui_plan)
            # Bug VV (2026-06-08): baseline must include same TOU + grid_fee as plán scenár
            _bl_tou = None
            _bl_gf = 0.0
            try:
                import settlement as _stl_bl
                from core.profile_resolver import get_active as _ga_bl
                _prof_bl = _ga_bl()
                if _stl_bl.profile_uses_tou(_prof_bl):
                    import datetime as _dt_bl
                    _bl_tou = _stl_bl.get_tou_for_day(_prof_bl, _dt_bl.date.today().isoformat(), T=n, dt_h=0.25)
                _bl_gf = float(_pui_plan.get("grid_fee", 0.0) or 0.0)
            except Exception:
                pass
            _bl = bc.compute_baseline_day(
                np.asarray(pv15[:n], float),
                np.asarray(load96[:n] if load96 is not None else np.zeros(n), float),
                np.asarray(price15[:n], float),
                im_mode=_bp["im_mode"], im_val=_bp["im_val"],
                ex_mode=_bp["ex_mode"], ex_val=_bp["ex_val"], dt=0.25,
                tou_eur_per_mwh=_bl_tou, grid_fee_eur_per_mwh=_bl_gf)
            _bl_net = float(_bl["net_profit"])
            _benefit = float(summ.get("ZISK_EUR", 0.0)) - _bl_net
            _bl_mode_txt = (f"Import: {('DT×' + str(_bp['im_val'])) if _bp['im_mode'] == 'dt_x' else (str(_bp['im_val']) + ' €/MWh fix')} · "
                             f"Export: {('DT×' + str(_bp['ex_val'])) if _bp['ex_mode'] == 'dt_x' else (str(_bp['ex_val']) + ' €/MWh fix')}")
            _bl_dentrh = (
                f"<div style='background:#fff3cd;border:1px solid #ffe399;border-radius:10px;padding:10px 14px'>"
                f"<div style='font-size:12px;color:#7a5d00'>Baseline (bez batérie + plánu)</div>"
                f"<div style='font-size:20px;font-weight:600;color:#7a5d00'>{_bl_net:+.1f} €</div>"
                f"<div style='font-size:11px;color:#888'>{_bl_mode_txt}</div></div>"
                f"<div style='background:#e8f5e9;border-radius:10px;padding:10px 14px'>"
                f"<div style='font-size:12px;color:#1B5E20'>Prínos batérie + plánu</div>"
                f"<div style='font-size:20px;font-weight:600;color:#2E7D32'>{_benefit:+.1f} €</div></div>")
        except Exception:
            _bl_dentrh = ""
    cards = "".join(
        f"<div style='background:#f3f6fb;border-radius:10px;padding:10px 14px'>"
        f"<div style='font-size:12px;color:#666'>{lab}</div>"
        f"<div style='font-size:20px;font-weight:600;color:{col}'>{val}</div></div>"
        for lab, val, col in [("Zisk (s batériou)", f"{summ['ZISK_EUR']:.1f} €", "#2E7D32"),
                              ("Bez batérie (LP)", f"{summ['bez_baterie_EUR']:.1f} €", "#222"),
                              ("Prínos batérie", f"{summ['prinos_baterie_EUR']:.1f} €", "#1F4E78"),
                              ("Cykly", f"{cycles:.2f}", "#1F4E78")]) + _bl_dentrh
    chp = np.mean([price[i] for i in range(len(batt)) if batt[i] < -0.5]) if any(b < -0.5 for b in batt) else float("nan")
    dip = np.mean([price[i] for i in range(len(batt)) if batt[i] > 0.5]) if any(b > 0.5 for b in batt) else float("nan")
    info = (f"Výroba FTV: <b>{sch.pv_kwh.sum():.0f} kWh</b> &nbsp;•&nbsp; "
            f"orezané: <b>{summ['orezane_kWh']:.0f} kWh</b> &nbsp;•&nbsp; "
            f"nabíja pri ~<b>{chp:.0f} €/MWh</b>, vybíja pri ~<b>{dip:.0f} €/MWh</b> "
            f"(spread <b>{dip-chp:.0f} €/MWh</b>) &nbsp;•&nbsp; cyklov: <b>{cycles:.2f}</b>")
    if abs(cal - 1) > 1e-9:
        info += f"<br><span style='color:#2E7D32'>Kalibrácia výroby ({d.strftime('%Y-%m')}): ×{cal:.3f}</span>"

    rows = ""
    mult_arr_use = np.asarray(mult96, dtype=float).reshape(-1)
    rt_arr_use = np.asarray(rt_mask96, dtype=float).reshape(-1)
    base_kw = sch_base["batt_kw"].values.astype(float)
    for i, (_, r) in enumerate(sch.iterrows()):
        pc = "#C00000" if r.price_eur < 0 else "#222"
        bt = ("#2E7D32" if r.batt_kw > 0 else ("#C49000" if r.batt_kw < 0 else "#999"))
        bk = float(base_kw[i]) if i < len(base_kw) else 0.0
        bbt = ("#2E7D32" if bk > 0 else ("#C49000" if bk < 0 else "#999"))
        cu = float(r.curtail_kwh)
        cucol = "#C0392B" if cu > 0.05 else "#ccc"
        hl = " style='background:#fff7e6'" if (d == dt.date.today() and i == (dt.datetime.now().hour*4 + dt.datetime.now().minute//15)) else ""
        mv = float(mult_arr_use[i]) if i < len(mult_arr_use) else 1.0
        rt_on = (rt_arr_use[i] > 0.5) if i < len(rt_arr_use) else True
        m_cls = "" if abs(mv - 1.0) < 1e-6 else " style='background:#fff7e6'"
        rt_cls = "" if rt_on else " style='background:#ffe2e2'"
        diff = abs(float(r.batt_kw) - bk)
        fin_emp = " style='font-weight:700;background:#f0f7e6'" if diff > 0.5 else f" style='color:{bt};font-weight:600'"
        # × a RT už NIE SÚ editovateľné v detail tabuľke — sú v /dentrh formulári (sekcia "× a RT šablóna").
        _load_val = float(r.load_kwh) if hasattr(r, 'load_kwh') and 'load_kwh' in sch.columns else 0.0
        _load_cls = "" if _load_val < 0.05 else " style='color:#7030A0;font-weight:600'"
        rows += (f"<tr{hl}><td>{times[i]}</td><td>{r.pv_kwh:.1f}</td>"
                 f"<td{_load_cls}>{_load_val:.2f}</td>"
                 f"<td style='color:{cucol};font-weight:{600 if cu>0.05 else 400}'>{cu:.1f}</td>"
                 f"<td style='color:{pc}'>{r.price_eur:.1f}</td>"
                 f"<td style='color:{bbt};font-size:11px'>{bk:+.1f}</td>"
                 f"<td{fin_emp}>{r.batt_kw:+.1f}</td>"
                 f"<td>{r.grid_kwh:+.1f}</td><td>{r.soc_pct:.0f}</td>"
                 f"<td{m_cls}>{mv:.2f}</td>"
                 f"<td{rt_cls} style='text-align:center'>{'✓' if rt_on else '✗'}</td></tr>")
    L = "[" + ",".join(f"'{t}'" for t in times) + "]"
    P = "[" + ",".join(f"{x:.1f}" for x in price) + "]"
    B = "[" + ",".join(f"{x:.1f}" for x in batt) + "]"
    S = "[" + ",".join(f"{x:.0f}" for x in socp) + "]"
    C = "[" + ",".join(f"{x:.1f}" for x in sch["curtail_kwh"].tolist()) + "]"
    PVU = "[" + ",".join(f"{max(float(p)-float(c),0):.1f}"
                         for p, c in zip(sch["pv_kwh"], sch["curtail_kwh"])) + "]"
    body = f"""<style>
table{{border-collapse:collapse;width:100%;font-size:13px}}
table th,table td{{border:1px solid #e3e3e3;padding:4px 8px;text-align:right}}
table th{{background:var(--primary);color:#fff;position:sticky;top:0}}
table td:first-child{{text-align:center}}
.wrap{{max-height:420px;overflow:auto;border-radius:8px}}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<h1>⚡ Denný trh 15-min — {date}</h1>
{now_card}
<div style="display:flex;gap:12px;margin:12px 0;flex-wrap:wrap">{cards}</div>
<p style="background:#f8f9fb;border-radius:8px;padding:8px 12px;font-size:14px">{info}</p>
{_carried_soc_banner(date, soc_init) + _overrides_status(date) + (f'<div style="background:#e8f5e9;border-left:4px solid #2E7D32;border-radius:6px;padding:8px 12px;margin:6px 0;color:#1B5E20;font-size:13px">💾 <b>Plán uložený</b> do <code>{plan_saved_path}</code></div>' if plan_saved_path and not str(plan_saved_path).startswith("ERR") else (f'<div style="background:#fff3cd;border-left:4px solid #f0b80f;border-radius:6px;padding:8px 12px;margin:6px 0;color:#7a5c00;font-size:13px">⚠ Plán sa NEpodarilo uložiť: {plan_saved_path}</div>' if plan_saved_path else '')) + ((f'<div style="background:{"#e8f5e9" if mult_msg.startswith(chr(10003)) else "#fff3cd"};border-left:4px solid {"#2E7D32" if mult_msg.startswith(chr(10003)) else "#f0b80f"};border-radius:6px;padding:10px 14px;margin:8px 0;color:{"#1B5E20" if mult_msg.startswith(chr(10003)) else "#7a5c00"};font-size:14px;font-weight:500">{mult_msg}</div>') if mult_msg else '') + _render_mult_warnings(summ)}
<div style="height:320px;margin:12px 0"><canvas id="ch"></canvas></div>
<h2>Predikcia FTV a orezanie</h2>
<div style="height:260px;margin:12px 0"><canvas id="ch2"></canvas></div>
<h2>Rozvrh po 15 min</h2>
<div class="wrap"><table><tr><th>čas</th><th>FTV kWh</th><th title="predikovaná spotreba zákazníka z naimportovaného profilu (15-min slot)">Load kWh</th><th>Orezané kWh</th><th>DT €/MWh</th><th title="návrh optimizéra bez ručnej úpravy">Návrh kW</th><th title="finálny plán po násobiteľoch a SOC oreze">FINÁL kW</th><th title="net sieť po odpočte load: + export / − import">Sieť kWh</th><th>SOC %</th><th title="ručný násobiteľ návrhu optimizéra">×</th><th title="RT odchýlka povolená (✓) alebo zablokovaná (□) v slote">RT</th></tr>
{rows}</table></div>
<p style="background:#eef5e0;border-left:4px solid #2E7D32;border-radius:6px;padding:10px 14px;margin:14px 0;font-size:13px;color:#1B5E20">
  ℹ <b>× a RT sa editujú v <a href='/dentrh' style='color:#1B5E20;font-weight:600'>/dentrh formulári</a></b> (sekcia "× a RT šablóna"), nie tu v detaile.
  Šablóna platí pre celý aktívny profil — pre všetky dni rovnako.
</p>
<p style="color:#666;font-size:13px">Batéria: + vybíja / − nabíja &nbsp;•&nbsp; Sieť: + predaj / − nákup &nbsp;•&nbsp;
Orezané kWh = znížená/vypnutá FTV (záporná cena / plná batéria) &nbsp;•&nbsp;
žltý riadok = aktuálny interval (ak je zvolený dnešok) &nbsp;•&nbsp; žltý podklad v stĺpci × = aktívny násobiteľ ≠ 1.00.</p>
<script>
new Chart(document.getElementById('ch'),{{data:{{labels:{L},datasets:[
{{type:'line',label:'DT cena €/MWh',data:{P},borderColor:'#1F4E78',borderWidth:2,pointRadius:0,yAxisID:'y'}},
{{type:'bar',label:'Batéria kW (+vybi/−nabi)',data:{B},backgroundColor:{B}.map(v=>v>=0?'rgba(46,125,50,.55)':'rgba(192,57,43,.55)'),yAxisID:'y1'}},
{{type:'line',label:'Orezaná FTV kWh',data:{C},borderColor:'#C0392B',backgroundColor:'rgba(192,57,43,.30)',fill:true,stepped:true,pointRadius:0,borderWidth:1.5,yAxisID:'y1'}},
{{type:'line',label:'SOC %',data:{S},borderColor:'#C49000',borderWidth:1.5,borderDash:[5,3],pointRadius:0,yAxisID:'y2'}}
]}},options:{{responsive:true,maintainAspectRatio:false,interaction:{{mode:'index',intersect:false}},
scales:{{y:{{position:'left',title:{{display:true,text:'€/MWh'}}}},
y1:{{position:'right',title:{{display:true,text:'kW'}},grid:{{color:(c)=>c.tick.value===0?'#333':'rgba(0,0,0,0)'}}}},
y2:{{position:'right',min:0,max:100,display:false}}}}}}}});
new Chart(document.getElementById('ch2'),{{type:'bar',data:{{labels:{L},datasets:[
{{label:'Dodaná FTV kWh',data:{PVU},backgroundColor:'rgba(46,125,50,.65)',stack:'ftv'}},
{{label:'Orezaná FTV kWh',data:{C},backgroundColor:'rgba(192,57,43,.75)',stack:'ftv'}}
]}},options:{{responsive:true,maintainAspectRatio:false,interaction:{{mode:'index',intersect:false}},
plugins:{{tooltip:{{callbacks:{{footer:(it)=>'Predikcia spolu: '+it.reduce((s,x)=>s+x.parsed.y,0).toFixed(1)+' kWh'}}}}}},
scales:{{x:{{stacked:true}},y:{{stacked:true,title:{{display:true,text:'kWh / 15 min'}}}}}}}}}});
</script>
"""
    return render_legacy_body(None, f"Denný trh {date}", body)


def _data_page(report=None, logs=None, request=None):
    """Render /data stránky cez Jinja2 (Fáza 3 refactor)."""
    import backfill as bf
    from ui.templates import render
    coverage = bf.coverage()
    return render(request, "pages/data.html",
                   coverage=coverage, report=report, logs=logs)


import threading as _threading
_LIVESIM_LOCK = _threading.Lock()   # vlákno na pozadí aj prehliadač zapisujú do toho istého logu → serializuj

# Bug W (2026-06-08): /livesim GET nesmie zbytocne pretacat advance() pri kazdom
# otvoreni dashboardu. BG scheduler tick volá advance() každú minútu → CSV+meta.json
# sa aktualizuje. Medzi tickami nema dashboard čo nového počítať.
#
# Strategy: cache `r` dict per (case, port, profile) podla meta.json mtime. Pokial
# meta nezmenila od posledneho hitu, vratime cachovane r → ZIADNE volanie advance().
# Cache invaliduje pri prvom GET po BG ticku (meta sa zmenila) + uloží nove r.
#
# Toto je medzistupeň pred plnym refactorom (BG by mal zapisovat snapshot na disk).
# Aktualne riešenie: prvý GET po každom BG tick = 1× advance, ostatné GETy z cache.
_LIVESIM_R_CACHE: dict = {}            # (case, port, profile) → (meta_mtime, r_dict)
_LIVESIM_R_CACHE_LOCK = _threading.Lock()
# Bug COMPUTE-WORKER (2026-06-11): background compute stav — request nikdy nepočíta.
_LIVESIM_COMPUTE_INFLIGHT = {}    # key → started_ts (epoch s)
_LIVESIM_COMPUTE_ERR = {}         # key → posledná chyba background behu (str)
_LIVESIM_BG_RR = 0                # round-robin offset pre bg tick (BG-STAMPEDE throttle)
_LIVESIM_COMPUTE_PROGRESS = {}    # key → {done, total, day} pre progress bar (BG-PROGRESS)


def _livesim_meta_mtime(case: str, port: str, profile=None) -> float:
    """Vráti cache-signatúru livesim stavu (0.0 ak neexistuje). Per-profil.

    Bug SOC-UNIFY-TODAY (2026-06-14): VDT paper trades menia DNEŠNÝ engine SOC
    (Bug BB ich pridá do plánu), ale zapisujú sa do vdt_paper_trades.csv — NIE do
    meta.json. Bez nich v signatúre R-cache nepadne pri novom obchode → /livesim
    servíruje zastaraný dnešok (SOC pred obchodom), kým /vdt číta čerstvé trades →
    rôzne SOC. Zahrň mtime VDT trades → nový obchod invaliduje cache → dnešok sa
    prepočíta čerstvo (engine RT+DT+VDT) a meta.today_soc_pct je aktuálny."""
    try:
        if lsim is None:
            return 0.0
        _, meta_path = lsim.paths(case, port, profile or None)
        m = os.path.getmtime(meta_path)
        try:
            import vdt_live_advisor as _vla_sig
            _vp = _vla_sig.paper_trades_csv_path(profile or None)
            m += os.path.getmtime(_vp)
        except Exception:
            pass
        return m
    except Exception:
        return 0.0


def _livesim_cache_store(case, port, profile, r):
    """BG tick po advance uloží svoj výsledok `r` do render-cache, aby OTVORENIE
    profilu bolo OKAMŽITÉ (cache hit, žiadny prepočet).

    Bug BG-INVALIDATES-CACHE (2026-06-13, user: "druhé otvorenie toho istého profilu
    zas prepočítava"): _LIVESIM_R_CACHE je platná len kým sa meta.json mtime nezmení.
    Lenže bg-all každý tick posúva profil → prepíše meta → mtime sa zmení → GET cache
    VŽDY padne → re-open prepočítava. bg pritom `r` už má spočítané — len ho zahadzoval.
    Teraz ho uloží pod kľúč (case, port, profile) s aktuálnym mtime → GET ho rovno vráti.
    Kľúč = surové meno profilu (zhodné s GET `_profile_key = get_active()` a bg list_profiles)."""
    try:
        if not isinstance(r, dict):
            return
        m = _livesim_meta_mtime(case, port, profile or None)
        if m > 0:
            with _LIVESIM_R_CACHE_LOCK:
                _LIVESIM_R_CACHE[(case, port, str(profile or ""))] = (m, r)
    except Exception:
        pass


# ── COLD-START persistencia r-cache (2026-06-14) ───────────────────────────
# Po reštarte kontajnera je in-memory _LIVESIM_R_CACHE prázdna → prvé otvorenie
# /livesim by čakalo na bg compute (progress page). Persistujeme posledné hotové
# `r` (vrátane dnešného trace) na disk vedľa livesim meta; pri cold hydratujeme
# in-memory cache → prvé otvorenie po reštarte je okamžité. Fail-soft.
def _livesim_rcache_path(case, port, profile=None):
    try:
        if lsim is None:
            return None
        _, meta_path = lsim.paths(case, port, profile or None)
        if meta_path.endswith(".meta.json"):
            return meta_path[:-len(".meta.json")] + ".rcache.pkl"
        return meta_path + ".rcache.pkl"
    except Exception:
        return None


def _livesim_rcache_save(case, port, profile, mtime, r):
    p = _livesim_rcache_path(case, port, profile)
    if not p or r is None:
        return
    try:
        import pickle as _pk
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            _pk.dump((float(mtime), r), f, protocol=_pk.HIGHEST_PROTOCOL)
        os.replace(tmp, p)
    except Exception as _e:
        print(f"[livesim rcache save] {_e}")


def _livesim_rcache_load(case, port, profile):
    p = _livesim_rcache_path(case, port, profile)
    if not p or not os.path.exists(p):
        return None
    try:
        import pickle as _pk
        with open(p, "rb") as f:
            mt, r = _pk.load(f)
        if isinstance(r, dict) and ("today_trace" in r or "cum_total" in r):
            return (float(mt), r)
    except Exception as _e:
        print(f"[livesim rcache load] {_e}")
    return None


def _livesim_cached_advance(case, start, port, base_case, d1_step_min,
                              live_minutes, rt_params, plan_params,
                              use_rt_override, profile_key: str = ""):
    """Wrapper okolo lsim.advance s mtime-based cache.

    Bug W (2026-06-08): pokial sa meta.json nezmenila od posledneho hitu (= BG tick
    nebol), vracia cachovane `r` dict — ZIADNE volanie advance() = ziadne pocitanie
    pri otvarani dashboardu. Cache key zahrna profile_key aby per-profil isolacia
    fungovala (rovnaky port, rozne profily by mali samostatne caches).
    """
    key = (case, port, profile_key)
    mtime_before = _livesim_meta_mtime(case, port, profile_key)
    with _LIVESIM_R_CACHE_LOCK:
        cached = _LIVESIM_R_CACHE.get(key)
        if os.environ.get("LIVESIM_TIMING") == "1":
            _cst = ("FRESH" if (cached is not None and cached[0] == mtime_before and mtime_before > 0)
                    else ("STALE" if cached is not None else "MISS"))
            print(f"[SWITCH-INSTANT] profile={profile_key} cache={_cst} "
                  f"cached_mtime={cached[0] if cached else None} now_mtime={mtime_before} "
                  f"n_cache={len(_LIVESIM_R_CACHE)}")
        if cached is not None and cached[0] == mtime_before and mtime_before > 0:
            return cached[1]
    # COLD-START hydrate (2026-06-14): in-memory cache prázdna (po reštarte) → načítaj
    # persistovaný r z disku, naplň cache. Ak jeho mtime == aktuálnej meta → vráť rovno
    # (instant, žiadny bg). Inak poslúži ako _stale fallback nižšie + bg dopočíta čerstvý.
    if cached is None:
        _disk = _livesim_rcache_load(case, port, profile_key)
        if _disk is not None:
            with _LIVESIM_R_CACHE_LOCK:
                if _LIVESIM_R_CACHE.get(key) is None:
                    _LIVESIM_R_CACHE[key] = _disk
                cached = _LIVESIM_R_CACHE.get(key)
            if _disk[0] == mtime_before and mtime_before > 0:
                return _disk[1]
    # Bug COMPUTE-WORKER (2026-06-11): cache miss → advance beží v BACKGROUND vlákne,
    # request NEBLOKUJE. Vraciame posledný hotový stav (_stale=True) alebo None
    # (= prvý beh bez akéhokoľvek stavu → volajúci ukáže progress stránku).
    import time as _t_cw

    def _progress_cb(done, total, day_iso):
        with _LIVESIM_R_CACHE_LOCK:
            _LIVESIM_COMPUTE_PROGRESS[key] = {"done": int(done), "total": int(total),
                                              "day": day_iso, "ts": _t_cw.time()}

    def _do_compute():
        try:
            # BG-PROGRESS init (2026-06-13, user: "pri prepnutí profilu zmizol bar"):
            # odhadni total dní (start→dnes) a nastav progress HNEĎ — bar sa ukáže
            # na 0 % aj počas čakania na _LIVESIM_LOCK / prípravy dát (predtým total=0
            # → "pripravujem dáta" bez baru). advance progress_cb to potom spresní.
            try:
                _est_start = pd.to_datetime(start).date() if start else (dt.date.today() - dt.timedelta(days=7))
                _est_total = max(1, (dt.date.today() - _est_start).days + 1)
            except Exception:
                _est_total = 1
            with _LIVESIM_R_CACHE_LOCK:
                _LIVESIM_COMPUTE_PROGRESS[key] = {"done": 0, "total": _est_total,
                                                  "day": "", "ts": _t_cw.time()}
            with _LIVESIM_LOCK:
                r_bg = lsim.advance(case, start, port=port, base_case=base_case,
                                    d1_step_min=d1_step_min,
                                    live_minutes=live_minutes, rt_params=rt_params,
                                    plan_params=plan_params, use_rt_override=use_rt_override,
                                    profile=profile_key or None, progress_cb=_progress_cb)
            # Bug SOC-CONT-V3: po advance over drift LP plánov budúcich dní voči
            # meta.soc_after_done → auto-regen + prepočet projekcie.
            try:
                if _auto_regen_stale_plans(case, port, profile=profile_key or None):
                    with _LIVESIM_LOCK:
                        r_bg = lsim.advance(case, start, port=port, base_case=base_case,
                                            d1_step_min=d1_step_min,
                                            live_minutes=live_minutes, rt_params=rt_params,
                                            plan_params=plan_params,
                                            use_rt_override=use_rt_override,
                                            profile=profile_key or None)
            except Exception as _e_v3:
                print(f"[SOC-CONT-V3] auto-regen check zlyhal: {_e_v3}")
            m_after = _livesim_meta_mtime(case, port, profile_key)
            with _LIVESIM_R_CACHE_LOCK:
                _LIVESIM_R_CACHE[key] = (m_after, r_bg)
                _LIVESIM_COMPUTE_ERR.pop(key, None)
            # COLD-START: persistuj na disk (mimo locku — IO) → prežije reštart kontajnera
            _livesim_rcache_save(case, port, profile_key, m_after, r_bg)
        except Exception as _e_bg:
            import traceback as _tb_bg
            with _LIVESIM_R_CACHE_LOCK:
                _LIVESIM_COMPUTE_ERR[key] = str(_e_bg)[:800]
            print(f"[livesim-worker] {key} zlyhal:\n{_tb_bg.format_exc()[:1500]}")
        finally:
            with _LIVESIM_R_CACHE_LOCK:
                _LIVESIM_COMPUTE_INFLIGHT.pop(key, None)
                _LIVESIM_COMPUTE_PROGRESS.pop(key, None)

    with _LIVESIM_R_CACHE_LOCK:
        _already_running = key in _LIVESIM_COMPUTE_INFLIGHT
        if not _already_running:
            _LIVESIM_COMPUTE_INFLIGHT[key] = _t_cw.time()
            _LIVESIM_COMPUTE_ERR.pop(key, None)
    if not _already_running:
        _threading.Thread(target=_do_compute, daemon=True,
                          name=f"livesim-worker-{case}").start()
    # SWITCH-INSTANT (2026-06-14): ak existuje AKÝKOĽVEK cache (čerstvý alebo zastaraný),
    # vráť ho HNEĎ — bg compute beží na pozadí, ďalší refresh dá čerstvý. Žiadne 2s grace
    # čakanie pri prepnutí profilu (drahé profily ~4 s nestihnú grace → predtým user čakal).
    # Stale flag len keď sa mtime líši (živá minúta/obchod pribudol od posledného compute).
    with _LIVESIM_R_CACHE_LOCK:
        cached_now = _LIVESIM_R_CACHE.get(key)
        _started = _LIVESIM_COMPUTE_INFLIGHT.get(key)
    if cached_now is not None:
        r_out = dict(cached_now[1])
        if cached_now[0] != mtime_before:
            r_out["_stale"] = True
            r_out["_stale_since"] = _started
        return r_out
    # Žiadny cache (úplne prvé otvorenie profilu) → krátka grace, či compute dobehne,
    # inak None → volajúci ukáže progress stránku (a bg dopočíta + zapíše COLD-START pkl).
    _deadline = _t_cw.time() + 2.0
    while _t_cw.time() < _deadline:
        with _LIVESIM_R_CACHE_LOCK:
            _done = key not in _LIVESIM_COMPUTE_INFLIGHT
            cached2 = _LIVESIM_R_CACHE.get(key)
        if cached2 is not None:
            r_out = dict(cached2[1])
            if not _done:
                r_out["_stale"] = True
            return r_out
        if _done:
            break                                        # skončil s chybou — rieši sa nižšie
        _t_cw.sleep(0.1)
    return None


def _livesim_compute_status(case: str, port: str, profile_key: str = "") -> dict:
    """Stav background compute pre (case, port, profile): running/started/err."""
    key = (case, port, profile_key)
    with _LIVESIM_R_CACHE_LOCK:
        return dict(running=key in _LIVESIM_COMPUTE_INFLIGHT,
                    started=_LIVESIM_COMPUTE_INFLIGHT.get(key),
                    err=_LIVESIM_COMPUTE_ERR.get(key),
                    progress=dict(_LIVESIM_COMPUTE_PROGRESS.get(key) or {}))


def _livesim_cache_invalidate(case: str = None):
    """Zruší cache pre konkrétny case (alebo všetky ak case=None).
    Použiteľné pri zmene profile/template — donúti rebuild pri ďalšom GET.
    """
    with _LIVESIM_R_CACHE_LOCK:
        if case is None:
            _LIVESIM_R_CACHE.clear()
        else:
            for k in list(_LIVESIM_R_CACHE.keys()):
                if k[0] == case:
                    _LIVESIM_R_CACHE.pop(k, None)


def _find_stale_future_plans(case: str = "plan_d1", port: str = None, max_days: int = 7, profile=None):
    """Bug SOC-CONT-V3 (2026-06-11): nájde LP plány pre BUDÚCE dni (date > meta.done_through)
    ktorých soc_init (schedule.soc_pct[0]) sa líši od meta.soc_after_done o > 1 %.
    Také plány boli generované PRED livesim regenom (race condition) → graf má SOC skok
    medzi koncom done_through a začiatkom ďalšieho dňa.

    Vracia dict: {carried_pct, done_through, step_min, kind, stale=[(date_iso, soc0_pct), ...]}
    alebo None ak meta/livesim nie sú k dispozícii."""
    if lsim is None or ps is None:
        return None
    try:
        _, meta_path = lsim.paths(case, port or _PORT, profile or None)
        with open(meta_path) as f:
            meta = json.load(f)
    except Exception:
        return None
    done_through = meta.get("done_through")
    soc_after = meta.get("soc_after_done")
    try:
        bkwh = float((meta.get("params") or {}).get("batt_kwh", 0.0) or 0.0)
    except (TypeError, ValueError):
        bkwh = 0.0
    if not done_through or soc_after is None or bkwh <= 0:
        return None
    carried_pct = float(soc_after) / bkwh * 100.0
    step_min = 60 if case == "plan_d1" else 15
    kind = "plan" if case == "plan_d1" else "dentrh"
    try:
        done_d = dt.date.fromisoformat(str(done_through))
    except (TypeError, ValueError):
        return None
    stale = []
    for off in range(1, max_days + 1):
        d_iso = (done_d + dt.timedelta(days=off)).isoformat()
        plan = ps.load_plan_safe(d_iso, step_min, kind=kind)
        if not plan:
            continue
        soc_arr = (plan.get("schedule") or {}).get("soc_pct") or []
        try:
            soc0 = float(soc_arr[0])
        except (IndexError, TypeError, ValueError):
            continue
        if abs(soc0 - carried_pct) > 1.0:
            stale.append((d_iso, soc0))
    return dict(carried_pct=carried_pct, done_through=str(done_through),
                step_min=step_min, kind=kind, stale=stale)


# Guard: auto-regen bež najviac RAZ pre každú (profile, case, done_through, soc_after_done)
# kombináciu — chráni pred opakovanými LP behmi keď regen nedotiahne drift pod 1 %
# (LP zaokrúhľuje soc_pct) alebo keď regen zlyhá.
_PLAN_AUTOREGEN_GUARD = {}
_PLAN_AUTOREGEN_LOCK = _threading.Lock()


def _auto_regen_stale_plans(case: str, port: str = None, profile=None) -> list:
    """Bug SOC-CONT-V3 (2026-06-11): po livesim advance automaticky regeneruje LP plány
    pre budúce dni ktorých soc_init je zastaraný voči meta.soc_after_done (drift > 1 %).
    Tým graf aj nominácia ostanú kontinuálne bez ručného /plan_batch hotfixu.

    Vracia zoznam regenerovaných dátumov (prázdny ak nič netreba)."""
    found = _find_stale_future_plans(case, port, profile=profile)
    if not found or not found["stale"]:
        return []
    try:
        profile = ps.resolve_profile(None)
    except Exception:
        profile = "default"
    gkey = (profile, case)
    gval = (found["done_through"], round(found["carried_pct"], 2))
    with _PLAN_AUTOREGEN_LOCK:
        if _PLAN_AUTOREGEN_GUARD.get(gkey) == gval:
            return []                                       # už riešené pre tento stav meta
        _PLAN_AUTOREGEN_GUARD[gkey] = gval                  # nastav HNEĎ — žiadne retry loopy
    ui_key = "plan" if found["step_min"] == 60 else "dentrh"
    fp = dict(_ui_load(ui_key, DEF))
    regen = []
    for d_iso, soc0 in found["stale"]:
        try:
            print(f"[SOC-CONT-V3] {d_iso}: LP plán má soc_init={soc0:.1f}% ale "
                  f"meta.soc_after_done={found['carried_pct']:.1f}% "
                  f"(done_through={found['done_through']}) → auto-regen")
            _gen_one_plan(d_iso, found["step_min"], found["kind"], fp)
            regen.append(d_iso)
        except Exception as _e_regen:
            print(f"[SOC-CONT-V3] auto-regen {d_iso} zlyhal: {_e_regen}")
    if regen:
        # Bug CACHE-WIPE-2 + STALE-NOT-DROP (2026-06-15): po auto-regene NEodstraňuj cache entry
        # (to spôsobilo plnú progress stránku pri ďalšom otvorení — user: "preplo sa to naspäť do
        # výpočtového") a NIE celý case (to mazalo ostatné profily). Namiesto toho len TENTO profil
        # OZNAČ ako stale (mtime=-1) a NECHAJ staré r → SWITCH-INSTANT vráti staré dáta + banner
        # "prepočítava sa", bg medzitým prepočíta čerstvé. Žiadny skok do výpočtovej stránky.
        with _LIVESIM_R_CACHE_LOCK:
            for _k in list(_LIVESIM_R_CACHE.keys()):
                if _k[0] == case and _k[2] == str(profile or "") and _LIVESIM_R_CACHE.get(_k):
                    _LIVESIM_R_CACHE[_k] = (-1.0, _LIVESIM_R_CACHE[_k][1])
    return regen


def _livesim_pred_dt(today):
    """PREDIKOVANÉ hodinové DT ceny pre dnešok (rovnaký model ako generátor plánu /plan).
    Vráti dict {ts15 -> cena} pre celý deň, alebo None pri zlyhaní. CESTA B – plán ako pri nominácii."""
    try:
        f = _ui_load("plan", DEF)
        d = pd.Timestamp(today).date()
        # Pre batt-only profily (kwp=0) syntetický wx grid (nula FTV, neutrálne počasie pre ISOT model)
        if float(f.get("kwp") or 0) > 0.01:
            wx = _fetch_pv_cached(f["lat"], f["lon"], f["kwp"], f["tilt"], f["azimuth"], f["eff"], start=d, end=d)
            wx["time"] = pd.to_datetime(wx["time"]); wx = wx[wx.time.dt.date == d].copy()
            if wx.empty:
                return None
        else:
            wx = pd.DataFrame({
                "time": pd.date_range(pd.Timestamp(d), periods=24, freq="h"),
                "kw": np.zeros(24), "gti": np.zeros(24),
                "temp": np.full(24, 15.0), "cloud": np.full(24, 50.0),
            })
        hist = _isot_history(d, days=8)
        wx2 = wx[["time", "gti", "temp", "cloud"]].copy(); wx2["isot_eur"] = np.nan
        h2 = hist.copy()
        for c in ["gti", "temp", "cloud"]:
            h2[c] = np.nan
        ctx = pd.concat([h2[["time", "isot_eur", "gti", "temp", "cloud"]], wx2], ignore_index=True)
        pred = _model().predict(ctx)
        dayp = pred[pred.time.dt.date == d][["time", "pred_isot"]].sort_values("time")
        if len(dayp) < 24:
            return None
        out = {}
        for _, row in dayp.iterrows():
            t = pd.Timestamp(row["time"]).floor("h"); p = float(row["pred_isot"])
            for q in range(4):
                out[t + pd.Timedelta(minutes=15*q)] = p     # hodinová predikcia → 4×15-min
        return out
    except Exception:
        return None


def _livesim_rt_params(cfg):
    """RT parametre riadiaceho signálu rovnako, ako ich vidí /rt poradca: posuvníky uložené v UI stave 'rt'
    (margin/bchg/kdis/kchg/dtk/rboost), inak hodnoty z prípadu. → livesim riadi rovnako ako poradca."""
    s = _ui_load("rt", {})
    def g(key, caseval):
        v = s.get(key, None)
        try:
            return float(v) if v is not None else float(caseval)
        except Exception:
            return float(caseval)
    return dict(kdis=g("kdis", cfg.rt_kdis), kchg=g("kchg", cfg.rt_kchg),
                dtk=g("dtk", cfg.dt_bias_k), rboost=g("rboost", getattr(cfg, "reversal_boost", 0.0)))


def _livesim_live_minutes():
    """Dnešné ŽIVÉ minúty pre real-time livesim. DT (day-ahead) sa naplní pre CELÝ deň
    (známy D-1 z aukcie → plán musí mať celodenné ceny), signál (sys/aktivácie) + odhad ZCO len po „teraz".
    Znovu používa overený /rt fetch. Vráti None, ak čokoľvek zlyhá.

    SK trh: kombinuje SK SEPS reg.výkon + SK OKTE DT/VDT/ZCO z historianu;
    FRR aktivácie ostávajú z CZ ako proxy. Realizované v seps_sk.build_sk_live_minutes.

    Cache: TTL 45 s — UI sa refreshne každých 60 s, takže pri jednom otvorení sa live fetch
    spustí najviac raz (úspora 4-5 network volaní pri každej refresh)."""
    import time as _t
    _now_ts = _t.time()
    if _LIVE_FETCH_CACHE.get("df") is not None and (_now_ts - _LIVE_FETCH_CACHE.get("ts", 0)) < _LIVE_FETCH_TTL:
        return _LIVE_FETCH_CACHE["df"]
    try:
        today, now, isot, est, sysd, afrr, act, vdt, _fetch_errors = _rt_fetch()
        lf = _rt_live_frame(today, isot, sysd, afrr, act)

        # ── SK trh: postaviť SK live_minutes (SEPS sys_MW + SK OKTE ceny + CZ FRR proxy) ──
        try:
            _is_sk_live = (mk is not None and str(mk.get_active_market()).lower() == "sk")
        except Exception:
            _is_sk_live = False
        if _is_sk_live:
            try:
                import seps_sk as _seps_live
                sk_result = _seps_live.build_sk_live_minutes(today=today, cz_lf=lf)
                if sk_result is not None and not sk_result.empty:
                    print(f"[_livesim_live_minutes] SK: {len(sk_result)} min, "
                          f"sys_MW pokrytie: {sk_result['sys_MW'].notna().sum()}, "
                          f"ZCO pokrytie: {sk_result['zco_eur'].notna().sum()}")
                    _LIVE_FETCH_CACHE["df"] = sk_result
                    _LIVE_FETCH_CACHE["ts"] = _now_ts
                    return sk_result
            except Exception as _e:
                print(f"[_livesim_live_minutes] SK build zlyhal, fallback na CZ: {_e}")
        dtmap = {}
        if isot is not None and not isot.empty and "ts" in isot and "cena_EUR" in isot:
            dtmap = {pd.Timestamp(t).floor("15min"): float(c) for t, c in zip(isot["ts"], isot["cena_EUR"])}
        if not dtmap:
            return None                                   # bez celodenných DT cien nemá zmysel (plán by bol zlý)
        zmap = {}
        if est is not None and not est.empty and "ts" in est and "est_eur" in est:
            zmap = {pd.Timestamp(t).floor("15min"): float(c) for t, c in zip(est["ts"], est["est_eur"])}
        # celodenná minútová mriežka 00:00–23:59 — DT pre VŠETKY periódy (day-ahead je známy D-1)
        f0 = pd.Timestamp(today).normalize()
        grid = pd.date_range(f0, f0 + pd.Timedelta(hours=24) - pd.Timedelta(minutes=1), freq="1min")
        df = pd.DataFrame({"time": grid})
        df["ts15"] = df["time"].dt.floor("15min")
        pred_map = _livesim_pred_dt(today)                # CESTA B: predikované ceny (ako generátor plánu)
        if pred_map:
            df["isot_eur"] = df["ts15"].map(pred_map)
            if df["isot_eur"].isna().mean() > 0.5:        # predikcia nepokryla deň → fallback na reálny day-ahead
                df["isot_eur"] = df["ts15"].map(dtmap)
        else:
            df["isot_eur"] = df["ts15"].map(dtmap)        # fallback: reálny day-ahead
        df["zco_eur"] = df["ts15"].map(zmap)              # odhad ZCO (len kde už publikované)
        df["dt_real_eur"] = df["ts15"].map(dtmap)         # REÁLNY clearovaný day-ahead (na porovnanie s predikciou)
        vmap = {}
        if vdt is not None and not vdt.empty and "ts" in vdt and "cena_EUR" in vdt:
            vmap = {pd.Timestamp(t).floor("15min"): float(c) for t, c in zip(vdt["ts"], vdt["cena_EUR"])}
        df["vdt_eur"] = df["ts15"].map(vmap)              # vnútrodenný trh (VDT) – kde je k dispozícii
        # signál (sys_MW, aktivácie) z live rámca – len po „teraz“
        if lf is not None and not lf.empty and "time" in lf:
            lf = lf.copy()
            lf["time"] = pd.to_datetime(lf["time"]).dt.floor("min")
            sig_cols = [c for c in ["sys_MW", "aFRR_plus", "aFRR_minus", "mFRR_plus", "mFRR_minus", "mFRR5"]
                        if c in lf.columns]
            df = df.merge(lf[["time"] + sig_cols].drop_duplicates("time"), on="time", how="left")
        for c in ["aFRR_plus", "aFRR_minus", "mFRR_plus", "mFRR_minus", "mFRR5", "sys_MW"]:
            if c not in df.columns:
                df[c] = np.nan
        df["zco_eur"] = df["zco_eur"].ffill()             # 15-min blok ZCO podrž (zaostáva za signálom)
        df = df.dropna(subset=["isot_eur"])               # DT je nutné (máme celý deň)
        cols = ["time", "ts15", "sys_MW", "aFRR_plus", "aFRR_minus", "mFRR_plus",
                "mFRR_minus", "mFRR5", "isot_eur", "zco_eur", "dt_real_eur", "vdt_eur"]
        out = df[[c for c in cols if c in df.columns]]
        result = out if len(out) else None
        _LIVE_FETCH_CACHE["df"] = result; _LIVE_FETCH_CACHE["ts"] = _now_ts
        return result
    except Exception:
        _LIVE_FETCH_CACHE["df"] = None; _LIVE_FETCH_CACHE["ts"] = _now_ts
        return None


def _livesim_modes():
    cases = cc.list_cases()
    base = "realistic" if "realistic" in cases else (cases[0] if cases else "realistic")
    return {"plan_d1": ("Plán D-1 (hodinový)", base, 60),
            "dt_15min": ("Denný trh 15-min", base, 15)}


def _livesim_rt_params_from_profile(prof_plan_rt: dict, cfg):
    """RT signál params (kdis/kchg/dtk/rboost) z PROFILU (nie UI stavu) — pre bg tick
    per profil. Fallback na hodnoty z prípadu keď v profile chýbajú."""
    rt = prof_plan_rt or {}
    def g(key, caseval):
        v = rt.get(key, None)
        try:
            return float(v) if v is not None else float(caseval)
        except Exception:
            return float(caseval)
    return dict(kdis=g("kdis", cfg.rt_kdis), kchg=g("kchg", cfg.rt_kchg),
                dtk=g("dtk", cfg.dt_bias_k), rboost=g("rboost", getattr(cfg, "reversal_boost", 0.0)))


def _livesim_bg_profiles():
    """Profily, ktoré sa majú posúvať na pozadí: všetky v aktívnom trhu.
    AKTÍVNY profil je PRVÝ — jeho stav (užívateľov pohľad) je hotový najskôr,
    najmä pri dopočte po reštarte. (Rozsah ako VDT scheduler — list_profiles.)"""
    try:
        import profiles as _pr
        profs = [p for p in (_pr.list_profiles() or []) if p and p != "default"]
        try:
            from core.profile_resolver import get_active as _ga
            act = _ga(None)
            if act in profs:
                profs = [act] + [p for p in profs if p != act]
        except Exception:
            pass
        return profs
    except Exception:
        return []


def _livesim_bg_tick_one(case, start, _bc, _st, live, profile):
    """Posun jedného profilu na pozadí s JEHO vlastnými params z profilu.
    Bug BG-ALL-PROFILES (2026-06-13, user: "DT sa má počítať automaticky podľa
    nastavených parametrov pre všetky background profily; pri zobrazení sa len
    načíta aktuálny stav; po reštarte sa dopočítajú chýbajúce intervaly")."""
    try:
        import profiles as _pr
        _pdata = _pr.load_profile(profile) or {}
        plan_pp = dict(_pdata.get("plan") or DEF)
        rtp = _livesim_rt_params_from_profile(_pdata.get("rt") or {}, cc.load_case(_bc))
        with _LIVESIM_LOCK:
            r = lsim.advance(case, start, port=_PORT, base_case=_bc, d1_step_min=_st,
                             live_minutes=live, rt_params=rtp, plan_params=plan_pp,
                             use_rt_override=None, profile=profile)
        try:
            _auto_regen_stale_plans(case, _PORT, profile=profile)
        except Exception as _e_v3:
            print(f"[SOC-CONT-V3 bg/{profile}] {_e_v3}")
        # BG-INVALIDATES-CACHE fix: ulož `r` do render-cache → otvorenie tohto profilu okamžité
        _livesim_cache_store(case, _PORT, profile or None, r)
        return int(r.get("appended", 0))
    except Exception as _e_one:
        print(f"[livesim-bg/{profile}] preskočené: {_e_one}")
        return 0


def _livesim_bg_tick():
    """Jeden krok simulácie NA POZADÍ pre VŠETKY profily (BG-ALL-PROFILES, 2026-06-13).
    Per-profil úložisko (LIVESIM-PER-PROFILE) umožňuje posúvať každý profil nezávisle
    do JEHO súboru — žiadny thrashing. Po reštarte sa tým dopočítajú chýbajúce
    intervaly pre VŠETKY profily, takže otvorenie ktoréhokoľvek profilu = len cache.
    Vracia súčet pridaných minút. Jedno zlyhanie neblokuje ostatné."""
    try:
        MODES = _livesim_modes()
        saved = _ui_load("livesim", {"case": "plan_d1",
                                     "start": (dt.date.today() - dt.timedelta(days=7)).isoformat()})
        case = saved.get("case")
        if case not in MODES:
            case = "plan_d1"
        start = saved.get("start")
        _lbl, _bc, _st = MODES[case]
        live = _livesim_live_minutes()
        # Bug BG-LOOP-REVERT (2026-06-13, user: "5+ h sa nič nezapísalo, zacyklené"):
        # BG-ALL-PROFILES posúval každý profil s PROFILE.plan parametrami, kým GET
        # používa UI plan → rozdielne settings_sig → každý beh full re-backfill +
        # auto-regen menil plány → sig churn → nekonečný loop, ktorý držal lock a
        # GET worker aktívneho profilu hladoval. Návrat na pôvodné, OVERENÉ správanie:
        # bg tick posúva LEN aktívny profil s UI parametrami (zhodné s GET cestou →
        # stabilný sig, inkrementálne). Per-profil úložisko ostáva; ostatné profily
        # sa dopočítajú keď ich užívateľ otvorí (GET worker). BG-ALL-PROFILES späť až
        # po vyriešení sig-konzistencie (úloha #7).
        rtp = _livesim_rt_params(cc.load_case(_bc))
        plan_pp = _ui_load("plan", DEF)
        _bg_use_rt = saved.get("use_rt", None)
        # Bug BG-VS-GET-LOCK (2026-06-13, user: "deň 0/44 visí 466s"): bg tick aj GET
        # worker robia ten istý advance aktívneho profilu a súťažia o _LIVESIM_LOCK.
        # Keď bg drží lock (bez hlásenia progresu), GET worker (ktorého progress
        # stránka ukazuje) hladuje → bar zamrznutý na init 0/44. Fix: ak GET worker
        # už počíta tento profil (_INFLIGHT), bg tick PRESKOČ — nech to dokončí GET
        # worker, ktorý hlási progres. (Inak by sme robili dvojitú prácu + skrytý lock.)
        try:
            from core.profile_resolver import get_active as _ga_bg
            _act_bg = str(_ga_bg() or "")
        except Exception:
            _act_bg = ""
        with _LIVESIM_R_CACHE_LOCK:
            _get_busy = any(k[2] == _act_bg for k in _LIVESIM_COMPUTE_INFLIGHT)
        if _get_busy:
            return 0          # GET worker to počíta s progresom → nelez mu do zámku
        import time as _t_bgp
        _bg_key = (case, _PORT, _act_bg)
        def _bg_progress(d, t, day):
            with _LIVESIM_R_CACHE_LOCK:
                _LIVESIM_COMPUTE_PROGRESS[_bg_key] = {"done": int(d), "total": int(t),
                                                      "day": day, "ts": _t_bgp.time()}
        with _LIVESIM_LOCK:
            r = lsim.advance(case, start, port=_PORT, base_case=_bc, d1_step_min=_st,
                             live_minutes=live, rt_params=rtp, plan_params=plan_pp,
                             use_rt_override=_bg_use_rt, profile=_act_bg or None,
                             progress_cb=_bg_progress)
        try:
            _auto_regen_stale_plans(case, _PORT)
        except Exception as _e_v3:
            print(f"[SOC-CONT-V3 bg] {_e_v3}")
        # BG-INVALIDATES-CACHE fix: ulož `r` aktívneho profilu do render-cache → otvorenie okamžité
        _livesim_cache_store(case, _PORT, _act_bg or None, r)
        # Úloha #7 BG-ALL-PROFILES (re-enable 2026-06-13): keď LIVESIM_BG_ALL=1, posúvaj
        # na pozadí aj OSTATNÉ profily, ktorých súbor UŽ EXISTUJE (= inkrementálny
        # catch-up, lacné) → prepnutie na ne je okamžité (len cache). PLAN-SIG-CONTENT
        # už bráni sig-churn loopu (predošlá katastrofa). Čerstvé profily (bez súboru)
        # sa NEbackfillujú hromadne (žiadny stampede) — spočítajú sa pri prvom otvorení.
        # Skip ak GET worker profil počíta. FLAG-GATED, default OFF (najprv dev 8001).
        if os.environ.get("LIVESIM_BG_ALL") == "1":
            try:
                _cold_done_this_tick = False
                for _p2 in _livesim_bg_profiles():
                    if _p2 == _act_bg:
                        continue
                    with _LIVESIM_R_CACHE_LOCK:
                        if any(k[2] == _p2 for k in _LIVESIM_COMPUTE_INFLIGHT):
                            continue
                    try:
                        _csvp2, _ = lsim.paths(case, _PORT, _p2)
                    except Exception:
                        continue
                    # WARM-COLD (2026-06-15, user: "prepnutie profilu stale pocita desiatky
                    # sekund"): predzohrej AJ profily bez CSV (cold), nielen existujuce.
                    # Predtym sa cold profily nikdy nezohriali -> prve otvorenie = full backfill
                    # desiatky s. Teraz ich dopocita bg, ale LEN JEDEN cold za tick (ziadny
                    # stampede) -> postupne sa zohrievaju vsetky -> prepnutie = cache hit.
                    if not os.path.exists(_csvp2):
                        if _cold_done_this_tick:
                            continue                       # max 1 cold backfill za tick
                        _cold_done_this_tick = True
                        print(f"[livesim-bg WARM-COLD] dopocitavam cold profil {_p2} (prvy backfill)...")
                    _livesim_bg_tick_one(case, start, _bc, _st, live, _p2)
            except Exception as _e_all:
                print(f"[livesim-bg ALL] {_e_all}")
        return int(r.get("appended", 0))
    except Exception as e:
        print("[livesim-bg] preskočené:", e)
        return -1


@app.get("/livesim/chC_export")
def livesim_chC_export(case: str = "plan_d1", view: str = None):
    """Manažérsky Excel report zo živej simulácie.
    7 sheetov: Zhrnutie, Po_mesiacoch (+grafy), Po_dnoch (+grafy), Vsetky_15min,
               Detail_15min (+grafy), Per_minute (raw), Metadata."""
    if lsim is None:
        return Response("livesim nedostupný", status_code=500)
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.chart import BarChart, LineChart, Reference, BarChart3D
        from openpyxl.utils import get_column_letter
        from openpyxl.worksheet.table import Table, TableStyleInfo
        import io as _io
        # PLNÁ resolution (NIE decimovaná) — pre presné overenie výpočtu
        df = lsim.load_series(case, port=_PORT, max_points=10**9)
        if df is None or df.empty:
            return Response("Žiadne dáta v livesim logu pre tento prípad.", status_code=404)
        # baseline params + výpočet per minúta
        _pui_plan = _ui_load("plan", {}) or {}
        _bp = bc.parse_baseline_params(_pui_plan) if bc is not None else {"im_mode":"dt_x","im_val":1.0,"ex_mode":"dt_x","ex_val":1.0}
        _ftv_min = pd.to_numeric(df.get("ftv_min_real_kw", df.get("ftv_kw")), errors="coerce").fillna(0).values
        _load_min = pd.to_numeric(df.get("load_min_real_kw", pd.Series([0.0]*len(df))), errors="coerce").fillna(0).values
        _dtp = pd.to_numeric(df.get("dt_real_eur", df.get("dt_eur")), errors="coerce").fillna(0).values
        _net = _ftv_min - _load_min
        _ex_kwh = np.maximum(_net, 0.0) / 60.0
        _im_kwh = -np.minimum(_net, 0.0) / 60.0
        _p_imp = (_dtp * _bp["im_val"]) if _bp["im_mode"] == "dt_x" else np.full_like(_dtp, _bp["im_val"])
        _p_exp = (_dtp * _bp["ex_val"]) if _bp["ex_mode"] == "dt_x" else np.full_like(_dtp, _bp["ex_val"])
        _bl_rev = _ex_kwh * _p_exp / 1000.0
        _bl_cost = _im_kwh * _p_imp / 1000.0
        _bl_per_min = _bl_rev - _bl_cost
        df = df.copy()
        df["ftv_min_kw"] = _ftv_min
        df["load_min_kw"] = _load_min
        df["dt_real_eur"] = _dtp
        df["baseline_net_kw"] = _net
        df["baseline_export_kwh"] = _ex_kwh
        df["baseline_import_kwh"] = _im_kwh
        df["baseline_p_imp_eur_mwh"] = _p_imp
        df["baseline_p_exp_eur_mwh"] = _p_exp
        df["baseline_rev_eur"] = _bl_rev
        df["baseline_cost_eur"] = _bl_cost
        df["baseline_per_min_eur"] = _bl_per_min
        df["baseline_cum_eur"] = np.cumsum(_bl_per_min)
        # Reálna akcia batérie [kW] = plan_batt_kw + rt_dir * rt_power_pct/100 * batt_kw_max
        _bkw_max = float(_pui_plan.get("batt_kw", 100.0))
        _plan_b = pd.to_numeric(df.get("plan_batt_kw", pd.Series([0.0]*len(df))), errors="coerce").fillna(0).values
        _rt_d   = pd.to_numeric(df.get("rt_dir", pd.Series([0.0]*len(df))), errors="coerce").fillna(0).values
        _rt_p   = pd.to_numeric(df.get("rt_power_pct", pd.Series([0.0]*len(df))), errors="coerce").fillna(0).values
        _batt_raw = _plan_b + _rt_d * _rt_p / 100.0 * _bkw_max
        # Bug #610 (Excel _agg): clip predikciu na fyzické limity batt (rovnako ako livesim.py)
        df["batt_kw_actual"]   = np.clip(_batt_raw, -_bkw_max, +_bkw_max) if _bkw_max > 0 else _batt_raw
        df["batt_kwh_min"]     = df["batt_kw_actual"] / 60.0   # energia za minútu
        # DIST-FEE (2026-06-15): distribučná úspora zvlášť = grid_fee × (baseline_import − skutočný_import)
        _gf_x = float(_pui_plan.get("grid_fee", 0) or 0)
        # NETTO import (2026-06-16): poplatok na skutočný odber zo siete = max(load−FTV−batt,0);
        # nabíjanie z FTV netto import nezvýši (oslobodené), nabíjanie zo siete áno (TBB).
        _act_imp_kwh_x = np.maximum(_load_min - _ftv_min - df["batt_kw_actual"].values, 0.0) / 60.0
        df["dist_actual_import_kwh"] = _act_imp_kwh_x
        df["dist_fee_min"] = _gf_x * (_im_kwh - _act_imp_kwh_x) / 1000.0   # € za minútu
        # ZCO (zúčtovacia cena odchýlky) — pre 15-min agregácie potrebujeme priemer
        df["zco_eur_min"]      = pd.to_numeric(df.get("zco_eur", pd.Series([np.nan]*len(df))), errors="coerce")
        # Mesiac kľúč pre agregáty
        df["month"] = pd.to_datetime(df["time"]).dt.strftime("%Y-%m")

        # ── Spoločné štýly + formáty ──
        bold = Font(bold=True, color="FFFFFF")
        hdrfill = PatternFill("solid", fgColor="1F4E78")
        subhdr_font = Font(bold=True, size=12, color="1F4E78")
        section_fill = PatternFill("solid", fgColor="EEF3F9")
        kpi_label_font = Font(size=10, color="666666")
        kpi_value_font = Font(bold=True, size=12, color="222222")
        thin = Side(border_style="thin", color="CCCCCC")
        cell_border = Border(left=thin, right=thin, top=thin, bottom=thin)
        # Excel number formats
        FMT_EUR  = '#,##0.00 "€"'
        FMT_EUR0 = '#,##0 "€"'
        FMT_MWH  = '#,##0.00 "MWh"'
        FMT_KWH  = '#,##0 "kWh"'
        FMT_PCT  = '0.0 "%"'
        FMT_PRICE = '#,##0.00 "€/MWh"'
        FMT_KW   = '#,##0 "kW"'
        FMT_KWP  = '#,##0 "kWp"'
        FMT_CYC  = '0.00'
        FMT_INT  = '#,##0'

        # ── Agregačné helpery ──
        # Bug #603 (Excel export): rovnaký fix ako pre /livesim karty + graf.
        # rt_rev_realistic_min = skutočný financial impact (dev × ZCO), zatiaľ čo
        # rt_rev_min je teoretický výstup RT enginu. Excel report doteraz ukazoval
        # fiktívnu pokutu napr. -370€ pri reálnom impacte 0€.
        from core.effect import resolve_rt_col as _resolve_rt
        _rt_col_xl = _resolve_rt(df)
        # Bug #608: ak vdt_arb_min existuje, agreguj ho do vdt_arb_eur (samostatná zložka)
        _has_vdt_arb = "vdt_arb_min" in df.columns
        # Bug #649 (2026-06-09): rozklad RT zisku na komponenty pre atribúciu efektu.
        # User: 'efekt by sa mal pocitat z komodity a odchylky len z toho co je v
        # grafe riadenie' — rt_batt_eur = LEN odchýlka z batt riadenia, nie z FTV/Load.
        _has_dev_decomp = ("rt_rev_batt_min" in df.columns
                           and "rt_rev_ftv_min" in df.columns
                           and "rt_rev_load_min" in df.columns)
        # Bug #650 (2026-06-09): Zisk RT filtrovaný podľa joint LP toggles profilu.
        # User insight: 'ked je zaskrtnuta v sablone len baterka, pocita sa odchylka
        # len z baterie. ked aj FTV obchod, tak aj FTV. ked aj load, tak aj load.'
        # Tým "Zisk RT" v Excel odrazí len tie komponenty ktoré profil OBCHODUJE
        # (= sú v plan_grid_kwh nominácii). Drift inych je 'šum prostredia' a nepatrí
        # do atribúcie efektu systému.
        try:
            import joint_lp_integration as _jli_eff
            _xl_prof = pr.get_active() if pr is not None else None
            _xl_joint = _jli_eff.get_flags_from_profile(_xl_prof) if _xl_prof else None
        except Exception:
            _xl_joint = None
        if _xl_joint is not None:
            # Použi get_rt_eur_series ktorá vie aj runtime fallback (Bug #650-B)
            # — pre staré CSVky bez rt_rev_batt_min dopočíta z batt_kw/plan_batt_kw × zco
            from core.effect import get_rt_eur_series as _get_rt_xl
            df = df.copy()
            df["_rt_filtered_min"] = _get_rt_xl(df, joint_flags=_xl_joint)
            _rt_col_xl = "_rt_filtered_min"   # použi filtrovaný stĺpec namiesto total
            _tb_xl = bool(_xl_joint.get("trade_batt", True))
            _tf_xl = bool(_xl_joint.get("trade_ftv", False))
            _tl_xl = bool(_xl_joint.get("trade_load", False))
            print(f"[Excel #650] joint_flags filter: batt={_tb_xl} ftv={_tf_xl} load={_tl_xl} "
                  f"has_decomp={_has_dev_decomp} → Zisk RT z {_rt_col_xl}")
        def _agg(g):
            agg_args = dict(
                dt_eur=("dt_rev_min", "sum"),
                rt_eur=(_rt_col_xl, "sum"),
                baseline_eur=("baseline_per_min_eur", "sum"),
                dt_cena_avg=("dt_real_eur", "mean"),
                zco_cena_avg=("zco_eur_min", "mean"),
                ftv_vyroba_kwh=("ftv_min_kw", lambda s: float(s.sum()) / 60.0),
                spotreba_kwh=("load_min_kw", lambda s: float(s.sum()) / 60.0),
                export_kwh=("baseline_export_kwh", "sum"),
                import_kwh=("baseline_import_kwh", "sum"),
                dist_fee_eur=("dist_fee_min", "sum"),                 # DIST-FEE: úspora distribúcie
                batt_charge_kwh=("batt_kwh_min", lambda s: float(-s[s < 0].sum())),
                batt_discharge_kwh=("batt_kwh_min", lambda s: float(s[s > 0].sum())),
                soc_avg_pct=("soc_pct", "mean"),
                soc_min_pct=("soc_pct", "min"),
                soc_max_pct=("soc_pct", "max"),
            )
            if _has_vdt_arb:
                agg_args["vdt_arb_eur"] = ("vdt_arb_min", "sum")
            # Bug #649: pridať atribučné rt_batt/ftv/load € pre čitateľný rozklad RT zisku
            if _has_dev_decomp:
                agg_args["rt_batt_eur"] = ("rt_rev_batt_min", "sum")
                agg_args["rt_ftv_eur"] = ("rt_rev_ftv_min", "sum")
                agg_args["rt_load_eur"] = ("rt_rev_load_min", "sum")
            return g.agg(**agg_args).reset_index()

        def _finalize(agg_df):
            # Bug #608: total_eur = DT + RT + VDT arbitráž
            _vdt_arb_col = agg_df["vdt_arb_eur"].fillna(0) if "vdt_arb_eur" in agg_df.columns else 0
            _dist_col = agg_df["dist_fee_eur"].fillna(0) if "dist_fee_eur" in agg_df.columns else 0
            agg_df["total_eur"] = (agg_df["dt_eur"].fillna(0) + agg_df["rt_eur"].fillna(0)
                                   + _vdt_arb_col + _dist_col)
            agg_df["prinos_bat_plan_eur"] = agg_df["total_eur"] - agg_df["baseline_eur"].fillna(0)
            agg_df["batt_cycles"] = (agg_df["batt_charge_kwh"] + agg_df["batt_discharge_kwh"]) / 2.0 / max(float(_pui_plan.get("batt_kwh", 200.0)), 1.0)
            return _vdt_eff_decorate(agg_df)   # Bug VDT-EFEKTIVITA: + nákup/predaj per deň

        # Slovník stĺpcov → (pekný_nazov, format)
        COL_META = {
            "date":               ("Dátum",                  None),
            "month":              ("Mesiac",                 None),
            "ts15":               ("Čas (15-min)",           None),
            "dni":                ("Dni",                    FMT_INT),
            "dt_eur":             ("Zisk D-1",               FMT_EUR),
            "rt_eur":             ("Zisk RT (total)",        FMT_EUR),
            # Bug #649: atribučné komponenty RT (zisk z drift voči nominácii)
            "rt_batt_eur":        ("Zisk RT batt",           FMT_EUR),
            "rt_ftv_eur":         ("Zisk RT FTV",            FMT_EUR),
            "rt_load_eur":        ("Zisk RT load",           FMT_EUR),
            "total_eur":          ("Zisk SPOLU",             FMT_EUR),
            "baseline_eur":       ("Baseline",               FMT_EUR),
            "prinos_bat_plan_eur":("Prínos batérie + plán",  FMT_EUR),
            "dt_cena_avg":        ("Priemer DT cena",        FMT_PRICE),
            "zco_cena_avg":       ("Priemer ZCO cena",       FMT_PRICE),
            "ftv_vyroba_kwh":     ("FTV výroba",             FMT_KWH),
            "spotreba_kwh":       ("Spotreba",               FMT_KWH),
            "export_kwh":         ("Export do siete",        FMT_KWH),
            "import_kwh":         ("Import zo siete (baseline)", FMT_KWH),
            "dist_fee_eur":       ("Úspora distribúcia",     FMT_EUR),
            "batt_charge_kwh":    ("Batt nabíjanie",         FMT_KWH),
            "batt_discharge_kwh": ("Batt vybíjanie",         FMT_KWH),
            "batt_cycles":        ("Cyklov",                 FMT_CYC),
            "soc_avg_pct":        ("Priemer SOC",            FMT_PCT),
            "soc_min_pct":        ("Min SOC",                FMT_PCT),
            "soc_max_pct":        ("Max SOC",                FMT_PCT),
            # Bug VDT-EFEKTIVITA (2026-06-11)
            "vdt_arb_eur":        ("Zisk VDT arbitráž",      FMT_EUR),
            "vdt_buy_kwh":        ("VDT nákup",              FMT_KWH),
            "vdt_buy_avg":        ("VDT nákup cena",         FMT_PRICE),
            "vdt_sell_kwh":       ("VDT predaj",             FMT_KWH),
            "vdt_sell_avg":       ("VDT predaj cena",        FMT_PRICE),
        }

        def _write_table(ws, title, agg_df, start_row=1, info=""):
            """Vypíše hlavičku + tabuľku so štýlmi a číselnými formátmi. Vracia (header_row, last_row, last_col)."""
            ws.cell(row=start_row, column=1, value=title).font = subhdr_font
            if info:
                ws.cell(row=start_row, column=2, value=info).font = Font(italic=True, color="666666")
            hdr_row = start_row + 2
            hdr = list(agg_df.columns)
            for col_i, c in enumerate(hdr, 1):
                pretty = COL_META.get(c, (c, None))[0]
                cell = ws.cell(row=hdr_row, column=col_i, value=pretty)
                cell.font = bold; cell.fill = hdrfill
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.border = cell_border
            for r_idx, (_, row) in enumerate(agg_df.iterrows(), start=hdr_row + 1):
                for col_i, c in enumerate(hdr, 1):
                    val = row[c]
                    if val is not None and pd.notna(val) and isinstance(val, (int, float)):
                        # round prevention of negligible noise
                        if isinstance(val, float) and abs(val) < 1e-9:
                            val = 0.0
                    cell = ws.cell(row=r_idx, column=col_i, value=val)
                    fmt = COL_META.get(c, (None, None))[1]
                    if fmt:
                        cell.number_format = fmt
                    cell.border = cell_border
                    if isinstance(val, str):
                        cell.alignment = Alignment(horizontal="left")
            # column widths podľa typu
            for col_i, c in enumerate(hdr, 1):
                width = 18 if c in ("date", "month", "ts15") else 16
                ws.column_dimensions[get_column_letter(col_i)].width = width
            # freeze pane pod hlavičkou
            ws.freeze_panes = ws.cell(row=hdr_row + 1, column=1)
            return hdr_row, hdr_row + len(agg_df), len(hdr)

        # ─── Príprava agregátov ───
        daily = _finalize(_agg(df.groupby("date")))
        monthly = _finalize(_agg(df.groupby("month")))
        monthly["dni"] = df.groupby("month")["date"].nunique().values
        cols_m = list(monthly.columns); cols_m.remove("dni"); cols_m.insert(1, "dni")
        monthly = monthly[cols_m]
        all15 = _finalize(_agg(df.groupby(["date", "ts15"])))

        # Detail pre view day (alebo posledný)
        _detail_day = view or (str(df["date"].iloc[-1]) if "date" in df.columns and not df.empty else None)
        det15 = None
        if _detail_day:
            _det = df[df["date"].astype(str) == str(_detail_day)].copy() if "date" in df.columns else pd.DataFrame()
            if not _det.empty and "ts15" in _det.columns:
                det15 = _finalize(_agg(_det.groupby("ts15")))

        # ── Konfiguračné/period info pre Zhrnutie a Metadata ──
        try:
            _market = mk.get_active_market() if mk is not None else "cz"
        except Exception:
            _market = "cz"
        try:
            _active_profile = pr.get_active() if pr is not None else None
        except Exception:
            _active_profile = None
        _period_from = str(df["date"].min()) if "date" in df.columns and not df.empty else "?"
        _period_to   = str(df["date"].max()) if "date" in df.columns and not df.empty else "?"
        _period_days = int(df["date"].nunique()) if "date" in df.columns and not df.empty else 0

        wb = Workbook()

        # ════════════════════════════════════════════════════════════════
        # Sheet 1: ZHRNUTIE (executive summary)
        # ════════════════════════════════════════════════════════════════
        ws_sum = wb.active; ws_sum.title = "Zhrnutie"
        ws_sum.column_dimensions["A"].width = 4
        ws_sum.column_dimensions["B"].width = 32
        ws_sum.column_dimensions["C"].width = 22
        ws_sum.column_dimensions["D"].width = 22
        ws_sum.column_dimensions["E"].width = 14

        # Hlavička
        ws_sum["B2"] = "📊 FTV + batéria — Report výsledkov"
        ws_sum["B2"].font = Font(bold=True, size=16, color="1F4E78")
        ws_sum.merge_cells("B2:E2")
        ws_sum["B3"] = f"Profil: {_active_profile or '(default)'}    Trh: {_market.upper()}    Case: {case}"
        ws_sum["B3"].font = Font(size=11, color="555555")
        ws_sum.merge_cells("B3:E3")
        ws_sum["B4"] = f"Obdobie: {_period_from} → {_period_to}  ({_period_days} dní)"
        ws_sum["B4"].font = Font(size=11, color="555555")
        ws_sum.merge_cells("B4:E4")
        ws_sum["B5"] = f"Generované: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}"
        ws_sum["B5"].font = Font(size=10, italic=True, color="888888")
        ws_sum.merge_cells("B5:E5")

        def _section(row, title):
            ws_sum.cell(row=row, column=2, value=title).font = Font(bold=True, size=12, color="FFFFFF")
            ws_sum.cell(row=row, column=2).fill = PatternFill("solid", fgColor="1F4E78")
            for c in range(2, 6):
                ws_sum.cell(row=row, column=c).fill = PatternFill("solid", fgColor="1F4E78")

        def _kpi(row, label, value, fmt=None, note=""):
            ws_sum.cell(row=row, column=2, value=label).font = kpi_label_font
            cell = ws_sum.cell(row=row, column=3, value=value)
            cell.font = kpi_value_font
            if fmt: cell.number_format = fmt
            if note:
                ws_sum.cell(row=row, column=4, value=note).font = Font(size=10, italic=True, color="888888")

        # Konfigurácia
        _section(7, "🔧 Konfigurácia inštalácie")
        _kpi(8,  "FTV inštalácia [kWp]", float(_pui_plan.get("kwp", 99)), FMT_KWP,
              f"  @ {float(_pui_plan.get('lat', 0)):.2f}°/{float(_pui_plan.get('lon', 0)):.2f}°")
        _kpi(9,  "Batéria výkon [kW]",   float(_pui_plan.get("batt_kw", 100)), FMT_KW)
        _kpi(10, "Batéria kapacita [kWh]", float(_pui_plan.get("batt_kwh", 200)), FMT_KWH)
        _kpi(11, "SOC limity",  f"{float(_pui_plan.get('soc_min',5)):.0f} % – {float(_pui_plan.get('soc_max',95)):.0f} %")
        _kpi(12, "Limit prípojky [kW]",  float(_pui_plan.get("grid_kw", 100)), FMT_KW)
        _kpi(13, "Min. spread D-1",      float(_pui_plan.get("min_spread", 30)), FMT_PRICE)

        # Ekonomika
        _total_dt   = float(daily["dt_eur"].sum())
        _total_rt   = float(daily["rt_eur"].sum())
        _total_dist = float(daily["dist_fee_eur"].sum()) if "dist_fee_eur" in daily.columns else 0.0
        _total_spolu = _total_dt + _total_rt + _total_dist
        _total_bl   = float(daily["baseline_eur"].sum())
        _prinos     = _total_spolu - _total_bl
        _prinos_pct = (_prinos / _total_bl * 100.0) if _total_bl > 0 else 0
        _per_day    = (_total_spolu / max(_period_days, 1))

        _section(15, "💰 Kľúčové ukazovatele")
        ws_sum.cell(row=16, column=2, value="").font = kpi_label_font
        ws_sum.cell(row=16, column=3, value="Suma [€]").font = bold; ws_sum.cell(row=16, column=3).fill = hdrfill
        ws_sum.cell(row=16, column=4, value="Per deň [€]").font = bold; ws_sum.cell(row=16, column=4).fill = hdrfill
        _kpi(17, "Celkový zisk", _total_spolu, FMT_EUR)
        ws_sum.cell(row=17, column=4, value=_per_day).number_format = FMT_EUR
        ws_sum.cell(row=17, column=4).font = kpi_value_font
        _kpi(18, "  ├─ Z denného trhu (D-1)", _total_dt, FMT_EUR)
        ws_sum.cell(row=18, column=4, value=_total_dt/max(_period_days,1)).number_format = FMT_EUR
        ws_sum.cell(row=18, column=4).font = kpi_value_font
        _kpi(19, "  └─ Z odchýlky (RT)", _total_rt, FMT_EUR)
        ws_sum.cell(row=19, column=4, value=_total_rt/max(_period_days,1)).number_format = FMT_EUR
        ws_sum.cell(row=19, column=4).font = kpi_value_font
        _kpi(21, "Baseline (bez batérie + plánu)", _total_bl, FMT_EUR)
        ws_sum.cell(row=21, column=4, value=_total_bl/max(_period_days,1)).number_format = FMT_EUR
        ws_sum.cell(row=21, column=4).font = kpi_value_font
        _kpi(22, "➜ Prínos batérie + plánu", _prinos, FMT_EUR, f"  ({_prinos_pct:+.1f} % oproti baseline)")
        ws_sum.cell(row=22, column=4, value=_prinos/max(_period_days,1)).number_format = FMT_EUR
        ws_sum.cell(row=22, column=4).font = Font(bold=True, color="2E7D32" if _prinos >= 0 else "C0392B", size=12)
        _kpi(23, "    z toho úspora na distribúcii", _total_dist, FMT_EUR)
        ws_sum.cell(row=23, column=4, value=_total_dist/max(_period_days,1)).number_format = FMT_EUR
        ws_sum.cell(row=23, column=4).font = kpi_value_font

        # Energia
        _ftv_total = float(daily["ftv_vyroba_kwh"].sum())
        _load_total = float(daily["spotreba_kwh"].sum())
        _exp_total = float(daily["export_kwh"].sum())
        _imp_total = float(daily["import_kwh"].sum())

        _section(24, "⚡ Energia")
        ws_sum.cell(row=25, column=3, value="Suma [MWh]").font = bold; ws_sum.cell(row=25, column=3).fill = hdrfill
        ws_sum.cell(row=25, column=4, value="Priemer [kWh/deň]").font = bold; ws_sum.cell(row=25, column=4).fill = hdrfill
        for i, (label, val) in enumerate([
            ("FTV výroba", _ftv_total),
            ("Spotreba zákazníka", _load_total),
            ("Export do siete", _exp_total),
            ("Import zo siete", _imp_total),
        ]):
            row = 26 + i
            ws_sum.cell(row=row, column=2, value=label).font = kpi_label_font
            ws_sum.cell(row=row, column=3, value=val / 1000.0).number_format = FMT_MWH
            ws_sum.cell(row=row, column=3).font = kpi_value_font
            ws_sum.cell(row=row, column=4, value=val / max(_period_days, 1)).number_format = FMT_KWH
            ws_sum.cell(row=row, column=4).font = kpi_value_font

        # Batéria
        _batt_ch = float(daily["batt_charge_kwh"].sum())
        _batt_di = float(daily["batt_discharge_kwh"].sum())
        _batt_cyc = float(daily["batt_cycles"].sum())
        _soc_avg = float(daily["soc_avg_pct"].mean()) if not daily.empty else 0
        _soc_min = float(daily["soc_min_pct"].min()) if not daily.empty else 0
        _soc_max = float(daily["soc_max_pct"].max()) if not daily.empty else 0

        _section(31, "🔋 Batéria")
        _kpi(32, "Nabíjanie spolu", _batt_ch / 1000.0, FMT_MWH)
        _kpi(33, "Vybíjanie spolu", _batt_di / 1000.0, FMT_MWH)
        _kpi(34, "Cyklov spolu", _batt_cyc, FMT_CYC, f"  ({_batt_cyc / max(_period_days,1):.2f}/deň)")
        _kpi(35, "Priemerný SOC", _soc_avg, FMT_PCT)
        _kpi(36, "Rozsah SOC", f"{_soc_min:.0f} % – {_soc_max:.0f} %")

        # Ceny
        _dt_avg = float((df["dt_real_eur"].mean()) if "dt_real_eur" in df.columns else 0)
        _zco_avg = float((df["zco_eur_min"].dropna().mean()) if "zco_eur_min" in df.columns else 0)
        _max_spread = float(daily["dt_cena_avg"].max() - daily["dt_cena_avg"].min()) if not daily.empty else 0

        _section(38, "💶 Ceny")
        _kpi(39, "Priemerná DT cena", _dt_avg, FMT_PRICE)
        _kpi(40, "Priemerná ZCO (odchýlka)", _zco_avg, FMT_PRICE)
        _kpi(41, "Rozdiel medzi denným max/min", _max_spread, FMT_PRICE)

        # ════════════════════════════════════════════════════════════════
        # Sheet 2: Po mesiacoch + grafy
        # ════════════════════════════════════════════════════════════════
        ws_m = wb.create_sheet("Po_mesiacoch")
        hdr_row_m, last_row_m, last_col_m = _write_table(ws_m, "📅 Po mesiacoch", monthly, info=f"{len(monthly)} mesiacov")

        # Graf 1: Zisk porovnanie (D-1, RT, Baseline) — stacked bar
        ch1 = BarChart()
        ch1.type = "col"; ch1.style = 10; ch1.title = "Zisk po mesiacoch (D-1 + RT vs Baseline)"
        ch1.y_axis.title = "€"; ch1.x_axis.title = "Mesiac"
        ch1.height = 9; ch1.width = 18
        _col_dt = list(monthly.columns).index("dt_eur") + 1
        _col_rt = list(monthly.columns).index("rt_eur") + 1
        _col_bl = list(monthly.columns).index("baseline_eur") + 1
        data1 = Reference(ws_m, min_col=_col_dt, min_row=hdr_row_m, max_row=last_row_m, max_col=_col_rt)
        data2 = Reference(ws_m, min_col=_col_bl, min_row=hdr_row_m, max_row=last_row_m)
        cats = Reference(ws_m, min_col=1, min_row=hdr_row_m + 1, max_row=last_row_m)
        ch1.add_data(data1, titles_from_data=True)
        ch1.add_data(data2, titles_from_data=True)
        ch1.set_categories(cats)
        ws_m.add_chart(ch1, f"A{last_row_m + 3}")

        # Graf 2: Prínos batérie — line
        ch2 = LineChart()
        ch2.title = "Prínos batérie + plánu (€/mesiac)"
        ch2.y_axis.title = "€"; ch2.x_axis.title = "Mesiac"
        ch2.height = 9; ch2.width = 18
        _col_pr = list(monthly.columns).index("prinos_bat_plan_eur") + 1
        data_pr = Reference(ws_m, min_col=_col_pr, min_row=hdr_row_m, max_row=last_row_m)
        ch2.add_data(data_pr, titles_from_data=True)
        ch2.set_categories(cats)
        ws_m.add_chart(ch2, f"K{last_row_m + 3}")

        # Graf 3: FTV vs Spotreba — bar
        ch3 = BarChart()
        ch3.type = "col"; ch3.style = 11; ch3.title = "FTV výroba vs Spotreba (kWh/mesiac)"
        ch3.y_axis.title = "kWh"; ch3.x_axis.title = "Mesiac"
        ch3.height = 9; ch3.width = 18
        _col_ftv = list(monthly.columns).index("ftv_vyroba_kwh") + 1
        _col_sp = list(monthly.columns).index("spotreba_kwh") + 1
        data_en = Reference(ws_m, min_col=_col_ftv, min_row=hdr_row_m, max_row=last_row_m, max_col=_col_sp)
        ch3.add_data(data_en, titles_from_data=True)
        ch3.set_categories(cats)
        ws_m.add_chart(ch3, f"A{last_row_m + 22}")

        # Graf 4: Cykly batérie
        ch4 = LineChart()
        ch4.title = "Cyklov batérie po mesiacoch"
        ch4.y_axis.title = "Cyklov"; ch4.x_axis.title = "Mesiac"
        ch4.height = 9; ch4.width = 18
        _col_cyc = list(monthly.columns).index("batt_cycles") + 1
        data_cyc = Reference(ws_m, min_col=_col_cyc, min_row=hdr_row_m, max_row=last_row_m)
        ch4.add_data(data_cyc, titles_from_data=True)
        ch4.set_categories(cats)
        ws_m.add_chart(ch4, f"K{last_row_m + 22}")

        # ════════════════════════════════════════════════════════════════
        # Sheet 3: Po dňoch + grafy
        # ════════════════════════════════════════════════════════════════
        ws_d = wb.create_sheet("Po_dnoch")
        hdr_row_d, last_row_d, last_col_d = _write_table(ws_d, "📆 Po dňoch", daily, info=f"{len(daily)} dní")

        # Graf 1: Denný zisk (D-1, RT, baseline) — line
        chd1 = LineChart()
        chd1.title = "Denný zisk (D-1, RT, Baseline)"
        chd1.y_axis.title = "€"; chd1.x_axis.title = "Deň"
        chd1.height = 10; chd1.width = 22
        _col_dt_d = list(daily.columns).index("dt_eur") + 1
        _col_bl_d = list(daily.columns).index("baseline_eur") + 1
        data_zisk = Reference(ws_d, min_col=_col_dt_d, min_row=hdr_row_d, max_row=last_row_d, max_col=_col_bl_d)
        cats_d = Reference(ws_d, min_col=1, min_row=hdr_row_d + 1, max_row=last_row_d)
        chd1.add_data(data_zisk, titles_from_data=True)
        chd1.set_categories(cats_d)
        ws_d.add_chart(chd1, f"A{last_row_d + 3}")

        # Graf 2: FTV výroba + spotreba — line
        chd2 = LineChart()
        chd2.title = "FTV výroba vs Spotreba (kWh/deň)"
        chd2.y_axis.title = "kWh"; chd2.x_axis.title = "Deň"
        chd2.height = 10; chd2.width = 22
        _col_ftv_d = list(daily.columns).index("ftv_vyroba_kwh") + 1
        _col_sp_d = list(daily.columns).index("spotreba_kwh") + 1
        data_en_d = Reference(ws_d, min_col=_col_ftv_d, min_row=hdr_row_d, max_row=last_row_d, max_col=_col_sp_d)
        chd2.add_data(data_en_d, titles_from_data=True)
        chd2.set_categories(cats_d)
        ws_d.add_chart(chd2, f"A{last_row_d + 24}")

        # Graf 3: SOC range (min/avg/max) — line
        chd3 = LineChart()
        chd3.title = "SOC rozsah po dňoch"
        chd3.y_axis.title = "%"; chd3.x_axis.title = "Deň"
        chd3.height = 10; chd3.width = 22
        _col_soc_avg = list(daily.columns).index("soc_avg_pct") + 1
        _col_soc_max = list(daily.columns).index("soc_max_pct") + 1
        data_soc = Reference(ws_d, min_col=_col_soc_avg, min_row=hdr_row_d, max_row=last_row_d, max_col=_col_soc_max)
        chd3.add_data(data_soc, titles_from_data=True)
        chd3.set_categories(cats_d)
        ws_d.add_chart(chd3, f"A{last_row_d + 45}")

        # ════════════════════════════════════════════════════════════════
        # Sheet 4: Všetky 15-min
        # ════════════════════════════════════════════════════════════════
        ws_all = wb.create_sheet("Vsetky_15min")
        _write_table(ws_all, "🕐 Všetky 15-min sloty",
                      all15, info=f"{len(all15)} riadkov, {df['date'].nunique()} dní")

        # ════════════════════════════════════════════════════════════════
        # Sheet 5: Detail 15-min pre vybraný deň + grafy
        # ════════════════════════════════════════════════════════════════
        ws_det = wb.create_sheet("Detail_15min")
        if det15 is not None and not det15.empty:
            hdr_row_det, last_row_det, _ = _write_table(
                ws_det, f"🔍 Detail 15-min pre deň {_detail_day}", det15)

            # Graf: DT + ZCO ceny
            chx1 = LineChart()
            chx1.title = f"DT a ZCO ceny ({_detail_day})"
            chx1.y_axis.title = "€/MWh"; chx1.x_axis.title = "Čas"
            chx1.height = 8; chx1.width = 22
            _col_dtc = list(det15.columns).index("dt_cena_avg") + 1
            _col_zco = list(det15.columns).index("zco_cena_avg") + 1
            data_p = Reference(ws_det, min_col=_col_dtc, min_row=hdr_row_det, max_row=last_row_det, max_col=_col_zco)
            cats_det = Reference(ws_det, min_col=1, min_row=hdr_row_det + 1, max_row=last_row_det)
            chx1.add_data(data_p, titles_from_data=True)
            chx1.set_categories(cats_det)
            ws_det.add_chart(chx1, f"A{last_row_det + 3}")

            # Graf: SOC %
            chx2 = LineChart()
            chx2.title = f"SOC % počas dňa ({_detail_day})"
            chx2.y_axis.title = "%"; chx2.x_axis.title = "Čas"
            chx2.height = 8; chx2.width = 22
            _col_sa = list(det15.columns).index("soc_avg_pct") + 1
            data_soc_d = Reference(ws_det, min_col=_col_sa, min_row=hdr_row_det, max_row=last_row_det)
            chx2.add_data(data_soc_d, titles_from_data=True)
            chx2.set_categories(cats_det)
            ws_det.add_chart(chx2, f"A{last_row_det + 22}")

            # Graf: Energie FTV + spotreba + export
            chx3 = LineChart()
            chx3.title = f"FTV / spotreba / export ({_detail_day})"
            chx3.y_axis.title = "kWh"; chx3.x_axis.title = "Čas"
            chx3.height = 8; chx3.width = 22
            _col_ftv_det = list(det15.columns).index("ftv_vyroba_kwh") + 1
            _col_exp_det = list(det15.columns).index("export_kwh") + 1
            data_en_det = Reference(ws_det, min_col=_col_ftv_det, min_row=hdr_row_det, max_row=last_row_det, max_col=_col_exp_det)
            chx3.add_data(data_en_det, titles_from_data=True)
            chx3.set_categories(cats_det)
            ws_det.add_chart(chx3, f"A{last_row_det + 41}")
        else:
            ws_det["A1"] = f"Pre deň '{_detail_day or '?'}' nie sú dáta v logu."

        # ════════════════════════════════════════════════════════════════
        # Sheet 6: Per_minute (raw)
        # ════════════════════════════════════════════════════════════════
        ws_raw = wb.create_sheet("Per_minute")
        ws_raw["A1"] = "Surové minútové dáta + výpočet baseline (pre audit)"
        ws_raw["A1"].font = subhdr_font
        # Bug #603: pridať rt_rev_realistic_min ako audit stĺpec — auditor vidí
        # aj teoretickú RT engine prognózu (rt_rev_min) aj skutočný financial
        # impact cez dev×ZCO (rt_rev_realistic_min). Súčty v Sheet 1-5 ríjajú
        # realistic (cez _rt_col_xl), tu sa zobrazia oba.
        cols_min = ["time", "date", "ts15", "ftv_min_kw", "load_min_kw",
                    "dt_real_eur", "zco_eur_min", "batt_kw_actual", "soc_pct",
                    "baseline_net_kw", "baseline_export_kwh", "baseline_import_kwh",
                    "baseline_p_imp_eur_mwh", "baseline_p_exp_eur_mwh",
                    "baseline_rev_eur", "baseline_cost_eur", "baseline_per_min_eur", "baseline_cum_eur",
                    "dt_rev_min", "rt_rev_min", "rt_rev_realistic_min",
                    "vdt_arb_min",   # Bug #608
                    "cum_dt", "cum_rt", "cum_total"]
        cols_min = [c for c in cols_min if c in df.columns]
        ws_raw.append([])
        ws_raw.append(cols_min)
        for cell in ws_raw[ws_raw.max_row]:
            cell.font = bold; cell.fill = hdrfill
        for _, row in df.iterrows():
            ws_raw.append([row.get(c) for c in cols_min])
        for col_i, _ in enumerate(cols_min, 1):
            ws_raw.column_dimensions[get_column_letter(col_i)].width = 16
        ws_raw.freeze_panes = "A4"

        # ════════════════════════════════════════════════════════════════
        # Sheet 7: Metadata
        # ════════════════════════════════════════════════════════════════
        ws_meta = wb.create_sheet("Metadata")
        ws_meta.column_dimensions["A"].width = 30
        ws_meta.column_dimensions["B"].width = 60
        ws_meta["A1"] = "Metadata reportu"
        ws_meta["A1"].font = subhdr_font

        meta_rows = [
            ("Case", case),
            ("Aktívny profil", _active_profile or "(default)"),
            ("Trh", _market),
            ("Obdobie od", _period_from),
            ("Obdobie do", _period_to),
            ("Počet dní", _period_days),
            ("Generované", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("", ""),
            ("FTV kWp", float(_pui_plan.get("kwp", 99))),
            ("FTV lat/lon", f"{_pui_plan.get('lat')}, {_pui_plan.get('lon')}"),
            ("Batt kW", float(_pui_plan.get("batt_kw", 100))),
            ("Batt kWh", float(_pui_plan.get("batt_kwh", 200))),
            ("SOC min %", float(_pui_plan.get("soc_min", 5))),
            ("SOC max %", float(_pui_plan.get("soc_max", 95))),
            ("Grid kW imp", float(_pui_plan.get("grid_kw_import", _pui_plan.get("grid_kw", 100)))),
            ("Grid kW exp", float(_pui_plan.get("grid_kw_export", _pui_plan.get("grid_kw", 100)))),
            ("Min spread €/MWh", float(_pui_plan.get("min_spread", 30))),
            ("", ""),
            ("Baseline import mode",
              f"{_bp['im_mode']} × {_bp['im_val']}" if _bp['im_mode'] == 'dt_x' else f"FIX {_bp['im_val']} €/MWh"),
            ("Baseline export mode",
              f"{_bp['ex_mode']} × {_bp['ex_val']}" if _bp['ex_mode'] == 'dt_x' else f"FIX {_bp['ex_val']} €/MWh"),
            ("Vzorec baseline (per min)",
              "ex_kwh × p_exp/1000 − im_kwh × p_imp/1000  (kde ex/im = max/-min(FTV−load,0)/60)"),
            ("Vzorec prínos",
              "prinos = total − baseline,  total = D-1 zisk + RT zisk"),
            ("Vzorec cyklov",
              "cyklov = (nabíjanie + vybíjanie) / 2 / batt_kwh"),
        ]
        for i, (k, v) in enumerate(meta_rows, start=3):
            ws_meta.cell(row=i, column=1, value=k).font = kpi_label_font
            ws_meta.cell(row=i, column=2, value=v).font = kpi_value_font

        # ── Save + filename ──
        buf = _io.BytesIO()
        wb.save(buf); buf.seek(0)
        _prof_part = (_active_profile or "default").replace(" ", "_")
        fname = f"report_{_prof_part}_{_period_from}_{_period_to}.xlsx"
        return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                  headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    except Exception as e:
        import traceback
        return Response(f"Export chyba: {e}\n{traceback.format_exc()}", status_code=500, media_type="text/plain")


@app.get("/livesim/chC_export_pdf")
def livesim_chC_export_pdf(case: str = "plan_d1", view: str = None):
    """PDF report — kompaktný manažérsky súhrn (Variant A) s grafmi cez matplotlib.

    Obsah: titulná strana so Zhrnutím + KPI, mesačná tabuľka + 4 grafy,
    denná tabuľka + 3 grafy, denný detail (ak je ?view=) + 3 grafy."""
    if lsim is None:
        return Response("livesim nedostupný", status_code=500)
    # Skontroluj PDF deps PRED hlavným try — vrátime peknú HTML stránku s inštaláciou
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import cm, mm
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
                                          Image, PageBreak, KeepTogether)
        from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as _imp_e:
        _missing = "reportlab" if "reportlab" in str(_imp_e) else ("matplotlib" if "matplotlib" in str(_imp_e) else str(_imp_e))
        html = f"""<!doctype html><html lang="sk"><head><meta charset="utf-8"><title>PDF — chýba modul</title>
<style>body{{font-family:-apple-system,Segoe UI,Arial;max-width:780px;margin:40px auto;padding:0 20px;color:#222}}
h1{{color:#C0392B}} code{{background:#f5f5f5;padding:2px 6px;border-radius:4px;font-family:Menlo,monospace}}
.box{{background:#fff3f3;border-left:4px solid #C0392B;border-radius:8px;padding:14px 18px;margin:14px 0}}
.cmd{{background:#1F4E78;color:#fff;padding:10px 14px;border-radius:8px;font-family:Menlo,monospace;margin:8px 0;display:block}}
a.btn{{display:inline-block;background:#2E7D32;color:#fff;padding:9px 16px;border-radius:8px;text-decoration:none;font-weight:600;margin:8px 8px 0 0}}
</style></head><body>
<h1>📄 PDF export — chýba modul <code>{_missing}</code></h1>
<div class="box">
<p>PDF reporty potrebujú <b>reportlab</b> (PDF engine) a <b>matplotlib</b> (grafy). V aktuálnom Python prostredí chýbajú.</p>
</div>
<h3>Inštalácia (v tom istom termináli kde beží appka)</h3>
<code class="cmd">cd "/Users/radoslavstompf/Documents/_FUERGY/01_Zakaznici/CZ/_Spolocne/Analyzy/Predikcia FTV/Aplikacia"
source .venv/bin/activate  &nbsp; # ak používaš venv
pip install reportlab matplotlib</code>
<p>Alebo všetky závislosti naraz cez už aktualizované <code>requirements.txt</code>:</p>
<code class="cmd">pip install -r requirements.txt</code>
<p>Potom <b>reštartuj appku</b> (<code>./stop_dev.sh && ./start_dev.sh</code>) a klikni PDF tlačidlo znova.</p>
<a class="btn" href="javascript:history.back()">← Späť</a>
<a class="btn" href="/livesim">🟢 /livesim</a>
</body></html>"""
        return HTMLResponse(html, status_code=503)
    try:
        import io as _io

        # ── Unicode font registrácia (Helvetica nepodporuje SK diakritiku š,č,ž,á,í,ú,ô,ť,ľ...) ──
        # Skúsime systémové fonty Mac/Linux, fallback na matplotlib-bundled DejaVu Sans (vždy dostupný).
        def _find_ttf(candidates):
            for p in candidates:
                if p and os.path.exists(p):
                    return p
            return None
        _mpl_data = matplotlib.get_data_path()
        _font_reg = _find_ttf([
            "/Library/Fonts/Arial Unicode.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            os.path.join(_mpl_data, "fonts", "ttf", "DejaVuSans.ttf"),
        ])
        _font_bold = _find_ttf([
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            os.path.join(_mpl_data, "fonts", "ttf", "DejaVuSans-Bold.ttf"),
        ])
        FONT_REG = "Helvetica"
        FONT_BOLD = "Helvetica-Bold"
        if _font_reg:
            try:
                pdfmetrics.registerFont(TTFont("UFont", _font_reg))
                FONT_REG = "UFont"
            except Exception:
                pass
        if _font_bold:
            try:
                pdfmetrics.registerFont(TTFont("UFont-Bold", _font_bold))
                FONT_BOLD = "UFont-Bold"
            except Exception:
                pass
        # Tell reportlab the bold variant belongs to UFont (rieši <b>...</b> v Paragraph)
        if FONT_REG == "UFont" and FONT_BOLD == "UFont-Bold":
            try:
                from reportlab.pdfbase.pdfmetrics import registerFontFamily
                registerFontFamily("UFont", normal="UFont", bold="UFont-Bold",
                                   italic="UFont", boldItalic="UFont-Bold")
            except Exception:
                pass

        # Helper: odstráni emoji znaky (📊 🔧 💰 ⚡ 🔋 💶 📅 📆 🔍 ✓ ✗ ➜ ├ └ —) — DejaVu Sans/Arial ich
        # nerendrujú a vyzerajú ako prázdne štvorce. Necháme čisto textový report.
        import re as _re
        _EMOJI_RE = _re.compile(
            "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF"
            "\U00002500-\U00002BFF─-▟]",
            flags=_re.UNICODE)
        def _strip_emoji(s):
            return _EMOJI_RE.sub("", str(s)).strip()

        # ── Dáta — rovnaký zdroj a výpočty ako Excel export ──
        df = lsim.load_series(case, port=_PORT, max_points=10**9)
        if df is None or df.empty:
            return Response("Žiadne dáta v livesim logu pre tento prípad.", status_code=404)
        _pui_plan = _ui_load("plan", {}) or {}
        _bp = bc.parse_baseline_params(_pui_plan) if bc is not None else \
            {"im_mode": "dt_x", "im_val": 1.0, "ex_mode": "dt_x", "ex_val": 1.0}
        _ftv_min = pd.to_numeric(df.get("ftv_min_real_kw", df.get("ftv_kw")), errors="coerce").fillna(0).values
        _load_min = pd.to_numeric(df.get("load_min_real_kw", pd.Series([0.0]*len(df))), errors="coerce").fillna(0).values
        _dtp = pd.to_numeric(df.get("dt_real_eur", df.get("dt_eur")), errors="coerce").fillna(0).values
        _net = _ftv_min - _load_min
        _ex_kwh = np.maximum(_net, 0.0) / 60.0
        _im_kwh = -np.minimum(_net, 0.0) / 60.0
        _p_imp = (_dtp * _bp["im_val"]) if _bp["im_mode"] == "dt_x" else np.full_like(_dtp, _bp["im_val"])
        _p_exp = (_dtp * _bp["ex_val"]) if _bp["ex_mode"] == "dt_x" else np.full_like(_dtp, _bp["ex_val"])
        _bl_per_min = _ex_kwh * _p_exp / 1000.0 - _im_kwh * _p_imp / 1000.0
        df = df.copy()
        df["ftv_min_kw"] = _ftv_min
        df["load_min_kw"] = _load_min
        df["dt_real_eur"] = _dtp
        df["baseline_export_kwh"] = _ex_kwh
        df["baseline_import_kwh"] = _im_kwh
        df["baseline_per_min_eur"] = _bl_per_min
        df["zco_eur_min"] = pd.to_numeric(df.get("zco_eur", pd.Series([np.nan]*len(df))), errors="coerce")
        _bkw_max = float(_pui_plan.get("batt_kw", 100.0))
        _plan_b = pd.to_numeric(df.get("plan_batt_kw", pd.Series([0.0]*len(df))), errors="coerce").fillna(0).values
        _rt_d = pd.to_numeric(df.get("rt_dir", pd.Series([0.0]*len(df))), errors="coerce").fillna(0).values
        _rt_p = pd.to_numeric(df.get("rt_power_pct", pd.Series([0.0]*len(df))), errors="coerce").fillna(0).values
        _batt_raw2 = _plan_b + _rt_d * _rt_p / 100.0 * _bkw_max
        # Bug #610 (Excel _agg, second): clip predikciu na fyzické limity batt
        df["batt_kw_actual"] = np.clip(_batt_raw2, -_bkw_max, +_bkw_max) if _bkw_max > 0 else _batt_raw2
        df["batt_kwh_min"] = df["batt_kw_actual"] / 60.0
        df["month"] = pd.to_datetime(df["time"]).dt.strftime("%Y-%m")
        # DIST-FEE (2026-06-15): distribučná úspora zvlášť = grid_fee × (baseline_import − skutočný_import)
        _gf_pdf = float(_pui_plan.get("grid_fee", 0) or 0)
        _ftv_p = pd.to_numeric(df.get("ftv_min_real_kw", df.get("ftv_kw")), errors="coerce").fillna(0).values
        _load_p = pd.to_numeric(df.get("load_min_real_kw", pd.Series([0.0]*len(df))), errors="coerce").fillna(0).values
        _battp = pd.to_numeric(df["batt_kw_actual"], errors="coerce").fillna(0).values
        # NETTO import (2026-06-16): poplatok na skutočný odber zo siete = max(load−FTV−batt,0)
        df["dist_actual_import_kwh"] = np.maximum(_load_p - _ftv_p - _battp, 0.0) / 60.0
        df["dist_fee_min"] = _gf_pdf * (np.maximum(_load_p - _ftv_p, 0.0) / 60.0
                                        - df["dist_actual_import_kwh"].values) / 1000.0

        _bkwh_max = float(_pui_plan.get("batt_kwh", 200.0))

        # Bug #606 (Excel export, second _agg): rovnaký fix ako vyššie.
        # rt_rev_realistic_min = skutočný financial impact (dev × ZCO), rt_rev_min
        # je teoretický výstup RT enginu ktorý môže ukazovať fiktívne pokuty
        # (napr. -370€) keď reálny dopad cez ZCO bol 0€.
        from core.effect import resolve_rt_col as _resolve_rt2
        _rt_col_xl2 = _resolve_rt2(df)
        # Bug #608: VDT arbitráž (delta VDT vs DT clearing). Ak stĺpec existuje v CSV.
        _has_vdt_arb2 = "vdt_arb_min" in df.columns
        def _agg(g):
            agg_args = dict(
                dt_eur=("dt_rev_min", "sum"), rt_eur=(_rt_col_xl2, "sum"),
                baseline_eur=("baseline_per_min_eur", "sum"),
                dt_cena_avg=("dt_real_eur", "mean"),
                zco_cena_avg=("zco_eur_min", "mean"),
                ftv_vyroba_kwh=("ftv_min_kw", lambda s: float(s.sum()) / 60.0),
                spotreba_kwh=("load_min_kw", lambda s: float(s.sum()) / 60.0),
                export_kwh=("baseline_export_kwh", "sum"),
                import_kwh=("baseline_import_kwh", "sum"),
                dist_fee_eur=("dist_fee_min", "sum"),                 # DIST-FEE: úspora distribúcie
                batt_charge_kwh=("batt_kwh_min", lambda s: float(-s[s < 0].sum())),
                batt_discharge_kwh=("batt_kwh_min", lambda s: float(s[s > 0].sum())),
                soc_avg_pct=("soc_pct", "mean"),
                soc_min_pct=("soc_pct", "min"),
                soc_max_pct=("soc_pct", "max"),
            )
            if _has_vdt_arb2:
                agg_args["vdt_arb_eur"] = ("vdt_arb_min", "sum")
            return g.agg(**agg_args).reset_index()

        def _finalize(a):
            # Bug #608: total_eur = DT + RT + VDT arbitráž
            _vdt_arb_col2 = a["vdt_arb_eur"].fillna(0) if "vdt_arb_eur" in a.columns else 0
            _dist_col2 = a["dist_fee_eur"].fillna(0) if "dist_fee_eur" in a.columns else 0
            a["total_eur"] = (a["dt_eur"].fillna(0) + a["rt_eur"].fillna(0)
                              + _vdt_arb_col2 + _dist_col2)
            a["prinos_eur"] = a["total_eur"] - a["baseline_eur"].fillna(0)
            a["cycles"] = (a["batt_charge_kwh"] + a["batt_discharge_kwh"]) / 2.0 / max(_bkwh_max, 1.0)
            return _vdt_eff_decorate(a)        # Bug VDT-EFEKTIVITA: + nákup/predaj per deň

        daily = _finalize(_agg(df.groupby("date")))
        monthly = _finalize(_agg(df.groupby("month")))
        monthly["dni"] = df.groupby("month")["date"].nunique().values
        _detail_day = view or (str(df["date"].iloc[-1]) if "date" in df.columns and not df.empty else None)
        det15 = None
        if _detail_day:
            _det = df[df["date"].astype(str) == str(_detail_day)].copy() if "date" in df.columns else pd.DataFrame()
            if not _det.empty and "ts15" in _det.columns:
                det15 = _finalize(_agg(_det.groupby("ts15")))

        # ── Metadata ──
        try:
            _market = mk.get_active_market() if mk is not None else "cz"
        except Exception:
            _market = "cz"
        try:
            _active_profile = pr.get_active() if pr is not None else None
        except Exception:
            _active_profile = None
        _period_from = str(df["date"].min())
        _period_to = str(df["date"].max())
        _period_days = int(df["date"].nunique())

        # ── Pomocné funkcie pre matplotlib grafy (vrátia BytesIO + Image) ──
        def _fig_to_image(fig, width_cm=17):
            """Save matplotlib fig to bytes a vráti reportlab.platypus.Image."""
            buf = _io.BytesIO()
            fig.tight_layout()
            fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
            plt.close(fig)
            buf.seek(0)
            img = Image(buf, width=width_cm * cm)
            img._restrictSize(width_cm * cm, 10 * cm)
            return img

        def _decimate_ticks(n, max_ticks=18):
            """Vyber rovnomerne max_ticks indexov z [0..n-1], vždy vrátane prvého a posledného."""
            if n <= max_ticks:
                return list(range(n))
            step = max(1, int(np.ceil(n / max_ticks)))
            idxs = list(range(0, n, step))
            if (n - 1) not in idxs:
                idxs.append(n - 1)
            return idxs

        def _bar_compare(labels, series_dict, title, ylabel):
            fig, ax = plt.subplots(figsize=(8, 3.5))
            n = len(labels)
            x = np.arange(n)
            w = 0.8 / max(len(series_dict), 1)
            for i, (label, vals) in enumerate(series_dict.items()):
                ax.bar(x + i*w - 0.4 + w/2, vals, width=w, label=label)
            tick_idx = _decimate_ticks(n)
            ax.set_xticks([x[i] for i in tick_idx])
            ax.set_xticklabels([labels[i] for i in tick_idx], rotation=30, ha="right", fontsize=8)
            ax.set_title(title, fontsize=11, fontweight="bold", color="#1F4E78")
            ax.set_ylabel(ylabel, fontsize=9)
            ax.grid(axis="y", linestyle=":", alpha=0.5)
            ax.legend(fontsize=8, loc="best")
            ax.tick_params(axis="y", labelsize=8)
            return fig

        def _line(labels, series_dict, title, ylabel):
            fig, ax = plt.subplots(figsize=(8, 3.5))
            n = len(labels)
            x = np.arange(n)
            # ak je veľa bodov, vypneme marker aby nezahltil chart
            _marker = "o" if n <= 40 else None
            _msize = 3 if n <= 40 else 0
            for label, vals in series_dict.items():
                ax.plot(x, vals, label=label, linewidth=1.4, marker=_marker, markersize=_msize)
            tick_idx = _decimate_ticks(n)
            ax.set_xticks([x[i] for i in tick_idx])
            ax.set_xticklabels([labels[i] for i in tick_idx], rotation=30, ha="right", fontsize=8)
            ax.set_title(title, fontsize=11, fontweight="bold", color="#1F4E78")
            ax.set_ylabel(ylabel, fontsize=9)
            ax.grid(axis="y", linestyle=":", alpha=0.5)
            ax.legend(fontsize=8, loc="best")
            ax.tick_params(axis="y", labelsize=8)
            return fig

        # ── PDF build ──
        buf_pdf = _io.BytesIO()
        doc = SimpleDocTemplate(buf_pdf, pagesize=A4,
                                  leftMargin=1.5*cm, rightMargin=1.5*cm,
                                  topMargin=1.5*cm, bottomMargin=1.5*cm,
                                  title=f"Report {_active_profile or 'default'}")
        styles = getSampleStyleSheet()
        h1 = ParagraphStyle("h1", parent=styles["Heading1"], textColor=colors.HexColor("#1F4E78"),
                              fontSize=18, spaceAfter=6, fontName=FONT_BOLD)
        h2 = ParagraphStyle("h2", parent=styles["Heading2"], textColor=colors.HexColor("#1F4E78"),
                              fontSize=13, spaceBefore=10, spaceAfter=4, fontName=FONT_BOLD)
        sub = ParagraphStyle("sub", parent=styles["Normal"], textColor=colors.HexColor("#666666"),
                               fontSize=10, spaceAfter=2, fontName=FONT_REG)
        small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8,
                                textColor=colors.HexColor("#888888"), fontName=FONT_REG)
        story = []

        # ── Titulná hlavička ──
        story.append(Paragraph("FTV + batéria — Report výsledkov", h1))
        story.append(Paragraph(f"Profil: <b>{_active_profile or '(default)'}</b> &nbsp;&nbsp;&nbsp; "
                                f"Trh: <b>{_market.upper()}</b> &nbsp;&nbsp;&nbsp; Case: {case}", sub))
        story.append(Paragraph(f"Obdobie: <b>{_period_from} – {_period_to}</b> &nbsp; ({_period_days} dní) &nbsp;&nbsp;&nbsp; "
                                f"Generované: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}", sub))
        story.append(Spacer(1, 6))

        # ── Konfigurácia (2-stĺpcová tabuľka) ──
        story.append(Paragraph("Konfigurácia inštalácie", h2))
        config_data = [
            ["FTV inštalácia", f"{float(_pui_plan.get('kwp', 99)):,.0f} kWp",
             "Lokalita", f"{float(_pui_plan.get('lat', 0)):.2f}° / {float(_pui_plan.get('lon', 0)):.2f}°"],
            ["Batéria výkon", f"{float(_pui_plan.get('batt_kw', 100)):,.0f} kW",
             "Batéria kapacita", f"{float(_pui_plan.get('batt_kwh', 200)):,.0f} kWh"],
            ["SOC limit", f"{float(_pui_plan.get('soc_min', 5)):.0f} – {float(_pui_plan.get('soc_max', 95)):.0f} %",
             "Limit prípojky", f"{float(_pui_plan.get('grid_kw', 100)):,.0f} kW"],
            ["Min. spread D-1", f"{float(_pui_plan.get('min_spread', 30)):.0f} €/MWh",
             "", ""],
        ]
        t_cfg = Table(config_data, colWidths=[3.5*cm, 4*cm, 3.5*cm, 5*cm])
        t_cfg.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), FONT_REG),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#666666")),
            ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#666666")),
            ("FONTNAME", (1, 0), (1, -1), FONT_BOLD),
            ("FONTNAME", (3, 0), (3, -1), FONT_BOLD),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.HexColor("#CCCCCC")),
        ]))
        story.append(t_cfg)
        story.append(Spacer(1, 6))

        # ── KPI: Ekonomika ──
        _total_dt = float(daily["dt_eur"].sum())
        _total_rt = float(daily["rt_eur"].sum())
        _total_dist = float(daily["dist_fee_eur"].sum()) if "dist_fee_eur" in daily.columns else 0.0
        _total_spolu = _total_dt + _total_rt + _total_dist
        _total_bl = float(daily["baseline_eur"].sum())
        _prinos = _total_spolu - _total_bl
        _prinos_pct = (_prinos / _total_bl * 100.0) if _total_bl > 0 else 0
        _per_day = _total_spolu / max(_period_days, 1)
        story.append(Paragraph("Kľúčové ukazovatele", h2))
        kpi_data = [
            ["", "Suma [€]", "Per deň [€]", "% z baseline"],
            ["Celkový zisk", f"{_total_spolu:,.2f}", f"{_per_day:,.2f}", ""],
            ["    Z denného trhu (D-1)", f"{_total_dt:,.2f}", f"{_total_dt/max(_period_days,1):,.2f}", ""],
            ["    Z odchýlky (RT)", f"{_total_rt:,.2f}", f"{_total_rt/max(_period_days,1):,.2f}", ""],
            ["Baseline (bez batérie + plánu)", f"{_total_bl:,.2f}", f"{_total_bl/max(_period_days,1):,.2f}", "100 %"],
            ["Prínos batérie + plánu", f"{_prinos:+,.2f}", f"{_prinos/max(_period_days,1):+,.2f}", f"{_prinos_pct:+.1f} %"],
            ["    z toho úspora na distribúcii", f"{_total_dist:+,.2f}", f"{_total_dist/max(_period_days,1):+,.2f}", ""],
        ]
        t_kpi = Table(kpi_data, colWidths=[7*cm, 3.2*cm, 3.2*cm, 3.2*cm])
        _green = colors.HexColor("#2E7D32") if _prinos >= 0 else colors.HexColor("#C0392B")
        t_kpi.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), FONT_REG),
            ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF3F9")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("FONTNAME", (1, 1), (-1, 1), FONT_BOLD),  # Celkový zisk bold
            ("FONTNAME", (0, 5), (-1, 5), FONT_BOLD),  # Prínos bold
            ("TEXTCOLOR", (1, 5), (-1, 5), _green),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#1F4E78")),
            ("LINEABOVE", (0, 5), (-1, 5), 0.5, colors.HexColor("#CCCCCC")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t_kpi)
        story.append(Spacer(1, 6))

        # ── KPI: Energia + Batéria (dve dvojstĺpcové tabuľky vedľa seba) ──
        _ftv_total = float(daily["ftv_vyroba_kwh"].sum())
        _load_total = float(daily["spotreba_kwh"].sum())
        _exp_total = float(daily["export_kwh"].sum())
        _imp_total = float(daily["import_kwh"].sum())
        _batt_ch = float(daily["batt_charge_kwh"].sum())
        _batt_di = float(daily["batt_discharge_kwh"].sum())
        _batt_cyc = float(daily["cycles"].sum())
        _soc_avg = float(daily["soc_avg_pct"].mean()) if not daily.empty else 0
        _soc_min = float(daily["soc_min_pct"].min()) if not daily.empty else 0
        _soc_max = float(daily["soc_max_pct"].max()) if not daily.empty else 0
        _dt_avg = float(df["dt_real_eur"].mean()) if "dt_real_eur" in df.columns else 0
        _zco_avg = float(df["zco_eur_min"].dropna().mean()) if "zco_eur_min" in df.columns else 0

        story.append(Paragraph("Energia &nbsp;&nbsp;|&nbsp;&nbsp; Batéria &nbsp;&nbsp;|&nbsp;&nbsp; Ceny", h2))
        eb_data = [
            ["Energia", "MWh", "kWh/deň",  "", "Batéria", "", "", "Ceny", "€/MWh"],
            ["FTV výroba",          f"{_ftv_total/1000:,.2f}",  f"{_ftv_total/max(_period_days,1):,.0f}", "",
             "Nabíjanie",           f"{_batt_ch/1000:,.2f} MWh", "", "",
             "Priemerná DT",        f"{_dt_avg:,.2f}"],
            ["Spotreba",            f"{_load_total/1000:,.2f}", f"{_load_total/max(_period_days,1):,.0f}", "",
             "Vybíjanie",           f"{_batt_di/1000:,.2f} MWh", "", "",
             "Priemerná ZCO",       f"{_zco_avg:,.2f}"],
            ["Export",              f"{_exp_total/1000:,.2f}",  f"{_exp_total/max(_period_days,1):,.0f}", "",
             "Cyklov spolu",        f"{_batt_cyc:.2f}",  "", "",
             "", ""],
            ["Import",              f"{_imp_total/1000:,.2f}",  f"{_imp_total/max(_period_days,1):,.0f}", "",
             "Priemer SOC",         f"{_soc_avg:.1f} %", "", "",
             "", ""],
            ["", "", "", "",
             "Rozsah SOC",          f"{_soc_min:.0f} – {_soc_max:.0f} %", "", "",
             "", ""],
        ]
        t_eb = Table(eb_data, colWidths=[2.5*cm, 1.8*cm, 1.8*cm, 0.3*cm,
                                          2.2*cm, 2*cm, 0.0*cm, 0.3*cm,
                                          2.5*cm, 1.8*cm])
        t_eb.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), FONT_REG),
            ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("BACKGROUND", (0, 0), (2, 0), colors.HexColor("#EEF3F9")),
            ("BACKGROUND", (4, 0), (5, 0), colors.HexColor("#EEF3F9")),
            ("BACKGROUND", (8, 0), (9, 0), colors.HexColor("#EEF3F9")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
            ("ALIGN", (1, 0), (2, -1), "RIGHT"),
            ("ALIGN", (5, 0), (5, -1), "RIGHT"),
            ("ALIGN", (9, 0), (9, -1), "RIGHT"),
            ("FONTNAME", (1, 1), (2, -1), FONT_BOLD),
            ("FONTNAME", (5, 1), (5, -1), FONT_BOLD),
            ("FONTNAME", (9, 1), (9, -1), FONT_BOLD),
            ("LINEBELOW", (0, 0), (2, 0), 0.5, colors.HexColor("#1F4E78")),
            ("LINEBELOW", (4, 0), (5, 0), 0.5, colors.HexColor("#1F4E78")),
            ("LINEBELOW", (8, 0), (9, 0), 0.5, colors.HexColor("#1F4E78")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.append(t_eb)

        # ── Page 2: Mesačná sekcia ──
        if len(monthly) > 0:
            story.append(PageBreak())
            story.append(Paragraph(f"Mesačný prehľad ({len(monthly)} mesiacov)", h1))
            story.append(Spacer(1, 4))
            # Tabuľka mesiacov
            mh = ["Mesiac", "Dni", "D-1 €", "RT €", "Spolu €", "Baseline €", "Prínos €",
                  "Ø DT", "Ø ZCO", "FTV MWh", "Cyklov"]
            mdata = [mh]
            for _, r in monthly.iterrows():
                mdata.append([
                    r["month"], f"{int(r['dni'])}",
                    f"{r['dt_eur']:,.0f}", f"{r['rt_eur']:,.0f}",
                    f"{r['total_eur']:,.0f}", f"{r['baseline_eur']:,.0f}",
                    f"{r['prinos_eur']:+,.0f}",
                    f"{r['dt_cena_avg']:.0f}",
                    (f"{r['zco_cena_avg']:.0f}" if pd.notna(r['zco_cena_avg']) else "–"),
                    f"{r['ftv_vyroba_kwh']/1000:.1f}",
                    f"{r['cycles']:.1f}",
                ])
            t_m = Table(mdata, colWidths=[1.7*cm, 1*cm, 1.6*cm, 1.4*cm, 1.6*cm, 1.6*cm, 1.6*cm,
                                            1.3*cm, 1.3*cm, 1.5*cm, 1.3*cm])
            t_m.setStyle(TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("FONTNAME", (0, 0), (-1, -1), FONT_REG),
                ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(t_m)
            story.append(Spacer(1, 8))

            # Mesačný graf: Zisk vs Baseline
            m_labels = monthly["month"].tolist()
            fig1 = _bar_compare(m_labels, {
                "D-1 + RT (Spolu)": monthly["total_eur"].values,
                "Baseline":         monthly["baseline_eur"].values,
            }, "Zisk po mesiacoch - Spolu vs Baseline", "€")
            story.append(_fig_to_image(fig1))
            story.append(Spacer(1, 4))

            # FTV výroba vs Spotreba
            fig2 = _bar_compare(m_labels, {
                "FTV výroba [kWh]": monthly["ftv_vyroba_kwh"].values,
                "Spotreba [kWh]":   monthly["spotreba_kwh"].values,
            }, "FTV výroba vs Spotreba (kWh/mesiac)", "kWh")
            story.append(_fig_to_image(fig2))

        # ── Page 3: Denná sekcia ──
        if len(daily) > 0:
            story.append(PageBreak())
            story.append(Paragraph(f"Denný prehľad ({len(daily)} dní)", h1))
            story.append(Paragraph(
                f"Obdobie: {_period_from} – {_period_to}. "
                f"Grafy nižšie zobrazujú x-osové popisy len pre vybrané dni "
                f"(rovnomerne rozložené); plné údaje sú v tabuľke a v Excel exporte.",
                sub))
            story.append(Spacer(1, 4))

            d_labels = [str(d) for d in daily["date"].tolist()]
            # Denný zisk
            fig3 = _line(d_labels, {
                "D-1": daily["dt_eur"].values,
                "RT": daily["rt_eur"].values,
                "Spolu": daily["total_eur"].values,
                "Baseline": daily["baseline_eur"].values,
            }, "Denný zisk (€)", "€")
            story.append(_fig_to_image(fig3))
            story.append(Spacer(1, 4))

            # Prínos
            fig4 = _line(d_labels, {
                "Prínos batérie + plán": daily["prinos_eur"].values,
            }, "Denný prínos batérie + plánu vs baseline", "€")
            story.append(_fig_to_image(fig4))

            # SOC + cykly
            story.append(PageBreak())
            story.append(Paragraph("Denné batériové metriky", h1))
            story.append(Spacer(1, 4))
            fig5 = _line(d_labels, {
                "SOC priemer": daily["soc_avg_pct"].values,
                "SOC min": daily["soc_min_pct"].values,
                "SOC max": daily["soc_max_pct"].values,
            }, "SOC % počas dní", "%")
            story.append(_fig_to_image(fig5))
            story.append(Spacer(1, 4))
            fig6 = _line(d_labels, {
                "Cyklov za deň": daily["cycles"].values,
            }, "Počet cyklov batérie po dňoch", "cyklov")
            story.append(_fig_to_image(fig6))

        # ── Page 4: Detail vybraný deň ──
        if det15 is not None and not det15.empty:
            story.append(PageBreak())
            story.append(Paragraph(f"Detail dňa {_detail_day}", h1))
            story.append(Spacer(1, 4))
            t_labels = [str(t)[11:16] for t in det15["ts15"]]
            fig7 = _line(t_labels, {
                "DT cena": det15["dt_cena_avg"].values,
                "ZCO cena": det15["zco_cena_avg"].values,
            }, f"DT a ZCO ceny ({_detail_day})", "€/MWh")
            story.append(_fig_to_image(fig7))
            story.append(Spacer(1, 4))

            fig8 = _line(t_labels, {
                "SOC %": det15["soc_avg_pct"].values,
            }, f"SOC % počas dňa ({_detail_day})", "%")
            story.append(_fig_to_image(fig8))
            story.append(Spacer(1, 4))

            fig9 = _line(t_labels, {
                "FTV výroba [kWh]": det15["ftv_vyroba_kwh"].values,
                "Export [kWh]": det15["export_kwh"].values,
            }, f"FTV výroba a export ({_detail_day})", "kWh")
            story.append(_fig_to_image(fig9))

        # ── Posledná strana(y): Kompletná denná tabuľka ──
        # User reportoval že v grafoch chýbajú mesiace — pridáme explicitnú tabuľku
        # všetkých dní (auto-break cez stránky, repeatRows hlavička).
        if len(daily) > 0:
            story.append(PageBreak())
            story.append(Paragraph(f"Tabuľka všetkých dní ({len(daily)} dní)", h1))
            story.append(Paragraph(
                "Každý simulovaný deň s celkovým ziskom, baseline, prínosom a batériovými KPI. "
                "Tabuľka sa automaticky rozdelí na viacero strán.", sub))
            story.append(Spacer(1, 4))
            dh = ["Dátum", "D-1 €", "RT €", "Spolu €", "Baseline €", "Prínos €",
                  "Ø DT €/MWh", "FTV kWh", "Spotr. kWh", "Cyklov", "SOC ø %"]
            ddata = [dh]
            for _, r in daily.iterrows():
                ddata.append([
                    str(r["date"]),
                    f"{r['dt_eur']:,.1f}", f"{r['rt_eur']:,.1f}",
                    f"{r['total_eur']:,.1f}", f"{r['baseline_eur']:,.1f}",
                    f"{r['prinos_eur']:+,.1f}",
                    f"{r['dt_cena_avg']:.1f}",
                    f"{r['ftv_vyroba_kwh']:,.0f}", f"{r['spotreba_kwh']:,.0f}",
                    f"{r['cycles']:.2f}", f"{r['soc_avg_pct']:.0f}",
                ])
            t_d = Table(ddata, colWidths=[2.1*cm, 1.5*cm, 1.4*cm, 1.5*cm, 1.5*cm, 1.5*cm,
                                            1.6*cm, 1.5*cm, 1.6*cm, 1.2*cm, 1.2*cm],
                          repeatRows=1)
            t_d.setStyle(TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("FONTNAME", (0, 0), (-1, -1), FONT_REG),
                ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("GRID", (0, 0), (-1, -1), 0.2, colors.HexColor("#DDDDDD")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                # zebra
                ("ROWBACKGROUNDS", (0, 1), (-1, -1),
                 [colors.white, colors.HexColor("#F7FAFD")]),
            ]))
            story.append(t_d)

        # Footer cez vlastnú callback funkciu — generated by stamp
        def _footer(canvas, doc):
            canvas.saveState()
            canvas.setFont(FONT_REG, 7)
            canvas.setFillColor(colors.HexColor("#888888"))
            canvas.drawString(1.5*cm, 1*cm, f"Report {_active_profile or 'default'} | "
                                              f"{_period_from} – {_period_to} | "
                                              f"strana {doc.page}")
            canvas.restoreState()

        doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
        buf_pdf.seek(0)
        _prof_part = (_active_profile or "default").replace(" ", "_")
        fname = f"report_{_prof_part}_{_period_from}_{_period_to}.pdf"
        return StreamingResponse(buf_pdf, media_type="application/pdf",
                                  headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    except Exception as e:
        import traceback
        return Response(f"PDF chyba: {e}\n{traceback.format_exc()}",
                         status_code=500, media_type="text/plain")


@app.get("/livesim_table_xlsx")
def livesim_table_xlsx(case: str = "plan_d1", day: str = None):
    """Export 15-min agregát tabuľky ako Excel (xlsx) pre celý deň.
    Slot, batt_kw, DAM_kw, VDT_kw, work_kWh, SOC%, FTV_kw, DT_eur, VDT_eur.

    Bug TABLE-EXPORT (2026-06-15): predtým export (CSV) načítaval natvrdo case='default'
    → pre aktívny profil/case (plan_d1) nenašiel dáta → 404 'Žiadne dáta' = export nedal
    nič. Teraz berie case z requestu (default plan_d1) + výstup je Excel namiesto CSV."""
    from fastapi.responses import StreamingResponse
    import io
    if not day:
        day = dt.date.today().isoformat()
    try:
        df = lsim.load_series(case, port=_PORT, day=day, max_points=10**9)
    except Exception as e:
        return PlainTextResponse(f"ERROR load_series: {e}", status_code=500)
    if df is None or df.empty:
        return PlainTextResponse(f"Žiadne dáta pre {day} (case={case})", status_code=404)
    df = df.copy()
    df["_slot"] = pd.to_datetime(df["time"]).dt.floor("15min")
    agg = {"plan_batt_kw": "mean", "soc_pct": "last",
           "ftv_kw": "mean", "dt_eur": "mean"}
    if "plan_batt_dam_kw" in df.columns:
        agg["plan_batt_dam_kw"] = "mean"
    if "plan_batt_vdt_kw" in df.columns:
        agg["plan_batt_vdt_kw"] = "mean"
    if "dt_real_eur" in df.columns:
        agg["dt_real_eur"] = "mean"
    if "vdt_eur" in df.columns:
        agg["vdt_eur"] = "mean"
    g = df.groupby("_slot").agg(agg).reset_index()
    g["work_kwh"] = g["plan_batt_kw"] * 0.25
    # VDT cena z OKTE historian ak v dview prázdna
    try:
        if "vdt_eur" not in g.columns or g["vdt_eur"].abs().max() < 0.01:
            import seps_sk as _ss_c
            vm = _ss_c.load_okte_vdt_preliminary_for_day(day) or _ss_c.load_okte_vdt_for_day(day) or {}
            if vm:
                g["vdt_eur"] = g["_slot"].map(
                    lambda t: float(vm.get(f"{pd.Timestamp(t).hour:02d}:{pd.Timestamp(t).minute:02d}", 0.0)))
    except Exception:
        pass
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"livesim {day}"[:31]
    headers = ["slot_start", "slot_end", "batt_kw_net", "batt_dam_kw", "batt_vdt_kw",
               "work_kwh", "soc_pct", "ftv_kw", "dt_eur_mwh", "dt_real_eur_mwh", "vdt_eur_mwh"]
    ws.append(headers)
    _hdr_fill = PatternFill("solid", fgColor="1F4E78")
    _hdr_font = Font(bold=True, color="FFFFFF")
    for _c in ws[1]:
        _c.fill = _hdr_fill
        _c.font = _hdr_font
        _c.alignment = Alignment(horizontal="center")
    for _, r in g.iterrows():
        ts = pd.Timestamp(r["_slot"])
        ts_end = ts + pd.Timedelta(minutes=15)
        ws.append([
            ts.strftime("%Y-%m-%d %H:%M"),
            ts_end.strftime("%H:%M"),
            round(float(r.get('plan_batt_kw', 0)), 1),
            round(float(r.get('plan_batt_dam_kw', 0)), 1),
            round(float(r.get('plan_batt_vdt_kw', 0)), 1),
            round(float(r.get('work_kwh', 0)), 2),
            round(float(r.get('soc_pct', 0)), 1),
            round(float(r.get('ftv_kw', 0)), 1),
            round(float(r.get('dt_eur', 0)), 1),
            round(float(r.get('dt_real_eur', 0)), 1),
            round(float(r.get('vdt_eur', 0)), 1),
        ])
    ws.freeze_panes = "A2"
    for _col in ws.columns:
        ws.column_dimensions[_col[0].column_letter].width = 14
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="livesim_15min_{day}.xlsx"'}
    )


@app.get("/livesim", response_class=HTMLResponse)
def livesim_get(case: str = None, start: str = None, view: str = None, curtail: str = None,
                 use_rt: str = None, realio_overlay: str = None, profile: str = None,
                 table_offset: int = 0, table_rows: int = 20):
    """Živá simulácia. Voliteľné parametre:
      • realio_overlay=1 — nahradí sim FTV/load/SOC/batt reálnym meraním z realio CSV
        (pre minúty kde máme záznam). Plány zostávajú simulované. Slúži pre tab
        „Reálne riadenie" v /realio.
      • profile=<name>   — vynúti konkrétny profil (rovnako ako env FTV_PROFILE).
    """
    # Profile override — nastav cez env aby plan_store.resolve_profile() ho použil.
    # CRITICAL: env var je process-wide — ak nereset-ujeme keď profile=None, predošlé
    # nastavenie (napr. z /realio iframe /livesim?profile=Trakany_real) pretrváva NAVŽDY
    # a všetky ďalšie /livesim requesty vidia ten profil namiesto globálneho active.
    # → Bug G: po /profiles/apply nový profile chip OK, ale /livesim ukazuje stary.
    if profile:
        os.environ["FTV_PROFILE"] = profile
    else:
        os.environ.pop("FTV_PROFILE", None)
    # Realio overlay flag — explicitne cez query alebo automaticky ak je aktívny profile typu 'real'
    realio_on = (str(realio_overlay or "") == "1")
    if not realio_on:
        try:
            import profiles as _pr
            import plan_store as _ps
            _cur_prof = _ps.resolve_profile()
            if _pr.get_mode(_cur_prof) == _pr.MODE_REAL:
                realio_on = True
        except Exception:
            pass
    try:
        cases = cc.list_cases()
        base = "realistic" if "realistic" in cases else (cases[0] if cases else "realistic")
        # dve pomenované možnosti – OBE používajú nastavenia z 'base' (tvoje naladené), líšia sa granularitou plánu
        MODES = {"plan_d1": ("Plán D-1 (hodinový)", base, 60),
                 "dt_15min": ("Denný trh 15-min", base, 15)}
        saved = _ui_load("livesim", {"case": "plan_d1",
                                     "start": (dt.date.today() - dt.timedelta(days=7)).isoformat()})
        case = case or saved.get("case")
        if case not in MODES:
            case = "plan_d1"
        start = start or saved.get("start")
        # --- orezanie FTV: uloží sa do PRÍPADU (pamätá sa + prejaví sa v simulácii) ---
        _bcfg = cc.load_case(base)
        if curtail is not None:                       # formulár odoslaný → ulož voľbu do prípadu
            new_cu = (str(curtail) == "1")
            if bool(_bcfg.allow_curtail) != new_cu:
                _bcfg.allow_curtail = new_cu
                cc.save_case(_bcfg)
                # nastavenie zmenilo plán → starý log je neaktuálny, zmaž ho (prepočíta sa odznova)
                for _p in (paths_glob := __import__("glob").glob(f"out/livesim_{case}*")):
                    try:
                        os.remove(_p)
                    except OSError:
                        pass
        cur_curtail = bool(_bcfg.allow_curtail)
        # --- use_rt toggle (per-livesim, NEZdieľané s case) ---
        # POZOR: v móde dt_15min je RT odchýlka ZÁKLADNE VYPNUTÁ — 15-min DT ide na čistú cenovú arbitráž,
        # odchýlka sa nezohľadňuje (ceny už poznáme dopredu, treba ich len optimálne využiť).
        _is_dentrh_mode = (case == "dt_15min")
        if _is_dentrh_mode:
            cur_use_rt = False
        else:
            cur_use_rt = bool(saved.get("use_rt", bool(getattr(_bcfg, "use_rt", True))))
            if use_rt is not None:
                new_rt = (str(use_rt) == "1")
                if new_rt != cur_use_rt:
                    cur_use_rt = new_rt
                    # zmena režimu RT → starý log je neaktuálny, zmaž ho
                    for _p in __import__("glob").glob(f"out/livesim_{case}*"):
                        try:
                            os.remove(_p)
                        except OSError:
                            pass
        head = ("""<!doctype html><html lang="sk"><head><meta charset="utf-8"><title>Živá simulácia</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:-apple-system,Segoe UI,Arial;max-width:1680px;margin:24px auto;padding:0 16px;color:#222}
h1,h2{color:#1F4E78} .card{background:#f3f6fb;border-radius:10px;padding:10px 14px;min-width:150px}
.card .l{font-size:12px;color:#666} .card .v{font-size:22px;font-weight:700}
label{font-size:14px} input,select{padding:5px 8px;border:1px solid #ccc;border-radius:7px}
table{border-collapse:collapse;width:100%;font-size:12px} th,td{border:1px solid #e3e3e3;padding:3px 7px;text-align:right}
th{background:#1F4E78;color:#fff} td:first-child{text-align:left} .wrap{max-height:300px;overflow:auto;border-radius:8px}</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script></head><body>
<h1>\U0001F7E2 Živá simulácia</h1>""" + _nav("/livesim"))
        opts = "".join(f'<option value="{k}"{" selected" if k==case else ""}>{lbl}</option>'
                        for k, (lbl, _bc, _st) in MODES.items())
        cuopts = (f'<option value="1"{" selected" if cur_curtail else ""}>povolené</option>'
                  f'<option value="0"{" selected" if not cur_curtail else ""}>vypnuté</option>')
        # V dt_15min móde je RT vynútene OFF a dropdown je disabled (odchýlka sa nezohľadňuje, ide čistá DT arbitráž).
        if _is_dentrh_mode:
            rtopts = f'<option value="0" selected>nie (15-min DT iba arbitráž)</option>'
            _rt_label = ("RT odchýlka<br><select name=\"use_rt\" disabled "
                          "style='background:#f0f0f0;color:#888;cursor:not-allowed'>" + rtopts + "</select>"
                          "<input type='hidden' name='use_rt' value='0'>")
            _rt_title = "V móde 15-min DT je RT odchýlka VYPNUTÁ (čistá cenová arbitráž — ceny už poznáme z OTE)."
        else:
            rtopts = (f'<option value="1"{" selected" if cur_use_rt else ""}>áno (s odchýlkou)</option>'
                      f'<option value="0"{" selected" if not cur_use_rt else ""}>nie (čistý plán)</option>')
            _rt_label = f"RT odchýlka<br><select name=\"use_rt\">{rtopts}</select>"
            _rt_title = "Áno = MW signal RT + FTV-balance vrstva. Nie = batéria ide IBA podľa plánu (žiadna odchýlková arbitráž)."
        form = (f'<form method="get" action="/livesim" style="margin:8px 0 16px;display:flex;gap:10px;flex-wrap:wrap;align-items:end">'
                f'<label>Prípad<br><select name="case">{opts}</select></label>'
                f'<label>Dátum štartu<br><input name="start" type="date" value="{start}"></label>'
                f'<label>Orezať/vypnúť FTV<br><select name="curtail">{cuopts}</select></label>'
                f'<label title="{_rt_title}">{_rt_label}</label>'
                f'<button type="submit" style="padding:7px 16px;background:#1F4E78;color:#fff;border:none;border-radius:8px;cursor:pointer">Spustiť / obnoviť</button>'
                f'<span style="color:#666;font-size:13px">Beží na porte {_PORT}, nastavenia z prípadu <b>{base}</b>. Prvé spustenie (od dátumu štartu) môže chvíľu trvať.</span></form>')
        if not case:
            return head + form + "</body></html>"
        _ui_save("livesim", {"case": case, "start": start, "use_rt": cur_use_rt})
        _lbl, _bc, _st = MODES[case]
        # pre-flight kontrola: existujú plány pre celý rozsah start → today?
        plan_kind = "dentrh" if int(_st) == 15 else "plan"
        miss_plans = []
        if ps is not None:
            try:
                miss_plans = ps.missing_plans(start, dt.date.today().isoformat(), int(_st), plan_kind)
            except Exception:
                miss_plans = []
        # ── AUTO-DETEKCIA: ak aktuálny case nemá žiadne plány pre rozsah dni
        # → ponúkni prepnutie na druhý mód (ak má plány v tomto profile) ──
        mode_switch_banner = ""
        if ps is not None:
            try:
                _today_iso = dt.date.today().isoformat()
                _all_dates = pd.date_range(start, _today_iso, freq="D")
                _total_days = max(1, len(_all_dates))
                _miss_cur = len(miss_plans)                              # pre aktuálny mód
                # druhý mód
                other_case = "dt_15min" if case == "plan_d1" else "plan_d1"
                _other_st = 15 if other_case == "dt_15min" else 60
                other_kind = "dentrh" if other_case == "dt_15min" else "plan"
                _miss_other = ps.missing_plans(start, _today_iso, _other_st, other_kind)
                _have_cur = _total_days - _miss_cur
                _have_other = _total_days - len(_miss_other)
                active_prof = ps.resolve_profile()
                # zobraz banner keď: aktuálny mód má 0 plánov ALEBO druhý má signifikantne viac (>=2× viac)
                if _have_cur == 0 and _have_other > 0:
                    _other_lbl = MODES[other_case][0]
                    mode_switch_banner = (
                        f"<div style='background:#ffeaea;border-left:5px solid #C0392B;border-radius:8px;"
                        f"padding:12px 16px;margin:10px 0;color:#7a0000'>"
                        f"❌ <b>Aktívny mód „{_lbl}" + "“"
                        f" nemá <u>žiadne</u> plány v profile <code>{active_prof}</code></b> "
                        f"pre rozsah {start} … {_today_iso}. "
                        f"Druhý mód <b>„{_other_lbl}"+"“"+f"</b> má v tomto profile <b>{_have_other} z {_total_days}</b> dní pokrytých. "
                        f"<a href='/livesim?case={other_case}&start={start}' "
                        f"style='background:#2E7D32;color:#fff;padding:6px 12px;border-radius:6px;"
                        f"text-decoration:none;margin-left:8px;font-weight:600'>"
                        f"→ Prepnúť na „{_other_lbl}"+"“"+f"</a>"
                        f"</div>")
                elif _have_other > 2 * max(1, _have_cur):
                    _other_lbl = MODES[other_case][0]
                    mode_switch_banner = (
                        f"<div style='background:#fff3cd;border-left:5px solid #f0b80f;border-radius:8px;"
                        f"padding:12px 16px;margin:10px 0;color:#7a5c00'>"
                        f"⚠ Druhý mód <b>„{_other_lbl}"+"“"+f"</b> má v profile <code>{active_prof}</code> viac plánov "
                        f"({_have_other} vs {_have_cur} v aktuálnom móde). "
                        f"<a href='/livesim?case={other_case}&start={start}' "
                        f"style='background:#5E35B1;color:#fff;padding:6px 12px;border-radius:6px;"
                        f"text-decoration:none;margin-left:8px;font-weight:600'>"
                        f"→ Prepnúť na „{_other_lbl}"+"“"+f"</a>"
                        f"</div>")
            except Exception:
                pass
        # Ak aktuálny mód má 0 plánov v rozsahu → musíme vrátiť stránku BEZ volania advance
        # (inak by spadla s RuntimeError). Banner ukazuje ako prepnúť alebo dogenerovať.
        _abort_advance = False
        try:
            _abort_advance = (ps is not None and _have_cur == 0)
        except NameError:
            pass
        if _abort_advance:
            _today_iso2 = dt.date.today().isoformat()
            return head + form + (mode_switch_banner or
                f"<div style='background:#fff3cd;border-left:5px solid #f0b80f;padding:12px 16px;margin:10px 0'>"
                f"⚠ Pre tento profil + mód neexistujú plány v rozsahu {start} … {_today_iso2}. "
                f"Vygeneruj cez /plan_batch alebo /plan.</div>") + "</body></html>"
        # Custom batch form (vždy zobrazený) — generovať plány pre voľný rozsah
        _t_iso = dt.date.today().isoformat()
        batch_form = (f"<form method='post' action='/plan_batch' style='display:inline-flex;gap:8px;align-items:center'>"
                       f"<input type='hidden' name='step_min' value='{int(_st)}'>"
                       f"<input type='hidden' name='kind' value='{plan_kind}'>"
                       f"<input name='from_date' type='date' value='{start}' style='padding:4px;border:1px solid #ccc;border-radius:5px'>"
                       f"<span>→</span>"
                       f"<input name='to_date' type='date' value='{_t_iso}' style='padding:4px;border:1px solid #ccc;border-radius:5px'>"
                       f"<button type='submit' style='background:#2E7D32;color:#fff;border:0;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:13px'>"
                       f"Generovať plány od–do</button></form>")
        plan_warn = ""
        if miss_plans:
            preview = ", ".join(miss_plans[:6]) + (f" … (+{len(miss_plans)-6})" if len(miss_plans) > 6 else "")
            quick_batch_btn = (f"<form method='post' action='/plan_batch' style='display:inline'>"
                          f"<input type='hidden' name='from_date' value='{miss_plans[0]}'>"
                          f"<input type='hidden' name='to_date' value='{miss_plans[-1]}'>"
                          f"<input type='hidden' name='step_min' value='{int(_st)}'>"
                          f"<input type='hidden' name='kind' value='{plan_kind}'>"
                          f"<button type='submit' style='background:#C0392B;color:#fff;border:0;padding:8px 14px;border-radius:7px;cursor:pointer;font-weight:600'>"
                          f"⚡ Generovať plány pre chýbajúce dni ({len(miss_plans)})</button></form>")
            plan_warn = (f"<div style='background:#ffe8e0;border-left:4px solid #C0392B;border-radius:6px;"
                          f"padding:10px 14px;margin:10px 0;font-size:13px'>"
                          f"❌ <b>Chýbajú D-1 plány pre {len(miss_plans)} dni</b> ({plan_kind}, {int(_st)}-min): {preview}.<br>"
                          f"Livesim STRICT mode tieto dni <b>preskočí</b>. {quick_batch_btn}<br><br>"
                          f"<span style='color:#666'>Alebo vlastný rozsah:</span> {batch_form}</div>")
        else:
            plan_warn = (f"<div style='background:#eef5e0;border-left:4px solid #2E7D32;border-radius:6px;"
                          f"padding:8px 12px;margin:6px 0;font-size:13px'>"
                          f"✓ Všetky plány pre rozsah <b>{start} → {_t_iso}</b> sú k dispozícii. "
                          f"<span style='color:#666'>Doplniť rozsah:</span> {batch_form}</div>")
        live_min = _livesim_live_minutes()
        rtp = _livesim_rt_params(_bcfg)
        plan_pp = _ui_load("plan", DEF)            # nastavenia z formulára PLÁNU (SOC, terminal, min_spread…)
        try:
            # Bug W: cached_advance vráti cachované r ak meta.json nezmenila od BG ticku.
            # Žiadne zbytočné výpočty pri opakovanom otváraní dashboardu.
            try:
                from core.profile_resolver import get_active as _ga_w
                _profile_key = str(_ga_w() or "")
            except Exception:
                _profile_key = ""
            r = _livesim_cached_advance(
                case, start, _PORT, _bc, _st,
                live_min, rtp, plan_pp, cur_use_rt,
                profile_key=_profile_key)
            # Bug COMPUTE-WORKER: žiadny hotový stav (prvý beh po resete) → progress
            # stránka s auto-refresh; pri chybe background behu → friendly error page.
            if r is None:
                _cw_st = _livesim_compute_status(case, _PORT, _profile_key)
                if _cw_st.get("err") and not _cw_st.get("running"):
                    raise RuntimeError(_cw_st["err"])
                import time as _t_pp
                _cw_run_s = int(_t_pp.time() - float(_cw_st.get("started") or _t_pp.time()))
                # BG-PROGRESS: progress bar z _LIVESIM_COMPUTE_PROGRESS (done/total dní)
                _pg = _cw_st.get("progress") or {}
                _pg_done = int(_pg.get("done", 0)); _pg_total = int(_pg.get("total", 0))
                _pg_day = _pg.get("day", "")
                if _pg_total > 0:
                    _pct = max(0, min(100, int(_pg_done / _pg_total * 100)))
                    _eta = ""
                    if _pg_done > 0 and _cw_run_s > 2:
                        _per = _cw_run_s / _pg_done
                        _rem = int(_per * (_pg_total - _pg_done))
                        _eta = f" · ostáva ~{_rem//60} min {_rem%60} s" if _rem >= 60 else f" · ostáva ~{_rem} s"
                    _bar = (
                        f"<div style='margin:14px 0 6px'>"
                        f"<div style='background:#cfe0f0;border-radius:8px;height:22px;overflow:hidden'>"
                        f"<div style='background:#1F4E78;height:100%;width:{_pct}%;"
                        f"transition:width .4s;display:flex;align-items:center;justify-content:flex-end;"
                        f"padding-right:8px;color:#fff;font-size:12px;font-weight:600'>{_pct}%</div></div>"
                        f"<div style='color:#666;font-size:13px;margin-top:6px'>"
                        f"deň {_pg_done}/{_pg_total}{(' · ' + _pg_day) if _pg_day else ''}{_eta}</div>"
                        f"</div>")
                else:
                    _bar = ("<div style='color:#666;font-size:13px;margin-top:8px'>"
                            "pripravujem dáta…</div>")
                return (
                    f"<!doctype html><html lang='sk'><head><meta charset='utf-8'>"
                    f"<meta http-equiv='refresh' content='5'>"
                    f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    f"<title>Živá simulácia — prepočet beží</title></head>"
                    f"<body style='font-family:-apple-system,Segoe UI,Arial;max-width:780px;"
                    f"margin:60px auto;padding:0 20px;color:#222'>"
                    f"{_nav('/livesim')}"
                    f"<h1 style='color:#1F4E78'>🟢 Živá simulácia</h1>"
                    f"<div style='background:#e3f2fd;border-left:5px solid #1F4E78;"
                    f"border-radius:8px;padding:16px 20px;font-size:15px'>"
                    f"⏳ <b>Prepočítavam históriu simulácie…</b> beží {_cw_run_s} s."
                    f"{_bar}"
                    f"<span style='color:#666;font-size:13px'>Prvý beh po resete / zmene "
                    f"nastavení simuluje celú históriu minútu po minúte. Stránka sa obnovuje "
                    f"automaticky každých 5 s; výpočet beží na pozadí aj keď okno zavrieš.</span></div>"
                    f"</body></html>")
        except RuntimeError as _adv_err:
            # Typicky: žiadny deň v rozsahu nemá plán v plan_store → strict mode raise.
            # Namiesto Internal Server Error ukáž user-friendly stránku s odkazom na batch.
            import html as _html_e
            import datetime as _dt_e
            _t_iso_e = _dt_e.date.today().isoformat()
            _step_now = int(_st)
            _kind_now = "dentrh" if _step_now == 15 else "plan"
            _batch_url = (f"/plan_batch?from_date={start or _t_iso_e}"
                          f"&to_date={_t_iso_e}&step_min={_step_now}&kind={_kind_now}")
            _err_msg = _html_e.escape(str(_adv_err)[:600])
            _page = (
                f"<!doctype html><html lang='sk'><head><meta charset='utf-8'>"
                f"<title>Chýba plán</title></head><body style='font-family:-apple-system,"
                f"Segoe UI,Arial;max-width:900px;margin:30px auto;padding:0 20px'>"
                f"{_nav('/livesim')}"
                f"<h1 style='color:#1F4E78'>🟢 Živá simulácia</h1>"
                f"<div style='background:#fff3cd;border-left:4px solid #856404;"
                f"border-radius:8px;padding:14px 18px;margin:14px 0'>"
                f"<b style='font-size:16px'>⚠ Plán pre tento deň ešte neexistuje</b><br>"
                f"<span style='color:#666;font-size:13px'>Livesim STRICT mode nevedel pokračovať "
                f"lebo v plan_store chýba D-1 plán pre aspoň jeden deň v rozsahu. "
                f"Vygeneruj plány a skús znova.</span>"
                f"<pre style='background:#f8f9fa;padding:10px;border-radius:4px;"
                f"font-size:11px;color:#666;margin-top:10px;overflow:auto'>{_err_msg}</pre>"
                f"</div>"
                f"<div style='display:flex;gap:10px;margin:14px 0'>"
                f"<a href='{_batch_url}' style='background:#1F4E78;color:#fff;padding:10px 18px;"
                f"border-radius:6px;text-decoration:none;font-weight:600'>"
                f"🔧 Vygenerovať plány (batch)</a>"
                f"<a href='/dentrh' style='background:#666;color:#fff;padding:10px 18px;"
                f"border-radius:6px;text-decoration:none'>📅 /dentrh (manuálne)</a>"
                f"<a href='/livesim' style='background:#28a745;color:#fff;padding:10px 18px;"
                f"border-radius:6px;text-decoration:none'>← Skúsiť znova</a>"
                f"</div></body></html>"
            )
            return HTMLResponse(_page)
        days = lsim.available_days(case, port=_PORT)
        # Rozšíriť dropdown o dni so saved plánmi pre aktívny profil (minulé aj budúce),
        # aby sa dali pozrieť aj dni, na ktoré livesim ešte nedobehol. plan_store iteruje
        # iba aktívny profil/market, takže nevidíš plány z iného profilu.
        # Step_min sa odvodí z plan_kind (60 pre 'plan', 15 pre 'dentrh').
        try:
            if ps is not None:
                _live_step = 15 if plan_kind == "dentrh" else 60
                _plan_items = ps.list_plans(kind=plan_kind) or []
                _plan_days = set()
                for _pi in _plan_items:
                    if int(_pi.get("step_min", 60)) != _live_step:
                        continue
                    try:
                        _plan_days.add(dt.date.fromisoformat(_pi["date"]))
                    except Exception:
                        pass
                if _plan_days:
                    days = sorted(set(days) | _plan_days)
        except Exception as _e_pl:
            print(f"[/livesim] list_plans pre dropdown zlyhal: {_e_pl}")
        prov = r.get("prov_date")
        if prov:
            pdate = dt.date.fromisoformat(prov)
            if pdate not in days:
                days = days + [pdate]
                days = sorted(set(days))
        view_day = view or (prov if prov else (days[-1].isoformat() if days else None))
        import time as _t_rt
        _t_render_ls = _t_rt.perf_counter()
        dfull = lsim.load_series(case, port=_PORT)
        # `trace_full` drží plnú minútovú resolution (predtým decimovanú do dview),
        # aby denné agregáty (FTV výroba, Zisk za deň) neboli podhodnotené 2-3×.
        trace_full = None
        if prov and view_day == prov and r.get("today_trace") is not None and len(r["today_trace"]):
            tdf = r["today_trace"]
            trace_full = tdf   # pred decimáciou — 1440 min alebo do current minute
            if len(tdf) > 600:
                step = max(1, len(tdf)//600)
                dec = tdf.iloc[::step]
                # vždy zahrň poslednú ŽIVÚ minútu, aby SOC v grafe sedel s bunkou „SOC teraz"
                if "is_live" in tdf.columns:
                    liv = tdf[tdf["is_live"] == 1]
                    if len(liv) and liv.index[-1] not in dec.index:
                        dec = pd.concat([dec, liv.iloc[[-1]]]).sort_index()
                tdf = dec
            dview = tdf
        else:
            dview = lsim.load_series(case, port=_PORT, day=view_day, max_points=2000) if view_day else dfull
        if os.environ.get("LIVESIM_TIMING") == "1":
            print(f"[RENDER-TIMING] {case} load_series = {(_t_rt.perf_counter()-_t_render_ls)*1000:.0f}ms "
                  f"dfull_rows={len(dfull) if dfull is not None else 0}")
        # Bug X (2026-06-07): VŽDY re-aplikuj VDT agregát ako single source of truth.
        # CSV stĺpce plan_batt_vdt_kw/plan_batt_dam_kw môžu byť outdated alebo NaN pre
        # staré minúty pred Bug V deploy. Plus VDT trades sa môžu pridať / zmeniť po
        # zapísaní livesim minúty. vdt_paper_trades.csv je single source — vždy ho
        # použi pri renderingu. ŽIADNY LP, žiadne prepisovanie livesim CSV.
        _vdt_diag = {"applied": 0, "kwh_total": 0.0, "trades": 0, "profile": ""}
        try:
            if dview is not None and not dview.empty and view_day:
                import vdt_state as _vs_load
                _prof_load = None
                try:
                    from core.profile_resolver import get_active as _ga_load
                    _prof_load = _ga_load()
                except Exception:
                    pass
                if _prof_load:
                    _vdt_diag["profile"] = _prof_load
                    _vdt_kw_arr = _vs_load.get_realized_batt_kw(
                        _prof_load, today_iso=view_day, dt_h=0.25)
                    _vdt_st_load = _vs_load._load_vdt_realized(_prof_load, view_day)
                    _vdt_kwh_arr_load = (_vdt_st_load or {}).get("kwh_batt_view") or [0.0]*96
                    _vdt_diag["trades"] = int((_vdt_st_load or {}).get("count", 0))
                    _vdt_diag["kwh_total"] = sum(abs(k) for k in _vdt_kwh_arr_load)
                    if isinstance(_vdt_kw_arr, list) and len(_vdt_kw_arr) >= 96 and any(_vdt_kw_arr):
                        def _slot15_from_ts(t):
                            try:
                                _ts = pd.Timestamp(t)
                                return min(95, max(0, (_ts.hour * 60 + _ts.minute) // 15))
                            except Exception:
                                return 0
                        _times = (dview["time"] if "time" in dview.columns
                                  else dview.index)
                        _pidx15_load = [_slot15_from_ts(t) for t in _times]
                        _vdt_kw_per = [float(_vdt_kw_arr[j] or 0.0) for j in _pidx15_load]
                        _vdt_kwh_per = [float(_vdt_kwh_arr_load[j] or 0.0) for j in _pidx15_load]
                        dview = dview.copy()
                        # Baseline D-1: ak `plan_batt_dam_kw` existuje (Bug V advance), použi
                        # ten; inak (staré CSV) D-1 = plan_batt_kw (ktorý sám neobsahuje VDT).
                        if "plan_batt_dam_kw" in dview.columns:
                            _dam_baseline_kw = dview["plan_batt_dam_kw"].fillna(
                                dview["plan_batt_kw"])
                        else:
                            _dam_baseline_kw = dview["plan_batt_kw"]
                        dview["plan_batt_dam_kw"] = _dam_baseline_kw
                        dview["plan_batt_vdt_kw"] = _vdt_kw_per
                        dview["plan_batt_kw"] = (dview["plan_batt_dam_kw"].fillna(0.0)
                                                  + dview["plan_batt_vdt_kw"].fillna(0.0))
                        # Bug VDT-CLIP-RENDER (2026-06-11): engine clipuje D-1+VDT na
                        # ±batt_kw (#625-A) — render musí tiež, inak graf ukazuje
                        # nemožné výkony (dam 5935 + vdt 2707 = 8642 kW > 6000 limit).
                        try:
                            import profiles as _pr_clip
                            _bkw_clip = float((((_pr_clip.load_profile(_prof_load) or {})
                                                .get("plan") or {}).get("batt_kw", 0.0)) or 0.0)
                            if _bkw_clip > 0:
                                dview["plan_batt_kw"] = dview["plan_batt_kw"].clip(
                                    -_bkw_clip, _bkw_clip)
                        except Exception as _e_clip:
                            print(f"[VDT-CLIP-RENDER] zlyhal: {_e_clip}")
                        # Grid: rovnaký approach
                        if "plan_grid_kwh" in dview.columns:
                            if "plan_grid_dam_kwh" in dview.columns:
                                _dam_baseline_g = dview["plan_grid_dam_kwh"].fillna(
                                    dview["plan_grid_kwh"])
                            else:
                                _dam_baseline_g = dview["plan_grid_kwh"]
                            dview["plan_grid_dam_kwh"] = _dam_baseline_g
                            dview["plan_grid_vdt_kwh"] = _vdt_kwh_per
                            dview["plan_grid_kwh"] = (dview["plan_grid_dam_kwh"].fillna(0.0)
                                                       + dview["plan_grid_vdt_kwh"].fillna(0.0))
                        _vdt_diag["applied"] = int(sum(1 for v in _vdt_kw_per if abs(v) > 0.01))

                        # Bug BATT-REAL-RECOMPUTE (2026-06-10): livesim CSV mohol zapísať
                        # batt_kw_realistic PRED tým, ako sa VDT trade dopísal do paper trades
                        # (= za behu sa neaktualizuje, ostáva stale). Plus pre future minúty
                        # batt_kw_realistic vôbec neexistuje. Po Bug Z VDT prepise plan_batt_kw
                        # prepočítaj batt_kw_realistic z aktuálneho plánu + grid+FTV clip
                        # (identická logika ako livesim.py riadky ~1062-1086).
                        try:
                            import profiles as _pr_br
                            _p_br = _pr_br.load_profile(_prof_load) or {}
                            _pl_br = (_p_br.get("plan") or {})
                            _gki = float(_pl_br.get("grid_kw_import", 0.0) or 0.0)
                            _gke = float(_pl_br.get("grid_kw_export", 0.0) or 0.0)
                            _bkw_max_br = float(_pl_br.get("batt_kw", 0.0) or 0.0)
                            _ftv_arr_br = pd.to_numeric(
                                dview.get("ftv_min_real_kw", pd.Series([0.0]*len(dview))),
                                errors="coerce").fillna(0.0).values
                            _load_arr_br = pd.to_numeric(
                                dview.get("load_min_real_kw", pd.Series([0.0]*len(dview))),
                                errors="coerce").fillna(0.0).values
                            _pb_arr_br = pd.to_numeric(
                                dview["plan_batt_kw"], errors="coerce").fillna(0.0).values
                            _rd_arr_br = pd.to_numeric(
                                dview.get("rt_dir", pd.Series([0.0]*len(dview))),
                                errors="coerce").fillna(0.0).values
                            _rp_arr_br = pd.to_numeric(
                                dview.get("rt_power_pct", pd.Series([0.0]*len(dview))),
                                errors="coerce").fillna(0.0).values
                            _bp_br = _pb_arr_br + _rd_arr_br * _rp_arr_br / 100.0 * _bkw_max_br
                            if _bkw_max_br > 0:
                                _bp_br = np.clip(_bp_br, -_bkw_max_br, _bkw_max_br)
                            _plan_chg_br = np.maximum(-_bp_br, 0.0)
                            _plan_dis_br = np.maximum(_bp_br, 0.0)
                            _avail_chg_br = np.maximum(_ftv_arr_br - _load_arr_br, 0.0) + _gki
                            _avail_dis_br = _gke + np.maximum(_load_arr_br - _ftv_arr_br, 0.0)
                            _chg_r_br = np.minimum(_plan_chg_br, _avail_chg_br)
                            _dis_r_br = np.minimum(_plan_dis_br, _avail_dis_br)
                            dview["batt_kw_realistic"] = np.round(_dis_r_br - _chg_r_br, 1)
                            _vdt_diag["batt_real_recomputed"] = int(len(_pb_arr_br))
                            _vdt_diag["gki"] = _gki
                            _vdt_diag["gke"] = _gke
                        except Exception as _e_br:
                            print(f"[BATT-REAL-RECOMPUTE] zlyhal: {_e_br}")

                        # Bug Z: SOC trajektória musí reflektovať plan_batt_kw (D-1+VDT).
                        # Predtým: soc_pct z _run_physical_day = D-1 only → SOC nereaguje na VDT.
                        # Recompute: integruj plan_batt_kw cez čas (1-min step) + start SOC z prvého
                        # neprázdneho riadku + batt capacity/eff z profilu.
                        try:
                            import profiles as _pr_soc
                            _p_soc = _pr_soc.load_profile(_prof_load) or {}
                            _pl_soc = (_p_soc.get("plan") or {})
                            _batt_kwh_cap = float(_pl_soc.get("batt_kwh", 800.0))
                            _eff_c = float(_pl_soc.get("eff_c", 0.95))
                            _eff_d = float(_pl_soc.get("eff_d", 0.95))
                            _soc_min_p = float(_pl_soc.get("soc_min", 5.0))
                            _soc_max_p = float(_pl_soc.get("soc_max", 100.0))
                            # Bug AA: start SOC z vdt_state.compute_current_state.start_soc_pct
                            # (single source of truth — rovnaké ako na /vdt/live_advisor). Predtým
                            # sa bral first valid soc_pct v dview = z _run_physical_day = D-1 only
                            # start. Tým vznikala nezhoda: VDT page 16%, livesim PREDIKCIA 22%.
                            _start_soc = None
                            _start_soc_src = "?"
                            # Bug SOC-DAY-START (2026-06-11): compute_current_state vracia SOC
                            # na začiatku AKTUÁLNEHO dňa — pre minulé dni je to nezmysel
                            # (integrácia dňa N od štartu dneška → falošný koniec dňa, graf
                            # ukazoval 06-10 koniec 57 % hoci CSV = 16 %). Použi ho IBA keď
                            # zobrazený deň == aktuálny deň simulácie (prov); pre minulé dni
                            # štart = prvý soc_pct z CSV trajektórie dňa (autoritatívny zdroj).
                            _is_current_day = bool(prov and str(view_day) == str(prov))
                            if _is_current_day:
                                try:
                                    _cs = _vs_load.compute_current_state(_prof_load)
                                    _start_soc = float(_cs.get("start_soc_pct"))
                                    _start_soc_src = str(_cs.get("start_soc_source", "vdt_state"))
                                except Exception:
                                    pass
                            if _start_soc is None:
                                if "soc_pct" in dview.columns:
                                    _first_valid = dview["soc_pct"].dropna()
                                    if not _first_valid.empty:
                                        _start_soc = float(_first_valid.iloc[0])
                                        _start_soc_src = "dview.soc_pct first"
                            if _start_soc is None:
                                _start_soc = float(_pl_soc.get("soc_init", 50.0))
                                _start_soc_src = "profile.soc_init"
                            _vdt_diag["start_soc"] = round(_start_soc, 1)
                            _vdt_diag["start_soc_src"] = _start_soc_src
                            # Bug START-SOC-CASE diag: zdroj štartu SOC do logu — pri zlej
                            # hodnote okamžite vidno či šiel default/zlý case/zlý fallback.
                            print(f"[SOC-START {view_day}] start={_start_soc:.1f}% "
                                  f"src={_start_soc_src}")
                            # Integrate: pre každú minútu Δ_kwh = plan_batt_kw / 60 (kW × 1/60 h)
                            # Sign: + discharge → SOC klesá; − charge → SOC stúpa
                            # Bug MM (2026-06-08): scale-down planu pri SOC floor/ceiling.
                            # Predtym sme len ostrihovali SOC ale plan_batt_kw zostal high
                            # → tabulka ukazovala +5600 kW pri SOC=5% (nemozne). Teraz pri
                            # narazani na floor (discharge) alebo ceiling (charge) zredukujeme
                            # planovanu hodnotu pre tu minutu na to, co bolo realne mozne.
                            _socs = []
                            _pb_realiz = []   # actual executable batt kW per minute
                            _soc_min_kwh = _soc_min_p / 100.0 * _batt_kwh_cap
                            _soc_max_kwh = _soc_max_p / 100.0 * _batt_kwh_cap
                            _soc_cur_kwh = max(_soc_min_kwh,
                                                min(_soc_max_kwh,
                                                    _start_soc / 100.0 * _batt_kwh_cap))
                            # Bug SOC-REALISTIC-SOURCE (2026-06-10): SOC musí integrovať
                            # reálny výkon na batérii — `batt_kw_realistic` (= D-1 plán +
                            # VDT + RT, post grid+FTV clip). Predtým integroval `plan_batt_kw`
                            # (= D-1 + VDT BEZ RT) → graf nereagoval na RT zásahy.
                            # Užívateľ: "spocita aktualny vykon na baterii (nezalezi ako vznikol)".
                            _src_col = "plan_batt_kw"
                            if "batt_kw_realistic" in dview.columns:
                                _br_check = pd.to_numeric(
                                    dview["batt_kw_realistic"], errors="coerce").fillna(0)
                                if _br_check.abs().sum() > 0.1:
                                    _src_col = "batt_kw_realistic"
                            _vdt_diag["soc_src_col"] = _src_col
                            # Bug FUTURE-PRED (2026-06-16, user VW_simulacia_3): pre BUDÚCE
                            # minúty batt_kw_realistic ešte neexistuje (NaN). Aby SOC predikcia
                            # integrovala PLÁNOVÉ vybíjanie (nie ploché 0), doplníme budúce
                            # NaN plánom (plan_batt_kw). Minulosť ostáva z realized.
                            if _src_col == "batt_kw_realistic" and "plan_batt_kw" in dview.columns:
                                _rs_fp = pd.to_numeric(dview["batt_kw_realistic"], errors="coerce")
                                _ps_fp = pd.to_numeric(dview["plan_batt_kw"], errors="coerce")
                                _pb_arr = _rs_fp.where(_rs_fp.notna(), _ps_fp).fillna(0.0).tolist()
                            else:
                                _pb_arr = dview[_src_col].fillna(0.0).tolist()
                            for _pb in _pb_arr:
                                _dkwh_req = float(_pb) / 60.0   # kW × 1/60 h (signed)
                                if _dkwh_req > 0:
                                    # discharge: SOC ↓; kontrola floor
                                    _draw_kwh = _dkwh_req / max(0.01, _eff_d)
                                    _avail_kwh = max(0.0, _soc_cur_kwh - _soc_min_kwh)
                                    _draw_act = min(_draw_kwh, _avail_kwh)
                                    _soc_cur_kwh -= _draw_act
                                    _pb_real = (_draw_act * _eff_d) * 60.0   # späť na kW
                                elif _dkwh_req < 0:
                                    # charge: SOC ↑; kontrola ceiling
                                    _push_kwh = abs(_dkwh_req) * _eff_c
                                    _room_kwh = max(0.0, _soc_max_kwh - _soc_cur_kwh)
                                    _push_act = min(_push_kwh, _room_kwh)
                                    _soc_cur_kwh += _push_act
                                    _pb_real = -(_push_act / max(0.01, _eff_c)) * 60.0
                                else:
                                    _pb_real = 0.0
                                _soc_pct = (_soc_cur_kwh / max(1.0, _batt_kwh_cap)) * 100.0
                                _soc_pct = max(_soc_min_p, min(_soc_max_p, _soc_pct))
                                _socs.append(_soc_pct)
                                _pb_realiz.append(_pb_real)
                            # Bug SOC-UNIFY (2026-06-13): engine livesim je JEDINÝ zdroj
                            # reálneho soc_pct — render ho už NEPREPOČÍTAVA, len číta engine
                            # trace. (Render integrácia _socs divergovala od engine: chýbal
                            # RT audit clip + engine sekvencia → skok na hranici dní.) Overené:
                            # engine CSV soc_pct má 0 NaN naprieč všetkými traceami, takže
                            # bývalý fill bol no-op. Prípadné medzery doplníme engine vlastnými
                            # susedmi (ffill/bfill), NIE paralelnou integráciou. _socs zostáva
                            # nižšie len ako power-clip (_pb_realiz, Bug MM), nie pre soc_pct.
                            _soc_eng = (pd.to_numeric(dview["soc_pct"], errors="coerce")
                                        if "soc_pct" in dview.columns else None)
                            if _soc_eng is None or _soc_eng.isna().all():
                                # engine soc_pct úplne chýba (legacy CSV) → posledná záchrana
                                dview["soc_pct"] = _socs
                                _vdt_diag["soc_source"] = "render-fallback (engine soc chýba)"
                            else:
                                # Bug SOC-REALITY-ANCHOR (2026-06-16, user VW_simulacia_3):
                                # MINULOSŤ = effect_minute (bg worker = stabilná REALITA, nie
                                # flaky load_series čo skákal 5↔100). BUDÚCNOSŤ = integruj PLÁN
                                # dopredu od POSLEDNÉHO REÁLNEHO SOC (effect_minute), NIE z _socs
                                # (to integrovalo flaky load_series → batéria "nenabitá" →
                                # predikcia padla na 4 %). Tak SOC predikcia naozaj zobrazuje
                                # realitu (teraz 100 %) a večer klesá podľa plánu 100→5 %.
                                import numpy as _np_soc
                                _se_vals = pd.to_numeric(_soc_eng, errors="coerce").values.astype(float)
                                _so_vals = _np_soc.asarray(_socs, dtype=float)
                                try:
                                    import core.effect_db as _edb_soc
                                    _em_soc = _edb_soc.get_minute_series(_prof_load, str(view_day))
                                    if (_em_soc is not None and not _em_soc.empty
                                            and "soc_pct" in _em_soc.columns and len(_se_vals) == len(dview)):
                                        _emk = dict(zip(
                                            pd.to_datetime(_em_soc["time"]).dt.strftime("%Y-%m-%d %H:%M"),
                                            pd.to_numeric(_em_soc["soc_pct"], errors="coerce")))
                                        _dvk = pd.to_datetime(dview["time"]).dt.strftime("%Y-%m-%d %H:%M")
                                        _em_al = _dvk.map(_emk).astype(float).values
                                        _has_em = ~_np_soc.isnan(_em_al)
                                        # ČASOVÁ HRANICA "TERAZ": effect_minute drží aj STALE
                                        # projekciu budúcnosti (starý beh) → NEdelíme existenciou
                                        # dát (_has_em = celý deň), ale ČASOM. Minulosť (≤teraz) =
                                        # realita z DB; budúcnosť (>teraz) = plán dopredu od SOC teraz.
                                        _tmin_s = (pd.to_datetime(dview["time"]).dt.hour * 60
                                                   + pd.to_datetime(dview["time"]).dt.minute).values
                                        if _is_current_day:
                                            _now_min_s = dt.datetime.now().hour * 60 + dt.datetime.now().minute
                                        else:
                                            _now_min_s = 24 * 60 + 1   # historický deň → celý deň = minulosť (realita)
                                        _past_m = (_tmin_s <= _now_min_s)
                                        # 1) MINULOSŤ (≤teraz) = realita z effect_minute (kde existuje)
                                        _pe = _past_m & _has_em
                                        _se_vals[_pe] = _em_al[_pe]
                                        # 2) BUDÚCNOSŤ (>teraz) = integruj PLÁN dopredu od SOC TERAZ
                                        _plan_kw_arr = (pd.to_numeric(dview["plan_batt_kw"], errors="coerce")
                                                        .fillna(0.0).values
                                                        if "plan_batt_kw" in dview.columns
                                                        else _np_soc.zeros(len(dview)))
                                        _idx_past = _np_soc.where(_past_m)[0]
                                        if _is_current_day and len(_idx_past) > 0:
                                            _cap = max(1.0, float(_batt_kwh_cap))
                                            _lo = _soc_min_p / 100.0 * _cap
                                            _hi = _soc_max_p / 100.0 * _cap
                                            try:
                                                _tt_soc = pd.to_datetime(dview["time"])
                                                _step_h = float((_tt_soc.iloc[1] - _tt_soc.iloc[0]).total_seconds()) / 3600.0
                                                if not (_step_h > 0):
                                                    _step_h = 1.0 / 60.0
                                            except Exception:
                                                _step_h = 1.0 / 60.0
                                            _li = int(_idx_past[-1])
                                            # anchor = SOC TERAZ (realita); ak NaN, posledná platná dozadu
                                            _anchor = _se_vals[_li]
                                            if _np_soc.isnan(_anchor):
                                                _vv = _se_vals[:_li + 1][~_np_soc.isnan(_se_vals[:_li + 1])]
                                                _anchor = float(_vv[-1]) if len(_vv) else float(_so_vals[_li] if not _np_soc.isnan(_so_vals[_li]) else 50.0)
                                            _cur = float(_anchor) / 100.0 * _cap
                                            for _j in range(_li + 1, len(_se_vals)):
                                                _dk = float(_plan_kw_arr[_j]) * _step_h   # kWh za krok (+vybíja −nabíja)
                                                if _dk > 0:
                                                    _cur -= _dk / max(0.01, _eff_d)
                                                elif _dk < 0:
                                                    _cur += (-_dk) * _eff_c
                                                _cur = max(_lo, min(_hi, _cur))
                                                _se_vals[_j] = _cur / _cap * 100.0   # OVERRIDE stale DB projekcie
                                except Exception as _e_em_soc:
                                    print(f"[SOC-REALITY-ANCHOR] {_e_em_soc}")
                                # zvyšné NaN (medzery) doplň _socs / ffill
                                _nan_m = _np_soc.isnan(_se_vals)
                                if _nan_m.any() and len(_so_vals) == len(_se_vals):
                                    _se_vals[_nan_m] = _so_vals[_nan_m]
                                _soc_eng = pd.Series(_se_vals, index=dview.index).ffill().bfill()
                                dview["soc_pct"] = _soc_eng
                                _vdt_diag["soc_source"] = "effect_minute(realita)+plan-forward(buducnost)"
                            # Bug MM: prepiseme plan_batt_kw na to, co bolo realne mozne
                            # vykonatelne dane SOC limits — zhoda batt_kW <-> SOC pohybu.
                            # SOC-REALISTIC-SOURCE: prepisuj IBA keď zdroj bol plan_batt_kw.
                            # Ak sme integrovali batt_kw_realistic, ten je už post-cap reality;
                            # prepísanie plan_batt_kw by zamiešalo plán a realitu.
                            if _src_col == "plan_batt_kw":
                                dview["plan_batt_kw"] = _pb_realiz

                            # Bug SOC-PLAN-PARALLEL (2026-06-10): paralelný SOC z plan_batt_kw
                            # (= D-1 nominácia + VDT realized + plánované VDT). Užívateľ chce
                            # vidieť aj full-day predikciu (vrátane budúcich zobchodovaných slotov),
                            # nielen post-cap realitu. Reálne `soc_pct` ostáva z batt_kw_realistic.
                            try:
                                _soc_cur_plan = max(_soc_min_kwh,
                                                    min(_soc_max_kwh,
                                                        _start_soc / 100.0 * _batt_kwh_cap))
                                _socs_plan = []
                                _pb_plan_arr = dview["plan_batt_kw"].fillna(0.0).tolist()
                                for _pbp in _pb_plan_arr:
                                    _dkwh = float(_pbp) / 60.0
                                    if _dkwh > 0:
                                        _draw = _dkwh / max(0.01, _eff_d)
                                        _avail = max(0.0, _soc_cur_plan - _soc_min_kwh)
                                        _soc_cur_plan -= min(_draw, _avail)
                                    elif _dkwh < 0:
                                        _push = abs(_dkwh) * _eff_c
                                        _room = max(0.0, _soc_max_kwh - _soc_cur_plan)
                                        _soc_cur_plan += min(_push, _room)
                                    _sp = (_soc_cur_plan / max(1.0, _batt_kwh_cap)) * 100.0
                                    _sp = max(_soc_min_p, min(_soc_max_p, _sp))
                                    _socs_plan.append(_sp)
                                dview["soc_pct_plan"] = _socs_plan
                            except Exception:
                                pass
                            _vdt_diag["soc_recomputed"] = len(_socs)
                            _vdt_diag["plan_clipped"] = int(sum(
                                1 for a, b in zip(_pb_arr, _pb_realiz)
                                if abs(float(a) - float(b)) > 1.0))
                        except Exception as _e_soc:
                            try:
                                print(f"[Bug Z SOC recompute] zlyhalo: {_e_soc}")
                            except Exception:
                                pass
        except Exception as _ex_vdt:
            try:
                print(f"[Bug X retro fix] zlyhalo: {_ex_vdt}")
            except Exception:
                pass
        # Banner ak vybraný deň má saved plán ale livesim CSV pre neho nemá záznamy
        # (typicky: budúci deň, alebo minulý deň pre ktorý sa livesim neobehol).
        plan_only_warn = ""
        try:
            if view_day and (dview is None or dview.empty):
                _has_plan = ps.has_plan(view_day, _st, plan_kind) if ps is not None else False
                _is_future = dt.date.fromisoformat(view_day) > dt.date.today()
                if _has_plan:
                    _label = "budúci" if _is_future else "minulý"
                    plan_only_warn = (
                        f"<div style='background:#fff3cd;border-left:4px solid #f0b80f;border-radius:6px;"
                        f"padding:10px 14px;margin:10px 0;font-size:13px;color:#7a5c00'>"
                        f"📅 <b>Pre {_label} deň {view_day} existuje uložený plán</b>, ale živá simulácia "
                        f"pre tento deň ešte nemá žiadne minútové záznamy. Grafy preto nezobrazia reálne "
                        f"hodnoty (FTV, batéria, SOC, RT) — uvidíš len holý plán.<br>"
                        f"<b>Pre minulé dni:</b> klikni <b>Spustiť/Obnoviť simuláciu</b> nižšie (livesim "
                        f"dobehne história od najstaršej minúty). <b>Pre budúce dni:</b> reálne dáta "
                        f"pribudnú postupne ako deň prebehne.</div>")
                elif _is_future:
                    plan_only_warn = (
                        f"<div style='background:#eef3fb;border-left:4px solid #1F4E78;border-radius:6px;"
                        f"padding:10px 14px;margin:10px 0;font-size:13px;color:#33506e'>"
                        f"📆 <b>Vybraný deň {view_day} je v budúcnosti</b> a ešte nemá vygenerovaný plán "
                        f"ani simuláciu. <a href='/' style='color:#1F4E78;font-weight:600'>/plan</a> alebo "
                        f"<a href='/plan_batch' style='color:#1F4E78;font-weight:600'>batch generátor</a> "
                        f"pre vytvorenie plánu.</div>")
        except Exception:
            pass
        # ── all-zero plán detekcia: ak má aktuálne zobrazený deň rt_mask aj mults samé 0,
        #     RT engine sa nikdy nestrelí a batéria nereaguje. Tipická chyba: rt_freedom=False
        #     + mults=0 v šablóne. Banner ponúkne 1-klik re-generáciu s rt_freedom=on.
        zero_plan_warn = ""
        try:
            if ps is not None and view_day:
                _pdat = ps.load_plan_safe(view_day, _st, plan_kind)
                if _pdat:
                    _mlts = _pdat.get("mults") or []
                    _rtmm = _pdat.get("rt_mask") or []
                    _mlt_zero = bool(_mlts) and all(abs(float(x)) < 1e-6 for x in _mlts)
                    _rtm_zero = bool(_rtmm) and all(abs(float(x)) < 1e-6 for x in _rtmm)
                    if _mlt_zero and _rtm_zero:
                        zero_plan_warn = (
                            f"<div style='background:#ffe8e0;border-left:4px solid #C0392B;border-radius:6px;"
                            f"padding:10px 14px;margin:10px 0;font-size:13px;color:#7a1810'>"
                            f"⚠ <b>Plán pre {view_day} má × aj RT mask všade 0</b> — batéria <b>vôbec nereaguje</b> "
                            f"(ani na ČEPS signál). Toto je typicky výsledok kombinácie "
                            f"<code>mults=0</code> všade + <code>rt_freedom=False</code> v profile.<br>"
                            f"<b>Náprava:</b> otvor <a href='/' style='color:#1F4E78;font-weight:600'>/plan</a>, "
                            f"zaškrtni „RT slobodná aj mimo D-1 slotov\" a klikni <b>Generuj plán D-1</b>. "
                            f"Vygeneruje sa plán s RT mask = 1 aj keď × ostane 0 — batéria bude bežať len na RT odchýlke.</div>")
        except Exception:
            pass
        # ── Realio overlay: nahradiť simulované hodnoty reálnymi meraniami ────
        realio_banner = ""
        _overlay_stats = {"applied_dfull": 0, "applied_dview": 0, "applied_trace": 0,
                           "rdf_rows": 0, "rdf_columns": []}
        if realio_on:
            try:
                import realio as _rio
                rdf = _rio.read_recent(n_minutes=1440 * 2)  # 2 dni
                if not rdf.empty:
                    # Vytvor mapping čas → realio merania (ftv, load, batt, soc)
                    rdf = rdf.copy()
                    rdf["time"] = pd.to_datetime(rdf["time"], errors="coerce")
                    rdf = rdf.dropna(subset=["time"])
                    # Normalizuj na minútu pre join (realio CSV je 1-min)
                    rdf["_min"] = rdf["time"].dt.floor("min")
                    # **CRITICAL FIX**: per-stĺpec deduplikácia s "last non-null".
                    # Pôvodne sme robili drop_duplicates(subset=["_min"], keep="last")
                    # na celý DataFrame — to ale zachová JEDEN riadok per minúta a ten
                    # môže mať NaN v niektorom stĺpci (polling zapíše sub-minútový riadok
                    # iba s časťou tagov). Per-stĺpec deduplikácia: pre každý meraný stĺpec
                    # zoberieme posledný NON-NULL záznam v danej minúte → maximálne zachované dáta.
                    _data_cols = [c for c in rdf.columns if c not in ("time", "_min")]
                    _all_mins = sorted(rdf["_min"].dropna().unique())
                    rdf_clean = pd.DataFrame(index=pd.DatetimeIndex(_all_mins))
                    for _col in _data_cols:
                        _sub = rdf[["_min", _col]].dropna(subset=[_col])
                        if _sub.empty:
                            continue
                        _sub = _sub.drop_duplicates(subset=["_min"], keep="last").set_index("_min")[_col]
                        rdf_clean[_col] = _sub
                    rdf = rdf_clean.sort_index()
                    _overlay_stats["rdf_rows"] = len(rdf)
                    _overlay_stats["rdf_columns"] = [c for c in rdf.columns]

                    def _apply_overlay(df, label=""):
                        """Pridá realio CSV ako NOVÉ stĺpce realio_* v DataFrame.

                        NEPRIEPISUJE sim hodnoty — namiesto toho pridá `realio_ftv_kw`,
                        `realio_load_kw`, `realio_batt_kw`, `realio_soc_pct`. Tým grafy
                        a karty môžu ukázať **predikciu aj realitu paralelne** (dve
                        farebné čiary, dve karty), bez miešania.

                        **Nearest-neighbor matching:** pre každú minútu v df sa hľadá
                        najbližší realio bod v okolí ±5 min. Tak sa zaplnia diery keď
                        Bender history poslal nepravidelné buckety (každú 1-5 minút).
                        Mimo rozsahu realio dát ostáva NaN.
                        """
                        if df is None or len(df) == 0:
                            return df, 0
                        df = df.copy()
                        if "time" not in df.columns:
                            return df, 0
                        # Convert df['time'] na minute-floor pre nearest-neighbor join
                        try:
                            _t_idx = pd.to_datetime(df["time"], errors="coerce").dt.floor("min")
                        except Exception:
                            return df, 0
                        # Mapuj realio kanály → nové realio_* stĺpce
                        mapping = {
                            "realio_ftv_kw":      "ftv_power_kw",
                            "realio_load_kw":     "load_power_kw",       # ELM1 1-min = SIEŤ (vysoký šum)
                            "realio_load_kw_15m": "load_power_kw_15m",   # ELM1 15-min priemer (presný pre nomináciu)
                            "realio_batt_kw":     "batt_power_kw",
                            "realio_soc_pct":     "batt_soc_pct",
                        }
                        n_applied = 0
                        # Strict match: tolerancia **30 sekúnd** (= prakticky exact match
                        # na minútovú granularitu). Žiadny ffill, žiadne 30-min nearest
                        # rozprašovanie hodnôt do okolitých slotov.
                        # User pravidlo: minúta bez DB záznamu → NaN (chart prerušený).
                        # Nikdy nemiešať dáta z časovo vzdialeného bodu.
                        _tolerance = pd.Timedelta(seconds=30)
                        # Postav DatetimeIndex z _t_idx (zachovaj pozície aj pri NaT)
                        target_arr = pd.to_datetime(_t_idx.values, errors="coerce")
                        valid_mask_target = pd.notna(target_arr)
                        if not valid_mask_target.any():
                            return df, 0
                        valid_target_idx = pd.DatetimeIndex(target_arr[valid_mask_target])
                        # Cutoff: NIČ za dnešným now (live polling sa zastaví pri current minute).
                        # Future minúty musia ostať NaN — žiadny realio záznam tam nemôže byť.
                        _now_floor = pd.Timestamp.now().floor("min")
                        for new_col, real_col in mapping.items():
                            if real_col not in rdf.columns:
                                continue
                            # Per-stĺpec dropna pred reindex (drop NaN z DB)
                            col_series = rdf[real_col].dropna()
                            if col_series.empty:
                                continue
                            # Reindex s ostrou toleranciou — IBA exact match (do 30s).
                            try:
                                ser_valid = col_series.reindex(valid_target_idx,
                                                                method="nearest",
                                                                tolerance=_tolerance)
                            except Exception:
                                continue
                            # Rozkopírujeme valid hodnoty späť na pôvodné pozície v df
                            full_arr = pd.Series([float("nan")] * len(df), dtype="float64")
                            valid_positions = [i for i, v in enumerate(valid_mask_target) if v]
                            for pos, val, t_pos in zip(valid_positions, ser_valid.values,
                                                         valid_target_idx):
                                # Drop budúcich minút (po current minute) — žiadne realio dáta tam neexistujú
                                if t_pos > _now_floor:
                                    continue
                                full_arr.iloc[pos] = val
                            df[new_col] = full_arr.values
                            n_applied += int(pd.notna(full_arr).sum())
                        return df, n_applied

                    dfull, _overlay_stats["applied_dfull"] = _apply_overlay(dfull, "dfull")
                    dview, _overlay_stats["applied_dview"] = _apply_overlay(dview, "dview")
                    # Today trace pre 'Hodnoty teraz' karty — najnovší riadok berie odtiaľ
                    if isinstance(r, dict) and "today_trace" in r and r["today_trace"] is not None:
                        r["today_trace"], _overlay_stats["applied_trace"] = _apply_overlay(r["today_trace"], "trace")

                    realio_banner = (
                        "<div style='background:#fff3cd;border-left:6px solid #C62828;padding:10px 16px;"
                        "border-radius:10px;margin:10px 0;font-size:14px'>"
                        "<b style='color:#C62828'>🔴 REÁLNE RIADENIE</b> &nbsp;—&nbsp; "
                        f"<b>{_overlay_stats['rdf_rows']}</b> minútových realio záznamov · "
                        f"overlay aplikovaný: trace=<b>{_overlay_stats['applied_trace']}</b>, "
                        f"dview=<b>{_overlay_stats['applied_dview']}</b>, "
                        f"dfull=<b>{_overlay_stats['applied_dfull']}</b> hodnôt. "
                        "Plány (D-1 nominácie) zostávajú simulované — slúžia ako referencia pre RT controller. "
                        "Editor FTV scenára je skrytý.</div>")
                else:
                    realio_banner = (
                        "<div style='background:#fff3cd;border-left:6px solid #B45309;padding:10px 16px;"
                        "border-radius:10px;margin:10px 0;font-size:14px'>"
                        "<b>🔴 REÁLNE RIADENIE</b> — realio CSV je prázdne. Zatiaľ vidíš čisto simulované hodnoty. "
                        "Skontroluj <a href='/realio?tab=nastavenie' style='color:#B45309;font-weight:600'>"
                        "⚙ Nastavenie</a> a počkaj kým poll job pridá prvý záznam.</div>")
            except Exception as _ovl_err:
                realio_banner = (
                    f"<div style='background:#ffeaea;border-left:6px solid #C0392B;padding:10px;border-radius:8px;"
                    f"margin:10px 0;font-size:13px'><b>⚠ Realio overlay zlyhal:</b> {_ovl_err}</div>")
        body = _livesim_body(r, dfull, dview, view_day, days, realio_overlay=realio_on, trace_full=trace_full,
                              table_offset=table_offset, table_rows=table_rows)
        # Bug COMPUTE-WORKER: stale dáta (background prepočet beží) → banner + rýchlejší refresh
        stale_banner = ""
        _refresh_s = "60"
        # BG-PROGRESS-INLINE (2026-06-13, user: "doplniť progress bar do živej simulácie
        # — stránka sa zobrazí, ale nie sú všetky dni prepočítané"): ak na pozadí beží
        # backfill (compute worker), ukáž progress bar aj na vykreslenej stránke.
        _cw_live = _livesim_compute_status(case, _PORT, _profile_key)
        _running = bool(_cw_live.get("running"))
        # Bug COVERAGE-BANNER (2026-06-13, user: "chcem progres bar keď nie sú spočítané
        # všetky dni, nech viem že to čo vidím nie je definitívne"): okrem "worker beží"
        # ukáž banner aj keď DÁTA sú NEÚPLNÉ — pokrytie dní v logu < očakávaný rozsah
        # (start → dnes). Tým užívateľ vždy vidí, či je zobrazené finálne alebo čiastočné.
        _cov_done = _cov_total = 0
        try:
            _cov_start = pd.to_datetime(start).date() if start else (dt.date.today() - dt.timedelta(days=7))
            _cov_total = max(1, (dt.date.today() - _cov_start).days + 1)
            _days_set = set(d for d in (days or []) if _cov_start <= d <= dt.date.today())
            _cov_done = len(_days_set)
        except Exception:
            pass
        _incomplete = (_cov_total > 0 and _cov_done < _cov_total)
        _show_banner = (isinstance(r, dict) and r.get("_stale")) or _running or _incomplete
        if _show_banner:
            import time as _t_sb
            _sb_run = int(_t_sb.time() - float((r.get("_stale_since") if isinstance(r, dict) else None)
                                               or _cw_live.get("started") or _t_sb.time()))
            _pgl = _cw_live.get("progress") or {}
            # PROGRESS-LIVE (2026-06-15, user: "ked sa pocita nech ukaze realne % napr 20%, nie 100%"):
            # keď beží worker, ukáž JEHO reálny progres (done/total). Pôvodné `done or cov_done`
            # pri done=0 (čerstvý štart) spadlo na cov_done (15/15 = 100%) → bar ukázal 100% hoci
            # sa práve začalo počítať. Keď worker NEbeží, ukáž pokrytie dní (data-completeness).
            if _running and int(_pgl.get("total", 0)) > 0:
                _dl = int(_pgl.get("done", 0))
                _tl = max(1, int(_pgl.get("total", 0)))
            else:
                _dl = _cov_done
                _tl = _cov_total
            _barl = ""
            if _tl > 0:
                _pctl = max(0, min(100, int(_dl / _tl * 100)))
                _etal = ""
                if _running and _dl > 0 and _sb_run > 2:
                    _reml = int((_sb_run / max(_dl, 1)) * (_tl - _dl))
                    _etal = (f" · ostáva ~{_reml//60} min {_reml%60} s" if _reml >= 60
                             else f" · ostáva ~{_reml} s")
                _barl = (
                    f"<div style='background:#cfe0f0;border-radius:8px;height:18px;overflow:hidden;margin:8px 0 4px'>"
                    f"<div style='background:#1F4E78;height:100%;width:{_pctl}%;transition:width .4s;"
                    f"display:flex;align-items:center;justify-content:flex-end;padding-right:8px;"
                    f"color:#fff;font-size:11px;font-weight:600'>{_pctl}%</div></div>"
                    f"<div style='color:#666;font-size:12px'>spočítaných {_dl}/{_tl} dní"
                    f"{(' · práve ' + _pgl.get('day','')) if (_running and _pgl.get('day')) else ''}{_etal}</div>")
            if _running:
                _hdr = f"⏳ <b>Prepočet beží na pozadí</b> ({_sb_run} s)"
                _refresh_s = "10"
            else:
                _hdr = "⚠ <b>Zobrazené dáta NIE SÚ kompletné</b>"
                _refresh_s = "30"
            stale_banner = (
                "<div style='background:#fff3cd;border-left:5px solid #f0b80f;padding:10px 14px;"
                "border-radius:8px;margin:10px 0;font-size:14px'>"
                f"{_hdr} — niektoré dni v rozsahu ešte nie sú dopočítané, takže súčty (Zisk SPOLU) "
                f"nie sú definitívne. {'Klikni „Spustiť/Obnoviť“ pre dopočet.' if not _running else 'Stránka sa obnoví automaticky.'}{_barl}</div>")
        return (head.replace("</head>", f'<meta http-equiv="refresh" content="{_refresh_s}">' + "</head>")
                + form + plan_warn + plan_only_warn + zero_plan_warn + realio_banner + stale_banner + body + "</body></html>")
    except Exception as ex:
        import traceback
        tb = traceback.format_exc()
        return ("<!doctype html><html lang='sk'><head><meta charset='utf-8'>"
                "<title>Živá simulácia – chyba</title></head>"
                "<body style='font-family:-apple-system,Segoe UI,Arial;max-width:1680px;margin:24px auto;padding:0 16px'>"
                "<h2 style='color:#C00000'>Živá simulácia – chyba</h2>"
                f"<p><b>{type(ex).__name__}: {ex}</b></p>"
                "<pre style='white-space:pre-wrap;background:#fff5f5;border:1px solid #f3c2c2;border-radius:8px;"
                f"padding:10px;font-size:12px;overflow:auto'>{tb}</pre>"
                "<p><a href='/livesim'>← skús znova</a> &nbsp; <a href='/'>← domov</a></p></body></html>")


def _livesim_body(r, dfull, dview, view_day, days, realio_overlay: bool = False, trace_full=None,
                   table_offset: int = 0, table_rows: int = 20):
    import numpy as _np
    bkw = float(r.get("batt_kw", 100.0))
    n = 0 if dfull is None else len(dfull)
    rtnote = "" if r["use_rt"] else " <span style='color:#888'>(prípad bez RT – iba denný trh)</span>"
    # ── DIAGNOSTIC banner: stav RT regulácie pre zobrazený deň ──────────────
    _diag_html = ""
    if dview is not None and not dview.empty:
        _dv = dview.copy()
        _dv["hour"] = pd.to_datetime(_dv["time"]).dt.hour
        _n_total = len(_dv)
        _n_sys = int(_dv["sys_MW"].notna().sum()) if "sys_MW" in _dv.columns else 0
        _n_rt = int((_dv.get("rt_dir", pd.Series([0]*_n_total)).abs() > 0).sum())
        _mw_mean = float(_dv["sys_MW"].dropna().mean()) if "sys_MW" in _dv.columns else float('nan')
        _ok_sig = _n_sys > 0
        _ok_rt = _n_rt > 0
        # per-hour breakdown: signál priemer, RT aktivita, plan_batt
        per_h_rows = []
        for h in range(24):
            hh = _dv[_dv.hour == h]
            if len(hh) == 0:
                continue
            sys_mean = float(hh["sys_MW"].mean()) if "sys_MW" in hh.columns and hh["sys_MW"].notna().any() else float('nan')
            rt_act_min = int((hh.get("rt_dir", pd.Series([0]*len(hh))).abs() > 0).sum())
            rt_act_pct = rt_act_min / max(1, len(hh)) * 100
            avg_rt_pow = float((hh.get("rt_dir", 0) * hh.get("rt_power_pct", 0)).abs().mean()) if rt_act_min else 0
            plan_kw = float(hh["plan_batt_kw"].mean()) if "plan_batt_kw" in hh.columns else 0
            soc_pct = float(hh["soc_pct"].iloc[-1]) if "soc_pct" in hh.columns and len(hh) else 0
            mark = ""
            if abs(sys_mean) > 50 and rt_act_pct < 20:
                mark = " ⚠"  # silný signál ale málo RT akcie
            sig_str = f"{sys_mean:+.0f}" if sys_mean == sys_mean else "—"
            per_h_rows.append(f"<tr><td>{h:02d}</td><td style='text-align:right'>{sig_str}</td>"
                              f"<td style='text-align:right'>{plan_kw:+.0f}</td>"
                              f"<td style='text-align:right'>{rt_act_pct:.0f}%</td>"
                              f"<td style='text-align:right'>{avg_rt_pow:.0f}%</td>"
                              f"<td style='text-align:right'>{soc_pct:.0f}%</td>"
                              f"<td>{mark}</td></tr>")
        _per_h_table = ("<table style='width:100%;margin-top:6px;font-size:12px;border-collapse:collapse'>"
                        "<tr style='background:#f0f0f0'><th>hod</th><th>priem sys_MW</th>"
                        "<th>plán kW</th><th>RT aktívne %</th><th>priem RT %</th><th>SOC</th><th></th></tr>"
                        + "".join(per_h_rows) + "</table>")
        _bg = "#e8f5e9" if (_ok_sig and _ok_rt) else "#fff3cd"
        _bd = "#2E7D32" if (_ok_sig and _ok_rt) else "#f0b80f"
        _color = "#1B5E20" if (_ok_sig and _ok_rt) else "#7a5c00"
        if not _ok_sig:
            _state = "⚠ <b>Žiadny sys_MW signál</b> — _livesim_live_minutes vrátil None alebo nemá dáta."
        elif not _ok_rt:
            _state = (f"⚠ Signál prítomný ({_n_sys}/{_n_total} min, priemer sys_MW = {_mw_mean:+.0f} MW), "
                      f"ale RT NEVYDÁVA akcie. Možno: dev_budget vyčerpaný (max_cycles=3), SOC limit, alebo úzke pásma.")
        else:
            _state = (f"✓ RT aktívna: {_n_rt} z {_n_total} minút ({_n_rt/_n_total*100:.0f}%), priemer sys_MW = {_mw_mean:+.0f} MW. "
                      f"<b>⚠ riadky = silný signál (|sys_MW|>50) ale málo RT akcie</b> — vidíš stratený potenciál v evening!")
        _diag_html = (f"<details style='background:{_bg};border-left:4px solid {_bd};padding:8px 12px;margin:6px 0;"
                       f"font-size:13px;color:{_color};border-radius:4px'>"
                       f"<summary><b>🔍 RT diagnostika ({view_day}):</b> {_state}</summary>"
                       f"{_per_h_table}</details>")
    # --- hodnoty TERAZ (posledná ŽIVÁ minúta dneška, inak posledná minúta logu) ---
    _tt = r.get("today_trace")
    if _tt is not None and len(_tt):
        _liv = _tt[_tt.get("is_live", 1) == 1] if "is_live" in _tt.columns else _tt
        now = _liv.iloc[-1] if len(_liv) else _tt.iloc[-1]    # reálny aktuálny čas (nie projekcia konca dňa)
    elif dfull is not None and not dfull.empty:
        now = dfull.iloc[-1]
    else:
        now = None
    def _nz(x, d=0.0):
        try:
            return float(x)
        except Exception:
            return d
    def _js(x):
        """JS-bezpečné číslo: NaN/inf → 'null' (medzera v grafe), inak 1 desatinné."""
        try:
            v = float(x)
            return f"{v:.1f}" if _np.isfinite(v) else "null"
        except Exception:
            return "null"
    if now is not None:
        rt_kw_now = _nz(now.get("rt_dir"))*_nz(now.get("rt_power_pct"))/100.0*bkw
        sm = "VYBI" if _nz(now.get("rt_dir")) > 0 else ("NABI" if _nz(now.get("rt_dir")) < 0 else "DRŽ")
        # DT teraz = REÁLNA clearing cena (známa pre uplynulé hodiny); fallback na predikciu pre budúce hodiny
        _dtreal = now.get("dt_real_eur", None) if hasattr(now, "get") else None
        if _dtreal is not None and pd.notna(_dtreal) and abs(float(_dtreal)) > 1e-6:
            _dt_show, _dt_lbl = float(_dtreal), "DT teraz (realita)"
        else:
            _dt_show, _dt_lbl = _nz(now.get("dt_eur")), "DT teraz (predikcia)"

        # ── Realio reálne hodnoty pre karty ────────────────────────────────────
        # Single source of truth = realio.read_recent() najnovší riadok (rovnaký zdroj
        # ako Vizualizácia → konzistentné hodnoty). Ignorujeme trace overlay match
        # (problematický pri timestamp mismatch / floor("min") rozdieloch).
        _r_ftv = _r_batt = _r_soc = None
        _r_ts_str = "—"
        _r_age_min = None
        if realio_overlay:
            try:
                import realio as _rio
                # PRIMÁRNY ZDROJ: fetch_latest_all (priamy HTTP Bender request)
                # — rovnaký zdroj ako Live odpočet → konzistentné hodnoty.
                _live = _rio.fetch_latest_all()
                if _live and isinstance(_live, dict) and not _live.get("_error"):
                    v = _live.get("ftv_power_kw")
                    if v is not None: _r_ftv = float(v)
                    v = _live.get("batt_power_kw")
                    if v is not None: _r_batt = float(v)
                    v = _live.get("batt_soc_pct")
                    if v is not None: _r_soc = float(v)
                # FALLBACK: per-stĺpec last valid z CSV
                if _r_ftv is None or _r_batt is None or _r_soc is None:
                    _rdf_last = _rio.read_recent(n_minutes=120)
                    if _rdf_last is not None and not _rdf_last.empty:
                        def _lv(col):
                            if col not in _rdf_last.columns: return None
                            s = _rdf_last[col].dropna()
                            if s.empty: return None
                            try: return float(s.iloc[-1])
                            except (TypeError, ValueError): return None
                        if _r_ftv is None:  _r_ftv  = _lv("ftv_power_kw")
                        if _r_batt is None: _r_batt = _lv("batt_power_kw")
                        if _r_soc is None:  _r_soc  = _lv("batt_soc_pct")
                # Časová značka — z live (now) alebo z CSV
                _r_ts_str = pd.Timestamp.now().strftime("%H:%M:%S")
            except Exception:
                pass

        # FTV výkon — v realio_overlay móde použiť realio meranie, inak plán
        if realio_overlay:
            if _r_ftv is not None:
                _ftv_val = _r_ftv; _ftv_lbl = "FTV výkon (meranie)"; _ftv_color = "#C62828"
            else:
                _ftv_val = _nz(now.get('ftv_kw')); _ftv_lbl = "FTV výkon (plán — chýba meranie)"; _ftv_color = "#666"
        else:
            _ftv_val = _nz(now.get('ftv_kw')); _ftv_lbl = "FTV výkon"; _ftv_color = "#222"

        # SOC karta — v realio_overlay móde IBA reálne meranie (rovnaká hodnota ako
        # vo Vizualizácii). Predikčný SOC z trace by používateľa mátol, lebo môže
        # divergovať od reality (sim plán vs skutočný stav batérie).
        # Bug NN (2026-06-08): SINGLE SOURCE OF TRUTH pre SOC. Card SOC teraz musí
        # byť rovnaké ako chart SOC PREDIKCIA aj /vdt SOC teraz. Všetky tri pochádzajú
        # z vdt_state.compute_current_state.start_soc_pct integrovaného cez D-1 + VDT.
        # dview["soc_pct"] uz post Bug Z/AA/MM recompute = canonical source. Card
        # predtym pouzival now["soc_pct"] z raw today_trace (pred recompute) → desync.
        # Bug SOC-UNIFY (2026-06-14): karta SOC teraz = engine trace (dview). Primárne
        # posledná živá minúta dview soc_pct; ak chýba, fallback na engine
        # compute_current_state.current_soc_pct (= ten istý engine dnešok cez meta) —
        # NIE raw now['soc_pct'] (mohla byť projekcia/default → falošných 48 %).
        _soc_plan_val = None
        try:
            if (dview is not None and len(dview)
                and "soc_pct" in dview.columns and "time" in dview.columns):
                _now_ts = pd.Timestamp(now["time"])
                _past = dview[pd.to_datetime(dview["time"]) <= _now_ts]
                if not _past.empty:
                    _v = _past["soc_pct"].iloc[-1]
                    if pd.notna(_v):
                        _soc_plan_val = float(_v)
        except Exception:
            pass
        if _soc_plan_val is None:
            try:
                import vdt_state as _vs_card
                _cs_card = _vs_card.compute_current_state(_prof_load)
                _soc_plan_val = float(_cs_card.get("current_soc_pct"))
            except Exception:
                _soc_plan_val = _nz(now.get('soc_pct'))
        if realio_overlay:
            if _r_soc is not None:
                _soc_plan_val = _r_soc
                _soc_lbl = "SOC (meranie)"
                _soc_color = "#C62828"
            else:
                _soc_lbl = "SOC (chýba meranie)"
                _soc_color = "#666"
            soc_real_card = ""  # netreba — primárna karta SOC už ukazuje meranie
        else:
            _soc_lbl = "SOC teraz"; _soc_color = "#222"
            soc_real_card = ""

        # Batéria reálna — len v overlay móde, z realio merača
        if realio_overlay:
            batt_real_card = (
                f"<div class='card' style='background:#ffeaea'><div class='l'>Batéria reálna (meranie)</div>"
                f"<div class='v' style='color:#C62828'>"
                f"{('—' if _r_batt is None else f'{_r_batt:+.1f} kW')}"
                f"</div></div>"
            )
        else:
            batt_real_card = ""

        now_cards = (
            f"<div class='card'><div class='l'>Čas</div><div class='v' style='font-size:16px'>{str(now['time'])[5:16]}</div></div>"
            f"<div class='card'><div class='l'>{_ftv_lbl}</div><div class='v' style='color:{_ftv_color}'>{_ftv_val:.0f} kW</div></div>"
            f"<div class='card'><div class='l'>Batéria plán (DT)</div><div class='v'>{_nz(now.get('plan_batt_kw')):+.0f} kW</div></div>"
            f"<div class='card'><div class='l'>Batéria RT (odchýlka)</div><div class='v'>{rt_kw_now:+.0f} kW <span style='font-size:12px;color:#666'>{sm}</span></div></div>"
            f"{batt_real_card}"
            f"<div class='card'><div class='l'>{_soc_lbl}</div><div class='v' style='color:{_soc_color}'>{_soc_plan_val:.0f} %</div></div>"
            f"<div class='card'><div class='l'>{_dt_lbl}</div><div class='v'>{_dt_show:.0f} €/MWh</div></div>")
        # ── Odporúčanie teraz (rovnaký dizajn ako v /rt poradca) ──
        _reco_label = sm
        _reco_pow = int(round(abs(_nz(now.get("rt_power_pct")))))
        _reco_col = _RECO_COL.get(_reco_label, "#888")
        _reco_time = str(now["time"])[11:16] if pd.notna(now["time"]) else "—"
        reco_card = (
            f"<h2 style='margin:14px 0 4px'>Odporúčanie teraz ({_reco_time})</h2>"
            f"<div style='display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:4px 0 8px'>"
            f"<span style='font-size:28px;font-weight:800;padding:8px 18px;border-radius:12px;color:#fff;display:inline-block;background:{_reco_col}'>{_reco_label}</span>"
            f"<span style='font-size:16px;font-weight:700;padding:5px 12px;border-radius:9px;color:#fff;display:inline-block;background:#1F4E78'>výkon {_reco_pow} %</span>"
            f"<span style='color:#666;font-size:13px'>{str(now.get('rt_reason','') or '')}</span>"
            f"</div>")

        # ── SEPS okamžité hodnoty (len pre SK trh) — frekvencia + reg.výkon ────
        try:
            _is_sk = (mk is not None and str(mk.get_active_market()).lower() == "sk")
        except Exception:
            _is_sk = False
        if _is_sk:
            try:
                import seps_sk as _seps
                _sst = _seps.fetch_seps_realtime()
            except Exception as _e:
                _sst = None
            if _sst and _sst.get("frequency_hz") is not None:
                _hz = float(_sst.get("frequency_hz") or 0)
                _re = _sst.get("regulation_power_mw")
                _re = float(_re) if _re is not None else 0.0
                # Sign konvencia SEPS RE_WITH_GCC:
                #   RE > 0 → systém v DEFICITE (regulátory ťahajú produkciu hore) → VYBI
                #   RE < 0 → systém v PREBYTKU → NABI
                _seps_thr = 5.0                                    # MW deadband
                if _re > _seps_thr:
                    _seps_lbl, _seps_col = "VYBI", "#C0392B"
                elif _re < -_seps_thr:
                    _seps_lbl, _seps_col = "NABI", "#1F4E78"
                else:
                    _seps_lbl, _seps_col = "DRŽ", "#888"
                _hz_dev = _hz - 50.0
                _hz_col = "#2E7D32" if abs(_hz_dev) < 0.05 else ("#E0A800" if abs(_hz_dev) < 0.1 else "#C0392B")
                _upd = str(_sst.get("updated_at", ""))[11:19]
                _load = _sst.get("load_mw"); _prod = _sst.get("production_mw")
                _sbal = _sst.get("scheduled_balance_mw"); _rbal = _sst.get("real_balance_mw")
                reco_card += (
                    f"<div style='border:1px solid #d0d7de;border-radius:10px;padding:10px 14px;margin:6px 0 8px;background:#f6f8fa'>"
                    f"<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px'>"
                    f"<b style='color:#1F4E78'>🇸🇰 SEPS okamžité hodnoty</b>"
                    f"<span style='color:#666;font-size:12px'>updated {_upd} UTC</span>"
                    f"</div>"
                    f"<div style='display:flex;gap:10px;align-items:center;flex-wrap:wrap'>"
                    f"<span style='font-size:24px;font-weight:800;padding:6px 14px;border-radius:10px;color:#fff;background:{_seps_col}'>SEPS: {_seps_lbl}</span>"
                    f"<span style='font-size:14px;padding:5px 10px;border-radius:8px;background:#fff;border:1px solid #d0d7de'>"
                    f"Reg.výkon <b>{_re:+.1f} MW</b></span>"
                    f"<span style='font-size:14px;padding:5px 10px;border-radius:8px;background:#fff;border:1px solid #d0d7de;color:{_hz_col}'>"
                    f"Frekvencia <b>{_hz:.3f} Hz</b> <small>({_hz_dev:+.3f})</small></span>"
                    + (f"<span style='font-size:13px;color:#666'>Zaťaženie {_load:.0f} MW · Výroba {_prod:.0f} MW · "
                       f"Saldo {_rbal:+.0f} (plán {_sbal:+.0f}) MW</span>"
                       if _load is not None and _prod is not None else "")
                    + f"</div>"
                    f"<div style='font-size:11px;color:#888;margin-top:5px'>"
                    f"Konvencia SEPS: <b>+ MW = deficit</b> (regulátory ↑) → vybíjať batériu; "
                    f"<b>− MW = prebytok</b> → nabíjať. <b>Opačné znamienko</b> než ČEPS sys_MW "
                    f"(v rt_controlleri sa hodnoty interne flip-uju).</div>"
                    f"</div>"
                )
    else:
        now_cards = "<div class='card'><div class='l'>Stav</div><div class='v' style='font-size:15px'>—</div></div>"
        reco_card = ""
    # --- súhrn za zvolený deň ---
    # dview je decimovaný (max 600 bodov) — sum cez tieto body podhodnocuje denné kWh
    # 2-3×. Preto najprv pripravíme dfull_full (plná resolution) a urobíme z neho aj
    # denné agregáty, nielen baseline.
    # ── Načítaj PLNÚ resolution pre presné agregáty (cards, Po dňoch, baseline cum) ──
    # dfull (default max_points=2000) je decimovaný → sum(per_min) je 1/N reálneho súčtu.
    # Plnú resolution použijeme len pre AGREGÁTY. Chart display ostáva decimovaný kvôli performance.
    dfull_full = None
    if dfull is not None and not dfull.empty:
        try:
            _case_local = r.get("case", "plan_d1") if isinstance(r, dict) else "plan_d1"
            dfull_full = lsim.load_series(_case_local, port=_PORT, max_points=10**9)
        except Exception:
            dfull_full = dfull

    # ── Denné agregáty z PLNEJ resolution ──
    # Predtým: zo `dview` (decimovaný max 600 bodov) → sum bola 2-3× podhodnotená.
    # Teraz: priority order:
    #   1. `trace_full` — neoreznutý live today_trace (1440 minút aktuálneho dňa)
    #   2. `dfull_full` filtrovaný na view_day — z CSV (minulé dni)
    #   3. `dview` — fallback ak nič iné nie je dostupné
    d_dt = d_rt = d_ftv = 0.0
    _agg_src = None
    if trace_full is not None and not trace_full.empty:
        _agg_src = trace_full
    elif view_day:
        # ONE-DAY-LOAD (2026-06-15): agregát view-dňa = JEDEN deň z CSV/DB, nie filter celej
        # histórie cez dfull_full. Súčasť "netahať kompletnu historiu — staci jeden den".
        try:
            _case_agg = r.get("case", "plan_d1") if isinstance(r, dict) else "plan_d1"
            _df_day = lsim.load_series(_case_agg, port=_PORT, day=view_day, max_points=10**9)
            if _df_day is not None and not _df_day.empty:
                _agg_src = _df_day
        except Exception:
            _agg_src = None
    # DB unify F4 (2026-06-10): JEDINÝ zdroj pravdy pre €-hodnoty = core/effect_db.py.
    # Karty hore (cum_*), chC graf (kumulatív/po dňoch/15-min), Excel kľúčové ukazovatele
    # — všetko cez ten istý SQL agregát. Žiadne paralelné výpočty.
    from core.effect import compute_effect_totals as _eff
    # F4 fix: použiť pr.get_active() (rovnaký zdroj ako Excel + chC daily) namiesto
    # core.profile_resolver.get_active(). Resolver vracal iný/None profil → DEFAULT_FLAGS
    # (všetky toggles True) → filter no-op → RT karta == total.
    try:
        import profiles as _pr_eff
        _active_profile_eff = _pr_eff.get_active()
    except Exception:
        try:
            from core.profile_resolver import get_active as _ga_eff
            _active_profile_eff = _ga_eff()
        except Exception:
            _active_profile_eff = None
    try:
        import joint_lp_integration as _jli_eff_d
        _eff_joint = _jli_eff_d.get_flags_from_profile(_active_profile_eff) if _active_profile_eff else None
    except Exception:
        _eff_joint = None
    print(f"[livesim F4 diag] profile={_active_profile_eff} joint_flags={_eff_joint}")
    # DB unify F4: získaj obdobie z dfull (min/max time) a volaj jediný SQL agregát.
    # Override r['cum_total/cum_rt/cum_dt/cum_vdt_arb'] z DB → karty + chC ukážu konzistentné čísla.
    _eff_db_period = None
    _eff_db_from = None
    _eff_db_to = None
    if dfull is not None and not dfull.empty and _active_profile_eff:
        try:
            from core import effect_db as _eff_db_mod
            _eff_db_from = pd.Timestamp(dfull["time"].min()).strftime("%Y-%m-%d")
            _eff_db_to = pd.Timestamp(dfull["time"].max()).strftime("%Y-%m-%d")
            # DISPLAY-FROM-DB (2026-06-14): dnešok nie je v CSV (dfull = dokončené dni), ale
            # Fáza A ho UŽ zapisuje do effect_db → rozšír koniec obdobia na dnešok (prov_date),
            # inak by sumár + chC graf dnešok vynechali. Bug VDT-PROV-SCOPE (2026-06-15): `prov`
            # nie je v tomto scope definovaná → NameError padol CELÝ effect_db blok (karty 0,
            # vrátane VDT). Ber prov_date priamo z `r`.
            _prov_dt = r.get("prov_date") if isinstance(r, dict) else None
            if _prov_dt:
                _eff_db_to = max(_eff_db_to, str(_prov_dt)[:10])
            _eff_db_period = _eff_db_mod.get_period_effect(
                _active_profile_eff, _eff_db_from, _eff_db_to,
                joint_flags=_eff_joint)
            if _eff_db_period.get("days_count", 0) > 0 and isinstance(r, dict):
                # DISPLAY-FROM-DB Fáza B1 (2026-06-14): dnešok je teraz v effect_db (Fáza A
                # zapisuje provizórny dnešný trace+daily pri každom bg ticku) a obdobie končí
                # dnešok (prov), takže get_period_effect UŽ zahŕňa dnešok → karty čítajú
                # PRIAMO z DB, bez rekonštrukcie z advance. Predtým BREAKDOWN-INCLUDE-TODAY
                # dopočítaval dnešok z today_trace; to by teraz dvojito počítalo. Žiadna
                # závislosť kariet na advance cum_* — krok k „display = DB čítanie".
                r["cum_dt"] = float(_eff_db_period["dt_eur"])
                r["cum_rt"] = float(_eff_db_period["rt_eur"])
                # VDT-TODAY-FIX (2026-06-15): vdt_arb je render-overlay (Bug X), NIE v livesim
                # trace pri zápise → effect_db má dnešný VDT = 0. B1 ho omylom úplne vypustil
                # ("z toho VDT 0 €" napriek reálnym obchodom). DT/RT sú v trace, takže v effect_db
                # sedia (vrátane dnešku cez fázu A); VDT dnešok pripočítaj z today_trace. Past dni
                # = effect_db. Bez double-countu (dnešok v effect_db vdt = 0, lebo trace ho nemá).
                _today_vdt = 0.0
                _tt = r.get("today_trace")
                if _tt is not None and hasattr(_tt, "columns") and "vdt_arb_min" in _tt.columns:
                    try:
                        _today_vdt = float(pd.to_numeric(_tt["vdt_arb_min"], errors="coerce").fillna(0).sum())
                    except Exception:
                        _today_vdt = 0.0
                r["cum_vdt_arb"] = float(_eff_db_period["vdt_arb_eur"]) + _today_vdt
                # DIST-FEE kumulatív (od štartu) — SAMOSTATNÝ value stream (úspora zo
                # samospotreby: batéria/FTV kryje load → ušetrený distribučný poplatok).
                # NIE je súčasťou DT (DT = trhová arbitráž, distribúcia = meranie), preto
                # sa PRIPOČÍTAVA do Zisk SPOLU (user 2026-06-15). Konzistentne cez effect_minute.
                try:
                    _gf_cum = float((_ui_load("plan", {}) or {}).get("grid_fee", 0) or 0)
                    r["cum_dist"] = _eff_db_mod.get_period_dist_fee(
                        _active_profile_eff, _eff_db_from, _eff_db_to, _gf_cum)
                except Exception as _e_cumdist:
                    print(f"[DIST-FEE cum] {_e_cumdist}")
                    r["cum_dist"] = 0.0
                r["cum_total"] = (r["cum_dt"] + r["cum_rt"] + r["cum_vdt_arb"]
                                  + r.get("cum_dist", 0.0))
        except Exception as _e_dbf4:
            print(f"[livesim F4] effect_db.get_period_effect zlyhal: {_e_dbf4}")
            _eff_db_period = {"_error": str(_e_dbf4)}
    # F4 DIAG banner — viditeľný v HTML, ukáže prečo override nezbehol
    _f4_diag_banner = (
        f"<div style='background:#fff3cd;border:1px solid #ffe399;padding:8px 12px;"
        f"margin:8px 0;font-size:11px;font-family:monospace'>"
        f"<b>F4 diag:</b> profile=<code>{_active_profile_eff}</code> "
        f"joint_flags=<code>{_eff_joint}</code> "
        f"period=<code>{_eff_db_from}..{_eff_db_to}</code> "
        f"db_result=<code>{_eff_db_period}</code></div>"
    )
    if _agg_src is not None and not _agg_src.empty:
        try:
            _t = _eff(_agg_src, profile=_active_profile_eff, day=view_day,
                       joint_flags=_eff_joint)
            d_dt = _t["dt_eur"]; d_rt = _t["rt_eur"]
            # FTV: preferuj reálne meranie (realio overlay) pred plánom
            _ftv_col = ("ftv_min_real_kw"
                         if "ftv_min_real_kw" in _agg_src.columns
                             and _agg_src["ftv_min_real_kw"].notna().any()
                         else "ftv_kw")
            d_ftv = float(pd.to_numeric(_agg_src.get(_ftv_col, 0), errors="coerce")
                            .fillna(0).sum()) / 60.0   # kW per minute → kWh
        except Exception:
            _agg_src = None
    if _agg_src is None and dview is not None and not dview.empty:
        # Fallback: decimovaný dview (môže byť 2-3× podhodnotené)
        _t = _eff(dview, profile=_active_profile, day=view_day, joint_flags=_eff_joint)
        d_dt = _t["dt_eur"]; d_rt = _t["rt_eur"]
        d_ftv = float(dview["ftv_kw"].fillna(0).sum()) / 60.0

    # ── BASELINE kumulatívne (bez batérie + plánu) — z PLNEJ resolution per-minute FTV − load × real_DT ──
    bl_cum_card = ""
    _bl_total = None
    if bc is not None and dfull_full is not None and not dfull_full.empty:
        try:
            _pui_plan = _ui_load("plan", {}) or {}
            _bp = bc.parse_baseline_params(_pui_plan)
            # per-minute baseline z FULL data
            _ftv_min = pd.to_numeric(dfull_full.get("ftv_min_real_kw", dfull_full.get("ftv_kw")), errors="coerce").fillna(0).values
            _load_min = pd.to_numeric(dfull_full.get("load_min_real_kw", pd.Series([0.0]*len(dfull_full))), errors="coerce").fillna(0).values
            _dtp = pd.to_numeric(dfull_full.get("dt_real_eur", dfull_full.get("dt_eur")), errors="coerce").fillna(0).values
            _net_kw = _ftv_min - _load_min
            _ex_kwh_min = np.maximum(_net_kw, 0.0) / 60.0
            _im_kwh_min = -np.minimum(_net_kw, 0.0) / 60.0
            _p_imp = (_dtp * _bp["im_val"]) if _bp["im_mode"] == "dt_x" else np.full_like(_dtp, _bp["im_val"])
            _p_exp = (_dtp * _bp["ex_val"]) if _bp["ex_mode"] == "dt_x" else np.full_like(_dtp, _bp["ex_val"])
            _bl_rev = float((_ex_kwh_min * _p_exp / 1000.0).sum())
            _bl_cost = float((_im_kwh_min * _p_imp / 1000.0).sum())
            _bl_total = _bl_rev - _bl_cost
            _benefit = float(r.get("cum_total", 0)) - _bl_total
            _mode_txt = (f"Im: {'DT×'+str(_bp['im_val']) if _bp['im_mode']=='dt_x' else str(_bp['im_val'])+' €/MWh'} · "
                          f"Ex: {'DT×'+str(_bp['ex_val']) if _bp['ex_mode']=='dt_x' else str(_bp['ex_val'])+' €/MWh'}")
            bl_cum_card = (
                f"<div class='card' style='background:#fff3cd;border:1px solid #ffe399'><div class='l'>Baseline (bez bat. + plánu)</div>"
                f"<div class='v' style='color:#7a5d00'>{_bl_total:+.1f} €</div>"
                f"<div style='font-size:10px;color:#888'>{_mode_txt}</div></div>"
                f"<div class='card' style='background:#e8f5e9'><div class='l'>Prínos bat. + plánu</div>"
                f"<div class='v' style='color:#2E7D32'>{_benefit:+.1f} €</div></div>")
        except Exception:
            bl_cum_card = ""
    # Bug #608: VDT arbitráž karta (delta VDT cena vs DT clearing). VŽDY zobraz
    # (user: "chýba z toho VDT tak ako DT a RT") — aj keď 0, nech je breakdown
    # Zisk SPOLU = DT + RT + VDT kompletný a konzistentný.
    _cum_vdt_arb = float(r.get("cum_vdt_arb", 0) or 0)
    _vdt_arb_card = (f"<div class='card'><div class='l'>z toho VDT</div>"
                      f"<div class='v'>{_cum_vdt_arb:+.1f} €</div></div>")
    # Bug VDT-EFEKTIVITA (2026-06-11): karty efektivity obchodovania — objemy +
    # vážené ceny nákup/predaj za zobrazený deň aj od štartu.
    _vdt_eff_cards = ""
    try:
        from core.profile_resolver import get_active as _ga_ve
        _prof_ve = _ga_ve() or ""
        if _prof_ve:
            _vts_day = _vdt_trade_stats(_prof_ve, day=view_day)
            _vts_all = _vdt_trade_stats(_prof_ve)
            if _vts_all["n"]:
                _spread_all = _vts_all["sell_avg"] - _vts_all["buy_avg"]
                _day_part = ""
                if _vts_day["n"]:
                    # Bug VDT-EFEKTIVITA: presnosť exekučnej ceny vs OKTE clearing
                    _acc = _vdt_price_accuracy(_prof_ve, view_day, dview)
                    _acc_txt = ""
                    if _acc.get("werr") is not None:
                        _acc_txt = (f" · vs clearing {_acc['werr']:+.1f} €/MWh"
                                    f" (n={_acc['n']})")
                    _day_part = (
                        f"<div class='card' style='background:#f3e8fd'>"
                        f"<div class='l'>VDT obchody {view_day}</div>"
                        f"<div class='v' style='font-size:13px'>"
                        f"kúpené {_vts_day['buy_kwh']:.0f} kWh @ {_vts_day['buy_avg']:.1f} · "
                        f"predané {_vts_day['sell_kwh']:.0f} kWh @ {_vts_day['sell_avg']:.1f} €/MWh</div>"
                        f"<div style='font-size:10px;color:#888'>{_vts_day['n']} obchodov · "
                        f"cash {_vts_day['cash_eur']:+.1f} €{_acc_txt}</div></div>")
                # Bug VDT-EFEKTIVITA: zisk na cyklus — VDT arbitráž / cykly z VDT objemov
                _cyc_txt = ""
                try:
                    import profiles as _pr_cyc
                    _bkwh_cyc = float((((_pr_cyc.load_profile(_prof_ve) or {})
                                        .get("plan") or {}).get("batt_kwh", 0.0)) or 0.0)
                    _vdt_cycles = ((_vts_all["buy_kwh"] + _vts_all["sell_kwh"]) / 2.0
                                   / _bkwh_cyc) if _bkwh_cyc > 0 else 0.0
                    if _vdt_cycles > 0.05 and _cum_vdt_arb:
                        _cyc_txt = (f" · {_vdt_cycles:.1f} cyklov "
                                    f"≈ {_cum_vdt_arb / _vdt_cycles:+.1f} €/cyklus")
                except Exception:
                    pass
                _vdt_eff_cards = _day_part + (
                    f"<div class='card' style='background:#f3e8fd'>"
                    f"<div class='l'>VDT od štartu</div>"
                    f"<div class='v' style='font-size:13px'>"
                    f"kúpené {_vts_all['buy_kwh']:.0f} kWh @ {_vts_all['buy_avg']:.1f} · "
                    f"predané {_vts_all['sell_kwh']:.0f} kWh @ {_vts_all['sell_avg']:.1f} €/MWh</div>"
                    f"<div style='font-size:10px;color:#888'>{_vts_all['n']} obchodov · "
                    f"spread {_spread_all:+.1f} €/MWh · cash {_vts_all['cash_eur']:+.1f} €"
                    f"{_cyc_txt}</div></div>")
    except Exception as _e_ve:
        print(f"[VDT-EFEKTIVITA karty] {_e_ve}")
    # RT efektivita (bod 2 RT v2): ex-post vyhodnotenie zásahov za zobrazený deň
    _rt_eff_card = ""
    try:
        _res = _rt_eff_stats(dview)
        if _res["n"] > 0:
            _hit_txt = (f"hit {_res['hit_pct']:.0f} %" if _res["hit_pct"] is not None else "hit —")
            _sp_txt = (f" · Ø spread {_res['avg_spread']:+.1f} €/MWh"
                       if _res["avg_spread"] is not None else "")
            _eng_txt = ""
            if _res.get("engine"):
                _eng_txt = f" · {_res['engine']}"   # RT-EFF-V3-LABEL
            elif _res["v2_share"] is not None:
                _eng_txt = (" · v2" if _res["v2_share"] >= 99
                            else (" · v1" if _res["v2_share"] <= 1
                                  else f" · v2 {_res['v2_share']:.0f} %"))
            _rev_col = "#2E7D32" if _res["rev_eur"] >= 0 else "#C62828"
            _rt_eff_card = (
                f"<div class='card' style='background:#fdf3e8'>"
                f"<div class='l'>RT efektivita {view_day}{_eng_txt}</div>"
                f"<div class='v' style='font-size:13px;color:{_rev_col}'>"
                f"{_res['rev_eur']:+.1f} € · {_res['n']} zásahov · {_hit_txt}</div>"
                f"<div style='font-size:10px;color:#888'>hit = smer zásahu sa zhodol "
                f"so znamienkom realizovaného ZCO−DT spreadu{_sp_txt}</div></div>")
    except Exception as _e_rte_c:
        print(f"[RT-EFEKTIVITA karta] {_e_rte_c}")
    # daily VDT arb
    _d_vdt = 0.0
    try:
        if "vdt_arb_min" in dview.columns:
            _d_vdt = float(pd.to_numeric(dview["vdt_arb_min"], errors="coerce").fillna(0).sum())
    except Exception:
        pass
    # DIST-FEE (2026-06-15): distribučná úspora zvlášť = grid_fee × (baseline_import − skutočný_import)
    _d_dist = 0.0; _dist_reduction_kwh = 0.0; _gf_dist = 0.0
    try:
        _gf_dist = float((_ui_load("plan", {}) or {}).get("grid_fee", 0) or 0)
        if bc is not None and _gf_dist > 0:
            _dist_r = bc.compute_dist_fee_savings(dview, _gf_dist)
            _d_dist = float(_dist_r.get("dist_fee_eur", 0.0))
            _dist_reduction_kwh = float(_dist_r.get("import_reduction_kwh", 0.0))
    except Exception as _e_dist:
        print(f"[DIST-FEE karta] {_e_dist}")
    cards = (
        f"{_f4_diag_banner}"
        f"<div style='display:flex;gap:12px;flex-wrap:wrap;margin:10px 0'>"
        f"<div class='card'><div class='l'>Zisk SPOLU (od štartu)</div><div class='v' style='color:#2E7D32'>{r['cum_total']:.1f} €</div></div>"
        f"<div class='card'><div class='l'>z toho DT</div><div class='v'>{r['cum_dt']:.1f} €</div></div>"
        f"<div class='card'><div class='l'>z toho odchýlka (RT)</div><div class='v'>{r['cum_rt']:.1f} €</div></div>"
        f"<div class='card'><div class='l'>z toho distribúcia (od štartu)</div><div class='v'>{r.get('cum_dist', 0.0):.1f} €</div></div>"
        f"{_vdt_arb_card}"
        f"{_vdt_eff_cards}"
        f"{_rt_eff_card}"
        f"{bl_cum_card}"
        f"<div class='card' style='background:#eef7ee'><div class='l'>Zisk za deň {view_day}</div><div class='v' style='color:#2E7D32'>{d_dt+d_rt+_d_vdt+_d_dist:.1f} €</div>"
        f"<div style='font-size:11px;color:#555'>DT {d_dt:+.1f} · RT {d_rt:+.1f} · VDT {_d_vdt:+.1f} · Dist {_d_dist:+.1f} €</div></div>"
        f"<div class='card' style='background:#eef7ee'><div class='l'>Úspora na distribúcii {view_day}</div>"
        f"<div class='v' style='color:#2E7D32'>{_d_dist:+.1f} €</div>"
        f"<div style='font-size:11px;color:#555'>{_dist_reduction_kwh:+.0f} kWh menej odberu zo siete · poplatok {_gf_dist:.2f} €/MWh</div></div>"
        f"<div class='card' style='background:#eef7ee'><div class='l'>FTV výroba za deň</div><div class='v'>{d_ftv:.0f} kWh</div></div></div>"
        f"<h2 style='margin:6px 0'>Hodnoty teraz</h2><div style='display:flex;gap:12px;flex-wrap:wrap;margin:4px 0'>{now_cards}</div>"
        f"{reco_card}")
    _liverow = None; _lagmin = None
    if _tt is not None and len(_tt):
        _lvr = _tt[_tt.get("is_live", 1) == 1] if "is_live" in _tt.columns else _tt
        if len(_lvr):
            _lt = pd.Timestamp(_lvr["time"].iloc[-1])
            _liverow = str(_lt)[5:16]
            _lagmin = int(round((dt.datetime.now() - _lt.to_pydatetime()).total_seconds()/60.0))
    _refreshed = dt.datetime.now().strftime("%H:%M:%S")
    _lagtxt = (f" <span style='color:#b06a00'>(≈{_lagmin} min za reálnym časom — ČEPS publikuje s oneskorením)</span>"
               if _lagmin is not None and _lagmin >= 3 else "")
    _liveinfo = (f" &nbsp;•&nbsp; živé dáta po <b>{_liverow}</b>{_lagtxt}" if _liverow else "") + \
                f" &nbsp;•&nbsp; obnovené <b>{_refreshed}</b>"
    info = (f"<p style='background:#f8f9fb;border-radius:8px;padding:8px 12px;font-size:14px'>"
            f"Prípad <b>{r['case']}</b>{rtnote} &nbsp;•&nbsp; štart <b>{r['start_date']}</b> &nbsp;•&nbsp; "
            f"uložené po <b>{r['last_min']}</b> &nbsp;•&nbsp; minút v logu <b>{n}</b>{_liveinfo} &nbsp;•&nbsp; "
            f"súbor <code>{r['csv']}</code></p>")
    _pu = r.get("plan_used", {}) or {}
    _ru = r.get("rt_used", {}) or {}
    def _g(d, k, suf=""):
        return f"{d[k]:g}{suf}" if k in d and d[k] is not None else "—"
    info += (f"<p style='background:#eef3fb;border-radius:8px;padding:6px 12px;font-size:13px;color:#33506e'>"
             f"<b>Nastavenia plánu (z formulára):</b> SOC koniec {_g(_pu,'terminal_soc_pct','%')} · "
             f"min. rozdiel {_g(_pu,'min_spread_eur',' €')} · orezanie "
             f"{('áno' if _pu.get('allow_curtail') else 'nie') if 'allow_curtail' in _pu else '—'} · "
             f"batéria {_g(_pu,'batt_kw',' kW')}/{_g(_pu,'batt_kwh',' kWh')} &nbsp;|&nbsp; "
             f"<b>RT signál (poradca):</b> kdis {_g(_ru,'kdis')} · kchg {_g(_ru,'kchg')} · dtk {_g(_ru,'dtk')}</p>")
    # --- navigácia dní (história) ---
    daylist = [d.isoformat() for d in days] if days else []
    nav = ""
    if daylist:
        vi = daylist.index(view_day) if view_day in daylist else len(daylist)-1
        prev_d = daylist[vi-1] if vi > 0 else None
        next_d = daylist[vi+1] if vi < len(daylist)-1 else None
        opts = "".join(f"<option value='{d}'{' selected' if d==view_day else ''}>{d}</option>" for d in daylist)
        pl = (f"<a href='/livesim?case={r['case']}&view={prev_d}' style='text-decoration:none;padding:5px 10px;"
              f"background:#1F4E78;color:#fff;border-radius:7px'>◀ {prev_d}</a>") if prev_d else "<span></span>"
        nl = (f"<a href='/livesim?case={r['case']}&view={next_d}' style='text-decoration:none;padding:5px 10px;"
              f"background:#1F4E78;color:#fff;border-radius:7px'>{next_d} ▶</a>") if next_d else "<span></span>"
        latest = daylist[-1]
        newest = (f"<a href='/livesim?case={r['case']}&view={latest}' style='text-decoration:none;padding:5px 12px;"
                  f"background:#2E7D32;color:#fff;border-radius:7px;font-weight:600'>⏭ Najnovší deň ({latest})</a>")
        nav = (f"<h2>Zvolený deň</h2><div style='display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0'>{pl}"
               f"<form method='get' action='/livesim' style='display:inline'><input type='hidden' name='case' value=\"{r['case']}\">"
               f"<select name='view' onchange='this.form.submit()'>{opts}</select></form>{nl}{newest}"
               f"<span style='color:#666;font-size:13px'>(◀ ▶ listuj dni; alebo skoč na najnovší. Simulátor počíta len dni, "
               f"ku ktorým už existujú reálne dáta — dnešok pribudne, keď doň dorazia.)</span></div>")
    # --- grafy za zvolený deň ---
    if dview is not None and not dview.empty:
        Ld = "[" + ",".join(f"'{str(t)[11:16]}'" for t in dview["time"]) + "]"
        # Plné 0-24h: živé minúty + plán projekcia (PB + 0 RT, projected SOC). is_live
        # naďalej rozlišuje minulosť (s reálnym RT signálom) od projekcie pre tooltip popisy.
        _lv = list(dview["is_live"].values) if "is_live" in dview.columns else [1]*len(dview)
        PB = "[" + ",".join(_js(x) for x in dview["plan_batt_kw"]) + "]"
        # RT akcia: pre živé minúty reálna; pre budúce 0 (žiadny live signál) → krivka klesne na 0 po teraz
        RK = "[" + ",".join(_js(_nz(d)*_nz(p)/100.0*bkw)
                            for d, p in zip(dview["rt_dir"], dview["rt_power_pct"])) + "]"
        # Batéria SPOLU = REALISTICKY clipnutý plán + RT
        # (batt_kw_realistic = plán clipnutý na fyzicky možné given FTV+grid limity; fallback na čistý plán
        # ak nový stĺpec nie je v CSV — pre staré logy bez bumpu csv_cols_v)
        # Bug KK (2026-06-07): ak Bug X overlay pridal VDT do plan_batt_kw,
        # batt_kw_realistic je outdated (D-1 only, bez VDT). Použi plan_batt_kw
        # ktoré obsahuje D-1 + VDT (SOC tiež integruje plan_batt_kw → consistency).
        _has_vdt_overlay = ("plan_batt_vdt_kw" in dview.columns
                            and pd.to_numeric(dview["plan_batt_vdt_kw"],
                                              errors="coerce").fillna(0).abs().sum() > 0.1)
        # Bug SOC-DOUBLE-RT (2026-06-10): batt_kw_realistic JE výstup rt_controller PO
        # aplikácii RT zásahov (vrátane plan+RT cap). Predtým sa k nemu PRIDÁVAL
        # rt_dir × rt_power_pct/100 × bkw → DVOJITÉ ZAPOČÍTANIE RT v grafe.
        # Dôsledok: zelená "Batéria PREDIKCIA (plán+RT)" ukazovala ±6000 kW peaky,
        # ale SOC krivka (= integrácia skutočného batt_kw_realistic) zostala plochá
        # lebo SOC integroval iba skutočné batt akcie (post-cap).
        # Fix: ak batt_kw_realistic existuje → použiť priamo (zahŕňa všetky vrstvy).
        #      inak (predikcia budúcich minút) → plan_batt_kw + RT intent (bez RT lebo
        #      pre budúcnosť rt_dir=0).
        # Bug SOC-DOUBLE-RT-v2: po Bug BB (#560 sch.batt_kw += VDT PRED rt_controller)
        # batt_kw_realistic už zahŕňa aj VDT, nielen D-1 plán. Legacy Bug KK komentár
        # bol pre starý code path. Odstránené `not _has_vdt_overlay` aby fix platil
        # AJ pre use_vdt=True profily (VW_simulacia_2). Skontroluj že stĺpec má aspoň
        # jednu nenulovú hodnotu (= rt_controller skutočne zbehol).
        _batt_real_col = dview.get("batt_kw_realistic") if "batt_kw_realistic" in dview.columns else None
        _has_real = (_batt_real_col is not None
                      and pd.to_numeric(_batt_real_col, errors="coerce").fillna(0).abs().sum() > 0.1)
        if _has_real:
            # Bug FUTURE-PRED-NAN (2026-06-16, user VW_simulacia_3): pre BUDÚCE minúty
            # batt_kw_realistic = NaN → zelená predikcia padala na NaN (= "+nan kW",
            # večerné vybíjanie sa nevykreslilo). Minulosť z realized (post-cap, vrátane
            # RT+VDT), budúcnosť doplníme PLÁNOM → predikcia ukáže plánované vybíjanie.
            _real_s = pd.to_numeric(dview["batt_kw_realistic"], errors="coerce")
            _plan_s = (pd.to_numeric(dview["plan_batt_kw"], errors="coerce")
                       if "plan_batt_kw" in dview.columns else _real_s)
            _batt_base = _real_s.where(_real_s.notna(), _plan_s).fillna(0.0)
            _add_rt_intent = False   # realized už zahŕňa plán+VDT+RT post-cap; budúce = plán
        else:
            _batt_base = dview["plan_batt_kw"]
            _add_rt_intent = True    # fallback pre staré CSV bez batt_kw_realistic
        # Bug #610: clip predikciu na fyzické limity batt
        def _clip_to_batt(v):
            if bkw > 0:
                if v > bkw: return bkw
                if v < -bkw: return -bkw
            return v
        if _add_rt_intent:
            _act_per_min = [_clip_to_batt(_nz(pb) + _nz(d) * _nz(p) / 100.0 * bkw)
                              for pb, d, p in zip(_batt_base, dview["rt_dir"], dview["rt_power_pct"])]
        else:
            _act_per_min = [_clip_to_batt(_nz(pb)) for pb in _batt_base]
        AC = "[" + ",".join(_js(float(x)) for x in _act_per_min) + "]"
        # PLÁN batérie (D-1 + VDT realized + planned) — referencia "čo malo byť".
        # Rozdiel voči AC = odchýlka (typicky keď VDT plán prekročí grid_kw_import
        # alebo FTV nedodá → batt_kw_realistic clipnutý na 0 a vznikne dev).
        _plan_for_ref = dview["plan_batt_kw"].fillna(0.0).tolist() if "plan_batt_kw" in dview.columns else [0.0]*len(dview)
        AP = "[" + ",".join(_js(float(x)) for x in _plan_for_ref) + "]"
        # 15-min agregat ako druhy dataset (transparentny prehlad)
        try:
            _act_df = pd.DataFrame({"_t": dview["time"].values, "_v": _act_per_min})
            _act_df["_slot"] = pd.to_datetime(_act_df["_t"]).dt.floor("15min")
            _slot_mean = _act_df.groupby("_slot")["_v"].transform("mean")
            AC_AGG = "[" + ",".join(_js(float(x)) for x in _slot_mean) + "]"
        except Exception:
            AC_AGG = AC
        # Bug SOC-FORECAST-EOD (2026-06-13, user: "oranžová sa má projektovať vždy do
        # konca dňa, aby bolo vidieť ako na tom je"): pre BUDÚCE minúty dnešného dňa
        # (po „teraz") soc_pct zo živej simulácie zaostáva (RT=0 pre budúcnosť → krivka
        # sa zarovná). Dopočítaj ju dopredu z poslednej REÁLNEJ SOC integráciou
        # plan_batt_kw (= clipnutý plán+VDT, post-kapacitná poistka), clip na [min,max].
        try:
            if ("soc_pct" in dview.columns and "plan_batt_kw" in dview.columns
                    and "time" in dview.columns and len(dview) > 1):
                import profiles as _pr_eod
                _pl_eod = ((_pr_eod.load_profile(_active_profile_eff) or {}).get("plan")
                           if _active_profile_eff else {}) or {}
                _bk_eod = float(_pl_eod.get("batt_kwh", 800.0) or 800.0)
                _smin = float(_pl_eod.get("soc_min", _pl_eod.get("soc_min_pct", 5.0)) or 5.0)
                _smax = float(_pl_eod.get("soc_max", _pl_eod.get("soc_max_pct", 100.0)) or 100.0)
                _ec = float(_pl_eod.get("eff_c", 0.95) or 0.95)
                _ed = float(_pl_eod.get("eff_d", 0.95) or 0.95)
                _lo_eod = _smin / 100.0 * _bk_eod
                _hi_eod = _smax / 100.0 * _bk_eod
                _t_now_eod = pd.Timestamp.now()
                _times_eod = pd.to_datetime(dview["time"], errors="coerce")
                _soc_eod = pd.to_numeric(dview["soc_pct"], errors="coerce").tolist()
                _pb_eod = pd.to_numeric(dview["plan_batt_kw"], errors="coerce").fillna(0.0).tolist()
                # posledná reálna minúta (≤ teraz) s platnou SOC
                _last_real = -1
                for _i in range(len(dview)):
                    _ti = _times_eod.iloc[_i]
                    if pd.notna(_ti) and _ti <= _t_now_eod and _soc_eod[_i] == _soc_eod[_i]:
                        _last_real = _i
                if 0 <= _last_real < len(dview) - 1 and _bk_eod > 0:
                    # REÁLNY časový krok riadku (dview môže byť 2-/15-min, nie 1-min).
                    # Bug SOC-EOD-STEP (2026-06-16, user VW_simulacia_3): napevno /60 (1-min)
                    # pri 2-min dview podhodnotilo energiu na polovicu → SOC končil na 50 %
                    # namiesto 5 %. Použijeme skutočný krok.
                    try:
                        _step_eod = float((_times_eod.iloc[1] - _times_eod.iloc[0]).total_seconds()) / 3600.0
                        if not (_step_eod > 0):
                            _step_eod = 1.0 / 60.0
                    except Exception:
                        _step_eod = 1.0 / 60.0
                    _soc_kwh_eod = float(_soc_eod[_last_real]) / 100.0 * _bk_eod
                    for _i in range(_last_real + 1, len(dview)):
                        _eg = float(_pb_eod[_i]) * _step_eod      # kWh za krok (+vybíja −nabíja)
                        if _eg > 0:
                            _soc_kwh_eod -= _eg / max(_ed, 0.5)
                        else:
                            _soc_kwh_eod += (-_eg) * _ec
                        _soc_kwh_eod = max(_lo_eod, min(_hi_eod, _soc_kwh_eod))
                        _soc_eod[_i] = _soc_kwh_eod / _bk_eod * 100.0
                    dview = dview.copy()
                    dview["soc_pct"] = _soc_eod
        except Exception as _e_eod:
            print(f"[SOC-FORECAST-EOD] {_e_eod}")
        # SOC PREDIKCIA: z trace (plánovaný/projektovaný SOC, výsledok RT engine + plánu)
        SO = "[" + ",".join(_js(x) for x in dview["soc_pct"]) + "]"
        # SOC PLÁN: full-day predikcia z plan_batt_kw (D-1 + VDT realized + plánované VDT)
        # — ide cez celý deň, aj cez zobchodované budúce sloty.
        if "soc_pct_plan" in dview.columns:
            SOP = "[" + ",".join(_js(x) for x in dview["soc_pct_plan"]) + "]"
        else:
            SOP = "[" + ",".join(["null"] * len(dview)) + "]"
        # ── Realio realita pre SOC + batt (len v realio_overlay móde, paralelne k predikcii) ──
        if realio_overlay and "realio_batt_kw" in dview.columns:
            BATT_REAL = "[" + ",".join(_js(x) for x in dview["realio_batt_kw"]) + "]"
        else:
            BATT_REAL = "[" + ",".join(["null"] * len(dview)) + "]"
        if realio_overlay and "realio_soc_pct" in dview.columns:
            SOC_REAL = "[" + ",".join(_js(x) for x in dview["realio_soc_pct"]) + "]"
        else:
            SOC_REAL = "[" + ",".join(["null"] * len(dview)) + "]"
        # ── Konverzia kWh/perióda → kW pre dt_15min ───────────────────────────
        # dview["ftv_kw"] obsahuje pvper (kWh per perióda) z plánu. V plan_d1 móde
        # je perióda 60 min, takže kWh/h = kW (zhoda náhod). V dt_15min móde je
        # perióda 15 min, takže kW = kWh / 0.25 = kWh × 4.
        # POZOR: konverziu aplikujeme IBA v realio_overlay móde — užívateľ chce
        # aby simulačné grafy ostali nezmenené (správanie po starom).
        _case_for_dt = r.get("case", "plan_d1") if isinstance(r, dict) else "plan_d1"
        _step_min = 15 if _case_for_dt == "dt_15min" else 60
        _dt_h = _step_min / 60.0                  # 0.25 (15-min) alebo 1.0 (60-min)
        _kwh_to_kw = (1.0 / _dt_h) if realio_overlay else 1.0   # × 4 v 15-min real-móde, inak 1.0

        # FT = hodinová predikcia (alebo scenár ak je) — stepped
        # V dt_15min móde sú aj `ftv_hour_plan_kw` aj `ftv_kw` v kWh/perióda (napriek menu kW)
        # — v realio_overlay móde aplikujeme konverziu × 4. V sim móde necháme bez zmeny.
        if "ftv_hour_plan_kw" in dview.columns:
            FT = "[" + ",".join(_js(_nz(x) * _kwh_to_kw) for x in dview["ftv_hour_plan_kw"]) + "]"
        else:
            FT = "[" + ",".join(_js(_nz(x) * _kwh_to_kw) for x in dview["ftv_kw"]) + "]"
        # FT_ORIG = pôvodný PVF plán (z dview["ftv_kw"]). Konverzia len v realio_overlay.
        FT_ORIG = "[" + ",".join(_js(_nz(x) * _kwh_to_kw) for x in dview["ftv_kw"]) + "]"
        # FT_REAL_MIN = minútová realita (zo scenára / ftv_minute generator) — už v kW
        _ftv_real_src = dview["ftv_min_real_kw"] if "ftv_min_real_kw" in dview.columns else dview["ftv_kw"]
        FTM_FROM_TRACE = "[" + ",".join(_js(x) for x in _ftv_real_src) + "]"
        # FT_REALIO = reálne meranie z realio CSV (len v realio_overlay móde, kW)
        if realio_overlay and "realio_ftv_kw" in dview.columns:
            FT_REALIO = "[" + ",".join(_js(x) for x in dview["realio_ftv_kw"]) + "]"
        else:
            FT_REALIO = "[" + ",".join(["null"] * len(dview)) + "]"
        # LOAD: per-perióda plán (stepped) + minútová realita (s šumom) — kreslíme ZÁPORNE pre vizuál
        _load_plan_src = dview["load_plan_kw"] if "load_plan_kw" in dview.columns else None
        _load_min_src = dview["load_min_real_kw"] if "load_min_real_kw" in dview.columns else None
        LOAD_PLAN_NEG = ("[" + ",".join(_js(-_nz(x)) for x in _load_plan_src) + "]"
                          if _load_plan_src is not None else "[" + ",".join(["null"] * len(dview)) + "]")
        LOAD_MIN_NEG = ("[" + ",".join(_js(-_nz(x)) for x in _load_min_src) + "]"
                          if _load_min_src is not None else "[" + ",".join(["null"] * len(dview)) + "]")
        # NET (FTV − load) hodinová stepped — výsledný net priebeh na prahu (pred batériou)
        _ftv_hour_src = dview["ftv_hour_plan_kw"] if "ftv_hour_plan_kw" in dview.columns else dview["ftv_kw"]
        _net_hour = ([_nz(f) - _nz(l) for f, l in zip(_ftv_hour_src, _load_plan_src)]
                      if _load_plan_src is not None else list(_ftv_hour_src))
        NET_HOUR = "[" + ",".join(_js(x) for x in _net_hour) + "]"
        # CU = per-minútová realita orezania (priorita) → fallback na hodinové plánové orezanie
        if "ftv_min_curtailed_kw" in dview.columns:
            CU = "[" + ",".join(_js(x) for x in dview["ftv_min_curtailed_kw"]) + "]"
        else:
            CU = "[" + ",".join(_js(x) for x in dview["plan_curtail_kwh"]) + "]"   # už v kW (max ~99)
        MW = "[" + ",".join(_js(x) for x in dview["mw_sig"]) + "]"
        BD = "[" + ",".join(_js(x) for x in dview["band_dis"]) + "]"
        BC = "[" + ",".join(_js(-_nz(x) if _np.isfinite(_nz(x, float('nan'))) else float('nan')) for x in dview["band_chg"]) + "]"
        DT = "[" + ",".join(_js(x) for x in dview["dt_eur"]) + "]"
        # — realita výkonu na prahu zákazníka (FTV + batéria SPOLU) + ODCHÝLKA VOČI NOMINÁCII OBCHODU
        # Plán = `plan_grid_kwh` (Obchod) v kW. Konverzia kWh/perióda → kW podľa step_min.
        _step_min = int(r.get("step_min", 60)) if isinstance(r, dict) else 60
        _step_h = max(_step_min, 1) / 60.0
        # Bug NOM-DOUBLE (2026-06-11): plan_grid_kwh UŽ obsahuje VDT (engine #625-B aj
        # render Bug X rebuild). "DAM nominácia" musí byť ČISTÝ DAM (plan_grid_dam_kwh),
        # inak "Aktuálna nominácia" (nižšie: DAM + VDT realizované) pripočíta VDT 2×
        # a graf ukáže pozíciu nad fyzický limit (symptóm: 6500 pri reálnych 6000).
        if "plan_grid_dam_kwh" in dview.columns:
            _pg_series_nom = dview["plan_grid_dam_kwh"].fillna(dview["plan_grid_kwh"])
            _pg_has_pure_dam = True
        else:
            _pg_series_nom = dview["plan_grid_kwh"]
            _pg_has_pure_dam = False
        _pg_kw = [(_nz(x) / _step_h) for x in _pg_series_nom]                                  # plán siete v kW (Obchod, čistý DAM)
        # ─── ENERGY BALANCE pre chFlow (stacked bars FTV/Load/Batt/Grid) ───────────────────
        # Konvencia: zdroje (energia do prahu) sú KLADNÉ, spotreba (energia zo prahu) je ZÁPORNÁ.
        # Bilancia: FTV + Batt_dis + Grid_im = Load + Batt_chg + Grid_ex + Curtail
        # Plan batt: + = vybíja (zdroj), − = nabíja (spotreba).
        # Plan grid (Obchod): + = export (spotrebič — energia ide do siete), − = import (zdroj).
        _flow_ftv = [max(_nz(x) * _kwh_to_kw, 0.0) for x in dview["ftv_kw"]]                   # FTV produkcia +
        _plan_batt = list(dview["plan_batt_kw"])
        _flow_batt_dis = [max(_nz(b), 0.0) for b in _plan_batt]                                # batt vybíja +
        _flow_batt_chg = [min(_nz(b), 0.0) for b in _plan_batt]                                # batt nabíja − (záporné)
        _flow_grid_im = [max(-x, 0.0) for x in _pg_kw]                                          # grid import +
        _flow_grid_ex = [-max(x, 0.0) for x in _pg_kw]                                          # grid export − (záporné)
        _flow_load_src = dview["load_plan_kw"] if "load_plan_kw" in dview.columns else None
        _flow_load = ([-_nz(x) for x in _flow_load_src] if _flow_load_src is not None
                       else [0.0] * len(dview))                                                  # load − (záporné)
        _flow_curtail = [-_nz(x) for x in dview["plan_curtail_kwh"]]                            # curtail − (záporné)
        FLOW_FTV = "[" + ",".join(_js(x) for x in _flow_ftv) + "]"
        FLOW_BATT_DIS = "[" + ",".join(_js(x) for x in _flow_batt_dis) + "]"
        FLOW_BATT_CHG = "[" + ",".join(_js(x) for x in _flow_batt_chg) + "]"
        FLOW_GRID_IM = "[" + ",".join(_js(x) for x in _flow_grid_im) + "]"
        FLOW_GRID_EX = "[" + ",".join(_js(x) for x in _flow_grid_ex) + "]"
        FLOW_LOAD = "[" + ",".join(_js(x) for x in _flow_load) + "]"
        FLOW_CURTAIL = "[" + ",".join(_js(x) for x in _flow_curtail) + "]"

        # ── VDT vrstvy: plánované extras + realizované paper trades ─────────
        # Cieľ: zobraziť čo by sa malo nakúpiť/predať na VDT NAD rámec DAM nominácie.
        # Konvencia: kladné = predaj (export), záporné = nákup (import) — zhoda s "Plán Obchodu".
        # Per slot 15-min kWh → kW = kWh * 4. Per minute v dview → pohľadom na slot_idx.
        _vdt_planned96 = [0.0] * 96   # VDT extras nad DAM z vdt_live_advisor cache
        _vdt_realized96 = [0.0] * 96  # paper trades — skutočne zadané VDT obchody
        try:
            import vdt_live_advisor as _vla_chart
            import json as _json_v
            import os as _os_v
            import plan_store as _ps_vla
            _cur_prof_vla = _ps_vla.resolve_profile() or None
            _vla_p = _vla_chart.cache_path(_cur_prof_vla)
            if _os_v.path.exists(_vla_p):
                with open(_vla_p) as _f_v:
                    _vla_d = _json_v.load(_f_v)
                # Match podľa profilu — VDT cache patrí jednému profilu (zvyčajne real)
                # PROFILE MUSI SEDIET — inak by VDT z iného profilu (napr. Trakany s
                # batt 500 kW) leakoval do view iného profilu (Coop batt 200 kW).
                _cache_prof = str(_vla_d.get("profile") or "")
                try:
                    import plan_store as _ps_v
                    _cur_prof = _ps_v.resolve_profile()
                except Exception:
                    _cur_prof = ""
                if _cache_prof and _cur_prof and _cache_prof == _cur_prof:
                    _ts_cache = str(_vla_d.get("ts", ""))[:10]
                    if _ts_cache == view_day:
                        _fp = _vla_d.get("full_plan", []) or []
                        _dam96 = _vla_d.get("dam_commits", []) or []
                        # full_plan je rolling MPC ktorý pokrýva 24h dopredu od cache.ts —
                        # vrátane ďalšieho dňa. Treba sledovať kedy slot_idx "klesne" (00:00
                        # po 23:45) a tam ukončiť iteráciu (ďalej už je zajtra).
                        _last_idx = -1
                        for _ent in _fp:
                            _sl = str(_ent.get("slot", ""))
                            if "-" not in _sl or len(_sl) < 5:
                                continue
                            try:
                                _h = int(_sl[:2]); _m = int(_sl[3:5])
                                _idx = (_h * 60 + _m) // 15
                            except Exception:
                                continue
                            if not (0 <= _idx < 96):
                                continue
                            # Detekcia prechodu cez polnoc → zajtra → stop
                            if _last_idx >= 0 and _idx < _last_idx:
                                break
                            _last_idx = _idx
                            _kwh_e = float(_ent.get("kwh", 0) or 0)
                            _act = str(_ent.get("action", ""))
                            if _act == "discharge":
                                _signed = _kwh_e
                            elif _act == "charge":
                                _signed = -_kwh_e
                            else:
                                _signed = 0.0
                            _dam_v = float(_dam96[_idx]) if _idx < len(_dam96) else 0.0
                            _vdt_planned96[_idx] = _signed - _dam_v
        except Exception as _e_vp:
            print(f"[livesim chPlan VDT plan] {_e_vp}")
        try:
            import csv as _csv_v
            import os as _os_v2
            import market as _mk_v
            import plan_store as _ps_v2
            import profiles as _pr_v2
            _cur_prof_real = _ps_v2.resolve_profile() or ""
            # Mode súčasného profilu — sim profil je tolerantný k legacy záznamom
            # (s prázdnym profile column ktoré boli migrované zo starých sim trades).
            # Real profil ostáva strict aby sa nemiešali viaceré real profily.
            _cur_prof_data_v = _pr_v2.load_profile(_cur_prof_real) if _cur_prof_real else None
            _cur_prof_mode = str((_cur_prof_data_v or {}).get("mode", "")).lower()
            _is_sim_profile = (_cur_prof_mode == "simulation")
            # Bug #619: chart musí čítať per-profile CSV cestu (sandbox layout),
            # nie legacy market data_dir. paper_trades_csv_path resolvuje správnu
            # cestu (out/profiles/<name>/vdt_paper_trades.csv).
            try:
                from vdt_live_advisor import paper_trades_csv_path as _pt_path
                _csv_p = _pt_path(_cur_prof_real)
            except Exception:
                _csv_p = _os_v2.path.join(_mk_v.data_dir(), "vdt_paper_trades.csv")
            if _os_v2.path.exists(_csv_p) and _cur_prof_real:
                with open(_csv_p, newline="") as _f_v2:
                    _rdr = _csv_v.DictReader(_f_v2)
                    # FILTER PODĽA PROFILU:
                    #   - exact match na profile → always accept
                    #   - sim profile + prázdny profile column → accept (legacy sim trades)
                    #   - inak skip (zabráni leakovaniu real trades do iného profilu)
                    for _row in _rdr:
                        _row_prof = str(_row.get("profile", "") or "")
                        if _row_prof == _cur_prof_real:
                            pass   # exact match → accept
                        elif _is_sim_profile and not _row_prof:
                            pass   # sim profile sees legacy empty-profile rows
                        else:
                            continue
                        _ts_r = str(_row.get("ts", ""))[:10]
                        if _ts_r != view_day:
                            continue
                        _sl2 = str(_row.get("slot", ""))
                        if "-" not in _sl2 or len(_sl2) < 5:
                            continue
                        try:
                            _h2 = int(_sl2[:2]); _m2 = int(_sl2[3:5])
                            _idx2 = (_h2 * 60 + _m2) // 15
                        except Exception:
                            continue
                        if not (0 <= _idx2 < 96):
                            continue
                        _kwh_r = float(_row.get("kwh", 0) or 0)
                        _act_r = str(_row.get("action", ""))
                        if _act_r == "discharge":
                            _vdt_realized96[_idx2] += _kwh_r
                        elif _act_r == "charge":
                            _vdt_realized96[_idx2] -= _kwh_r
        except Exception as _e_vr:
            print(f"[livesim chPlan VDT real] {_e_vr}")

        # Expandni 96-element kWh arrays na per-minute kW arrays
        # rovnakou cestou ako dview indexovaním zo slotu daného hodiny:minúty
        def _slot_kwh_to_min_kw(arr96, dview_times):
            _out = []
            for _t in dview_times:
                try:
                    _h_t = _t.hour; _m_t = _t.minute
                except Exception:
                    _out.append(0.0); continue
                _idx_t = (_h_t * 60 + _m_t) // 15
                _kwh_t = arr96[_idx_t] if 0 <= _idx_t < 96 else 0.0
                _out.append(_kwh_t * 4.0)   # 15-min kWh → kW
            return _out
        _vdt_plan_kw = _slot_kwh_to_min_kw(_vdt_planned96, dview["time"])
        _vdt_real_kw = _slot_kwh_to_min_kw(_vdt_realized96, dview["time"])
        # Aktuálna nominácia voči trhu = DAM (záväzná D-1) + VDT realizované (uzavreté trades).
        # VDT plán je IBA návrh z live_advisor — nie je commitment, takže sa NEPRIRÁTAVA
        # (inak by sa rovnaký obchod počítal 2× a graf by ukázal 2-3× vyšší výkon než batéria
        # vie poskytnúť).
        # Bug NOM-DOUBLE: VDT pripočítaj len keď _pg_kw je čistý DAM; pri fallbacku
        # (staré dáta bez plan_grid_dam_kwh) plan_grid_kwh už VDT obsahuje.
        if _pg_has_pure_dam:
            _dam_plus_vdt_kw = [
                (_nz(pg) + _nz(vr))
                for pg, vr in zip(_pg_kw, _vdt_real_kw)
            ]
        else:
            _dam_plus_vdt_kw = [_nz(pg) for pg in _pg_kw]
        # _actv = plán+RT pre celý deň (pre budúce minúty rt=0 → len plán). Predtým tu bolo
        # `if lv else NaN` čo orezávalo THR/DEV pri "teraz" — teraz to ide 0-24h.
        _actv = [(_nz(pb)+_nz(d)*_nz(p)/100.0*bkw)
                 for pb, d, p in zip(dview["plan_batt_kw"], dview["rt_dir"], dview["rt_power_pct"])]
        # _ftvv pre threshold = MINÚTOVÁ REALITA (zo scenára/ftv_minute, ak je v trace)
        # _ftvv pre tabuľku/popis = hodinová predikcia / scenár (stepwise)
        _ftv_real_for_thr = (dview["ftv_min_real_kw"].tolist()
                              if "ftv_min_real_kw" in dview.columns
                              else dview["ftv_kw"].tolist())
        _ftvv = [_nz(x) for x in dview["ftv_kw"]]                                              # hodinový (pre popis/tabuľku)
        _ftvv_real = [_nz(x) for x in _ftv_real_for_thr]                                       # minútová realita (pre threshold)
        _cuv = [_nz(x) for x in dview["plan_curtail_kwh"]]                                     # plánové orezanie (už v kW)
        # ── LOAD: minútová spotreba (= odber zákazníka) — odčíta sa z thresholdu ──
        # threshold (realita na prahu) = FTV_min_real − orezanie + battery − load_min_real
        _load_real_for_thr = (dview["load_min_real_kw"].tolist()
                               if "load_min_real_kw" in dview.columns
                               else [0.0] * len(dview))
        _load_plan_arr = (dview["load_plan_kw"].tolist()
                          if "load_plan_kw" in dview.columns
                          else [0.0] * len(dview))
        _loadv_real = [_nz(x) for x in _load_real_for_thr]
        _loadv_plan = [_nz(x) for x in _load_plan_arr]
        # Maska: future minúty (lv==0) majú THR/DEV NaN. SK aj CZ idú teraz cez identický RT engine.
        _thr1 = [((f - c + a - l) if (_np.isfinite(a) and (lv == 1)) else float('nan'))
                  for f, c, a, l, lv in zip(_ftvv_real, _cuv, _actv, _loadv_real, _lv)]
        # ── Realio override pre Sieť (Výkon na prahu) ─────────────────────────
        # **Konvencia: SIGN-FLIPPED ako Plán Obchodu** (užívateľ 2026-06-01 evening):
        #   - kladné = export do grid (sieť výstup z prahu — zhoda s Plán Obchodu)
        #   - záporné = import z grid (sieť vstup do prahu)
        # ELM1 raw má opačnú konvenciu (kladné=import); znamienko otáčame **iba pre chart**
        # aby porovnanie s Plán Obchodu bolo intuitívne (oba ukazujú "smer voči trhu").
        # KPI dlaždica vo Vizualizácii má raw konvenciu — to nie je zmenené.
        # **PRAVIDLO**: v realio_overlay móde **NIKDY** sim fallback pre Sieť.
        # Minúta bez DB záznamu = NaN (chart prerušený), žiadne miešanie reality so sim.
        if realio_overlay:
            if "realio_load_kw" in dview.columns:
                _rload_arr = list(dview["realio_load_kw"])
                _thr1 = [
                    (-float(rl) if (rl is not None and pd.notna(rl)) else float('nan'))
                    for rl in _rload_arr
                ]
            else:
                _thr1 = [float('nan')] * len(dview)
        _dev1 = [(t - p) if (_np.isfinite(t) and (lv == 1)) else float('nan')
                  for t, p, lv in zip(_thr1, _pg_kw, _lv)]
        _ts15v = list(dview["ts15"]) if "ts15" in dview.columns else [str(t)[:15] for t in dview["time"]]
        _aux = pd.DataFrame({"ts15": _ts15v, "thr": _thr1, "dev": _dev1, "pg": _pg_kw})
        # 15-min priebeh: v realio_overlay móde použij **priamy 15m tag z Bender**
        # (ELM1_Aggregated_C_Power_15m), rovnaký sign-flip ako 1m. Žiadny fallback.
        if realio_overlay:
            if "realio_load_kw_15m" in dview.columns:
                _rload15_arr = list(dview["realio_load_kw_15m"])
                _thr15 = [
                    (-float(rl15) if (rl15 is not None and pd.notna(rl15)) else float('nan'))
                    for rl15 in _rload15_arr
                ]
            else:
                _thr15 = [float('nan')] * len(dview)
        else:
            # Sim mode: pôvodný groupby mean z 1-min thr
            _thr15 = _aux.groupby("ts15")["thr"].transform("mean").tolist()
        # _dev15 je odvodené z _thr15 (priame z 15m tagu) − plán Obchodu
        _dev15 = [(t15 - p) if (_np.isfinite(t15) and (lv == 1)) else float('nan')
                   for t15, p, lv in zip(_thr15, _pg_kw, _lv)]
        PG = "[" + ",".join(_js(x) for x in _pg_kw) + "]"
        VDT_PLAN = "[" + ",".join(_js(x) for x in _vdt_plan_kw) + "]"
        VDT_REAL = "[" + ",".join(_js(x) for x in _vdt_real_kw) + "]"
        DAM_VDT = "[" + ",".join(_js(x) for x in _dam_plus_vdt_kw) + "]"
        THR1 = "[" + ",".join(_js(x) for x in _thr1) + "]"
        THR15 = "[" + ",".join(_js(x) for x in _thr15) + "]"
        DEV1 = "[" + ",".join(_js(x) for x in _dev1) + "]"
        DEV15 = "[" + ",".join(_js(x) for x in _dev15) + "]"
        _prov = r.get("prov_date"); _is_today = (str(view_day) == str(_prov)) if _prov else False
        _dtreal_src = dview["dt_real_eur"] if "dt_real_eur" in dview.columns else dview["dt_eur"]
        DTREAL = "[" + ",".join(_js(x) for x in _dtreal_src) + "]"
        VDT = "[" + ",".join(_js(x) for x in (dview["vdt_eur"] if "vdt_eur" in dview.columns
                                              else [float('nan')]*len(dview))) + "]"
        # ZCO = zúčtovacia cena odchýlky (ČEPS imbalance settlement). Pre dokončené dni je reálna,
        # pre dnešok provizórny odhad (kde už ČEPS zverejnil). Ak chýba → null v grafe.
        ZCO = "[" + ",".join(_js(x) for x in (dview["zco_eur"] if "zco_eur" in dview.columns
                                              else [float('nan')]*len(dview))) + "]"
        # — Horný graf: MW signál + pásma (ako v RT poradcovi) — vysvetľuje rozhodnutia odchýlky —
        chMW = (f"<h2>MW signál + pásma (deň {view_day})</h2>"
                f"<div style='height:300px'><canvas id='chMW'></canvas></div>"
                f"<script>new Chart(document.getElementById('chMW'),{{type:'line',data:{{labels:{Ld},datasets:["
                f"{{label:'MW signál',data:{MW},borderColor:'#C0392B',backgroundColor:'rgba(192,57,43,.10)',fill:true,pointRadius:0,borderWidth:1.4,tension:.2}},"
                f"{{label:'+vybíjacia hranica',data:{BD},borderColor:'#2E7D32',borderDash:[5,4],pointRadius:0,borderWidth:1}},"
                f"{{label:'−nabíjacia hranica',data:{BC},borderColor:'#1F4E78',borderDash:[5,4],pointRadius:0,borderWidth:1}},"
                f"{{label:'DT cena €/MWh',data:{DT},borderColor:'#15803d',borderDash:[2,3],pointRadius:0,borderWidth:1.3,yAxisID:'y1'}}"
                f"]}},options:{{responsive:true,maintainAspectRatio:false,interaction:{{mode:'index',intersect:false}},"
                f"elements:{{point:{{radius:0}}}},scales:{{y:{{title:{{display:true,text:'MW'}},grid:{{color:(c)=>c.tick.value===0?'#333':'#eee'}}}},"
                f"y1:{{position:'right',grid:{{drawOnChartArea:false}},title:{{display:true,text:'€/MWh'}}}}}}}}}});</script>")

        # chSEPSMW bol presunutý do /rt (RT poradca) — pozri seps_sk.load_sys_arrays_for_rt
        chSEPSMW = ""
        # — DT ceny: predikcia (čo sa nominovalo) / realita (clearing) / VDT (vnútrodenný) —
        # ZCO dataset spoločný (kreslí sa len kde nie je NaN/null — chart.js prerušuje líniu)
        _ds_zco = (f"{{label:'ZCO €/MWh (zúčtovacia cena odchýlky)',data:{ZCO},"
                   f"borderColor:'#7030A0',backgroundColor:'rgba(112,48,160,.06)',fill:false,"
                   f"borderDash:[1,2],pointRadius:0,borderWidth:1.5,spanGaps:false}}")
        if _is_today:
            _ds_dt = (f"{{label:'DT predikcia €/MWh',data:{DT},borderColor:'#15803d',borderDash:[3,3],pointRadius:0,borderWidth:1.6}},"
                      f"{{label:'DT realita (clearing) €/MWh',data:{DTREAL},borderColor:'#1F4E78',pointRadius:0,borderWidth:1.8}},"
                      f"{{label:'VDT €/MWh',data:{VDT},borderColor:'#E0A800',borderDash:[2,2],pointRadius:0,borderWidth:1.4}},"
                      f"{_ds_zco}")
        else:
            _ds_dt = (f"{{label:'DT realita €/MWh',data:{DT},borderColor:'#1F4E78',pointRadius:0,borderWidth:1.8}},"
                      f"{{label:'VDT €/MWh',data:{VDT},borderColor:'#E0A800',borderDash:[2,2],pointRadius:0,borderWidth:1.4}},"
                      f"{_ds_zco}")
        chDT = (f"<h2>DT ceny — predikcia / realita / VDT + ZCO (deň {view_day})</h2>"
                f"<div style='height:280px'><canvas id='chDT'></canvas></div>"
                f"<script>new Chart(document.getElementById('chDT'),{{type:'line',data:{{labels:{Ld},datasets:[{_ds_dt}"
                f"]}},options:{{responsive:true,maintainAspectRatio:false,interaction:{{mode:'index',intersect:false}},"
                f"layout:{{padding:{{right:55}}}},"   # zarovnanie šírky chart area s grafmi čo majú pravú os
                f"elements:{{point:{{radius:0}}}},scales:{{y:{{title:{{display:true,text:'€/MWh'}}}}}}}}}});</script>"
                f"<p style='color:#666;font-size:12px;margin:4px 0'>"
                f"<b>ZCO (fialová prerušovaná)</b> = zúčtovacia cena odchýlky z ČEPS/OTE. "
                f"Pre dokončené dni reálna; pre dnešok provizórna (čo už ČEPS publikoval; tam kde ešte nie, krivka chýba).</p>")
        # — Plán a realita výkonu: nominácia + výkon na prahu (FTV+batéria) + výsledná odchýlka (1-min aj 15-min) —
        if realio_overlay:
            _thr_desc = ("<b>🔴 Sieť 1-min</b> = ELM1_Aggregated_C_Power_1m "
                          "(Bender 1-min priemer); <b>15-min</b> = ELM1_Aggregated_C_Power_15m "
                          "(Bender 15-min priemer — fakturačné okno). "
                          "<b>Znamienko otočené pre porovnanie s Plán Obchodu</b>: "
                          "<u>kladné = export do grid</u> (zhoda s plánom), "
                          "<u>záporné = import z grid</u>. "
                          "(Vo Vizualizácii / KPI dlaždici je raw konvencia: kladné=import.) ")
            _thr1_lbl = "🔴 Sieť 1-min (ELM1 1m, otočené pre porovnanie)"
            _thr15_lbl = "🟢 Sieť 15-min (ELM1 15m — fakturačné okno)"
            _thr1_color = "#C62828"
            _thr1_width = 2.0
        else:
            _thr_desc = "<b>Realita na prahu</b> = FTV − orezanie + skutočná batéria. "
            _thr1_lbl = "Výkon na prahu 1-min (FTV+batéria)"
            _thr15_lbl = "Výkon na prahu 15-min"
            _thr1_color = "#7CB342"
            _thr1_width = 1
        chPlan = (f"<h2>Nominácia voči trhu + realita na prahu + skutočná odchýlka (deň {view_day})</h2>"
                f"<div style='height:360px'><canvas id='chPl'></canvas></div>"
                f"<script>new Chart(document.getElementById('chPl'),{{type:'line',data:{{labels:{Ld},datasets:["
                f"{{label:'DAM nominácia (D-1, záväzná)',data:{PG},borderColor:'#1F4E78',backgroundColor:'rgba(31,78,120,.10)',fill:true,stepped:true,pointRadius:0,borderWidth:2.2}},"
                # #29 (user 2026-06-18): graf zobrazuje LEN uzavreté VDT obchody — dataset
                # „VDT plán (návrh)" odstránený (návrh nie je uzavretý obchod, mätie).
                f"{{label:'VDT realizované (paper trades)',data:{VDT_REAL},borderColor:'#E65100',backgroundColor:'rgba(230,81,0,.15)',stepped:true,pointRadius:0,borderWidth:1.8}},"
                f"{{label:'Aktuálna nominácia (DAM + uzavreté VDT)',data:{DAM_VDT},borderColor:'#0D47A1',stepped:true,pointRadius:0,borderWidth:2.4}},"
                f"{{label:'Plán batérie kW (info)',data:{PB},borderColor:'#999',borderDash:[3,3],pointRadius:0,borderWidth:1,hidden:true}},"
                f"{{label:'{_thr1_lbl}',data:{THR1},borderColor:'{_thr1_color}',pointRadius:0,borderWidth:{_thr1_width},tension:.15}},"
                f"{{label:'{_thr15_lbl}',data:{THR15},borderColor:'#1B5E20',stepped:true,pointRadius:0,borderWidth:2.2}},"
                f"{{label:'Odchýlka voči Obchodu 1-min kW',data:{DEV1},borderColor:'#CE93D8',pointRadius:0,borderWidth:1,tension:.15}},"
                f"{{label:'Odchýlka voči Obchodu 15-min kW',data:{DEV15},borderColor:'#6A1B9A',stepped:true,pointRadius:0,borderWidth:1.8,borderDash:[4,2]}}"
                f"]}},options:{{responsive:true,maintainAspectRatio:false,interaction:{{mode:'index',intersect:false}},"
                f"layout:{{padding:{{right:55}}}},"   # zarovnanie šírky chart area s chMW/chRiadenie
                f"elements:{{point:{{radius:0}}}},scales:{{y:{{title:{{display:true,text:'kW'}},grid:{{color:(c)=>c.tick.value===0?'#333':'#eee'}}}}}}}}}});</script>"
                f"<p style='color:#666;font-size:12px;margin:4px 0'>"
                f"<b>DAM nominácia (D-1)</b> = pôvodný plán uzavretý deň vopred po OKTE DAM uzávierke "
                f"— záväzná voči trhu (svetlomodrá, vyplnená). "
                f"<b>VDT realizované</b> = uzavreté paper trades z <code>vdt_paper_trades.csv</code> (oranžová). "
                f"<b>Aktuálna nominácia (DAM + uzavreté VDT)</b> = pôvodná D-1 nominácia + všetky VDT trade-y "
                f"čo medzitým prešli (tmavomodrá) — toto musí batéria + FTV fyzicky trafiť aby nevznikla ZCO. "
                f"{_thr_desc}"
                f"<b>Odchýlka</b> = realita − Aktuálna nominácia "
                f"(záporná = nedodali sme, kladná = preplnili). "
                f"<b>Plán batérie</b> je iba interný (kliknutím na legendu zapni/vypni). "
                f"Kladné = predaj/export · záporné = nákup/import.</p>")
        # — Tok energie (stacked bars): FTV+Batt+Grid zdroje a Load+Curtail spotreba —
        # Energy balance: zdroje (hore +) = spotreba (dole −). Užívateľ vidí v každom slote
        # ako sa kombinujú zdroje (FTV produkcia, Batt vybíja, Grid import) a kam ide energia
        # (Load, Batt nabíja, Grid export, Curtail). Súčet pozitívnych = súčet negatívnych = balance.
        chFlow = (f"<h2>Tok energie — zdroje (+) vs spotreba (−) (deň {view_day})</h2>"
                f"<div style='height:340px'><canvas id='chFlow'></canvas></div>"
                f"<script>new Chart(document.getElementById('chFlow'),{{type:'bar',data:{{labels:{Ld},datasets:["
                # Zdroje hore (kladné)
                f"{{label:'FTV produkcia',data:{FLOW_FTV},backgroundColor:'rgba(255,193,7,.85)',borderColor:'#FFB300',borderWidth:0,stack:'flow'}},"
                f"{{label:'Batéria vybíja',data:{FLOW_BATT_DIS},backgroundColor:'rgba(46,125,50,.85)',borderColor:'#1B5E20',borderWidth:0,stack:'flow'}},"
                f"{{label:'Sieť import',data:{FLOW_GRID_IM},backgroundColor:'rgba(192,57,43,.85)',borderColor:'#922B21',borderWidth:0,stack:'flow'}},"
                # Spotreba dole (záporné)
                f"{{label:'Spotreba (Load)',data:{FLOW_LOAD},backgroundColor:'rgba(112,48,160,.85)',borderColor:'#5C2D8A',borderWidth:0,stack:'flow'}},"
                f"{{label:'Batéria nabíja',data:{FLOW_BATT_CHG},backgroundColor:'rgba(33,150,243,.85)',borderColor:'#1976D2',borderWidth:0,stack:'flow'}},"
                f"{{label:'Sieť export',data:{FLOW_GRID_EX},backgroundColor:'rgba(13,71,161,.85)',borderColor:'#0D47A1',borderWidth:0,stack:'flow'}},"
                f"{{label:'Orezanie (Curtail)',data:{FLOW_CURTAIL},backgroundColor:'rgba(158,158,158,.85)',borderColor:'#616161',borderWidth:0,stack:'flow'}}"
                f"]}},options:{{responsive:true,maintainAspectRatio:false,"
                f"interaction:{{mode:'index',intersect:false}},"
                f"layout:{{padding:{{right:55}}}},"
                f"plugins:{{tooltip:{{callbacks:{{footer:(items)=>{{"
                f"let pos=0,neg=0;items.forEach(i=>{{const v=Number(i.raw)||0;if(v>0)pos+=v;else neg+=v;}});"
                f"return 'Σ zdroje +'+pos.toFixed(1)+' kW · Σ spotreba '+neg.toFixed(1)+' kW';}}}}}}}},"
                f"scales:{{x:{{stacked:true}},"
                f"y:{{stacked:true,title:{{display:true,text:'kW'}},grid:{{color:(c)=>c.tick.value===0?'#333':'#eee'}}}}}}}}}});</script>"
                f"<p style='color:#666;font-size:12px;margin:4px 0'>"
                f"<b>Hore (zdroje +)</b>: FTV produkcia 🟡 · Batéria vybíja 🟢 · Sieť import 🔴. "
                f"<b>Dole (spotreba −)</b>: Spotreba zákazníka 🟣 · Batéria nabíja 🔵 · Sieť export 🔷 · Orezanie ⚫. "
                f"Energia balance: <i>Σ zdroje = Σ spotreba</i> (každú minútu/slot). "
                f"Kliknutím v legende skry/zobraz dataset.</p>")
        # — Riadenie batérie: fyzická akcia (plán+RT) + odchýlka + SOC —
        # Tlačidlo "📤 Export 15-min na Bender" iba v realio_overlay móde (= reálny profil)
        _export_btn = ""
        if realio_overlay:
            _export_btn = (
                f"<a href='/realio/batt_plan_export?day={view_day}' target='_top' "
                f"style='background:#C62828;color:#fff;padding:6px 14px;border-radius:8px;"
                f"text-decoration:none;font-weight:600;font-size:13px;margin-left:14px' "
                f"title='Pošle 96 setpointov pre vybraný deň priamo na Bender (s preview)'>"
                f"📤 Export 15-min riadenia na Bender</a>"
            )
        # Bug SS (2026-06-08): pridať vizualizáciu AKTUÁLNEJ HODINY v grafe (žltý band)
        # + display aktuálnej hodnoty batt výkonu + SOC v nadpise.
        # Funguje IBA pre dnešný deň (view_day == dnes); pre historické dni žiadny highlight.
        _now_idx = -1                 # index v Ld array pre aktuálnu minútu
        _hour_start_idx = -1          # index začiatok aktuálnej hodiny
        _hour_end_idx = -1            # index koniec aktuálnej hodiny
        _now_batt_str = "—"
        _now_soc_str = "—"
        try:
            _today_str = dt.date.today().isoformat()
            if str(view_day) == _today_str:
                _now_dt = dt.datetime.now()
                _now_minute = _now_dt.hour * 60 + _now_dt.minute
                # Hľadaj index v dview["time"] kde minute matchuje
                _times = pd.to_datetime(dview["time"]).dt.hour * 60 + pd.to_datetime(dview["time"]).dt.minute
                _matches = (_times <= _now_minute)
                if _matches.any():
                    _now_idx = int(_matches.cumsum().iloc[-1] - 1)
                    # Hour boundaries
                    _h_start = _now_dt.hour * 60
                    _h_end = _h_start + 60
                    _hs_mask = (_times <= _h_start)
                    _he_mask = (_times <= _h_end)
                    _hour_start_idx = int(_hs_mask.cumsum().iloc[-1] - 1) if _hs_mask.any() else 0
                    _hour_end_idx = int(_he_mask.cumsum().iloc[-1] - 1) if _he_mask.any() else len(dview) - 1
                    # Aktuálne hodnoty
                    _row_now = dview.iloc[_now_idx]
                    _batt_now = float(_act_per_min[_now_idx]) if _now_idx < len(_act_per_min) else 0.0
                    _soc_now = float(_row_now.get("soc_pct", 0))
                    _now_batt_str = f"{_batt_now:+.0f} kW"
                    _now_soc_str = f"{_soc_now:.1f}%"
        except Exception:
            pass
        # Banner s aktuálnou hodnotou pre dnešok
        _now_banner = ""
        if _now_idx >= 0:
            _now_banner = (
                f"<span style='display:inline-block;margin-left:14px;padding:6px 14px;"
                f"background:#FFEB3B;border-radius:8px;font-size:14px;font-weight:600;"
                f"color:#5D4037'>"
                f"⏱ TERAZ {dt.datetime.now().strftime('%H:%M')}: "
                f"<b style='color:#1B5E20'>{_now_batt_str}</b> · "
                f"SOC <b style='color:#E65100'>{_now_soc_str}</b>"
                f"</span>"
            )
        # JS plugin pre žltý band aktuálnej hodiny
        _curhour_plugin = ""
        if _hour_start_idx >= 0 and _hour_end_idx > _hour_start_idx:
            _curhour_plugin = (
                f"{{id:'currentHour',beforeDraw:(c)=>{{const xs=c.scales.x,a=c.chartArea;"
                f"if(!xs||!a)return;const x1=xs.getPixelForValue({_hour_start_idx}),"
                f"x2=xs.getPixelForValue({_hour_end_idx});c.ctx.save();"
                f"c.ctx.fillStyle='rgba(255,235,59,0.18)';"
                f"c.ctx.fillRect(x1,a.top,x2-x1,a.bottom-a.top);"
                f"c.ctx.strokeStyle='rgba(245,127,23,0.55)';c.ctx.lineWidth=1.5;"
                f"c.ctx.beginPath();c.ctx.moveTo(x1,a.top);c.ctx.lineTo(x1,a.bottom);"
                f"c.ctx.moveTo(x2,a.top);c.ctx.lineTo(x2,a.bottom);c.ctx.stroke();"
                f"c.ctx.restore();}}}}"
            )
        chRiadenie = (f"<h2>Riadenie batérie — predikcia (plán+RT) vs realita + SOC (deň {view_day}){_export_btn}{_now_banner}</h2>"
                f"<div style='height:360px'><canvas id='chRi'></canvas></div>"
                f"<script>new Chart(document.getElementById('chRi'),{{type:'line',data:{{labels:{Ld},datasets:["
                f"{{label:'Plán batérie (D-1 + VDT, kW)',data:{AP},borderColor:'#37474F',borderWidth:1.4,borderDash:[2,3],fill:false,stepped:true,pointRadius:0,tension:.0}},"
                f"{{label:'Batéria PREDIKCIA kW (plán+RT, 1-min, post-cap)',data:{AC},borderColor:'#2E7D32',backgroundColor:'rgba(46,125,50,.08)',fill:true,stepped:true,pointRadius:0,borderWidth:1.8}},"
                f"{{label:'Batéria PREDIKCIA kW (15-min agregát)',data:{AC_AGG},borderColor:'#1565C0',borderDash:[6,3],fill:false,stepped:true,pointRadius:0,borderWidth:2.2}},"
                f"{{label:'🔴 Batéria REÁLNE MERANIE kW',data:{BATT_REAL},borderColor:'#C62828',backgroundColor:'rgba(198,40,40,.0)',fill:false,pointRadius:0,borderWidth:2.2,tension:.15}},"
                f"{{label:'RT odchýlka kW',data:{RK},borderColor:'#7030A0',backgroundColor:'rgba(112,48,160,.12)',fill:true,stepped:true,pointRadius:0,borderWidth:1.2}},"
                f"{{label:'SOC PLÁN % (D-1 + VDT, full-day)',data:{SOP},borderColor:'#9E9E9E',borderWidth:1.4,borderDash:[2,3],pointRadius:0,yAxisID:'y2',tension:.1}},"
                f"{{label:'SOC PREDIKCIA % (realita post-cap)',data:{SO},borderColor:'#C49000',borderWidth:1.8,borderDash:[5,3],pointRadius:0,yAxisID:'y2'}},"
                f"{{label:'🔴 SOC REÁLNE MERANIE %',data:{SOC_REAL},borderColor:'#E65100',borderWidth:2.0,pointRadius:0,yAxisID:'y2',tension:.15}}"
                f"]}},options:{{responsive:true,maintainAspectRatio:false,interaction:{{mode:'index',intersect:false}},"
                f"elements:{{point:{{radius:0}}}},scales:{{y:{{title:{{display:true,text:'kW'}},grid:{{color:(c)=>c.tick.value===0?'#333':'#eee'}}}},"
                f"y2:{{position:'right',min:0,max:100,grid:{{drawOnChartArea:false}},title:{{display:true,text:'SOC %'}}}}}}}}"
                + (f",plugins:[{_curhour_plugin}]" if _curhour_plugin else "")
                + f"}});</script>")
        # — Minútová realita FTV — priamo z trace (uložená v livesim.py ako ftv_min_real_kw)
        # Toto je TÁ ISTÁ krivka ktorú batéria naozaj použila v RT enginu.
        # Ak trace stĺpec nie je k dispozícii (starší CSV), vygeneruje sa on-the-fly.
        if "ftv_min_real_kw" in dview.columns:
            FTM = FTM_FROM_TRACE
        else:
            try:
                import ftv_minute as _fm
                _ftv_min_arr = _np.asarray(_ftvv, float)
                if _ftv_min_arr.size == 1440:
                    _hr = _ftv_min_arr.reshape(24, 60).mean(axis=1)
                else:
                    _hr = _np.array([_ftv_min_arr[i*max(_ftv_min_arr.size//24,1):(i+1)*max(_ftv_min_arr.size//24,1)].mean()
                                     if _ftv_min_arr.size >= 24 else 0.0 for i in range(24)])
                _fm_arr = _fm.hourly_to_minute(_hr)
                if _fm_arr.size != _ftv_min_arr.size:
                    _idx = _np.linspace(0, _fm_arr.size - 1, _ftv_min_arr.size).round().astype(int)
                    _fm_arr = _fm_arr[_idx]
                FTM = "[" + ",".join(_js(x) for x in _fm_arr) + "]"
            except Exception:
                FTM = "[" + ",".join(["null"] * len(_ftvv)) + "]"
        # tlačidlo na editáciu scenára + indikátor či už scenár existuje
        # (skryje sa v realio_overlay móde — pri reálnom meraní nemá zmysel editovať
        # simuláciu FTV)
        try:
            _has_sc = (fs is not None and fs.has_scenario(str(view_day)))
        except Exception:
            _has_sc = False
        if realio_overlay:
            _sc_btn = ("<span style='background:#37474F;color:#fff;padding:4px 11px;border-radius:7px;"
                        "font-weight:600;font-size:13px;margin-left:10px' "
                        "title='V Reálnom riadení sa FTV berie z merania — editor scenára je skrytý'>"
                        "🔴 FTV: reálne meranie</span>")
        else:
            _sc_btn = (f"<a href='/ftv_scenario?date={view_day}' "
                       f"style='background:{'#2E7D32' if _has_sc else '#5E35B1'};color:#fff;padding:4px 11px;"
                       f"border-radius:7px;text-decoration:none;font-weight:600;font-size:13px;margin-left:10px'>"
                       f"{'💾 Upraviť scenár (uložený)' if _has_sc else '🎨 Upraviť FTV scenár'}</a>")
        # V realio_overlay móde vynechávame FTM dataset (minútová sim realita) — máme
        # priamo reálne meranie z merača. V sim móde nech ostáva ako bol.
        if realio_overlay:
            _ftm_ds = ""
            _ftm_legend = ""
        else:
            _ftm_ds = (f"{{label:'FTV minútová realita kW (búrlivý profil)',data:{FTM},"
                       f"borderColor:'#F57F17',backgroundColor:'rgba(245,127,23,.0)',"
                       f"fill:false,pointRadius:0,borderWidth:1,tension:.15}},")
            _ftm_legend = ("<b>Minútová realita (oranžová)</b> = umelo vygenerovaný priebeh "
                            "s mračnovým šumom (AR(1), σ≈15 %), hodinová energia zachovaná. ")
        chF = (f"<h2>Výkon FTV + orezanie + minútová realita (deň {view_day}){_sc_btn}</h2>"
               f"<div style='height:380px'><canvas id='chF'></canvas></div>"
               f"<script>new Chart(document.getElementById('chF'),{{type:'line',data:{{labels:{Ld},datasets:["
               f"{{label:'FTV pôvodný plán kW (PVF predikcia, referencia)',data:{FT_ORIG},borderColor:'#888',borderDash:[6,4],fill:false,stepped:true,pointRadius:0,borderWidth:1.4}},"
               f"{{label:'FTV hodinová (scenár alebo plán) kW',data:{FT},borderColor:'#E0A800',backgroundColor:'rgba(224,168,0,.18)',fill:true,stepped:true,pointRadius:0,borderWidth:1.5}},"
               f"{_ftm_ds}"
               f"{{label:'🔴 FTV REÁLNE MERANIE kW',data:{FT_REALIO},borderColor:'#C62828',backgroundColor:'rgba(198,40,40,.10)',fill:false,pointRadius:0,borderWidth:2.4,tension:.15}},"
               f"{{label:'Orezané kW',data:{CU},borderColor:'#C0392B',backgroundColor:'rgba(192,57,43,.25)',fill:true,stepped:true,pointRadius:0,borderWidth:1.5}}"
               f"]}},options:{{responsive:true,maintainAspectRatio:false,interaction:{{mode:'index',intersect:false}},"
               f"layout:{{padding:{{right:55}}}},"   # zarovnanie šírky chart area s chMW/chRiadenie
               f"elements:{{point:{{radius:0}}}},scales:{{y:{{title:{{display:true,text:'kW'}},grid:{{color:(c)=>c.tick.value===0?'#333':'#eee'}}}}}}}}}});</script>"
               f"<p style='color:#666;font-size:12px;margin:4px 0'>"
               f"<b>Pôvodný plán (sivá prerušovaná)</b> = pôvodná PVF predikcia ako bola nominovaná D-1. "
               f"<b>Hodinová (žltá)</b> = aktuálne použitá hodinová krivka (scenár ak je uložený, inak plán). "
               f"{_ftm_legend}"
               f"<b>🔴 FTV reálne meranie (červená)</b> = živé meranie z realio CSV (Bender). "
               f"Ak scenár nie je aktívny, sivá a žltá sa prekrývajú.</p>")
        # Bug DD (2026-06-07): 15-min agregát tabuľka — user chce prehľad batt aktivity per slot
        # (D-1 + VDT) + SOC + práca v kWh. Nahradzuje 1-min "posledné minúty" tabuľku.
        try:
            _tdf = dview.copy()
            # Slot key: floor 15 min
            _tdf["_slot_ts"] = pd.to_datetime(_tdf["time"]).dt.floor("15min")
            agg_cols = {
                "plan_batt_kw": "mean",       # priemerný batt kW v slot-e
                "soc_pct": "last",            # SOC na konci slotu
                "ftv_kw": "mean",
                "dt_eur": "mean",             # DT predikcia €/MWh
            }
            # Doplň VDT/DAM komponenty ak existujú (Bug V/X)
            if "plan_batt_dam_kw" in _tdf.columns:
                agg_cols["plan_batt_dam_kw"] = "mean"
            if "plan_batt_vdt_kw" in _tdf.columns:
                agg_cols["plan_batt_vdt_kw"] = "mean"
            # Bug EE: pridať DT realita (clearing) + VDT cenu
            if "dt_real_eur" in _tdf.columns:
                agg_cols["dt_real_eur"] = "mean"
            if "vdt_eur" in _tdf.columns:
                agg_cols["vdt_eur"] = "mean"
            _agg = _tdf.groupby("_slot_ts").agg(agg_cols).reset_index()
            # Práca = priemerný kW × 0.25 h
            _agg["work_kwh"] = _agg["plan_batt_kw"] * 0.25
            # Akcia label
            def _act_lbl(kw):
                if kw > 1.0: return ("🔴 VYBÍJAŤ", "#C62828")
                if kw < -1.0: return ("🟢 NABÍJAŤ", "#2E7D32")
                return ("⊙ idle", "#999")
            # Bug GG/LL/PP/QQ: VDT cena z paper trades + OKTE fallback.
            # Bug QQ (2026-06-08): VDT cena v tabulke ma byt EXECUTED paper trade
            # price (cena za aku sa obchod uzavrel — kazdy paper trade ma vlastnu
            # price_predicted_eur), NIE OKTE clearing. To preto, ze v paper tradingu
            # kazdy trade ma vlastnu cenu, ovela presnejsiu nez priemer cez vsetky
            # ucastnikov trhu. OKTE clearing pouzivame iba ako fallback (D-1 a starsie).
            #
            # Poradie zdrojov:
            #   1. vdt_paper_trades.csv per slot weighted-mean cena (executed)
            #   2. OKTE final_15m (clearing, T-1 = real)
            #   3. OKTE preliminary (broken na 0, ale future-proof fallback)
            # VDT-PRICE-EXEC (2026-06-15, revert VDT-PRICE-MARKET): VDT €/MWh stĺpec =
            # EXEKUČNÁ cena paper tradov per slot (vážený priemer price_predicted_eur),
            # "—" ak v slote nebol VDT obchod. User chce overiť ZA AKÚ CENU sa reálne
            # kupovalo/predávalo. Trhová OKTE referencia mýlila — pre dnešok je len
            # predbežná a ukazuje nezmysly (napr. 300 €/MWh na poludnie keď DAM=3).
            try:
                import vdt_live_advisor as _vla_t
                import csv as _csv_t
                _ptp = (_vla_t.paper_trades_csv_path(_active_profile_eff)
                        if _active_profile_eff else None)
                _exec_pw = {}   # "HH:MM" -> [sum(price*kwh), sum(kwh)]
                if _ptp and os.path.exists(_ptp):
                    _vd_iso = str(view_day)[:10]
                    with open(_ptp, encoding="utf-8", newline="") as _f:
                        for _row in _csv_t.DictReader(_f):
                            if str(_row.get("profile") or "") != _active_profile_eff:
                                continue
                            if str(_row.get("ts", ""))[:10] != _vd_iso:
                                continue
                            if str(_row.get("action", "")).upper() not in (
                                    "BUY", "CHARGE", "SELL", "DISCHARGE"):
                                continue
                            try:
                                _kwh_t = abs(float(_row.get("kwh") or 0))
                                _pr_t = float(_row.get("price_predicted_eur") or 0)
                                _sl_t = str(_row.get("slot", ""))
                                _key_t = f"{int(_sl_t[:2]):02d}:{int(_sl_t[3:5]):02d}"
                            except (TypeError, ValueError):
                                continue
                            if _kwh_t <= 0:
                                continue
                            _acc = _exec_pw.setdefault(_key_t, [0.0, 0.0])
                            _acc[0] += _pr_t * _kwh_t
                            _acc[1] += _kwh_t
                # Override vdt_eur stĺpec: exekučná cena kde bol obchod, inak 0 → "—"
                def _vdt_exec_from_slot(ts):
                    try:
                        _t = pd.Timestamp(ts)
                        _a = _exec_pw.get(f"{_t.hour:02d}:{_t.minute:02d}")
                        return (_a[0] / _a[1]) if (_a and _a[1] > 0) else 0.0
                    except Exception:
                        return 0.0
                _agg["vdt_eur"] = _agg["_slot_ts"].map(_vdt_exec_from_slot)
            except Exception:
                pass
            # Bug GG: query param ?table_offset=N + ?table_rows=M pre scroll do minulosti
            _t_off = max(0, int(table_offset or 0))
            _t_rows = max(5, min(96, int(table_rows or 20)))
            _case_nav = r.get('case', 'plan_d1') if isinstance(r, dict) else 'plan_d1'   # B (#27): pre navigáciu dátum-pickera
            # Najnovších N slotov posunutých o offset (offset=0 → najnovšie, offset=20 → predošlých 20)
            _agg_all = _agg.copy()
            if _t_off > 0 and len(_agg) > _t_off:
                _agg = _agg.iloc[:-_t_off]
            _agg = _agg.tail(_t_rows)
            trows = ""
            for _, x in _agg.iterrows():
                _act, _col = _act_lbl(float(_nz(x.plan_batt_kw)))
                _slot_start = str(x._slot_ts)[11:16]
                _slot_end = (pd.Timestamp(x._slot_ts) + pd.Timedelta(minutes=15)).strftime("%H:%M")
                _dam_cell = (f"<td style='text-align:right'>{_nz(x.get('plan_batt_dam_kw', 0)):+.0f}</td>"
                              if "plan_batt_dam_kw" in _agg.columns
                              else "<td style='color:#999'>—</td>")
                _vdt_cell = (f"<td style='text-align:right;color:#E65100'>{_nz(x.get('plan_batt_vdt_kw', 0)):+.0f}</td>"
                              if "plan_batt_vdt_kw" in _agg.columns
                              else "<td style='color:#999'>—</td>")
                # Bug EE: DT predikcia/realita + VDT €/MWh
                _dt_pred = _nz(x.get('dt_eur', 0))
                _dt_real = _nz(x.get('dt_real_eur', 0)) if 'dt_real_eur' in _agg.columns else None
                _vdt_p = _nz(x.get('vdt_eur', 0)) if 'vdt_eur' in _agg.columns else None
                _dt_cell = (f"<td style='text-align:right'>{_dt_real:+.0f}</td>"
                            if _dt_real is not None and abs(_dt_real) > 0.01
                            else f"<td style='text-align:right;color:#888'>{_dt_pred:+.0f}*</td>")
                _vdt_eur_cell = (f"<td style='text-align:right;color:#E65100'>{_vdt_p:+.0f}</td>"
                                  if _vdt_p is not None and abs(_vdt_p) > 0.01
                                  else "<td style='color:#bbb;text-align:right'>—</td>")
                _slot_date = str(x._slot_ts)[:10]
                trows += (f"<tr>"
                           f"<td style='color:#888;font-size:11px'>{_slot_date}</td>"
                           f"<td>{_slot_start}–{_slot_end}</td>"
                           f"<td style='color:{_col};font-weight:600'>{_act}</td>"
                           f"<td style='text-align:right;font-weight:700'>{_nz(x.plan_batt_kw):+.0f}</td>"
                           f"{_dam_cell}{_vdt_cell}"
                           f"<td style='text-align:right'>{_nz(x.work_kwh):+.1f}</td>"
                           f"<td style='text-align:right;font-weight:600'>{_nz(x.soc_pct):.1f}%</td>"
                           f"<td style='text-align:right'>{_nz(x.ftv_kw):.0f}</td>"
                           f"{_dt_cell}{_vdt_eur_cell}"
                           f"</tr>")
            table = (f"<h2>Posledných 20 slotov (15-min · deň {view_day})</h2>"
                     f"<div class='wrap'>"
                     f"<table style='font-size:13px'>"
                     f"<tr style='background:#1F4E78;color:#fff'>"
                     f"<th>dátum</th><th>slot</th><th>akcia</th><th title='D-1 + VDT'>batt kW</th>"
                     f"<th title='D-1 plán'>z DAM</th>"
                     f"<th title='VDT realized'>z VDT</th>"
                     f"<th title='kWh za 15 min slot (+ vybíjanie, − nabíjanie)'>práca kWh</th>"
                     f"<th>SOC %</th><th>FTV kW</th>"
                     f"<th title='DT clearing €/MWh (realita ak je, inak predikcia*)'>DT €/MWh</th>"
                     f"<th title='VDT cena €/MWh'>VDT €/MWh</th>"
                     f"</tr>{trows}</table></div>"
                     f"<p style='color:#666;font-size:11px;margin:4px 0'>"
                     f"<b>batt kW</b> = priemerný setpoint za slot (D-1 + VDT). "
                     f"<b>z DAM</b> = D-1 plán. <b>z VDT</b> = intraday paper trades (oranžová). "
                     f"<b>práca</b> = kWh ktoré batt dodala (+) alebo prijala (−) za slot. "
                     f"<b>SOC</b> = stav na konci slotu. "
                     f"<b>DT</b> = clearing cena (* = predikcia, ak realita nie je). "
                     f"<b>VDT</b> = OKTE VDT cena.</p>"
                     # Bug GG: navigácia + CSV export
                     f"<div style='display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap'>"
                     f"<form method='get' style='display:inline-flex;gap:6px;align-items:center'>"
                     f"<input type='hidden' name='start' value='{view_day or ''}'>"
                     f"<input type='hidden' name='case' value='{r.get('case', 'plan_d1') if isinstance(r, dict) else 'plan_d1'}'>"
                     # B (#27): prezeranie iných dní — PRIAMA navigácia (len view+case, bez
                     # pinovania `start` na zobrazený deň; inak by skoršie dni vypadli z rozsahu
                     # a tabuľka by zmizla). Dáta sú v load_series (celá história), view filtruje deň.
                     f"<label style='font-size:12px'>Deň:</label>"
                     f'<input type="date" value="{view_day or ""}" '
                     f'onchange="window.location.href=\'/livesim?case={_case_nav}&view=\'+encodeURIComponent(this.value)" '
                     f'style="font-size:12px;padding:3px;border:1px solid #ccc;border-radius:4px">'
                     f"<label style='font-size:12px;margin-left:8px'>Posunúť späť:</label>"
                     f"<button type='submit' name='table_offset' value='{_t_off + _t_rows}' "
                     f"style='padding:4px 8px;font-size:12px'>← Predošlých {_t_rows}</button>"
                     + (f"<button type='submit' name='table_offset' value='{max(0, _t_off - _t_rows)}' "
                        f"style='padding:4px 8px;font-size:12px'>Ďalších {_t_rows} →</button>"
                        if _t_off > 0 else "")
                     + (f"<button type='submit' name='table_offset' value='0' "
                        f"style='padding:4px 8px;font-size:12px;background:#1F4E78;color:#fff;border:0;border-radius:4px'>Najnovšie</button>"
                        if _t_off > 0 else "")
                     + f"<label style='font-size:12px;margin-left:12px'>Počet slotov:</label>"
                     f"<select name='table_rows' onchange='this.form.submit()' style='font-size:12px;padding:3px'>"
                     + "".join(f"<option value='{n}' {'selected' if n == _t_rows else ''}>{n}</option>"
                                for n in [10, 20, 40, 96])
                     + f"</select>"
                     f"</form>"
                     f"<a href='/livesim_table_xlsx?case={r.get('case', 'plan_d1') if isinstance(r, dict) else 'plan_d1'}&day={view_day}' "
                     f"style='padding:4px 10px;background:#2E7D32;color:#fff;text-decoration:none;border-radius:4px;font-size:12px' "
                     f"download>📊 Export celý deň (Excel)</a>"
                     f"<span style='color:#888;font-size:11px;margin-left:6px'>"
                     f"Slot {_t_off + 1}–{_t_off + _t_rows} z {len(_agg_all)}</span>"
                     f"</div>")
        except Exception as _e_t15:
            table = f"<p style='color:#C62828'>15-min tabuľka zlyhala: {_e_t15}</p>"
    else:
        chMW = chDT = chPlan = chFlow = chRiadenie = chF = table = "<p style='color:#888'>Pre zvolený deň nie sú dáta.</p>"
        chSEPSMW = ""                                                  # SK-only chart, no data → empty
    # --- kumulatívny zisk za celé obdobie ---
    if dfull is not None and not dfull.empty:
        # DB unify F4: chC kumulatív X-axis = denný grid z effect_db (1 SQL query).
        # Predtým: minútový grid s 60000+ bodmi a 3 paralelné výpočty.
        # Teraz: denná granularita, jediný SQL filter joint LP, zhoda s kartami.
        _cum_df_db = None
        try:
            if _active_profile_eff and _eff_db_from and _eff_db_to:
                from core import effect_db as _eff_db_cum
                _cum_df_db = _eff_db_cum.get_period_cum_series(
                    _active_profile_eff, _eff_db_from, _eff_db_to,
                    joint_flags=_eff_joint)
        except Exception as _e_cum:
            print(f"[chC F4] get_period_cum_series zlyhal: {_e_cum}")
            _cum_df_db = None
        if _cum_df_db is not None and not _cum_df_db.empty:
            L = "[" + ",".join(f"'{d}'" for d in _cum_df_db["date"]) + "]"
            CT = "[" + ",".join(f"{x:.2f}" for x in _cum_df_db["cum_total"]) + "]"
            CD = "[" + ",".join(f"{x:.2f}" for x in _cum_df_db["cum_dt"]) + "]"
            CR = "[" + ",".join(f"{x:.2f}" for x in _cum_df_db["cum_rt"]) + "]"
            # VDT-IN-CHART (2026-06-15): VDT séria do grafu „Zisk za obdobie" (kumulatív z effect_db)
            _cv = _cum_df_db["cum_vdt"] if "cum_vdt" in _cum_df_db.columns else (_cum_df_db["cum_total"] * 0)
            CV = "[" + ",".join(f"{x:.2f}" for x in _cv) + "]"
        else:
            # Fallback: minútový grid z CSV (legacy, pre profily bez DB záznamov)
            L = "[" + ",".join(f"'{str(t)[5:16]}'" for t in dfull["time"]) + "]"
            CT = "[" + ",".join(f"{x:.2f}" for x in dfull["cum_total"].fillna(0)) + "]"
            CD = "[" + ",".join(f"{x:.2f}" for x in dfull["cum_dt"].fillna(0)) + "]"
            CR = "[" + ",".join(f"{x:.2f}" for x in dfull["cum_rt"].fillna(0)) + "]"
            CV = ("[" + ",".join(f"{x:.2f}" for x in dfull["cum_vdt_arb"].fillna(0)) + "]"
                  if "cum_vdt_arb" in dfull.columns else "[" + ",".join(["0"] * len(dfull)) + "]")
        # ── BASELINE per minúta (kumulatívne) — z DECIMOVANÝCH dát len pre CHART display ──
        # Pre PRESNÉ agregáty (denné, total) používame dfull_full nižšie.
        _bl_per_min = None
        try:
            _pui_plan = _ui_load("plan", {}) or {}
            _bp = bc.parse_baseline_params(_pui_plan) if bc is not None else None
            if _bp is not None:
                _ftv_min = pd.to_numeric(dfull.get("ftv_min_real_kw", dfull.get("ftv_kw")), errors="coerce").fillna(0).values
                _load_min = pd.to_numeric(dfull.get("load_min_real_kw", pd.Series([0.0]*len(dfull))), errors="coerce").fillna(0).values
                _dtp = pd.to_numeric(dfull.get("dt_real_eur", dfull.get("dt_eur")), errors="coerce").fillna(0).values
                _net = _ftv_min - _load_min
                _ex_kwh = np.maximum(_net, 0.0) / 60.0
                _im_kwh = -np.minimum(_net, 0.0) / 60.0
                _p_imp = (_dtp * _bp["im_val"]) if _bp["im_mode"] == "dt_x" else np.full_like(_dtp, _bp["im_val"])
                _p_exp = (_dtp * _bp["ex_val"]) if _bp["ex_mode"] == "dt_x" else np.full_like(_dtp, _bp["ex_val"])
                _bl_per_min = (_ex_kwh * _p_exp / 1000.0) - (_im_kwh * _p_imp / 1000.0)
        except Exception:
            _bl_per_min = None
        # CB pre kumulatívny chart: získame baseline cum z FULL data a sample-ujeme na časy dfull
        # (chart display zostáva decimovaný kvôli performance, ale Y-hodnoty sú presné).
        CB = "[" + ",".join(["null"] * len(dfull)) + "]"
        if dfull_full is not None and not dfull_full.empty and bc is not None:
            try:
                _pui_plan2 = _ui_load("plan", {}) or {}
                _bp2 = bc.parse_baseline_params(_pui_plan2)
                _ftv_f = pd.to_numeric(dfull_full.get("ftv_min_real_kw", dfull_full.get("ftv_kw")), errors="coerce").fillna(0).values
                _load_f = pd.to_numeric(dfull_full.get("load_min_real_kw", pd.Series([0.0]*len(dfull_full))), errors="coerce").fillna(0).values
                _dt_f = pd.to_numeric(dfull_full.get("dt_real_eur", dfull_full.get("dt_eur")), errors="coerce").fillna(0).values
                _net_f = _ftv_f - _load_f
                _ex_f = np.maximum(_net_f, 0.0) / 60.0
                _im_f = -np.minimum(_net_f, 0.0) / 60.0
                _pi_f = (_dt_f * _bp2["im_val"]) if _bp2["im_mode"] == "dt_x" else np.full_like(_dt_f, _bp2["im_val"])
                _pe_f = (_dt_f * _bp2["ex_val"]) if _bp2["ex_mode"] == "dt_x" else np.full_like(_dt_f, _bp2["ex_val"])
                _bl_full = (_ex_f * _pe_f / 1000.0) - (_im_f * _pi_f / 1000.0)
                _cum_bl_full = np.cumsum(_bl_full)
                # sample na časy dfull (decimovaný) — merge by time
                _idx_full = pd.DataFrame({"time": pd.to_datetime(dfull_full["time"]), "cb": _cum_bl_full})
                _idx_dec = pd.DataFrame({"time": pd.to_datetime(dfull["time"])})
                _cb_decim = pd.merge_asof(_idx_dec.sort_values("time"), _idx_full.sort_values("time"),
                                            on="time", direction="nearest")["cb"].fillna(0).values
                CB = "[" + ",".join(f"{x:.2f}" for x in _cb_decim) + "]"
                # store full baseline series per-minute pre Po dňoch nižšie
                dfull_full = dfull_full.copy()
                dfull_full["_bl"] = _bl_full
            except Exception:
                pass
        # ── DENNÉ agregáty z PLNEJ resolution (presné, nie decimované) ──
        if dfull_full is not None and not dfull_full.empty:
            _src_for_daily = dfull_full
            if "_bl" not in _src_for_daily.columns:
                _src_for_daily = _src_for_daily.copy(); _src_for_daily["_bl"] = 0.0
        else:
            _src_for_daily = dfull.copy()
            _src_for_daily["_bl"] = _bl_per_min if _bl_per_min is not None else 0.0
        # DB unify F4: chC "Po dňoch" cez effect_db.get_daily_series (1 SQL).
        # Fallback na CSV agregát ak DB nemá dáta (žiadny backfill spustený).
        _daily_db = None
        try:
            if _active_profile_eff and _eff_db_from and _eff_db_to:
                from core import effect_db as _eff_db_d
                _ddf = _eff_db_d.get_daily_series(
                    _active_profile_eff, _eff_db_from, _eff_db_to,
                    joint_flags=_eff_joint)
                if not _ddf.empty:
                    _daily_db = _ddf
        except Exception as _e_d:
            print(f"[chC F4] get_daily_series zlyhal: {_e_d}")
            _daily_db = None
        if _daily_db is not None:
            # Pripoj baseline z CSV agregátu (DB má len lokálny effect_daily baseline)
            _bl_by_day = (_src_for_daily.groupby("date")["_bl"].sum().reset_index()
                           if "_bl" in _src_for_daily.columns else pd.DataFrame())
            # VDT-IN-CHART: zachovaj vdt_arb z effect_db ak je
            _dcols = ["date", "dt", "rt"] + (["vdt_arb"] if "vdt_arb" in _daily_db.columns else [])
            _daily = _daily_db.rename(columns={"date": "date"})[_dcols].copy()
            if "vdt_arb" in _daily.columns:
                _daily = _daily.rename(columns={"vdt_arb": "vdt"})
            if not _bl_by_day.empty:
                _daily["date"] = _daily["date"].astype(str)
                _bl_by_day["date"] = _bl_by_day["date"].astype(str)
                _daily = _daily.merge(_bl_by_day.rename(columns={"_bl": "bl"}),
                                       on="date", how="left")
            else:
                _daily["bl"] = 0.0
        else:
            from core.effect import resolve_rt_col as _resolve_rt_d, get_rt_eur_series as _get_rt_d
            _rt_col_d = _resolve_rt_d(_src_for_daily)
            _src_for_daily = _src_for_daily.copy()
            _src_for_daily["_rt_eff"] = _get_rt_d(_src_for_daily, joint_flags=_eff_joint)
            _daily = _src_for_daily.groupby("date").agg(dt=("dt_rev_min", "sum"), rt=("_rt_eff", "sum"),
                                                          bl=("_bl", "sum")).reset_index()
        # VDT-IN-CHART: zabezpeč vdt stĺpec (z minútového vdt_arb_min ak DB nemala)
        if "vdt" not in _daily.columns:
            if "vdt_arb_min" in _src_for_daily.columns:
                _vd = (_src_for_daily.groupby("date")["vdt_arb_min"].sum()
                       .reset_index().rename(columns={"vdt_arb_min": "vdt"}))
                _vd["date"] = _vd["date"].astype(str)
                _daily["date"] = _daily["date"].astype(str)
                _daily = _daily.merge(_vd, on="date", how="left")
            else:
                _daily["vdt"] = 0.0
        _daily["vdt"] = _daily["vdt"].fillna(0)
        _daily["total"] = _daily["dt"].fillna(0) + _daily["rt"].fillna(0)
        DL = "[" + ",".join(f"'{str(x)}'" for x in _daily["date"]) + "]"
        DD = "[" + ",".join(f"{x:.2f}" for x in _daily["dt"].fillna(0)) + "]"
        DR = "[" + ",".join(f"{x:.2f}" for x in _daily["rt"].fillna(0)) + "]"
        DV = "[" + ",".join(f"{x:.2f}" for x in _daily["vdt"].fillna(0)) + "]"
        DT_= "[" + ",".join(f"{x:.2f}" for x in _daily["total"]) + "]"
        DB = "[" + ",".join(f"{x:.2f}" for x in _daily["bl"].fillna(0)) + "]"
        # 15-min detail pre view_day: aggregát z PLNEJ resolution (1440 min/deň).
        # dview je decimované na max_points=600 → suma za 15-min by inak bola ~polovičná.
        # Pre dnešok použijeme today_trace (živé minúty, 1-min); pre minulé dni full deň zo CSV.
        _prov_in_body = r.get("prov_date") if isinstance(r, dict) else None
        _case_in_body = r.get("case") if isinstance(r, dict) else None
        _det_src = None
        if view_day and _case_in_body:
            if _prov_in_body and str(view_day) == str(_prov_in_body) and r.get("today_trace") is not None and len(r["today_trace"]):
                _det_src = r["today_trace"]
            else:
                _det_src = lsim.load_series(_case_in_body, port=_PORT, day=view_day, max_points=10000)
        if _det_src is not None and not _det_src.empty and "ts15" in _det_src.columns:
            # baseline per minúta pre detail (rovnaký vzorec ako pre dfull) — agreguje per 15-min slot
            _det_with_bl = _det_src.copy()
            try:
                _bp2 = bc.parse_baseline_params(_ui_load("plan", {}) or {}) if bc is not None else None
                if _bp2 is not None:
                    _f = pd.to_numeric(_det_src.get("ftv_min_real_kw", _det_src.get("ftv_kw")), errors="coerce").fillna(0).values
                    _l = pd.to_numeric(_det_src.get("load_min_real_kw", pd.Series([0.0]*len(_det_src))), errors="coerce").fillna(0).values
                    _p = pd.to_numeric(_det_src.get("dt_real_eur", _det_src.get("dt_eur")), errors="coerce").fillna(0).values
                    _n2 = _f - _l
                    _e2 = np.maximum(_n2, 0.0) / 60.0
                    _i2 = -np.minimum(_n2, 0.0) / 60.0
                    _pi2 = (_p * _bp2["im_val"]) if _bp2["im_mode"] == "dt_x" else np.full_like(_p, _bp2["im_val"])
                    _pe2 = (_p * _bp2["ex_val"]) if _bp2["ex_mode"] == "dt_x" else np.full_like(_p, _bp2["ex_val"])
                    _det_with_bl["_bl"] = (_e2 * _pe2 / 1000.0) - (_i2 * _pi2 / 1000.0)
                else:
                    _det_with_bl["_bl"] = 0.0
            except Exception:
                _det_with_bl["_bl"] = 0.0
            # Konsolidácia: detail dňa rt cez core.effect (single source of truth)
            # Bug #650: filtered RT podľa joint_lp toggles (rovnaké pre 15-min detail)
            from core.effect import (resolve_rt_col as _resolve_rt_de,
                                       get_rt_eur_series as _get_rt_de)
            _det_with_bl = _det_with_bl.copy()
            _det_with_bl["_rt_eff"] = _get_rt_de(_det_with_bl, joint_flags=_eff_joint)
            # VDT-IN-CHART: agreguj minútový vdt_arb_min na 15-min slot ak je v zdroji
            if "vdt_arb_min" not in _det_with_bl.columns:
                _det_with_bl["vdt_arb_min"] = 0.0
            _det = (_det_with_bl.groupby("ts15").agg(dt=("dt_rev_min", "sum"), rt=("_rt_eff", "sum"),
                                                       vdt=("vdt_arb_min", "sum"), bl=("_bl", "sum"))
                    .reset_index().sort_values("ts15"))
            _det["total"] = _det["dt"].fillna(0) + _det["rt"].fillna(0)
            SL = "[" + ",".join(f"'{str(x)[11:16]}'" for x in _det["ts15"]) + "]"
            SDD = "[" + ",".join(f"{x:.3f}" for x in _det["dt"].fillna(0)) + "]"
            SDR = "[" + ",".join(f"{x:.3f}" for x in _det["rt"].fillna(0)) + "]"
            SDV = "[" + ",".join(f"{x:.3f}" for x in _det["vdt"].fillna(0)) + "]"
            SDT = "[" + ",".join(f"{x:.3f}" for x in _det["total"]) + "]"
            SDB = "[" + ",".join(f"{x:.3f}" for x in _det["bl"].fillna(0)) + "]"
            det_label = f"Detail {view_day} (15 min)"
        else:
            SL = "[]"; SDD = "[]"; SDR = "[]"; SDV = "[]"; SDT = "[]"; SDB = "[]"; det_label = "Detail dňa (15 min)"
        _exp_case = r.get("case", "plan_d1") if isinstance(r, dict) else "plan_d1"
        _exp_url = f"/livesim/chC_export?case={_exp_case}" + (f"&view={view_day}" if view_day else "")
        _exp_pdf_url = f"/livesim/chC_export_pdf?case={_exp_case}" + (f"&view={view_day}" if view_day else "")
        chC = (f"<h2 style='display:flex;align-items:center;gap:14px;flex-wrap:wrap'>"
               f"Zisk za obdobie "
               f"<span style='font-size:13px;display:inline-flex;border:1px solid #1F4E78;border-radius:8px;overflow:hidden'>"
               f"<button id='chC_b_cum' type='button' style='background:#1F4E78;color:#fff;border:0;padding:5px 12px;cursor:pointer;font-size:12px'>Kumulatívne</button>"
               f"<button id='chC_b_day' type='button' style='background:#fff;color:#1F4E78;border:0;padding:5px 12px;cursor:pointer;font-size:12px'>Po dňoch</button>"
               f"<button id='chC_b_det' type='button' style='background:#fff;color:#1F4E78;border:0;padding:5px 12px;cursor:pointer;font-size:12px'>{det_label}</button>"
               f"</span>"
               f"<a href='{_exp_url}' download style='background:#2E7D32;color:#fff;padding:5px 12px;border-radius:7px;text-decoration:none;font-size:12px;font-weight:600' "
               f"title='Stiahne Excel report (7 sheetov): Zhrnutie, Po mesiacoch+grafy, Po dňoch+grafy, Vsetky 15-min, Detail 15-min+grafy, Per-minute (raw), Metadata.'>📥 Export Excel</a>"
               f"<a href='{_exp_pdf_url}' download style='background:#C0392B;color:#fff;padding:5px 12px;border-radius:7px;text-decoration:none;font-size:12px;font-weight:600' "
               f"title='Stiahne PDF manažérsky report (kompaktný formát, grafy, denný + mesačný prehľad).'>📄 Export PDF</a>"
               f"</h2>"
               f"<div style='height:320px'><canvas id='chC'></canvas></div>"
               f"<script>(function(){{"
               f"const L={L}, CT={CT}, CD={CD}, CR={CR}, CV={CV}, CB={CB};"
               f"const DL={DL}, DD={DD}, DR={DR}, DV={DV}, DT_={DT_}, DB={DB};"
               f"const SL={SL}, SDD={SDD}, SDR={SDR}, SDV={SDV}, SDT={SDT}, SDB={SDB};"
               f"let mode='cum', chC_inst=null;"
               f"const mk=()=>{{ if(chC_inst){{chC_inst.destroy();chC_inst=null;}}"
               f" const ctx=document.getElementById('chC').getContext('2d');"
               f" if(mode==='cum'){{"
               f"  chC_inst=new Chart(ctx,{{type:'line',data:{{labels:L,datasets:["
               f"   {{label:'Spolu €',data:CT,borderColor:'#2E7D32',borderWidth:2,pointRadius:0,tension:.1}},"
               f"   {{label:'DT €',data:CD,borderColor:'#1F4E78',borderWidth:1.5,borderDash:[5,3],pointRadius:0,tension:.1}},"
               f"   {{label:'Odchýlka €',data:CR,borderColor:'#C49000',borderWidth:1.5,borderDash:[5,3],pointRadius:0,tension:.1}},"
               f"   {{label:'VDT €',data:CV,borderColor:'#8E24AA',borderWidth:1.5,borderDash:[5,3],pointRadius:0,tension:.1}},"
               f"   {{label:'Baseline € (bez bat. + plánu)',data:CB,borderColor:'#7a5d00',borderWidth:1.8,borderDash:[2,3],pointRadius:0,tension:.1}}]}},"
               f"   options:{{responsive:true,maintainAspectRatio:false,interaction:{{mode:'index',intersect:false}},"
               f"   layout:{{padding:{{right:55}}}},"
               f"   elements:{{point:{{radius:0}}}},scales:{{y:{{title:{{display:true,text:'€ (kumulatív)'}}}}}}}}}});"
               f" }} else if(mode==='day') {{"
               f"  chC_inst=new Chart(ctx,{{type:'bar',data:{{labels:DL,datasets:["
               f"   {{label:'DT €',data:DD,backgroundColor:'rgba(31,78,120,.75)',stack:'s'}},"
               f"   {{label:'Odchýlka €',data:DR,backgroundColor:'rgba(196,144,0,.75)',stack:'s'}},"
               f"   {{label:'VDT €',data:DV,backgroundColor:'rgba(142,36,170,.75)',stack:'s'}},"
               f"   {{type:'line',label:'Spolu €',data:DT_,borderColor:'#2E7D32',borderWidth:2,pointRadius:3,tension:0}},"
               f"   {{type:'line',label:'Baseline € (bez bat. + plánu)',data:DB,borderColor:'#7a5d00',borderWidth:2,pointRadius:3,borderDash:[4,3],tension:0}}]}},"
               f"   options:{{responsive:true,maintainAspectRatio:false,interaction:{{mode:'index',intersect:false}},"
               f"   layout:{{padding:{{right:55}}}},"
               f"   scales:{{x:{{stacked:true}},y:{{stacked:false,title:{{display:true,text:'€ (denný zisk)'}},grid:{{color:c=>c.tick.value===0?'#333':'#eee'}}}}}}}}}});"
               f" }} else {{"
               f"  chC_inst=new Chart(ctx,{{type:'bar',data:{{labels:SL,datasets:["
               f"   {{label:'DT €/15min',data:SDD,backgroundColor:'rgba(31,78,120,.75)',stack:'s'}},"
               f"   {{label:'Odchýlka €/15min',data:SDR,backgroundColor:'rgba(196,144,0,.75)',stack:'s'}},"
               f"   {{label:'VDT €/15min',data:SDV,backgroundColor:'rgba(142,36,170,.75)',stack:'s'}},"
               f"   {{type:'line',label:'Spolu €/15min',data:SDT,borderColor:'#2E7D32',borderWidth:2,pointRadius:2,tension:0}},"
               f"   {{type:'line',label:'Baseline €/15min (bez bat. + plánu)',data:SDB,borderColor:'#7a5d00',borderWidth:2,pointRadius:2,borderDash:[4,3],tension:0}}]}},"
               f"   options:{{responsive:true,maintainAspectRatio:false,interaction:{{mode:'index',intersect:false}},"
               f"   layout:{{padding:{{right:55}}}},"
               f"   scales:{{x:{{stacked:true}},y:{{stacked:false,title:{{display:true,text:'€ (15-min zisk vo zvolenom dni)'}},grid:{{color:c=>c.tick.value===0?'#333':'#eee'}}}}}}}}}});"
               f" }}"
               f"}};"
               f"const setBtn=()=>{{"
               f" const bs={{cum:'chC_b_cum',day:'chC_b_day',det:'chC_b_det'}};"
               f" for(const k in bs){{const el=document.getElementById(bs[k]);"
               f"  el.style.background=(mode===k?'#1F4E78':'#fff');"
               f"  el.style.color=(mode===k?'#fff':'#1F4E78');}}"
               f"}};"
               f"document.getElementById('chC_b_cum').onclick=()=>{{mode='cum';setBtn();mk();}};"
               f"document.getElementById('chC_b_day').onclick=()=>{{mode='day';setBtn();mk();}};"
               f"document.getElementById('chC_b_det').onclick=()=>{{mode='det';setBtn();mk();}};"
               f"mk();}})();</script>")
    else:
        chC = ""
    badge = ""
    if r.get("prov_date") and view_day == r.get("prov_date"):
        badge = ("<div style='background:#fff8e1;border:1px solid #f0d488;border-radius:8px;padding:8px 12px;"
                 "margin:8px 0;font-size:14px;color:#7a5c00'>⚠ <b>Dnešok beží naživo a je PROVIZÓRNY</b> — "
                 "zúčtované na <b>odhadovanej</b> ZCO (skutočná cena odchýlky ešte nie je vyrovnaná). "
                 "Rozhodnutia sú real-time (kauzálne), ale zisk za dnešok sa prepočíta, keď ČEPS vyrovná skutočnú ZCO. "
                 "Dnešok sa neukladá do logu.</div>")
    return (cards + badge + _diag_html + info + nav + chMW + chSEPSMW + chDT + chPlan + chFlow + chRiadenie + chF + chC + table +
            "<p style='color:#666;font-size:12px'>Auto-obnova 60 s — log rastie naživo. "
            "Dokončené dni sa ukladajú so skutočnou ZCO; dnešok sa počíta naživo (odhad ZCO) a neukladá.</p>")


# ═══════════════════════════════════════════════════════════════════
# LOAD IMPORT — CSV spotreby zákazníka per-profil (15-min timestamp + kW)
# ═══════════════════════════════════════════════════════════════════

def _load_import_page(msg: str = "", msg_kind: str = "info",
                        request=None) -> "HTMLResponse":
    """Render /load_import stránky cez Jinja2 template (Fáza 3 refactor).

    msg_kind: 'ok'|'err'|'info' → mapuje sa na banner.success/error/info.
    """
    from ui.templates import render
    active = "—"
    try:
        if pr is not None:
            active = pr.get_active() or "—"
    except Exception:
        pass
    meta = None
    chart_labels = "[]"
    chart_wd = "[]"
    chart_we = "[]"
    if lp is not None:
        meta = lp.get_meta()
        if meta:
            wd = meta.get("weekday_profile_kw") or []
            we = meta.get("weekend_profile_kw") or []
            if wd:
                chart_wd = "[" + ",".join(f"{x:.3f}" for x in wd) + "]"
            if we:
                chart_we = "[" + ",".join(f"{x:.3f}" for x in we) + "]"
            chart_labels = "[" + ",".join(
                f"'{i//4:02d}:{(i%4)*15:02d}'" for i in range(96)) + "]"
    # Map msg_kind → banner CSS class
    kind_map = {"ok": "success", "err": "error", "info": "info"}
    banner_kind = kind_map.get(msg_kind, "info")
    return render(request, "pages/load_import.html",
                   msg=msg, msg_kind=banner_kind,
                   active=active, meta=meta,
                   chart_labels=chart_labels, chart_wd=chart_wd, chart_we=chart_we)


@app.get("/load_import", response_class=HTMLResponse)
def load_import_get(request: Request):
    if lp is None:
        return HTMLResponse("<p>load_profile modul nedostupný.</p>", status_code=503)
    return _load_import_page(request=request)


@app.post("/load_import", response_class=HTMLResponse)
def load_import_post(request: Request, file: UploadFile = File(...),
                      mode: str = Form(default="replace"),
                      unit: str = Form(default="kW")):
    if lp is None:
        return _load_import_page("load_profile modul nedostupný.", "err", request=request)
    try:
        # ulož upload do dočasného súboru a parsuj
        import tempfile
        suffix = os.path.splitext(file.filename or "load.csv")[1] or ".csv"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(file.file.read())
            tmp_path = tmp.name
        try:
            res = lp.import_csv(tmp_path, replace=(mode != "append"), unit=unit)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        _clear_livesim_logs()           # invalidate livesim log → fresh prepočet so spotrebou
        msg = (f"✓ Spotreba naimportovaná v jednotke <b>{unit}</b>. Dni: {res['n_days_total']} "
               f"(WD: {res['n_days_wd']}, WE: {res['n_days_we']}), "
               f"rozsah {res.get('date_min','—')} → {res.get('date_max','—')}, "
               f"priemer {res.get('kwh_per_day_avg', 0):.1f} kWh/deň "
               f"(min/max/mean kW: {res.get('value_min_kw',0):.2f} / {res.get('value_max_kw',0):.2f} / "
               f"{res.get('value_mean_kw',0):.2f}). Livesim log vyresetovaný — pri ďalšom otvorení sa všetko prepočíta.")
        return _load_import_page(msg, "ok")
    except Exception as e:
        return _load_import_page(f"❌ Chyba pri importe: {e}", "err")


@app.post("/load_import/rescale", response_class=HTMLResponse)
def load_import_rescale(request: Request, factor: float = Form(...)):
    if lp is None:
        return _load_import_page("load_profile modul nedostupný.", "err", request=request)
    try:
        f = float(factor)
        if f == 0 or not (1e-9 < abs(f) < 1e9):
            return _load_import_page(f"❌ Neplatný faktor: {factor}", "err", request=request)
        ok = lp.rescale(f)
        if ok:
            _clear_livesim_logs()
            meta = lp.get_meta() or {}
            return _load_import_page(
                f"✓ Hodnoty prenásobené × {f}. Nový rozsah: "
                f"{meta.get('value_min_kw', 0):.2f} až {meta.get('value_max_kw', 0):.2f} kW "
                f"(priemer {meta.get('value_mean_kw', 0):.2f} kW). Livesim log vyresetovaný.",
                "ok", request=request)
        return _load_import_page("⚠ Žiadne dáta na konverziu — najprv naimportuj CSV.",
                                    "info", request=request)
    except Exception as e:
        return _load_import_page(f"❌ Chyba pri rescale: {e}", "err", request=request)


@app.post("/load_import/clear", response_class=HTMLResponse)
def load_import_clear(request: Request):
    if lp is None:
        return HTMLResponse("<p>load_profile modul nedostupný.</p>", status_code=503)
    ok = lp.clear()
    if ok:
        _clear_livesim_logs()
    msg = ("✓ Importovaná spotreba zmazaná pre aktívny profil. Livesim log vyresetovaný." if ok
           else "⚠ Žiadne dáta pre aktívny profil neboli k dispozícii.")
    return _load_import_page(msg, "ok" if ok else "info", request=request)


# ═══════════════════════════════════════════════════════════════════════════
# /REALIO — Reálne meranie FTV + batérie + setpoint riadenie (DAMSU tagy)
# ═══════════════════════════════════════════════════════════════════════════
# Zoznam aktívnych zákazníkov pre /realio. Pre teraz hardcoded; neskôr napojiť na profiles.
# Pre každého zákazníka môžeme mať vlastnú config (per-cust file ako realio_config_<cust>.json),
# zatiaľ ale všetci zdieľajú jednu config (realio_config.json) — keďže máme len Trakany.
REALIO_CUSTOMERS = ["Trakany"]


def _realio_customer_header(active_cust: str, active_tab: str) -> str:
    """Vráti HTML hornej časti /realio: breadcrumb + customer dropdown + sub-tab bar.

    Linky majú `target="_top"` — keď je realio embedovaný v iframe (napr. v tabe
    Reálne riadenie ktorý loaduje /livesim), klik na tab navigáciu vyskočí do
    top window namiesto vnorenia ďalšieho iframu.
    """
    # Tab bar: Vizualizácia (default) + Reálne riadenie + Nastavenie
    tabs = [("vizualizacia", "📊 Vizualizácia"),
            ("riadenie",     "🔴 Reálne riadenie"),
            ("nastavenie",   "⚙ Nastavenie")]
    links = "".join(
        f'<a href="/realio?cust={active_cust}&tab={tab}" target="_top" '
        f'style="padding:8px 18px;text-decoration:none;font-size:13px;font-weight:600;'
        f'{"background:#1F4E78;color:#fff;border-radius:8px 8px 0 0" if tab==active_tab else "color:#1F4E78"}">'
        f'{lbl}</a>'
        for tab, lbl in tabs)
    # Customer chooser (pre teraz len jeden — pripravená štruktúra na viac)
    if len(REALIO_CUSTOMERS) > 1:
        opts = "".join(f'<option value="{c}"{" selected" if c==active_cust else ""}>{c}</option>'
                        for c in REALIO_CUSTOMERS)
        cust_picker = (f'<form method="get" action="/realio" style="display:inline">'
                        f'<input type="hidden" name="tab" value="{active_tab}">'
                        f'<select name="cust" onchange="this.form.submit()" '
                        f'style="padding:6px 10px;border:1px solid #ccc;border-radius:6px;font-size:13px">'
                        f'{opts}</select></form>')
    else:
        cust_picker = (f'<span style="background:#1F4E78;color:#fff;padding:5px 12px;'
                        f'border-radius:6px;font-size:13px;font-weight:600">{active_cust}</span>')
    return (
        f'<div style="display:flex;align-items:center;gap:14px;margin:6px 0 0;flex-wrap:wrap">'
        f'<span style="color:#666;font-size:13px">Zákazník:</span> {cust_picker}'
        f'</div>'
        f'<div style="display:flex;gap:0;align-items:flex-end;margin:10px 0 0;border-bottom:1px solid #ccc">'
        f'{links}</div>')


def _realio_vizualizacia_page(msg: str = "", msg_kind: str = "info",
                                 cust: str = "Trakany") -> str:
    """Dark dashboard v Fuergy Brain štýle. Live KPI tiles + Energy Flow + mini history.

    Číta hodnoty z `out/<market>/realio_measurements.csv` (najnovší riadok).
    Auto-refresh každých 5 s cez meta refresh.
    """
    # PRIMÁRNY ZDROJ = Bender HTTP fetch_latest_all (rovnako ako Live odpočet).
    # FALLBACK = CSV read_recent (ak Bender nie je dostupný / cookies expirovali).
    # Tým máme zhodu medzi Live odpočet, Vizualizáciou a Hodnoty teraz.
    _live_vals = None
    df = pd.DataFrame()
    cfg = {}
    try:
        import realio as _rio
        cfg = _rio.load_config()
        if cfg.get("enabled"):
            _live_vals = _rio.fetch_latest_all()
        df = _rio.read_recent(n_minutes=120)  # pre sparkline historiu
    except Exception:
        pass

    # Časová značka — z fetch_latest ak je, inak z CSV
    ts_str = "—"
    minutes_ago = None
    if _live_vals and isinstance(_live_vals, dict) and not _live_vals.get("_error"):
        try:
            _ts_iso = _live_vals.get("_ts")
            if _ts_iso:
                ts = pd.to_datetime(_ts_iso)
                ts_str = ts.strftime("%H:%M:%S")
                minutes_ago = (pd.Timestamp.now() - ts).total_seconds() / 60.0
        except Exception:
            pass
    if ts_str == "—" and not df.empty:
        try:
            ts = pd.to_datetime(df["time"].dropna().iloc[-1])
            ts_str = ts.strftime("%H:%M:%S")
            minutes_ago = (pd.Timestamp.now() - ts).total_seconds() / 60.0
        except Exception:
            pass

    # Per-stĺpec last valid pre CSV fallback
    def _last_valid_csv(col):
        if df is None or df.empty or col not in df.columns:
            return None
        s = df[col].dropna()
        if s.empty:
            return None
        try:
            return float(s.iloc[-1])
        except (TypeError, ValueError):
            return None

    # KPI hodnota: 1) z live (fetch_latest_all), 2) fallback z CSV
    def _kpi(key):
        if _live_vals and isinstance(_live_vals, dict) and not _live_vals.get("_error"):
            v = _live_vals.get(key)
            if v is not None:
                try: return float(v)
                except (TypeError, ValueError): pass
        return _last_valid_csv(key)

    def _vfmt(key, decimals=1):
        v = _kpi(key)
        if v is None:
            return "—", ""
        return f"{v:,.{decimals}f}".replace(",", " ").replace(".", ",").replace(" ", " "), ""

    # MAPOVANIE z DAMSU tagov na fyzický význam:
    #   load_power_kw v configu → ELM1 elektromer = REÁLNA SIEŤ (čo prechádza prahom)
    #   grid_power_kw v configu → REG_C_Regulation_Power = regulačný signál (NIE hlavná sieť)
    # Spotreba sa DOPOČÍTAVA z energetickej bilancie:
    #   Spotreba = FTV + Batéria_vybíja − Sieť_export
    #            = ftv + batt − sieť   (so znamienkom: + sieť = export, − sieť = import)
    # User feedback (2026-05-31): "Ta hodnota co je na spotrebe je siet, spotreba v tomot
    # pripade je potom siet -( FTV + baterka)" → potvrdené, prepočítavame.
    def _f(x, default=0.0):
        try:
            return float(x) if x not in (None, "", float("nan")) else default
        except (TypeError, ValueError):
            return default

    # Live z fetch_latest_all (cez _kpi) → konzistencia s Live odpočtom; CSV fallback
    ftv_kw   = _kpi("ftv_power_kw")  or 0.0
    siet_kw  = _kpi("load_power_kw") or 0.0   # = ELM1 = sieť
    batt_kw  = _kpi("batt_power_kw") or 0.0
    grid_kw  = _kpi("grid_power_kw") or 0.0   # REG_C (info)
    # Reálna spotreba z energetickej bilancie:
    #   Spotreba = Sieť + FTV + Batéria   (user formula, 2026-05-31)
    # Konvencia znamenia ELM1 závisí od inštalácie — Trakany má sign tak, že priamy súčet
    # všetkých zdrojov dáva spotrebu.
    spotreba_kw = siet_kw + ftv_kw + batt_kw

    def _fmt(v, dec=1):
        if v is None: return "—"
        return f"{v:,.{dec}f}".replace(",", " ").replace(".", ",")

    ftv_val      = _fmt(ftv_kw)
    siet_val     = _fmt(siet_kw)
    batt_val     = _fmt(batt_kw)
    soc_val, _   = _vfmt("batt_soc_pct")
    spotreba_val = _fmt(spotreba_kw)
    grid_val     = _fmt(grid_kw)  # regulačný (info-only)

    batt_sub = ("⚡ nabíja" if batt_kw < -1 else ("⚡ vybíja" if batt_kw > 1 else "idle"))
    # ELM1 sign konvencia (Trakany): záporné = export do siete, kladné = import zo siete
    siet_sub = ("↑ export do siete" if siet_kw < -1 else
                ("↓ import zo siete" if siet_kw > 1 else "vyrovnané"))

    # Live badge alebo stale warning
    if minutes_ago is None:
        live_state = ("<div class='live-badge stale'><div class='live-dot'></div>"
                       "<span>NO DATA</span></div>")
    elif minutes_ago < 3:
        live_state = (f"<div class='live-badge'><div class='live-dot'></div>"
                       f"<span>LIVE · {ts_str}</span></div>")
    else:
        live_state = (f"<div class='live-badge stale'><div class='live-dot'></div>"
                       f"<span>STALE · {ts_str} (pred {minutes_ago:.0f} min)</span></div>")

    # Mini tabuľka — posledných 10 riadkov (s dopočítanou spotrebou)
    table_rows = ""
    if df is not None and not df.empty:
        recent = df.sort_values("time", ascending=False).head(10)
        for _, r in recent.iterrows():
            try:
                t = pd.to_datetime(r["time"]).strftime("%H:%M:%S")
            except Exception:
                t = str(r.get("time", ""))[:8]
            def _td(key, decimals=1, klass=""):
                v = r.get(key)
                try:
                    if pd.isna(v) or v == "":
                        return f"<td class='{klass}'>—</td>"
                    return f"<td class='{klass}'>{float(v):,.{decimals}f}</td>".replace(",", " ").replace(".", ",")
                except (TypeError, ValueError):
                    return f"<td class='{klass}'>—</td>"
            # Dopočítaná spotreba per row: Sieť + FTV + Batt
            _ftv  = _f(r.get("ftv_power_kw"))
            _batt = _f(r.get("batt_power_kw"))
            _siet = _f(r.get("load_power_kw"))
            _spot = _siet + _ftv + _batt
            _spot_td = (f"<td class='num-spot'>{_spot:,.1f}</td>".replace(",", " ").replace(".", ","))
            table_rows += (
                f"<tr><td class='cell-time'>{t}</td>"
                f"{_spot_td}"
                f"{_td('ftv_power_kw', 1, 'num-fv')}"
                f"{_td('batt_power_kw', 1, 'num-bat')}"
                f"{_td('batt_soc_pct', 1, 'num-soc')}"
                f"{_td('load_power_kw', 1, 'num-grid')}</tr>")
    else:
        table_rows = "<tr><td colspan='6' style='text-align:center;padding:24px;color:var(--text-2)'>Zatiaľ žiadne merania (CSV je prázdny)</td></tr>"

    # Param tile values z configu
    cust_kwp = "—"
    cust_batt_kw = "—"
    cust_batt_kwh = "—"
    cust_grid_imp = "—"
    cust_grid_exp = "—"
    # Skús ich potiahnuť z profile (alebo z ui_settings.plan) — neskôr pripojiť na profile system
    try:
        _ui = _ui_load("plan", {}) or {}
        if _ui.get("kwp"): cust_kwp = f"{float(_ui['kwp']):,.0f}".replace(",", " ")
        if _ui.get("batt_kw"): cust_batt_kw = f"{float(_ui['batt_kw']):,.0f}".replace(",", " ")
        if _ui.get("batt_kwh"): cust_batt_kwh = f"{float(_ui['batt_kwh']):,.0f}".replace(",", " ")
        if _ui.get("grid_kw_import"): cust_grid_imp = f"{float(_ui['grid_kw_import']):,.0f}".replace(",", " ")
        if _ui.get("grid_kw_export"): cust_grid_exp = f"{float(_ui['grid_kw_export']):,.0f}".replace(",", " ")
    except Exception:
        pass

    msg_html = ""
    if msg:
        bg = {"ok": "rgba(74,222,128,.12)", "info": "rgba(96,165,250,.12)", "err": "rgba(239,68,68,.12)"}.get(msg_kind, "rgba(96,165,250,.12)")
        bd = {"ok": "#4ade80", "info": "#60a5fa", "err": "#ef4444"}.get(msg_kind, "#60a5fa")
        msg_html = (f"<div style='background:{bg};border:1px solid {bd};border-radius:10px;"
                     f"padding:12px 16px;margin:16px 0;color:var(--text-0)'>{msg}</div>")

    # Flow diagram hodnoty (sieť = ELM, spotreba = dopočítaná)
    s_ftv  = _fmt(ftv_kw)
    s_batt = _fmt(batt_kw)
    s_load = _fmt(spotreba_kw)   # dopočítaná spotreba
    s_siet = _fmt(siet_kw)        # ELM1

    # ── Sparklines pre pravý stĺpec (posledných 60 min) ────────────────────
    def _sparkline_svg(values: list, color: str, fill_color: str = None,
                         width: int = 200, height: int = 50) -> str:
        """Vytvor SVG sparkline path z list-of-floats. None v hodnotách sa preskočí."""
        clean = [v for v in values if v is not None and v == v]  # NaN-safe
        if len(clean) < 2:
            return f'<svg viewBox="0 0 {width} {height}"></svg>'
        vmin, vmax = min(clean), max(clean)
        rng = max(vmax - vmin, 0.001)
        n = len(values)
        pts = []
        for i, v in enumerate(values):
            if v is None or v != v:
                continue
            x = i / max(n - 1, 1) * width
            y = height - ((float(v) - vmin) / rng) * (height - 4) - 2
            pts.append(f"{x:.1f},{y:.1f}")
        if not pts:
            return f'<svg viewBox="0 0 {width} {height}"></svg>'
        # Line + optional fill
        polyline = f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="1.5"/>'
        fill = ""
        if fill_color:
            # closed path for fill
            first_x = pts[0].split(",")[0]
            last_x = pts[-1].split(",")[0]
            fill_pts = pts + [f"{last_x},{height}", f"{first_x},{height}"]
            fill = f'<polygon points="{" ".join(fill_pts)}" fill="{fill_color}" opacity="0.18"/>'
        return f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" style="width:100%;height:50px">{fill}{polyline}</svg>'

    # Vytiahni posledných ~60 min hodnôt z df pre každý metric
    def _series(key: str, n: int = 60) -> list:
        if df is None or df.empty or key not in df.columns:
            return []
        s = pd.to_numeric(df[key], errors="coerce").tail(n)
        return s.where(s.notna(), None).tolist()

    ftv_series  = _series("ftv_power_kw")
    siet_series = _series("load_power_kw")
    batt_series = _series("batt_power_kw")
    soc_series  = _series("batt_soc_pct")
    # Dopočítaná spotreba = sieť + FTV + batt per row
    spot_series = []
    if df is not None and not df.empty:
        last_n = df.tail(60)
        for _, r in last_n.iterrows():
            try:
                _ftv  = float(r.get("ftv_power_kw") or 0)
                _siet = float(r.get("load_power_kw") or 0)
                _batt = float(r.get("batt_power_kw") or 0)
                spot_series.append(_siet + _ftv + _batt)
            except (TypeError, ValueError):
                spot_series.append(None)

    spark_spot = _sparkline_svg(spot_series, "#4ade80", "#4ade80")
    spark_ftv  = _sparkline_svg(ftv_series,  "#fb923c", "#fb923c")
    spark_batt = _sparkline_svg(batt_series, "#f472b6", "#f472b6")
    spark_soc  = _sparkline_svg(soc_series,  "#a855f7", "#a855f7")
    spark_siet = _sparkline_svg(siet_series, "#60a5fa", "#60a5fa")

    return f"""<!doctype html><html lang="sk" data-theme="dark"><head><meta charset="utf-8">
<title>Vizualizácia · {cust} · Reálne meranie</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="10">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root[data-theme="dark"] {{
  --bg-0:#07090d; --bg-1:#0d1117; --bg-2:#161c25; --bg-3:#1f2733;
  --line:#1f2733; --line-strong:#2a3441;
  --text-0:#f5f9ff; --text-1:#a8b3c4; --text-2:#6b7889;
  --accent:#4ade80; --accent-2:#60a5fa; --accent-3:#fb923c; --accent-4:#f472b6;
  --danger:#ef4444;
  --shadow:0 8px 32px -8px rgba(0,0,0,.5),0 2px 6px -2px rgba(0,0,0,.3);
  --shadow-soft:0 2px 12px -2px rgba(0,0,0,.4);
  --grad-card:linear-gradient(180deg,rgba(255,255,255,.025) 0%,rgba(255,255,255,0) 100%);
}}
* {{ box-sizing:border-box; margin:0; padding:0; }}
html,body {{ background:var(--bg-0); color:var(--text-0); }}
body {{
  font-family:'Sora',system-ui,sans-serif; font-weight:400;
  -webkit-font-smoothing:antialiased; min-height:100vh;
  background-image:
    radial-gradient(ellipse 1200px 600px at 80% -200px,rgba(74,222,128,.06),transparent 70%),
    radial-gradient(ellipse 800px 400px at 0% 100%,rgba(96,165,250,.05),transparent 70%);
  background-attachment:fixed;
}}
.page {{ padding:24px 28px 64px; max-width:1640px; margin:0 auto; }}
h1 {{ font-size:28px; font-weight:600; letter-spacing:-.02em; }}
a {{ color:var(--accent-2); text-decoration:none; }}
a:hover {{ color:var(--accent); }}
/* Re-use _nav style for top nav (light) but blend with dark page */
.dark-nav {{ background:var(--bg-1); border:1px solid var(--line); border-radius:10px;
            padding:8px; margin-bottom:18px; display:flex; flex-wrap:wrap; gap:6px; align-items:center; }}
.dark-nav a {{ padding:8px 12px; border-radius:8px; color:var(--text-1); font-size:13px; font-weight:500; }}
.dark-nav a:hover {{ color:var(--text-0); background:var(--bg-2); }}
.dark-nav a.active {{ background:color-mix(in srgb,var(--accent) 18%,transparent);
                       color:var(--accent); border:1px solid color-mix(in srgb,var(--accent) 35%,transparent); }}
.dark-tabs {{ display:flex; gap:0; align-items:flex-end; margin:14px 0 0; border-bottom:1px solid var(--line); }}
.dark-tabs a {{ padding:10px 22px; font-size:13px; font-weight:600; color:var(--text-1);
                border-radius:8px 8px 0 0; }}
.dark-tabs a.active {{ background:var(--bg-1); color:var(--accent); border:1px solid var(--line); border-bottom:1px solid var(--bg-1); margin-bottom:-1px; }}
.cust-badge {{ display:inline-flex; align-items:center; gap:8px; padding:6px 14px;
               background:color-mix(in srgb,var(--accent-2) 12%,transparent);
               border:1px solid color-mix(in srgb,var(--accent-2) 30%,transparent);
               border-radius:999px; color:var(--accent-2); font-weight:600; font-size:13px; }}

/* Cards */
.card {{ background:var(--bg-1); background-image:var(--grad-card);
        border:1px solid var(--line); border-radius:14px; box-shadow:var(--shadow-soft);
        overflow:hidden; transition:border-color .25s; }}
.card:hover {{ border-color:var(--line-strong); }}
.card-head {{ display:flex; align-items:center; justify-content:space-between;
              padding:16px 20px; border-bottom:1px solid var(--line); }}
.card-title {{ display:flex; align-items:center; gap:10px; font-size:13px; font-weight:600;
                letter-spacing:.02em; text-transform:uppercase; color:var(--text-1); }}
.tag-dot {{ width:8px; height:8px; border-radius:2px; }}
.card-body {{ padding:20px; }}
.card-body.no-pad {{ padding:0; }}

/* KPI tiles */
.kpi-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin:24px 0; }}
@media (max-width:980px) {{ .kpi-grid {{ grid-template-columns:1fr 1fr; }} }}
.kpi-tile {{ background:var(--bg-1); border:1px solid var(--line);
             border-left:3px solid var(--accent); border-radius:14px; padding:18px 20px;
             box-shadow:var(--shadow-soft); position:relative; }}
.kpi-tile.tone-green  {{ border-left-color:var(--accent); }}
.kpi-tile.tone-orange {{ border-left-color:var(--accent-3); }}
.kpi-tile.tone-pink   {{ border-left-color:var(--accent-4); }}
.kpi-tile.tone-blue   {{ border-left-color:var(--accent-2); }}
.kpi-label {{ font-size:11px; font-weight:600; text-transform:uppercase;
              letter-spacing:.08em; color:var(--text-2); margin-bottom:6px; }}
.kpi-value {{ font-size:32px; font-weight:600; letter-spacing:-.02em;
              font-variant-numeric:tabular-nums; line-height:1.05;
              font-family:'JetBrains Mono',monospace; color:var(--text-0); }}
.kpi-value .unit {{ font-size:14px; color:var(--text-2); font-weight:400; margin-left:4px; }}
.kpi-value.tone-green  {{ color:var(--accent); }}
.kpi-value.tone-orange {{ color:var(--accent-3); }}
.kpi-value.tone-pink   {{ color:var(--accent-4); }}
.kpi-value.tone-blue   {{ color:var(--accent-2); }}
.kpi-meta {{ font-size:11px; color:var(--text-2); font-family:'JetBrains Mono',monospace;
             margin-top:6px; }}

/* Live badge */
.live-badge {{ display:inline-flex; align-items:center; gap:8px; padding:6px 12px;
                background:color-mix(in srgb,var(--accent) 12%,transparent);
                border:1px solid color-mix(in srgb,var(--accent) 25%,transparent);
                border-radius:999px; color:var(--accent);
                font-family:'JetBrains Mono',monospace; font-size:11px; font-weight:500; }}
.live-badge.stale {{ background:color-mix(in srgb,var(--danger) 12%,transparent);
                      border-color:color-mix(in srgb,var(--danger) 25%,transparent);
                      color:var(--danger); }}
.live-dot {{ width:8px; height:8px; border-radius:50%; background:currentColor;
              box-shadow:0 0 0 0 currentColor; animation:pulse 2s infinite; }}
@keyframes pulse {{
  0%,100% {{ box-shadow:0 0 0 0 color-mix(in srgb,currentColor 50%,transparent); }}
  50%      {{ box-shadow:0 0 0 8px transparent; }}
}}

/* Flow diagram */
.flow-demo {{ position:relative; height:420px; background:var(--bg-1); overflow:hidden; }}
.flow-demo svg {{ width:100%; height:100%; display:block; }}
.flow-line {{ stroke:var(--line-strong); stroke-width:2; fill:none; }}
.flow-particles {{ fill:none; stroke-width:3; stroke-linecap:round;
                    stroke-dasharray:4 10; animation:flow 1.5s linear infinite; }}
.flow-particles.dir-reverse {{ animation-direction:reverse; }}
@keyframes flow {{ to {{ stroke-dashoffset:-28; }} }}
.flow-node {{ fill:var(--bg-2); stroke:var(--line-strong); stroke-width:1.5; }}
.flow-node.center {{ fill:var(--bg-2); stroke:var(--accent); stroke-width:2;
                      filter:drop-shadow(0 0 16px rgba(74,222,128,.25)); }}

/* Data table */
table.data {{ width:100%; border-collapse:collapse; font-size:12px; }}
table.data thead th {{ background:var(--bg-2); color:var(--text-1); font-weight:600;
                        text-align:right; padding:10px 14px; font-size:11px;
                        text-transform:uppercase; letter-spacing:.06em;
                        border-bottom:1px solid var(--line); }}
table.data thead th:first-child {{ text-align:left; color:var(--accent-2); }}
table.data tbody td {{ padding:11px 14px; color:var(--text-0); text-align:right;
                       font-variant-numeric:tabular-nums; font-family:'JetBrains Mono',monospace;
                       border-bottom:1px solid var(--line); }}
table.data tbody td.cell-time {{ color:var(--accent-2); font-weight:500; text-align:left; }}
table.data tbody tr:hover {{ background:color-mix(in srgb,var(--accent) 5%,transparent); }}
.num-fv   {{ color:var(--accent-3); }}
.num-bat  {{ color:var(--accent-4); }}
.num-soc  {{ color:var(--accent); }}
.num-grid {{ color:var(--accent-2); }}
.num-spot {{ color:var(--accent); font-weight:600; }}

/* Params grid */
.params {{ display:grid; grid-template-columns:repeat(4,1fr); gap:1px; background:var(--line); }}
@media (max-width:980px) {{ .params {{ grid-template-columns:1fr 1fr; }} }}
.param {{ background:var(--bg-1); padding:18px 20px; }}
.param-label {{ font-size:10px; text-transform:uppercase; letter-spacing:.08em;
                color:var(--text-2); margin-bottom:8px; }}
.param-value {{ font-size:22px; font-weight:600; font-variant-numeric:tabular-nums;
                letter-spacing:-.01em; color:var(--text-0);
                font-family:'JetBrains Mono',monospace; }}
.param-value .unit {{ font-size:12px; color:var(--text-2); font-weight:400; margin-left:4px; }}

.row {{ display:grid; gap:18px; margin-bottom:18px; }}
.row.cols-flow {{ grid-template-columns:1.6fr 1fr; }}
@media (max-width:980px) {{ .row.cols-flow {{ grid-template-columns:1fr; }} }}

/* Sparkline cards — riadok 5 v rade pod Energy Flow */
.spark-row {{ display:grid; grid-template-columns:repeat(5,1fr); gap:14px; margin-bottom:18px; }}
@media (max-width:1200px) {{ .spark-row {{ grid-template-columns:repeat(2,1fr); }} }}
@media (max-width:640px) {{ .spark-row {{ grid-template-columns:1fr; }} }}
.spark-card {{ background:var(--bg-1); background-image:var(--grad-card);
                border:1px solid var(--line); border-left:3px solid var(--accent);
                border-radius:14px; padding:14px 16px;
                display:flex; flex-direction:column; gap:8px;
                box-shadow:var(--shadow-soft); transition:border-color .2s;
                min-width:0; }}
.spark-card:hover {{ border-color:var(--line-strong); }}
.spark-card-text {{ min-width:0; }}
.spark-card-label {{ font-size:10px; font-weight:600; text-transform:uppercase;
                       letter-spacing:.08em; color:var(--text-2); margin-bottom:4px; }}
.spark-card-value {{ font-size:24px; font-weight:600; letter-spacing:-.02em;
                       line-height:1.05; font-variant-numeric:tabular-nums;
                       font-family:'JetBrains Mono',monospace; }}
.spark-card-value .unit {{ font-size:11px; color:var(--text-2); font-weight:400; margin-left:3px; }}
.spark-card-meta {{ font-size:10px; color:var(--text-2);
                      font-family:'JetBrains Mono',monospace; margin-top:3px; }}
.spark-card-chart {{ overflow:hidden; }}
</style></head><body>
<div class="page">

<div class="dark-nav">
<a href="/">🗓 Plán D-1</a>
<a href="/dentrh">⚡ Denný trh</a>
<a href="/rt">🔴 RT</a>
<a href="/livesim">🟢 Živá simulácia</a>
<a href="/realio" class="active">🔌 Reálne meranie</a>
<a href="/profiles">⚙ Profily</a>
</div>

<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:8px">
  <h1>🔌 Reálne meranie</h1>
  {live_state}
</div>

<div style="display:flex;align-items:center;gap:14px;margin:6px 0 0;flex-wrap:wrap">
  <span style="color:var(--text-2);font-size:13px">Zákazník:</span>
  <span class="cust-badge">📍 {cust}</span>
</div>

<div class="dark-tabs">
  <a href="/realio?cust={cust}&tab=vizualizacia" target="_top" class="active">📊 Vizualizácia</a>
  <a href="/realio?cust={cust}&tab=riadenie" target="_top">🔴 Reálne riadenie</a>
  <a href="/realio?cust={cust}&tab=nastavenie" target="_top">⚙ Nastavenie</a>
</div>

{msg_html}

<!-- Energy Flow diagram — full width -->
<div class="row" style="grid-template-columns:1fr">
  <div class="card">
    <div class="card-head">
      <div class="card-title">
        <span class="tag-dot" style="background:var(--accent)"></span>
        Energy Flow · Live
      </div>
      <span style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text-2)">auto-refresh 10 s</span>
    </div>
    <div class="card-body no-pad">
      <div class="flow-demo">
        <svg viewBox="0 0 800 240" preserveAspectRatio="xMidYMid meet">
          <!-- FTV → SPOTREBA (oranžové, vždy doprava — FTV produkuje len smerom k spotrebe/sieti) -->
          <path class="flow-line" d="M 200 70 L 360 110"/>
          <path class="flow-particles" d="M 200 70 L 360 110" stroke="var(--accent-3)"/>
          <!-- BATÉRIA ↔ SPOTREBA (ružové, smer animácie podľa znamienka batt) -->
          <path class="flow-line" d="M 200 180 L 360 140"/>
          <path class="flow-particles {'dir-reverse' if batt_kw < 0 else ''}" d="M 200 180 L 360 140" stroke="var(--accent-4)"/>
          <!-- SPOTREBA ↔ SIEŤ (modré, smer podľa znamienka sieť):
               sieť < 0 = EXPORT → tečie spotreba→sieť (forward, M→L)
               sieť > 0 = IMPORT → tečie sieť→spotreba (reverse) -->
          <path class="flow-line" d="M 540 125 L 660 125"/>
          <path class="flow-particles {'dir-reverse' if siet_kw > 0 else ''}" d="M 540 125 L 660 125" stroke="var(--accent-2)"/>

          <!-- FTV node -->
          <rect class="flow-node" x="80" y="40" width="120" height="60" rx="10"/>
          <text x="140" y="64" text-anchor="middle" fill="var(--text-2)" font-family="Sora" font-size="10" font-weight="600" letter-spacing="0.08em">FTV</text>
          <text x="140" y="86" text-anchor="middle" fill="var(--accent-3)" font-family="JetBrains Mono" font-size="14" font-weight="600">{s_ftv} kW</text>

          <!-- BATTERY node -->
          <rect class="flow-node" x="80" y="150" width="120" height="60" rx="10"/>
          <text x="140" y="174" text-anchor="middle" fill="var(--text-2)" font-family="Sora" font-size="10" font-weight="600" letter-spacing="0.08em">BATÉRIA · {soc_val}%</text>
          <text x="140" y="196" text-anchor="middle" fill="var(--accent-4)" font-family="JetBrains Mono" font-size="14" font-weight="600">{s_batt} kW</text>

          <!-- CENTER: SPOTREBA (dopočítaná) -->
          <rect class="flow-node center" x="360" y="95" width="180" height="60" rx="12"/>
          <text x="450" y="118" text-anchor="middle" fill="var(--text-2)" font-family="Sora" font-size="10" font-weight="600" letter-spacing="0.08em">SPOTREBA</text>
          <text x="450" y="140" text-anchor="middle" fill="var(--accent)" font-family="JetBrains Mono" font-size="16" font-weight="600">{s_load} kW</text>

          <!-- SIEŤ node (ELM1) -->
          <rect class="flow-node" x="660" y="95" width="120" height="60" rx="10"/>
          <text x="720" y="119" text-anchor="middle" fill="var(--text-2)" font-family="Sora" font-size="10" font-weight="600" letter-spacing="0.08em">SIEŤ</text>
          <text x="720" y="141" text-anchor="middle" fill="var(--accent-2)" font-family="JetBrains Mono" font-size="14" font-weight="600">{s_siet} kW</text>
        </svg>
      </div>
    </div>
  </div>

</div>

<!-- Sparkline cards row (5 v rade pod Energy Flow) -->
<div class="spark-row">
  <div class="spark-card" style="border-left-color:var(--accent)">
    <div class="spark-card-text">
      <div class="spark-card-label">Spotreba</div>
      <div class="spark-card-value" style="color:var(--accent)">{spotreba_val}<span class="unit">kW</span></div>
      <div class="spark-card-meta">Sieť + FTV + Batt</div>
    </div>
    <div class="spark-card-chart">{spark_spot}</div>
  </div>
  <div class="spark-card" style="border-left-color:var(--accent-3)">
    <div class="spark-card-text">
      <div class="spark-card-label">FTV</div>
      <div class="spark-card-value" style="color:var(--accent-3)">{ftv_val}<span class="unit">kW</span></div>
      <div class="spark-card-meta">solárna produkcia</div>
    </div>
    <div class="spark-card-chart">{spark_ftv}</div>
  </div>
  <div class="spark-card" style="border-left-color:var(--accent-4)">
    <div class="spark-card-text">
      <div class="spark-card-label">Stav batérie</div>
      <div class="spark-card-value" style="color:var(--accent-4)">{batt_val}<span class="unit">kW</span></div>
      <div class="spark-card-meta">{batt_sub}</div>
    </div>
    <div class="spark-card-chart">{spark_batt}</div>
  </div>
  <div class="spark-card" style="border-left-color:#a855f7">
    <div class="spark-card-text">
      <div class="spark-card-label">SOC</div>
      <div class="spark-card-value" style="color:#a855f7">{soc_val}<span class="unit">%</span></div>
      <div class="spark-card-meta">stav nabitia</div>
    </div>
    <div class="spark-card-chart">{spark_soc}</div>
  </div>
  <div class="spark-card" style="border-left-color:var(--accent-2)">
    <div class="spark-card-text">
      <div class="spark-card-label">Sieť</div>
      <div class="spark-card-value" style="color:var(--accent-2)">{siet_val}<span class="unit">kW</span></div>
      <div class="spark-card-meta">{siet_sub}</div>
    </div>
    <div class="spark-card-chart">{spark_siet}</div>
  </div>
</div>

<!-- Recent measurements -->
<div class="card">
  <div class="card-head">
    <div class="card-title">
      <span class="tag-dot" style="background:var(--accent)"></span>
      Posledné odpočty
    </div>
    <span style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text-2)">posledných 10 minút</span>
  </div>
  <div class="card-body no-pad">
    <table class="data">
      <thead>
        <tr>
          <th>Čas</th>
          <th>Spotreba (kW)<br><span style="font-size:9px;font-weight:400;text-transform:none;color:var(--text-2)">dopočítaná</span></th>
          <th>FTV (kW)</th>
          <th>Batéria (kW)</th>
          <th>SOC (%)</th>
          <th>Sieť (kW)<br><span style="font-size:9px;font-weight:400;text-transform:none;color:var(--text-2)">ELM1</span></th>
        </tr>
      </thead>
      <tbody>
        {table_rows}
      </tbody>
    </table>
  </div>
</div>

</div>
</body></html>"""


def _realio_page(msg: str = "", msg_kind: str = "info",
                  tab: str = "nastavenie", cust: str = "Trakany",
                  profile: str = "") -> str:
    """Dispatch /realio rendering podľa tabu.

    tab='nastavenie'   → klasická konfiguračná stránka (existujúce UI)
    tab='vizualizacia' → dark dashboard (Fuergy Brain štýl, live KPI tiles)
    tab='riadenie'     → Reálne riadenie — embedded /livesim s realio overlay
    """
    if cust not in REALIO_CUSTOMERS:
        cust = REALIO_CUSTOMERS[0]
    if tab == "vizualizacia":
        return _realio_vizualizacia_page(msg=msg, msg_kind=msg_kind, cust=cust)
    if tab == "riadenie":
        return _realio_riadenie_page(msg=msg, msg_kind=msg_kind, cust=cust, profile=profile)
    return _realio_nastavenie_page(msg=msg, msg_kind=msg_kind, cust=cust)


def _realio_riadenie_page(msg: str = "", msg_kind: str = "info",
                            cust: str = "Trakany", profile: str = "") -> str:
    """Render /realio?tab=riadenie — Reálne riadenie.

    Zobrazuje plnú živú simuláciu (/livesim) v iframe, s vrchným pruhom realio
    header + tab bar + výberom profilu. /livesim sa zavolá s parametrami:
      • realio_overlay=1 → simulačné FTV/load/SOC/batt sa nahradia reálnymi
                            meraniami z out/<market>/realio_measurements.csv
      • profile=<name>   → konkrétny profil pre plán a parametre batérie

    Plány (D-1 nominácie) zostávajú simulované — sú referenčný benchmark
    pre RT controller. Žiadne tlačidlá na editáciu FTV scenára (zbytočné
    keď máme reálne meranie).
    """
    # Profile dispatch — Realio má VLASTNÝ pinned profil, NEZÁVISLÝ od globálneho active.
    # Užívateľ tak môže mať sim profil aktívny globálne (pre /plan, /livesim, /rt)
    # a real profil pinned pre Realio (manual setpoint, riadenie). Per-port (PORT/APP_PORT).
    try:
        import profiles as _pr
        all_profs = _pr.list_profiles()
        real_profs = [p for p in all_profs if _pr.get_mode(p) == _pr.MODE_REAL]
    except Exception:
        _pr = None
        all_profs = []
        real_profs = []
    try:
        import plan_store as _ps
        global_active = _ps.resolve_profile()
    except Exception:
        global_active = "default"

    # Bug Q (2026-06-06): realio-pinned mechanizmus zrušený. Realio teraz používa
    # rovnaký active profile ako celá aplikácia (single source of truth). Dropdown
    # tu submituje cez /profiles/apply (= globálny set_active).
    selected_profile = profile if profile else global_active

    # Mode vybraného profilu
    try:
        selected_mode = _pr.get_mode(selected_profile) if _pr else "simulation"
    except Exception:
        selected_mode = "simulation"

    # Dropdown: VŠETKY profily s mode chipom; sim sú disabled (ale visible — aby
    # user videl ich existenciu a vedel rozlíšiť real vs sim podľa farby chip-u).
    # Ordering: real prvý (selectable), sim druhý (disabled).
    prof_list = sorted(real_profs) + sorted(p for p in all_profs if p not in real_profs)
    if selected_profile and selected_profile not in prof_list:
        prof_list.insert(0, selected_profile)

    # Profile picker form — GET refresh stránky s novým ?profile=...
    # VŠETKY profily v zozname; real sú selectable, sim disabled (visible aby user videl rozdiel).
    def _prof_opt(p):
        try:
            m = _pr.get_mode(p) if _pr else "simulation"
        except Exception:
            m = "simulation"
        sel = " selected" if p == selected_profile else ""
        if m == "real":
            return f'<option value="{p}"{sel}>🔴 {p}  [Real]</option>'
        else:
            return (f'<option value="{p}"{sel} disabled '
                    f'style="color:#999">🎮 {p}  [Sim — nedá sa použiť na reálne riadenie]</option>')
    prof_opts = "".join(_prof_opt(p) for p in prof_list)
    if not real_profs:
        # Žiadne real profily — pridať vysvetľujúci option na vrch
        prof_opts = ('<option value="" disabled>⚠ Žiadny Real profil — vytvor v /profiles</option>'
                      + prof_opts)
    # Bug Q: form submituje cez POST /profiles/apply — prepne GLOBÁLNY active profil.
    # Po POST nás server presmeruje späť na /realio?tab=riadenie (cez redirect logiku
    # ktorá zachováva referer alebo defaultne ide na /profiles).
    profile_picker = (
        f'<form method="post" action="/profiles/apply" style="display:inline-flex;align-items:center;gap:6px;margin-left:18px">'
        f'<input type="hidden" name="redirect_to" value="/realio?tab=riadenie&cust={cust}">'
        f'<span style="color:#666;font-size:13px">Aktívny profil:</span>'
        f'<select name="name" onchange="this.form.submit()" '
        f'style="padding:5px 10px;border:1px solid #ccc;border-radius:6px;font-size:13px;font-weight:600;background:#fff;min-width:280px">'
        f'{prof_opts}</select>'
        f'<span style="color:#666;font-size:11px;margin-left:6px" '
        f'title="Prepne aktívny profil pre celú aplikáciu (single source of truth)">'
        f'<i>(globálny — zmena ovplyvní všetky stránky)</i></span></form>')

    # Realio CSV presence — banner ak ešte nemáme dáta
    has_realio = False
    last_age_min = None
    try:
        import realio as _rio
        df_recent = _rio.read_recent(n_minutes=10)
        if not df_recent.empty:
            has_realio = True
            last_age_min = (dt.datetime.now() - df_recent["time"].iloc[-1]).total_seconds() / 60.0
    except Exception:
        pass

    if has_realio:
        status_html = (f"<div style='background:#e6f4ea;border-left:4px solid #2E7D32;padding:8px 14px;"
                        f"border-radius:6px;font-size:13px;color:#1b5e20'>"
                        f"✓ Realio dáta dostupné — posledný záznam pred <b>{last_age_min:.1f} min</b>. "
                        f"Simulačné hodnoty FTV/Sieť/SOC/batt sú nahradené reálnymi meraniami pre minúty "
                        f"kde máme záznam. Plány (D-1 nominácia) zostávajú z modelu.</div>")
    else:
        status_html = ("<div style='background:#fff3cd;border-left:4px solid #B45309;padding:8px 14px;"
                        "border-radius:6px;font-size:13px;color:#7a3500'>"
                        "⚠ Realio nemá zatiaľ dáta v <code>out/&lt;market&gt;/realio_measurements.csv</code>. "
                        "V záložke <a href='/realio?tab=nastavenie' style='color:#7a3500;font-weight:600'>"
                        "&#9881; Nastavenie</a> zapni &bdquo;Modul akt&iacute;vny&ldquo; a klikni "
                        "&bdquo;&#128269; Test &#269;&iacute;tania&ldquo; &mdash; alebo po&#269;kaj k&yacute;m "
                        "poll job prid&aacute; prv&yacute; z&aacute;znam (~1 min).</div>")

    msg_html = ""
    if msg:
        bg = {"ok": "#e6f4ea", "info": "#eef3f9", "err": "#ffeaea"}.get(msg_kind, "#eef3f9")
        bd = {"ok": "#2E7D32", "info": "#1F4E78", "err": "#C0392B"}.get(msg_kind, "#1F4E78")
        msg_html = f"<div style='background:{bg};border-left:4px solid {bd};padding:10px 14px;border-radius:8px;margin:10px 0'>{msg}</div>"

    # iframe src — propaguj profile cez query
    from urllib.parse import urlencode
    iframe_qs = urlencode({"realio_overlay": "1", "profile": selected_profile})
    iframe_src = f"/livesim?{iframe_qs}"

    # Hlavný obsah — buď iframe (selected je real) alebo warning
    if selected_mode == "real":
        # Date defaults pre control panel forms
        _today = dt.date.today()
        _plan_default_from = (_today - dt.timedelta(days=7)).isoformat()
        _plan_default_to = _today.isoformat()
        # History backfill — len dnešný deň (od 00:00 do teraz). Backend expandne to_date na koniec dňa.
        _hist_default_from = _today.isoformat()
        _hist_default_to = _today.isoformat()

        control_panel = f"""
<div style="background:#f3f6fb;border:1px solid #d6dce5;border-radius:10px;padding:14px 18px;margin:14px 0">
<h2 style="margin:0 0 8px;font-size:15px;color:#1F4E78">⚙ Príprava dát pre reálne riadenie</h2>
<p style="color:#666;font-size:12px;margin:0 0 10px">Pred reálnym riadením treba mať
(a) D-1 plány pre dni v ktorých riadime a (b) historické realio merania
pre porovnanie. Oba operácie môžeš spustiť pre intervaly nižšie.</p>

<div style="display:flex;gap:18px;flex-wrap:wrap;align-items:flex-start">

<form method="post" action="/plan_batch" target="_blank"
      style="flex:1;min-width:340px;background:#fff;border:1px solid #e0e0e0;
             border-radius:8px;padding:12px 14px">
  <h3 style="margin:0 0 8px;font-size:14px;color:#1F4E78">📋 Generovať plán D-1 (od — do)</h3>
  <p style="color:#666;font-size:11px;margin:0 0 8px">Vygeneruje nominácie pre aktívny profil
  cez celý interval. Otvorí sa v novom okne (priebeh streamuje).</p>
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:end">
    <label style="display:flex;flex-direction:column;font-size:12px">od
      <input type="date" name="from_date" value="{_plan_default_from}" required
             style="padding:5px 8px;border:1px solid #ccc;border-radius:5px"></label>
    <label style="display:flex;flex-direction:column;font-size:12px">do
      <input type="date" name="to_date" value="{_plan_default_to}" required
             style="padding:5px 8px;border:1px solid #ccc;border-radius:5px"></label>
    <label style="display:flex;flex-direction:column;font-size:12px">krok
      <select name="step_min" style="padding:5px 8px;border:1px solid #ccc;border-radius:5px">
        <option value="60" selected>60 min (plán D-1)</option>
        <option value="15">15 min (denný trh)</option>
      </select></label>
    <button type="submit" style="background:#1F4E78;color:#fff;border:0;padding:7px 14px;
            border-radius:6px;font-weight:600;cursor:pointer;font-size:13px">📋 Generuj plán</button>
  </div>
</form>

<form method="post" action="/realio/backfill_range"
      onsubmit="return _checkRange(this);"
      style="flex:1;min-width:340px;background:#fff;border:1px solid #e0e0e0;
             border-radius:8px;padding:12px 14px">
  <h3 style="margin:0 0 8px;font-size:14px;color:#C62828">📥 Stiahnuť realio históriu (od — do)</h3>
  <p style="color:#666;font-size:11px;margin:0 0 8px">Dotiahne minútové merania z Bender
  servera za interval. <b>Max 2 dni</b> naraz (server by mohol crashnúť pri väčšom rozsahu).
  Použiteľné keď appka nebežala alebo treba spätne doplniť záznamy.</p>
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:end">
    <label style="display:flex;flex-direction:column;font-size:12px">od
      <input type="date" name="from_date" value="{_hist_default_from}" required
             style="padding:5px 8px;border:1px solid #ccc;border-radius:5px"></label>
    <label style="display:flex;flex-direction:column;font-size:12px">do
      <input type="date" name="to_date" value="{_hist_default_to}" required
             style="padding:5px 8px;border:1px solid #ccc;border-radius:5px"></label>
    <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:#7a3500;cursor:pointer"
           title="Pred merge odstráni existujúce záznamy v rozsahu — užitočné na vyplnenie dier.">
      <input type="checkbox" name="overwrite" value="1">
      <span>Prepísať existujúce</span></label>
    <button id="hist-btn" type="submit" style="background:#C62828;color:#fff;border:0;padding:7px 14px;
            border-radius:6px;font-weight:600;cursor:pointer;font-size:13px">📥 Stiahnuť históriu</button>
  </div>
  <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;align-items:center">
    <button type="button" formaction="/realio/relogin" formmethod="post" formtarget="_self"
            onclick="this.form.action='/realio/relogin';this.form.submit();return false;"
            style="background:#5E35B1;color:#fff;border:0;padding:6px 12px;border-radius:6px;
                   font-size:12px;font-weight:600;cursor:pointer">🔄 Refresh cookies</button>
    <button type="button"
            onclick="if(confirm('Vyčistí CSV od duplicitných a prázdnych riadkov. Pokračovať?'))
                     {{var f=document.createElement('form');f.method='post';
                       f.action='/realio/cleanup_csv';document.body.appendChild(f);f.submit();}}"
            style="background:#37474F;color:#fff;border:0;padding:6px 12px;border-radius:6px;
                   font-size:12px;font-weight:600;cursor:pointer">🧹 Vyčistiť CSV</button>
    <button type="button"
            onclick="if(confirm('Posunie všetky DB riadky s timestampom v budúcnosti o -2h (oprava po TZ fixe). Pokračovať?'))
                     {{var f=document.createElement('form');f.method='post';
                       f.action='/realio/fix_future_timestamps';document.body.appendChild(f);f.submit();}}"
            style="background:#C49000;color:#fff;border:0;padding:6px 12px;border-radius:6px;
                   font-size:12px;font-weight:600;cursor:pointer">⏪ Opraviť future timestamps</button>
    <span style="color:#666;font-size:11px">Refresh cookies pri zlyhaní polling-u • Cleanup zlúči partial riadky • Fix future = oprava po TZ bug fixe</span>
    <div id="hist-progress" style="display:none;align-items:center;gap:8px;font-size:12px;color:#C62828">
      <div class="hist-bar"><div class="hist-bar-inner"></div></div>
      <span>Sťahujem z Bender…</span>
    </div>
    <div id="hist-error" style="color:#C62828;font-size:12px;font-weight:600"></div>
  </div>
</form>

</div>
<style>
.hist-bar{{width:120px;height:6px;background:#ffeaea;border-radius:3px;overflow:hidden;position:relative;display:inline-block}}
.hist-bar-inner{{position:absolute;left:-40%;width:40%;height:100%;background:linear-gradient(90deg,#C62828,#F44336,#C62828);
                  animation:hist-slide 1.2s ease-in-out infinite;border-radius:3px}}
@keyframes hist-slide{{0%{{left:-40%}}100%{{left:100%}}}}
</style>
<script>
function _checkRange(f) {{
  var errEl = document.getElementById('hist-error');
  var prog = document.getElementById('hist-progress');
  var btn = document.getElementById('hist-btn');
  errEl.textContent = '';
  var fd = new Date(f.from_date.value);
  var td = new Date(f.to_date.value);
  if (isNaN(fd) || isNaN(td)) {{
    errEl.textContent = '✗ Zadaj platné dátumy.';
    return false;
  }}
  if (td < fd) {{
    errEl.textContent = '✗ Dátum „do" musí byť po „od".';
    return false;
  }}
  var diff = (td - fd) / 86400000;
  if (diff > 2) {{
    errEl.textContent = '✗ Max 2 dni — zadal si ' + diff.toFixed(1) + ' dní.';
    return false;
  }}
  // OK — zobraz progress bar, deaktivuj tlačítko
  btn.disabled = true;
  btn.style.opacity = '0.5';
  btn.style.cursor = 'not-allowed';
  prog.style.display = 'inline-flex';
  return true;
}}
</script>
</div>

<p style="color:#666;font-size:13px;margin:10px 0 0">Nižšie je živá simulácia ako v
<code>/livesim</code>, ale FTV výkon, výkon na sieti, SOC a výkon batérie sú prepojené
s reálnym meraním (realio CSV). Plány zostávajú simulované — slúžia ako referencia pre
RT controller. Editor FTV scenára nie je dostupný (nemá zmysel keď máme reálne meranie).</p>
<iframe class="livesim" src="{iframe_src}" title="Živá simulácia s realio overlay"></iframe>
"""
        main_html = control_panel
    else:
        from urllib.parse import quote as _quote
        sel_lbl = _quote(selected_profile)
        main_html = f"""
<div style="background:#fff3cd;border-left:6px solid #C62828;padding:18px 22px;border-radius:10px;margin:20px 0">
<h2 style="color:#C62828;margin:0 0 8px">⛔ Reálne riadenie zamietnuté</h2>
<p style="font-size:15px;margin:6px 0">Aktívny profil <b>{selected_profile}</b> je v móde
<b style="color:#2E7D32">🎮 Simulácia</b> — nemôže riadiť reálnu batériu ani FTV.</p>
<p style="margin:10px 0;font-size:14px">Reálne riadenie vyžaduje profil typu <b style="color:#C62828">🔴 Reálny chod</b>.
Tento mód je fixovaný pri vytváraní profilu a nedá sa neskôr prepnúť (bezpečnostná poistka, aby si
nechcene nepustil simulačné testy na živú batériu).</p>
<div style="margin-top:18px;display:flex;gap:10px;flex-wrap:wrap">
<a href="/profiles/edit?name=__new__&preset_mode=real" style="background:#C62828;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600">➕ Vytvoriť real profile</a>
<a href="/profiles" style="background:#1F4E78;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600">⚙ Spravovať profily</a>
</div>
</div>
<details style="margin-top:14px"><summary style="cursor:pointer;color:#666;font-size:13px">Náhľad simulácie (read-only, nedá sa použiť na reálne riadenie)</summary>
<iframe class="livesim" src="/livesim?profile={sel_lbl}" title="Živá simulácia — read-only náhľad"></iframe>
</details>
"""

    return f"""
<!DOCTYPE html>
<html lang="sk"><head><meta charset="utf-8">
<title>🔴 Reálne riadenie — {cust} — FTV+batéria</title>
<!-- Break-out z iframe (zabráni nested vnoreniu keď sa /realio?tab=riadenie načíta vo vnútri /livesim iframu) -->
<script>
if (window.top !== window.self) {{
  try {{ window.top.location.href = window.location.href; }}
  catch(e) {{ /* cross-origin — nevadí, ostane v iframe */ }}
}}
</script>
<style>
body{{font-family:-apple-system,system-ui,Arial;margin:14px;background:#f5f7fb;color:#222}}
.wrap{{max-width:100%;margin:0 auto;background:#fff;border-radius:10px;padding:14px 18px;
       box-shadow:0 2px 8px rgba(0,0,0,.06)}}
h1{{margin:0 0 6px;font-size:22px;display:flex;align-items:center;gap:10px}}
iframe.livesim{{width:100%;height:calc(100vh - 220px);min-height:1400px;border:1px solid #ddd;
                border-radius:10px;background:#fff;margin-top:14px}}
</style></head>
<body>
{_nav("/realio")}
<div class="wrap">
<h1>🔴 Reálne riadenie — {cust}</h1>
<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:6px 0 12px">
<a href="/realio?cust={cust}&tab=vizualizacia" style="color:#1F4E78;text-decoration:none;font-size:13px">← Späť na Vizualizácia</a>
{profile_picker}
</div>
{_realio_customer_header(cust, "riadenie")}
{msg_html}
<div style="margin:14px 0">
{status_html}
</div>
{main_html}
</div>
</body></html>
"""


def _realio_profile_banner() -> str:
    """Banner zobrazujúci aktívny realio-pinned profil + globálne active.
    Tým má user prehľad, ktorý profil sa použije pri manual setpointe (pinned),
    a ktorý sa použije pre /plan, /livesim, atď. (globálny active).
    """
    try:
        import profiles as _pr
        import plan_store as _ps
    except Exception:
        return ""
    try:
        pinned = _resolve_realio_profile()
        global_active = _ps.resolve_profile()
        pinned_mode = _pr.get_mode(pinned) if pinned else ""
        global_mode = _pr.get_mode(global_active) if global_active else ""
    except Exception:
        return ""
    def _chip(name, mode):
        if not name:
            return "<span style='color:#999'>—</span>"
        if mode == "real":
            bg = "#C62828"; icon = "🔴"; lbl = "Real"
        else:
            bg = "#2E7D32"; icon = "🎮"; lbl = "Sim"
        return (f"<span style='background:{bg};color:#fff;padding:3px 9px;border-radius:5px;"
                f"font-size:12px;font-weight:600'>{icon} {name} [{lbl}]</span>")
    same = (pinned == global_active)
    if same:
        body = (f"Aktívny profil pre Realio aj globálne: {_chip(pinned, pinned_mode)} &nbsp;"
                f"<a href='/realio?tab=riadenie' style='font-size:12px;color:#1F4E78'>"
                f"⚙ Zmeniť realio profil</a>")
    else:
        body = (f"<b>Realio</b> používa: {_chip(pinned, pinned_mode)} &nbsp;|&nbsp; "
                f"<b>Globálne</b>: {_chip(global_active, global_mode)} &nbsp;"
                f"<a href='/realio?tab=riadenie' style='font-size:12px;color:#1F4E78'>"
                f"⚙ Zmeniť realio profil</a>")
    return (f"<div style='background:#eef3f9;border-left:4px solid #1F4E78;padding:8px 14px;"
            f"border-radius:6px;font-size:13px;margin:10px 0'>{body}</div>")


def _realio_nastavenie_page(msg: str = "", msg_kind: str = "info", cust: str = "Trakany") -> str:
    """Render /realio?tab=nastavenie — konfigurácia tagov + live status + setpoint write."""
    try:
        import realio as _rio
    except ImportError:
        return ("<p style='color:#C0392B'>realio modul nedostupný (chyba importu).</p>"
                "<a href='/'>← Späť</a>")
    cfg = _rio.load_config()
    # Live status — posledné hodnoty (volá iba ak enabled)
    live = None
    if cfg.get("enabled"):
        try:
            live = _rio.fetch_latest_all()
        except Exception as _e:
            live = {"_error": str(_e)}
    # Last writes (audit)
    lw = cfg.get("last_write") or {}

    def _fld(name, val, ph="", w="260px"):
        v = "" if val is None else str(val)
        return (f'<label style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin:4px 0">'
                f'<span style="min-width:160px">{name}</span>'
                f'<input name="{name}" value="{v}" placeholder="{ph}" type="text" '
                f'style="width:{w};padding:5px 8px;border:1px solid #ccc;border-radius:6px"></label>')

    tr = cfg.get("tags_read") or {}
    tw = cfg.get("tags_write") or {}
    fc = cfg.get("fve_control") or {}

    def _live_val(k, unit=""):
        if not live:
            return "<span style='color:#999'>—</span>"
        v = live.get(k)
        if v is None:
            return "<span style='color:#C0392B'>chýba</span>"
        try:
            return f"<b style='color:#2E7D32'>{float(v):,.1f}</b> {unit}"
        except Exception:
            return f"<span style='color:#999'>{v}</span>"

    live_html = ""
    if cfg.get("enabled"):
        if live and not live.get("_error"):
            live_html = (
                "<div style='background:#e6f4ea;border-left:4px solid #2E7D32;border-radius:8px;"
                "padding:12px 16px;margin:10px 0'>"
                f"<b>Live odpočet</b> ({live.get('_ts','?')}):<br>"
                f"&nbsp;FTV výkon: {_live_val('ftv_power_kw','kW')} "
                f"&nbsp;|&nbsp; Spotreba: {_live_val('load_power_kw','kW')} "
                f"&nbsp;|&nbsp; Batéria: {_live_val('batt_power_kw','kW')} "
                f"&nbsp;|&nbsp; SOC: {_live_val('batt_soc_pct','%')} "
                f"&nbsp;|&nbsp; Grid: {_live_val('grid_power_kw','kW')}"
                "</div>")
        elif live and live.get("_error"):
            live_html = (f"<div style='background:#ffeaea;border-left:4px solid #C0392B;border-radius:8px;"
                          f"padding:12px 16px;margin:10px 0'>"
                          f"<b>⚠ Chyba odpočtu</b>: {live['_error']}</div>")
        else:
            live_html = ("<div style='background:#fff3cd;border-left:4px solid #f0b80f;border-radius:8px;"
                          "padding:12px 16px;margin:10px 0'>Modul je enabled ale nemá nakonfigurované read tagy.</div>")
    else:
        live_html = ("<div style='background:#eef3f9;border-left:4px solid #1F4E78;border-radius:8px;"
                      "padding:12px 16px;margin:10px 0'>Modul je <b>vypnutý</b> — nastav tagy nižšie, "
                      "potom zapni v sekcii Bezpečnosť.</div>")

    # Last setpoint audit
    audit_html = ""
    has_audit = (lw.get("batt_setpoint_kw") is not None
                 or lw.get("ftv_curtail_kw") is not None
                 or lw.get("fve_pct") is not None)
    if has_audit:
        rows = ""
        if lw.get("batt_setpoint_kw") is not None:
            rows += (f"<tr><td>Batéria setpoint</td><td><b>{lw['batt_setpoint_kw']:.1f} kW</b></td>"
                      f"<td>{lw.get('batt_setpoint_kw_ts','?')}</td>"
                      f"<td style='color:#666'>{lw.get('batt_setpoint_kw_source','?')}</td></tr>")
        if lw.get("ftv_curtail_kw") is not None:
            rows += (f"<tr><td>FTV orezanie</td><td><b>{lw['ftv_curtail_kw']:.1f} kW</b></td>"
                      f"<td>{lw.get('ftv_curtail_kw_ts','?')}</td>"
                      f"<td style='color:#666'>{lw.get('ftv_curtail_kw_source','?')}</td></tr>")
        if lw.get("fve_pct") is not None:
            rows += (f"<tr><td>☀ FVE výkon</td><td><b>{lw['fve_pct']} %</b></td>"
                      f"<td>{lw.get('fve_pct_ts','?')}</td>"
                      f"<td style='color:#666'>{lw.get('fve_pct_source','?')}</td></tr>")
        audit_html = (
            "<h3>Posledné odoslané setpointy</h3>"
            "<table style='border-collapse:collapse;font-size:13px;width:100%'>"
            "<tr style='background:#1F4E78;color:#fff'>"
            "<th style='padding:6px 10px;text-align:left'>Tag</th>"
            "<th style='padding:6px 10px;text-align:right'>Hodnota</th>"
            "<th style='padding:6px 10px;text-align:left'>Čas</th>"
            "<th style='padding:6px 10px;text-align:left'>Zdroj</th></tr>"
            f"{rows}</table>")

    msg_html = ""
    if msg:
        bg = {"ok": "#e6f4ea", "info": "#eef3f9", "err": "#ffeaea"}.get(msg_kind, "#eef3f9")
        bd = {"ok": "#2E7D32", "info": "#1F4E78", "err": "#C0392B"}.get(msg_kind, "#1F4E78")
        msg_html = f"<div style='background:{bg};border-left:4px solid {bd};padding:10px 14px;border-radius:8px;margin:10px 0'>{msg}</div>"

    # ── Polling status banner ──
    try:
        import realio as _rio2
        status = _rio2.poll_status()
    except Exception:
        status = {"enabled": False, "rows": 0}
    if status.get("enabled"):
        mm = status.get("minutes_since_last")
        if mm is None:
            stat_color, stat_icon = "#888", "○"
            stat_msg = "CSV ešte žiadne dáta (čaká na prvý poll cycle, max do {} s)".format(status.get("poll_interval_s", 60))
        elif mm < 3:
            stat_color, stat_icon = "#2E7D32", "●"
            stat_msg = f"Polling beží — posledný odpočet pred {mm:.1f} min ({status.get('rows'):,} riadkov v CSV)"
        elif mm < 30:
            stat_color, stat_icon = "#C49000", "◐"
            stat_msg = f"Polling je oneskorený — posledný odpočet pred {mm:.0f} min ({status.get('rows'):,} riadkov)"
        else:
            stat_color, stat_icon = "#C0392B", "✕"
            stat_msg = f"Polling NEBEŽÍ — posledný odpočet pred {mm:.0f} min. Skontroluj scheduler logy alebo cookies."
        status_banner = (
            f"<div style='background:#fafbfc;border:1px solid {stat_color};border-radius:8px;"
            f"padding:8px 14px;margin:10px 0;display:flex;align-items:center;gap:10px'>"
            f"<span style='font-size:18px;color:{stat_color}'>{stat_icon}</span>"
            f"<span><b>Auto polling:</b> {stat_msg}</span>"
            f"<span style='color:#666;font-size:12px;margin-left:auto'>interval {status.get('poll_interval_s')} s</span>"
            f"</div>")
    else:
        status_banner = (
            "<div style='background:#fafbfc;border:1px solid #888;border-radius:8px;"
            "padding:8px 14px;margin:10px 0'>"
            "<span style='color:#666'>○ <b>Auto polling vypnutý</b> — zaškrtni „Modul aktívny" + "“"
            " v sekcii Bezpečnosť a ulož, polling sa spustí automaticky každú minútu cez scheduler.</span></div>")

    return f"""<!doctype html><html lang="sk"><head><meta charset="utf-8">
<title>Reálne meranie + riadenie</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:-apple-system,Segoe UI,Arial;max-width:1100px;margin:24px auto;padding:0 16px;color:#222}}
h1{{color:#1F4E78}} h2{{color:#2E75B6;margin:18px 0 8px}}
h3{{color:#1F4E78;font-size:15px;margin:12px 0 4px}}
fieldset{{border:1px solid #e0e0e0;border-radius:10px;margin:12px 0;padding:12px 16px}}
legend{{color:#2E75B6;font-weight:600}}
button{{cursor:pointer;border:0;color:#fff;padding:9px 16px;border-radius:7px;font-size:14px;font-weight:600;margin-right:6px}}
.btn-save{{background:#2E7D32}} .btn-test{{background:#1F4E78}} .btn-send{{background:#5E35B1}}
.btn-danger{{background:#C0392B}}
.cols2{{display:grid;grid-template-columns:1fr 1fr;gap:0 24px}}
table th, table td{{border:1px solid #e3e3e3;padding:5px 9px}}
input[type=text],input[type=number]{{padding:5px 8px;border:1px solid #ccc;border-radius:6px;font-size:13px}}
</style></head><body>
<h1>🔌 Reálne meranie + riadenie batérie</h1>
{_nav("/realio")}
{_realio_customer_header(cust, "nastavenie")}
<p style="color:#666;margin-top:14px">Čítanie reálnych meraní (FTV výkon, spotreba, SOC batérie, výkon na prahu) a
zapisovanie setpoint commandov cez interný historian (DAMSU pattern). Tagy sa konfigurujú nižšie.</p>
{msg_html}
{status_banner}
{_realio_profile_banner()}
{live_html}

<form method="post" action="/realio/save">
<fieldset><legend>🔗 Pripojenie</legend>
<div class="cols2">
<div>
{_fld("host", cfg.get("host"), "https://10.200.136.21", "300px")}
{_fld("endpoint_path", cfg.get("endpoint_path","/tag-data"), "/tag-data", "200px")}
{_fld("poll_interval_s", cfg.get("poll_interval_s", 60), "60 (sekundy)", "120px")}
</div><div>
{_fld("username", cfg.get("username","admin"), "admin (fallback auto-login)", "180px")}
{_fld("password", cfg.get("password","admin"), "admin (fallback auto-login)", "180px")}
<label style="display:flex;align-items:center;gap:8px;margin:4px 0">
  <input type="checkbox" name="verify_ssl" {"checked" if cfg.get("verify_ssl") else ""}>
  <span>Verifikovať SSL certifikát (default vypnuté pre LAN/self-signed)</span>
</label>
</div></div>
<div style="margin-top:10px">
<label style="display:block;font-size:13px;color:#555;margin-bottom:4px">
  <b>🍪 Manuálne cookies</b> (PREFEROVANÉ — Bender Express.js servery zvyčajne nemajú jednotný login API):
</label>
<textarea name="cookies" rows="3" placeholder="connect.sid=s%3A...; Bender-Authenticate=6eecb9f3-..."
  style="width:100%;padding:6px 8px;border:1px solid #ccc;border-radius:6px;font-family:Menlo,monospace;font-size:12px">{cfg.get("cookies","")}</textarea>
<p style="color:#666;font-size:12px;margin:4px 0">
  <b>Ako získať cookies:</b> Otvor <code>{cfg.get('host','https://10.200.136.21')}</code> v Chrome,
  prihlás sa <code>admin/admin</code>, F12 → Network → klikni na <code>tag-data</code> alebo iný request →
  v Request Headers nájdi riadok <code>Cookie:</code> → skopíruj celý obsah za „Cookie: " a vlož sem.
  Príklad: <code>connect.sid=s%3A1T9SRa...; Bender-Authenticate=6eecb9f3-1586-...</code>
</p>
</div>
</fieldset>

<fieldset><legend>⚖ Scale faktory (raw → kW/%)</legend>
<p style="color:#666;font-size:13px;margin:0 0 6px">Hodnoty v dashboarde sú vo <b>Wattoch</b> → ×0.001 pre kW. SOC je v %, nemení sa (×1.0).
Ak má niektorý tag iný jednotku, uprav faktor.</p>
<div class="cols2">
<div>
{_fld("scale_ftv_power_kw",  cfg.get("scale_read",{}).get("ftv_power_kw",0.001),  "0.001 (W→kW)", "100px")}
{_fld("scale_load_power_kw", cfg.get("scale_read",{}).get("load_power_kw",0.001), "0.001 (W→kW)", "100px")}
{_fld("scale_batt_power_kw", cfg.get("scale_read",{}).get("batt_power_kw",0.001), "0.001 (W→kW)", "100px")}
</div><div>
{_fld("scale_batt_soc_pct",  cfg.get("scale_read",{}).get("batt_soc_pct",1.0),    "1.0 (% = %)", "100px")}
{_fld("scale_grid_power_kw", cfg.get("scale_read",{}).get("grid_power_kw",0.001), "0.001 (W→kW)", "100px")}
</div></div>
</fieldset>

<fieldset><legend>📥 Read tagy (logické názvy → server tag)</legend>
<p style="color:#666;font-size:13px;margin:0 0 8px">Tieto tagy sa čítajú periodicky (každých {cfg.get('poll_interval_s',60)} s)
a zapisujú do out/realio_measurements.csv. Vyplň presné názvy podľa Bender/DAMSU naming convention.</p>
<div class="cols2">
<div>
{_fld("tag_ftv_power_kw",  tr.get("ftv_power_kw"),  "napr. FTV_PWR_kW")}
{_fld("tag_load_power_kw", tr.get("load_power_kw"), "napr. LOAD_PWR_kW")}
{_fld("tag_batt_power_kw", tr.get("batt_power_kw"), "napr. BATT_PWR_kW")}
</div><div>
{_fld("tag_batt_soc_pct",  tr.get("batt_soc_pct"),  "napr. BATT_SOC_pct")}
{_fld("tag_grid_power_kw", tr.get("grid_power_kw"), "napr. GRID_PWR_kW")}
</div></div>
</fieldset>

<fieldset><legend>📤 Write tagy (setpoint riadenie batérie)</legend>
<p style="color:#666;font-size:13px;margin:0 0 8px"><b>Pozor:</b> tieto tagy ovládajú reálnu batériu / inverter.
Trakany protokol: pri každom zápise setpoint sa <b>dvojicovo</b> pošle Manual_Plan (mode = enable_value)
+ Param3 (power vo W). Oba s rovnakou minútovo zarovnanou časovou značkou.</p>
<div class="cols2">
<div>
{_fld("tag_batt_setpoint_kw",  tw.get("batt_setpoint_kw"),  "REG_Regulator_Param3 (W)")}
{_fld("tag_batt_control_mode", tw.get("batt_control_mode"), "REG_Regulator_Manual_Plan")}
{_fld("tag_ftv_curtail_kw",    tw.get("ftv_curtail_kw"),    "voliteľné FTV_LIMIT_kW")}
</div><div>
{_fld("control_mode_enable_value",  cfg.get("control_mode_enable_value", 2),  "2 (Manual_Plan enable)", "100px")}
{_fld("control_mode_disable_value", cfg.get("control_mode_disable_value", 0), "0 (Manual_Plan auto)",   "100px")}
</div></div>
</fieldset>

<fieldset><legend>☀ FVE setpoint (Huawei SmartLogger cez SSH + Modbus)</legend>
<p style="color:#666;font-size:13px;margin:0 0 8px">Riadenie činného výkonu FVE — SSH na jump host
<code>ssh_user@ssh_host:ssh_port</code> + <code>modpoll</code> zápis registra
<code>40428</code> (Active power adjustment by percentage, gain 10) v Huawei SmartLogger.
<b>0%</b> = FTV vypnutá, <b>100%</b> = max výkon. Implementácia v <code>fve_setpoint.py</code>.</p>
<div class="cols2">
<div>
{_fld("fve_ssh_host",  fc.get("ssh_host", "10.200.136.21"), "SSH host (jump)")}
{_fld("fve_ssh_user",  fc.get("ssh_user", "support"),        "SSH user", "120px")}
{_fld("fve_ssh_port",  fc.get("ssh_port", 8222),             "SSH port", "100px")}
{_fld("fve_ssh_key",   fc.get("ssh_key",  "~/.ssh/support.rsa"), "SSH key path")}
</div><div>
{_fld("fve_device_ip", fc.get("device_ip", "192.168.1.250"), "SmartLogger IP z jump hosta")}
{_fld("fve_modpoll",   fc.get("modpoll", "modpoll"),         "modpoll cesta na jump hoste")}
{_fld("fve_slave_id",  fc.get("slave_id", 0),                "Modbus slave ID", "100px")}
{_fld("fve_register",  fc.get("ctrl_register", 40428),       "Riadiaci register", "120px")}
{_fld("fve_gain",      fc.get("gain", 10),                   "Gain (pct × gain)", "100px")}
</div></div>
<label style="display:flex;align-items:center;gap:10px;margin:8px 0 0;color:#7a3500">
  <input type="checkbox" name="fve_enabled" {"checked" if fc.get("enabled") else ""}>
  <span><b>☀ FVE control povolený</b> — bez tohto je <b>každý</b> FVE setpoint zamietnutý.
  Vyžaduje sa aj <code>control_enabled</code> nižšie.</span>
</label>
</fieldset>

<fieldset><legend>🛡 Bezpečnosť — master switche</legend>
<label style="display:flex;align-items:center;gap:10px;margin:6px 0">
  <input type="checkbox" name="enabled" {"checked" if cfg.get("enabled") else ""}>
  <span><b>Modul aktívny</b> — povolí periodické čítanie a logovanie do CSV</span>
</label>
<label style="display:flex;align-items:center;gap:10px;margin:6px 0;color:#7a3500">
  <input type="checkbox" name="control_enabled" {"checked" if cfg.get("control_enabled") else ""}>
  <span><b>⚡ Write/control povolený</b> — bez tohto je <b>každý</b> setpoint write zamietnutý
  (platí pre batt aj FVE). Po prvom zapnutí choď cez „Test write" nižšie aby si overil že nemení nič nečakané.</span>
</label>
</fieldset>

<div style="margin:12px 0;display:flex;gap:8px;flex-wrap:wrap">
<button type="submit" class="btn-save">💾 Uložiť konfiguráciu</button>
<button type="submit" formaction="/realio/test_read" class="btn-test">🔍 Test čítania (latest)</button>
<button type="submit" formaction="/realio/relogin" style="background:#5E35B1;color:#fff;border:0;padding:9px 16px;border-radius:7px;font-weight:600;cursor:pointer">🔄 Refresh cookies (Playwright login)</button>
</div>
</form>

<fieldset style="margin-top:18px"><legend>📤 Manuálny setpoint (test) — dual-write protokol</legend>
<p style="color:#666;font-size:13px;margin:0 0 8px">Pri batt setpoint sa pošle súčasne <code>Manual_Plan=enable_value</code> + <code>Param3=value×1000</code> (W) s rovnakým minútovo-zarovnaným timestampom. Pri uvolnení kontroly sa pošle iba <code>Manual_Plan=disable_value</code>.</p>
<form method="post" action="/realio/write" style="display:flex;gap:10px;align-items:end;flex-wrap:wrap">
<label style="display:flex;flex-direction:column;font-size:13px">
  Logický tag:
  <select name="logical" style="padding:5px 8px;border:1px solid #ccc;border-radius:6px">
    <option value="batt_setpoint_kw">batt_setpoint_kw (batéria kW; + vybíja / − nabíja)</option>
    <option value="ftv_curtail_kw">ftv_curtail_kw (FTV limit kW)</option>
  </select>
</label>
<label style="display:flex;flex-direction:column;font-size:13px">
  Hodnota [kW]:
  <input type="number" name="value" step="0.1" value="0" style="width:120px">
</label>
<button type="submit" class="btn-send">📤 Odoslať setpoint</button>
</form>
<form method="post" action="/realio/disable_control" style="display:flex;gap:10px;align-items:center;margin-top:10px">
<button type="submit" class="btn-danger" onclick="return confirm('Naozaj uvoľniť riadenie? Pošle Manual_Plan=0 — batéria sa vráti do auto módu.')">🛑 Vypnúť manual control (Manual_Plan = 0)</button>
<span style="color:#666;font-size:12px">Príkaz uvolní externé riadenie a vráti batériu do auto módu.</span>
</form>
</fieldset>

<fieldset style="margin-top:18px"><legend>☀ Manuálny FVE setpoint (Huawei SmartLogger)</legend>
<p style="color:#666;font-size:13px;margin:0 0 8px">Nastavenie činného výkonu FVE v percentách (0..100). Pošle sa cez SSH+modpoll do registra <code>40428</code>: zápisová hodnota = <code>pct × gain</code>. <b>0 %</b> = FTV vypnutá, <b>100 %</b> = max výkon.</p>
<form method="post" action="/realio/fve_write" style="display:flex;gap:10px;align-items:end;flex-wrap:wrap">
<label style="display:flex;flex-direction:column;font-size:13px">
  Výkon FVE [%]:
  <input type="number" name="value" step="1" min="0" max="100" value="100" style="width:120px;padding:5px 8px;border:1px solid #ccc;border-radius:6px">
</label>
<button type="submit" style="background:#F9A825;color:#1a1a1a;border:0;padding:8px 16px;border-radius:7px;font-weight:700;cursor:pointer">☀ Odoslať FVE setpoint</button>
<span style="color:#666;font-size:12px">Vyžaduje obe — <code>control_enabled</code> aj <code>fve_control.enabled</code>.</span>
</form>
<div style="margin-top:8px;display:flex;gap:10px;flex-wrap:wrap">
<form method="post" action="/realio/fve_write" style="display:inline">
  <input type="hidden" name="value" value="100">
  <button type="submit" style="background:#388E3C;color:#fff;border:0;padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer">⚡ 100% (max)</button>
</form>
<form method="post" action="/realio/fve_write" style="display:inline">
  <input type="hidden" name="value" value="50">
  <button type="submit" style="background:#FB8C00;color:#fff;border:0;padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer">⚙ 50%</button>
</form>
<form method="post" action="/realio/fve_write" style="display:inline">
  <input type="hidden" name="value" value="0" onclick="return confirm('Naozaj vypnúť FTV (0%)? Inverter zastaví produkciu.');">
  <button type="submit" style="background:#C62828;color:#fff;border:0;padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer" onclick="return confirm('Naozaj vypnúť FTV (0%)? Inverter zastaví produkciu.');">🛑 0% (vypnúť FTV)</button>
</form>
</div>

<!-- Discovery tlačidlá odstránené — write protokol je teraz implementovaný cez
     POST /tag-set-new-values (zistené 2026-05-31 z F12 capture).
     Endpointy /realio/discover_write, /realio/scan_js, /realio/probe_ws,
     /realio/scan_msg_types, /realio/ws_listen sú stále registrované pre prípad
     potreby ďalšieho ladenia, ale UI tlačidlá sú schované. -->
</fieldset>

{audit_html}

<fieldset style="margin-top:18px"><legend>📥 Backfill histórie z dashboardu</legend>
<p style="color:#666;font-size:13px;margin:0 0 6px">Stiahne históriu vybraného obdobia priamo z DAMSU
a uloží do lokálneho CSV (s deduplikáciou podľa času). Užitočné keď chceš dohnať dni keď appka nebežala,
alebo natiahnuť dlhšiu históriu pred prvým spustením livesim.</p>
<form method="post" action="/realio/backfill" style="display:flex;gap:10px;align-items:end;flex-wrap:wrap">
<label style="display:flex;flex-direction:column;font-size:13px">
  Počet dní späť:
  <input type="number" name="days" value="7" min="1" max="365" style="width:100px">
</label>
<label style="display:flex;flex-direction:column;font-size:13px">
  Max bodov per tag:
  <input type="number" name="count_per_tag" value="10000" min="100" max="100000" step="1000" style="width:120px">
</label>
<button type="submit" class="btn-test">📥 Spustiť backfill</button>
<span style="color:#666;font-size:12px">Pre 7 dní × 5 tagov ≈ 50k bodov, môže trvať ~30 s.</span>
</form>
</fieldset>

<h2>História — posledné minúty z lokálneho CSV</h2>
<p style="color:#666;font-size:13px">Súbor: <code>out/realio_measurements.csv</code> (každý riadok = jeden poll cyklus).</p>
<div id="last_csv">{_realio_recent_table()}</div>

</body></html>"""


def _realio_recent_table(n: int = 12) -> str:
    """Vráti HTML tabuľku s poslednými N riadkami z CSV."""
    try:
        import realio as _rio
        df = _rio.read_recent(n_minutes=240)
        if df is None or df.empty:
            return "<p style='color:#999'>(zatiaľ žiadne meranie)</p>"
        df = df.sort_values("time", ascending=False).head(n)
        cols = ["time", "ftv_power_kw", "load_power_kw", "batt_power_kw",
                "batt_soc_pct", "grid_power_kw", "batt_setpoint_kw_cmd"]
        cols = [c for c in cols if c in df.columns]
        hdr = "".join(f"<th style='padding:5px 9px;text-align:right'>{c}</th>" for c in cols)
        rows = ""
        for _, r in df.iterrows():
            tds = ""
            for c in cols:
                v = r[c]
                tds += f"<td style='padding:4px 9px;text-align:right'>{'' if pd.isna(v) else v}</td>"
            rows += f"<tr>{tds}</tr>"
        return (f"<table style='border-collapse:collapse;font-size:12px;width:100%'>"
                f"<tr style='background:#1F4E78;color:#fff'>{hdr}</tr>{rows}</table>")
    except Exception as _e:
        return f"<p style='color:#C0392B'>Chyba načítania: {_e}</p>"


def _render_settlement_card(settle: dict) -> str:
    """Vykreslí kartu „Bilancia dnešného dňa" s rozkladom DAM + VDT + Odchýlka.

    Vstup z `daily_settlement.compute_daily_settlement`.
    """
    import html as _html
    if not settle or not settle.get("ok"):
        return ""
    dam = settle.get("dam") or {}
    vdt = settle.get("vdt") or {}
    dev = settle.get("deviation") or {}
    date_s = settle.get("date", "?")
    prof_s = settle.get("profile", "?")
    is_today = settle.get("is_today", False)
    total = float(settle.get("total_eur", 0))

    def _eur(v, color_pos="#28a745", color_neg="#C62828", neutral="#666"):
        try:
            vf = float(v)
        except Exception:
            return "<span style='color:#999'>—</span>"
        if abs(vf) < 0.01:
            return f"<span style='color:{neutral}'>0.00 €</span>"
        clr = color_pos if vf > 0 else color_neg
        return f"<span style='color:{clr};font-weight:700'>{vf:+.2f} €</span>"

    def _kwh(v, decimals=0):
        try:
            return f"{float(v):,.{decimals}f}".replace(",", " ")
        except Exception:
            return "—"

    def _price(v):
        try:
            return f"{float(v):.1f}"
        except Exception:
            return "—"

    status_chip = ("priebežná (live)" if is_today
                    else ("uzavretá (settled)" if dev.get("settled") else "predbežná"))
    chip_bg = "#FF9800" if is_today else ("#28a745" if dev.get("settled") else "#999")

    # DAM riadok
    dam_html = (
        f"<tr><td style='padding:6px 10px;width:32%'>"
        f"<b style='color:#1F4E78'>D-1 DAM plán</b>"
        f"<div style='font-size:11px;color:#888'>záväzná nominácia × reálna DAM cena</div></td>"
        f"<td style='padding:6px 10px;width:42%;font-size:12px;color:#555'>"
        f"📤 predaj <b>{_kwh(dam.get('kwh_sell',0))} kWh</b> "
        f"@ {_price(dam.get('price_avg_sell'))} €/MWh "
        f"= <span style='color:#28a745'>+{float(dam.get('revenue_eur',0)):.2f} €</span><br>"
        f"📥 nákup <b>{_kwh(dam.get('kwh_buy',0))} kWh</b> "
        f"@ {_price(dam.get('price_avg_buy'))} €/MWh "
        f"= <span style='color:#C62828'>−{float(dam.get('cost_eur',0)):.2f} €</span>"
        f"</td>"
        f"<td style='padding:6px 10px;text-align:right;font-size:16px'>"
        f"{_eur(dam.get('net_eur',0))}</td></tr>"
    )

    # VDT extras riadok
    vdt_html = (
        f"<tr><td style='padding:6px 10px'>"
        f"<b style='color:#AD1457'>VDT extras (intraday)</b>"
        f"<div style='font-size:11px;color:#888'>{vdt.get('n_trades',0)} paper trades</div></td>"
        f"<td style='padding:6px 10px;font-size:12px;color:#555'>"
        f"🛒 BUY <b>{_kwh(vdt.get('buy_kwh',0))} kWh</b> "
        f"= <span style='color:#C62828'>−{float(vdt.get('cost_eur',0)):.2f} €</span><br>"
        f"💰 SELL <b>{_kwh(vdt.get('sell_kwh',0))} kWh</b> "
        f"= <span style='color:#28a745'>+{float(vdt.get('revenue_eur',0)):.2f} €</span>"
        + (f"<br>✂️ CURTAIL_FTV <b>{_kwh(vdt.get('curtail_ftv_kwh',0))} kWh</b>"
           if float(vdt.get('curtail_ftv_kwh', 0)) > 0.5 else "")
        + (f"<br>🔌 LOAD_COVER <b>{_kwh(vdt.get('load_cover_kwh',0))} kWh</b>"
           if float(vdt.get('load_cover_kwh', 0)) > 0.5 else "")
        + f"</td>"
        f"<td style='padding:6px 10px;text-align:right;font-size:16px'>"
        f"{_eur(vdt.get('net_eur',0))}</td></tr>"
    )

    # Odchýlka × ZCO riadok
    dev_settled = "✓ ZCO real" if dev.get("settled") else "ZCO predikcia"
    zco_a = dev.get("zco_avg")
    zco_s = f"priemer {float(zco_a):.0f} €/MWh" if zco_a is not None else "—"
    dev_html = (
        f"<tr><td style='padding:6px 10px'>"
        f"<b style='color:#6A1B9A'>Odchýlka × ZCO</b>"
        f"<div style='font-size:11px;color:#888'>{dev_settled} · {zco_s}</div></td>"
        f"<td style='padding:6px 10px;font-size:12px;color:#555'>"
        f"deficit <b>{_kwh(dev.get('kwh_deficit',0))} kWh</b> · "
        f"surplus <b>{_kwh(dev.get('kwh_surplus',0))} kWh</b>"
        + (f"<br><i style='color:#999;font-size:11px'>{_html.escape(str(dev.get('diag','')))}</i>"
           if dev.get("diag") else "")
        + f"</td>"
        f"<td style='padding:6px 10px;text-align:right;font-size:16px'>"
        f"{_eur(dev.get('cost_eur',0))}</td></tr>"
    )

    total_html = (
        f"<tr style='border-top:2px solid #1F4E78;background:#fafbfc'>"
        f"<td style='padding:10px;font-size:15px;font-weight:700;color:#1F4E78'>"
        f"ČISTÝ DENNÝ VÝSLEDOK</td>"
        f"<td style='padding:10px;text-align:left;font-size:11px;color:#888'>"
        f"DAM + VDT + (Odchýlka × ZCO)</td>"
        f"<td style='padding:10px;text-align:right;font-size:22px;font-weight:800'>"
        f"{_eur(total)}</td></tr>"
    )

    return (
        f"<h2 style='color:#1F4E78;margin-top:20px;font-size:18px'>"
        f"💰 Bilancia dňa {_html.escape(date_s)} "
        f"<span style='background:{chip_bg};color:#fff;padding:2px 10px;"
        f"border-radius:10px;font-size:11px;vertical-align:middle;margin-left:6px'>"
        f"{status_chip}</span>"
        f"<span style='color:#666;font-weight:400;font-size:12px;margin-left:8px'>"
        f"profil <b>{_html.escape(prof_s)}</b></span></h2>"
        f"<table style='width:100%;border-collapse:collapse;border:1px solid #ddd;"
        f"border-radius:8px;background:#fff;font-size:13px'>"
        f"<tbody>{dam_html}{vdt_html}{dev_html}{total_html}</tbody></table>"
        + (f"<p style='color:#aaa;font-size:11px;margin:6px 0 0'>"
           f"{_html.escape(str(settle.get('diag_summary','')))}</p>"
           if settle.get("diag_summary") else "")
    )


@app.get("/vdt/live_advisor", response_class=HTMLResponse)
def vdt_live_advisor_page(
    soc_override: float = -1.0,
    batt_kw: float = 0,
    batt_kwh: float = 0,
    eff_c: float = 0,
    eff_d: float = 0,
    grid_fee: float = 0,
    cycle_cost: float = 0,
    min_spread: float = 0,
    soc_end_min_pct: float = 20.0,
    soc_min_pct: float = 5.0,
    soc_max_pct: float = 95.0,
    max_cycles: float = 3.0,
    profile: str = "",
):
    """🎯 Odporúčanie TERAZ — rolling MPC advisor.

    Pre každý refresh prepočíta plán s aktuálnym SOC + aktuálnym orderbookom.
    Read-only — odporúčanie, nie auto-execute.
    """
    import html as _html
    import json as _json
    try:
        import vdt_arbitrage as _arb
        import vdt_live_advisor as _adv
        import plan_store as _ps
    except ImportError as e:
        return f"<p style='color:#C0392B'>Modul nedostupný: {e}</p>"

    # Resolve EXPLICITNÝ profile — rovnaký mechanizmus ako /vdt/d1
    # (URL param > active profile). To garantuje že Live advisor + D-1
    # používajú TEN ISTÝ profile name pre params, DAM commits aj plan_store lookup.
    try:
        active_profile = _ps.resolve_profile(profile or None)
    except Exception:
        active_profile = profile or "default"

    # Bug OO (2026-06-08): Live advisor sa MUSI riadit profilom (sablonou) —
    # ziadne user-editovatelne overridy v form-e. Predtym form mal SOC override
    # + Batt kW + Batt kWh + eff + Fee + Cyklus + Min spread + SOC koniec atd.
    # User mohol zmenit hodnoty pre tento request → drift voci D-1 planu, voci
    # auto_control schedulerom (ktore citaju z profile) a voci /vdt/d1.
    # FIX: vsetky params predat None → vdt_live_advisor.get_live_recommendation
    # ich nacita z profile.plan (Bug P). SOC vzdy z compute_current_state.
    # URL backwards-compat: query params soc_override/batt_kw/... su ignorovane.
    defaults = _arb.get_default_params_from_profile(profile=active_profile)
    batt_kw = defaults["batt_kw"]
    batt_kwh = defaults["batt_kwh"]
    eff_c = defaults["eff_c"]
    eff_d = defaults["eff_d"]
    grid_fee = defaults["grid_fee"]
    cycle_cost = defaults["cycle_cost"]
    min_spread = defaults["min_spread"]
    # SOC vzdy z vdt_state.compute_current_state (Bug O single source of truth)
    soc_arg = None

    # Posledný cached run (zo scheduler-a) — pre info banner
    cached = None
    try:
        cached = _adv.load_cache()
    except Exception:
        pass

    nav = _nav("/vdt")
    body = (
        f"{nav}"
        f"<div style='max-width:1500px;margin:14px auto;padding:0 16px;"
        f"font-family:-apple-system,Segoe UI,Arial'>"
        f"<h1 style='color:#1F4E78;margin-bottom:4px'>🎯 Odporúčanie TERAZ — Live MPC advisor</h1>"
        f"{_vdt_subnav('/vdt/live_advisor')}"
        f"<p style='color:#666;margin:0 0 12px;font-size:13px'>"
        f"Profile: <b style='background:#1F4E78;color:#fff;padding:2px 10px;border-radius:6px'>"
        f"{_html.escape(active_profile)}</b> &nbsp;|&nbsp; "
        f"Rolling MPC: pri každom refresh prepočíta plán z <b>aktuálneho SOC</b> a "
        f"<b>aktuálneho orderbooku</b>. Reaguje na zmenu cien automaticky. "
        f"<b>Read-only — odporúčanie, žiadne auto-execute do Bender.</b></p>"
    )

    # Bug OO: read-only info box namiesto editovatelneho formulara.
    # Vsetky params su z profilu (sablona) — single source of truth.
    # Tlacidlo iba refreshne (POST nepotreba, GET stranku).
    try:
        _max_cyc_disp = float((_arb.get_default_params_from_profile(
            profile=active_profile) or {}).get("max_cycles", 3.0))
    except Exception:
        _max_cyc_disp = 3.0
    _params_box = (
        f"<div style='background:#eef3f9;padding:12px 16px;border-radius:8px;"
        f"margin:8px 0;border-left:4px solid #1F4E78;font-size:13px'>"
        f"<div style='display:flex;justify-content:space-between;align-items:center;"
        f"margin-bottom:8px;flex-wrap:wrap;gap:8px'>"
        f"<b style='color:#1F4E78'>📋 Parametre z profilu (read-only)</b>"
        f"<div style='display:flex;gap:8px;align-items:center'>"
        f"<a href='/profiles/edit?name={_html.escape(active_profile)}' "
        f"style='background:#5E35B1;color:#fff;padding:5px 11px;border-radius:6px;"
        f"text-decoration:none;font-size:12px;font-weight:600'>✏ Upraviť profil</a>"
        f"<a href='/vdt/live_advisor?profile={_html.escape(active_profile)}' "
        f"style='background:#1F4E78;color:#fff;padding:5px 11px;border-radius:6px;"
        f"text-decoration:none;font-size:12px;font-weight:600'>🔄 Prepočítať teraz</a>"
        f"</div></div>"
        f"<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));"
        f"gap:6px;font-size:12px;color:#33506e'>"
        f"<span><b>Batt:</b> {batt_kw:.0f} kW / {batt_kwh:.0f} kWh</span>"
        f"<span><b>η:</b> nab {eff_c:.2f} · vyb {eff_d:.2f}</span>"
        f"<span><b>Fee:</b> {grid_fee:.1f} €/MWh</span>"
        f"<span><b>Cyklus:</b> {cycle_cost:.1f} €/MWh</span>"
        f"<span><b>Min spread:</b> {min_spread:.1f} €/MWh</span>"
        f"<span><b>Max cyklov:</b> {_max_cyc_disp:.1f}/deň</span>"
        f"<span><b>SOC rozsah:</b> {soc_min_pct:.0f}–{soc_max_pct:.0f}%</span>"
        f"<span><b>SOC koniec ≥:</b> {soc_end_min_pct:.0f}%</span>"
        f"</div>"
        f"<div style='font-size:11px;color:#888;margin-top:6px;line-height:1.4'>"
        f"SOC pre LP sa berie z <code>vdt_state.compute_current_state</code> "
        f"(kumulatívna integrácia od 00:00 + VDT realized). "
        f"Pre zmenu konštánt edituj profil — všetky stránky (D-1, Live advisor, MPC, "
        f"auto_control) ich potom použijú konzistentne.</div>"
        f"</div>"
    )
    body += _params_box
    # Update overriding max_cycles z profile (predtym z URL param)
    max_cycles = _max_cyc_disp

    # ── ZCO predikcia diagnostika (vždy zobraziť) ────────────────────────
    try:
        import zco_advisor as _za_diag
        import deviation_stats as _ds_diag
        import os as _os_diag
        import datetime as _dt_diag
        _today_iso = _dt_diag.date.today().isoformat()
        _sk_prof_exists = _os_diag.path.exists(_ds_diag.PROFILE_PATH_SK)
        # Skús obe metódy a porovnaj
        _sk_pred = _za_diag.predict_zco_for_slots(_today_iso, method="sk_profile") if _sk_prof_exists else {}
        _mean_pred = _za_diag.predict_zco_for_slots(_today_iso, method="mean")
        _used_method = "SK profile" if _sk_pred else ("7-day mean" if _mean_pred else "žiadna")
        _used_color = "#2E7D32" if _sk_pred else ("#E65100" if _mean_pred else "#C0392B")
        _used_icon = "🎯" if _sk_pred else ("📊" if _mean_pred else "⚠")
        # Profile metadata
        _prof_meta = ""
        if _sk_prof_exists:
            try:
                _sk_prof = _ds_diag.load_profile(_ds_diag.PROFILE_PATH_SK)
                _n_days = _sk_prof.get("n_days", 0)
                _span = _sk_prof.get("span", ["?", "?"])
                _by_wd = "✓" if _sk_prof.get("by_weekday") else "✗"
                _by_pv = "✓" if _sk_prof.get("by_pv") else "✗"
                _prof_meta = (f"<span style='color:#666;font-size:12px'>"
                              f"({_n_days} dní, {_span[0]} → {_span[1]}, "
                              f"weekday split {_by_wd}, PV split {_by_pv})</span>")
            except Exception:
                pass
        # Vzorka 4 slotov pre porovnanie
        _samples_html = ""
        if _sk_pred or _mean_pred:
            _samples = []
            for _h in (8, 12, 14, 20):
                _idx = _h * 4
                _sv = _sk_pred.get(_idx)
                _mv = _mean_pred.get(_idx)
                _sk_str = f"{_sv:.0f}" if _sv is not None else "—"
                _mn_str = f"{_mv:.0f}" if _mv is not None else "—"
                _samples.append(f"{_h:02d}h: SK={_sk_str} / mean={_mn_str}")
            _samples_html = (f"<span style='color:#666;font-size:11px;margin-left:8px'>"
                             f"{' · '.join(_samples)} €/MWh</span>")
        body += (
            f"<div style='background:#fff;border-left:4px solid {_used_color};"
            f"padding:10px 14px;border-radius:6px;margin:8px 0;font-size:13px'>"
            f"{_used_icon} <b>ZCO predikcia:</b> "
            f"<span style='color:{_used_color};font-weight:700'>{_used_method}</span> "
            f"{_prof_meta}<br>{_samples_html}"
            f"</div>"
        )
    except Exception:
        pass

    # Spusti advisor
    try:
        res = _adv.get_live_recommendation(
            batt_kw=batt_kw, batt_kwh=batt_kwh,
            eff_c=eff_c, eff_d=eff_d,
            grid_fee=grid_fee, cycle_cost=cycle_cost,
            min_spread=min_spread,
            soc_min_pct=soc_min_pct, soc_max_pct=soc_max_pct,
            soc_start_pct=soc_arg,
            soc_end_min_pct=soc_end_min_pct,
            max_cycles_per_day=max_cycles if max_cycles > 0 else None,
            use_orderbook=True,
            profile=active_profile,
        )
    except Exception as e:
        body += f"<p style='color:#C0392B'>Advisor zlyhal: {_html.escape(str(e))}</p></div>"
        return render_legacy_body(None, "VDT Live Advisor", body)

    # Bug O3: Banner pre INSUFFICIENT DATA — VDT nemôže obchodovať bez kompletného kontextu
    if not res.get("ok") and res.get("data_completeness") is False:
        _missing = res.get("missing_items", []) or []
        _warnings = res.get("warnings", []) or []
        _state = res.get("state") or {}
        _missing_human = {
            "dam_today": "D-1 plán pre dnes (treba vygenerovať cez /plan alebo /dentrh)",
            "state_module_error": "vdt_state modul (interná chyba — pozri logy)",
            "state_not_computed": "vdt_state nevypočítané (interná chyba)",
        }
        _missing_html = "<ul style='margin:8px 0 4px 24px;padding:0'>" + "".join(
            f"<li><b>{_html.escape(m)}</b>{(' — ' + _missing_human[m]) if m in _missing_human else ''}</li>"
            for m in _missing) + "</ul>"
        _warn_html = ""
        if _warnings:
            _warn_html = ("<div style='margin-top:8px;padding:8px;background:#fff3cd;"
                           "border-radius:4px;font-size:12px;color:#856404'>"
                           "<b>Upozornenia:</b><ul style='margin:4px 0 0 20px;padding:0'>"
                           + "".join(f"<li>{_html.escape(w)}</li>" for w in _warnings)
                           + "</ul></div>")
        body += (
            f"<div style='background:#fee;border:2px solid #C0392B;border-radius:10px;"
            f"padding:18px;margin:12px 0;color:#7d1010'>"
            f"<h2 style='margin:0 0 8px;color:#C0392B'>❌ VDT NEMÔŽE OBCHODOVAŤ</h2>"
            f"<p style='margin:0 0 8px;font-size:14px'>Pre validný VDT trade potrebujem "
            f"<b>kompletný kontext</b> — aktuálny SOC, DAM nomináciu z D-1 plánu a všetky "
            f"realizované VDT trades. Niečo z toho chýba:</p>"
            f"{_missing_html}"
            f"<p style='margin:8px 0 0;font-size:13px;color:#444'>"
            f"<b>Profil:</b> {_html.escape(active_profile)} · "
            f"<b>Start SOC (odhad):</b> {_state.get('start_soc_pct', '—'):.1f}% "
            f"({_html.escape(str(_state.get('start_soc_source', '?')))})</p>"
            f"{_warn_html}"
            f"<p style='margin:14px 0 0;font-size:12px;color:#888;font-style:italic'>"
            f"VDT trades sa nevykonali. Auto_control scheduler tento profil preskočí. "
            f"Vyrieš chýbajúce dáta a obnov stránku.</p>"
            f"</div>"
        )
        # Pokračuj na cache fallback (zobrazí poslednú úspešnú rec ak existuje)
        _cached = None
        try:
            _cached = _adv.load_cache(profile=active_profile)
        except Exception:
            _cached = None
        if _cached and _cached.get("full_plan"):
            body += (f"<div style='background:#fff3cd;border-left:4px solid #FFA000;"
                       f"padding:10px 14px;border-radius:6px;margin:8px 0;font-size:12px'>"
                       f"📋 Zobrazujem dáta z <b>poslednej úspešnej cache</b> "
                       f"(ts: {_html.escape(str(_cached.get('ts','?'))[:19])}). "
                       f"<b>POZOR:</b> tieto čísla sú stale a NEDÔVERYHODNÉ pre aktuálne rozhodnutia.</div>")
            res = _cached
        else:
            body += "</div></body></html>"
            return render_legacy_body(None, "VDT Live Advisor", body)

    if not res.get("ok"):
        soc = res.get("soc", {})
        _err_msg = str(res.get("error", "?"))
        # Diagnostika pri Infeasible — najčastejšia príčina je SOC mimo limitov +
        # DAM commitment ktorý núti charge/discharge.
        _diag_hint = ""
        try:
            _soc_now = float(soc.get("soc_pct") or 0)
            if "infeasible" in _err_msg.lower() or "HiGHS Status 8" in _err_msg:
                _hints = []
                if _soc_now >= 99.5:
                    _hints.append("SOC je 100% — ak D-1 plán núti nabíjať v tomto slote, "
                                   "nie je kam pridať (relax: zníž soc_max_pct alebo "
                                   "vyšší max_cycles)")
                if _soc_now <= 0.5:
                    _hints.append("SOC je 0% — ak D-1 plán núti vybíjať, batéria je prázdna")
                if not _hints:
                    _hints.append("LP nemá riešenie pri zadaných parametroch — skús "
                                   "zvýšiť max_cycles alebo znížiť soc_end_min_pct")
                _diag_hint = (f"<br><span style='font-size:12px;color:#7a4a00'>"
                               f"💡 <b>Diagnostika:</b> " + " · ".join(_hints) + "</span>")
        except Exception:
            pass
        body += (
            f"<div style='background:#ffe7e7;border-left:4px solid #C0392B;padding:14px;"
            f"border-radius:6px;margin:10px 0'>"
            f"<b>⚠ Advisor zlyhal:</b> {_html.escape(_err_msg)}<br>"
            f"<span style='font-size:12px;color:#666'>SOC zdroj: "
            f"{_html.escape(str(soc.get('source', '?')))} · "
            f"SOC: {soc.get('soc_pct') or '—'}</span>"
            f"{_diag_hint}</div>"
        )
        # Fallback: nahraj poslednú validnú cache aby Settlement card + tabuľka
        # zostali viditeľné aj keď aktuálny LP zlyhal.
        _cached = None
        try:
            _cached = _adv.load_cache(profile=active_profile)
        except Exception:
            _cached = None
        if _cached and _cached.get("full_plan"):
            body += (f"<div style='background:#fff3cd;border-left:4px solid #FFA000;"
                     f"padding:10px 14px;border-radius:6px;margin:8px 0;font-size:12px'>"
                     f"📋 Zobrazujem dáta z <b>poslednej úspešnej cache</b> "
                     f"(ts: {_html.escape(str(_cached.get('ts','?'))[:19])}). "
                     f"Pre čerstvé prepočítanie oprav príčinu chyby vyššie.</div>")
            res = _cached   # použiť cache miesto failed res
        else:
            # Žiadna cache — ukáž aspoň Settlement card (číta zo plan_store +
            # paper_trades, funguje bez advisor outputu)
            try:
                import daily_settlement as _dsett
                _settle = _dsett.compute_daily_settlement(active_profile,
                                                           dt.date.today().isoformat())
                body += _render_settlement_card(_settle)
            except Exception:
                pass
            body += "</div></body></html>"
            return render_legacy_body(None, "VDT Live Advisor", body)

    # Bug O3: State diagnostic card — kompletný kontext pred VDT rozhodnutím
    _st = res.get("state") or {}
    if _st.get("data_completeness"):
        _dam_kind = _st.get("dam_kind", "?")
        _start_soc = _st.get("start_soc_pct", 0.0)
        _start_src = _st.get("start_soc_source", "?")
        _cur_soc = _st.get("current_soc_pct", 0.0)
        _vdt_n = _st.get("vdt_realized_count", 0)
        _vdt_eur = _st.get("vdt_realized_eur", 0.0)
        _slot_idx = _st.get("current_slot_idx", 0)
        _slot_h, _slot_m = _slot_idx * 15 // 60, (_slot_idx * 15) % 60
        body += (
            f"<div style='background:#e8f5e9;border-left:4px solid #2E7D32;border-radius:6px;"
            f"padding:10px 14px;margin:10px 0;font-size:12px;color:#1B5E20'>"
            f"<b>✓ Kompletný kontext (single source of truth)</b><br>"
            f"<b>Start SOC</b> {_start_soc:.1f}% <span style='color:#666'>"
            f"({_html.escape(_start_src)})</span> · "
            f"<b>SOC teraz</b> (slot {_slot_h:02d}:{_slot_m:02d}) "
            f"<span style='font-size:14px;font-weight:700'>{_cur_soc:.1f}%</span> · "
            f"<b>DAM</b> kind={_dam_kind} · "
            f"<b>VDT realized</b> {_vdt_n} trades ({_vdt_eur:+.2f} €) "
            f"= kumulatívna integrácia od 00:00 dnes."
            f"</div>"
        )

    # Hlavná karta — ČO TERAZ
    cur = res["current"]
    soc = res["soc"]
    action = cur["action"]
    if action == "charge":
        action_bg = "#1F4E78"
        action_label = "💚 NABÍJAŤ"
        action_text_color = "#fff"
    elif action == "discharge":
        action_bg = "#C62828"
        action_label = "🔴 VYBÍJAŤ"
        action_text_color = "#fff"
    else:
        action_bg = "#888"
        action_label = "⏸ IDLE (nečakať)"
        action_text_color = "#fff"

    price_str = (f"{cur['price_eur_mwh']:.2f} €/MWh"
                 if cur.get("price_eur_mwh") is not None else "—")
    body += (
        f"<div style='background:{action_bg};color:{action_text_color};"
        f"padding:24px 28px;border-radius:12px;margin:14px 0;"
        f"box-shadow:0 4px 14px rgba(0,0,0,0.15)'>"
        f"<div style='font-size:13px;opacity:0.9'>ČO TERAZ — slot <b>{_html.escape(cur['slot'])}</b></div>"
        f"<div style='font-size:42px;font-weight:800;margin:6px 0'>{action_label}</div>"
        f"<div style='font-size:16px;margin:8px 0'>"
        f"<b>{cur['kw']:.0f} kW</b> ({cur['kwh_per_slot']:.0f} kWh za 15 min) "
        f"@ <b>{price_str}</b></div>"
        f"<div style='font-size:13px;opacity:0.9;line-height:1.5;margin-top:8px'>"
        f"{_html.escape(cur['reason'])}</div>"
        f"<div style='font-size:12px;opacity:0.8;margin-top:10px;padding-top:10px;"
        f"border-top:1px solid rgba(255,255,255,0.3)'>"
        f"SOC teraz: <b>{soc['soc_pct']:.1f} %</b> ({_html.escape(soc.get('source','?'))}) → "
        f"po slote: <b>{cur['soc_after_pct']:.1f} %</b> ({cur['soc_after_kwh']:.0f} kWh)"
        f"</div></div>"
    )

    # Sumár karty
    s = res["summary"]
    profit = res.get("profit_eur", 0.0)
    pcolor = "#28a745" if profit > 0 else "#C0392B"
    body += (
        f"<div style='display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:10px 0'>"
        f"<div style='background:#fff;border-left:4px solid {pcolor};padding:12px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Profit zvyšok dňa</div>"
        f"<div style='font-size:22px;font-weight:700;color:{pcolor}'>{profit:+.2f} €</div>"
        f"</div>"
        f"<div style='background:#fff;border-left:4px solid #1F4E78;padding:12px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Cykly</div>"
        f"<div style='font-size:22px;font-weight:700;color:#1F4E78'>{s['cycles']:.2f}</div>"
        f"</div>"
        f"<div style='background:#fff;border-left:4px solid #C49000;padding:12px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Akcie</div>"
        f"<div style='font-size:22px;font-weight:700;color:#C49000'>"
        f"{s['n_charge_slots']}+{s['n_discharge_slots']}</div>"
        f"<div style='font-size:11px;color:#666'>nabíj + vybíj (z {res['n_slots']})</div>"
        f"</div>"
        f"<div style='background:#fff;border-left:4px solid #28a745;padding:12px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Orderbook</div>"
        f"<div style='font-size:14px;font-weight:600;color:#28a745;margin-top:6px'>"
        f"{_html.escape(res.get('orderbook_status','?'))}</div>"
        f"<div style='font-size:11px;color:#666'>refresh: {_html.escape(res.get('ts','?')[11:19])}</div>"
        f"</div>"
        f"</div>"
    )

    # DAM commitment banner (ak je D-1 plán napojený)
    dam_status = res.get("dam_status", "")
    if "✓" in dam_status:
        dam_ex = res.get("dam_committed_export_kwh", 0)
        dam_im = res.get("dam_committed_import_kwh", 0)
        vdt_ex_dis = res.get("vdt_extra_discharge_kwh", 0)
        vdt_ex_chg = res.get("vdt_extra_charge_kwh", 0)
        body += (
            f"<div style='background:#fff;border-left:4px solid #5E35B1;padding:12px 16px;"
            f"border-radius:8px;margin:10px 0;font-size:13px'>"
            f"<b>📅 DAM commitment napojený</b> · "
            f"export {dam_ex:.0f} kWh · import {dam_im:.0f} kWh "
            f"<span style='color:#666'>(z D-1 plánu)</span> · "
            f"<b>VDT extra:</b> +{vdt_ex_dis:.0f} discharge · +{vdt_ex_chg:.0f} charge "
            f"<span style='color:#666;font-style:italic'>(LP rieši iba nadrámcové obchody)</span>"
            f"</div>"
        )
    elif "(žiadny" in dam_status:
        body += (
            f"<div style='background:#fff;border-left:4px solid #999;padding:8px 14px;"
            f"border-radius:6px;margin:8px 0;font-size:12px;color:#666'>"
            f"💡 Žiadny D-1 plán pre dnes/zajtra v plan_store. "
            f"Pre lepšiu integráciu spusti <a href='/vdt/d1' style='color:#1F4E78'>D-1 plán</a> najprv."
            f"</div>"
        )

    # Celodenná tabuľka — všetkých 96 slotov (predtým preview = next 16)
    dam_commits_full = res.get("dam_commits", []) or []
    full_plan_all = res.get("full_plan", [])
    slot_to_dam = {}
    for i, fp in enumerate(full_plan_all):
        if i < len(dam_commits_full):
            slot_to_dam[fp["slot"]] = float(dam_commits_full[i] or 0)
    # Aktuálny slot label pre highlight + auto-scroll
    try:
        _now_h = dt.datetime.now().hour
        _now_m = (dt.datetime.now().minute // 15) * 15
        _cur_slot_label = f"{_now_h:02d}:{_now_m:02d}"
    except Exception:
        _cur_slot_label = ""
    if full_plan_all:
        body += ("<h2 style='color:#1F4E78;margin-top:16px'>"
                  "🔮 Celodenný plán "
                  "<span style='font-size:13px;color:#666;font-weight:400'>"
                  "(scrollable, aktuálny slot zvýraznený)</span>"
                  "</h2>")
        # Wrapper s max-height + overflow + sticky header
        body += (
            "<div id='vdtPlanWrap' style='max-height:430px;overflow-y:auto;"
            "border:1px solid #ddd;border-radius:6px'>"
            "<table style='width:100%;border-collapse:collapse;font-size:12px'>"
            "<thead><tr style='background:#1F4E78;color:#fff;"
            "position:sticky;top:0;z-index:5'>"
            "<th style='padding:6px 8px;text-align:left'>slot</th>"
            "<th style='padding:6px 8px;text-align:left'>akcia (VDT total)</th>"
            "<th style='padding:6px 8px;text-align:right'>kW</th>"
            "<th style='padding:6px 8px;text-align:right'>kWh / 15min</th>"
            "<th style='padding:6px 8px;text-align:right;background:#E65100'>DAM kWh</th>"
            "<th style='padding:6px 8px;text-align:right'>VDT extra kWh</th>"
            "<th style='padding:6px 8px;text-align:right;background:#2E7D32'>realizované kWh</th>"
            "<th style='padding:6px 8px;text-align:right'>cena €/MWh</th>"
            "<th style='padding:6px 8px;text-align:right'>SOC po</th>"
            "</tr></thead><tbody>"
        )
        # Bug VDT-EFEKTIVITA (2026-06-11): REALIZOVANÉ paper trades per slot —
        # tabuľka je NÁVRH LP; bez tohto stĺpca nesedela s livesim (ten ukazuje
        # len realizované obchody, návrh mohol byť audit gate-om odmietnutý).
        _realized96 = [0.0] * 96
        try:
            import vdt_state as _vs_re
            from core.profile_resolver import get_active as _ga_re
            _prof_re = _ga_re() or ""
            if _prof_re:
                _st_re = _vs_re._load_vdt_realized(_prof_re, dt.date.today().isoformat())
                _realized96 = (_st_re or {}).get("kwh_batt_view") or [0.0] * 96
        except Exception as _e_re:
            print(f"[vdt realized col] {_e_re}")
        # Bug VDT-EFEKTIVITA: miera realizácie návrhu (Σ zrealizované / Σ návrh)
        _sum_prop_kwh = 0.0
        _sum_real_match_kwh = 0.0
        for p in full_plan_all:
            a = p.get("action", "")
            if a == "charge":
                act_html = "<span style='background:#1F4E78;color:#fff;padding:2px 8px;border-radius:10px;font-size:11px'>💚 NABÍJAŤ</span>"
                row_bg = "#e7f3ff"
            elif a == "discharge":
                act_html = "<span style='background:#C62828;color:#fff;padding:2px 8px;border-radius:10px;font-size:11px'>🔴 VYBÍJAŤ</span>"
                row_bg = "#ffe9e9"
            else:
                act_html = "<span style='color:#999'>idle</span>"
                row_bg = "#fafafa"
            # `full_plan` schema: kwh, buy_price, sell_price (žiadny kw ani price)
            # `preview` schema:    kw, kwh, price
            # Unify: kW = kwh × 4 (15-min slot), cena = sell_price/buy_price/price
            _kwh_val = float(p.get("kwh", 0) or 0)
            _kw_val = float(p.get("kw", _kwh_val * 4.0) or 0)
            price = (p.get("price")
                      if p.get("price") is not None
                      else (p.get("sell_price") if a == "discharge"
                              else p.get("buy_price")))
            price_s = f"{price:.2f}" if price is not None else "—"
            # DAM kWh pre slot (+ = export, − = import)
            dam_kwh = slot_to_dam.get(p["slot"], 0.0)
            if abs(dam_kwh) < 0.05:
                dam_html = "<span style='color:#aaa'>—</span>"
            elif dam_kwh > 0:
                dam_html = f"<span style='color:#BF360C;font-weight:600'>+{dam_kwh:.1f}</span>"   # export — tmavo oranžová
            else:
                dam_html = f"<span style='color:#E65100;font-weight:600'>{dam_kwh:.1f}</span>"     # import — oranžová
            # VDT extra (rozdiel medzi total signed kWh a DAM)
            batt_signed = (_kwh_val if a == "charge"
                            else (-_kwh_val if a == "discharge" else 0))
            dam_batt_signed = -dam_kwh   # DAM export(+) = batt discharge(−); DAM import(−) = batt charge(+)
            extra = batt_signed - dam_batt_signed
            if abs(extra) < 0.05:
                extra_html = "<span style='color:#aaa'>—</span>"
            elif extra > 0:
                extra_html = f"<span style='color:#1F4E78;font-weight:600'>+{extra:.1f}</span>"
            else:
                extra_html = f"<span style='color:#C62828;font-weight:600'>{extra:.1f}</span>"
            # Bug VDT-EFEKTIVITA: realizované obchody pre slot (kwh_batt_view:
            # + discharge / − charge → otoč na konvenciu tabuľky charge=+)
            try:
                _sl_re = str(p.get("slot", ""))
                _ridx = (int(_sl_re[:2]) * 60 + int(_sl_re[3:5])) // 15
                _re_signed = (-float(_realized96[_ridx])
                              if 0 <= _ridx < 96 else 0.0)
            except Exception:
                _re_signed = 0.0
            if abs(_re_signed) < 0.05 and abs(extra) < 0.05:
                re_html = "<span style='color:#aaa'>—</span>"
            elif abs(_re_signed) < 0.05:
                re_html = ("<span style='color:#999' title='návrh nebol zrealizovaný "
                           "(neexekuované / audit gate odmietol)'>✗ 0</span>")
            else:
                _re_col = "#1F4E78" if _re_signed > 0 else "#C62828"
                _re_match = ("✓" if abs(_re_signed - extra) < max(5.0, abs(extra) * 0.05)
                             else "⬇")
                re_html = (f"<span style='color:{_re_col};font-weight:600' "
                           f"title='realizovaný paper trade'>{_re_match} {_re_signed:+.1f}</span>")
            _sum_prop_kwh += abs(extra)
            if extra * _re_signed > 0:                       # rovnaký smer
                _sum_real_match_kwh += min(abs(_re_signed), abs(extra))
            # Highlight aktuálny slot (slot label začína "HH:MM-..." → porovnaj prefix)
            _is_now = bool(_cur_slot_label and str(p["slot"]).startswith(_cur_slot_label))
            _row_extra = ""
            _anchor = ""
            if _is_now:
                _row_extra = ("font-weight:600;outline:2px solid #FFC107;"
                               "outline-offset:-2px")
                row_bg = "#fff3cd"
                _anchor = " id='vdtPlanNow'"
            _soc_after = float(p.get("soc_after_pct", 0) or 0)
            body += (
                f"<tr{_anchor} style='background:{row_bg};{_row_extra}'>"
                f"<td style='padding:3px 8px;font-family:monospace'>"
                f"{_html.escape(str(p.get('slot','')))}"
                f"{' ← teraz' if _is_now else ''}</td>"
                f"<td style='padding:3px 8px'>{act_html}</td>"
                f"<td style='padding:3px 8px;text-align:right;font-weight:600'>{_kw_val:.0f}</td>"
                f"<td style='padding:3px 8px;text-align:right'>{_kwh_val:.1f}</td>"
                f"<td style='padding:3px 8px;text-align:right'>{dam_html}</td>"
                f"<td style='padding:3px 8px;text-align:right'>{extra_html}</td>"
                f"<td style='padding:3px 8px;text-align:right'>{re_html}</td>"
                f"<td style='padding:3px 8px;text-align:right'>{price_s}</td>"
                f"<td style='padding:3px 8px;text-align:right;color:#C49000;font-weight:600'>{_soc_after:.1f}%</td>"
                f"</tr>"
            )
        body += "</tbody></table></div>"
        # Auto-scroll na aktuálny slot pri loade (s malým offsetom vyšiel highlight)
        body += (
            "<script>"
            "(function(){var w=document.getElementById('vdtPlanWrap');"
            "var t=document.getElementById('vdtPlanNow');"
            "if(w && t){var off=t.offsetTop - w.clientHeight/3;"
            "if(off>0) w.scrollTop=off;}})();"
            "</script>"
        )
        # Bug VDT-EFEKTIVITA: súhrn realizácie návrhu nad tabuľkou legiend
        _real_pct = (100.0 * _sum_real_match_kwh / _sum_prop_kwh) if _sum_prop_kwh > 0.5 else None
        if _real_pct is not None:
            _rp_col = "#2E7D32" if _real_pct >= 80 else ("#B45309" if _real_pct >= 40 else "#C62828")
            body += (
                f"<div style='background:#fff;border-left:4px solid {_rp_col};padding:8px 14px;"
                f"border-radius:6px;margin:8px 0;font-size:13px'>"
                f"📊 <b>Realizácia VDT návrhu dnes:</b> "
                f"<b style='color:{_rp_col}'>{_real_pct:.0f} %</b> objemu "
                f"({_sum_real_match_kwh:.0f} z {_sum_prop_kwh:.0f} kWh zrealizovaných v smere návrhu). "
                f"<span style='color:#666'>Nízke % = exekúcia neprešla (audit kapacita/SOC) "
                f"alebo job ešte nebežal.</span></div>")
        body += (
            f"<p style='color:#666;font-size:11px;margin-top:6px'>"
            f"<b style='color:#E65100'>DAM kWh</b>: koľko je tento slot už predaný/kúpený v D-1 (+ export, − import). "
            f"<b style='color:#0D47A1'>VDT extra</b>: čo robí VDT navyše oproti DAM "
            f"(+ extra nabíjanie, − extra vybíjanie z batt pohľadu). VDT total kWh = DAM + VDT extra. "
            f"<b style='color:#2E7D32'>realizované kWh</b>: uzavreté paper trades (✓ plne · ⬇ čiastočne · ✗ nič).</p>"
        )

    # ── Denný settlement card (Fáza SIM-C) ─────────────────────────────
    try:
        import daily_settlement as _dsett
        _settle_prof = res.get("profile") or active_profile
        _settle = _dsett.compute_daily_settlement(_settle_prof,
                                                    dt.date.today().isoformat())
        body += _render_settlement_card(_settle)
    except Exception as _e_sett:
        body += (f"<p style='color:#aaa;font-size:11px;margin:8px 0'>"
                 f"⚠ Denný settlement nedostupný: {_html.escape(str(_e_sett))}</p>")

    # ── ZCO príležitosti (Fáza C1) ───────────────────────────────────────
    zco_info = res.get("zco", {}) or {}
    zco_opps = zco_info.get("opportunities", []) or []
    if zco_info.get("ok") or zco_opps:
        body += "<h2 style='color:#1F4E78;margin-top:18px'>💡 ZCO príležitosti — nesplniť DAM</h2>"
        body += (
            f"<p style='color:#666;font-size:12px;margin:4px 0 8px'>"
            f"Sloty kde je predikovaná ZCO výhodnejšia ako VDT — t.j. <b>oplatilo by sa "
            f"zámerne nesplniť DAM nomináciu</b> a zaplatiť ZCO penalty namiesto VDT obchodu. "
            f"{_html.escape(str(zco_info.get('zco_pred_status', '')))}</p>"
        )
        if not zco_opps:
            body += (
                f"<div style='background:#fff;border-left:4px solid #aaa;padding:10px 14px;"
                f"border-radius:6px;color:#666;font-size:13px'>"
                f"Pre žiadny slot s DAM commitmentom nie je ZCO výhodnejší ako VDT. "
                f"({zco_info.get('n_checked', 0)} slotov skontrolovaných, "
                f"{zco_info.get('n_profitable', 0)} ziskových.)"
                f"</div>"
            )
        else:
            total = float(zco_info.get("total_profit_eur", 0))
            body += (
                f"<div style='background:#fff3e0;border-left:4px solid #E65100;"
                f"padding:10px 14px;border-radius:6px;margin-bottom:8px'>"
                f"<b>Top {len(zco_opps)} ziskových slotov: +{total:.2f} €</b> "
                f"<span style='color:#666;font-size:12px'>"
                f"(zo {zco_info.get('n_profitable', 0)} celkovo, "
                f"{zco_info.get('n_checked', 0)} skontrolovaných)</span></div>"
            )
            # Wrap do scrollable kontajnera ak je veľa záznamov (>10)
            _zco_scroll = len(zco_opps) > 10
            _zco_wrap_open = ("<div style='max-height:380px;overflow-y:auto;"
                              "border:1px solid #ddd;border-radius:6px'>"
                              if _zco_scroll else "")
            body += _zco_wrap_open + (
                "<table style='width:100%;border-collapse:collapse;font-size:12px"
                + ("" if _zco_scroll else ";border:1px solid #ddd") + "'>"
                "<thead><tr style='background:#E65100;color:#fff"
                + (";position:sticky;top:0;z-index:5" if _zco_scroll else "")
                + "'>"
                "<th style='padding:6px 8px;text-align:left'>slot</th>"
                "<th style='padding:6px 8px;text-align:center'>strana</th>"
                "<th style='padding:6px 8px;text-align:right'>DAM kWh</th>"
                "<th style='padding:6px 8px;text-align:right'>DAM €/MWh</th>"
                "<th style='padding:6px 8px;text-align:right'>ZCO pred. €</th>"
                "<th style='padding:6px 8px;text-align:right'>VDT bid €</th>"
                "<th style='padding:6px 8px;text-align:right'>VDT ask €</th>"
                "<th style='padding:6px 8px;text-align:right'>Profit €</th>"
                "</tr></thead><tbody>"
            )
            for o in zco_opps:
                side = o.get("side", "")
                side_html = ("<span style='background:#C62828;color:#fff;padding:2px 8px;"
                              "border-radius:10px;font-size:11px'>EXPORT</span>"
                              if side == "export"
                              else "<span style='background:#1F4E78;color:#fff;padding:2px 8px;"
                                    "border-radius:10px;font-size:11px'>IMPORT</span>")
                bid = o.get("vdt_bid")
                ask = o.get("vdt_ask")
                body += (
                    f"<tr style='background:#fff'>"
                    f"<td style='padding:3px 8px;font-family:monospace'>{_html.escape(str(o.get('slot','')))}</td>"
                    f"<td style='padding:3px 8px;text-align:center'>{side_html}</td>"
                    f"<td style='padding:3px 8px;text-align:right'>{o.get('dam_kwh',0):+.1f}</td>"
                    f"<td style='padding:3px 8px;text-align:right'>{o.get('dam_price',0):.1f}</td>"
                    f"<td style='padding:3px 8px;text-align:right;color:#E65100;font-weight:600'>"
                    f"{o.get('zco_pred',0):.1f}</td>"
                    f"<td style='padding:3px 8px;text-align:right'>"
                    f"{('—' if bid is None else f'{bid:.1f}')}</td>"
                    f"<td style='padding:3px 8px;text-align:right'>"
                    f"{('—' if ask is None else f'{ask:.1f}')}</td>"
                    f"<td style='padding:3px 8px;text-align:right;color:#2E7D32;font-weight:700'>"
                    f"+{o.get('profit_eur',0):.2f}</td>"
                    f"</tr>"
                )
            body += "</tbody></table>"
            if _zco_scroll:
                body += "</div>"
            body += (
                f"<p style='color:#888;font-size:11px;margin-top:6px'>"
                f"<i>Logika: pre EXPORT slot je profit = "
                f"(VDT_ask + fee − ZCO) × kWh ÷ 1000. Pre IMPORT slot je "
                f"profit = (ZCO − VDT_bid − fee) × kWh ÷ 1000. ZCO predikcia = "
                f"priemer ZCO cien za posledných 7 dní v rovnakom slote. "
                f"Toto je <b>signál</b>, nie auto-execute — finálne rozhodnutie je na operátorovi.</i></p>"
            )

    # Graf — full plan cez deň (Chart.js)
    full_plan = res.get("full_plan", [])
    dam_commits_arr = res.get("dam_commits", []) or []
    if full_plan:
        labels = [p["slot"][:5] for p in full_plan]
        buy_prices = [p.get("buy_price") for p in full_plan]
        sell_prices = [p.get("sell_price") for p in full_plan]
        soc_arr = [p["soc_after_pct"] for p in full_plan]
        # VDT total akcie (zahŕňa aj DAM + extra)
        total_charges = [p["kwh"] if p["action"] == "charge" else 0 for p in full_plan]
        total_discharges = [-p["kwh"] if p["action"] == "discharge" else 0 for p in full_plan]

        # DAM commits (export kladné, import záporné) — namatchujeme na slots
        n_plan = len(full_plan)
        dam_export = [0.0] * n_plan
        dam_import = [0.0] * n_plan
        for i in range(min(n_plan, len(dam_commits_arr))):
            v = float(dam_commits_arr[i] or 0)
            if v > 0:
                dam_export[i] = -v
            elif v < 0:
                dam_import[i] = -v

        # VDT extra (rozdiel medzi total a DAM)
        vdt_extra_charge = [max(0, total_charges[i] - dam_import[i]) for i in range(n_plan)]
        vdt_extra_discharge = [min(0, total_discharges[i] - dam_export[i]) for i in range(n_plan)]

        # ── Cenové rady pre druhý graf: DT clearing, VDT clearing, ZCO predikcia ──
        # Pre každý label "HH:MM" zostavíme slot_idx (predpoklad: dnes, pre nálsedujúci
        # deň fallback na priemer/None). Pre presnosť by sa muselo pamätať `date` v full_plan.
        dt_arr = [None] * n_plan
        vdt_arr = [None] * n_plan
        zco_pred_arr = [None] * n_plan
        try:
            import seps_sk as _sps
            import zco_advisor as _zaa
            import datetime as _dt
            _today_iso = _dt.date.today().isoformat()
            _tomorrow_iso = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
            # ZCO predikcia per slot_idx 0..95 (rovnaká pre dnes aj zajtra — vzorový profil)
            _zco_today = _zaa.predict_zco_for_slots(_today_iso, method="auto")
            _zco_tom = _zaa.predict_zco_for_slots(_tomorrow_iso, method="auto") if _today_iso != _tomorrow_iso else {}
            # DT (DAM clearing) — dict ts → eur (today aj tomorrow)
            _dt_today = _sps.load_okte_dt_for_day(_today_iso) or {}
            _dt_tom = _sps.load_okte_dt_for_day(_tomorrow_iso) or {}
            # VDT predbežné/final per slot
            _vdt_today = _sps.load_okte_vdt_preliminary_for_day(_today_iso) or _sps.load_okte_vdt_for_day(_today_iso) or {}
            _vdt_tom = _sps.load_okte_vdt_preliminary_for_day(_tomorrow_iso) or _sps.load_okte_vdt_for_day(_tomorrow_iso) or {}
            # Konvertuj dicts (ts_str → val) na slot_idx → val
            def _to_slot_map(d):
                out = {}
                for ts_str, v in d.items():
                    try:
                        hh = int(str(ts_str)[11:13]); mm = int(str(ts_str)[14:16])
                        out[(hh*60+mm)//15] = float(v)
                    except Exception:
                        continue
                return out
            _dt_today_m = _to_slot_map(_dt_today); _dt_tom_m = _to_slot_map(_dt_tom)
            _vdt_today_m = _to_slot_map(_vdt_today); _vdt_tom_m = _to_slot_map(_vdt_tom)
            # Rolling MPC snapshot začína od aktuálneho slotu — predpokladáme dnes po slot 95
            # potom zajtra. Detekcia preklopenia: keď HH klesne (napr. 23:45 → 00:00) → next day.
            _last_h = -1
            _is_tomorrow = False
            for i, lab in enumerate(labels):
                try:
                    hh = int(lab[:2]); mm = int(lab[3:5])
                except Exception:
                    continue
                if hh < _last_h:
                    _is_tomorrow = True
                _last_h = hh
                slot_idx = (hh*60+mm)//15
                if _is_tomorrow:
                    dt_arr[i] = _dt_tom_m.get(slot_idx)
                    vdt_arr[i] = _vdt_tom_m.get(slot_idx)
                    zco_pred_arr[i] = _zco_tom.get(slot_idx)
                else:
                    dt_arr[i] = _dt_today_m.get(slot_idx)
                    vdt_arr[i] = _vdt_today_m.get(slot_idx)
                    zco_pred_arr[i] = _zco_today.get(slot_idx)
        except Exception as _e:
            pass

        body += (
            f"<h2 style='color:#1F4E78;margin-top:16px'>📊 Plán cez deň — DAM + VDT</h2>"
            f"<p style='color:#666;font-size:12px;margin:4px 0 8px'>"
            f"<b style='color:#E65100'>Oranžové bary</b> = DAM commitment (z D-1 plánu). "
            f"<b style='color:#0D47A1'>Modré</b> = VDT extra nabíjanie. "
            f"<b style='color:#B71C1C'>Červené</b> = VDT extra vybíjanie. Stacked bars → total batt akcia.</p>"
            f"<div style='background:#fff;padding:12px;border-radius:8px;border:1px solid #ddd'>"
            f"<canvas id='chPlanLive' height='90'></canvas>"
            f"<canvas id='chPricesLive' height='80' style='margin-top:12px'></canvas>"
            f"<canvas id='chSocLive' height='60' style='margin-top:12px'></canvas>"
            f"</div>"
            f"<script src='https://cdn.jsdelivr.net/npm/chart.js'></script>"
            f"<script>"
            f"const lbls = {_json.dumps(labels)};"
            # Graf 1: výkony (bez Ask/Bid línií)
            f"new Chart(document.getElementById('chPlanLive'), {{"
            f" data:{{labels:lbls, datasets:["
            f"  {{type:'bar', label:'DAM import (nabíj)', data:{_json.dumps(dam_import)}, "
            f"   backgroundColor:'rgba(255,167,38,0.9)', borderColor:'#E65100', borderWidth:1, "
            f"   stack:'batt'}},"
            f"  {{type:'bar', label:'VDT extra nabíj', data:{_json.dumps(vdt_extra_charge)}, "
            f"   backgroundColor:'rgba(13,71,161,0.9)', borderColor:'#0D47A1', borderWidth:1, "
            f"   stack:'batt'}},"
            f"  {{type:'bar', label:'DAM export (vybíj)', data:{_json.dumps(dam_export)}, "
            f"   backgroundColor:'rgba(230,81,0,0.9)', borderColor:'#BF360C', borderWidth:1, "
            f"   stack:'batt'}},"
            f"  {{type:'bar', label:'VDT extra vybíj', data:{_json.dumps(vdt_extra_discharge)}, "
            f"   backgroundColor:'rgba(183,28,28,0.9)', borderColor:'#7F0000', borderWidth:1, "
            f"   stack:'batt'}}"
            f" ]}}, options:{{responsive:true, plugins:{{title:{{display:true,"
            f"  text:'Akcie batérie cez deň (kWh)'}}}}, scales:{{"
            f"   x:{{stacked:true}},"
            f"   y:{{stacked:true, title:{{display:true,text:'kWh'}}}}}}}}}});"
            # Graf 2: ceny — DT, VDT, ZCO predikcia, Ask, Bid
            f"new Chart(document.getElementById('chPricesLive'), {{"
            f" type:'line', data:{{labels:lbls, datasets:["
            f"  {{label:'DT (DAM clearing)', data:{_json.dumps(dt_arr)}, "
            f"   borderColor:'#1F4E78', backgroundColor:'transparent', borderWidth:2.5, "
            f"   tension:0.1, pointRadius:0, spanGaps:true}},"
            f"  {{label:'VDT clearing', data:{_json.dumps(vdt_arr)}, "
            f"   borderColor:'#2E7D32', backgroundColor:'transparent', borderWidth:2, "
            f"   tension:0.1, pointRadius:0, spanGaps:true, borderDash:[4,2]}},"
            f"  {{label:'ZCO predikcia', data:{_json.dumps(zco_pred_arr)}, "
            f"   borderColor:'#E65100', backgroundColor:'rgba(230,81,0,0.08)', borderWidth:2.5, "
            f"   tension:0.2, pointRadius:0, spanGaps:true, fill:true}},"
            f"  {{label:'Ask (orderbook)', data:{_json.dumps(buy_prices)}, "
            f"   borderColor:'#C62828', backgroundColor:'rgba(198,40,40,0.85)', "
            f"   showLine:false, pointRadius:5, pointHoverRadius:7, "
            f"   pointStyle:'circle', pointBorderColor:'#7F0000', pointBorderWidth:1, "
            f"   spanGaps:true}},"
            f"  {{label:'Bid (orderbook)', data:{_json.dumps(sell_prices)}, "
            f"   borderColor:'#5E35B1', backgroundColor:'rgba(94,53,177,0.85)', "
            f"   showLine:false, pointRadius:5, pointHoverRadius:7, "
            f"   pointStyle:'triangle', pointBorderColor:'#311B92', pointBorderWidth:1, "
            f"   spanGaps:true}}"
            f" ]}}, options:{{responsive:true, plugins:{{title:{{display:true,"
            f"  text:'Ceny: DT clearing · VDT clearing · ZCO predikcia · orderbook Ask/Bid'}}}}, "
            f"scales:{{y:{{title:{{display:true,text:'€/MWh'}}}}}}}}}});"
            # Graf 3: SOC
            f"new Chart(document.getElementById('chSocLive'), {{"
            f" type:'line', data:{{labels:lbls, datasets:["
            f"  {{label:'SOC %', data:{_json.dumps(soc_arr)}, "
            f"   borderColor:'#C49000', backgroundColor:'rgba(196,144,0,0.2)', "
            f"   borderWidth:2, fill:true, tension:0.2}}"
            f" ]}}, options:{{responsive:true, plugins:{{title:{{display:true,"
            f"  text:'SOC profil cez deň'}}}}, scales:{{y:{{min:0,max:100}}}}}}}});"
            f"</script>"
        )

    # ── Sekcia: Extra obchody nad DAM ─────────────────────────────────────
    try:
        import vdt_extras as _vex
        import datetime as _dt_vex
        _ob_per_slot = res.get("orderbook_per_slot") or {}
        _dam_commits = res.get("dam_commits") or []
        _soc_pct_now = float(res.get("soc", {}).get("soc_pct") or 50.0)
        # Fallback: ak orderbook_per_slot je prázdny → skry sekciu + banner
        if not _ob_per_slot:
            body += (
                f"<div style='background:#fff3cd;border-left:4px solid #856404;"
                f"padding:12px 14px;border-radius:6px;margin:14px 0;font-size:13px'>"
                f"<b>💡 Extra obchody nad DAM:</b> "
                f"VDT orderbook nedostupný (OKTE neodpovedá alebo prázdny). "
                f"Sekcia sa zobrazí pri ďalšom refreshi keď príde čerstvý orderbook."
                f"</div>"
            )
        else:
            # DAM clearing per slot
            _dam_clr = _vex.load_dam_clearing_for_day(_dt_vex.date.today())
            # Skonštruuj mini DataFrame z orderbook_per_slot (vdt_extras očakáva DF)
            import pandas as _pd_vex
            _rows = []
            for _si, _rec in _ob_per_slot.items():
                _rows.append({
                    "slot_idx": int(_si),
                    "ob_best_bid_eur": _rec.get("bid_eur"),
                    "ob_best_bid_mw": _rec.get("bid_mw"),
                    "ob_best_ask_eur": _rec.get("ask_eur"),
                    "ob_best_ask_mw": _rec.get("ask_mw"),
                })
            _ob_df = _pd_vex.DataFrame(_rows)
            _profile_cfg = {
                "batt_kw": batt_kw, "batt_kwh": batt_kwh,
                "eff_c": eff_c, "eff_d": eff_d,
                "soc_min_pct": soc_min_pct, "soc_max_pct": soc_max_pct,
            }
            # Spusti obe metódy — pass profile + date_iso pre FTV curtail logiku
            _today_iso = _dt_vex.date.today().isoformat()
            try:
                _res_greedy = _vex.propose_greedy(
                    _ob_df, _dam_commits, _dam_clr,
                    _profile_cfg, _soc_pct_now,
                    threshold_eur_mwh=min_spread or 5.0,
                    grid_fee=grid_fee,
                    profile=active_profile, date_iso=_today_iso)
            except Exception as _e_g:
                _res_greedy = {"ok": False, "error": str(_e_g), "slots": [], "summary": {}}
            try:
                _res_lp = _vex.propose_lp(
                    _ob_df, _dam_commits, _dam_clr,
                    _profile_cfg, _soc_pct_now,
                    grid_fee=grid_fee, cycle_cost=cycle_cost,
                    profile=active_profile, date_iso=_today_iso)
            except Exception as _e_l:
                _res_lp = {"ok": False, "error": str(_e_l), "slots": [], "summary": {}}

            body += (
                f"<h2 style='color:#1F4E78;margin-top:24px;border-top:2px solid #1F4E78;"
                f"padding-top:14px'>💡 Extra obchody nad DAM</h2>"
                f"<p style='color:#666;font-size:12px;margin:0 0 12px'>"
                f"Návrhy obchodov ktoré sú výhodnejšie než plnenie DAM nominácie "
                f"cez vlastnú výrobu/odber. Threshold = min spread "
                f"({min_spread:.1f} €/MWh), grid fee {grid_fee:.0f} €/MWh. "
                f"<b>Read-only</b> — žiadne auto-execute do OKTE.</p>"
            )

            def _render_extras_table(res_extras, title, color):
                _slots = res_extras.get("slots") or []
                _summary = res_extras.get("summary") or {}
                _ok = res_extras.get("ok")
                _err = res_extras.get("error", "")
                _html_out = (
                    f"<div style='background:#fff;border:1px solid #ddd;border-top:4px solid {color};"
                    f"border-radius:6px;padding:10px'>"
                    f"<h3 style='margin:0 0 8px;color:{color};font-size:14px'>{title}</h3>"
                )
                if not _ok:
                    _html_out += (
                        f"<p style='color:#C0392B;font-size:12px;margin:0'>"
                        f"⚠ {_html.escape(str(_err))}</p></div>"
                    )
                    return _html_out
                _curtail_kwh = _summary.get('total_curtail_ftv_kwh', 0)
                _load_kwh_s = _summary.get('total_load_cover_kwh', 0)
                _curtail_str = (f" · ✂️ CURTAIL FTV <b>{_curtail_kwh:.1f}</b> kWh"
                                if _curtail_kwh > 0.1 else "")
                _load_str = (f" · 🔌 LOAD COVER <b>{_load_kwh_s:.1f}</b> kWh"
                             if _load_kwh_s > 0.1 else "")
                _html_out += (
                    f"<div style='font-size:12px;color:#666;margin-bottom:8px'>"
                    f"<b>{_summary.get('n_proposals',0)}</b> návrhov · "
                    f"BUY <b>{_summary.get('total_buy_kwh',0):.1f}</b> kWh · "
                    f"SELL <b>{_summary.get('total_sell_kwh',0):.1f}</b> kWh"
                    f"{_curtail_str}{_load_str} · "
                    f"Δ profit <b style='color:#28a745'>+{_summary.get('total_delta_profit_eur',0):.2f} €</b>"
                    f"</div>"
                )
                if not _slots:
                    _html_out += (
                        f"<p style='color:#888;font-size:12px;margin:0'>"
                        f"Žiadne výhodné obchody nad rámec DAM plánu.</p></div>"
                    )
                    return _html_out
                _html_out += (
                    f"<table style='width:100%;border-collapse:collapse;font-size:11px'>"
                    f"<thead><tr style='background:#f0f0f0'>"
                    f"<th style='padding:4px;text-align:left'>čas</th>"
                    f"<th style='padding:4px;text-align:center'>smer</th>"
                    f"<th style='padding:4px;text-align:right'>kWh</th>"
                    f"<th style='padding:4px;text-align:right'>cena €/MWh</th>"
                    f"<th style='padding:4px;text-align:right'>DAM</th>"
                    f"<th style='padding:4px;text-align:right'>Δ €</th>"
                    f"<th style='padding:4px;text-align:right'>SOC pred→po</th>"
                    f"<th style='padding:4px;text-align:center'>status</th>"
                    f"</tr></thead><tbody>"
                )
                for _s in _slots:
                    _dir = _s.get("direction", "")
                    if _dir == "BUY":
                        _dir_clr = "#C62828"; _dir_icon = "📥"
                    elif _dir == "SELL":
                        _dir_clr = "#28a745"; _dir_icon = "📤"
                    elif _dir == "CURTAIL_FTV":
                        _dir_clr = "#FF9800"; _dir_icon = "✂️"
                    elif _dir == "LOAD_COVER":
                        _dir_clr = "#0288D1"; _dir_icon = "🔌"
                    else:
                        _dir_clr = "#666"; _dir_icon = ""
                    _status = _s.get("status", "LIVE")
                    _status_clr = "#28a745" if _status == "LIVE" else "#E65100"
                    _soc_b = _s.get('soc_before_pct')
                    _soc_a = _s.get('soc_after_pct')
                    _soc_str = (f"{_soc_b:.0f}%→{_soc_a:.0f}%"
                                if (_soc_b is not None and _soc_a is not None)
                                else "—")
                    _row_extra = ""
                    if _dir == "CURTAIL_FTV":
                        _reason_txt = _s.get("reason", "")
                        if _reason_txt:
                            _row_extra = (f"<tr><td colspan='8' style='padding:0 8px 4px;"
                                          f"font-size:10px;color:#FF6F00;font-style:italic'>"
                                          f"&nbsp;&nbsp;&nbsp;↳ {_html.escape(_reason_txt)}</td></tr>")
                    _html_out += (
                        f"<tr style='border-bottom:1px solid #eee'>"
                        f"<td style='padding:3px;font-family:monospace'>{_s.get('slot_label','')}</td>"
                        f"<td style='padding:3px;text-align:center;color:{_dir_clr};font-weight:600'>"
                        f"{_dir_icon} {_dir}</td>"
                        f"<td style='padding:3px;text-align:right'>{_s.get('kwh',0):.1f}</td>"
                        f"<td style='padding:3px;text-align:right'>{_s.get('price_eur_mwh',0):.2f}</td>"
                        f"<td style='padding:3px;text-align:right;color:#666'>{_s.get('dam_clearing_eur_mwh',0):.2f}</td>"
                        f"<td style='padding:3px;text-align:right;color:#28a745;font-weight:600'>"
                        f"+{_s.get('delta_profit_eur',0):.2f}</td>"
                        f"<td style='padding:3px;text-align:right;color:#666'>{_soc_str}</td>"
                        f"<td style='padding:3px;text-align:center'>"
                        f"<span style='background:{_status_clr};color:#fff;padding:1px 6px;"
                        f"border-radius:3px;font-size:10px'>{_status}</span></td>"
                        f"</tr>{_row_extra}"
                    )
                _html_out += "</tbody></table></div>"
                return _html_out

            body += (
                f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:8px 0'>"
                f"{_render_extras_table(_res_lp, '🧮 LP re-solve (globálne optimum)', '#1F4E78')}"
                f"{_render_extras_table(_res_greedy, '⚡ Greedy (per-slot scoring)', '#E65100')}"
                f"</div>"
            )
            body += (
                f"<p style='color:#888;font-size:11px;margin-top:8px'>"
                f"<i>LP a Greedy môžu vrátiť rozdielne sady — LP zohľadňuje "
                f"globálne SOC plánovanie, Greedy berie každý slot izolovane. "
                f"Cenu zapíšeš manuálne do OKTE participant portálu ako limit order.</i></p>"
            )
    except Exception as _e_extras:
        body += (
            f"<p style='color:#C0392B;font-size:12px;margin-top:14px'>"
            f"⚠ Extra obchody nad DAM zlyhali: {_html.escape(str(_e_extras))}</p>"
        )

    body += (
        f"<p style='color:#888;font-size:11px;margin-top:14px'>"
        f"<i>Rolling MPC (Model Predictive Control) — LP re-optimization s aktuálnym "
        f"SOC + fresh orderbookom. Pri každom refresh stránky alebo zmene parametrov "
        f"sa plán prepočíta. <b>Read-only</b> — neposiela obchody na trh. "
        f"Pre auto-execute do Bender treba samostatne potvrdiť.</i></p>"
        f"</div>"
    )

    return render_legacy_body(None, "VDT Live Advisor", body,
                                head_extra="<meta http-equiv='refresh' content='60'>")


@app.get("/vdt/d1", response_class=HTMLResponse)
def vdt_d1_page(date: str = "", profile: str = ""):
    """D-1 plán — READ-ONLY VIEWER existujúceho plánu z plan_store.

    Plán sa generuje cez /dentrh (15-min) alebo /plan (hodinový).
    Táto stránka len zobrazuje aktuálny stav cascade fallback (dentrh → plan).
    VDT live advisor číta z toho istého cascade — guarantuje konzistenciu.
    """
    import html as _html
    import json as _json
    import datetime as _dt
    try:
        import market as _mk
        import plan_store as _ps
    except ImportError as e:
        return f"<p style='color:#C0392B'>Modul nedostupný: {e}</p>"

    # Default deň = zajtra
    today = _dt.date.today()
    tomorrow = today + _dt.timedelta(days=1)
    if not date:
        date_obj = tomorrow
    else:
        try:
            date_obj = _dt.datetime.strptime(date, "%Y-%m-%d").date()
        except Exception:
            date_obj = tomorrow

    active_market = _mk.get_active_market()
    market_label = _mk.label_for(active_market)

    # Profile resolve
    try:
        active_profile = _ps.resolve_profile() or "default"
    except Exception:
        active_profile = "default"
    if not profile:
        profile = active_profile

    nav = _nav("/vdt")
    body = (
        f"{nav}"
        f"<div style='max-width:1500px;margin:14px auto;padding:0 16px;"
        f"font-family:-apple-system,Segoe UI,Arial'>"
        f"<h1 style='color:#1F4E78;margin-bottom:4px'>📅 D-1 plán — {market_label}</h1>"
        f"{_vdt_subnav('/vdt/d1')}"
        f"<p style='color:#666;margin:0 0 12px;font-size:13px'>"
        f"D-1 plán pre <b>{date_obj.strftime('%a %d.%m.%Y')}</b>, profile "
        f"<b>{_html.escape(profile)}</b>. Plán sa generuje cez "
        f"<a href='/dentrh' style='color:#1F4E78'>Denný trh 15-min</a> alebo "
        f"<a href='/plan' style='color:#1F4E78'>Plán D-1 (hodinový)</a>. "
        f"Táto stránka len zobrazuje aktuálny stav.</p>"
    )

    # Date selector (pre prepínanie dní)
    body += (
        f"<form method='get' action='/vdt/d1' "
        f"style='background:#eef3f9;padding:10px;border-radius:8px;margin:10px 0;"
        f"display:flex;gap:10px;align-items:center;font-size:13px'>"
        f"<label>Deň: <input type='date' name='date' value='{date_obj.isoformat()}' "
        f"style='padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Profile: <input type='text' name='profile' value='{_html.escape(profile)}' "
        f"style='width:140px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<button type='submit' style='background:#1F4E78;color:#fff;border:0;"
        f"padding:8px 14px;border-radius:6px;cursor:pointer;font-weight:600'>"
        f"🔄 Zobraziť</button>"
        f"</form>"
    )

    # Cascade: skús dentrh (15-min) najprv, potom plan (hourly)
    schedule = None
    summary = None
    params = None
    source_kind = None
    source_step = None
    try:
        plan_15 = _ps.load_plan(date_obj.isoformat(), step_min=15, kind="dentrh",
                                  profile=profile)
        if plan_15 and plan_15.get("schedule"):
            schedule = plan_15["schedule"]
            summary = plan_15.get("summary", {})
            params = plan_15.get("params", {})
            source_kind = "dentrh"
            source_step = 15
    except Exception:
        pass
    if schedule is None:
        try:
            plan_60 = _ps.load_plan(date_obj.isoformat(), step_min=60, kind="plan",
                                      profile=profile)
            if plan_60 and plan_60.get("schedule"):
                schedule = plan_60["schedule"]
                summary = plan_60.get("summary", {})
                params = plan_60.get("params", {})
                source_kind = "plan"
                source_step = 60
        except Exception:
            pass

    if schedule is None:
        body += (
            f"<div style='background:#fff3cd;border-left:4px solid #856404;padding:16px;"
            f"border-radius:8px;margin:14px 0;font-size:14px'>"
            f"<b>⚠ Žiadny plán pre {date_obj.strftime('%a %d.%m.%Y')} v profile "
            f"<code>{_html.escape(profile)}</code></b><br>"
            f"<span style='color:#666;font-size:13px'>"
            f"Vygeneruj plán cez "
            f"<a href='/dentrh?date={date_obj.isoformat()}' style='color:#1F4E78'>Denný trh 15-min</a>"
            f" alebo <a href='/plan?date={date_obj.isoformat()}' style='color:#1F4E78'>Plán D-1 (hodinový)</a>."
            f"</span></div></div></body></html>"
        )
        return render_legacy_body(None, "VDT D-1", body)

    # Convert Dict[str, list] na list of dicts pre zobrazenie
    if isinstance(schedule, dict):
        n = max((len(v) for v in schedule.values() if isinstance(v, list)), default=0)
        rows = []
        for i in range(n):
            row = {}
            for k, v in schedule.items():
                if isinstance(v, list) and i < len(v):
                    row[k] = v[i]
            rows.append(row)
        schedule = rows

    # Normalizácia polí — /dentrh + /plan ukladajú `_charge_kw` (kW), `price_eur`,
    # `_export_kwh`..., zatiaľ čo /vdt/d1 viewer pôvodne očakával `ch_kwh`, `cena_EUR`,
    # `ex_kwh`... (formát z d1_planner). Tu doplníme chýbajúce aliasy.
    # Pre dt_h zistíme z source_step (60-min → 1.0, 15-min → 0.25).
    _dt_h = 1.0 if source_step == 60 else 0.25
    def _safe_num(v):
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0
    for i, r in enumerate(schedule):
        # ch_kwh / di_kwh ak chýba → vyrob z _charge_kw / _discharge_kw × dt_h
        if "ch_kwh" not in r and "_charge_kw" in r:
            r["ch_kwh"] = _safe_num(r.get("_charge_kw")) * _dt_h
        if "di_kwh" not in r and "_discharge_kw" in r:
            r["di_kwh"] = _safe_num(r.get("_discharge_kw")) * _dt_h
        # cena_EUR ak chýba → použiť price_eur
        if "cena_EUR" not in r and "price_eur" in r:
            r["cena_EUR"] = r["price_eur"]
        # ex_kwh / im_kwh ak chýba → _export_kwh / _import_kwh
        if "ex_kwh" not in r and "_export_kwh" in r:
            r["ex_kwh"] = r["_export_kwh"]
        if "im_kwh" not in r and "_import_kwh" in r:
            r["im_kwh"] = r["_import_kwh"]
        # period ak chýba — vyrob z indexu i a source_step
        if "period" not in r or not r.get("period"):
            if source_step == 60:
                r["period"] = f"{i:02d}:00-{(i+1):02d}:00"
            else:
                _h, _m = (i * 15) // 60, (i * 15) % 60
                _eh, _em = ((i+1) * 15) // 60, ((i+1) * 15) % 60
                r["period"] = f"{_h:02d}:{_m:02d}-{_eh:02d}:{_em:02d}"

    # Source banner
    src_label = "Denný trh 15-min" if source_kind == "dentrh" else "Plán D-1 (hodinový)"
    src_color = "#1F4E78" if source_kind == "dentrh" else "#C49000"
    body += (
        f"<div style='background:#e7f3ff;border-left:4px solid {src_color};padding:10px 14px;"
        f"border-radius:6px;margin:8px 0;font-size:13px'>"
        f"<b>Zdroj:</b> <a href='/{source_kind if source_kind != 'plan' else 'plan'}?date={date_obj.isoformat()}' "
        f"style='color:{src_color}'>{src_label}</a> "
        f"<span style='color:#666;font-size:12px'>"
        f"(kind=<code>{source_kind}</code>, step={source_step}-min, "
        f"{len(schedule)} slotov)</span></div>"
    )

    # Sumár karty
    if summary:
        zisk = summary.get("ZISK_EUR", 0)
        pcolor = "#28a745" if zisk > 0 else "#C0392B"
        nab = summary.get("nabite_kWh", 0)
        vyb = summary.get("vybite_kWh", 0)
        imp = summary.get("import_kWh", 0)
        batt_kwh_ref = (params or {}).get("batt_kwh", 800)
        body += (
            f"<div style='display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0'>"
            f"<div style='background:#fff;border-left:4px solid {pcolor};padding:14px;border-radius:8px'>"
            f"<div style='font-size:11px;color:#666;text-transform:uppercase'>ZISK z plánu</div>"
            f"<div style='font-size:24px;font-weight:700;color:{pcolor}'>{zisk:+.2f} €</div>"
            f"</div>"
            f"<div style='background:#fff;border-left:4px solid #1F4E78;padding:14px;border-radius:8px'>"
            f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Nabíjanie</div>"
            f"<div style='font-size:24px;font-weight:700;color:#1F4E78'>{nab:.0f} kWh</div>"
            f"<div style='font-size:11px;color:#666'>z toho import {imp:.0f} kWh</div>"
            f"</div>"
            f"<div style='background:#fff;border-left:4px solid #C62828;padding:14px;border-radius:8px'>"
            f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Vybíjanie</div>"
            f"<div style='font-size:24px;font-weight:700;color:#C62828'>{vyb:.0f} kWh</div>"
            f"<div style='font-size:11px;color:#666'>= ~{vyb/max(1,batt_kwh_ref):.2f} cyklov</div>"
            f"</div>"
            f"<div style='background:#fff;border-left:4px solid #C49000;padding:14px;border-radius:8px'>"
            f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Náklady</div>"
            f"<div style='font-size:14px;font-weight:600;color:#C49000;margin-top:4px'>"
            f"poplatok siete: {summary.get('naklad_import_EUR',0):.2f} €<br>"
            f"cyklus: {summary.get('naklad_cyklus_EUR',0):.2f} €</div>"
            f"</div>"
            f"</div>"
        )

    # Chart.js graf — ceny + plán
    labels = [str(r.get("period", ""))[:5] for r in schedule]
    prices = [r.get("cena_EUR") for r in schedule]
    socs = [r.get("soc_pct") for r in schedule]
    charges = [r.get("ch_kwh", 0) or 0 for r in schedule]
    discharges = [-(r.get("di_kwh", 0) or 0) for r in schedule]
    body += (
        f"<h2 style='color:#1F4E78;margin-top:16px'>📊 D-1 plán cez deň</h2>"
        f"<div style='background:#fff;padding:12px;border-radius:8px;border:1px solid #ddd'>"
        f"<canvas id='chD1Plan' height='90'></canvas>"
        f"<canvas id='chD1Soc' height='60' style='margin-top:12px'></canvas>"
        f"</div>"
        f"<script src='https://cdn.jsdelivr.net/npm/chart.js'></script>"
        f"<script>"
        f"const lbls = {_json.dumps(labels)};"
        f"new Chart(document.getElementById('chD1Plan'),{{ data:{{labels:lbls,datasets:["
        f" {{type:'bar',label:'Nabíj kWh',data:{_json.dumps(charges)},backgroundColor:'rgba(31,78,120,0.7)',yAxisID:'y2'}},"
        f" {{type:'bar',label:'Vybíj kWh',data:{_json.dumps(discharges)},backgroundColor:'rgba(198,40,40,0.7)',yAxisID:'y2'}},"
        f" {{type:'line',label:'DAM cena €/MWh',data:{_json.dumps(prices)},borderColor:'#1F4E78',backgroundColor:'transparent',borderWidth:2,tension:0.1,pointRadius:1,yAxisID:'y1'}}"
        f"]}},options:{{responsive:true,scales:{{y1:{{position:'left',title:{{display:true,text:'€/MWh'}}}},y2:{{position:'right',title:{{display:true,text:'kWh'}},grid:{{drawOnChartArea:false}}}}}}}}}});"
        f"new Chart(document.getElementById('chD1Soc'),{{type:'line',data:{{labels:lbls,datasets:["
        f" {{label:'SOC %',data:{_json.dumps(socs)},borderColor:'#C49000',backgroundColor:'rgba(196,144,0,0.2)',borderWidth:2,fill:true,tension:0.2}}"
        f"]}},options:{{responsive:true,scales:{{y:{{min:0,max:100}}}}}}}});"
        f"</script>"
    )

    # Tabuľka iba aktívnych slotov (nie idle)
    body += (
        f"<h2 style='color:#1F4E78;margin-top:16px'>📋 Plánované akcie</h2>"
        f"<table style='width:100%;border-collapse:collapse;font-size:12px;border:1px solid #ddd'>"
        f"<thead><tr style='background:#1F4E78;color:#fff'>"
        f"<th style='padding:6px 8px'>slot</th>"
        f"<th style='padding:6px 8px'>akcia</th>"
        f"<th style='padding:6px 8px;text-align:right'>kWh</th>"
        f"<th style='padding:6px 8px;text-align:right'>cena €/MWh</th>"
        f"<th style='padding:6px 8px;text-align:right'>SOC po</th>"
        f"<th style='padding:6px 8px;text-align:right'>€ slot</th>"
        f"</tr></thead><tbody>"
    )
    for r in schedule:
        ch = r.get("ch_kwh", 0) or 0
        di = r.get("di_kwh", 0) or 0
        ex = r.get("ex_kwh", 0) or 0
        im = r.get("im_kwh", 0) or 0
        price = r.get("cena_EUR") or 0
        if ch < 0.5 and di < 0.5:
            continue
        if ch > 0.5:
            act_html = "<span style='background:#1F4E78;color:#fff;padding:2px 8px;border-radius:10px'>💚 NABÍJAŤ</span>"
            kwh = ch
            row_bg = "#e7f3ff"
            eur_slot = -(price * im / 1000.0)
        else:
            act_html = "<span style='background:#C62828;color:#fff;padding:2px 8px;border-radius:10px'>🔴 VYBÍJAŤ</span>"
            kwh = di
            row_bg = "#ffe9e9"
            eur_slot = (price * ex / 1000.0)
        soc = r.get("soc_pct") or 0
        body += (
            f"<tr style='background:{row_bg}'>"
            f"<td style='padding:3px 8px;font-family:monospace'>{_html.escape(str(r.get('period','')))}</td>"
            f"<td style='padding:3px 8px'>{act_html}</td>"
            f"<td style='padding:3px 8px;text-align:right;font-weight:600'>{kwh:.1f}</td>"
            f"<td style='padding:3px 8px;text-align:right'>{price:.2f}</td>"
            f"<td style='padding:3px 8px;text-align:right;color:#C49000;font-weight:600'>{soc:.1f}%</td>"
            f"<td style='padding:3px 8px;text-align:right;color:{'#28a745' if eur_slot>=0 else '#C0392B'}'>{eur_slot:+.3f}</td>"
            f"</tr>"
        )
    body += "</tbody></table>"

    body += (
        f"<p style='color:#888;font-size:11px;margin-top:14px'>"
        f"<i>VDT live advisor číta tento plán cez cascade <code>dentrh → plan</code>. "
        f"Pre úpravu otvor "
        f"<a href='/{source_kind if source_kind == 'plan' else 'dentrh'}?date={date_obj.isoformat()}' "
        f"style='color:#1F4E78'>{src_label}</a>.</i></p>"
        f"</div>"
    )

    return render_legacy_body(None, "VDT D-1", body)


@app.get("/vdt/backtest", response_class=HTMLResponse)
def vdt_backtest_page(
    date_from: str = "",
    date_to: str = "",
    batt_kw: float = 0,
    batt_kwh: float = 0,
    eff_c: float = 0,
    eff_d: float = 0,
    grid_fee: float = 0,
    cycle_cost: float = 0,
    min_spread: float = 0,
    soc_start_pct: float = 20.0,
    soc_end_min_pct: float = 20.0,
    soc_min_pct: float = 5.0,
    soc_max_pct: float = 95.0,
    max_cycles: float = 2.0,
    run: int = 0,
):
    """VDT Backtest — simulácia cez historické dni z OKTE clearing cien.

    Pre každý deň v rozsahu spustí LP optimizer na IDM/DAM clearing prices
    (nie live orderbook — ten historicky nemáme). Akumuluje profit + cykly.

    READ-ONLY simulácia.
    """
    import html as _html
    import datetime as _dt
    import pandas as pd
    try:
        import vdt_arbitrage as _arb
        import vdt_optimizer as _opt
    except ImportError as e:
        return f"<p style='color:#C0392B'>Modul nedostupný: {e}</p>"

    # Default range = posledných 7 dní (do včera)
    today = _dt.date.today()
    if not date_from:
        from_d = today - _dt.timedelta(days=7)
    else:
        try:
            from_d = _dt.datetime.strptime(date_from, "%Y-%m-%d").date()
        except Exception:
            from_d = today - _dt.timedelta(days=7)
    if not date_to:
        to_d = today - _dt.timedelta(days=1)
    else:
        try:
            to_d = _dt.datetime.strptime(date_to, "%Y-%m-%d").date()
        except Exception:
            to_d = today - _dt.timedelta(days=1)
    if to_d < from_d:
        from_d, to_d = to_d, from_d
    # Safety cap — max 90 dní
    if (to_d - from_d).days > 90:
        to_d = from_d + _dt.timedelta(days=90)

    # Defaults z profile
    defaults = _arb.get_default_params_from_profile()
    if batt_kw <= 0:    batt_kw = defaults["batt_kw"]
    if batt_kwh <= 0:   batt_kwh = defaults["batt_kwh"]
    if eff_c <= 0:      eff_c = defaults["eff_c"]
    if eff_d <= 0:      eff_d = defaults["eff_d"]
    if grid_fee <= 0:   grid_fee = defaults["grid_fee"]
    if cycle_cost <= 0: cycle_cost = defaults["cycle_cost"]
    if min_spread <= 0: min_spread = defaults["min_spread"]

    nav = _nav("/vdt")
    body = (
        f"{nav}"
        f"<div style='max-width:1500px;margin:14px auto;padding:0 16px;"
        f"font-family:-apple-system,Segoe UI,Arial'>"
        f"<h1 style='color:#1F4E78;margin-bottom:4px'>📜 VDT Backtest</h1>"
        f"{_vdt_subnav('/vdt/backtest')}"
        f"<p style='color:#666;margin:0 0 12px'>Spätná simulácia: čo keby som obchodoval VDT cez minulé dni "
        f"s daným batt setupom. Zdroj cien: <b>OKTE clearing</b> (IDM priemer + DAM fallback). "
        f"<b>Read-only.</b></p>"
    )

    # Form
    form = (
        f"<form method='get' action='/vdt/backtest' "
        f"style='background:#eef3f9;padding:12px;border-radius:8px;margin:10px 0;"
        f"display:grid;grid-template-columns:repeat(4,1fr);gap:10px;font-size:13px'>"
        f"<label>Od dátumu: <input type='date' name='date_from' value='{from_d.isoformat()}' "
        f"style='padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Do dátumu: <input type='date' name='date_to' value='{to_d.isoformat()}' "
        f"style='padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Batt kW: <input type='number' name='batt_kw' value='{batt_kw}' step='10' "
        f"style='width:80px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Batt kWh: <input type='number' name='batt_kwh' value='{batt_kwh}' step='10' "
        f"style='width:80px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"

        f"<label>η nab: <input type='number' name='eff_c' value='{eff_c}' step='0.01' min='0.5' max='1.0' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>η vyb: <input type='number' name='eff_d' value='{eff_d}' step='0.01' min='0.5' max='1.0' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Poplatok €/MWh: <input type='number' name='grid_fee' value='{grid_fee}' step='1' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Cyklus €/MWh: <input type='number' name='cycle_cost' value='{cycle_cost}' step='0.5' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"

        f"<label>Min spread €/MWh: <input type='number' name='min_spread' value='{min_spread}' step='1' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Max cyklov/deň: <input type='number' name='max_cycles' value='{max_cycles}' step='0.1' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>SOC štart %: <input type='number' name='soc_start_pct' value='{soc_start_pct}' step='5' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>SOC koniec min %: <input type='number' name='soc_end_min_pct' value='{soc_end_min_pct}' step='5' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"

        f"<label>SOC min %: <input type='number' name='soc_min_pct' value='{soc_min_pct}' step='1' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>SOC max %: <input type='number' name='soc_max_pct' value='{soc_max_pct}' step='1' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<input type='hidden' name='run' value='1'>"
        f"<button type='submit' style='grid-column:span 2;background:#1F4E78;color:#fff;"
        f"border:0;padding:8px 14px;border-radius:6px;cursor:pointer;font-weight:600'>"
        f"▶ Spustiť backtest</button>"
        f"</form>"
    )
    body += form

    if not run:
        body += (
            "<p style='color:#666;font-style:italic;padding:20px;background:#fafafa;"
            "border-radius:6px;border:1px dashed #ccc'>"
            "Nastavte rozsah dátumov a parametre batérie, potom kliknite ▶ Spustiť backtest. "
            "Optimizer prejde každý deň v rozsahu, vypočíta najlepší obchodný plán z clearing cien "
            "(IDM/DAM) a sčíta výsledky.</p>"
            "</div></body></html>"
        )
        return render_legacy_body(None, "VDT Backtest", body)

    # Loop per deň
    days_results = []
    total_profit = 0.0
    total_cycles = 0.0
    total_charged = 0.0
    total_discharged = 0.0
    days_with_data = 0
    days_no_data = 0
    days_iter = []
    cur = from_d
    while cur <= to_d:
        days_iter.append(cur)
        cur += _dt.timedelta(days=1)

    for d in days_iter:
        try:
            snap = _arb.build_backtest_snapshot(d)
        except Exception as e:
            days_results.append({"date": d, "ok": False, "error": f"snapshot zlyhal: {e}"})
            days_no_data += 1
            continue
        # Kontroluj či máme aspoň nejaké ceny
        if snap is None or snap.empty or snap["price_eur"].notna().sum() == 0:
            days_results.append({"date": d, "ok": False, "error": "žiadne clearing ceny"})
            days_no_data += 1
            continue

        try:
            res = _opt.optimize_vdt_day(
                snap,
                batt_kw=batt_kw, batt_kwh=batt_kwh,
                eff_c=eff_c, eff_d=eff_d,
                grid_fee=grid_fee, cycle_cost=cycle_cost,
                min_spread=min_spread,
                soc_min_pct=soc_min_pct, soc_max_pct=soc_max_pct,
                soc_start_pct=soc_start_pct,
                soc_end_min_pct=soc_end_min_pct,
                max_cycles_per_day=max_cycles if max_cycles > 0 else None,
                slot_minutes=15,
                use_orderbook=False,
                future_only=False,
            )
        except Exception as e:
            days_results.append({"date": d, "ok": False, "error": f"LP zlyhal: {e}"})
            days_no_data += 1
            continue

        if not res.get("ok"):
            days_results.append({"date": d, "ok": False, "error": res.get("error", "?")})
            days_no_data += 1
            continue

        s = res["summary"]
        days_results.append({
            "date": d, "ok": True,
            "profit": res["profit_eur"],
            "cycles": s["cycles"],
            "charged_kwh": s["total_charged_kwh"],
            "discharged_kwh": s["total_discharged_kwh"],
            "n_ch": s["n_charge_slots"],
            "n_d": s["n_discharge_slots"],
            "revenue": s["revenue_eur"],
            "cost": s["cost_eur"],
            "fees": s["fees_eur"],
            "cycle_cost_eur": s["cycle_cost_eur"],
            "soc_min": s["soc_min_pct"],
            "soc_max": s["soc_max_pct"],
        })
        total_profit += res["profit_eur"]
        total_cycles += s["cycles"]
        total_charged += s["total_charged_kwh"]
        total_discharged += s["total_discharged_kwh"]
        days_with_data += 1

    # Sumár
    n_days = len(days_iter)
    avg_profit = total_profit / days_with_data if days_with_data > 0 else 0
    profit_color = "#28a745" if total_profit > 0 else "#C0392B"
    body += (
        f"<div style='display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0'>"
        f"<div style='background:#fff;border-left:4px solid {profit_color};padding:14px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Celkový profit</div>"
        f"<div style='font-size:24px;font-weight:700;color:{profit_color}'>{total_profit:+.2f} €</div>"
        f"<div style='font-size:11px;color:#666'>za {days_with_data} dní · ⌀ {avg_profit:.2f} €/deň</div>"
        f"</div>"

        f"<div style='background:#fff;border-left:4px solid #1F4E78;padding:14px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Celkové cykly</div>"
        f"<div style='font-size:24px;font-weight:700;color:#1F4E78'>{total_cycles:.1f}</div>"
        f"<div style='font-size:11px;color:#666'>⌀ {total_cycles/days_with_data if days_with_data else 0:.2f} cyklov/deň</div>"
        f"</div>"

        f"<div style='background:#fff;border-left:4px solid #C49000;padding:14px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Energia</div>"
        f"<div style='font-size:18px;font-weight:700;color:#C49000'>{total_charged:.0f}/{total_discharged:.0f} kWh</div>"
        f"<div style='font-size:11px;color:#666'>nabité / vybité (strata {total_charged-total_discharged:.0f} kWh)</div>"
        f"</div>"

        f"<div style='background:#fff;border-left:4px solid #28a745;padding:14px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Pokrytie</div>"
        f"<div style='font-size:24px;font-weight:700;color:#28a745'>{days_with_data}/{n_days}</div>"
        f"<div style='font-size:11px;color:#666'>{days_no_data} dní bez dát</div>"
        f"</div>"
        f"</div>"
    )

    # Chart: per-day profit + kumulatívny
    chart_labels = [d["date"].strftime("%d.%m") for d in days_results]
    chart_profits = [round(d["profit"], 2) if d.get("ok") else None for d in days_results]
    chart_cum = []
    cum = 0
    for d in days_results:
        if d.get("ok"):
            cum += d["profit"]
        chart_cum.append(round(cum, 2))

    import json as _json
    body += (
        f"<h2 style='color:#1F4E78;margin-top:18px'>📊 Profit cez dni</h2>"
        f"<div style='background:#fff;padding:12px;border-radius:8px;border:1px solid #ddd'>"
        f"<canvas id='chartBacktest' height='70'></canvas></div>"
        f"<script src='https://cdn.jsdelivr.net/npm/chart.js'></script>"
        f"<script>"
        f"const lbls = {_json.dumps(chart_labels)};"
        f"const dailyP = {_json.dumps(chart_profits)};"
        f"const cumP = {_json.dumps(chart_cum)};"
        f"new Chart(document.getElementById('chartBacktest'), {{"
        f"  data:{{labels:lbls, datasets:["
        f"    {{type:'bar', label:'Profit/deň €', data:dailyP, "
        f"      backgroundColor: dailyP.map(v => v >= 0 ? 'rgba(40,167,69,0.7)' : 'rgba(192,57,43,0.7)'), yAxisID:'y1'}},"
        f"    {{type:'line', label:'Kumulatívny profit €', data:cumP, "
        f"      borderColor:'#1F4E78', backgroundColor:'rgba(31,78,120,0.1)', "
        f"      borderWidth:2, fill:true, tension:0.1, yAxisID:'y2'}}"
        f"  ]}}, options:{{responsive:true, scales:{{"
        f"    y1:{{position:'left',title:{{display:true,text:'€/deň'}}}},"
        f"    y2:{{position:'right',title:{{display:true,text:'kumulatív €'}}, grid:{{drawOnChartArea:false}}}}}}}}}});"
        f"</script>"
    )

    # Tabuľka per deň
    body += (
        f"<h2 style='color:#1F4E78;margin-top:18px'>📋 Detail per deň</h2>"
        f"<table style='width:100%;border-collapse:collapse;font-size:12px;border:1px solid #ddd'>"
        f"<thead><tr style='background:#1F4E78;color:#fff'>"
        f"<th style='padding:6px 8px;text-align:left'>dátum</th>"
        f"<th style='padding:6px 8px;text-align:right'>profit €</th>"
        f"<th style='padding:6px 8px;text-align:right'>príjem</th>"
        f"<th style='padding:6px 8px;text-align:right'>náklad</th>"
        f"<th style='padding:6px 8px;text-align:right'>fee</th>"
        f"<th style='padding:6px 8px;text-align:right'>cyklus</th>"
        f"<th style='padding:6px 8px;text-align:right'>cykly</th>"
        f"<th style='padding:6px 8px;text-align:right'>nabité kWh</th>"
        f"<th style='padding:6px 8px;text-align:right'>vybité kWh</th>"
        f"<th style='padding:6px 8px;text-align:right'>SOC %</th>"
        f"<th style='padding:6px 8px;text-align:right'>akcie</th>"
        f"</tr></thead><tbody>"
    )
    for d in days_results:
        weekday = d["date"].strftime("%a")
        if not d.get("ok"):
            body += (
                f"<tr style='background:#ffe7e7'>"
                f"<td style='padding:3px 8px;font-family:monospace'>"
                f"{d['date'].strftime('%Y-%m-%d')} <span style='color:#999;font-size:10px'>{weekday}</span></td>"
                f"<td colspan='10' style='padding:3px 8px;color:#C0392B;font-size:11px'>"
                f"⚠ {_html.escape(d.get('error','?'))[:200]}</td></tr>"
            )
            continue
        pcolor = "#28a745" if d["profit"] >= 0 else "#C0392B"
        body += (
            f"<tr>"
            f"<td style='padding:3px 8px;font-family:monospace'>"
            f"{d['date'].strftime('%Y-%m-%d')} <span style='color:#999;font-size:10px'>{weekday}</span></td>"
            f"<td style='padding:3px 8px;text-align:right;color:{pcolor};font-weight:600'>{d['profit']:+.2f}</td>"
            f"<td style='padding:3px 8px;text-align:right;color:#28a745'>+{d['revenue']:.2f}</td>"
            f"<td style='padding:3px 8px;text-align:right;color:#C0392B'>−{d['cost']:.2f}</td>"
            f"<td style='padding:3px 8px;text-align:right;color:#888'>−{d['fees']:.2f}</td>"
            f"<td style='padding:3px 8px;text-align:right;color:#888'>−{d['cycle_cost_eur']:.2f}</td>"
            f"<td style='padding:3px 8px;text-align:right;color:#1F4E78;font-weight:600'>{d['cycles']:.2f}</td>"
            f"<td style='padding:3px 8px;text-align:right'>{d['charged_kwh']:.0f}</td>"
            f"<td style='padding:3px 8px;text-align:right'>{d['discharged_kwh']:.0f}</td>"
            f"<td style='padding:3px 8px;text-align:right;color:#C49000'>{d['soc_min']:.0f}–{d['soc_max']:.0f}</td>"
            f"<td style='padding:3px 8px;text-align:right;color:#666'>{d['n_ch']}+{d['n_d']}</td>"
            f"</tr>"
        )
    body += "</tbody></table>"

    body += (
        f"<p style='color:#888;font-size:11px;margin-top:14px'>"
        f"<i>Backtest používa <b>IDM avg + DAM clearing</b> ako predpokladané ceny (žiadny "
        f"orderbook bid/ask spread — reálny zisk z orderbooku by mohol byť aj vyšší). "
        f"LP optimum per deň, eff_RT = {eff_c*eff_d:.4f}.</i></p>"
        f"</div>"
    )

    return ("<!doctype html><html lang='sk'><head><meta charset='utf-8'>"
            "<title>VDT Backtest</title></head><body>" + body + "</body></html>")


@app.get("/vdt/simulator", response_class=HTMLResponse)
def vdt_simulator_page(
    batt_kw: float = 0,
    batt_kwh: float = 0,
    eff_c: float = 0,
    eff_d: float = 0,
    grid_fee: float = 0,
    cycle_cost: float = 0,
    min_spread: float = 0,
    soc_start_pct: float = 20.0,
    soc_end_min_pct: float = 20.0,
    soc_min_pct: float = 5.0,
    soc_max_pct: float = 95.0,
    max_cycles: float = 2.0,
    use_orderbook: int = 1,
    run: int = 0,
):
    """VDT Simulátor — LP optimalizácia denného obchodovania s batériou.

    READ-ONLY: iba simuluje, nezasiela žiadne objednávky na trh.
    """
    import html as _html
    import datetime as _dt
    import pandas as pd
    try:
        import vdt_arbitrage as _arb
        import vdt_optimizer as _opt
        import okte_vdt as _vdt
    except ImportError as e:
        return f"<p style='color:#C0392B'>Modul nedostupný: {e}</p>"

    # Defaults z profile params
    defaults = _arb.get_default_params_from_profile()
    if batt_kw <= 0:    batt_kw = defaults["batt_kw"]
    if batt_kwh <= 0:   batt_kwh = defaults["batt_kwh"]
    if eff_c <= 0:      eff_c = defaults["eff_c"]
    if eff_d <= 0:      eff_d = defaults["eff_d"]
    if grid_fee <= 0:   grid_fee = defaults["grid_fee"]
    if cycle_cost <= 0: cycle_cost = defaults["cycle_cost"]
    if min_spread <= 0: min_spread = defaults["min_spread"]

    nav = _nav("/vdt")
    body = (
        f"{nav}"
        f"<div style='max-width:1500px;margin:14px auto;padding:0 16px;"
        f"font-family:-apple-system,Segoe UI,Arial'>"
        f"<h1 style='color:#1F4E78;margin-bottom:4px'>🎯 VDT Simulátor</h1>"
        f"{_vdt_subnav('/vdt/simulator')}"
        f"<p style='color:#666;margin:0 0 12px'>LP optimalizácia denného obchodovania — "
        f"max profit pri batt fyzike + likvidite orderbook. <b>Read-only — nezasiela obchody.</b></p>"
    )

    # Form
    form = (
        f"<form method='get' action='/vdt/simulator' "
        f"style='background:#eef3f9;padding:12px;border-radius:8px;margin:10px 0;"
        f"display:grid;grid-template-columns:repeat(4,1fr);gap:10px;font-size:13px'>"
        f"<label>Batt kW: <input type='number' name='batt_kw' value='{batt_kw}' step='10' "
        f"style='width:80px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Batt kWh: <input type='number' name='batt_kwh' value='{batt_kwh}' step='10' "
        f"style='width:80px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>η nab: <input type='number' name='eff_c' value='{eff_c}' step='0.01' min='0.5' max='1.0' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>η vyb: <input type='number' name='eff_d' value='{eff_d}' step='0.01' min='0.5' max='1.0' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"

        f"<label>Poplatok €/MWh: <input type='number' name='grid_fee' value='{grid_fee}' step='1' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Cyklus €/MWh: <input type='number' name='cycle_cost' value='{cycle_cost}' step='0.5' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Min spread €/MWh: <input type='number' name='min_spread' value='{min_spread}' step='1' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Max cyklov/deň: <input type='number' name='max_cycles' value='{max_cycles}' step='0.1' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"

        f"<label>SOC štart %: <input type='number' name='soc_start_pct' value='{soc_start_pct}' step='5' min='0' max='100' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>SOC koniec min %: <input type='number' name='soc_end_min_pct' value='{soc_end_min_pct}' step='5' min='0' max='100' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>SOC min %: <input type='number' name='soc_min_pct' value='{soc_min_pct}' step='1' min='0' max='100' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>SOC max %: <input type='number' name='soc_max_pct' value='{soc_max_pct}' step='1' min='0' max='100' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"

        f"<label style='grid-column:span 2;display:flex;align-items:center;gap:6px'>"
        f"<input type='checkbox' name='use_orderbook' value='1'"
        + (" checked" if use_orderbook else "") +
        f"> Použiť live orderbook (bid/ask + likvidita)</label>"
        f"<input type='hidden' name='run' value='1'>"
        f"<button type='submit' style='grid-column:span 2;background:#1F4E78;color:#fff;"
        f"border:0;padding:8px 14px;border-radius:6px;cursor:pointer;font-weight:600'>"
        f"▶ Spustiť simuláciu</button>"
        f"</form>"
    )
    body += form

    # Spustenie iba ak run=1
    if not run:
        body += (
            "<p style='color:#666;font-style:italic;padding:20px;background:#fafafa;"
            "border-radius:6px;border:1px dashed #ccc'>"
            "Nastavte parametre a stlačte tlačidlo ▶ Spustiť simuláciu. Optimizer vypočíta najlepší "
            "plán nabíjania/vybíjania pre zostávajúce sloty dňa (od aktuálnej 15-min vpred) "
            "aby maximalizoval profit pri zadaných obmedzeniach.</p>"
            "</div></body></html>"
        )
        return render_legacy_body(None, "VDT Simulátor", body)

    # Fetch snapshot + orderbook
    today = _dt.date.today()
    try:
        snapshot = _arb.get_market_snapshot(today, days_ahead=1, from_current_slot=True)
    except Exception as e:
        body += f"<p style='color:#C0392B'>get_market_snapshot zlyhal: {_html.escape(str(e))}</p></div>"
        return render_legacy_body(None, "VDT Simulátor", body)

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

    # Spusti LP
    try:
        result = _opt.optimize_vdt_day(
            snapshot,
            batt_kw=batt_kw, batt_kwh=batt_kwh,
            eff_c=eff_c, eff_d=eff_d,
            grid_fee=grid_fee, cycle_cost=cycle_cost,
            min_spread=min_spread,
            soc_min_pct=soc_min_pct, soc_max_pct=soc_max_pct,
            soc_start_pct=soc_start_pct,
            soc_end_min_pct=soc_end_min_pct,
            max_cycles_per_day=max_cycles if max_cycles > 0 else None,
            slot_minutes=15,
            use_orderbook=bool(use_orderbook),
            future_only=True,
        )
    except Exception as e:
        body += f"<p style='color:#C0392B'>Optimizer zlyhal: {_html.escape(str(e))}</p></div>"
        return render_legacy_body(None, "VDT Simulátor", body)

    if not result.get("ok"):
        body += (
            f"<div style='background:#ffe7e7;border-left:4px solid #C0392B;padding:14px;border-radius:6px'>"
            f"<b>Simulácia zlyhala:</b> {_html.escape(str(result.get('error','?')))}</div>"
            "</div>"
        )
        return render_legacy_body(None, "VDT Simulátor", body)

    # Sumár
    s = result["summary"]
    profit = result["profit_eur"]
    profit_color = "#28a745" if profit > 0 else "#C0392B"
    body += (
        f"<div style='display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0'>"
        f"<div style='background:#fff;border-left:4px solid {profit_color};padding:14px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Očakávaný profit</div>"
        f"<div style='font-size:24px;font-weight:700;color:{profit_color}'>{profit:+.2f} €</div>"
        f"</div>"

        f"<div style='background:#fff;border-left:4px solid #1F4E78;padding:14px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Cykly</div>"
        f"<div style='font-size:24px;font-weight:700;color:#1F4E78'>{s['cycles']:.2f}</div>"
        f"<div style='font-size:11px;color:#666'>nabité {s['total_charged_kwh']:.0f} · vybité {s['total_discharged_kwh']:.0f} kWh</div>"
        f"</div>"

        f"<div style='background:#fff;border-left:4px solid #C49000;padding:14px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>SOC rozsah</div>"
        f"<div style='font-size:24px;font-weight:700;color:#C49000'>{s['soc_min_pct']:.0f}–{s['soc_max_pct']:.0f}%</div>"
        f"<div style='font-size:11px;color:#666'>štart {s['soc_start_pct']:.0f}% → koniec {s['soc_end_pct']:.0f}%</div>"
        f"</div>"

        f"<div style='background:#fff;border-left:4px solid #28a745;padding:14px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Akcie</div>"
        f"<div style='font-size:24px;font-weight:700;color:#28a745'>{s['n_charge_slots']} + {s['n_discharge_slots']}</div>"
        f"<div style='font-size:11px;color:#666'>nabíjacích + vybíjacích slotov · z {result['n_slots']}</div>"
        f"</div>"
        f"</div>"

        # Detail ekonomiky
        f"<div style='background:#f8f9fa;padding:12px;border-radius:8px;margin:10px 0;font-size:13px'>"
        f"<b>Ekonomika:</b> "
        f"📈 príjem <b style='color:#28a745'>+{s['revenue_eur']:.2f} €</b> · "
        f"📉 náklad <b style='color:#C0392B'>−{s['cost_eur']:.2f} €</b> · "
        f"⚡ poplatok siete <b>−{s['fees_eur']:.2f} €</b> · "
        f"🔋 amortizácia <b>−{s['cycle_cost_eur']:.2f} €</b> · "
        f"<b>orderbook: {_html.escape(ob_status)}</b>"
        f"</div>"
    )

    # Chart.js graf
    chart_labels = [t["slot"][:5] for t in result["trades"]]
    chart_buy = [t.get("buy_price_eur_mwh") for t in result["trades"]]
    chart_sell = [t.get("sell_price_eur_mwh") for t in result["trades"]]
    chart_soc = [t["soc_after_pct"] for t in result["trades"]]
    chart_charge = [t["charge_kwh"] if t["action"] == "charge" else 0 for t in result["trades"]]
    chart_discharge = [-t["discharge_kwh"] if t["action"] == "discharge" else 0 for t in result["trades"]]

    # Vsetky ceny z orderbook (aj keď slot bol idle)
    chart_ask = []
    chart_bid = []
    for t in result["trades"]:
        idx = t["slot_idx"]
        if idx < len(snapshot):
            row = snapshot.iloc[idx]
            ask = row.get("ob_best_ask_eur")
            bid = row.get("ob_best_bid_eur")
            chart_ask.append(float(ask) if ask is not None and pd.notna(ask) else None)
            chart_bid.append(float(bid) if bid is not None and pd.notna(bid) else None)
        else:
            chart_ask.append(None); chart_bid.append(None)

    import json as _json
    body += (
        f"<h2 style='color:#1F4E78;margin-top:18px'>📊 Plán cez deň</h2>"
        f"<div style='background:#fff;padding:12px;border-radius:8px;border:1px solid #ddd'>"
        f"<canvas id='chartPlan' height='90'></canvas>"
        f"<canvas id='chartSOC' height='60' style='margin-top:14px'></canvas>"
        f"</div>"
        f"<script src='https://cdn.jsdelivr.net/npm/chart.js'></script>"
        f"<script>"
        f"const labels = {_json.dumps(chart_labels)};"
        f"const askArr = {_json.dumps(chart_ask)};"
        f"const bidArr = {_json.dumps(chart_bid)};"
        f"const chgArr = {_json.dumps(chart_charge)};"
        f"const dchArr = {_json.dumps(chart_discharge)};"
        f"const socArr = {_json.dumps(chart_soc)};"

        f"new Chart(document.getElementById('chartPlan'), {{"
        f"  type:'bar', data:{{labels:labels, datasets:["
        f"    {{label:'Nabíjanie kWh', data:chgArr, backgroundColor:'rgba(31,78,120,0.7)', yAxisID:'y2'}},"
        f"    {{label:'Vybíjanie kWh', data:dchArr, backgroundColor:'rgba(198,40,40,0.7)', yAxisID:'y2'}},"
        f"    {{type:'line', label:'Ask €/MWh', data:askArr, borderColor:'#C62828', backgroundColor:'transparent', borderWidth:2, yAxisID:'y1', tension:0.1, pointRadius:1, spanGaps:true}},"
        f"    {{type:'line', label:'Bid €/MWh', data:bidArr, borderColor:'#1F4E78', backgroundColor:'transparent', borderWidth:2, yAxisID:'y1', tension:0.1, pointRadius:1, spanGaps:true}}"
        f"  ]}}, options:{{responsive:true, plugins:{{title:{{display:true,text:'Ceny + akcie batérie'}}}}, scales:{{y1:{{position:'left',title:{{display:true,text:'€/MWh'}}}}, y2:{{position:'right',title:{{display:true,text:'kWh (kladná=nabíjanie, záporná=vybíjanie)'}}, grid:{{drawOnChartArea:false}}}}}}}}}});"

        f"new Chart(document.getElementById('chartSOC'), {{"
        f"  type:'line', data:{{labels:labels, datasets:["
        f"    {{label:'SOC %', data:socArr, borderColor:'#C49000', backgroundColor:'rgba(196,144,0,0.2)', borderWidth:2, fill:true, tension:0.2}}"
        f"  ]}}, options:{{responsive:true, plugins:{{title:{{display:true,text:'Stav nabitia batérie cez deň'}}}}, scales:{{y:{{min:0,max:100,title:{{display:true,text:'SOC %'}}}}}}}}}});"
        f"</script>"
    )

    # Tabuľka akcií
    body += (
        f"<h2 style='color:#1F4E78;margin-top:18px'>📋 Plán obchodov</h2>"
        f"<table style='width:100%;border-collapse:collapse;font-size:12px;border:1px solid #ddd'>"
        f"<thead><tr style='background:#1F4E78;color:#fff'>"
        f"<th style='padding:6px 8px;text-align:left'>slot</th>"
        f"<th style='padding:6px 8px;text-align:left'>akcia</th>"
        f"<th style='padding:6px 8px;text-align:right'>kWh</th>"
        f"<th style='padding:6px 8px;text-align:right'>cena €/MWh</th>"
        f"<th style='padding:6px 8px;text-align:right'>SOC po (kWh)</th>"
        f"<th style='padding:6px 8px;text-align:right'>SOC po (%)</th>"
        f"<th style='padding:6px 8px;text-align:right'>€ slot</th>"
        f"</tr></thead><tbody>"
    )
    for t in result["trades"]:
        action = t["action"]
        if action == "charge":
            act_html = "<span style='background:#1F4E78;color:#fff;padding:2px 8px;border-radius:10px'>💚 NABÍJAŤ</span>"
            kwh_str = f"{t['charge_kwh']:.1f}"
            price = t.get('buy_price_eur_mwh')
            price_str = f"{price:.2f}" if price is not None else "—"
            eur_slot = -(price or 0) * t['charge_kwh'] / 1000.0 - grid_fee * t['charge_kwh'] / 1000.0 if price else 0
            row_bg = "#e7f3ff"
        elif action == "discharge":
            act_html = "<span style='background:#C62828;color:#fff;padding:2px 8px;border-radius:10px'>🔴 VYBÍJAŤ</span>"
            kwh_str = f"{t['discharge_kwh']:.1f}"
            price = t.get('sell_price_eur_mwh')
            price_str = f"{price:.2f}" if price is not None else "—"
            eur_slot = (price or 0) * t['discharge_kwh'] / 1000.0 - grid_fee * t['discharge_kwh'] / 1000.0 if price else 0
            row_bg = "#ffe9e9"
        else:
            act_html = "<span style='color:#999'>idle</span>"
            kwh_str = "—"
            price_str = "—"
            eur_slot = 0
            row_bg = "#fafafa"
        body += (
            f"<tr style='background:{row_bg}'>"
            f"<td style='padding:3px 8px;font-family:monospace'>{t['slot']}</td>"
            f"<td style='padding:3px 8px'>{act_html}</td>"
            f"<td style='padding:3px 8px;text-align:right;font-weight:600'>{kwh_str}</td>"
            f"<td style='padding:3px 8px;text-align:right'>{price_str}</td>"
            f"<td style='padding:3px 8px;text-align:right;color:#666'>{t['soc_after_kwh']:.0f}</td>"
            f"<td style='padding:3px 8px;text-align:right;color:#C49000;font-weight:600'>{t['soc_after_pct']:.1f}%</td>"
            f"<td style='padding:3px 8px;text-align:right;color:{'#28a745' if eur_slot>=0 else '#C0392B'}'>"
            f"{eur_slot:+.3f}</td>"
            f"</tr>"
        )
    body += "</tbody></table>"

    body += (
        f"<p style='color:#888;font-size:11px;margin-top:14px'>"
        f"<i>LP optimizer (scipy.linprog) · {result['n_slots']} slotov · "
        f"status: {_html.escape(s.get('lp_status', '?'))}. "
        f"Read-only nástroj — žiadne objednávky sa neposielajú na trh.</i></p>"
        f"</div>"
    )

    return ("<!doctype html><html lang='sk'><head><meta charset='utf-8'>"
            "<title>VDT Simulátor</title></head><body>" + body + "</body></html>")


@app.get("/vdt/board", response_class=HTMLResponse)
def vdt_board_page(
    date: str = "",
    batt_kw: float = 0,
    batt_kwh: float = 0,
    eff_c: float = 0,
    eff_d: float = 0,
    grid_fee: float = 0,
    cycle_cost: float = 0,
    min_spread: float = 0,
    top_n: int = 20,
    show_unprofitable: int = 0,
    days_ahead: int = 1,
):
    """Arbitrage cockpit — top 5 pairs + 96 slot table + status. Auto-refresh 30s.
    Form polia editovateľné (default z aktívneho profile)."""
    import html as _html
    import datetime as _dt
    import pandas as pd
    try:
        import vdt_arbitrage as _arb
        import okte_vdt as _vdt
    except ImportError as e:
        return f"<p style='color:#C0392B'>Modul nedostupný: {e}</p>"

    # Parse date
    if not date:
        date_obj = _dt.date.today()
    else:
        try:
            date_obj = _dt.datetime.strptime(date, "%Y-%m-%d").date()
        except Exception:
            date_obj = _dt.date.today()

    # Defaults z profilu ak nie sú zadané v URL
    defaults = _arb.get_default_params_from_profile()
    if batt_kw <= 0:    batt_kw = defaults["batt_kw"]
    if batt_kwh <= 0:   batt_kwh = defaults["batt_kwh"]
    if eff_c <= 0:      eff_c = defaults["eff_c"]
    if eff_d <= 0:      eff_d = defaults["eff_d"]
    if grid_fee <= 0:   grid_fee = defaults["grid_fee"]
    if cycle_cost <= 0: cycle_cost = defaults["cycle_cost"]
    if min_spread <= 0: min_spread = defaults["min_spread"]

    # Fetch market data — VŽDY od dnes vpred (aj keď user vyberie zajtra v form),
    # aby sme nestratili dnešné IDM dáta. days_ahead pokrýva aj zajtra (DAM).
    today = _dt.date.today()
    # Ak user vybral budúci deň, rozšírime days_ahead aby vybraný deň bol pokrytý
    span_days = max(int(days_ahead or 1), (date_obj - today).days, 1)
    span_days = min(span_days, 7)   # safety cap
    try:
        snapshot = _arb.get_market_snapshot(today, days_ahead=span_days,
                                              from_current_slot=True)
    except Exception as e:
        return f"<p style='color:#C0392B'>get_market_snapshot zlyhal: {_html.escape(str(e))}</p>"

    # Pridať vlastné obchody overlay (range = celý zobrazený interval)
    try:
        today_iso = date_obj.isoformat()
        next_iso = (date_obj + _dt.timedelta(days=2)).isoformat()
        trades_res = _vdt.get_trades(
            delivery_from=f"{today_iso}T00:00:00Z",
            delivery_to=f"{next_iso}T00:00:00Z",
        )
        snapshot = _arb.add_own_trades(snapshot, trades_res)
    except Exception as e:
        snapshot["own_buy_qty"] = 0.0
        snapshot["own_sell_qty"] = 0.0

    # Live orderbook overlay (best bid/ask z OKTE SOAP IdmOrderBook)
    # Tichý fallback — keď SOAP zlyhá, snapshot zostane bez ob_* stĺpcov
    orderbook_status = "(nedostupné)"
    try:
        ob_res = _vdt.get_orderbook()   # všetky produkty (15+60)
        snapshot = _arb.add_orderbook(snapshot, ob_res)
        if ob_res.get("ok"):
            orderbook_status = f"✓ {ob_res.get('stats',{}).get('trades_parsed',0)} ponúk"
        else:
            orderbook_status = f"✗ {str(ob_res.get('error',''))[:60]}"
    except Exception as e:
        orderbook_status = f"✗ exception: {str(e)[:60]}"
        # Zabezpečí že ob_* stĺpce existujú
        for c in ("ob_best_bid_eur", "ob_best_bid_mw", "ob_best_ask_eur",
                  "ob_best_ask_mw", "ob_spread_eur", "ob_n_bids", "ob_n_asks"):
            if c not in snapshot.columns:
                snapshot[c] = None

    # Stav trhu
    try:
        ms = _vdt.get_market_status()
        if ms.get("ok"):
            md = ms.get("data") or {}
            market_status = md.get("tradingStatus", "?")
            market_time = md.get("systemTime", "?")
        else:
            market_status = "(unavailable)"; market_time = "?"
    except Exception:
        market_status = "(error)"; market_time = "?"

    # Arbitrage pairs — preferuj orderbook (live bid/ask) ak je dostupný,
    # inak fallback na DAM/IDM clearing
    show_unprof_bool = bool(show_unprofitable)
    has_orderbook = ("ob_best_ask_eur" in snapshot.columns and
                       snapshot["ob_best_ask_eur"].notna().any())
    if has_orderbook:
        # Live orderbook: buy @ best_ask, sell @ best_bid
        pairs_filtered = _arb.compute_arbitrage_pairs_orderbook(
            snapshot,
            batt_kw=batt_kw, batt_kwh=batt_kwh,
            eff_c=eff_c, eff_d=eff_d,
            grid_fee=grid_fee, cycle_cost=cycle_cost,
            min_spread=min_spread, top_n=5,
            include_unprofitable=False,
        )
        pairs_all = _arb.compute_arbitrage_pairs_orderbook(
            snapshot,
            batt_kw=batt_kw, batt_kwh=batt_kwh,
            eff_c=eff_c, eff_d=eff_d,
            grid_fee=grid_fee, cycle_cost=cycle_cost,
            min_spread=min_spread, top_n=top_n,
            include_unprofitable=True,
        )
        pairs_source = "live orderbook (best bid/ask)"
    else:
        pairs_filtered = _arb.compute_arbitrage_pairs(
            snapshot,
            batt_kw=batt_kw, batt_kwh=batt_kwh,
            eff_c=eff_c, eff_d=eff_d,
            grid_fee=grid_fee, cycle_cost=cycle_cost,
            min_spread=min_spread,
            future_only=True, top_n=5,
            include_unprofitable=False,
        )
        pairs_all = _arb.compute_arbitrage_pairs(
            snapshot,
            batt_kw=batt_kw, batt_kwh=batt_kwh,
            eff_c=eff_c, eff_d=eff_d,
            grid_fee=grid_fee, cycle_cost=cycle_cost,
            min_spread=min_spread,
            future_only=True, top_n=top_n,
            include_unprofitable=True,
        )
        pairs_source = "DAM/IDM clearing prices (orderbook nedostupné)"
    pairs = pairs_filtered  # legacy var (top 5)

    # Tabuľka slotov — od aktuálnej 15-min dopredu (dnes + zajtra)
    rows_html = []
    eff_rt = eff_c * eff_d
    # Snapshot diag
    diag = _arb.snapshot_diag(snapshot)
    # Day separator — keď sa zmení dátum medzi riadkami, vložíme oddelovací riadok
    prev_date = None
    for _, r in snapshot.iterrows():
        row_date = r.get("date") or str(r["start_local"])[:10]
        if prev_date is not None and row_date != prev_date:
            # Day boundary marker
            day_label = pd.to_datetime(row_date).strftime("%a %d.%m.%Y")
            rows_html.append(
                f"<tr><td colspan='11' style='background:#1F4E78;color:#fff;padding:6px 10px;"
                f"font-weight:600;text-align:center'>📅 {day_label}</td></tr>"
            )
        prev_date = row_date

        period = r["period"]
        is_past = bool(r["is_past"])
        is_live = bool(r.get("is_live", False))
        price = r.get("price_eur")
        bg = _arb.slot_color(price, snapshot["price_eur"])
        if is_past:
            bg = "#eaeaea"
        elif is_live:
            bg = "#cfe2ff"

        price_str = f"{price:.2f}" if (price is not None and pd.notna(price)) else "—"
        src = r.get("price_source", "?")
        vol_str = f"{r.get('idm_volume', 0):.1f}" if r.get("idm_volume", 0) > 0 else "—"
        rng_str = ""
        if pd.notna(r.get("idm_min")) and pd.notna(r.get("idm_max")):
            rng_str = f"{r['idm_min']:.0f}–{r['idm_max']:.0f}"

        own_buy = r.get("own_buy_qty", 0)
        own_sell = r.get("own_sell_qty", 0)
        own_str = ""
        if own_buy > 0:
            own_str += f"<span style='color:#1F4E78;font-weight:600'>↑{own_buy:.2f} @ {r['own_buy_avg']:.0f}</span>"
        if own_sell > 0:
            if own_str: own_str += " "
            own_str += f"<span style='color:#C62828;font-weight:600'>↓{own_sell:.2f} @ {r['own_sell_avg']:.0f}</span>"

        action = ""
        if not is_past and price is not None and pd.notna(price):
            valid = snapshot["price_eur"].dropna()
            if len(valid) >= 4:
                q25 = valid.quantile(0.25); q75 = valid.quantile(0.75)
                if price <= q25:
                    action = "<span style='color:#2E7D32;font-weight:600'>💚 NABÍJAŤ</span>"
                elif price >= q75:
                    action = "<span style='color:#C62828;font-weight:600'>🔴 VYBÍJAŤ</span>"

        # Status: text + farba (jasnejší ako emoji)
        if is_live:
            status_html = "<span style='background:#28a745;color:#fff;padding:2px 8px;border-radius:10px;font-weight:600;font-size:11px'>● LIVE</span>"
            row_border = "border-left:4px solid #28a745;"
        elif is_past:
            status_html = "<span style='background:#999;color:#fff;padding:2px 8px;border-radius:10px;font-size:11px'>Uplynulo</span>"
            row_border = ""
        else:
            status_html = "<span style='background:#1F4E78;color:#fff;padding:2px 8px;border-radius:10px;font-size:11px'>Budúce</span>"
            row_border = ""
        # Skrátený dátum
        day_short = pd.to_datetime(row_date).strftime("%d.%m") if row_date else "?"

        # Orderbook bid/ask
        ob_bid = r.get("ob_best_bid_eur")
        ob_ask = r.get("ob_best_ask_eur")
        ob_bid_mw = r.get("ob_best_bid_mw")
        ob_ask_mw = r.get("ob_best_ask_mw")
        bid_str = (f"<span style='color:#1F4E78;font-weight:600'>{ob_bid:.1f}</span>"
                   f" <span style='color:#999;font-size:10px'>×{ob_bid_mw:.2f}</span>"
                   if (ob_bid is not None and pd.notna(ob_bid)) else "—")
        ask_str = (f"<span style='color:#C62828;font-weight:600'>{ob_ask:.1f}</span>"
                   f" <span style='color:#999;font-size:10px'>×{ob_ask_mw:.2f}</span>"
                   if (ob_ask is not None and pd.notna(ob_ask)) else "—")

        rows_html.append(
            f"<tr style='background:{bg};{row_border}'>"
            f"<td style='padding:3px 8px;color:#888;font-size:11px;font-family:monospace'>{day_short}</td>"
            f"<td style='padding:3px 8px'>{status_html}</td>"
            f"<td style='padding:3px 8px;font-family:monospace'>{period}</td>"
            f"<td style='padding:3px 8px;text-align:right;font-weight:600'>{price_str}</td>"
            f"<td style='padding:3px 8px;color:#888;font-size:11px'>{src}</td>"
            f"<td style='padding:3px 8px;text-align:right;font-size:11px'>{bid_str}</td>"
            f"<td style='padding:3px 8px;text-align:right;font-size:11px'>{ask_str}</td>"
            f"<td style='padding:3px 8px;text-align:right;color:#666'>{rng_str}</td>"
            f"<td style='padding:3px 8px;text-align:right;color:#666'>{vol_str}</td>"
            f"<td style='padding:3px 8px;font-size:11px'>{own_str}</td>"
            f"<td style='padding:3px 8px;font-size:11px'>{action}</td>"
            f"</tr>"
        )

    # Top 5 párov HTML
    pairs_html = []
    if not pairs:
        pairs_html.append(
            "<p style='color:#666;font-style:italic'>"
            "Žiadne profitable páry pri tvojich parametroch "
            f"(min_spread={min_spread} €/MWh, fee={grid_fee} €/MWh, cycle={cycle_cost} €/MWh, "
            f"eff_RT={eff_rt:.4f}). Skús znížiť min_spread alebo zvýšiť max_hold.</p>"
        )
    else:
        for i, p in enumerate(pairs, 1):
            kwh_in_top = p.get("kwh_into_batt", 0.0) or 0.0
            kwh_out_top = p.get("kwh_from_batt", 0.0) or 0.0
            soc_d_top = p.get("soc_delta_pct", 0.0) or 0.0
            pairs_html.append(
                f"<div style='background:#f0f8ff;border-left:4px solid #1F4E78;"
                f"padding:10px 14px;margin:6px 0;border-radius:6px'>"
                f"<b style='font-size:15px'>#{i} ⚡ Profit {p['profit_per_mwh']:+.1f} €/MWh "
                f"({p['profit_eur']:+.2f} € na {p['max_kwh']:.0f} kWh cyklu)</b><br>"
                f"<span style='color:#1F4E78'>💚 KÚP {p['buy_period']}</span> "
                f"@ <b>{p['buy_price']:.2f} €/MWh</b> ({p['buy_source']}) → "
                f"<span style='color:#C62828'>🔴 PREDAJ {p['sell_period']}</span> "
                f"@ <b>{p['sell_price']:.2f} €/MWh</b> ({p['sell_source']}) "
                f"<span style='color:#666;font-size:12px'>· hold {p['hold_hours']:.1f}h</span><br>"
                f"<span style='font-size:12px;color:#555'>"
                f"🔋 do batérie: <b style='color:#1F4E78'>{kwh_in_top:.0f} kWh</b> "
                f"(<b>+{soc_d_top:.1f}% SOC</b>) · "
                f"zo siete pri predaji: <b style='color:#C62828'>{kwh_out_top:.0f} kWh</b>"
                f"</span></div>"
            )

    nav = _nav("/vdt")
    now_str = _dt.datetime.now().strftime("%H:%M:%S")
    # Form (editovateľné parametre)
    form_html = (
        f"<form method='get' action='/vdt/board' "
        f"style='display:flex;flex-wrap:wrap;gap:8px;align-items:center;"
        f"background:#eef3f9;padding:10px;border-radius:8px;margin:10px 0;font-size:13px'>"
        f"<label>Deň: <input type='date' name='date' value='{date_obj.isoformat()}' "
        f"style='padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Batt kW: <input type='number' name='batt_kw' value='{batt_kw}' step='10' "
        f"style='width:70px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Batt kWh: <input type='number' name='batt_kwh' value='{batt_kwh}' step='10' "
        f"style='width:70px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>η nab: <input type='number' name='eff_c' value='{eff_c}' step='0.01' min='0.5' max='1.0' "
        f"style='width:50px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>η vyb: <input type='number' name='eff_d' value='{eff_d}' step='0.01' min='0.5' max='1.0' "
        f"style='width:50px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Poplatok €/MWh: <input type='number' name='grid_fee' value='{grid_fee}' step='1' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Cyklus €/MWh: <input type='number' name='cycle_cost' value='{cycle_cost}' step='0.5' "
        f"style='width:55px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Min spread €/MWh: <input type='number' name='min_spread' value='{min_spread}' step='1' "
        f"style='width:55px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Top N: <input type='number' name='top_n' value='{top_n}' step='5' min='5' max='200' "
        f"style='width:55px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label style='display:flex;align-items:center;gap:4px'>"
        f"<input type='checkbox' name='show_unprofitable' value='1'"
        + (" checked" if show_unprof_bool else "") +
        f"> aj stratové páry</label>"
        f"<button type='submit' style='background:#1F4E78;color:#fff;border:0;padding:5px 14px;"
        f"border-radius:6px;cursor:pointer'>Prepočítať</button>"
        f"</form>"
    )

    eff_rt_str = f"{eff_rt:.4f} ({(eff_rt * 100):.1f}%)"
    break_even = f"{(grid_fee + cycle_cost + min_spread) / eff_rt:.1f}"

    # Diag banner — koľko slotov, ceny, dni
    diag_color = "#28a745" if diag.get("with_price", 0) > 0 else "#C0392B"
    diag_msg = ""
    if diag.get("total_slots", 0) == 0:
        diag_msg = "Žiadny snapshot — fetch zlyhal alebo žiadne dni v rozsahu."
    elif diag.get("with_price", 0) == 0:
        diag_msg = (
            "Snapshot obsahuje sloty ale <b>žiadne ceny</b>. "
            "Možné príčiny: DAM clearing pre vybraný deň ešte nebol publikovaný "
            "(typicky D-1 ~12:00), alebo OKTE fetch zlyhal. "
            "Skús zmeniť deň na <b>dnes</b> alebo počkať na publikáciu DAM."
        )
    days_str = ", ".join(diag.get("days", []))
    ob_color = "#28a745" if "✓" in orderbook_status else "#888"
    diag_html = (
        f"<div style='background:#fff;border-left:4px solid {diag_color};"
        f"padding:10px 14px;margin:10px 0;border-radius:6px;font-size:13px'>"
        f"<b>📊 Snapshot diagnostika:</b> "
        f"{diag.get('total_slots',0)} slotov · "
        f"<span style='color:{diag_color};font-weight:600'>{diag.get('with_price',0)} s cenou</span> · "
        f"{diag.get('missing_price',0)} bez ceny · "
        f"IDM={diag.get('idm_slots',0)} · DAM={diag.get('dam_slots',0)} · "
        f"<span style='color:{ob_color}'>orderbook: {_html.escape(orderbook_status)}</span> · "
        f"dni: <code>{days_str}</code><br>"
        f"<span style='color:#1F4E78;font-size:12px'>"
        f"<b>Zdroj párov:</b> {pairs_source}</span>"
        + (f"<br><span style='color:#C0392B'>⚠ {diag_msg}</span>" if diag_msg else "")
        + f"</div>"
    )

    # "Všetky páry" sekcia (vrátane stratových)
    all_pairs_html = []
    if not pairs_all:
        all_pairs_html.append(
            "<p style='color:#666;font-style:italic'>"
            "Žiadne páry — chýbajú ceny v snapshote.</p>"
        )
    else:
        all_pairs_html.append(
            "<table style='width:100%;border-collapse:collapse;font-size:12px;border:1px solid #ddd'>"
            "<thead><tr style='background:#1F4E78;color:#fff'>"
            "<th style='padding:6px 8px;text-align:left'>filter</th>"
            "<th style='padding:6px 8px;text-align:left'>kúp slot</th>"
            "<th style='padding:6px 8px;text-align:right'>buy €/MWh</th>"
            "<th style='padding:6px 8px;text-align:left'>predaj slot</th>"
            "<th style='padding:6px 8px;text-align:right'>sell €/MWh</th>"
            "<th style='padding:6px 8px;text-align:right'>hold h</th>"
            "<th style='padding:6px 8px;text-align:right' title='Energia ktorá ide DO batérie po stratách nabíjania (eff_c)'>kWh nabité</th>"
            "<th style='padding:6px 8px;text-align:right' title='Energia ktorá ide ZO siete pri predaji (po stratách vybíjania, eff_d)'>kWh predané</th>"
            "<th style='padding:6px 8px;text-align:right' title='O koľko stúpne SOC počas nabíjania (% z celkovej kapacity batérie)'>Δ SOC %</th>"
            "<th style='padding:6px 8px;text-align:right'>profit €/MWh</th>"
            "<th style='padding:6px 8px;text-align:right'>€ na cyklus</th>"
            "</tr></thead><tbody>"
        )
        for p in pairs_all:
            ok = p.get("is_profitable", False)
            mark = ("<span style='background:#28a745;color:#fff;padding:1px 6px;"
                    "border-radius:8px;font-size:11px'>✓</span>" if ok else
                    "<span style='background:#999;color:#fff;padding:1px 6px;"
                    "border-radius:8px;font-size:11px'>✗</span>")
            pf_color = "#2E7D32" if p["profit_per_mwh"] >= min_spread else (
                "#C49000" if p["profit_per_mwh"] >= 0 else "#C62828")
            row_bg = "#ffffff" if ok else "#fafafa"
            kwh_in = p.get("kwh_into_batt", 0.0) or 0.0
            kwh_out = p.get("kwh_from_batt", 0.0) or 0.0
            soc_d = p.get("soc_delta_pct", 0.0) or 0.0
            all_pairs_html.append(
                f"<tr style='background:{row_bg}'>"
                f"<td style='padding:3px 8px'>{mark}</td>"
                f"<td style='padding:3px 8px;font-family:monospace;color:#1F4E78'>"
                f"{p.get('buy_date','')} {p['buy_period']}</td>"
                f"<td style='padding:3px 8px;text-align:right'>{p['buy_price']:.2f}</td>"
                f"<td style='padding:3px 8px;font-family:monospace;color:#C62828'>"
                f"{p.get('sell_date','')} {p['sell_period']}</td>"
                f"<td style='padding:3px 8px;text-align:right'>{p['sell_price']:.2f}</td>"
                f"<td style='padding:3px 8px;text-align:right;color:#666'>"
                f"{p['hold_hours']:.1f}</td>"
                f"<td style='padding:3px 8px;text-align:right;color:#1F4E78'>{kwh_in:.1f}</td>"
                f"<td style='padding:3px 8px;text-align:right;color:#C62828'>{kwh_out:.1f}</td>"
                f"<td style='padding:3px 8px;text-align:right;color:#666'>{soc_d:+.1f}%</td>"
                f"<td style='padding:3px 8px;text-align:right;color:{pf_color};"
                f"font-weight:600'>{p['profit_per_mwh']:+.1f}</td>"
                f"<td style='padding:3px 8px;text-align:right;color:{pf_color}'>"
                f"{p['profit_eur']:+.2f}</td>"
                f"</tr>"
            )
        all_pairs_html.append("</tbody></table>")

    body = (
        f"{nav}"
        f"<div style='max-width:1400px;margin:14px auto;padding:0 16px;"
        f"font-family:-apple-system,Segoe UI,Arial'>"
        f"<h1 style='color:#1F4E78;margin-bottom:6px'>⚡ VDT Arbitrage cockpit — {date_obj.isoformat()}</h1>"
        f"{_vdt_subnav('/vdt/board')}"

        # Status banner
        f"<div style='background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px 14px;"
        f"display:flex;gap:18px;align-items:center;flex-wrap:wrap;font-size:13px'>"
        f"<span><b>🟢 Trh:</b> {_html.escape(market_status)}</span>"
        f"<span><b>⏰ Server čas:</b> <code>{_html.escape(market_time)}</code></span>"
        f"<span><b>🔁 Page refresh:</b> 30s · <i>obnovené {now_str}</i></span>"
        f"<span style='margin-left:auto'><b>Break-even sell:</b> "
        f"<code>{break_even} €/MWh</code> @ buy 0 €/MWh (eff RT {eff_rt_str})</span>"
        f"</div>"

        # Form parametre
        f"{form_html}"

        # Diag banner (snapshot info)
        f"{diag_html}"

        # Top 5 arbitrage pairs
        f"<h2 style='color:#1F4E78;margin-top:18px'>⚡ Top 5 príležitostí (budúce sloty)</h2>"
        f"<div>{''.join(pairs_html)}</div>"

        # Všetky páry (vrátane stratových)
        f"<h2 style='color:#1F4E78;margin-top:18px'>📊 Všetky páry — top {top_n} "
        f"<span style='font-size:13px;font-weight:400;color:#666'>"
        f"(zoradené podľa profit/MWh, vrátane stratových; ✓ = nad prahom {min_spread:.0f} €/MWh)</span></h2>"
        f"<div>{''.join(all_pairs_html)}</div>"

        # Tabuľka 96 slotov
        f"<h2 style='color:#1F4E78;margin-top:18px'>📋 Tabuľka slotov (96 × 15-min)</h2>"
        f"<p style='color:#666;font-size:12px;margin:4px 0'>"
        f"<span style='background:#28a745;color:#fff;padding:1px 6px;border-radius:8px;font-size:11px'>● LIVE</span> = práve teraz · "
        f"<span style='background:#999;color:#fff;padding:1px 6px;border-radius:8px;font-size:11px'>Uplynulo</span> · "
        f"<span style='background:#1F4E78;color:#fff;padding:1px 6px;border-radius:8px;font-size:11px'>Budúce</span> · "
        f"<span style='background:#d4edda;padding:2px 6px;border-radius:4px'>zelená</span> = lacné (Q25↓, kúpiť) · "
        f"<span style='background:#f8d7da;padding:2px 6px;border-radius:4px'>červená</span> = drahé (Q75↑, predať) · "
        f"<span style='background:#fff8e1;padding:2px 6px;border-radius:4px'>žltá</span> = priemer"
        f"</p>"
        f"<table style='width:100%;border-collapse:collapse;font-size:12px;border:1px solid #ddd'>"
        f"<thead><tr style='background:#1F4E78;color:#fff'>"
        f"<th style='padding:6px 8px;text-align:left'>deň</th>"
        f"<th style='padding:6px 8px;text-align:left'>stav</th>"
        f"<th style='padding:6px 8px;text-align:left'>perióda</th>"
        f"<th style='padding:6px 8px;text-align:right'>cena €/MWh</th>"
        f"<th style='padding:6px 8px;text-align:left'>zdroj</th>"
        f"<th style='padding:6px 8px;text-align:right' title='Najvyššia kúpna ponuka (live) — predáš za toto'>best bid (×MW)</th>"
        f"<th style='padding:6px 8px;text-align:right' title='Najnižšia predajná ponuka (live) — kúpiš za toto'>best ask (×MW)</th>"
        f"<th style='padding:6px 8px;text-align:right'>min–max</th>"
        f"<th style='padding:6px 8px;text-align:right'>obj. MWh</th>"
        f"<th style='padding:6px 8px;text-align:left'>moje obchody</th>"
        f"<th style='padding:6px 8px;text-align:left'>akcia</th>"
        f"</tr></thead><tbody>"
        f"{''.join(rows_html)}"
        f"</tbody></table>"

        f"<p style='color:#888;font-size:11px;margin-top:14px'>"
        f"<i>Read-only nástroj — žiadne objednávky sa neposielajú. "
        f"Ceny pre uplynulé sloty: skutočné IDM clearing. "
        f"Pre budúce sloty: DAM baseline z D-1 aukcie. "
        f"Profit/cyklus = sell × {eff_rt:.4f} − buy − {grid_fee} − {cycle_cost} − {min_spread} (min spread).</i>"
        f"</p>"

        f"</div>"
    )
    # Auto-refresh 30s (cez meta refresh v head_extra)
    return render_legacy_body(None, "VDT Arbitrage", body,
                                head_extra="<meta http-equiv='refresh' content='30'>")


def _vdt_subnav(active: str = "") -> str:
    """Sub-navigácia pre VDT stránky — záložky na rýchle prepínanie."""
    items = [
        ("/vdt", "💹 Prehľad"),
        ("/vdt/d1", "📅 D-1 plán"),
        ("/vdt/board", "⚡ Arbitrage cockpit"),
        ("/vdt/live_advisor", "🎯 Live advisor"),
        ("/vdt/simulator", "📐 Simulátor"),
        ("/vdt/backtest", "📜 Backtest"),
        ("/vdt/zco_backtest", "💡 ZCO backtest"),
        ("/vdt/test_orderbook", "🔧 Debug"),
    ]
    links = []
    for href, lab in items:
        is_active = (href == active)
        style = (
            "background:#1F4E78;color:#fff;font-weight:600"
            if is_active else
            "background:#fff;color:#1F4E78;border:1px solid #1F4E78"
        )
        links.append(
            f'<a href="{href}" style="padding:6px 12px;border-radius:6px;'
            f'text-decoration:none;font-size:13px;{style}">{lab}</a>'
        )
    return (
        f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin:0 0 14px;'
        f'padding:8px;background:#eef3f9;border-radius:8px">{"".join(links)}</div>'
    )


def _render_orderbook_section(_vdt) -> str:
    """Render orderbook section — top of book + hĺbka per produkt.
    Vykreslí 2 stĺpce (hodinové + 15-min) s top-3 bids/asks per produkt."""
    import html as _html
    try:
        res = _vdt.get_orderbook()
    except Exception as e:
        return (f"<h2>📖 Aktuálne ponuky (orderbook)</h2>"
                 f"<p style='background:#ffeaea;padding:10px;border-radius:6px;color:#C0392B'>"
                 f"⚠ Volanie zlyhalo: {_html.escape(str(e))}</p>")
    if not res.get("ok"):
        err = res.get("error", "?")
        # Special-case: chýba sender_eic
        if "sender_eic" in str(err):
            return (f"<h2>📖 Aktuálne ponuky (orderbook)</h2>"
                     f"<div style='background:#fff3cd;padding:12px;border-radius:8px;border-left:4px solid #C49000'>"
                     f"<b>⚙ Doplň svoj EIC kód:</b><br>"
                     f"1. Otvor súbor <code>out/sk/okte_vdt_config.json</code><br>"
                     f"2. Doplň pole <code>\"sender_eic\": \"24X-FUERGY----A\"</code> (16 znakov, "
                     f"presný kód nájdeš v OKTE portáli → Účastník trhu → Detail)<br>"
                     f"3. Reštartuj appku (<code>./restart_sluzby.sh 8000</code>) a refresh stránku</div>")
        raw = res.get("raw_xml_head") or ""
        return (f"<h2>📖 Aktuálne ponuky (orderbook)</h2>"
                 f"<div style='background:#ffeaea;padding:10px;border-radius:6px;color:#C0392B;font-size:13px'>"
                 f"<b>⚠ SOAP volanie zlyhalo</b><br>"
                 f"<code>{_html.escape(str(err)[:300])}</code>"
                 + (f"<details style='margin-top:8px'><summary>Raw XML</summary>"
                    f"<pre style='font-size:10px;max-height:300px;overflow:auto'>"
                    f"{_html.escape(raw)}</pre></details>" if raw else "")
                 + f"</div>")

    tob = res.get("top_of_book") or {}
    n_h = sum(1 for _ in (tob.get("hourly") or {}))
    n_q = sum(1 for _ in (tob.get("quarterly") or {}))
    parsed = res.get("stats", {}).get("trades_parsed", 0)

    def _period_sort_key(p):
        """Sort key pre period string '11:45-12:00' alebo '11-12' (legacy) alebo '48-49'."""
        try:
            first = p.split("-")[0]
            if ":" in first:
                h, m = first.split(":")
                return int(h) * 60 + int(m)
            return int(first)
        except Exception:
            return 9999

    def _fmt_mw(v):
        try:
            return f"{float(v):.2f} MW"
        except Exception:
            return "—"

    def _fmt_eur(v):
        try:
            return f"{float(v):.2f}"
        except Exception:
            return "—"

    def _render_table(title, bucket_name, top_bucket):
        if not top_bucket:
            return (f"<div style='flex:1;background:#f8f9fa;padding:12px;border-radius:8px'>"
                     f"<h3>{title}</h3><p style='color:#666;font-size:13px'>"
                     f"Žiadne aktuálne ponuky.</p></div>")
        rows = []
        for period in sorted(top_bucket.keys(), key=_period_sort_key):
            b = top_bucket[period]
            bid = b.get("best_bid") or {}
            ask = b.get("best_ask") or {}
            spread = b.get("spread_eur")
            spread_str = f"{spread:+.2f}" if spread is not None else "—"
            spread_color = "#2E7D32" if (spread is not None and spread > 0) else "#C49000"
            bid_mw_html = (f"<td style='padding:4px 8px;text-align:right;color:#1F4E78'>{_fmt_mw(bid.get('mw'))}</td>"
                           if bid else "<td style='padding:4px 8px;text-align:right;color:#999'>—</td>")
            ask_mw_html = (f"<td style='padding:4px 8px;text-align:right;color:#C62828'>{_fmt_mw(ask.get('mw'))}</td>"
                           if ask else "<td style='padding:4px 8px;text-align:right;color:#999'>—</td>")
            rows.append(
                f"<tr>"
                f"<td style='padding:4px 8px;font-weight:600;font-family:monospace;font-size:11px'>{period}</td>"
                f"{bid_mw_html}"
                f"<td style='padding:4px 8px;text-align:right;font-weight:600'>"
                f"{_fmt_eur(bid.get('eur')) if bid else '—'} €</td>"
                f"<td style='padding:4px 8px;text-align:right;font-weight:600'>"
                f"{_fmt_eur(ask.get('eur')) if ask else '—'} €</td>"
                f"{ask_mw_html}"
                f"<td style='padding:4px 8px;text-align:right;color:{spread_color};font-size:11px'>"
                f"{spread_str}</td>"
                f"<td style='padding:4px 8px;text-align:right;color:#666;font-size:11px'>"
                f"{b.get('n_bids',0)}/{b.get('n_asks',0)}</td>"
                f"</tr>"
            )
        return (
            f"<div style='flex:1;background:#fafafa;padding:12px;border-radius:8px;border:1px solid #e0e0e0'>"
            f"<h3 style='margin:0 0 8px'>{title}</h3>"
            f"<table style='width:100%;border-collapse:collapse;font-size:12px'>"
            f"<thead><tr style='background:#1F4E78;color:#fff'>"
            f"<th style='padding:6px;text-align:left'>perióda</th>"
            f"<th style='padding:6px;text-align:right'>bid MW</th>"
            f"<th style='padding:6px;text-align:right'>bid €</th>"
            f"<th style='padding:6px;text-align:right'>ask €</th>"
            f"<th style='padding:6px;text-align:right'>ask MW</th>"
            f"<th style='padding:6px;text-align:right'>spread</th>"
            f"<th style='padding:6px;text-align:right'>n B/A</th>"
            f"</tr></thead><tbody>"
            f"{''.join(rows)}"
            f"</tbody></table></div>"
        )

    return (
        f"<h2>📖 Aktuálne ponuky (orderbook) · "
        f"<span style='font-size:13px;color:#2E7D32'>✓ {parsed} ponúk · "
        f"{n_h} hod produktov · {n_q} 15-min produktov</span></h2>"
        f"<p style='color:#666;font-size:12px;margin:4px 0'>"
        f"Top of book: najlepšia kúpna (bid) a predajná (ask) cena per produkt. "
        f"Spread = ask − bid. Posledný stĺpec = počet bids/asks v knihe.</p>"
        f"<div style='display:flex;gap:14px;margin:10px 0'>"
        f"{_render_table('⏰ Hodinové produkty', 'hourly', tob.get('hourly') or {})}"
        f"{_render_table('🕒 15-min produkty', 'quarterly', tob.get('quarterly') or {})}"
        f"</div>"
    )


@app.get("/vdt/raw_orderbook")
def vdt_raw_orderbook(duration: int = 0):
    """Stiahne raw orderbook SOAP response ako .xml súbor.
    Použitie: http://localhost:8000/vdt/raw_orderbook?duration=15

    Vracia application/xml — prehliadač môže ponúknuť stiahnutie ako súbor.
    """
    from fastapi.responses import Response
    import datetime as _dt
    try:
        import okte_vdt as _vdt
    except Exception as e:
        return Response(f"Modul nedostupný: {e}".encode("utf-8"),
                        media_type="text/plain", status_code=500)
    dur_arg = duration if duration in (15, 60) else None
    res = _vdt.get_orderbook(delivery_duration=dur_arg)
    debug = res.get("debug", {}) or {}
    xml_body = debug.get("response_body", "")
    if not xml_body:
        return Response(b"<empty/>", media_type="application/xml")
    filename = f"okte_orderbook_{duration or 'all'}_{int(_dt.datetime.now().timestamp())}.xml"
    return Response(
        xml_body.encode("utf-8"),
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename=\"{filename}\"'},
    )


@app.get("/vdt/wsdl", response_class=HTMLResponse)
def vdt_wsdl_page():
    """Stiahne WSDL zo SOAP endpointu a ukáže zoznam operations + Action URIs."""
    import html as _html
    try:
        import okte_vdt as _vdt
    except Exception as e:
        return f"<p style='color:#C0392B'>Modul nedostupný: {e}</p>"

    res = _vdt.fetch_wsdl()
    nav = _nav("/vdt")
    body = f"{nav}<div style='max-width:1400px;margin:14px auto;padding:0 16px;font-family:-apple-system,Segoe UI,Arial'>"
    body += f"<h1 style='color:#1F4E78'>📋 OKTE IdmOrderBook WSDL</h1>"
    body += _vdt_subnav("/vdt/test_orderbook")
    body += f"<p><b>URL:</b> <code>{_html.escape(str(res.get('url','?')))}</code> · "
    body += f"<b>Status:</b> {res.get('status', '?')} · <b>Size:</b> {res.get('size_b', 0)} B</p>"

    if not res.get("ok"):
        body += f"<div style='background:#ffe7e7;padding:12px;border-radius:6px;color:#C0392B'>"
        body += f"<b>WSDL fetch zlyhal:</b> {_html.escape(str(res.get('error','?')))}</div>"
    else:
        wsdl = res.get("content", "")
        ops = _vdt.parse_wsdl_operations(wsdl)
        body += f"<h2 style='color:#1F4E78'>🔧 Operations ({len(ops)})</h2>"
        if not ops:
            body += "<p style='color:#666'>Žiadne operations nenájdené v WSDL.</p>"
        else:
            body += "<table style='border-collapse:collapse;width:100%;font-size:13px'>"
            body += "<thead><tr style='background:#1F4E78;color:#fff'>"
            body += "<th style='padding:6px 10px;text-align:left'>Operation</th>"
            body += "<th style='padding:6px 10px;text-align:left'>Action URI</th></tr></thead><tbody>"
            for op in ops:
                body += f"<tr><td style='padding:4px 10px;font-weight:600'>{_html.escape(op['name'])}</td>"
                body += f"<td style='padding:4px 10px;font-family:monospace;font-size:11px'>{_html.escape(op['action'])}</td></tr>"
            body += "</tbody></table>"

        body += "<h2 style='color:#1F4E78;margin-top:18px'>📄 Raw WSDL</h2>"
        body += f"<details><summary style='cursor:pointer'>Zobraziť celý WSDL ({len(wsdl)} znakov)</summary>"
        body += f"<pre style='background:#f0f0f0;padding:10px;font-size:10px;max-height:600px;overflow:auto'>{_html.escape(wsdl)}</pre></details>"
    body += "</div>"
    return render_legacy_body(None, "VDT WSDL", body)


@app.get("/vdt/zco_backtest", response_class=HTMLResponse)
def vdt_zco_backtest_page(date_from: str = "", date_to: str = "",
                            profile: str = "", grid_fee: float = 22.0):
    """ZCO backtest — keby som zámerne obchodoval cez ZCO namiesto VDT.

    Pre každý deň v rozsahu zoberie DAM nominácie z plánu, ZCO + VDT ceny z historian
    a porovná: splniť DAM cez VDT vs nesplniť cez ZCO. Sumarizuje úsporu.
    """
    import html as _html
    import datetime as _dt
    try:
        import zco_backtest as _zbt
        import plan_store as _ps
    except ImportError as e:
        return f"<p style='color:#C0392B'>Modul nedostupný: {e}</p>"

    # Default range = posledných 14 dní (do včera)
    today = _dt.date.today()
    if not date_from:
        from_d = today - _dt.timedelta(days=14)
    else:
        try:
            from_d = _dt.datetime.strptime(date_from, "%Y-%m-%d").date()
        except Exception:
            from_d = today - _dt.timedelta(days=14)
    if not date_to:
        to_d = today - _dt.timedelta(days=1)
    else:
        try:
            to_d = _dt.datetime.strptime(date_to, "%Y-%m-%d").date()
        except Exception:
            to_d = today - _dt.timedelta(days=1)
    if to_d < from_d:
        from_d, to_d = to_d, from_d
    if (to_d - from_d).days > 90:
        to_d = from_d + _dt.timedelta(days=90)

    # Profile resolve
    try:
        active_profile = _ps.resolve_profile() or "default"
    except Exception:
        active_profile = "default"
    if not profile:
        profile = active_profile

    nav = _nav("/vdt")
    body = (
        f"{nav}"
        f"<div style='max-width:1500px;margin:14px auto;padding:0 16px;"
        f"font-family:-apple-system,Segoe UI,Arial'>"
        f"<h1 style='color:#1F4E78;margin-bottom:4px'>💡 ZCO Backtest — keby som obchodoval cez ZCO</h1>"
        f"{_vdt_subnav('/vdt/zco_backtest')}"
        f"<p style='color:#666;margin:0 0 12px;font-size:13px'>"
        f"Pre minulé dni vyhodnotí: <b>splniť DAM nomináciu cez VDT</b> "
        f"vs <b>zámerne nesplniť a zaplatiť ZCO penalty</b>. Vyberá optimálne "
        f"per slot a sumarizuje úsporu oproti čistému VDT prístupu.</p>"
    )

    # Form
    body += (
        f"<form method='get' action='/vdt/zco_backtest' "
        f"style='background:#eef3f9;padding:10px;border-radius:8px;margin:8px 0;"
        f"display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-size:13px'>"
        f"<label>Od: <input type='date' name='date_from' value='{from_d.isoformat()}' "
        f"style='padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Do: <input type='date' name='date_to' value='{to_d.isoformat()}' "
        f"style='padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Profile: <input type='text' name='profile' value='{_html.escape(profile)}' "
        f"style='width:140px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<label>Fee €/MWh: <input type='number' name='grid_fee' value='{grid_fee}' step='1' "
        f"style='width:60px;padding:4px;border:1px solid #ccc;border-radius:4px'></label>"
        f"<button type='submit' style='background:#E65100;color:#fff;border:0;"
        f"padding:8px 14px;border-radius:6px;cursor:pointer;font-weight:600'>"
        f"▶ Spustiť backtest</button>"
        f"</form>"
    )

    # SK profile info + rebuild
    try:
        import deviation_stats as _ds, os as _os, json as _json
        prof_path = _ds.PROFILE_PATH_SK
        if _os.path.exists(prof_path):
            with open(prof_path, "r") as _f:
                _prof = _json.load(_f)
            _n_days = _prof.get("n_days", 0)
            _span = _prof.get("span", ["?", "?"])
            _n_cells = len(_prof.get("cells", []))
            _by_pv = "áno" if _prof.get("by_pv") else "nie"
            _by_wd = "áno" if _prof.get("by_weekday") else "nie"
            _info = (f"profil <b>{_n_days} dní</b> ({_span[0]} → {_span[1]}), "
                     f"{_n_cells} buniek, PV-bucket: {_by_pv}, weekday-split: {_by_wd}")
            _info_color = "#1F4E78"
        else:
            _info = "profil neexistuje — spusti rebuild aby si nahral SK historian dáta"
            _info_color = "#C0392B"
    except Exception as _e:
        _info = f"chyba pri čítaní profilu: {_e}"
        _info_color = "#C0392B"

    body += (
        f"<div style='background:#f8f9fa;border-left:4px solid {_info_color};padding:10px 14px;"
        f"border-radius:6px;margin:8px 0;font-size:12px;display:flex;justify-content:space-between;"
        f"align-items:center;gap:10px'>"
        f"<div><b>SK ZCO deviation profile:</b> {_info}<br>"
        f"<span style='color:#888;font-size:11px'>Cron: nedeľa 02:00 (auto). "
        f"Pre manuálny rebuild klikni vpravo.</span></div>"
        f"<form method='post' action='/vdt/zco_profile_rebuild' style='margin:0'>"
        f"<button type='submit' style='background:#1F4E78;color:#fff;border:0;"
        f"padding:8px 14px;border-radius:6px;cursor:pointer;font-weight:600;font-size:12px'>"
        f"🔄 Rebuild SK profil teraz</button></form></div>"
    )

    # Spusti backtest
    try:
        res = _zbt.run_backtest(from_d, to_d, profile=profile, grid_fee=grid_fee)
    except Exception as e:
        body += f"<p style='color:#C0392B'>Backtest zlyhal: {_html.escape(str(e))}</p></div>"
        return render_legacy_body(None, "ZCO Backtest", body)

    if not res.get("ok"):
        body += (
            f"<div style='background:#fff3cd;border-left:4px solid #856404;padding:14px;"
            f"border-radius:8px;margin:10px 0;font-size:13px'>"
            f"<b>⚠ {_html.escape(str(res.get('error','?')))}</b><br>"
            f"<span style='color:#666;font-size:12px'>"
            f"Potrebujem aspoň jeden deň s plánom (z /dentrh alebo /plan) + ZCO/VDT cenami v historian CSV."
            f"</span></div></div>"
        )
        return render_legacy_body(None, "ZCO Backtest", body)

    summary = res["summary"]
    days = res["days"]

    # Summary cards
    total_sav = float(summary.get("total_savings_eur", 0))
    sav_color = "#28a745" if total_sav > 0 else "#C0392B"
    body += (
        f"<div style='display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0'>"
        f"<div style='background:#fff;border-left:4px solid {sav_color};padding:14px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Total úspora cez ZCO</div>"
        f"<div style='font-size:24px;font-weight:700;color:{sav_color}'>"
        f"+{total_sav:.2f} €</div>"
        f"<div style='font-size:11px;color:#666'>"
        f"priemer {summary.get('avg_savings_per_day_eur',0):+.2f} €/deň</div>"
        f"</div>"
        f"<div style='background:#fff;border-left:4px solid #1F4E78;padding:14px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>Dni s ZCO výhrou</div>"
        f"<div style='font-size:24px;font-weight:700;color:#1F4E78'>"
        f"{summary.get('n_days_with_zco_wins',0)} / {summary.get('total_days',0)}</div>"
        f"<div style='font-size:11px;color:#666'>"
        f"{summary.get('pct_days_with_wins',0):.0f}% dní malo aspoň 1 ZCO win</div>"
        f"</div>"
        f"<div style='background:#fff;border-left:4px solid #E65100;padding:14px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>DAM commit objem</div>"
        f"<div style='font-size:24px;font-weight:700;color:#E65100'>"
        f"{summary.get('total_dam_kwh',0):.0f} kWh</div>"
        f"<div style='font-size:11px;color:#666'>cez všetky dni</div>"
        f"</div>"
        f"<div style='background:#fff;border-left:4px solid #C49000;padding:14px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#666;text-transform:uppercase'>VDT vs Optimal</div>"
        f"<div style='font-size:14px;font-weight:600;color:#C49000;margin-top:4px'>"
        f"len VDT: {summary.get('total_vdt_eur',0):+.2f} €<br>"
        f"optimal (max ZCO/VDT): {summary.get('total_optimal_eur',0):+.2f} €</div>"
        f"</div>"
        f"</div>"
    )

    # Tabuľka dni
    body += (
        "<h2 style='color:#1F4E78;margin-top:18px'>📋 Po dňoch</h2>"
        "<table style='width:100%;border-collapse:collapse;font-size:12px;border:1px solid #ddd'>"
        "<thead><tr style='background:#1F4E78;color:#fff'>"
        "<th style='padding:6px 8px;text-align:left'>deň</th>"
        "<th style='padding:6px 8px;text-align:right'>DAM kWh</th>"
        "<th style='padding:6px 8px;text-align:right'>ZCO wins / sloty</th>"
        "<th style='padding:6px 8px;text-align:right'>VDT profit €</th>"
        "<th style='padding:6px 8px;text-align:right'>ZCO profit €</th>"
        "<th style='padding:6px 8px;text-align:right'>Optimal €</th>"
        "<th style='padding:6px 8px;text-align:right'>Úspora €</th>"
        "</tr></thead><tbody>"
    )
    for d in days:
        sav = float(d.get("savings_eur", 0))
        sav_clr = "#28a745" if sav > 0.01 else ("#C0392B" if sav < -0.01 else "#666")
        wins_pct = (d["n_zco_wins"] / max(1, d["n_slots"])) * 100
        body += (
            f"<tr>"
            f"<td style='padding:3px 8px;font-family:monospace'>{d['date']}</td>"
            f"<td style='padding:3px 8px;text-align:right'>{d['dam_kwh_commit']:.0f}</td>"
            f"<td style='padding:3px 8px;text-align:right'>"
            f"<b>{d['n_zco_wins']}</b> / {d['n_slots']} "
            f"<span style='color:#888'>({wins_pct:.0f}%)</span></td>"
            f"<td style='padding:3px 8px;text-align:right'>{d['profit_vdt_eur']:+.2f}</td>"
            f"<td style='padding:3px 8px;text-align:right;color:#E65100'>{d['profit_zco_eur']:+.2f}</td>"
            f"<td style='padding:3px 8px;text-align:right;font-weight:600'>{d['optimal_eur']:+.2f}</td>"
            f"<td style='padding:3px 8px;text-align:right;font-weight:700;color:{sav_clr}'>"
            f"+{sav:.2f}</td>"
            f"</tr>"
        )
    body += "</tbody></table>"

    body += (
        f"<p style='color:#888;font-size:11px;margin-top:14px'>"
        f"<i>Backtest využíva skutočné ZCO + VDT clearing ceny z historian CSV. "
        f"DAM nominácie pochádzajú z uložených plánov (cascade <code>dentrh → plan</code>). "
        f"Toto je <b>kontrafaktuálna simulácia</b> — neukazuje čo sme reálne získali, ale "
        f"<b>aký by bol potenciál</b> z aktívneho riadenia ZCO odchýlky. Predpokladá perfektnú "
        f"informáciu o ZCO ex-post — v reálnom čase máme len predikciu.</i></p>"
        f"</div>"
    )

    return render_legacy_body(None, "ZCO Backtest", body)


@app.post("/vdt/zco_profile_rebuild", response_class=HTMLResponse)
def vdt_zco_profile_rebuild():
    """Manuálny rebuild SK ZCO deviation profile zo historian CSV.

    Volá rovnaký kód ako scheduler job (nedeľa 02:00). Po dokončení
    presmeruje späť na /vdt/zco_backtest s diag bannerom.
    """
    import html as _html
    try:
        import deviation_stats as _ds
    except Exception as e:
        return HTMLResponse(
            f"<p style='color:#C0392B'>deviation_stats modul nedostupný: {_html.escape(str(e))}</p>"
            f"<p><a href='/vdt/zco_backtest'>← späť</a></p>", status_code=500
        )

    msg = ""
    msg_color = "#28a745"
    try:
        prof = _ds.build_profile_sk(by_pv=False, by_weekday=True)
        _ds.save_profile(prof, _ds.PROFILE_PATH_SK)
        n_days = prof.get("n_days", 0)
        span = prof.get("span", ["?", "?"])
        n_cells = len(prof.get("cells", []))
        msg = (f"✓ Rebuild OK · <b>{n_days} dní</b> ({span[0]} → {span[1]}) · "
               f"{n_cells} buniek · uložené do <code>{_ds.PROFILE_PATH_SK}</code>")
    except Exception as e:
        msg = f"✗ Rebuild zlyhal: {_html.escape(str(e))}"
        msg_color = "#C0392B"

    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta http-equiv='refresh' content='3;url=/vdt/zco_backtest'>"
        f"</head><body style='font-family:-apple-system,Segoe UI,Arial;padding:30px;max-width:700px;margin:0 auto'>"
        f"<h2 style='color:#1F4E78'>🔄 SK ZCO profile rebuild</h2>"
        f"<div style='background:#fff;border-left:4px solid {msg_color};padding:14px;"
        f"border-radius:8px;font-size:14px'>{msg}</div>"
        f"<p style='color:#666;font-size:12px;margin-top:14px'>"
        f"Presmerujem späť na backtest za 3 sekundy… "
        f"<a href='/vdt/zco_backtest'>kliknúť ručne</a>"
        f"</p></body></html>"
    )


@app.get("/auto_control", response_class=HTMLResponse)
def auto_control_page():
    """Fáza A.5 paper trading dashboard — SIMULATION mode only.

    Real-write do Bender je HARD-BLOCKED. Ukáže čo by systém robil keby
    bol povolený, pre overenie správania pred prepnutím na real mode.
    """
    import html as _html
    try:
        import auto_control as _ac
        import profiles as _pr
        import market as _mk
    except ImportError as e:
        return f"<p style='color:#C0392B'>Modul nedostupný: {e}</p>"

    mk_code = _mk.get_active_market()
    real_unlocked = _ac.is_real_unlocked()
    kill_active = _ac.kill_switch_active()

    nav = _nav("/auto_control")
    body = (
        f"{nav}"
        f"<div style='max-width:1500px;margin:14px auto;padding:0 16px;"
        f"font-family:-apple-system,Segoe UI,Arial'>"
        f"<h1 style='color:#1F4E78;margin-bottom:4px'>🤖 Paper trading (Fáza A.5 simulation)</h1>"
        f"<p style='color:#666;margin:0 0 12px;font-size:13px'>"
        f"Systém každú 15-minútovku prečíta D-1 plán pre aktuálny slot, "
        f"vyráta plánovaný batt setpoint a <b>zaloguje rozhodnutie do CSV</b>. "
        f"Žiadny zápis do Bender. Slúži na verifikáciu správania pred prepnutím na real mode.</p>"
    )

    # SIMULATION banner — vždy hore, jasne viditeľný
    mode_color = "#28a745" if not real_unlocked else "#C62828"
    mode_label = "🟢 SIMULATION MODE" if not real_unlocked else "🔴 REAL MODE ODBLOKOVANÝ"
    mode_desc = ("Žiadne zápisy do Bender. Všetky setpointy len v logu."
                 if not real_unlocked
                 else "POZOR — auto_control_unlock.json existuje. Zápisy do Bender sú povolené!")
    body += (
        f"<div style='background:{mode_color};color:#fff;padding:14px 18px;"
        f"border-radius:8px;margin:10px 0;font-size:15px'>"
        f"<b style='font-size:18px'>{mode_label}</b><br>"
        f"<span style='font-size:13px'>{mode_desc}</span>"
        f"</div>"
    )

    # Kill switch
    if kill_active:
        body += (
            f"<div style='background:#fff3cd;border-left:4px solid #856404;"
            f"padding:12px;border-radius:6px;margin:10px 0;font-size:13px'>"
            f"<b>⏸ Kill switch aktívny</b> — scheduler job preskakuje každý cyklus.<br>"
            f"<form method='post' action='/auto_control/kill_switch' style='display:inline;margin-top:8px'>"
            f"<input type='hidden' name='action' value='clear'>"
            f"<button type='submit' style='background:#28a745;color:#fff;border:0;"
            f"padding:6px 12px;border-radius:6px;cursor:pointer;margin-top:6px'>"
            f"▶ Reštartovať scheduler job</button></form></div>"
        )
    else:
        body += (
            f"<form method='post' action='/auto_control/kill_switch' style='margin:10px 0'>"
            f"<input type='hidden' name='action' value='set'>"
            f"<button type='submit' style='background:#C62828;color:#fff;border:0;"
            f"padding:8px 14px;border-radius:6px;cursor:pointer;font-weight:600;font-size:13px'>"
            f"⏸ Aktivovať kill switch (zastavit scheduler)</button></form>"
        )

    # Aktuálne setpointy pre všetky profily
    try:
        profs = _pr.list_profiles() or []
    except Exception:
        profs = []
    enabled_set = _ac.get_enabled_profiles()
    n_enabled = len(enabled_set)
    body += (
        f"<h2 style='color:#1F4E78;margin-top:18px;font-size:18px'>"
        f"📊 Aktuálny plán pre teraz · trh: <b>{mk_code.upper()}</b> · "
        f"paper trading na <b>{n_enabled}</b> z {len(profs)} profilov</h2>"
        f"<p style='color:#666;font-size:12px;margin:0 0 8px'>"
        f"Zapni paper trading per profil — scheduler iteruje len zapnuté. "
        f"Default: všetko vypnuté.</p>"
        f"<table style='width:100%;border-collapse:collapse;font-size:12px;border:1px solid #ddd'>"
        f"<thead><tr style='background:#1F4E78;color:#fff'>"
        f"<th style='padding:6px 8px;text-align:center'>obchodujem</th>"
        f"<th style='padding:6px 8px;text-align:left'>profil</th>"
        f"<th style='padding:6px 8px;text-align:center'>slot</th>"
        f"<th style='padding:6px 8px;text-align:right'>plán setpoint</th>"
        f"<th style='padding:6px 8px;text-align:right'>SOC teraz</th>"
        f"<th style='padding:6px 8px;text-align:right'>batt max kW</th>"
        f"<th style='padding:6px 8px;text-align:left'>plan kind</th>"
        f"<th style='padding:6px 8px;text-align:left'>stav plánu</th>"
        f"</tr></thead><tbody>"
    )
    if not profs:
        body += "<tr><td colspan='8' style='padding:8px;color:#888'>Žiadne profily v tomto trhu.</td></tr>"
    for p in profs:
        name = p if isinstance(p, str) else (p.get("name", "?") if isinstance(p, dict) else "?")
        sp = _ac.compute_setpoint_for_now(profile=name)
        if sp is None:
            continue
        is_on = name in enabled_set
        setp_kw = sp.get("setpoint_kw")
        setp_str = f"{setp_kw:+.1f} kW" if setp_kw is not None else "—"
        setp_clr = "#28a745" if (setp_kw is not None and setp_kw > 0) else (
            "#C62828" if (setp_kw is not None and setp_kw < 0) else "#666")
        soc_pct = sp.get("soc_pct")
        soc_src = sp.get("soc_source", "")
        # Krátka skratka zdroja
        soc_src_short = ""
        if soc_src == "plan_soc_pct":
            soc_src_short = " <span style='color:#999;font-size:9px'>(plán)</span>"
        elif soc_src == "realio_db":
            soc_src_short = " <span style='color:#999;font-size:9px'>(real)</span>"
        elif soc_src == "realio_db_fallback":
            soc_src_short = " <span style='color:#E65100;font-size:9px'>(?)</span>"
        soc_str = (f"{soc_pct:.1f}%{soc_src_short}" if soc_pct is not None
                   else "—")
        reason = sp.get("reason", "?")
        # Neutrálna farba pre stav plánu — nezamieňať s ON/OFF toggle
        reason_clr = "#666" if reason == "ok" else "#E65100"
        toggle_action = "off" if is_on else "on"
        toggle_label = "🟢 ON" if is_on else "⚪ OFF"
        toggle_bg = "#28a745" if is_on else "#aaa"
        toggle_html = (
            f"<form method='post' action='/auto_control/toggle_profile' style='margin:0;display:inline'>"
            f"<input type='hidden' name='name' value='{_html.escape(name)}'>"
            f"<input type='hidden' name='action' value='{toggle_action}'>"
            f"<button type='submit' style='background:{toggle_bg};color:#fff;border:0;"
            f"padding:4px 10px;border-radius:12px;cursor:pointer;font-size:11px;"
            f"font-weight:600;min-width:60px'>{toggle_label}</button></form>"
        )
        row_bg = "background:#f0fff0;" if is_on else ""
        body += (
            f"<tr style='border-bottom:1px solid #eee;{row_bg}'>"
            f"<td style='padding:4px 8px;text-align:center'>{toggle_html}</td>"
            f"<td style='padding:4px 8px'><b>{_html.escape(name)}</b></td>"
            f"<td style='padding:4px 8px;text-align:center;font-family:monospace'>"
            f"{sp.get('slot_label', '?')}</td>"
            f"<td style='padding:4px 8px;text-align:right;color:{setp_clr};font-weight:600'>"
            f"{setp_str}</td>"
            f"<td style='padding:4px 8px;text-align:right'>{soc_str}</td>"
            f"<td style='padding:4px 8px;text-align:right'>{sp.get('batt_kw_max', 0):.0f}</td>"
            f"<td style='padding:4px 8px;color:#666'>{sp.get('plan_kind', '—')}/{sp.get('plan_step_min', '?')}min</td>"
            f"<td style='padding:4px 8px;color:{reason_clr};font-size:11px'>{reason}</td>"
            f"</tr>"
        )
    body += "</tbody></table>"
    # Bulk akcie
    body += (
        f"<div style='margin:10px 0;display:flex;gap:8px;align-items:center'>"
        f"<form method='post' action='/auto_control/toggle_profile' style='margin:0'>"
        f"<input type='hidden' name='action' value='all_on'>"
        f"<button type='submit' style='background:#28a745;color:#fff;border:0;"
        f"padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px'>"
        f"🟢 Zapnúť všetko</button></form>"
        f"<form method='post' action='/auto_control/toggle_profile' style='margin:0'>"
        f"<input type='hidden' name='action' value='all_off'>"
        f"<button type='submit' style='background:#666;color:#fff;border:0;"
        f"padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px'>"
        f"⚪ Vypnúť všetko</button></form>"
        f"<span style='margin-left:14px;color:#666;font-size:11px'>"
        f"<b>Vysvetlivky:</b> "
        f"stĺpec <b>obchodujem</b> = ON/OFF paper trading (jediné čo riadi scheduler) · "
        f"<b>stav plánu</b> = či sa našiel D-1 plán pre aktuálny slot (informačné)"
        f"</span>"
        f"</div>"
    )

    # Posledných N záznamov z logu
    log = _ac.read_log(market=mk_code, n=50)
    body += (
        f"<h2 style='color:#1F4E78;margin-top:24px;font-size:18px'>"
        f"📋 Posledných {len(log)} rozhodnutí z logu</h2>"
        f"<p style='color:#666;font-size:12px;margin:0 0 8px'>"
        f"Súbor: <code>out/{mk_code}/auto_control_log.csv</code></p>"
    )
    if not log:
        body += "<p style='color:#888;font-size:12px'>Žiadny log zatiaľ. Pri prvom cron run-e sa vytvorí.</p>"
    else:
        body += (
            f"<table style='width:100%;border-collapse:collapse;font-size:11px;border:1px solid #ddd'>"
            f"<thead><tr style='background:#f0f0f0'>"
            f"<th style='padding:4px;text-align:left'>čas</th>"
            f"<th style='padding:4px;text-align:left'>profil</th>"
            f"<th style='padding:4px;text-align:center'>slot</th>"
            f"<th style='padding:4px;text-align:center'>smer</th>"
            f"<th style='padding:4px;text-align:right'>kWh</th>"
            f"<th style='padding:4px;text-align:right'>cena €/MWh</th>"
            f"<th style='padding:4px;text-align:right'>setpoint kW</th>"
            f"<th style='padding:4px;text-align:right'>SOC %</th>"
            f"<th style='padding:4px;text-align:center'>mode</th>"
            f"<th style='padding:4px;text-align:left'>reason / error</th>"
            f"</tr></thead><tbody>"
        )
        dir_colors = {
            "BUY": "#C62828", "SELL": "#28a745",
            "VDT_BUY": "#AD1457", "VDT_SELL": "#2E7D32",
            "CURTAIL_FTV": "#E65100", "LOAD_COVER": "#0288D1",
            "idle": "#999",
        }
        dir_icons = {
            "BUY": "📥", "SELL": "📤",
            "VDT_BUY": "🛒", "VDT_SELL": "💰",
            "CURTAIL_FTV": "✂️", "LOAD_COVER": "🔌",
            "idle": "—",
        }
        for r in reversed(log[-50:]):
            mode_v = r.get("mode", "?")
            mode_clr = "#28a745" if mode_v == "simulation" else "#C62828"
            sp_v = r.get("setpoint_kw", "")
            try:
                sp_v_f = float(sp_v)
                sp_str = f"{sp_v_f:+.1f}"
            except (ValueError, TypeError):
                sp_str = "—"
            reason_or_err = r.get("error", "") or r.get("reason", "")
            # direction
            _dir = str(r.get("direction", "") or "")
            if not _dir or _dir == "idle":
                # Spätná kompatibilita: odvoď zo setpoint_kw
                try:
                    spf = float(sp_v)
                    if spf > 1.0: _dir = "SELL"
                    elif spf < -1.0: _dir = "BUY"
                    else: _dir = "idle"
                except Exception:
                    _dir = "idle"
            d_clr = dir_colors.get(_dir, "#666")
            d_icon = dir_icons.get(_dir, "")
            # kWh
            try:
                kwh_v = float(r.get("kwh", 0) or 0)
                kwh_str = f"{kwh_v:.1f}" if abs(kwh_v) > 0.05 else "—"
            except Exception:
                kwh_str = "—"
            # cena €/MWh
            try:
                pr_v = r.get("price_eur_mwh", "")
                pr_v_f = float(pr_v) if pr_v not in ("", None) else None
                pr_str = f"{pr_v_f:.1f}" if pr_v_f is not None else "—"
            except Exception:
                pr_str = "—"
            body += (
                f"<tr style='border-bottom:1px solid #eee'>"
                f"<td style='padding:3px 6px;font-family:monospace'>{_html.escape(str(r.get('ts', ''))[:19])}</td>"
                f"<td style='padding:3px 6px'>{_html.escape(str(r.get('profile', '')))}</td>"
                f"<td style='padding:3px 6px;text-align:center;font-family:monospace'>"
                f"{_html.escape(str(r.get('slot_label', '')))}</td>"
                f"<td style='padding:3px 6px;text-align:center;font-weight:600;color:{d_clr}'>"
                f"{d_icon} {_html.escape(_dir)}</td>"
                f"<td style='padding:3px 6px;text-align:right;font-family:monospace'>{kwh_str}</td>"
                f"<td style='padding:3px 6px;text-align:right;font-family:monospace;color:#1F4E78'>{pr_str}</td>"
                f"<td style='padding:3px 6px;text-align:right;font-family:monospace'>{sp_str}</td>"
                f"<td style='padding:3px 6px;text-align:right'>{_html.escape(str(r.get('soc_pct', ''))[:6])}</td>"
                f"<td style='padding:3px 6px;text-align:center'>"
                f"<span style='background:{mode_clr};color:#fff;padding:1px 6px;"
                f"border-radius:3px;font-size:10px'>{mode_v}</span></td>"
                f"<td style='padding:3px 6px;color:#666;font-size:10px'>"
                f"{_html.escape(str(reason_or_err)[:60])}</td>"
                f"</tr>"
            )
        body += "</tbody></table>"

    # ── Plánované VDT extras (nadrámcové) na dnes ──
    if enabled_set:
        try:
            import vdt_live_advisor as _adv
        except Exception:
            _adv = None
        if _adv is not None:
            body += (
                f"<h2 style='color:#1F4E78;margin-top:24px;font-size:18px'>"
                f"🚀 Plánované VDT extras (nadrámcové obchody) na dnes</h2>"
                f"<p style='color:#666;font-size:12px;margin:0 0 8px'>"
                f"BUY/SELL navyše oproti D-1 plánu, plus CURTAIL_FTV (orezanie FTV pri zlej VDT cene) "
                f"a LOAD_COVER (pokrytie spotreby z VDT keď je lacnejší než DAM). "
                f"Tieto návrhy sa zapíšu do logu pri cron-tick-u v príslušnom slote.</p>"
            )
            v_dir_clr = {
                "BUY": "#C62828", "SELL": "#28a745",
                "CHARGE": "#C62828", "DISCHARGE": "#28a745",
                "VDT_BUY": "#AD1457", "VDT_SELL": "#2E7D32",
                "CURTAIL_FTV": "#E65100", "LOAD_COVER": "#0288D1",
                "BOTH": "#6A1B9A",
            }
            v_dir_icon = {
                "BUY": "📥", "SELL": "📤", "CHARGE": "📥", "DISCHARGE": "📤",
                "VDT_BUY": "🛒", "VDT_SELL": "💰",
                "CURTAIL_FTV": "✂️", "LOAD_COVER": "🔌", "BOTH": "🔄",
            }
            cur_slot_now2 = ((dt.datetime.now().hour * 60 + dt.datetime.now().minute)
                              // 15)
            for prof_name in sorted(enabled_set):
                try:
                    cache = _adv.load_cache(profile=prof_name) or {}
                except Exception:
                    cache = {}
                fp = (cache or {}).get("full_plan") or []
                extras = [e for e in fp
                          if (e.get("action") or "").lower() not in ("idle", "")]
                summ_v = cache.get("summary") or {}
                tot_buy = float(summ_v.get("total_buy_kwh", 0) or 0)
                tot_sell = float(summ_v.get("total_sell_kwh", 0) or 0)
                tot_curt = float(summ_v.get("total_curtail_ftv_kwh", 0) or 0)
                tot_load = float(summ_v.get("total_load_cover_kwh", 0) or 0)
                tot_prof = float(summ_v.get("total_extra_profit_eur",
                                            summ_v.get("expected_profit_eur", 0)) or 0)
                # Fallback: ak summary nemá tieto polia (cache je z vdt_optimizer,
                # nie z vdt_extras), spočítaj zo slotov + spočítaj profit zo cien.
                if tot_buy == 0 and tot_sell == 0 and extras:
                    _calc_buy = _calc_sell = _calc_profit = 0.0
                    for _e in extras:
                        _act = (_e.get("action") or "").lower()
                        _kwh = float(_e.get("kwh", 0) or 0)
                        _bp = _e.get("buy_price")
                        _sp = _e.get("sell_price")
                        try:
                            _bp = float(_bp) if _bp is not None else None
                        except Exception:
                            _bp = None
                        try:
                            _sp = float(_sp) if _sp is not None else None
                        except Exception:
                            _sp = None
                        if _act in ("charge", "buy", "load_cover"):
                            _calc_buy += _kwh
                            if _bp is not None:
                                _calc_profit -= _kwh * _bp / 1000.0
                        elif _act in ("discharge", "sell"):
                            _calc_sell += _kwh
                            if _sp is not None:
                                _calc_profit += _kwh * _sp / 1000.0
                    tot_buy = _calc_buy
                    tot_sell = _calc_sell
                    if tot_prof == 0:
                        tot_prof = _calc_profit
                body += (
                    f"<details {'open' if extras else ''} "
                    f"style='margin:8px 0;border:1px solid #ddd;border-radius:8px;"
                    f"padding:10px;background:#fff'>"
                    f"<summary style='font-weight:600;color:#1F4E78;cursor:pointer;font-size:13px'>"
                    f"{_html.escape(prof_name)} "
                    f"<span style='color:#666;font-weight:400;font-size:12px;margin-left:8px'>"
                    f"BUY <b>{tot_buy:.0f}</b> kWh · SELL <b>{tot_sell:.0f}</b> kWh · "
                    f"✂️ <b>{tot_curt:.0f}</b> kWh · 🔌 <b>{tot_load:.0f}</b> kWh · "
                    f"očak. extra zisk <b style='color:#28a745'>"
                    f"+{tot_prof:.2f} €</b> · {len(extras)} návrhov</span>"
                    f"</summary>"
                )
                if not extras:
                    body += (
                        f"<p style='color:#888;font-size:11px;margin:6px 0 0'>"
                        f"Žiadne VDT extras pre dnešok (aktuálne ceny/SOC nedávajú "
                        f"ekonomický zmysel).</p></details>"
                    )
                    continue
                body += (
                    f"<table style='width:100%;border-collapse:collapse;font-size:11px;"
                    f"margin-top:8px'>"
                    f"<thead><tr style='background:#f0f0f0'>"
                    f"<th style='padding:3px 6px;text-align:left'>slot</th>"
                    f"<th style='padding:3px 6px;text-align:center'>akcia</th>"
                    f"<th style='padding:3px 6px;text-align:right'>kWh</th>"
                    f"<th style='padding:3px 6px;text-align:right'>cena €/MWh</th>"
                    f"<th style='padding:3px 6px;text-align:right'>SOC po slote</th>"
                    f"<th style='padding:3px 6px;text-align:left'>reason</th>"
                    f"</tr></thead><tbody>"
                )
                for e in extras:
                    sl = e.get("slot", "")
                    # Aktuálny slot?
                    is_now = False
                    try:
                        hh, mm = sl.split("-")[0].split(":")
                        slot_idx_e = int(hh) * 4 + int(mm) // 15
                        is_now = (slot_idx_e == cur_slot_now2)
                    except Exception:
                        pass
                    row_bg = ("background:#fff3cd;font-weight:600" if is_now else "")
                    act = (e.get("action") or "").upper()
                    clr = v_dir_clr.get(act, "#666")
                    ico = v_dir_icon.get(act, "")
                    kwh_e = float(e.get("kwh", 0) or 0)
                    # Cena: cache `full_plan` má `buy_price`/`sell_price` (z vdt_optimizer),
                    # vdt_extras zase `price_eur_mwh`. Pre CHARGE preferuj buy_price,
                    # pre DISCHARGE/SELL preferuj sell_price.
                    pr_e = e.get("price_eur_mwh")
                    if pr_e in (None, ""):
                        if act in ("CHARGE", "BUY", "VDT_BUY", "LOAD_COVER"):
                            pr_e = e.get("buy_price")
                        elif act in ("DISCHARGE", "SELL", "VDT_SELL"):
                            pr_e = e.get("sell_price")
                        else:
                            pr_e = e.get("buy_price") or e.get("sell_price")
                    try:
                        pr_str = f"{float(pr_e):.2f}" if pr_e not in (None, "") else "—"
                    except Exception:
                        pr_str = "—"
                    soc_a = e.get("soc_after", e.get("soc_pct"))
                    try:
                        soc_str = f"{float(soc_a):.0f}%" if soc_a not in (None, "") else "—"
                    except Exception:
                        soc_str = "—"
                    reason = (e.get("reason") or e.get("comment") or "")[:60]
                    body += (
                        f"<tr style='border-bottom:1px solid #eee;{row_bg}'>"
                        f"<td style='padding:2px 6px;font-family:monospace'>{_html.escape(sl)}"
                        f"{' ← teraz' if is_now else ''}</td>"
                        f"<td style='padding:2px 6px;text-align:center;color:{clr};font-weight:600'>"
                        f"{ico} {_html.escape(act)}</td>"
                        f"<td style='padding:2px 6px;text-align:right;font-family:monospace'>"
                        f"{kwh_e:.1f}</td>"
                        f"<td style='padding:2px 6px;text-align:right;font-family:monospace'>"
                        f"{pr_str}</td>"
                        f"<td style='padding:2px 6px;text-align:right'>{soc_str}</td>"
                        f"<td style='padding:2px 6px;color:#666;font-size:10px'>"
                        f"{_html.escape(reason)}</td>"
                        f"</tr>"
                    )
                body += "</tbody></table></details>"

    # Denný prehľad obchodov pre zapnuté profily
    if enabled_set:
        body += (
            f"<h2 style='color:#1F4E78;margin-top:24px;font-size:18px'>"
            f"📅 Denný plán obchodovania (zapnuté profily)</h2>"
            f"<p style='color:#666;font-size:12px;margin:0 0 8px'>"
            f"Čo sa kedy bude obchodovať podľa D-1 plánu + očakávaná SOC trajektória. "
            f"Aktuálny slot zvýraznený.</p>"
        )
        cur_slot_now = (dt.datetime.now().hour * 60 + dt.datetime.now().minute) // 15
        for prof_name in sorted(enabled_set):
            day = _ac.get_day_schedule(prof_name)
            if not day.get("ok"):
                body += (
                    f"<details open style='margin:10px 0;border:1px solid #ddd;"
                    f"border-radius:8px;padding:10px'>"
                    f"<summary style='font-weight:600;color:#1F4E78;cursor:pointer'>"
                    f"{_html.escape(prof_name)} — <span style='color:#C0392B'>"
                    f"{day.get('error', 'plán nedostupný')}</span></summary></details>"
                )
                continue
            summ = day["summary"]
            slots = day["slots"]
            step_min = day["step_min"]
            cur_idx_for_step = (cur_slot_now // 4) if step_min == 60 else cur_slot_now

            # Filtruj iba aktívne sloty (akcia ≠ idle) pre kompaktnosť
            active = [s for s in slots if s["action"] != "idle"]
            mode_chip_bg = "#C62828" if day.get("profile_mode") == "real" else "#1F4E78"
            mode_chip_label = "REAL" if day.get("profile_mode") == "real" else "SIM"
            body += (
                f"<details open style='margin:10px 0;border:1px solid #ddd;"
                f"border-radius:8px;padding:10px;background:#fff'>"
                f"<summary style='font-weight:600;color:#1F4E78;cursor:pointer;"
                f"font-size:14px'>"
                f"{_html.escape(prof_name)} "
                f"<span style='background:{mode_chip_bg};color:#fff;padding:1px 6px;"
                f"border-radius:8px;font-size:10px;vertical-align:middle'>"
                f"{mode_chip_label}</span> "
                f"<span style='color:#666;font-weight:400;font-size:12px;margin-left:10px'>"
                f"BUY <b>{summ.get('total_buy_kwh', 0):.0f}</b> kWh · "
                f"SELL <b>{summ.get('total_sell_kwh', 0):.0f}</b> kWh · "
                f"SOC <b>{summ.get('soc_start_pct', 0):.0f}%</b>→<b>"
                f"{summ.get('soc_end_pct', 0):.0f}%</b> · "
                f"očak. zisk <b style='color:#28a745'>"
                f"+{summ.get('expected_profit_eur', 0):.2f} €</b> · "
                f"{summ.get('n_buy_slots', 0)+summ.get('n_sell_slots', 0)} aktívnych slotov "
                f"({day.get('kind', '?')}/{step_min}min)</span>"
                f"</summary>"
                f"<table style='width:100%;border-collapse:collapse;font-size:11px;"
                f"margin-top:8px'>"
                f"<thead><tr style='background:#f0f0f0'>"
                f"<th style='padding:3px 6px;text-align:left'>slot</th>"
                f"<th style='padding:3px 6px;text-align:center'>akcia</th>"
                f"<th style='padding:3px 6px;text-align:right'>kW</th>"
                f"<th style='padding:3px 6px;text-align:right'>cena €/MWh</th>"
                f"<th style='padding:3px 6px;text-align:right'>SOC po slote</th>"
                f"</tr></thead><tbody>"
            )
            for s in active:
                is_now = (s["slot_idx"] == cur_idx_for_step)
                act = s["action"]
                act_clr = "#28a745" if act == "SELL" else ("#C62828" if act == "BUY" else "#666")
                act_icon = "📤" if act == "SELL" else ("📥" if act == "BUY" else "—")
                row_bg = "background:#fff3cd;font-weight:600" if is_now else ""
                price_str = (f"{s['price_eur_mwh']:.2f}"
                             if s["price_eur_mwh"] is not None else "—")
                soc_str = (f"{s['soc_pct']:.0f}%"
                           if s["soc_pct"] is not None else "—")
                body += (
                    f"<tr style='border-bottom:1px solid #eee;{row_bg}'>"
                    f"<td style='padding:2px 6px;font-family:monospace'>{s['slot_label']}"
                    f"{' ← teraz' if is_now else ''}</td>"
                    f"<td style='padding:2px 6px;text-align:center;color:{act_clr};font-weight:600'>"
                    f"{act_icon} {act}</td>"
                    f"<td style='padding:2px 6px;text-align:right'>{s['setpoint_kw']:+.0f}</td>"
                    f"<td style='padding:2px 6px;text-align:right'>{price_str}</td>"
                    f"<td style='padding:2px 6px;text-align:right'>{soc_str}</td>"
                    f"</tr>"
                )
            if not active:
                body += (
                    f"<tr><td colspan='5' style='padding:6px;color:#888;text-align:center'>"
                    f"Žiadne aktívne sloty v pláne.</td></tr>"
                )
            body += "</tbody></table></details>"
    else:
        body += (
            f"<p style='color:#888;font-size:12px;margin-top:18px;"
            f"font-style:italic'>📅 Denný prehľad obchodov sa zobrazí keď zapneš "
            f"aspoň jeden profil.</p>"
        )

    # Real mode unlock guidance
    body += (
        f"<h2 style='color:#1F4E78;margin-top:24px;font-size:18px'>"
        f"🔓 Real mode unlock</h2>"
        f"<div style='background:#fff3cd;border-left:4px solid #856404;"
        f"padding:14px;border-radius:6px;font-size:13px'>"
        f"<b>Real mode (zápis do Bender) je hardcoded BLOCKED.</b> "
        f"Pre odblokovanie (až keď budeš mať dôveru v správanie):"
        f"<pre style='background:#f8f9fa;padding:10px;border-radius:4px;font-size:12px;margin-top:8px'>"
        f"echo '{{\"token\": \"I_UNDERSTAND_THIS_WRITES_TO_BENDER\", "
        f"\"unlocked_at\": \"{dt.datetime.now().isoformat(timespec='seconds')}\"}}' "
        f"&gt; out/auto_control_unlock.json</pre>"
        f"Po vytvorení súboru je real mode AKTÍVNY iba pre profily ktoré majú "
        f"<code>mode = 'real'</code>. Inak fallback na simulation.<br><br>"
        f"<b>Vrátiť na simulation:</b> zmaž <code>out/auto_control_unlock.json</code>"
        f"</div>"
    )

    body += "</div>"
    return render_legacy_body(None, "Paper trading", body,
                                head_extra="<meta http-equiv='refresh' content='60'>")


@app.post("/auto_control/kill_switch", response_class=HTMLResponse)
def auto_control_kill_switch(action: str = Form("set")):
    """Aktivuje alebo deaktivuje kill switch pre paper trading scheduler."""
    try:
        import auto_control as _ac
    except ImportError as e:
        return HTMLResponse(f"<p style='color:#C0392B'>Modul nedostupný: {e}</p>", status_code=500)
    if action == "clear":
        _ac.clear_kill_switch()
    else:
        _ac.set_kill_switch()
    return RedirectResponse(url="/auto_control", status_code=303)


@app.post("/auto_control/toggle_profile", response_class=HTMLResponse)
def auto_control_toggle_profile(name: str = Form(""), action: str = Form("on")):
    """Zapne/vypne paper trading pre konkrétny profil (alebo bulk all_on/all_off)."""
    try:
        import auto_control as _ac
        import profiles as _pr
    except ImportError as e:
        return HTMLResponse(f"<p style='color:#C0392B'>Modul nedostupný: {e}</p>", status_code=500)
    if action == "all_on":
        for p in _pr.list_profiles() or []:
            pname = p if isinstance(p, str) else (p.get("name") if isinstance(p, dict) else None)
            if pname:
                _ac.set_profile_enabled(pname, True)
    elif action == "all_off":
        for p in _pr.list_profiles() or []:
            pname = p if isinstance(p, str) else (p.get("name") if isinstance(p, dict) else None)
            if pname:
                _ac.set_profile_enabled(pname, False)
    elif name:
        _ac.set_profile_enabled(name, action == "on")
    return RedirectResponse(url="/auto_control", status_code=303)


@app.get("/vdt/test_orderbook", response_class=HTMLResponse)
def vdt_test_orderbook_page(duration: int = 0):
    """Diagnostický endpoint — ukáže raw SOAP request + response side-by-side.

    Query:
        duration: 0 = všetky produkty, 15 = iba 15-min, 60 = iba hodinové.

    Použitie: ked SOAP IdmOrderBook zlyhá, sem priam pozri request body
    (s vloženým podpisom) + response body od servera → uvidíš dôvod 500.
    """
    import html as _html
    try:
        import okte_vdt as _vdt
    except Exception as e:
        return f"<p style='color:#C0392B'>Modul nedostupný: {e}</p>"

    dur_arg = duration if duration in (15, 60) else None
    res = _vdt.get_orderbook(delivery_duration=dur_arg)
    debug = res.get("debug", {}) or {}

    status_color = "#28a745" if res.get("ok") else "#C0392B"
    ok_label = "✓ OK" if res.get("ok") else "✗ FAIL"

    req_xml = debug.get("request_body", "")
    resp_xml_full = debug.get("response_body", "")
    # Orezáme pre display (UI by sa duplilo na MB)
    resp_xml = resp_xml_full[:10000]
    if len(resp_xml_full) > 10000:
        resp_xml += f"\n\n... [orezané, plný response {len(resp_xml_full)} znakov — stiahni cez /vdt/raw_orderbook?duration={duration}] ..."
    resp_headers = debug.get("response_headers", {})

    headers_html = "".join(
        f"<tr><td style='padding:2px 8px;color:#666;font-family:monospace'>{_html.escape(k)}</td>"
        f"<td style='padding:2px 8px;font-family:monospace'>{_html.escape(str(v))[:200]}</td></tr>"
        for k, v in (resp_headers.items() if isinstance(resp_headers, dict) else [])
    )

    err_html = ""
    if not res.get("ok"):
        err_html = (
            f"<div style='background:#ffe7e7;border-left:4px solid #C0392B;"
            f"padding:10px;margin:8px 0;border-radius:6px'>"
            f"<b>Chyba:</b> {_html.escape(str(res.get('error','?')))[:600]}</div>"
        )

    nav = _nav("/vdt")
    body = (
        f"{nav}"
        f"<div style='max-width:1600px;margin:14px auto;padding:0 16px;"
        f"font-family:-apple-system,Segoe UI,Arial'>"
        f"<h1 style='color:#1F4E78'>🔧 SOAP IdmOrderBook — debug</h1>"
        f"{_vdt_subnav('/vdt/test_orderbook')}"
        f"<p style='color:#666'>Test podpísaného SOAP volania. "
        f"<a href='/vdt/test_orderbook?duration=15'>iba 15-min</a> · "
        f"<a href='/vdt/test_orderbook?duration=60'>iba hodinové</a> · "
        f"<a href='/vdt/test_orderbook'>všetko</a></p>"

        f"<div style='background:#f8f9fa;padding:14px;border-radius:8px;margin:10px 0'>"
        f"<h2 style='margin:0 0 8px;color:{status_color}'>{ok_label} · "
        f"HTTP {debug.get('status', '?')}</h2>"
        f"<table style='font-size:12px'>"
        f"<tr><td><b>URL:</b></td><td><code>{_html.escape(str(debug.get('url','?')))}</code></td></tr>"
        f"<tr><td><b>Action:</b></td><td><code style='font-size:11px'>{_html.escape(str(debug.get('action_uri','?')))}</code></td></tr>"
        f"<tr><td><b>WSSE signed:</b></td><td>{debug.get('wsse_signed', '?')}</td></tr>"
        f"<tr><td><b>Request size:</b></td><td>{debug.get('request_size_b', 0)} B</td></tr>"
        f"<tr><td><b>Response size:</b></td><td>{debug.get('response_size_b', 0)} B</td></tr>"
        f"</table></div>"
        f"{err_html}"

        f"<h2 style='color:#1F4E78;margin-top:16px'>📤 Request (podpísané SOAP)</h2>"
        f"<details><summary style='cursor:pointer;color:#1F4E78'>Zobraziť/skryť ({len(req_xml)} znakov)</summary>"
        f"<pre style='background:#f0f0f0;padding:10px;border-radius:6px;"
        f"font-size:10px;max-height:500px;overflow:auto'>{_html.escape(req_xml)}</pre>"
        f"</details>"

        f"<h2 style='color:#1F4E78;margin-top:16px'>📥 Response</h2>"
        f"<details open><summary style='cursor:pointer;color:#1F4E78'>Body ({len(resp_xml)} znakov)</summary>"
        f"<pre style='background:#f0f0f0;padding:10px;border-radius:6px;"
        f"font-size:10px;max-height:500px;overflow:auto'>{_html.escape(resp_xml)}</pre>"
        f"</details>"
        f"<details><summary style='cursor:pointer;color:#666'>Response Headers</summary>"
        f"<table style='font-size:11px;margin-top:6px'>{headers_html}</table></details>"

        f"</div>"
    )
    return render_legacy_body(None, "VDT debug", body)


@app.get("/vdt", response_class=HTMLResponse)
def vdt_page(request: Request):
    """OKTE ISOT VDT (intraday) — READ-ONLY view na participant účet.

    4 sekcie: status, aktívne príkazy, vlastné obchody, pozícia + balance.
    Žiadne formuláre na zápis — modul okte_vdt.py je iba GET klient.
    """
    import html as _html
    import json as _json
    try:
        import okte_vdt as _vdt
    except ImportError:
        return ("<!doctype html><html><head><meta charset='utf-8'><title>OKTE VDT</title></head>"
                "<body style='font-family:-apple-system,Segoe UI,Arial;max-width:1100px;margin:24px auto;padding:0 16px'>"
                "<h1>💹 OKTE VDT</h1>"
                "<p style='background:#ffeaea;padding:12px;border-radius:8px;color:#C0392B'>"
                "⚠ Modul <code>okte_vdt.py</code> nie je dostupný. Skontroluj inštaláciu.</p>"
                "</body></html>")

    nav = _nav("/vdt")
    status = _vdt.status_summary()

    # ── Sekcia 1: Status + config diag ─────────────────────────────────────
    if not status.get("enabled"):
        setup_html = (
            "<div style='background:#fff3cd;border-left:6px solid #C49000;padding:14px 18px;"
            "border-radius:10px;margin:14px 0'>"
            "<b>⚙ Modul nie je nakonfigurovaný.</b><br><br>"
            "<b>Postup:</b><br>"
            "1️⃣ V Keychain Access exportuj OKTE certifikát ako <code>.p12</code> "
            "(pravý klik na cert → Export → Personal Information Exchange .p12 → zadaj heslo).<br>"
            "2️⃣ V termináli v adresári <code>Aplikacia</code> spusti:<br>"
            "<pre style='background:#fff;padding:10px;border-radius:6px;margin:8px 0'>"
            "./install_okte_cert.sh ~/Downloads/okte_cert.p12</pre>"
            "3️⃣ Skript ti pýta heslo k .p12, extrahuje cert+kľúč, otestuje a aktivuje modul.<br>"
            "4️⃣ Refresh tejto stránky.<br><br>"
            "<b>Read-only režim:</b> modul vie iba čítať dáta z OKTE (orders, orderbook, trades, "
            "account). Žiadne POST/PUT/DELETE funkcie nie sú implementované."
            "</div>")
    else:
        cert_chip = (
            f"<span style='background:{'#2E7D32' if status['cert_exists'] else '#C62828'};color:#fff;"
            f"padding:3px 9px;border-radius:5px;font-size:12px;font-weight:600'>"
            f"{'✓ cert OK' if status['cert_exists'] else '✗ cert chýba'}</span>"
        )
        key_chip = (
            f"<span style='background:{'#2E7D32' if status['key_exists'] else '#C62828'};color:#fff;"
            f"padding:3px 9px;border-radius:5px;font-size:12px;font-weight:600'>"
            f"{'✓ kľúč OK' if status['key_exists'] else '✗ kľúč chýba'}</span>"
        )
        last_check = status.get("last_check_ts") or "(nikdy)"
        last_status = status.get("last_check_status") or "—"
        setup_html = (
            f"<div style='background:#eef3f9;padding:12px 16px;border-radius:10px;margin:10px 0'>"
            f"<b>💹 OKTE ISOT VDT</b> &nbsp; {cert_chip} {key_chip}<br>"
            f"<span style='font-size:13px;color:#555'>"
            f"<b>Base URL:</b> <code>{_html.escape(status.get('base_url') or '?')}</code> · "
            f"<b>Username:</b> <code>{_html.escape(status.get('username') or '(none)')}</code> · "
            f"<b>Last probe:</b> {last_check} ({last_status})"
            f"</span>"
            f"<form method='post' action='/vdt/probe' style='display:inline-block;margin-left:12px'>"
            f"<button type='submit' style='background:#1F4E78;color:#fff;border:0;"
            f"padding:5px 14px;border-radius:6px;cursor:pointer;font-size:12px'>🔍 Otestovať pripojenie</button>"
            f"</form>"
            f"<form method='post' action='/vdt/discover' style='display:inline-block;margin-left:6px'>"
            f"<button type='submit' style='background:#5E35B1;color:#fff;border:0;"
            f"padding:5px 14px;border-radius:6px;cursor:pointer;font-size:12px' "
            f"title='Skúsi ~50 URL variantov a ukáže ktoré nedávajú IIS 404'>"
            f"🧭 Discover URL paths</button></form>"
            f"<form method='post' action='/vdt/inspect_cert' style='display:inline-block;margin-left:6px'>"
            f"<button type='submit' style='background:#C49000;color:#fff;border:0;"
            f"padding:5px 14px;border-radius:6px;cursor:pointer;font-size:12px' "
            f"title='Zobrazí Subject, Issuer, Validity a Extended Key Usage cert-u'>"
            f"🪪 Diagnostika certifikátu</button></form></div>")

    # ── Sekcia 2: Aktívne príkazy (orders) ─────────────────────────────────
    def _render_data_block(title, fetch_fn, *args, **kwargs):
        try:
            res = fetch_fn(*args, **kwargs)
        except Exception as e:
            return (f"<h2>{title}</h2>"
                    f"<p style='background:#ffeaea;padding:10px;border-radius:6px;color:#C0392B'>"
                    f"⚠ Chyba volania: {_html.escape(str(e))}</p>")
        if not res.get("ok"):
            err = res.get("error", "?")
            url = res.get("url", "?")
            return (f"<h2>{title}</h2>"
                    f"<div style='background:#ffeaea;padding:10px;border-radius:6px;color:#C0392B;font-size:13px'>"
                    f"<b>⚠ Endpoint zlyhal</b><br>"
                    f"URL: <code>{_html.escape(url)}</code><br>"
                    f"Status: <code>{res.get('status','?')}</code><br>"
                    f"Detail: <pre style='white-space:pre-wrap;font-size:11px;margin:6px 0'>"
                    f"{_html.escape(str(err)[:500])}</pre>"
                    f"<i>Ak endpoint vracia 404, doplň správnu URL do "
                    f"<code>okte_vdt_config.json → endpoints</code>.</i></div>")
        data = res.get("data")
        sample_json = _json.dumps(data, ensure_ascii=False, indent=2)[:3000]
        return (f"<h2>{title} · <span style='font-size:13px;color:#2E7D32'>✓ HTTP {res.get('status')}</span></h2>"
                f"<details open style='background:#f8f9fa;border-radius:8px;padding:10px 14px;margin:8px 0'>"
                f"<summary style='cursor:pointer;font-weight:600;color:#1F4E78'>Response JSON ({type(data).__name__})</summary>"
                f"<pre style='background:#fff;padding:10px;border-radius:6px;margin:8px 0;"
                f"font-size:12px;line-height:1.4;overflow-x:auto;white-space:pre-wrap;max-height:400px'>"
                f"{_html.escape(sample_json)}</pre></details>")

    if status.get("enabled") and status.get("cert_exists"):
        sec_market   = _render_data_block("🟢 Stav trhu (market-status)", _vdt.get_market_status)
        sec_orderbook= _render_orderbook_section(_vdt)
        sec_orders   = _render_data_block("📝 Vlastné objednávky (orders)", _vdt.get_orders)
        sec_trades   = _render_data_block("💱 Vlastné obchody (trades, dnes)", _vdt.get_trades)
        sec_eval_d   = _render_data_block("📊 Vyhodnotenie dnes — súhrn (daily-summary)",
                                            _vdt.get_evaluations_daily_summary)
        sec_eval_dd  = _render_data_block("📈 Vyhodnotenie dnes — detail (daily-detail)",
                                            _vdt.get_evaluations_daily_detail)
        sec_eval_m   = _render_data_block("📅 Vyhodnotenie tento mesiac (monthly-summary)",
                                            _vdt.get_evaluations_monthly_summary)
        sec_h2h      = _render_data_block("🌐 Cezhraničné kapacity (hub-to-hub)",
                                            _vdt.get_hub_to_hub)
    else:
        sec_market = sec_orderbook = sec_orders = sec_trades = sec_eval_d = sec_eval_dd = sec_eval_m = sec_h2h = ""

    # ── Backfill card — od-do stiahnutie historických dát ──────────────────
    _today_iso = dt.date.today().isoformat()
    _week_ago = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    backfill_card = (
        f"<div style='background:#eef7ee;border:1px solid #87c79d;border-radius:10px;"
        f"padding:14px 18px;margin:14px 0'>"
        f"<div style='font-size:16px;font-weight:600;color:#1B5E20;margin-bottom:8px'>"
        f"📥 Stiahnuť historické dáta (od-do)</div>"
        f"<form method='post' action='/vdt/backfill_range' "
        f"style='display:flex;gap:10px;align-items:end;flex-wrap:wrap'>"
        f"<label style='font-size:13px'>Od<br>"
        f"<input type='date' name='date_from' value='{_week_ago}' required "
        f"style='padding:5px;border:1px solid #ccc;border-radius:5px'></label>"
        f"<label style='font-size:13px'>Do<br>"
        f"<input type='date' name='date_to' value='{_today_iso}' required "
        f"style='padding:5px;border:1px solid #ccc;border-radius:5px'></label>"
        f"<div style='display:flex;flex-direction:column;gap:4px;font-size:13px'>"
        f"<label><input type='checkbox' name='kind_trades' checked> "
        f"💱 Trades (vlastné, REST)</label>"
        f"<label><input type='checkbox' name='kind_eval_daily' checked> "
        f"📈 Eval daily-detail (REST)</label>"
        f"</div>"
        f"<div style='display:flex;flex-direction:column;gap:4px;font-size:13px'>"
        f"<label><input type='checkbox' name='kind_dam' checked> "
        f"📊 DAM clearing (OKTE public)</label>"
        f"<label><input type='checkbox' name='kind_vdt_15min' checked> "
        f"⚡ VDT 15-min (OKTE public)</label>"
        f"</div>"
        f"<button type='submit' style='background:#2E7D32;color:#fff;border:0;"
        f"padding:10px 20px;border-radius:6px;cursor:pointer;font-weight:600'>"
        f"▶ Stiahnuť</button>"
        f"</form>"
        f"<div style='font-size:11px;color:#555;margin-top:6px'>"
        f"Trades/Eval potrebujú mTLS cert (REST). DAM/VDT 15-min sú verejné OKTE dáta. "
        f"CSV výstupy: <code>out/sk/vdt_history/&lt;kind&gt;.csv</code></div>"
        f"</div>")
    body = (
        f"{nav}"
        f"<div style='max-width:1200px;margin:14px auto;padding:0 16px;"
        f"font-family:-apple-system,Segoe UI,Arial'>"
        f"<h1 style='color:#1F4E78'>💹 OKTE ISOT VDT (intraday)</h1>"
        f"{_vdt_subnav('/vdt')}"
        f"<p style='color:#666;font-size:13px;margin:4px 0'>"
        f"<b>Read-only</b> pohľad na účet účastníka OKTE intraday trhu. "
        f"Modul iba číta dáta — žiadne podávanie ani úprava príkazov.</p>"
        f"{setup_html}"
        f"{backfill_card}"
        f"{sec_market}"
        f"{sec_orderbook}"
        f"{sec_orders}"
        f"{sec_trades}"
        f"{sec_eval_d}"
        f"{sec_eval_dd}"
        f"{sec_eval_m}"
        f"{sec_h2h}"
        f"</div>"
    )
    from ui.templates import render_legacy_body
    return render_legacy_body(request, "OKTE VDT", body)


@app.post("/vdt/backfill_range", response_class=HTMLResponse)
def vdt_backfill_range_endpoint(
        date_from: str = Form(...),
        date_to: str = Form(...),
        kind_trades: str = Form(""),
        kind_eval_daily: str = Form(""),
        kind_dam: str = Form(""),
        kind_vdt_15min: str = Form(""),
        ):
    """VDT od-do backfill — stiahne zvolené kindy pre daný rozsah a uloží do CSV.

    Form fields (checkboxy posielajú "on" keď zaškrtnuté, "" keď nie):
        date_from, date_to: ISO YYYY-MM-DD
        kind_trades, kind_eval_daily, kind_dam, kind_vdt_15min: "on" alebo ""
    """
    import html as _html
    try:
        import okte_vdt_backfill as _vbf
    except ImportError as e:
        return HTMLResponse(f"<p style='color:#C0392B'>Modul okte_vdt_backfill nedostupný: {e}</p>",
                              status_code=500)
    kinds = []
    if kind_trades:     kinds.append("trades")
    if kind_eval_daily: kinds.append("eval_daily")
    if kind_dam:        kinds.append("dam")
    if kind_vdt_15min:  kinds.append("vdt_15min")
    if not kinds:
        kinds = ["trades", "eval_daily", "dam", "vdt_15min"]   # default = všetko
    log_lines: list[str] = []
    def _log(s: str):
        log_lines.append(s)
    res = _vbf.backfill_range(date_from, date_to, kinds, log=_log)
    cov = _vbf.coverage_summary()
    # Render log + summary
    log_html = "<pre style='background:#f8f9fa;padding:12px;border-radius:8px;font-size:12px;" \
                "max-height:400px;overflow-y:auto'>" + _html.escape("\n".join(log_lines)) + "</pre>"
    if not res.get("ok"):
        summary_html = (f"<div style='background:#ffeaea;padding:12px;border-radius:8px;color:#C0392B'>"
                          f"⚠ {_html.escape(str(res.get('error', '?')))}</div>")
    else:
        rows = []
        for k, s in (res.get("summary") or {}).items():
            rows.append(f"<tr><td>{k}</td><td>✓ {s['ok']}</td><td>✗ {s['fail']}</td>"
                          f"<td>{s['rows']}</td></tr>")
        summary_html = (
            f"<div style='background:#e8f5e9;padding:12px;border-radius:8px;color:#1B5E20'>"
            f"<b>✓ Backfill dokončený</b> — {res.get('days_total')} dní × "
            f"{res.get('kinds_total')} kindov</div>"
            f"<table style='width:100%;border-collapse:collapse;margin:10px 0;font-size:13px' "
            f"class='tbl-compact'>"
            f"<tr style='background:#eef3f9'><th>Kind</th><th>OK dní</th><th>FAIL</th>"
            f"<th>Total rows</th></tr>"
            f"{''.join(rows)}</table>")
    cov_rows = []
    for k, c in cov.items():
        if c.get("exists"):
            cov_rows.append(f"<tr><td>{k}</td><td>{c.get('rows', 0)}</td>"
                              f"<td>{c.get('days', '—')}</td>"
                              f"<td>{c.get('first', '—')}</td>"
                              f"<td>{c.get('last', '—')}</td></tr>")
        else:
            cov_rows.append(f"<tr><td>{k}</td><td colspan='4' style='color:#999'>(CSV neexistuje)</td></tr>")
    cov_html = (
        f"<h3>Pokrytie CSV po backfille</h3>"
        f"<table style='width:100%;border-collapse:collapse;font-size:13px' class='tbl-compact'>"
        f"<tr style='background:#eef3f9'><th>Kind</th><th>Rows</th><th>Days</th>"
        f"<th>First</th><th>Last</th></tr>{''.join(cov_rows)}</table>")
    nav = _nav("/vdt")
    body = (
        f"{nav}"
        f"<div style='max-width:1100px;margin:14px auto;padding:0 16px;"
        f"font-family:-apple-system,Segoe UI,Arial'>"
        f"<h1>📥 VDT Backfill — výsledok</h1>"
        f"<p><b>Rozsah:</b> {_html.escape(date_from)} → {_html.escape(date_to)} · "
        f"<b>Kindy:</b> {', '.join(kinds)}</p>"
        f"{summary_html}{cov_html}"
        f"<h3>Detail logu</h3>{log_html}"
        f"<p><a href='/vdt' style='background:#1F4E78;color:#fff;padding:8px 16px;"
        f"border-radius:6px;text-decoration:none'>← Späť na /vdt</a></p>"
        f"</div>")
    from ui.templates import render_legacy_body
    return render_legacy_body(None, "VDT Backfill", body)


@app.post("/vdt/inspect_cert", response_class=HTMLResponse)
def vdt_inspect_cert_endpoint():
    """Diagnostika klientskeho certifikátu — pozri Subject/Issuer/EKU/expiry."""
    import html as _html
    import json as _json
    try:
        import okte_vdt as _vdt
    except ImportError:
        return "<p style='color:#C0392B'>Modul okte_vdt nedostupný</p>"
    info = _vdt.inspect_cert()
    nav = _nav("/vdt")
    body = (
        f"{nav}"
        f"<div style='max-width:1100px;margin:20px auto;padding:0 16px;"
        f"font-family:-apple-system,Segoe UI,Arial'>"
        f"<h1>🪪 OKTE VDT — Diagnostika certifikátu</h1>"
        f"<pre style='background:#fff;padding:14px;border-radius:8px;border:1px solid #ddd;"
        f"font-size:12px;line-height:1.5;overflow-x:auto;white-space:pre-wrap'>"
        f"{_html.escape(_json.dumps(info, ensure_ascii=False, indent=2))}</pre>"
        f"<div style='background:#fff3cd;padding:12px;border-radius:8px;margin-top:14px;border-left:4px solid #C49000'>"
        f"<b>Čo skontrolovať:</b><br>"
        f"<b>1. has_client_auth</b> musí byť <code>true</code> (OKTE 443 striktná mTLS vyžaduje EKU=clientAuth)<br>"
        f"<b>2. expired</b> musí byť <code>false</code> (cert v platnosti)<br>"
        f"<b>3. issuer</b> by mal byť OKTE/sféra CA (overiť v PDF)<br>"
        f"<b>4. subject</b> obsahuje názov tvojej firmy + EIC kód<br><br>"
        f"Ak je všetko OK ale 443 stále hádže SSL handshake, použi port <b>8443</b> (default v aktualizovanom configu).</div>"
        f"<p style='margin-top:14px'>"
        f"<a href='/vdt' style='padding:8px 16px;background:#1F4E78;color:#fff;text-decoration:none;border-radius:6px'>← Späť</a></p>"
        f"</div>"
    )
    return render_legacy_body(None, "OKTE cert", body)


@app.post("/vdt/discover", response_class=HTMLResponse)
def vdt_discover_endpoint():
    """Discovery — skúsi ~50 URL variantov a ukáže ktoré nedávajú IIS 404.
    Read-only — iba GET requesty. Použiť keď probe vracia 404 lebo URL paths
    sú odhadnuté nesprávne."""
    import html as _html
    import json as _json
    try:
        import okte_vdt as _vdt
    except ImportError:
        return "<p style='color:#C0392B'>Modul okte_vdt nedostupný</p>"
    res = _vdt.discover_endpoints()
    nav = _nav("/vdt")

    # Render winners (URL ktoré vrátili niečo iné než IIS 404)
    winners_html = ""
    for ep_name, urls in (res.get("winners") or {}).items():
        if not urls:
            winners_html += (f"<h3 style='color:#C0392B'>❌ {ep_name}</h3>"
                              f"<p style='color:#666;font-size:12px'>žiadny variant nereaguje "
                              f"(všetko IIS 404 = path neexistuje)</p>")
            continue
        winners_html += f"<h3 style='color:#2E7D32'>✓ {ep_name} — {len(urls)} kandidátov</h3><ul>"
        for u in urls:
            status_color = "#2E7D32" if u['status'] == 200 else ("#C49000" if u['status'] in (401, 403, 405) else "#5E35B1")
            winners_html += (
                f"<li style='margin:6px 0'>"
                f"<code style='background:#f5f5f5;padding:2px 6px;border-radius:4px'>"
                f"{_html.escape(u['url'])}</code> &nbsp; "
                f"<span style='background:{status_color};color:#fff;padding:2px 8px;"
                f"border-radius:5px;font-size:11px'>HTTP {u['status']}</span> "
                f"<span style='color:#666;font-size:11px'>{_html.escape(u.get('ct') or '')[:40]}</span>"
                f"<br><pre style='background:#fafafa;padding:6px;border-radius:4px;font-size:11px;"
                f"margin:4px 0;max-height:120px;overflow:auto'>"
                f"{_html.escape((u.get('snippet') or '')[:300])}</pre>"
                f"</li>"
            )
        winners_html += "</ul>"

    # Control check banner
    ctl = res.get("control_check") or {}
    if ctl.get("ok"):
        ctl_html = (f"<div style='background:#d4edda;padding:10px;border-radius:8px;"
                    f"border-left:4px solid #2E7D32'>"
                    f"✓ <b>Kontrola cert-u:</b> public endpoint <code>{_html.escape(ctl['url'])}</code> "
                    f"vrátil HTTP 200 ({ctl.get('size_kb', 0):.1f} kB) — TLS/mTLS funguje.</div>")
    else:
        ctl_html = (f"<div style='background:#f8d7da;padding:10px;border-radius:8px;"
                    f"border-left:4px solid #C0392B'>"
                    f"⚠ <b>Kontrola cert-u zlyhala:</b> <code>{_html.escape(str(ctl))}</code></div>")

    body = (
        f"{nav}"
        f"<div style='max-width:1200px;margin:20px auto;padding:0 16px;"
        f"font-family:-apple-system,Segoe UI,Arial'>"
        f"<h1>🔍 OKTE VDT — Discovery</h1>"
        f"<p style='color:#666'>Skúšam ~50 URL variantov pre 5 endpointov. "
        f"Hľadám tie ktoré nedávajú IIS 404 (HTML).</p>"
        f"{ctl_html}"
        f"<p><b>{_html.escape(res.get('summary', ''))}</b></p>"
        f"<h2>Kandidáti na správne URL</h2>"
        f"{winners_html}"
        f"<details style='margin-top:20px'>"
        f"<summary style='cursor:pointer;color:#1F4E78'>📋 Plný JSON (všetky pokusy)</summary>"
        f"<pre style='background:#fff;padding:14px;border-radius:8px;border:1px solid #ddd;"
        f"font-size:11px;line-height:1.4;overflow-x:auto;white-space:pre-wrap;max-height:600px'>"
        f"{_html.escape(_json.dumps(res, ensure_ascii=False, indent=2))}</pre></details>"
        f"<p style='margin-top:20px'>"
        f"<a href='/vdt' style='padding:8px 16px;background:#1F4E78;color:#fff;"
        f"text-decoration:none;border-radius:6px'>← Späť</a></p>"
        f"</div>"
    )
    return render_legacy_body(None, "OKTE discover", body)


@app.post("/vdt/probe", response_class=HTMLResponse)
def vdt_probe_endpoint():
    """Otestuje pripojenie na OKTE — read-only probe všetkých 5 endpointov."""
    import html as _html
    import json as _json
    try:
        import okte_vdt as _vdt
    except ImportError:
        return "<p style='color:#C0392B'>Modul okte_vdt nedostupný</p>"
    res = _vdt.probe()
    nav = _nav("/vdt")
    body = (
        f"{nav}"
        f"<div style='max-width:1100px;margin:20px auto;padding:0 16px;"
        f"font-family:-apple-system,Segoe UI,Arial'>"
        f"<h1>🔍 OKTE VDT — výsledok probe</h1>"
        f"<p>{'✓ OK' if res.get('ok') else '⚠ Niečo zlyhalo'} — "
        f"{res.get('n_ok', 0)}/{res.get('n_total', 0)} endpointov</p>"
        f"<pre style='background:#fff;padding:14px;border-radius:8px;border:1px solid #ddd;"
        f"font-size:12px;line-height:1.5;overflow-x:auto;white-space:pre-wrap'>"
        f"{_html.escape(_json.dumps(res, ensure_ascii=False, indent=2))}</pre>"
        f"<a href='/vdt' style='display:inline-block;margin-top:14px;padding:8px 16px;"
        f"background:#1F4E78;color:#fff;text-decoration:none;border-radius:6px'>← Späť na /vdt</a>"
        f"</div>"
    )
    return render_legacy_body(None, "OKTE probe", body)


@app.get("/realio", response_class=HTMLResponse)
def realio_get(tab: str = "vizualizacia", cust: str = "Trakany", profile: str = ""):
    """Default landing → Vizualizácia dashboard.
    ?tab=nastavenie → config UI.
    ?tab=riadenie  → Reálne riadenie (embed /livesim s realio overlay)."""
    return _realio_page(tab=tab, cust=cust, profile=profile)


@app.post("/realio/save", response_class=HTMLResponse)
async def realio_save(req: Request):
    """Uloží konfiguráciu /realio formulára."""
    try:
        import realio as _rio
    except ImportError:
        return _realio_page("realio modul nedostupný", "err")
    form = await req.form()
    cfg = _rio.load_config()
    cfg["host"] = (form.get("host") or cfg.get("host") or "").strip()
    cfg["endpoint_path"] = (form.get("endpoint_path") or cfg.get("endpoint_path") or "/tag-data").strip()
    cfg["username"] = (form.get("username") or "").strip()
    cfg["password"] = (form.get("password") or "").strip()
    cfg["cookies"] = (form.get("cookies") or "").strip()
    cfg["verify_ssl"] = bool(form.get("verify_ssl"))
    try:
        cfg["poll_interval_s"] = max(5, int(form.get("poll_interval_s") or cfg.get("poll_interval_s") or 60))
    except (ValueError, TypeError):
        pass
    # Tagy: ak je form pole prázdne, ZACHOVAJ pôvodnú hodnotu z disku (nikdy ich
    # neresetuj cez accidental save s prázdnym formulárom).
    _existing_tags = cfg.get("tags_read") or {}
    def _keep_or(form_key: str, current_val: str) -> str:
        v = (form.get(form_key) or "").strip()
        return v if v else (current_val or "")
    cfg["tags_read"] = {
        "ftv_power_kw":  _keep_or("tag_ftv_power_kw",  _existing_tags.get("ftv_power_kw", "")),
        "load_power_kw": _keep_or("tag_load_power_kw", _existing_tags.get("load_power_kw", "")),
        "batt_power_kw": _keep_or("tag_batt_power_kw", _existing_tags.get("batt_power_kw", "")),
        "batt_soc_pct":  _keep_or("tag_batt_soc_pct",  _existing_tags.get("batt_soc_pct", "")),
        "grid_power_kw": _keep_or("tag_grid_power_kw", _existing_tags.get("grid_power_kw", "")),
    }
    # scale factors
    def _fscale(name, default):
        try:
            return float(form.get(name) or default)
        except (ValueError, TypeError):
            return float(default)
    cfg["scale_read"] = {
        "ftv_power_kw":  _fscale("scale_ftv_power_kw",  0.001),
        "load_power_kw": _fscale("scale_load_power_kw", 0.001),
        "batt_power_kw": _fscale("scale_batt_power_kw", 0.001),
        "batt_soc_pct":  _fscale("scale_batt_soc_pct",  1.0),
        "grid_power_kw": _fscale("scale_grid_power_kw", 0.001),
    }
    _existing_tw = cfg.get("tags_write") or {}
    cfg["tags_write"] = {
        "batt_setpoint_kw":  _keep_or("tag_batt_setpoint_kw",  _existing_tw.get("batt_setpoint_kw", "")),
        "batt_control_mode": _keep_or("tag_batt_control_mode", _existing_tw.get("batt_control_mode", "")),
        "ftv_curtail_kw":    _keep_or("tag_ftv_curtail_kw",    _existing_tw.get("ftv_curtail_kw", "")),
    }
    try:
        cfg["control_mode_enable_value"]  = float(form.get("control_mode_enable_value")  or 2)
        cfg["control_mode_disable_value"] = float(form.get("control_mode_disable_value") or 0)
    except (ValueError, TypeError):
        pass
    # FVE control (Huawei SmartLogger cez SSH+Modbus)
    fve_cfg = dict(cfg.get("fve_control") or {})
    fve_cfg["ssh_host"] = (form.get("fve_ssh_host") or fve_cfg.get("ssh_host") or "10.200.136.21").strip()
    fve_cfg["ssh_user"] = (form.get("fve_ssh_user") or fve_cfg.get("ssh_user") or "support").strip()
    fve_cfg["ssh_key"]  = (form.get("fve_ssh_key")  or fve_cfg.get("ssh_key")  or "~/.ssh/support.rsa").strip()
    fve_cfg["device_ip"] = (form.get("fve_device_ip") or fve_cfg.get("device_ip") or "192.168.1.250").strip()
    fve_cfg["modpoll"]   = (form.get("fve_modpoll")   or fve_cfg.get("modpoll")   or "modpoll").strip()
    try:
        fve_cfg["ssh_port"]      = int(form.get("fve_ssh_port") or fve_cfg.get("ssh_port") or 8222)
        fve_cfg["slave_id"]      = int(form.get("fve_slave_id") or fve_cfg.get("slave_id") or 0)
        fve_cfg["ctrl_register"] = int(form.get("fve_register") or fve_cfg.get("ctrl_register") or 40428)
        fve_cfg["gain"]          = int(form.get("fve_gain")     or fve_cfg.get("gain") or 10)
    except (ValueError, TypeError):
        pass
    fve_cfg["enabled"] = bool(form.get("fve_enabled"))
    cfg["fve_control"] = fve_cfg
    cfg["enabled"] = bool(form.get("enabled"))
    cfg["control_enabled"] = bool(form.get("control_enabled"))
    _rio.save_config(cfg)
    # Reset HTTP session aby sa znovu prihlasilo s novými credentials
    try:
        import realio as _ri
        _ri._SESSION = None
        _ri._SESSION_HOST = None
    except Exception:
        pass
    return _realio_page("✓ Konfigurácia uložená. (Login sa pokúsi pri ďalšom Test čítania.)", "ok")


@app.post("/realio/test_read", response_class=HTMLResponse)
async def realio_test_read(req: Request):
    """Test fetch latest hodnôt + diagnostika."""
    await realio_save(req)
    try:
        import realio as _rio
        diag = _rio.diagnose()
        # Postaviť info banner s detailmi
        if diag.get("errors"):
            err_html = "<br>".join(diag["errors"])
            return _realio_page(f"<b>Diagnostika:</b><br>host: <code>{diag['host']}{diag['endpoint_path']}</code><br>"
                                  f"tagov: {diag['tags_configured']} · verify_ssl: {diag['verify_ssl']}<br>"
                                  f"<b>Chyby:</b><br>{err_html}", "err")
        # úspech — zapíš do CSV pre feedback
        if diag.get("latest"):
            try:
                vals = dict(diag["latest"])
                vals["_ts"] = dt.datetime.now().isoformat(timespec="seconds")
                _rio.append_measurement(vals)
            except Exception:
                pass
        # Detail latest hodnôt v správe
        if diag.get("latest"):
            lh = " · ".join(f"<b>{k}</b>={'-' if v is None else f'{v:,.1f}'}"
                            for k, v in diag["latest"].items())
            return _realio_page(f"✓ Test čítania OK · {lh}", "ok")
        return _realio_page("Test prebehol bez chyby ale nevrátil žiadne hodnoty.", "err")
    except Exception as e:
        return _realio_page(f"Test zlyhal: {e}", "err")


@app.get("/realio/api/latest")
def realio_api_latest():
    """JSON API — vráti posledné odpočet hodnôt z lokálneho CSV (rýchle, bez volania na server).
    Použiteľné pre iPhone Shortcut, widget, voice query atď.

    Response (príklad):
        {
          "ok": true,
          "time": "2026-05-31T14:23:00",
          "minutes_ago": 0.4,
          "ftv_power_kw": 314.8,
          "load_power_kw": -316.0,
          "batt_power_kw": -69.9,
          "batt_soc_pct": 99.7,
          "grid_power_kw": -500.0,
          "batt_setpoint_kw_cmd": null
        }
    """
    try:
        import realio as _rio
        df = _rio.read_recent(n_minutes=10)
        if df is None or df.empty:
            return JSONResponse({"ok": False, "msg": "žiadne dáta v CSV (modul vypnutý alebo polling nebeží)"}, status_code=503)
        last = df.iloc[-1]
        ts = pd.to_datetime(last["time"])
        ago_min = (pd.Timestamp.now() - ts).total_seconds() / 60.0
        def _f(v):
            if pd.isna(v) or v == "":
                return None
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                return None
        return JSONResponse({
            "ok": True,
            "time": ts.isoformat(timespec="seconds"),
            "minutes_ago": round(ago_min, 1),
            "ftv_power_kw":  _f(last.get("ftv_power_kw")),
            "load_power_kw": _f(last.get("load_power_kw")),
            "batt_power_kw": _f(last.get("batt_power_kw")),
            "batt_soc_pct":  _f(last.get("batt_soc_pct")),
            "grid_power_kw": _f(last.get("grid_power_kw")),
            "batt_setpoint_kw_cmd": _f(last.get("batt_setpoint_kw_cmd")),
        })
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)}, status_code=500)


@app.get("/realio/api/soc")
def realio_api_soc():
    """Mini JSON endpoint vracia LEN SOC % — najjednoduchšie pre iPhone widget/Siri."""
    try:
        import realio as _rio
        df = _rio.read_recent(n_minutes=10)
        if df is None or df.empty:
            return JSONResponse({"soc": None, "ok": False}, status_code=503)
        last = df.iloc[-1]
        v = last.get("batt_soc_pct")
        try:
            soc = round(float(v), 1) if v not in (None, "", float("nan")) else None
        except (TypeError, ValueError):
            soc = None
        return JSONResponse({"soc": soc, "ok": soc is not None,
                              "time": pd.to_datetime(last["time"]).isoformat(timespec="seconds")})
    except Exception as e:
        return JSONResponse({"soc": None, "ok": False, "err": str(e)}, status_code=500)


@app.post("/realio/relogin", response_class=HTMLResponse)
async def realio_relogin(req: Request):
    """Spustí Playwright login do dashboardu — refresh cookies.
    Playwright sync API nemôže bežať v FastAPI async loope → dispatch do thread poolu."""
    # Najprv uložiť aktuálny formulár (host/user/password)
    await realio_save(req)
    import asyncio
    try:
        import realio_login
        # asyncio.to_thread() spustí sync funkciu v thread pool executor — Playwright
        # sa cíti ako v normálnom sync kontexte (vlastný event loop nevidí FastAPI loop).
        ok, msg = await asyncio.to_thread(realio_login.login_once, False)
    except Exception as e:
        return _realio_page(f"Refresh cookies zlyhal: {e}", "err")
    if ok:
        return _realio_page(f'✓ Cookies obnovené ({msg}). Skús „Test čítania" pre overenie.', "ok")
    return _realio_page(f"⚠ Refresh cookies zlyhal: {msg}", "err")


@app.post("/realio/cleanup_csv", response_class=HTMLResponse)
def realio_cleanup_csv_endpoint():
    """Cleanup realio_measurements.csv — odstráni partial / prázdne riadky,
    zlúči duplikáty s rovnakou minútou."""
    try:
        import realio as _rio
    except ImportError:
        return _realio_page("realio modul nedostupný", "err", tab="riadenie")
    try:
        res = _rio.cleanup_csv()
    except Exception as e:
        return _realio_page(f"⚠ Cleanup zlyhal: {e}", "err", tab="riadenie")
    if res.get("ok"):
        return _realio_page(
            f"✓ {res['msg']}",
            "ok", tab="riadenie")
    return _realio_page(f"⚠ {res.get('msg','cleanup neprešiel')}", "err", tab="riadenie")


@app.post("/realio/fix_future_timestamps", response_class=HTMLResponse)
def realio_fix_future_timestamps():
    """Oprava DB riadkov ktoré buggy polling job (pred F2.2) uložil s posunom +2h.

    Detekuje riadky s time_ms > now+5min a posunie ich o -2h späť. Vhodné po
    deployi TZ-aware time storage fixu, ak appka bežala v starom kóde a polling
    stihol napísať pár "future" riadkov.
    """
    try:
        import realio_db as _db
    except ImportError:
        return _realio_page("realio_db modul nedostupný", "err", tab="riadenie")
    try:
        res = _db.fix_future_timestamps()
    except Exception as e:
        return _realio_page(f"⚠ Fix zlyhal: {e}", "err", tab="riadenie")
    if res.get("ok"):
        return _realio_page(f"✓ {res['msg']}", "ok", tab="riadenie")
    return _realio_page(f"⚠ {res.get('msg','fix neprešiel')}", "err", tab="riadenie")


@app.post("/realio/backfill_range", response_class=HTMLResponse)
def realio_backfill_range(from_date: str = Form(...), to_date: str = Form(...),
                            overwrite: str = Form(default="")):
    """Stiahne realio históriu pre interval od-do (max 2 dni naraz).
    `overwrite=1` → najprv odstráni existujúce záznamy v rozsahu (vyplnenie dier).
    Volaná z control panelu v /realio?tab=riadenie."""
    try:
        import realio as _rio
    except ImportError:
        return _realio_page("realio modul nedostupný", "err", tab="riadenie")
    # Parse dátumov
    try:
        from_dt = dt.datetime.fromisoformat(from_date)
        to_dt = dt.datetime.fromisoformat(to_date)
        # Ak je len date bez času, expandni to_dt na koniec dňa
        if to_dt.hour == 0 and to_dt.minute == 0:
            to_dt = to_dt + dt.timedelta(days=1) - dt.timedelta(seconds=1)
    except (ValueError, TypeError) as e:
        return _realio_page(f"⚠ Neplatný formát dátumu: {e}", "err", tab="riadenie")
    ow_flag = (str(overwrite) == "1")
    try:
        res = _rio.backfill_range_to_csv(from_dt, to_dt, max_days=2.0, overwrite=ow_flag)
    except Exception as e:
        return _realio_page(f"⚠ Backfill range zlyhal: {e}", "err", tab="riadenie")
    if res.get("ok"):
        ow_txt = " (overwrite mode)" if ow_flag else ""
        return _realio_page(
            f"✓ {res['msg']}{ow_txt} · pridaných <b>{res.get('rows_added',0)}</b> riadkov"
            + (f", prepísaných <b>{res.get('rows_removed',0)}</b>" if ow_flag else ""),
            "ok", tab="riadenie")
    return _realio_page(f"⚠ {res.get('msg','backfill_range neprešiel')}", "err", tab="riadenie")


@app.post("/realio/backfill", response_class=HTMLResponse)
def realio_backfill(days: int = Form(7), count_per_tag: int = Form(10000)):
    """Stiahne históriu posledných N dní z dashboardu a uloží do realio_measurements.csv."""
    try:
        import realio as _rio
    except ImportError:
        return _realio_page("realio modul nedostupný", "err")
    try:
        res = _rio.backfill_to_csv(days=days, count_per_tag=count_per_tag)
    except Exception as e:
        return _realio_page(f"Backfill zlyhal: {e}", "err")
    if res.get("ok"):
        return _realio_page(
            f"✓ {res['msg']} · obdobie {res.get('period_from','?')[:10]} → {res.get('period_to','?')[:10]}",
            "ok")
    return _realio_page(f"⚠ {res.get('msg','backfill neprešiel')}", "err")


def _resolve_realio_profile() -> str:
    """Bug Q (2026-06-06): Realio-pinned mechanizmus ZRUŠENÝ. Teraz vracia
    iba globálny active profile cez unified resolver. Ak chceš real chod na
    Trakany_real, aktivuj ho cez /profiles. Plus _ui_settings.realio_profile
    sa ignoruje (a vyčistí pri ďalšom set_active).
    """
    try:
        from core.profile_resolver import get_active as _ga
        return _ga() or ""
    except Exception:
        pass
    try:
        import plan_store as _ps
        return _ps.resolve_profile() or ""
    except Exception:
        return ""


def _require_real_profile():
    """Vráti error msg ak Realio-pinned profil NIE JE real mode. None = OK.
    Bezpečnostná poistka pre manual setpoint write — Simulačné profily nemôžu ovládať
    živú batériu / FTV.

    Pozor: kontroluje **Realio-pinned profil** (per-port persistovaný v /realio?tab=riadenie),
    NIE globálny active. Tým môže byť sim aktívny pre /plan a real pinned pre Realio writes.
    """
    try:
        import profiles as _pr
        target = _resolve_realio_profile()
        if not target or target == "default":
            return ("Realio profil nie je nastavený. Otvor "
                     "<a href='/realio?tab=riadenie' style='color:inherit'>🔴 Reálne riadenie</a> "
                     "a vyber Real profil z dropdownu (alebo vytvor nový v "
                     "<a href='/profiles' style='color:inherit'>/profiles</a>).")
        if _pr.get_mode(target) != _pr.MODE_REAL:
            return (f"Realio profil <b>{target}</b> je 🎮 Simulácia — nemôže ovládať reálnu "
                     f"batériu / FTV. V "
                     f"<a href='/realio?tab=riadenie' style='color:inherit'>🔴 Reálne riadenie</a> "
                     f"vyber Real profil (alebo vytvor nový v "
                     f"<a href='/profiles' style='color:inherit'>/profiles</a>).")
        return None
    except Exception as e:
        return f"Kontrola profile mode zlyhala: {e}"


@app.post("/realio/fve_write", response_class=HTMLResponse)
def realio_fve_write(value: float = Form(...)):
    """Manuálny FVE setpoint — nastaví činný výkon Huawei SmartLoggera v percentách (0..100).
    Vyžaduje control_enabled=True + fve_control.enabled=True v configu + aktívny profile.mode='real'.
    Mechanizmus: SSH na jump host (ssh_host:ssh_port) → modpoll zápis registra 40428."""
    # Bezpečnostná poistka — iba real profile
    err = _require_real_profile()
    if err:
        return _realio_page(f"⛔ FVE setpoint zamietnutý: {err}", "err")
    try:
        import realio as _rio
    except ImportError:
        return _realio_page("realio modul nedostupný", "err")
    res = _rio.write_fve_percent(value, source="manual")
    if res.get("ok"):
        return _realio_page(f"✓ FVE setpoint odoslaný: <b>{int(value)}%</b> — {res.get('msg','')}", "ok")
    # Fail — pridaj výstup modpoll-u ak je
    import html as _html
    extra = ""
    if res.get("output"):
        extra = (f"<details style='margin-top:8px' open><summary style='cursor:pointer;color:#1F4E78'>"
                 f"📋 modpoll výstup</summary>"
                 f"<pre style='background:#fff;padding:10px;border-radius:6px;border:1px solid #ddd;"
                 f"font-size:11px;white-space:pre-wrap;overflow-x:auto'>"
                 f"{_html.escape(res['output'])}</pre></details>")
    return _realio_page(f"⚠ FVE setpoint NEodoslaný: {_html.escape(res.get('msg','?'))}{extra}", "err")


@app.get("/realio/batt_plan_export", response_class=HTMLResponse)
def realio_batt_plan_export_preview(day: str = "", profile: str = ""):
    """Preview stránka — zobrazí 96-slot batt plán pre zvolený deň, editovateľná tabuľka,
    tlačidlá Potvrdiť / Zrušiť. Po Potvrdiť ide POST /realio/batt_plan_export/submit."""
    # Bezpečnostná poistka — iba real profile
    err = _require_real_profile()
    if err:
        return _realio_page(f"⛔ Export zamietnutý: {err}", "err", tab="riadenie")

    import datetime as _dt
    if not day:
        day = _dt.date.today().isoformat()
    try:
        d = _dt.datetime.strptime(day, "%Y-%m-%d").date()
    except Exception:
        return _realio_page(f"⚠ Neplatný formát dátumu: {day}", "err", tab="riadenie")

    # Načítaj uložený plán z plan_store pre tento deň + profil
    try:
        import plan_store as _ps
        prof = profile or _ps.resolve_profile()
    except Exception:
        prof = "default"

    plan_15min_kw = [0.0] * 96
    plan_soc_pct = [None] * 96
    plan_source = "default (zero)"
    try:
        import plan_store as _ps
        # Skús v poradí: 15-min dentrh → 15-min plan → 60-min plan
        plan = None
        for (step_min, kind) in [(15, "dentrh"), (15, "plan"), (60, "plan")]:
            try:
                plan = _ps.load_plan_safe(day, step_min, kind, profile=prof)
            except Exception:
                plan = None
            if plan is not None and isinstance(plan, dict):
                plan_source = f"plan_store ({step_min}min/{kind})"
                break
        if plan is not None and isinstance(plan, dict):
            sched = plan.get("schedule") or {}
            # schedule je dict s arrayami
            arr = None
            batt_col = None
            for c in ("batt_kw", "plan_batt_kw", "di"):
                if c in sched and isinstance(sched[c], list) and len(sched[c]) > 0:
                    arr = sched[c]; batt_col = c; break
            if arr is not None:
                if len(arr) == 24:
                    arr_15 = []
                    for v in arr:
                        arr_15.extend([float(v)] * 4)
                    plan_15min_kw = arr_15[:96]
                    plan_source += f" · {batt_col} hourly→15min"
                elif len(arr) == 96:
                    plan_15min_kw = [float(v) for v in arr][:96]
                    plan_source += f" · {batt_col} 15min native"
                else:
                    import numpy as _np
                    xi = _np.linspace(0, len(arr) - 1, 96)
                    xp = _np.arange(len(arr))
                    plan_15min_kw = _np.interp(xi, xp, _np.array(arr, float)).tolist()
                    plan_source += f" · {batt_col} {len(arr)}→96 interp"
            # SOC plán info
            if isinstance(sched.get("soc_pct"), list) and len(sched["soc_pct"]) > 0:
                soc_arr = sched["soc_pct"]
                if len(soc_arr) == 24:
                    soc_15 = []
                    for v in soc_arr:
                        soc_15.extend([float(v)] * 4)
                    plan_soc_pct = soc_15[:96]
                elif len(soc_arr) == 96:
                    plan_soc_pct = [float(v) for v in soc_arr][:96]
        else:
            plan_source = f"žiadny plán pre {day} v profile '{prof}' (skús /plan alebo /dentrh)"
    except Exception as _e:
        plan_source = f"chyba načítania: {_e}"

    # Konvencia pre Bender: kladné = vybíjanie, záporné = nabíjanie
    # Optimizer plan_batt_kw má opačnú konvenciu (+ch − di) v niektorých výstupoch.
    # Pre konzistenciu s Bender konvenciou: ak `batt_col == "plan_batt_kw"` z optimizéra,
    # je už správne (kladné = grid actions positive). Ak je z di-ch tabuľky, môže byť opačné.
    # Tu ponechávame ako-je (užívateľ si zmení v UI ak treba).

    # Render preview HTML
    nav = _nav("/realio")
    _err_html = ""

    # Build rows — kompaktná tabuľka 24 rows × 4 stĺpce (každá hodina 4× 15-min)
    rows_html = []
    rows_html.append(
        "<table style='border-collapse:collapse;width:100%;font-family:monospace;font-size:13px'>"
        "<thead><tr>"
        "<th style='padding:6px;background:#1F4E78;color:#fff;width:60px'>hod</th>"
        "<th style='padding:6px;background:#1F4E78;color:#fff'>:00 kW</th>"
        "<th style='padding:6px;background:#1F4E78;color:#fff'>:15 kW</th>"
        "<th style='padding:6px;background:#1F4E78;color:#fff'>:30 kW</th>"
        "<th style='padding:6px;background:#1F4E78;color:#fff'>:45 kW</th>"
        "<th style='padding:6px;background:#37474F;color:#fff'>SOC plán % (info)</th>"
        "</tr></thead><tbody>"
    )
    for h in range(24):
        slots = [plan_15min_kw[h * 4 + q] for q in range(4)]
        soc_q = plan_soc_pct[h * 4]
        soc_str = f"{soc_q:.0f}" if (soc_q is not None) else "—"
        row_bg = "#fafafa" if h % 2 == 0 else "#fff"
        cells = []
        for q in range(4):
            v = slots[q]
            color = "#fff"
            if v is not None and v != 0:
                color = "#FFEBEE" if v > 0 else "#E3F2FD"  # discharge=red bg, charge=blue bg
            cells.append(
                f"<td style='padding:0;background:{color};border:1px solid #ddd'>"
                f"<input type='number' name='kw_{h * 4 + q}' value='{v:.1f}' step='0.1' "
                f"style='width:100%;border:0;background:transparent;text-align:right;"
                f"padding:5px 8px;font-family:monospace;font-size:13px'></td>"
            )
        rows_html.append(
            f"<tr style='background:{row_bg}'>"
            f"<td style='padding:5px 8px;border:1px solid #ddd;font-weight:600;background:#eef3f9'>"
            f"{h:02d}h</td>{''.join(cells)}"
            f"<td style='padding:5px 8px;border:1px solid #ddd;text-align:right;color:#666'>{soc_str}</td></tr>"
        )
    rows_html.append("</tbody></table>")

    # Sumár — výpočet z plan_15min_kw
    sum_ch = sum(-v * 0.25 for v in plan_15min_kw if v < 0)   # kWh nabíjanie
    sum_di = sum(v * 0.25 for v in plan_15min_kw if v > 0)    # kWh vybíjanie

    # Date picker — formulár pre zmenu dňa
    next_day = (d + _dt.timedelta(days=1)).isoformat()
    prev_day = (d - _dt.timedelta(days=1)).isoformat()
    today = _dt.date.today().isoformat()
    tomorrow = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()

    body = (
        f"{nav}"
        f"<div style='max-width:1100px;margin:20px auto;padding:0 16px;font-family:-apple-system,Segoe UI,Arial'>"
        f"<h1 style='color:#1F4E78'>📤 Export riadenia batérie 15-min</h1>"
        f"<div style='background:#fff3cd;border-left:6px solid #C49000;padding:12px 16px;border-radius:8px;margin:12px 0'>"
        f"<b>⚠ POZOR:</b> Po potvrdení sa <b>96 setpointov</b> zapíše priamo na Bender server "
        f"(REG_Regulator_Param3) s budúcimi timestampami. <b>Konvencia Bender:</b> "
        f"<u>kladné kW = vybíjanie</u> do siete, <u>záporné kW = nabíjanie</u> zo siete. "
        f"Hodnota v kW × 1000 → watty. <b>Mode handshake:</b> Manual_Plan=<b>1</b> (pending) "
        f"sa pripíše ku každému slotu — Bender ho po spracovaní sám zmení na <b>2</b> (aktívny)."
        f"</div>"

        # Date picker + navigation
        f"<form method='get' action='/realio/batt_plan_export' "
        f"style='display:flex;gap:8px;align-items:center;margin:16px 0;padding:12px;"
        f"background:#eef3f9;border-radius:10px'>"
        f"<a href='/realio/batt_plan_export?day={prev_day}' "
        f"style='padding:6px 12px;background:#5E35B1;color:#fff;text-decoration:none;border-radius:6px'>"
        f"◀ {prev_day[5:]}</a>"
        f"<input type='date' name='day' value='{day}' "
        f"style='padding:6px;border:1px solid #ccc;border-radius:6px;font-size:14px'>"
        f"<button type='submit' "
        f"style='padding:6px 14px;background:#1F4E78;color:#fff;border:0;border-radius:6px;cursor:pointer'>"
        f"📅 Načítať</button>"
        f"<a href='/realio/batt_plan_export?day={next_day}' "
        f"style='padding:6px 12px;background:#5E35B1;color:#fff;text-decoration:none;border-radius:6px'>"
        f"{next_day[5:]} ▶</a>"
        f"<span style='margin-left:auto;color:#666;font-size:12px'>"
        f"Rýchle: "
        f"<a href='/realio/batt_plan_export?day={today}' style='color:#1F4E78'>Dnes</a> · "
        f"<a href='/realio/batt_plan_export?day={tomorrow}' style='color:#1F4E78'>Zajtra</a>"
        f"</span>"
        f"</form>"

        # Info banner — zdroj dát
        f"<div style='background:#f8f9fa;border-radius:8px;padding:10px 14px;margin:10px 0;"
        f"font-size:13px;color:#555'>"
        f"<b>Zdroj plánu:</b> {plan_source} · <b>Deň:</b> {day} · <b>Profil:</b> {prof}<br>"
        f"<b>Suma nabíjanie:</b> {sum_ch:.1f} kWh · <b>Suma vybíjanie:</b> {sum_di:.1f} kWh · "
        f"<b>Net:</b> {sum_di - sum_ch:+.1f} kWh"
        f"</div>"

        # Edit form
        f"<form method='post' action='/realio/batt_plan_export/submit' "
        f"onsubmit=\"return confirm('Naozaj zapísať 96 setpointov pre {day} na Bender? Toto sa nedá vrátiť!');\">"
        f"<input type='hidden' name='day' value='{day}'>"
        f"{''.join(rows_html)}"
        f"<div style='display:flex;gap:12px;margin:20px 0;align-items:center'>"
        f"<button type='submit' "
        f"style='padding:12px 24px;background:#C62828;color:#fff;border:0;border-radius:8px;"
        f"cursor:pointer;font-size:15px;font-weight:600'>"
        f"✓ Potvrdiť a zapísať na Bender</button>"
        f"<a href='/realio?tab=riadenie' "
        f"style='padding:12px 24px;background:#5E35B1;color:#fff;text-decoration:none;border-radius:8px;"
        f"font-size:15px;font-weight:600'>✗ Zrušiť</a>"
        f"<span style='margin-left:auto;color:#666;font-size:13px'>"
        f"Kladné kW = vybíjanie (🔴) · Záporné kW = nabíjanie (🔵) · 0 = idle</span>"
        f"</div>"
        f"</form>"
        f"</div>"
    )
    return render_legacy_body(None, "Export batt plán 15-min", body)


@app.post("/realio/batt_plan_export/submit", response_class=HTMLResponse)
async def realio_batt_plan_export_submit(request: Request):
    """Po Potvrdiť — vezme 96 hodnôt z formulára, zavolá realio.write_battery_plan_15min."""
    err = _require_real_profile()
    if err:
        return _realio_page(f"⛔ Export zamietnutý: {err}", "err", tab="riadenie")

    form = await request.form()
    day = str(form.get("day") or "").strip()
    if not day:
        return _realio_page("⚠ Chýba parameter 'day'", "err", tab="riadenie")

    # Zbieraj kw_0 .. kw_95
    plan_kw = []
    for i in range(96):
        v = form.get(f"kw_{i}")
        try:
            plan_kw.append(float(v) if v is not None else 0.0)
        except (TypeError, ValueError):
            plan_kw.append(0.0)

    try:
        import realio as _rio
    except ImportError:
        return _realio_page("realio modul nedostupný", "err", tab="riadenie")

    res = _rio.write_battery_plan_15min(plan_kw, day, source="manual_export_15min")
    if res.get("ok"):
        return _realio_page(
            f"✓ {res['msg']} · {res.get('total_kwh_charge',0):.1f} kWh nabíjanie / "
            f"{res.get('total_kwh_discharge',0):.1f} kWh vybíjanie", "ok", tab="riadenie")
    import html as _html
    tried = res.get("tried", [])
    errors = res.get("errors", [])
    diag = []
    if tried:
        diag.append("Skúšané requesty:")
        diag.extend([f"  • {_html.escape(t)}" for t in tried])
    if errors:
        diag.append("")
        diag.append("Exceptions:")
        diag.extend([f"  • {_html.escape(e)}" for e in errors])
    diag_block = "\n".join(diag) or "(žiadne detaily)"
    msg = (f"⚠ Export NEúspešný: {_html.escape(res.get('msg','?'))}<br>"
           f"<details style='margin-top:8px' open><summary style='cursor:pointer;color:#1F4E78'>"
           f"🔍 Diagnostika</summary>"
           f"<pre style='background:#fff;padding:10px;border-radius:6px;border:1px solid #ddd;"
           f"font-size:11px;line-height:1.4;overflow-x:auto;white-space:pre-wrap'>"
           f"{diag_block}</pre></details>")
    return _realio_page(msg, "err", tab="riadenie")


@app.post("/realio/write", response_class=HTMLResponse)
def realio_write(logical: str = Form(...), value: float = Form(...)):
    """Manuálny setpoint write — vyžaduje control_enabled=True v configu + active profile.mode='real'.
    Pre batt_setpoint_kw: dual-write Manual_Plan=2 + Param3=value×1000 W."""
    # Bezpečnostná poistka — iba real profile
    err = _require_real_profile()
    if err:
        return _realio_page(f"⛔ Setpoint zamietnutý: {err}", "err")
    try:
        import realio as _rio
    except ImportError:
        return _realio_page("realio modul nedostupný", "err")
    res = _rio.write_setpoint(logical, value, source="manual")
    if res.get("ok"):
        return _realio_page(f"✓ Setpoint odoslaný: <b>{res.get('tag','?')}</b> {res.get('msg','')}", "ok")
    # Fail — zostav podrobný diag s každou schémou
    import html as _html
    tried = res.get("tried", [])
    errors = res.get("errors", [])
    diag_lines = []
    if tried:
        diag_lines.append("Skúšané schémy (HTTP status + skrátená odpoveď):")
        diag_lines.extend([f"  • {_html.escape(t)}" for t in tried])
    if errors:
        diag_lines.append("")
        diag_lines.append("Exceptions:")
        diag_lines.extend([f"  • {_html.escape(e)}" for e in errors])
    diag_block = "\n".join(diag_lines) or "(žiadne detaily — možno crash mimo schém)"
    msg_html = (f"⚠ Setpoint NEodoslaný: {_html.escape(res.get('msg','?'))}<br>"
                f"<details style='margin-top:8px' open><summary style='cursor:pointer;color:#1F4E78'>"
                f"🔍 Diagnostika ({len(tried)} schém / {len(errors)} chýb)</summary>"
                f"<pre style='background:#fff;padding:10px;border-radius:6px;border:1px solid #ddd;"
                f"font-size:11px;line-height:1.4;overflow-x:auto;white-space:pre-wrap'>"
                f"{diag_block}</pre></details>")
    return _realio_page(msg_html, "err")


@app.post("/realio/discover_write", response_class=HTMLResponse)
def realio_discover_write():
    """Probe-discover správny write endpoint. Vracia diag block s každým pokusom + HTML hintmi."""
    try:
        import realio as _rio
    except ImportError:
        return _realio_page("realio modul nedostupný", "err")
    res = _rio.discover_write_endpoint()
    import html as _html
    probes = res.get("probes", [])
    hints  = res.get("html_hints", [])
    bundles = res.get("js_bundles", [])
    blocks = []
    if probes:
        blocks.append("<b>Endpoint probes:</b>")
        blocks.extend(f"  {_html.escape(p)}" for p in probes)
    if hints:
        blocks.append("")
        blocks.append("<b>HTML / JS hints:</b>")
        blocks.extend(f"  {_html.escape(h)}" for h in hints)
    if bundles:
        blocks.append("")
        blocks.append("<b>JS bundles found:</b>")
        blocks.extend(f"  {_html.escape(b)}" for b in bundles)
    body = "\n".join(blocks) or "(nič nenájdené — možno auth zlyhal)"
    msg_html = (f"🔎 Discover write endpoint — "
                f"<b>{len(probes)}</b> probes, <b>{len(hints)}</b> hints<br>"
                f"<pre style='background:#fff;padding:10px;border-radius:6px;border:1px solid #ddd;"
                f"font-size:11px;line-height:1.4;overflow-x:auto;white-space:pre-wrap;max-height:500px'>"
                f"{body}</pre>")
    return _realio_page(msg_html, "ok")


@app.post("/realio/scan_js", response_class=HTMLResponse)
def realio_scan_js():
    """Stiahne dashboard JS bundle a vyhľadá v ňom konkrétne write/Socket.IO indikátory."""
    try:
        import realio as _rio
    except ImportError:
        return _realio_page("realio modul nedostupný", "err")
    res = _rio.scan_js_bundle()
    import html as _html
    bundles = res.get("bundles", [])
    hits    = res.get("hits", {})
    errors  = res.get("errors", [])
    blocks = []
    if bundles:
        blocks.append("<b>JS bundles stiahnuté:</b>")
        blocks.extend(f"  {_html.escape(b)}" for b in bundles)
        blocks.append("")
    for cat, lst in hits.items():
        if not lst:
            continue
        blocks.append(f"<b>{cat} ({len(lst)} unique):</b>")
        blocks.extend(f"  {_html.escape(h)}" for h in lst[:25])
        blocks.append("")
    if errors:
        blocks.append("<b>Errors:</b>")
        blocks.extend(f"  {_html.escape(e)}" for e in errors)
    body = "\n".join(blocks) or "(nič nenájdené)"
    total_hits = sum(len(v) for v in hits.values())
    msg_html = (f"📜 JS bundle scan — <b>{len(bundles)}</b> bundles, "
                f"<b>{total_hits}</b> hits naprieč kategóriami<br>"
                f"<pre style='background:#fff;padding:10px;border-radius:6px;border:1px solid #ddd;"
                f"font-size:11px;line-height:1.4;overflow-x:auto;white-space:pre-wrap;"
                f"max-height:700px'>{body}</pre>")
    return _realio_page(msg_html, "ok")


@app.post("/realio/probe_ws", response_class=HTMLResponse)
def realio_probe_ws():
    """Zavolá /ws-address a vyhľadá _onSetValueButtonClick body v app.js — najpravdepodobnejší
    write protokol je WebSocket. Toto odhalí presnú schému."""
    try:
        import realio as _rio
    except ImportError:
        return _realio_page("realio modul nedostupný", "err")
    res = _rio.probe_ws_endpoint()
    import html as _html
    blocks = []
    blocks.append(f"<b>/ws-address:</b> {_html.escape(res.get('ws_raw',''))}")
    if res.get("ws_address"):
        blocks.append(f"<b>Detegovaná WS URL:</b> <code>{_html.escape(res['ws_address'])}</code>")
    blocks.append("")
    sv = res.get("set_value_snippets", [])
    if sv:
        blocks.append(f"<b>_onSetValueButtonClick snippets ({len(sv)}):</b>")
        for i, s in enumerate(sv):
            blocks.append(f"  --- snippet {i+1} ---")
            blocks.append(f"  {_html.escape(s)}")
        blocks.append("")
    ws = res.get("ws_send_snippets", [])
    if ws:
        blocks.append(f"<b>WS send / setValue / set-value patterns ({len(ws)}):</b>")
        for s in ws[:25]:
            blocks.append(f"  {_html.escape(s)}")
    if res.get("errors"):
        blocks.append("")
        blocks.append("<b>Errors:</b>")
        blocks.extend(f"  {_html.escape(e)}" for e in res["errors"])
    body = "\n".join(blocks) or "(nič)"
    msg_html = (f"📡 WS endpoint probe<br>"
                f"<pre style='background:#fff;padding:10px;border-radius:6px;border:1px solid #ddd;"
                f"font-size:11px;line-height:1.4;overflow-x:auto;white-space:pre-wrap;"
                f"max-height:800px'>{body}</pre>")
    return _realio_page(msg_html, "ok")


@app.post("/realio/scan_msg_types", response_class=HTMLResponse)
def realio_scan_msg_types():
    """Vyhľadá všetky messageType: N + ich kontexty v app.js, odhalí ktorý kód je write."""
    try:
        import realio as _rio
    except ImportError:
        return _realio_page("realio modul nedostupný", "err")
    res = _rio.scan_message_types()
    import html as _html
    blocks = []
    if res.get("js_url"):
        blocks.append(f"<b>JS:</b> {_html.escape(res['js_url'])} ({res.get('js_size',0):,} bytes)")
        blocks.append("")
    mt = res.get("message_types", {})
    if mt:
        blocks.append(f"<b>messageType výskyty (N = sort numeric):</b>")
        for n in sorted(mt.keys(), key=lambda x: int(x)):
            ctxs = mt[n]
            blocks.append(f"  ━━━ messageType: {n} ({len(ctxs)} vzoriek) ━━━")
            for c in ctxs:
                blocks.append(f"    {_html.escape(c)}")
        blocks.append("")
    sc = res.get("send_callers", [])
    if sc:
        blocks.append(f"<b>_sendRequestsToServer callers ({len(sc)}):</b>")
        for i, c in enumerate(sc):
            blocks.append(f"  --- caller {i+1} ---")
            blocks.append(f"  {_html.escape(c)}")
        blocks.append("")
    om = res.get("on_message_ws", [])
    if om:
        blocks.append(f"<b>_onMessageWS celé telo ({len(om)} výskytov, 2500 chars každý):</b>")
        for i, c in enumerate(om):
            blocks.append(f"  --- _onMessageWS #{i+1} ---")
            blocks.append(f"  {_html.escape(c)}")
        blocks.append("")
    eb = res.get("enum_blocks", [])
    if eb:
        blocks.append(f"<b>MessageType enum-like definície ({len(eb)}):</b>")
        for e in eb:
            blocks.append(f"  {_html.escape(e)}")
        blocks.append("")
    wm = res.get("write_method_blocks", [])
    if wm:
        blocks.append(f"<b>Tag write metódy ({len(wm)}) — 1500-char telá:</b>")
        for i, m in enumerate(wm):
            blocks.append(f"  --- method {i+1} ---")
            blocks.append(f"  {_html.escape(m)}")
        blocks.append("")
    cu = res.get("create_or_update_blocks", [])
    if cu:
        blocks.append(f"<b>createOrUpdateValues + setTagValue scan ({len(cu)}):</b>")
        for i, m in enumerate(cu):
            blocks.append(f"  --- def/call {i+1} ---")
            blocks.append(f"  {_html.escape(m)}")
        blocks.append("")
    tb = res.get("tag_data_builder", [])
    if tb:
        blocks.append(f"<b>Request builders s tagmi/messageType ({len(tb)}):</b>")
        for t in tb:
            blocks.append(f"  {_html.escape(t)}")
    if res.get("errors"):
        blocks.append("")
        blocks.append("<b>Errors:</b>")
        blocks.extend(f"  {_html.escape(e)}" for e in res["errors"])
    body = "\n".join(blocks) or "(nič)"
    msg_html = (f"🔢 messageType scan<br>"
                f"<pre style='background:#fff;padding:10px;border-radius:6px;border:1px solid #ddd;"
                f"font-size:11px;line-height:1.4;overflow-x:auto;white-space:pre-wrap;"
                f"max-height:900px'>{body}</pre>")
    return _realio_page(msg_html, "ok")


@app.post("/realio/ws_listen", response_class=HTMLResponse)
def realio_ws_listen():
    """Pripojí WebSocket, počúva 6s, loguje server messages + posiela testovacie schémy."""
    try:
        import realio as _rio
    except ImportError:
        return _realio_page("realio modul nedostupný", "err")
    res = _rio.ws_listen_probe(duration_s=12)
    import html as _html
    blocks = []
    blocks.append(f"<b>WS URL:</b> <code>{_html.escape(res.get('ws_url',''))}</code>")
    blocks.append("")
    sent = res.get("sent", [])
    if sent:
        blocks.append(f"<b>Odoslané schémy ({len(sent)}):</b>")
        blocks.extend(f"  → {_html.escape(s)}" for s in sent)
        blocks.append("")
    msgs = res.get("messages", [])
    if msgs:
        blocks.append(f"<b>Príchodzie zprávy ({len(msgs)}):</b>")
        for i, m in enumerate(msgs):
            blocks.append(f"  ← [{i+1}] {_html.escape(m)}")
        blocks.append("")
    if res.get("errors"):
        blocks.append("<b>Errors:</b>")
        blocks.extend(f"  {_html.escape(e)}" for e in res["errors"])
    body = "\n".join(blocks) or "(žiadne zprávy ani chyby)"
    msg_html = (f"🔌 WebSocket listen probe ({len(msgs)} server msgs)<br>"
                f"<pre style='background:#fff;padding:10px;border-radius:6px;border:1px solid #ddd;"
                f"font-size:11px;line-height:1.4;overflow-x:auto;white-space:pre-wrap;"
                f"max-height:800px'>{body}</pre>")
    return _realio_page(msg_html, "ok")


@app.post("/realio/disable_control", response_class=HTMLResponse)
def realio_disable_control():
    """Vypne externé riadenie batérie — Manual_Plan = disable_value (typicky 0).
    Vyžaduje active profile.mode='real'."""
    err = _require_real_profile()
    if err:
        return _realio_page(f"⛔ Disable control zamietnutý: {err}", "err")
    try:
        import realio as _rio
    except ImportError:
        return _realio_page("realio modul nedostupný", "err")
    res = _rio.disable_battery_control(source="manual_ui")
    if res.get("ok"):
        return _realio_page(res.get("msg", "✓ Manual control vypnutý"), "ok")
    return _realio_page(f"⚠ Vypnutie zlyhalo: {res.get('msg','?')}", "err")


@app.get("/data", response_class=HTMLResponse)
def data_get(request: Request):
    return _data_page(request=request)


@app.post("/data", response_class=HTMLResponse)
def data_post(request: Request):
    import backfill as bf
    logs = []
    try:
        report = bf.backfill_all(log=lambda m: logs.append(str(m)))
    except Exception as e:
        logs.append(f"Chyba: {e}")
        report = []
    return _data_page(report=report, logs=logs, request=request)


@app.post("/plan", response_class=HTMLResponse)
def plan(date: str = Form(...), lat: float = Form(...), lon: float = Form(...),
         kwp: float = Form(...), tilt: float = Form(...), azimuth: float = Form(...), eff: float = Form(...),
         batt_kw: float = Form(...), batt_kwh: float = Form(...), eff_c: float = Form(...), eff_d: float = Form(...),
         soc_min: float = Form(...), soc_max: float = Form(...), soc_init: float = Form(...),
         soc_reserve_pct: float = Form(default=0.0),
         rt_grid_reserve_pct: float = Form(default=0.0),
         terminal_soc: float = Form(...), grid_kw: float = Form(...),
         grid_kw_import: float = Form(default=None), grid_kw_export: float = Form(default=None),
         grid_fee: float = Form(...),
         cycle_cost: float = Form(...), allow_grid_charge: str = Form(default=""),
         allow_curtail: str = Form(default=""),
         min_spread: float = Form(default=30.0), min_trade: float = Form(default=0.0),
         price_scale: float = Form(default=1.0), pv_scale: float = Form(default=1.0),
         block_neg_import: str = Form(default=""),
         no_planned_discharge: str = Form(default=""),
         zco_bias_w: float = Form(default=0.0),
         # POZOR: pre checkboxy MUSÍ byť default="" — neoznačený checkbox neposiela field v POST,
         # FastAPI by inak vrátil "on" (=True) a odčiarknutie by nefungovalo.
         rt_freedom: str = Form(default=""),
         aggressive_rt: str = Form(default=""),
         ftv_balance: str = Form(default=""),
         ftv_lookahead_h: float = Form(default=4.0),
         rt_audit_horizon_h: float = Form(default=1.0),
         terminal_soc_mode: str = Form(default="fixed"),
         vdt_breakeven_auto: str = Form(default=""),
         vdt_capacity_reserve_kw: float = Form(default=0.0),
         rt_engine: str = Form(default="v1"),
         rt2_margin_min_eur: float = Form(default=10.0),
         rt2_margin_full_eur: float = Form(default=60.0),
         rt2_zco_k: float = Form(default=0.6),
         rt2_margin_min_chg_eur: str = Form(default=""),
         ftv_persistence_throttle: str = Form(default=""),
         rt_no_worsen_dev: str = Form(default=""),
         ftv_strict_plan: str = Form(default=""),
         ftv_strict_deadband_kw: float = Form(default=5.0),
         baseline_im_mode: str = Form(default="dt_x"),
         baseline_im_value: float = Form(default=1.0),
         baseline_ex_mode: str = Form(default="dt_x"),
         baseline_ex_value: float = Form(default=1.0),
         max_export_kwh_day: float = Form(default=0.0),
         max_import_kwh_day: float = Form(default=0.0),
         mult_action: str = Form(default=""),
         mult_arr: list[float] = Form(default=[]),
         rt_arr: list[str] = Form(default=[]),
         save_only: str = Form(default=""),
         # Joint LP toggle (F2)
         joint_lp_enabled: str = Form(default=""),
         joint_trade_batt: str = Form(default=""),
         joint_trade_ftv: str = Form(default=""),
         joint_trade_load: str = Form(default=""),
         joint_use_vdt: str = Form(default=""),
         joint_optimize_dist: str = Form(default=""),
         vdt_closed_from: str = Form(default=""),
         vdt_closed_to: str = Form(default="")):
    d = dt.date.fromisoformat(date)
    # #27: rozsah dní pre VDT oceňovanie reálnymi uzavretými cenami (len história).
    _vdt_cl_from = str(vdt_closed_from or "")[:10]
    _vdt_cl_to = str(vdt_closed_to or "")[:10]
    # Stropy denného obchodovania (kWh/deň). 0 alebo záporné = bez stropu.
    _mex = float(max_export_kwh_day) if max_export_kwh_day and max_export_kwh_day > 0 else None
    _mim = float(max_import_kwh_day) if max_import_kwh_day and max_import_kwh_day > 0 else None
    agc = bool(allow_grid_charge)
    acu = bool(allow_curtail)
    npd = bool(no_planned_discharge)
    zbw = float(zco_bias_w or 0.0)
    # Bug #622 (Krok A): SOC carryover z livesim trace pre /plan POST.
    # Override user-vstupu `soc_init` reálnym SOC po predošlom dni.
    # Bug SOC-INIT-PERSIST (2026-06-10): rozdeliť na 2 premenné — carried
    # použiť LEN pre tento konkrétny LP beh, user manual hodnota zostane
    # uložená v profile (= východisko pre buduce dni). Predtým sa carried
    # zapisovala do profilu cez pr.save_profile → user nevidel svoju zadanú
    # hodnotu po každom auto-tick-u.
    _soc_init_carry_p, _soc_init_src_p = _resolve_soc_init_carryover(
        date, {"soc_init": soc_init, "soc_min": soc_min, "soc_max": soc_max}, case="plan_d1")
    soc_init_user = soc_init   # zachovaj user manual hodnotu pre save_profile
    if _soc_init_src_p == "carried":
        print(f"[#622 /plan POST] {date}: soc_init={soc_init:.1f}% → "
              f"carried {_soc_init_carry_p:.1f}% (LP only, profile zostáva {soc_init_user:.1f}%)")
        soc_init = _soc_init_carry_p
    rtf = bool(rt_freedom)
    aggr = bool(aggressive_rt)                                  # default False; ak True → RT bez cycle budgetu
    fbal = bool(ftv_balance)                                    # default True; ak True → FTV-driven RT balansovanie
    flah = max(0.0, min(12.0, float(ftv_lookahead_h or 4.0)))   # 0..12 hodín lookahead
    rah = max(0.0, min(12.0, float(rt_audit_horizon_h or 1.0)))   # 0..12 hodín audit horizon
    tsm = "next_day_price" if str(terminal_soc_mode) == "next_day_price" else "fixed"
    vba = bool(vdt_breakeven_auto)                              # auto breakeven prah pre VDT advisor
    vcr = max(0.0, float(vdt_capacity_reserve_kw or 0.0))       # kW headroom pre VDT/RT v D-1 LP
    # RT poradca 2.0: voľba enginu sa ukladá do PROFILU rt sekcie (livesim ju číta odtiaľ)
    try:
        import profiles as _pr_rteS
        _prof_rteS = ps.resolve_profile() if ps is not None else None
        if _prof_rteS and _prof_rteS != "default":
            _eng_new = str(rt_engine) if str(rt_engine) in ("v2", "v3") else "v1"
            _pobj_rte = _pr_rteS.load_profile(_prof_rteS) or {}
            _rt_sec = _pobj_rte.get("rt") or {}
            try:
                _mchg_new = float(rt2_margin_min_chg_eur) if str(rt2_margin_min_chg_eur).strip() else None
            except (TypeError, ValueError):
                _mchg_new = None
            _rt2_new = dict(engine=_eng_new,
                            rt2_margin_min_eur=max(0.0, float(rt2_margin_min_eur or 10.0)),
                            rt2_margin_full_eur=max(1.0, float(rt2_margin_full_eur or 60.0)),
                            rt2_zco_k=max(0.0, float(rt2_zco_k or 0.6)),
                            rt2_margin_min_chg_eur=_mchg_new)
            if any(_rt_sec.get(k) != v for k, v in _rt2_new.items()):
                _rt_sec.update(_rt2_new)
                _pobj_rte["rt"] = _rt_sec
                _pr_rteS.save_profile(_prof_rteS, _pobj_rte)
                print(f"[RT-ENGINE] profil {_prof_rteS}: {_rt2_new}")
    except Exception as _e_rteS:
        print(f"[RT-ENGINE] uloženie voľby zlyhalo: {_e_rteS}")
    fpth = bool(ftv_persistence_throttle)                       # default True; persistencia throttle
    rnwd = bool(rt_no_worsen_dev)                               # default True; RT nesmie zhoršovať threshold
    fsp = bool(ftv_strict_plan)                                 # default True; FTV-balance vždy fire keď pre_dev≠0
    fsdb = max(0.0, min(100.0, float(ftv_strict_deadband_kw or 5.0)))  # deadband pre strict_plan override
    # asymetrické limity siete: ak prázdne, použiť grid_kw (backward compat)
    gki = float(grid_kw_import) if grid_kw_import is not None else float(grid_kw)
    gke = float(grid_kw_export) if grid_kw_export is not None else float(grid_kw)
    # ── ručné násobitele + RT mask (uloženie/vyčistenie + načítanie effective) ──
    # KAŽDÝ submit /plan ukladá × a RT z formulára ako globálnu šablónu pre profil.
    # Per-day prepis tým zaniká (šablóna platí pre VŠETKY dni rovnako).
    if not mult_action and mult_arr and len(mult_arr) == 24:
        mult_action = "save_template"
    mult_msg, mult24, rt_mask24 = _handle_mult_action(date, 60, mult_arr, mult_action, rt_arr)
    # baseline params (bezpečné: dt_x|fix, hodnoty > 0)
    _bim_mode = baseline_im_mode if baseline_im_mode in ("dt_x", "fix") else "dt_x"
    _bex_mode = baseline_ex_mode if baseline_ex_mode in ("dt_x", "fix") else "dt_x"
    _bim_val = max(0.0, float(baseline_im_value or 1.0))
    _bex_val = max(0.0, float(baseline_ex_value or 1.0))
    # Joint LP flags z form
    _joint_flags = {
        "enabled": bool(joint_lp_enabled),
        "trade_batt": bool(joint_trade_batt),
        "trade_ftv": bool(joint_trade_ftv),
        "trade_load": bool(joint_trade_load),
        "use_vdt": bool(joint_use_vdt),
        "optimize_distribution": bool(joint_optimize_dist),
    }
    # Bug SOC-INIT-PERSIST: do ui_settings/profile sa zapisuje soc_init_user
    # (= manuálna hodnota zo formulára), NIE carried po Bug #622 override.
    # Carried platí iba pre tento konkrétny LP beh, profile zostáva s manualom.
    _ui_save("plan", dict(lat=lat, lon=lon, kwp=kwp, tilt=tilt, azimuth=azimuth, eff=eff,
                          batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
                          soc_min=soc_min, soc_max=soc_max, soc_init=soc_init_user, terminal_soc=terminal_soc,
                          soc_reserve_pct=float(soc_reserve_pct or 0.0),
                          rt_grid_reserve_pct=float(rt_grid_reserve_pct or 0.0),
                          grid_kw=grid_kw, grid_kw_import=gki, grid_kw_export=gke,
                          grid_fee=grid_fee, cycle_cost=cycle_cost,
                          min_spread=min_spread, min_trade=min_trade,
                          price_scale=price_scale, pv_scale=pv_scale, allow_curtail=acu,
                          allow_grid_charge=agc, block_neg_import=bool(block_neg_import),
                          no_planned_discharge=npd, zco_bias_w=zbw, rt_freedom=rtf,
                          aggressive_rt=aggr, ftv_balance=fbal,
                          ftv_lookahead_h=flah,
                          rt_audit_horizon_h=rah,  # Bug AUDIT-HORIZON-SAVE (2026-06-11): chýbal v _ui_save → neuložil sa do šablóny
                          terminal_soc_mode=tsm,
                          vdt_breakeven_auto=vba,
                          vdt_capacity_reserve_kw=vcr,
                          ftv_persistence_throttle=fpth,
                          rt_no_worsen_dev=rnwd, ftv_strict_plan=fsp,
                          ftv_strict_deadband_kw=fsdb,
                          max_export_kwh_day=float(max_export_kwh_day or 0),
                          max_import_kwh_day=float(max_import_kwh_day or 0),
                          baseline_im_mode=_bim_mode, baseline_im_value=_bim_val,
                          baseline_ex_mode=_bex_mode, baseline_ex_value=_bex_val,
                          vdt_closed_from=_vdt_cl_from, vdt_closed_to=_vdt_cl_to,  # #27
                          joint_lp=_joint_flags))
    # Uloženie joint_lp flags do aktívneho profilu (pre lookup z iných miest)
    try:
        import plan_store as _ps_jl
        import joint_lp_integration as _jli_save
        _active_jl = _ps_jl.resolve_profile() or "default"
        if _active_jl and _active_jl != "default":
            _jli_save.save_flags_to_profile(_active_jl, _joint_flags)
    except Exception as _e_jls:
        print(f"[/plan] save joint_lp flags do profilu zlyhalo: {_e_jls}")
    # SYNC: shared parametre tiež do ui_settings.dentrh aby /plan a /dentrh ostali konzistentné
    _SHARED_SYNC = dict(lat=lat, lon=lon, kwp=kwp, tilt=tilt, azimuth=azimuth, eff=eff,
                          batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
                          soc_min=soc_min, soc_max=soc_max, soc_init=soc_init_user, terminal_soc=terminal_soc,
                          soc_reserve_pct=float(soc_reserve_pct or 0.0),
                          rt_grid_reserve_pct=float(rt_grid_reserve_pct or 0.0),
                          grid_kw=grid_kw, grid_kw_import=gki, grid_kw_export=gke,
                          grid_fee=grid_fee, cycle_cost=cycle_cost, min_spread=min_spread,
                          max_export_kwh_day=float(max_export_kwh_day or 0),
                          max_import_kwh_day=float(max_import_kwh_day or 0),
                          zco_bias_w=zbw)
    _dentrh_existing = _ui_load("dentrh", {}) or {}
    _dentrh_existing.update(_SHARED_SYNC)
    _ui_save("dentrh", _dentrh_existing)
    _autosave_active_profile()      # zmeny v /plan ihneď premietnuť do aktívneho profilu (ak je)
    _clear_livesim_logs()           # invalidate cached livesim log — fresh prepočet pri ďalšom otvorení /livesim
    # synchronizuj orezanie do prípadu 'realistic' → prejaví sa aj v živej simulácii (jeden zdroj pravdy)
    try:
        _rc = cc.load_case("realistic")
        if bool(_rc.allow_curtail) != acu:
            _rc.allow_curtail = acu
            cc.save_case(_rc)
            for _p in __import__("glob").glob("out/livesim_*"):
                try:
                    os.remove(_p)
                except OSError:
                    pass
    except Exception:
        pass
    # ── Save-only režim: skončiť tu, vrátiť potvrdzovaciu stránku ──
    # User klikol 💾 "Uložiť do profilu" → uložili sme ui_settings + template + autosave profilu,
    # ale plán SA NEGENERUJE (užívateľ ho vygeneruje neskôr osobitne).
    if save_only:
        try:
            import profiles as _pr
            _active = _pr.get_active() or "—"
        except Exception:
            _active = "—"
        return f"""<!doctype html><html lang="sk"><head><meta charset="utf-8"><title>Uložené</title>
<meta http-equiv="refresh" content="2;url=/">
<style>body{{font-family:-apple-system,Segoe UI,Arial;max-width:680px;margin:48px auto;padding:0 16px;color:#222;text-align:center}}
.ok{{background:#e8f5e9;border-left:4px solid #2E7D32;border-radius:8px;padding:18px;margin:20px 0;color:#1B5E20;text-align:left}}
a{{color:#1F4E78}}</style></head><body>
<h1 style="color:#2E7D32">✓ Uložené do profilu</h1>
<div class="ok"><b>Uložené:</b><br>
• Parametre /plan (ui_settings.plan)<br>
• × a RT šablóna pre profil <b>{_active}</b> (out/plan_overrides/{_active}/_template.json)<br>
• Snapshot profilu (out/profiles/{_active}.json)<br><br>
{mult_msg if mult_msg else ""}</div>
<p>Plán som <b>NEGENEROVAL</b>. Vráť sa na <a href="/">/plan</a> a klikni „Generuj plán D-1" keď chceš vygenerovať plán s týmito nastaveniami.</p>
<p style="color:#666;font-size:13px">(Auto-redirect za 2 s na /plan…)</p>
</body></html>"""
    try:
        # Pre batt-only profily (kwp=0, napr. Trakany_real) PVF fetch nedáva zmysel —
        # vytvoríme syntetický wx grid s 0 kW + neutrálne počasie pre ISOT predikciu.
        if float(kwp or 0) > 0.01:
            wx = _fetch_pv_cached(lat, lon, kwp, tilt, azimuth, eff, start=d, end=d)
            wx["time"] = pd.to_datetime(wx["time"]); wx = wx[wx.time.dt.date == d].copy()
            if wx.empty:
                return form_page(f"Pre {d} nie sú dostupné dáta predpovede.")
        else:
            wx = pd.DataFrame({
                "time": pd.date_range(pd.Timestamp(d), periods=24, freq="h"),
                "kw": np.zeros(24), "gti": np.zeros(24),
                "temp": np.full(24, 15.0), "cloud": np.full(24, 50.0),
            })
        hist = _isot_history(d, days=8)                       # história cien pre lagy
        wx2 = wx[["time", "gti", "temp", "cloud"]].copy(); wx2["isot_eur"] = np.nan
        h2 = hist.copy()
        for c in ["gti", "temp", "cloud"]:
            h2[c] = np.nan
        ctx = pd.concat([h2[["time", "isot_eur", "gti", "temp", "cloud"]], wx2], ignore_index=True)
        pred = _model().predict(ctx)
        dayp = pred[pred.time.dt.date == d][["time", "pred_isot", "p_neg"]]
        day = wx.merge(dayp, on="time").sort_values("time")
        if len(day) < 24:
            return form_page(f"Pre {d} sa nepodarilo zostaviť celý deň predikcie.")
        cal = _cal_for(d)
        pv_arr = day.kw.values * cal * pv_scale               # mesačná kalibrácia × manuálna korekcia
        price_arr = day.pred_isot.values * price_scale        # korekcia ceny
        # voliteľná deviation-bias úprava ceny pre rozhodovanie (zúčtovanie na pôvodnej price_arr)
        decision_price = lsim._apply_zco_bias(price_arr, d, float(pv_arr.sum()), 60, zbw)
        # ── spotreba zákazníka (load) z naimportovaného profilu pre tento dátum (weekday/weekend) ──
        # load_profile.load_for_date vracia 96 × kW (15-min) → agregujeme na 24 × kWh (hodinový plán)
        load24 = None
        if lp is not None and lp.has_data():
            try:
                _load96_kw = lp.load_for_date(d.isoformat())                  # 96 × kW
                # 15-min priemer → kWh za 15 min: kW × 0.25; hodinové sumovanie: sum 4 slotov × 0.25
                load24 = _load96_kw.reshape(24, 4).mean(axis=1) * 1.0          # priemer kW × 1h = kWh/hod
            except Exception:
                load24 = None
        # baseline (NÁVRH) — bez akéhokoľvek overridu, na porovnanie s FINÁL
        # Joint LP integrácia: ak _joint_flags["enabled"], použije sa optimize_joint_day
        from joint_lp_integration import optimize_day_or_joint as _od_or_joint
        # Bug TERMINAL-SOC-MODE: efektívny terminál podľa zajtrajších cien (generické z form/profilu)
        _term_eff = _resolve_terminal_soc(d.isoformat(),
                                          dict(terminal_soc=terminal_soc, terminal_soc_mode=tsm,
                                               eff_c=eff_c, eff_d=eff_d, cycle_cost=cycle_cost,
                                               grid_fee=grid_fee, soc_max=soc_max),
                                          price_arr)
        # Bug VDT-CAP-RESERVE-HIST: rezerva pre intraday len pre dnešok/budúcnosť
        _vcr_eff_p = vcr if d >= dt.date.today() else 0.0
        # Bug LP-VDT-BOUNDS: uzavreté VDT obchody dňa = smerové stropy pre LP
        from joint_lp_integration import vdt_committed_kw_for_day as _vdtb_p
        try:
            import plan_store as _ps_vb
            _prof_vb = _ps_vb.resolve_profile() or "default"
        except Exception:
            _prof_vb = "default"
        _vdt_committed_p = _vdtb_p(_prof_vb, d.isoformat(), T=24, step_min=60)
        sch_base, summ_base = _od_or_joint(pv_arr, decision_price,
                                 joint_flags=_joint_flags,
                                 vdt_committed_kw=_vdt_committed_p,
                                 vdt_capacity_reserve_kw=_vcr_eff_p,
                                 settle_price=price_arr,
                                 batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
                                 soc_min_pct=soc_min, soc_max_pct=soc_max, soc_init_pct=soc_init,
                                 soc_reserve_pct=float(soc_reserve_pct or 0.0),
                                 rt_grid_reserve_pct=float(rt_grid_reserve_pct or 0.0),
                                 grid_kw=grid_kw, grid_kw_import=gki, grid_kw_export=gke,
                                 grid_fee=grid_fee, cycle_cost=cycle_cost,
                                 allow_grid_charge=agc, terminal_soc_pct=_term_eff,
                                 allow_curtail=acu,
                                 min_spread_eur=min_spread, min_trade_mwh=min_trade,
                                 block_neg_import=bool(block_neg_import),
                                 block_planned_discharge=npd,
                                 load_kwh=load24,
                                 max_export_kwh_day=_mex, max_import_kwh_day=_mim)
        # ak override nemá efekt (všetko 1.0), ušetríme druhý LP run
        if np.allclose(np.asarray(mult24, dtype=float), 1.0):
            sch, summ = sch_base, summ_base
        else:
            sch, summ = _od_or_joint(pv_arr, decision_price,
                                 joint_flags=_joint_flags,
                                 vdt_committed_kw=_vdt_committed_p,
                                 vdt_capacity_reserve_kw=_vcr_eff_p,
                                 settle_price=price_arr,
                                 batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
                                 soc_min_pct=soc_min, soc_max_pct=soc_max, soc_init_pct=soc_init,
                                 soc_reserve_pct=float(soc_reserve_pct or 0.0),
                                 rt_grid_reserve_pct=float(rt_grid_reserve_pct or 0.0),
                                 grid_kw=grid_kw, grid_kw_import=gki, grid_kw_export=gke,
                                 grid_fee=grid_fee, cycle_cost=cycle_cost,
                                 allow_grid_charge=agc, terminal_soc_pct=_term_eff,
                                 allow_curtail=acu,
                                 min_spread_eur=min_spread, min_trade_mwh=min_trade,
                                 block_neg_import=bool(block_neg_import),
                                 batt_kw_override=mult24,
                                 block_planned_discharge=npd,
                                 load_kwh=load24,
                                 max_export_kwh_day=_mex, max_import_kwh_day=_mim)
    except Exception as e:
        return form_page(f"Chyba pri generovaní: {e}")

    meta = dict(date=date, lat=lat, lon=lon, kwp=kwp, tilt=tilt, azimuth=azimuth, eff=eff,
                batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
                soc_min_pct=soc_min, soc_max_pct=soc_max, soc_init_pct=soc_init,
                soc_reserve_pct=float(soc_reserve_pct or 0.0),
                rt_grid_reserve_pct=float(rt_grid_reserve_pct or 0.0),
                grid_kw=grid_kw, grid_kw_import=gki, grid_kw_export=gke,
                grid_fee=grid_fee, cycle_cost=cycle_cost,
                allow_grid_charge=agc, terminal_soc_pct=terminal_soc, allow_curtail=acu,
                min_spread_eur=min_spread, min_trade_mwh=min_trade,
                price_scale=price_scale, pv_scale=pv_scale, block_neg_import=bool(block_neg_import),
                no_planned_discharge=npd, zco_bias_w=zbw, rt_freedom=rtf,
                aggressive_rt=aggr, ftv_balance=fbal, ftv_lookahead_h=flah,
                rt_audit_horizon_h=rah,
                ftv_persistence_throttle=fpth, rt_no_worsen_dev=rnwd,
                ftv_strict_plan=fsp, ftv_strict_deadband_kw=fsdb,
                baseline_im_mode=_bim_mode, baseline_im_value=_bim_val,
                baseline_ex_mode=_bex_mode, baseline_ex_value=_bex_val)
    xls = f"out/plan_{date}.xlsx"
    build_plan_excel(sch, summ, meta, xls)
    # uloženie plánu do plan_store — livesim si ho odtiaľto načíta v strict režime
    plan_saved_path = None
    if ps is not None:
        try:
            sched_cols = ["batt_kw", "grid_kwh", "pv_kwh", "price_eur", "curtail_kwh",
                          "soc_pct", "order_mwh", "_charge_kw", "_discharge_kw",
                          "_export_kwh", "_import_kwh", "soc_kwh", "load_kwh"]
            sched_dict = {c: sch[c].tolist() for c in sched_cols if c in sch.columns}
            # rt_mask effective: ak rt_freedom=False, RT iba kde plán ≠ 0 (≥ 0.5 kW absolútne).
            # AŽ-NA: keď je celý plán batérie nulový (× všade = 0 alebo LP nič nenašiel), nezarezávame
            # rt_mask — inak by sa RT úplne zablokovala. V tomto prípade je rt_freedom=False redundantné
            # (niet plánu, ktorý by mal RT kotviť) → necháme rt_mask šablónu nedotknutú.
            _bk = np.asarray(sch["batt_kw"].values, float)
            _rtm = np.asarray(rt_mask24, dtype=float).reshape(-1)
            _plan_is_empty = bool(np.all(np.abs(_bk) < 0.5))
            if not rtf and not _plan_is_empty:
                _rtm = np.where(np.abs(_bk) > 0.5, _rtm, 0.0)
            plan_saved_path = ps.save_plan(
                date, 60, "plan", params=meta, schedule=sched_dict, summary=summ,
                mults=list(map(float, np.asarray(mult24).reshape(-1))),
                rt_mask=list(map(float, _rtm)),
                block_planned_discharge=npd, zco_bias_w=zbw, rt_freedom=rtf,
                meta=dict(source="/plan", price_kind="predicted"))
        except Exception as _e:
            plan_saved_path = f"ERR: {_e}"

    # === KROK 1 (15-MIN MERGE, 2026-06-18): paralelne vygeneruj + ulož 15-min plán ===
    # ADITÍVNE — 60-min flow vyššie (display/Excel/save) ostáva NEDOTKNUTÝ. 15-min je
    # canonical pre livesim (Krok 2). Inputy upsamplnuté z hodinovej predikcie:
    # CENA sa KOPÍRUJE (rovnaká v hodine), ENERGIA (PV/load) /4. Reálne 15-min OTE ceny
    # doplníme neskôr; hodinový pohľad = priemer 15-min. Fail-safe: try/except → ak 15-min
    # zlyhá, 60-min plán ostáva v platnosti.
    try:
        from core.granularity import (upsample_price_h_to_15 as _up_px,
                                       upsample_series_h_to_15 as _up_ser,
                                       upsample_mask_h_to_15 as _up_msk)
        _dprice15 = _up_px(decision_price)
        _sprice15 = _up_px(price_arr)
        _pv15 = _up_ser(pv_arr, divide=True)
        _load15 = _up_ser(load24, divide=True) if load24 is not None else None
        _mult96 = _up_msk(mult24)
        _rt96 = _up_msk(rt_mask24)
        _vdt_committed_15 = _vdtb_p(_prof_vb, d.isoformat(), T=96, step_min=15)
        _has_mult = not np.allclose(np.asarray(mult24, dtype=float), 1.0)
        _sch15, _summ15 = _od_or_joint(
            _pv15, _dprice15, joint_flags=_joint_flags,
            vdt_committed_kw=_vdt_committed_15, vdt_capacity_reserve_kw=_vcr_eff_p,
            settle_price=_sprice15, batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
            soc_min_pct=soc_min, soc_max_pct=soc_max, soc_init_pct=soc_init,
            soc_reserve_pct=float(soc_reserve_pct or 0.0),
            rt_grid_reserve_pct=float(rt_grid_reserve_pct or 0.0),
            grid_kw=grid_kw, grid_kw_import=gki, grid_kw_export=gke,
            grid_fee=grid_fee, cycle_cost=cycle_cost, allow_grid_charge=agc,
            terminal_soc_pct=_term_eff, allow_curtail=acu,
            min_spread_eur=min_spread, min_trade_mwh=min_trade,
            block_neg_import=bool(block_neg_import), block_planned_discharge=npd,
            load_kwh=_load15, dt=0.25, max_export_kwh_day=_mex, max_import_kwh_day=_mim,
            batt_kw_override=(_mult96 if _has_mult else None))
        if ps is not None:
            _scols15 = ["batt_kw", "grid_kwh", "pv_kwh", "price_eur", "curtail_kwh",
                        "soc_pct", "order_mwh", "_charge_kw", "_discharge_kw",
                        "_export_kwh", "_import_kwh", "soc_kwh", "load_kwh"]
            _sched15 = {c: _sch15[c].tolist() for c in _scols15 if c in _sch15.columns}
            _bk15 = np.asarray(_sch15["batt_kw"].values, float)
            _rtm15 = np.asarray(_rt96, dtype=float).reshape(-1)
            if not rtf and not bool(np.all(np.abs(_bk15) < 0.5)):
                _rtm15 = np.where(np.abs(_bk15) > 0.5, _rtm15, 0.0)
            ps.save_plan(date, 15, "dentrh", params=meta, schedule=_sched15, summary=_summ15,
                         mults=list(map(float, np.asarray(_mult96).reshape(-1))),
                         rt_mask=list(map(float, _rtm15)),
                         block_planned_discharge=npd, zco_bias_w=zbw, rt_freedom=rtf,
                         meta=dict(source="/plan", price_kind="predicted_upsampled_15min"))
            print(f"[15-MIN] {date}: 15-min plán uložený paralelne (96 slotov, zisk={_summ15.get('ZISK_EUR', _summ15.get('zisk_eur', 0)):.1f})")
    except Exception as _e15:
        print(f"[15-MIN] {date}: 15-min plán zlyhal (60-min ostáva v platnosti): {_e15}")

    rows = ""
    mult24_arr = np.asarray(mult24, dtype=float).reshape(-1)
    rt24_arr = np.asarray(rt_mask24, dtype=float).reshape(-1)
    base_kw = sch_base["batt_kw"].values.astype(float)
    for i, (_, r) in enumerate(sch.iterrows()):
        pc = "#C00000" if r.price_eur < 0 else "#222"
        bt = ("#2E7D32" if r.batt_kw > 0 else ("#C49000" if r.batt_kw < 0 else "#999"))
        bk = float(base_kw[i]) if i < len(base_kw) else 0.0
        bbt = ("#2E7D32" if bk > 0 else ("#C49000" if bk < 0 else "#999"))
        mv = float(mult24_arr[i]) if i < len(mult24_arr) else 1.0
        rt_on = (rt24_arr[i] > 0.5) if i < len(rt24_arr) else True
        m_cls = "" if abs(mv - 1.0) < 1e-6 else " style='background:#fff7e6'"
        rt_cls = "" if rt_on else " style='background:#ffe2e2'"
        diff = abs(float(r.batt_kw) - bk)
        fin_emp = " style='font-weight:700;background:#f0f7e6'" if diff > 0.5 else f" style='color:{bt}'"
        # × a RT už NIE SÚ editovateľné v detail tabuľke — sú nastavené v /plan FORM editore (= šablóna profilu).
        # V detaile len read-only zobrazujeme čo bolo aplikované.
        _load_val = float(r.load_kwh) if hasattr(r, 'load_kwh') and 'load_kwh' in sch.columns else 0.0
        _load_cls = "" if _load_val < 0.05 else " style='color:#7030A0;font-weight:600'"
        rows += (f"<tr><td>{int(r.hour):02d}</td><td>{r.pv_kwh:.1f}</td>"
                 f"<td{_load_cls}>{_load_val:.1f}</td>"
                 f"<td style='color:{pc}'>{r.price_eur:.1f}</td>"
                 f"<td style='color:{bbt};font-size:12px'>{bk:+.1f}</td>"
                 f"<td{fin_emp}>{r.batt_kw:+.1f}</td>"
                 f"<td>{r.grid_kwh:+.1f}</td>"
                 f"<td style='font-weight:600'>{r.order_mwh:+.3f}</td>"
                 f"<td>{r.curtail_kwh:.1f}</td><td>{r.soc_pct:.0f}</td>"
                 f"<td{m_cls}>{mv:.2f}</td>"
                 f"<td{rt_cls} style='text-align:center'>{'✓' if rt_on else '✗'}</td></tr>")
    # ── BASELINE: scenár BEZ batérie a BEZ plánu (net-meter, FTV pokryje load najprv, zostatok DT×k alebo fix) ──
    baseline_card = ""
    if bc is not None:
        try:
            # Bug VV (2026-06-08): baseline must include TOU + grid_fee — rovnaké ceny ako plán
            _bl_tou_p = None
            _bl_gf_p = 0.0
            try:
                import settlement as _stl_blp
                from core.profile_resolver import get_active as _ga_blp
                _prof_blp = _ga_blp()
                if _stl_blp.profile_uses_tou(_prof_blp):
                    import datetime as _dt_blp
                    _bl_tou_p = _stl_blp.get_tou_for_day(
                        _prof_blp, _dt_blp.date.today().isoformat(),
                        T=24, dt_h=1.0)
                _bl_gf_p = float(f.get("grid_fee", 0.0) or 0.0)
            except Exception:
                pass
            _bl = bc.compute_baseline_day(
                pv_arr, (load24 if load24 is not None else np.zeros(24)),
                price_arr,                                                   # zúčtovacia DT cena
                im_mode=_bim_mode, im_val=_bim_val,
                ex_mode=_bex_mode, ex_val=_bex_val, dt=1.0,
                tou_eur_per_mwh=_bl_tou_p, grid_fee_eur_per_mwh=_bl_gf_p)
            _bl_net = float(_bl["net_profit"])
            _benefit = float(summ.get("ZISK_EUR", 0.0)) - _bl_net
            _bl_info = (f"Bez batérie/plánu: import {_bl['import_kwh']:.0f} kWh ({_bl['cost']:.1f} €), "
                         f"export {_bl['export_kwh']:.0f} kWh ({_bl['revenue']:.1f} €), "
                         f"self-cons {_bl['self_cons_kwh']:.0f} kWh, NET {_bl_net:+.1f} €")
            _bl_mode_txt = (f"Import: {('DT×' + str(_bim_val)) if _bim_mode == 'dt_x' else (str(_bim_val) + ' €/MWh fix')} · "
                             f"Export: {('DT×' + str(_bex_val)) if _bex_mode == 'dt_x' else (str(_bex_val) + ' €/MWh fix')}")
            baseline_card = (
                f"<div style='background:#fff3cd;border:1px solid #ffe399;border-radius:10px;padding:10px 14px'>"
                f"<div style='font-size:12px;color:#7a5d00'>Baseline (bez batérie + plánu)</div>"
                f"<div style='font-size:20px;font-weight:600;color:#7a5d00'>{_bl_net:+.1f} €</div>"
                f"<div style='font-size:11px;color:#888'>{_bl_mode_txt}</div></div>"
                f"<div style='background:#e8f5e9;border-radius:10px;padding:10px 14px'>"
                f"<div style='font-size:12px;color:#1B5E20'>Prínos batérie + plánu</div>"
                f"<div style='font-size:20px;font-weight:600;color:#2E7D32'>{_benefit:+.1f} €</div>"
                f"<div style='font-size:11px;color:#888'>= ZISK − Baseline</div></div>")
        except Exception as _e:
            baseline_card = (f"<div style='background:#ffe8e0;border-radius:10px;padding:10px 14px'>"
                               f"<div style='font-size:12px;color:#7a1810'>Baseline error</div>"
                               f"<div style='font-size:12px'>{_e}</div></div>")
    cards = "".join(
        f"<div style='background:#f3f6fb;border-radius:10px;padding:10px 14px'>"
        f"<div style='font-size:12px;color:#666'>{lab}</div>"
        f"<div style='font-size:20px;font-weight:600;color:{col}'>{summ[k]:.1f} €</div></div>"
        for lab, k, col in [("ZISK", "ZISK_EUR", "#2E7D32"), ("Bez batérie (LP)", "bez_baterie_EUR", "#222"),
                            ("Prínos batérie", "prinos_baterie_EUR", "#1F4E78")]) + baseline_card
    # ─── Joint LP Settlement card (F4) ──────────────────────────────────────────
    # Keď bol plán generovaný cez joint_lp (enabled=True), zobrazíme ekonomický rozklad:
    # FTV export, Batt arbitráž, Load import, DAM vs VDT, distribučné poplatky.
    joint_lp_card = ""
    if summ.get("_joint_lp"):
        _econ = summ.get("_joint_economics", {}) or {}
        _jflags = summ.get("_joint_flags", {}) or {}
        # Flag badges — farebne odlíšiť zapnuté/vypnuté toggle
        def _badge(label, on):
            if on:
                return (f"<span style='background:#e6f4ea;color:#1B5E20;border:1px solid #87c79d;"
                          f"padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;margin:0 3px'>"
                          f"✓ {label}</span>")
            return (f"<span style='background:#fbeaea;color:#7a1810;border:1px solid #e6b3b3;"
                      f"padding:2px 8px;border-radius:12px;font-size:11px;margin:0 3px'>"
                      f"✗ {label}</span>")
        _badges = (_badge("BAT", _jflags.get("trade_batt"))
                    + _badge("FTV", _jflags.get("trade_ftv"))
                    + _badge("LOAD", _jflags.get("trade_load"))
                    + _badge("VDT", _jflags.get("use_vdt"))
                    + _badge("DIST", _jflags.get("optimize_distribution")))
        _dam_rev = float(_econ.get("dam_revenue_eur", 0))
        _dam_cost = float(_econ.get("dam_cost_eur", 0))
        _vdt_rev = float(_econ.get("vdt_revenue_eur", 0))
        _vdt_cost = float(_econ.get("vdt_cost_eur", 0))
        _fee = float(_econ.get("grid_fee_eur", 0))
        _cyc = float(_econ.get("cycle_cost_eur", 0))
        _tou = float(_econ.get("tou_cost_eur", 0))
        _net = float(_econ.get("net_profit_eur", 0))
        _rev_total = _dam_rev + _vdt_rev
        _cost_total = _dam_cost + _vdt_cost + _fee + _cyc + _tou
        # Tabuľka rozkladu
        def _row(label, val, kind):
            color = "#2E7D32" if kind == "rev" else "#C0392B" if kind == "cost" else "#1F4E78"
            sign = "+" if kind == "rev" else "−" if kind == "cost" else ""
            return (f"<tr><td style='padding:4px 10px;color:#555'>{label}</td>"
                      f"<td style='padding:4px 10px;text-align:right;color:{color};font-weight:600;"
                      f"font-variant-numeric:tabular-nums'>{sign}{abs(val):.2f} €</td></tr>")
        _tou_row = (_row(("TOU + TPS + SS + OZE (distribúcia)" if _jflags.get("optimize_distribution")
                          else "TOU (distribúcia vypnutá)"), _tou, "cost")
                     if _tou > 0.001 or _jflags.get("optimize_distribution") else "")
        # Distribučná úspora — koľko sme ušetrili oproti baseline (load bez batt arbitráže)
        _tou_base = float(_econ.get("tou_baseline_eur", 0))
        _tou_sav = float(_econ.get("tou_savings_eur", 0))
        _tou_savings_row = ""
        if _jflags.get("optimize_distribution") and (_tou_base > 0.001 or abs(_tou_sav) > 0.001):
            _sav_color = "#2E7D32" if _tou_sav >= 0 else "#C0392B"
            _sav_sign = "+" if _tou_sav >= 0 else "−"
            _tou_savings_row = (
                f"<tr><td style='padding:4px 10px;color:#555'>"
                f"<span title='Baseline = TOU × load (bez batt arbitráže). "
                f"Úspora = baseline − aktuálne. Vyšší export/lepšie načasovanie spotreby zvyšuje úsporu.'>"
                f"Úspora distribúcie (baseline {_tou_base:.2f} €)</span></td>"
                f"<td style='padding:4px 10px;text-align:right;color:{_sav_color};font-weight:600;"
                f"font-variant-numeric:tabular-nums'>{_sav_sign}{abs(_tou_sav):.2f} €</td></tr>"
            )
        _vdt_rows = ""
        if _jflags.get("use_vdt"):
            _vdt_rows = (_row("VDT predaj (export)", _vdt_rev, "rev")
                          + _row("VDT nákup (import)", _vdt_cost, "cost"))
        joint_lp_card = (
            f"<div style='background:#eef5ff;border:1px solid #b9d4ec;border-radius:10px;"
            f"padding:12px 16px;margin:12px 0'>"
            f"<div style='display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px'>"
            f"<div style='font-size:14px;font-weight:600;color:#1F4E78'>🔵 Joint LP — rozklad zisku</div>"
            f"<div>{_badges}</div></div>"
            f"<table style='width:100%;margin-top:8px;border-collapse:collapse;font-size:13px;border:0'>"
            f"{_row('DAM predaj (export)', _dam_rev, 'rev')}"
            f"{_row('DAM nákup (import)', _dam_cost, 'cost')}"
            f"{_vdt_rows}"
            f"{_row('Poplatok prenos (grid_fee × import)', _fee, 'cost')}"
            f"{_row('Náklad cyklov batérie', _cyc, 'cost')}"
            f"{_tou_row}"
            f"{_tou_savings_row}"
            f"<tr><td colspan='2' style='border-top:2px solid #1F4E78;padding:6px 10px;"
            f"text-align:right;font-size:11px;color:#666'>"
            f"Σ príjmy {_rev_total:+.2f} € • Σ náklady {_cost_total:.2f} €</td></tr>"
            f"<tr><td style='padding:6px 10px;font-weight:600;font-size:14px'>NET zisk</td>"
            f"<td style='padding:6px 10px;text-align:right;font-size:16px;font-weight:700;"
            f"color:{'#2E7D32' if _net >= 0 else '#C0392B'};font-variant-numeric:tabular-nums'>"
            f"{_net:+.2f} €</td></tr>"
            f"</table>"
            f"<div style='font-size:11px;color:#888;margin-top:6px'>"
            f"Joint LP rieši FTV+Batt+Load+DAM"
            f"{'+VDT' if _jflags.get('use_vdt') else ''}"
            f"{' + Distribúcia' if _jflags.get('optimize_distribution') else ''}"
            f" naraz ako jeden LP. Toggle môžeš zmeniť v /plan formulári."
            f"</div></div>"
        )
    predaj = sch.loc[sch.order_mwh > 0, "order_mwh"].sum()
    nakup = abs(sch.loc[sch.order_mwh < 0, "order_mwh"].sum())
    info = (f"Očakávaná výroba FTV: <b>{sch.pv_kwh.sum():.0f} kWh</b> &nbsp;•&nbsp; "
            f"Orezané: <b>{summ['orezane_kWh']:.0f} kWh</b> &nbsp;•&nbsp; "
            f"Predaj spolu: <b>{predaj:.2f} MWh</b> &nbsp;•&nbsp; Nákup spolu: <b>{nakup:.2f} MWh</b>")
    if abs(pv_scale - 1) > 1e-9 or abs(price_scale - 1) > 1e-9:
        info += (f"<br><span style='color:#1F4E78'>Aplikované korekcie: "
                 f"výroba ×{pv_scale:g}, cena ×{price_scale:g}</span>")
    _cal = _cal_for(d)
    if abs(_cal - 1) > 1e-9:
        info += f"<br><span style='color:#2E7D32'>Kalibrácia výroby (nameraná, {d.strftime('%Y-%m')}): ×{_cal:.3f}</span>"
    # banner: carried SOC + stav overridov z disku + výsledok mult_action + warningy z optimize_day
    mult_banner = _carried_soc_banner(date, soc_init) + _overrides_status(date, kind="dentrh")
    if plan_saved_path and not str(plan_saved_path).startswith("ERR"):
        mult_banner += (f"<div style='background:#e8f5e9;border-left:4px solid #2E7D32;border-radius:6px;"
                        f"padding:8px 12px;margin:6px 0;color:#1B5E20;font-size:13px'>"
                        f"💾 <b>Plán uložený</b> do <code>{plan_saved_path}</code> — livesim ho odtiaľto strict-mode prečíta.</div>")
    elif plan_saved_path:
        mult_banner += (f"<div style='background:#fff3cd;border-left:4px solid #f0b80f;border-radius:6px;"
                        f"padding:8px 12px;margin:6px 0;color:#7a5c00;font-size:13px'>"
                        f"⚠ <b>Plán sa NEpodarilo uložiť:</b> {plan_saved_path}</div>")
    if mult_msg:
        _ok = mult_msg.startswith("✓")
        _bg = "#e8f5e9" if _ok else "#fff3cd"
        _bd = "#2E7D32" if _ok else "#f0b80f"
        _co = "#1B5E20" if _ok else "#7a5c00"
        mult_banner += (f"<div style='background:{_bg};border-left:4px solid {_bd};border-radius:6px;padding:10px 14px;"
                        f"margin:8px 0;color:{_co};font-size:14px;font-weight:500'>{mult_msg}</div>")
    mult_banner += _render_mult_warnings(summ)
    # skrytý form pre násobitele (inputy v tabuľke majú form='multform')
    _hidden = "".join(
        f"<input type='hidden' name='{k}' value='{v}'>" for k, v in [
            ("date", date), ("lat", lat), ("lon", lon), ("kwp", kwp), ("tilt", tilt),
            ("azimuth", azimuth), ("eff", eff), ("batt_kw", batt_kw), ("batt_kwh", batt_kwh),
            ("eff_c", eff_c), ("eff_d", eff_d), ("soc_min", soc_min), ("soc_max", soc_max),
            # Bug SOC-INIT-PERSIST-V3 (2026-06-10): hidden form posielal carried hodnotu.
            # Pri klik "Uložiť šablónu" / "Uložiť plán" sa hidden POST nesie soc_init.
            # soc_init premenná tu je už carried (prepísané v riadku 13869). Hidden musí
            # niesť soc_init_user (= user manual z formulára) inak ďalší POST uloží carried
            # do profilu/ui_settings → reload formuláru zobrazí 16.86%.
            ("soc_init", soc_init_user), ("terminal_soc", terminal_soc), ("grid_kw", grid_kw),
            ("grid_fee", grid_fee), ("cycle_cost", cycle_cost), ("min_spread", min_spread),
            ("min_trade", min_trade), ("price_scale", price_scale), ("pv_scale", pv_scale),
            # Bug HIDDEN-FORM-PARAMS (2026-06-12, user: "Uložiť šablónu prepíše na 60"):
            # hidden form NIESOL len staré polia → POST z "Uložiť šablónu" prišiel
            # s Form defaultmi pre všetko ostatné a prepísal profil/ui_settings
            # (rt2_* na defaulty, engine na v1, audit horizon na 1.0, …).
            ("soc_reserve_pct", soc_reserve_pct), ("rt_grid_reserve_pct", rt_grid_reserve_pct),
            ("max_export_kwh_day", max_export_kwh_day), ("max_import_kwh_day", max_import_kwh_day),
            ("zco_bias_w", zbw), ("ftv_lookahead_h", flah), ("rt_audit_horizon_h", rah),
            ("ftv_strict_deadband_kw", fsdb),
            ("terminal_soc_mode", tsm), ("vdt_capacity_reserve_kw", vcr),
            ("rt_engine", (str(rt_engine) if str(rt_engine) in ("v2", "v3") else "v1")),
            ("rt2_margin_min_eur", rt2_margin_min_eur),
            ("rt2_margin_full_eur", rt2_margin_full_eur),
            ("rt2_zco_k", rt2_zco_k),
            ("rt2_margin_min_chg_eur", rt2_margin_min_chg_eur),
        ])
    if agc:
        _hidden += "<input type='hidden' name='allow_grid_charge' value='1'>"
    if acu:
        _hidden += "<input type='hidden' name='allow_curtail' value='1'>"
    if block_neg_import:
        _hidden += "<input type='hidden' name='block_neg_import' value='1'>"
    if npd:
        _hidden += "<input type='hidden' name='no_planned_discharge' value='1'>"
    # Bug HIDDEN-FORM-PARAMS: checkbox flagy (prítomnosť = true)
    if vba:
        _hidden += "<input type='hidden' name='vdt_breakeven_auto' value='1'>"
    if rtf:
        _hidden += "<input type='hidden' name='rt_freedom' value='1'>"
    if aggr:
        _hidden += "<input type='hidden' name='aggressive_rt' value='1'>"
    if fbal:
        _hidden += "<input type='hidden' name='ftv_balance' value='1'>"
    if fpth:
        _hidden += "<input type='hidden' name='ftv_persistence_throttle' value='1'>"
    if rnwd:
        _hidden += "<input type='hidden' name='rt_no_worsen_dev' value='1'>"
    if fsp:
        _hidden += "<input type='hidden' name='ftv_strict_plan' value='1'>"
    body = f"""<style>
table{{border-collapse:collapse;width:100%;font-size:14px}}
table th,table td{{border:1px solid #e3e3e3;padding:5px 8px;text-align:right}}
table th{{background:var(--primary);color:#fff}}
table td:first-child{{text-align:center}}
a.btn,a.btn:visited{{display:inline-block;background:var(--success);color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;margin:12px 0}}
button.mb{{background:var(--primary);color:#fff;border:0;padding:8px 14px;border-radius:7px;cursor:pointer;margin:0 4px 0 0;font-size:13px}}
button.mb.s{{background:var(--success)}} button.mb.w{{background:#8a8a8a}} button.mb.x{{background:#aa3a3a}}
input[type=number]{{border:1px solid #ddd;border-radius:4px;padding:2px 4px}}
</style>
<h1>Plán D-1 — {date}</h1>
<div style="display:flex;gap:12px;margin:12px 0">{cards}</div>
{joint_lp_card}
<p style="background:#f8f9fb;border-radius:8px;padding:8px 12px;font-size:14px;margin:8px 0">{info}</p>
{mult_banner}
<a class="btn" href="/download?date={date}">⬇ Stiahnuť Excel</a> &nbsp; <a href="/">← Späť</a>
<table><tr><th>hod</th><th>FTV kWh</th><th title="predikovaná spotreba zákazníka z naimportovaného profilu">Load kWh</th><th>ISOT €</th><th title="návrh optimizéra bez ručnej úpravy">Návrh kW</th><th title="finálny plán po násobiteľoch a RT-maske">FINÁL kW</th><th title="net sieť po odpočte load: + export / − import">Sieť kWh</th><th>Obchod MWh</th><th>Orez. kWh</th><th>SOC %</th><th title="ručný násobiteľ návrhu optimizéra">×</th><th title="RT odchýlka povolená (✓) alebo zablokovaná (□) v slote">RT</th></tr>
{rows}</table>
<p style="color:#666;font-size:12px;margin:4px 0">
  <b>Návrh kW</b> = baseline optimizéra bez úprav &nbsp;•&nbsp;
  <b>FINÁL kW</b> = po aplikovaní násobiteľa a SOC orezu (žltozelený podklad = sa líši od návrhu) &nbsp;•&nbsp;
  <b>RT</b> = checkbox; ✓ = odchýlka môže reagovať na sys_MW; prázdne = batéria drží plán bez ohľadu na signál (červené podsvietenie).
</p>
<p style="background:#eef5e0;border-left:4px solid #2E7D32;border-radius:6px;padding:10px 14px;margin:14px 0;font-size:13px;color:#1B5E20">
  ℹ <b>× a RT sa editujú v <a href='/' style='color:#1B5E20;font-weight:600'>/plan formulári</a></b> (sekcia "× a RT šablóna"), nie tu v detaile.
  Šablóna platí pre celý aktívny profil — pre všetky dni rovnako.
</p>
<p style="color:#666;font-size:13px">Batéria: + vybíja / − nabíja &nbsp;•&nbsp; Sieť: + predaj / − nákup &nbsp;•&nbsp; Žltý podklad v stĺpci × = aktívny násobiteľ ≠ 1.00</p>
"""
    return render_legacy_body(None, f"Plán {date}", body)


SIM_DEF = dict(start="2026-03-01", end="", rt_margin=30.0, max_cycles=2.0)


def _sim_range_banner(start_iso, end_iso, R, skipped_dates, step_min, kind, rt_note):
    """Vytvorí info banner pre výsledok /simulacia: ukáže požadovaný rozsah vs. skutočne simulovaný,
    počet preskočených dní bez plánu, a link na /plan_batch s pre-fillom pre dogenerovanie."""
    req_from = start_iso or "—"
    req_to = end_iso or "—"
    sim_from = str(R.date.min())
    sim_to = str(R.date.max())
    sim_n = len(R)
    skipped_n = len(skipped_dates or [])
    skipped_note = ""
    batch_btn = ""
    if skipped_n:
        sk_first = skipped_dates[0]
        sk_last = skipped_dates[-1]
        skipped_note = (f" · <span style='color:#C00000'>⚠ preskočených {skipped_n} dní</span> "
                        f"<span style='color:#666;font-size:12px'>(prvý {sk_first}, posledný {sk_last})</span>")
        bf = start_iso or sk_first
        bt = end_iso or sk_last
        batch_btn = (f"<div style='margin-top:6px'><a href='/plan_batch?from_date={bf}&to_date={bt}"
                     f"&step_min={step_min}&kind={kind}' "
                     f"style='background:#5E35B1;color:#fff;padding:5px 11px;border-radius:7px;"
                     f"text-decoration:none;font-weight:600;font-size:13px'>"
                     f"🔧 Dogenerovať chýbajúce plány ({bf} … {bt})</a></div>")
    return (f"<div style='background:#eef3f9;border-left:4px solid #2E75B6;padding:8px 12px;"
            f"margin:8px 0;font-size:13px;color:#1F4E78'>"
            f"<b>Strict mode plan_store:</b> plány (× stĺpec, RT maska, všetky polia) sa čítajú "
            f"zo zapečených denných plánov v <code>out/plans/</code>. Dni bez plánu sa preskakujú.<br>"
            f"<b>Žiadaný rozsah:</b> {req_from} … {req_to} · "
            f"<b>simulovaných:</b> {sim_n} dní ({sim_from} … {sim_to}){skipped_note}<br>"
            f"<b>{rt_note}</b>"
            f"{batch_btn}</div>")


def sim_form_page(msg=""):
    f = _ui_load("sim", SIM_DEF)
    # ── Aktívne nastavenia z profilu/template (rovnaký zdroj ako /livesim) ──
    # Simulácia POUŽÍVA tieto hodnoty — formulár ich už nevypisuje, aby sa
    # živá simulácia a backtest nemohli rozísť kvôli rozdielnym vstupom.
    p = _ui_load("plan", DEF)
    try:
        active_profile = pr.get_active() if pr is not None else None
    except Exception:
        active_profile = None
    _prof_label = (f"<b style='color:#5E35B1'>🏷 {active_profile}</b>"
                   if active_profile else "<span style='color:#666'>(default)</span>")
    _settings_summary = (
        f"<table style='font-size:13px;border-collapse:collapse;margin:6px 0'>"
        f"<tr><td style='padding:2px 10px;color:#666'>Batéria</td>"
        f"<td><b>{float(p.get('batt_kw',100)):.0f} kW / {float(p.get('batt_kwh',200)):.0f} kWh</b></td></tr>"
        f"<tr><td style='padding:2px 10px;color:#666'>SOC limit</td>"
        f"<td>{float(p.get('soc_min',5)):.0f}–{float(p.get('soc_max',95)):.0f} %</td></tr>"
        f"<tr><td style='padding:2px 10px;color:#666'>FTV</td>"
        f"<td><b>{float(p.get('kwp',99)):.0f} kWp</b> @ {float(p.get('lat',49.6)):.2f}°/{float(p.get('lon',17.4)):.2f}°</td></tr>"
        f"<tr><td style='padding:2px 10px;color:#666'>Limit siete</td>"
        f"<td>imp {float(p.get('grid_kw_import',p.get('grid_kw',100))):.0f} kW · exp {float(p.get('grid_kw_export',p.get('grid_kw',100))):.0f} kW</td></tr>"
        f"<tr><td style='padding:2px 10px;color:#666'>Min. spread D-1</td>"
        f"<td>{float(p.get('min_spread',30)):.0f} €/MWh</td></tr>"
        f"</table>")
    return f"""<!doctype html><html lang="sk"><head><meta charset="utf-8"><title>Simulácia D-1 + RT</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:-apple-system,Segoe UI,Arial;max-width:900px;margin:24px auto;padding:0 16px;color:#222}}
h1{{color:#1F4E78}} fieldset{{border:1px solid #e0e0e0;border-radius:10px;margin:12px 0;padding:12px 16px}}
legend{{color:#2E75B6;font-weight:600}} .cols{{display:grid;grid-template-columns:1fr 1fr;gap:0 24px}}
button{{background:#1F4E78;color:#fff;border:0;padding:10px 18px;border-radius:8px;font-size:15px;cursor:pointer}}
.msg{{color:#C00000}}
.profile-box{{background:#eef3f9;border-left:4px solid #1F4E78;border-radius:8px;padding:10px 14px;margin:10px 0}}</style></head><body>
<h1>Simulácia — backtest</h1>
{_nav("/simulacia")}
<p style="color:#666">Walk-forward na histórii: D-1 (predikcia→optimalizácia, zúčtované proti skutočným cenám)
+ RT arbitráž odchýlky (živý odhad CEPS, zúčtované proti skutočnej ZCO), zdieľajúce jednu batériu.
Potrebné dáta: <code>out/price_train_2026.csv</code> a <code>out/imbalance_history.csv</code>.</p>
<div class="profile-box">
<div style="font-size:14px;margin-bottom:4px">Simulácia použije nastavenia <b>aktívneho profilu</b>: {_prof_label}
&nbsp;<a href="/profiles" style="color:#1F4E78;font-size:13px">⚙ Spravovať profily</a>
&nbsp;<a href="/" style="color:#1F4E78;font-size:13px">📝 Upraviť template (/plan)</a></div>
{_settings_summary}
<div style="color:#666;font-size:12px;margin-top:6px">Tieto hodnoty sa MUSIA zhodovať so živou simuláciou (/livesim) — pretože obe číta ten istý zdroj (<code>ui_settings.plan</code>). Ak chceš iné parametre, prepni profil alebo uprav template.</div>
</div>
<p class="msg">{msg}</p>
<form method="post" action="/simulacia">
<fieldset><legend>Model</legend>
<label style="display:flex;justify-content:space-between;align-items:center;margin:4px 0">
<span>Ktorý model simulovať</span>
<select name="model" style="padding:6px;border:1px solid #ccc;border-radius:6px;font-size:14px">
<option value="obchod"{" selected" if f.get('model','obchod')=='obchod' else ""}>Obchod a flexibilita (D-1 hodinový + RT odchýlka)</option>
<option value="dentrh"{" selected" if f.get('model','obchod')=='dentrh' else ""}>Denný trh 15-min (čistá arbitráž, bez RT)</option>
</select></label>
<p style="color:#666;font-size:13px;margin:6px 0 0">„Denný trh 15-min" = optimalizácia na reálnych 15-min cenách (známych vopred), bez RT vrstvy. „Obchod a flexibilita" = pôvodný D-1 (hodinový, predikčný) + RT odchýlka.</p></fieldset>
<fieldset><legend>Obdobie</legend><div class="cols">
<label style="display:flex;justify-content:space-between;margin:4px 0"><span>Od</span>
<input name="start" value="{f.get('start','')}" type="date" style="padding:4px;border:1px solid #ccc;border-radius:6px"></label>
<label style="display:flex;justify-content:space-between;margin:4px 0"><span>Do (prázdne = po koniec dát)</span>
<input name="end" value="{f.get('end','')}" type="date" style="padding:4px;border:1px solid #ccc;border-radius:6px"></label>
</div></fieldset>
<fieldset><legend>Beh-špecifické parametre</legend><div class="cols">
{_field("RT vybíjacie pásmo [MW]","rt_margin",f.get('rt_margin', 30.0))}
{_field("Max. cyklov/deň (D-1+RT)","max_cycles",f.get('max_cycles', 2.0))}
</div>
<label style="display:flex;align-items:center;gap:8px;margin:8px 0">
<input name="use_cal" type="checkbox" checked> <span>Použiť kalibráciu výroby (faktor po mesiacoch z nameraných dát)</span></label>
<p style="color:#666;font-size:13px;margin:6px 0 0">RT vrstva = poctivý MW-riadený minútový regulátor (systémová odchýlka + aktivácia FRR, žiadne ceny). Dostane len zvyšný rozpočet cyklov po D-1. Asymetrické pásmo: vybíjanie selektívne, nabíjanie agresívne.</p></fieldset>
<button type="submit">Spustiť simuláciu</button>
<p style="color:#888;font-size:13px">Pozn.: walk-forward trénovanie modelu — môže trvať desiatky sekúnd.</p>
</form></body></html>"""


_SIMULACIA_DEPRECATED_PAGE = """<!doctype html><html lang="sk"><head><meta charset="utf-8"><title>Simulácia — zrušená</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:-apple-system,Segoe UI,Arial;max-width:900px;margin:40px auto;padding:0 20px;color:#222}
h1{color:#1F4E78} .note{background:#eef3f9;border-left:4px solid #1F4E78;border-radius:8px;padding:16px 20px;margin:20px 0}
a.btn{display:inline-block;background:#1F4E78;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600;margin-right:8px}
a.btn.green{background:#2E7D32}</style></head><body>
<h1>🟢 Klasická simulácia bola zrušená</h1>
<div class="note">
<p>Backtest cez <code>/simulacia</code> produkoval výsledky ktoré sa nedali zjednotiť s <b>živou simuláciou</b> (rozdielne zdroje FTV dát, rozdielne modely cien, rozdielne RT engine cesty).</p>
<p><b>Všetka funkcionalita je teraz v <code>/livesim</code></b> — počíta presne podľa aktívneho profilu a šablóny. Pre prácu s historickými dňami:</p>
<ul>
  <li>📅 V dropdowne <b>Zvolený deň</b> klikni na hociktorý uplynulý deň — uvidíš plné grafy + zisky.</li>
  <li>📊 Tabuľka <b>Zisk za obdobie</b> má prepínač <b>Kumulatívne / Po dňoch / Detail 15-min</b>.</li>
  <li>📥 Tlačidlo <b>Export Excel</b> stiahne 7-sheetový report: <i>Zhrnutie + KPI, Po_mesiacoch (s grafmi), Po_dnoch (s grafmi), Vsetky_15min, Detail_15min (s grafmi), Per_minute (raw), Metadata</i> — s DT cenou, ZCO odchýlkovou cenou, FTV výrobou, spotrebou, batériou (charge/discharge/SOC) a baseline cenami.</li>
  <li>📄 Tlačidlo <b>Export PDF</b> stiahne kompaktný manažérsky report (KPI, mesačný prehľad + grafy, denný prehľad + grafy, detail vybraného dňa).</li>
</ul>
</div>
<a class="btn green" href="/livesim">🟢 Otvoriť živú simuláciu</a>
<a class="btn" href="/">🗓 Plán D-1</a>
</body></html>"""


@app.get("/simulacia", response_class=HTMLResponse)
def simulacia_get():
    return _SIMULACIA_DEPRECATED_PAGE


@app.post("/simulacia", response_class=HTMLResponse)
def simulacia(start: str = Form(default=""), end: str = Form(default=""),
              rt_margin: float = Form(default=30.0), max_cycles: float = Form(default=2.0),
              use_cal: str = Form(default=""),
              model: str = Form(default="obchod")):
    """DEPRECATED — /simulacia stránka zrušená; presmeruj na /livesim."""
    return _SIMULACIA_DEPRECATED_PAGE
    # ── (pôvodný handler odstavený, kód nižšie sa nevykoná — ponechaný len pre history) ──
    """Backtest simulácia. Všetky parametre batérie/FTV/SOC/grid/cien číta z aktívneho profilu
    cez _ui_load('plan', DEF) — IDENTICKÝ zdroj ako /livesim. Tým je zaručené že simulácia
    a živá simulácia majú zhodné vstupy (rovnaký výsledok pre rovnaký deň)."""
    try:
        # ── Načítaj profile/template hodnoty (rovnaké ako /livesim cez plan_params) ──
        _p = _ui_load("plan", DEF) or {}
        # form má SOC v %, dparams kľúče soc_*_pct ich očakávajú v %
        def _f(k, default):
            v = _p.get(k, default)
            try: return float(v)
            except (TypeError, ValueError): return float(default)
        dparams = dict(
            # FTV geometria (kwp škáluje historickú výrobu z price_train_2026 v combined_backtest)
            lat=_f("lat", 49.5961), lon=_f("lon", 17.3634),
            kwp=_f("kwp", 99.0),
            tilt=_f("tilt", 30.0), azimuth=_f("azimuth", 0.0), eff=_f("eff", 0.85),
            # Batéria
            batt_kw=_f("batt_kw", 100), batt_kwh=_f("batt_kwh", 200),
            eff_c=_f("eff_c", 0.95), eff_d=_f("eff_d", 0.95),
            soc_min_pct=_f("soc_min", 5), soc_max_pct=_f("soc_max", 95),
            soc_init_pct=_f("soc_init", 50), terminal_soc_pct=_f("terminal_soc", 50),
            # Sieť
            grid_kw=_f("grid_kw", 100),
            grid_kw_import=_f("grid_kw_import", _f("grid_kw", 100)),
            grid_kw_export=_f("grid_kw_export", _f("grid_kw", 100)),
            grid_fee=_f("grid_fee", 22.0),
            cycle_cost=_f("cycle_cost", 2.0),
            allow_grid_charge=bool(_p.get("allow_grid_charge", True)),
            allow_curtail=bool(_p.get("allow_curtail", True)),
            min_spread_eur=_f("min_spread", 30.0),
            block_neg_import=bool(_p.get("block_neg_import", True)),
        )
        s = dt.date.fromisoformat(start) if start else None
        e = dt.date.fromisoformat(end) if end else None
        # ulož len beh-špecifické hodnoty (obdobie + RT pásmo + cykly + model)
        _ui_save("sim", dict(start=start, end=end,
                             rt_margin=rt_margin, max_cycles=max_cycles, model=model))
        pv_cal = None; cal_note = "modelovaná výroba (bez kalibrácie)"
        c = _calibration()
        if bool(use_cal) and c:
            pv_cal = dict(c.get("by_month", {})); pv_cal["_default"] = c.get("factor", 1.0)
            cal_note = "kalibrácia výroby zapojená (faktor po mesiacoch)"
        is_dentrh = (model == "dentrh")
        # ── pre-validácia: zisti aké dátumy v žiadanom rozsahu majú plán v plan_store ──
        _kind_now = "dentrh" if is_dentrh else "plan"
        _step_now = 15 if is_dentrh else 60
        try:
            _ps_list = ps.list_plans(_kind_now) if ps is not None else []
        except Exception:
            _ps_list = []
        _ps_dates = sorted([x["date"] for x in _ps_list if int(x.get("step_min", 60)) == _step_now])
        # rozsah simulácie ktorý reálne budeme mať pokrytý plánmi
        _ps_min = _ps_dates[0] if _ps_dates else None
        _ps_max = _ps_dates[-1] if _ps_dates else None
        # chýbajúce dátumy iba ak má user explicitne zadaný start/end
        _missing_pre = []
        if ps is not None and (start or end) and _ps_min:
            _s_iso = start or _ps_min
            _e_iso = end or _ps_max
            try:
                _missing_pre = ps.missing_plans(_s_iso, _e_iso, _step_now, _kind_now)
            except Exception:
                _missing_pre = []
        # ── prevezmi RT poradca nastavenia z ui_settings.rt (kdis, kchg, dtk) ──
        _rt_ui = _ui_load("rt", {}) or {}
        _rt_kdis = float(_rt_ui.get("kdis", 1.0))
        _rt_kchg = float(_rt_ui.get("kchg", 1.0))
        _rt_dtk_raw = _rt_ui.get("dtk", None)
        _rt_dtk = None if (_rt_dtk_raw in (None, "", "None")) else float(_rt_dtk_raw)
        # ── aggressive_rt + FTV-balance pravidlá VŽDY z ui_settings.plan (globálne RT nastavenia,
        # ui_settings.dentrh ich neukladá — formulár /dentrh ich nemá) ──
        _pui = _ui_load("plan", {}) or {}
        _aggr = bool(_pui.get("aggressive_rt", False))
        _fb_on = bool(_pui.get("ftv_balance", True))
        _fb_lookah = float(_pui.get("ftv_lookahead_h", 4.0))
        _fb_persth = bool(_pui.get("ftv_persistence_throttle", True))
        _rt_nowor = bool(_pui.get("rt_no_worsen_dev", True))
        _fb_strict = bool(_pui.get("ftv_strict_plan", True))
        _fb_deadband = float(_pui.get("ftv_strict_deadband_kw", 5.0))
        rt_note = (f"RT poradca: kdis={_rt_kdis:g}, kchg={_rt_kchg:g}, "
                   f"dtk={_rt_dtk if _rt_dtk is not None else 'vyp'}"
                   f"{' · ⚡ agresívny RT' if _aggr else ''}"
                   f"{' · 🌿 FTV-balance' if _fb_on else ''}"
                   f"{' · 🎯 strict-plan' if (_fb_on and _fb_strict) else ''}"
                   f"{' · 🛡 no-worsen' if _rt_nowor else ''}"
                   f"{f' · ⏱ lookahead {_fb_lookah:g} h' if _fb_lookah > 0 else ''}"
                   f"{' · 🌊 persistence-throttle' if _fb_persth else ''}")
        R = run_combined(dparams=dparams, rt_margin=rt_margin, max_cycles=max_cycles,
                         start=s, end=e, pv_cal=pv_cal,
                         rt_kdis=_rt_kdis, rt_kchg=_rt_kchg, rt_dt_bias_k=_rt_dtk,
                         aggressive_rt=_aggr,
                         ftv_balance_on=_fb_on,
                         ftv_lookahead_h=_fb_lookah,
                         ftv_persistence_throttle=_fb_persth,
                         rt_no_worsen_dev=_rt_nowor,
                         ftv_strict_plan=_fb_strict,
                         ftv_strict_deadband_kw=_fb_deadband,
                         use_rt=not is_dentrh, d1_step_min=(15 if is_dentrh else 60))
    except FileNotFoundError:
        return sim_form_page("Chýba out/imbalance_history.csv — najprv spusti fetch_imbalance_history.py.")
    except Exception as ex:
        return sim_form_page(f"Chyba simulácie: {ex}")
    if R.empty:
        _hint = ""
        if _missing_pre:
            _hint = (f" Pre zvolený rozsah chýba <b>{len(_missing_pre)}</b> dní v plan_store "
                     f"(od {_missing_pre[0]} do {_missing_pre[-1]}). ")
            _batch_link = (f"/plan_batch?from_date={start or _missing_pre[0]}"
                           f"&to_date={end or _missing_pre[-1]}"
                           f"&step_min={_step_now}&kind={_kind_now}")
            _hint += f"<a href='{_batch_link}'>🔧 Otvor batch a dogeneruj</a>."
        return sim_form_page(f"Žiadne dni s plánom v plan_store v zvolenom období."
                             f"{_hint if _hint else ' Najprv vygeneruj plány v /plan_batch.'}")
    R.to_csv("out/combined_backtest.csv", index=False)
    _skipped = R.attrs.get("skipped_dates", []) or _missing_pre

    M = R.groupby("month").agg(dni=("date", "count"), baseline=("baseline", "sum"), d1=("d1", "sum"),
                               rt=("rt", "sum"), combined=("combined", "sum"), prinos=("prinos_baterie", "sum"))
    tb, td1, trt, tc, tp = R.baseline.sum(), R.d1.sum(), R.rt.sum(), R.combined.sum(), R.prinos_baterie.sum()
    nd = len(R)
    cards = "".join(
        f"<div style='background:#f3f6fb;border-radius:10px;padding:10px 14px'>"
        f"<div style='font-size:12px;color:#666'>{lab}</div>"
        f"<div style='font-size:20px;font-weight:600;color:{col}'>{val:.0f} €</div></div>"
        for lab, val, col in [("Prínos D-1", td1-tb, "#1F4E78"), ("Prínos RT", trt, "#2E75B6"),
                              ("Prínos batérie spolu", tp, "#2E7D32"), (f"~ €/mesiac", tp/nd*30, "#2E7D32")])
    mrows = "".join(
        f"<tr><td>{m}</td><td>{int(r.dni)}</td><td>{r.baseline:.1f}</td><td>{r.d1:.1f}</td>"
        f"<td>{r.rt:.1f}</td><td>{r.combined:.1f}</td><td style='font-weight:600'>{r.prinos:.1f}</td>"
        f"<td>{r.prinos/r.dni:.1f}</td></tr>" for m, r in M.iterrows())
    mrows += (f"<tr style='font-weight:700;background:#eef3f9'><td>SPOLU</td><td>{nd}</td><td>{tb:.0f}</td>"
              f"<td>{td1:.0f}</td><td>{trt:.0f}</td><td>{tc:.0f}</td><td>{tp:.0f}</td><td>{tp/nd*30:.0f}</td></tr>")
    drows = ""
    for m, g in R.groupby("month"):
        for _, r in g.iterrows():
            drows += (f"<tr><td>{r.date}</td><td>{r.baseline:.1f}</td><td>{r.d1:.1f}</td><td>{r.rt:.1f}</td>"
                      f"<td>{r.combined:.1f}</td><td style='font-weight:600'>{r.prinos_baterie:.1f}</td>"
                      f"<td>{r.d1_cycles:.2f}</td></tr>")
        s = g[["baseline", "d1", "rt", "combined", "prinos_baterie"]].sum()
        drows += (f"<tr style='font-weight:600;background:#f6f8fb'><td>── {m}</td><td>{s.baseline:.1f}</td>"
                  f"<td>{s.d1:.1f}</td><td>{s.rt:.1f}</td><td>{s.combined:.1f}</td><td>{s.prinos_baterie:.1f}</td><td></td></tr>")
    body = f"""<h1>Simulácia — {R.date.min()} … {R.date.max()} ({nd} dní)</h1>
<p style="color:var(--success);font-size:14px">Výroba: {cal_note}</p>
{_sim_range_banner(start, end, R, _skipped, _step_now, _kind_now, rt_note)}
<p><a href="/simulacia">← Späť na nastavenia</a></p>
<div style="display:flex;gap:12px;margin:12px 0;flex-wrap:wrap">{cards}</div>
<h2>Po mesiacoch (€)</h2>
<table class="tbl-compact"><tr><th>mesiac</th><th>dní</th><th>baseline</th><th>D-1</th><th>RT</th><th>spolu</th><th>prínos bat.</th><th>€/deň</th></tr>{mrows}</table>
<h2>Po dňoch (€)</h2>
<table class="tbl-compact"><tr><th>dátum</th><th>baseline</th><th>D-1</th><th>RT</th><th>spolu</th><th>prínos bat.</th><th>cykly D-1</th></tr>{drows}</table>
<p class="muted" style="font-size:13px">baseline = bez batérie · D-1 = denný trh · RT = odchýlka · spolu = kombinovaný · prínos = spolu − baseline.
Detail aj v out/combined_backtest.csv</p>
"""
    return render_legacy_body(None, "Výsledok simulácie", body)


def kalibracia_form(msg="", extra="", request=None):
    """Render /kalibracia stránky cez Jinja2 (Fáza 3 refactor).

    `extra` je voľný HTML blok (výsledky po POST kalibrácii) — renderuje sa cez |safe.
    """
    from ui.templates import render
    cur = _calibration_factor()
    return render(request, "pages/kalibracia.html",
                   cal_factor=cur, msg=msg, extra_html=extra)


@app.get("/kalibracia", response_class=HTMLResponse)
def kalibracia_get(request: Request):
    return kalibracia_form(request=request)


@app.post("/kalibracia", response_class=HTMLResponse)
def kalibracia_post(request: Request, unit: str = Form(default="kwh"),
                     file: UploadFile = File(...)):
    try:
        raw = file.file.read()
        hourly, dtcol, valcol = _read_production(raw, file.filename or "data.csv", unit)
        if hourly.empty:
            return kalibracia_form("Súbor neobsahuje použiteľné dáta.", request=request)
        hourly["time"] = pd.to_datetime(hourly["time"])
        d0, d1 = hourly.time.min().date(), hourly.time.max().date()
        wx = _fetch_pv_cached(DEF["lat"], DEF["lon"], DEF["kwp"], DEF["tilt"],
                                  DEF["azimuth"], DEF["eff"], start=d0, end=d1)
        wx["time"] = pd.to_datetime(wx["time"])
        m = hourly.merge(wx[["time", "kwh"]].rename(columns={"kwh": "model_kwh"}), on="time", how="inner")
        m = m[(m.real_kwh >= 0) & (m.model_kwh >= 0)]
        sun = m[m.model_kwh > 0.5]
        if len(sun) < 24:
            return kalibracia_form("Po spárovaní s počasím je málo denných hodín (min. 24). "
                                   "Skontroluj časový rozsah a formát.", request=request)
        factor = float(sun.real_kwh.sum() / sun.model_kwh.sum())
        m["month"] = m.time.dt.strftime("%Y-%m")
        bm = (m[m.model_kwh > 0.5].groupby("month")
              .apply(lambda g: g.real_kwh.sum()/g.model_kwh.sum()).round(3).to_dict())
        with open(CAL_PATH, "w") as fh:
            json.dump({"factor": round(factor, 4), "n_hours": int(len(sun)),
                       "from": str(d0), "to": str(d1), "by_month": bm,
                       "real_total": round(float(sun.real_kwh.sum()), 1),
                       "model_total": round(float(sun.model_kwh.sum()), 1)}, fh)
    except Exception as ex:
        return kalibracia_form(f"Chyba pri spracovaní: {ex}", request=request)

    rt, mt = sun.real_kwh.sum(), sun.model_kwh.sum()
    mrows = "".join(f"<tr><td>{mth}</td><td>×{fac:.3f}</td></tr>" for mth, fac in bm.items())
    pct = (factor - 1) * 100
    smer = "vyššia" if factor > 1 else "nižšia"
    extra = f"""<hr><h2 style="color:var(--primary)">Výsledok kalibrácie</h2>
<p>Detegované stĺpce: čas = <b>{dtcol}</b>, výroba = <b>{valcol}</b> &nbsp;•&nbsp; obdobie {d0} … {d1} ({len(sun)} denných hodín)</p>
<table class="tbl-compact"><tr><th>Nameraná výroba</th><th>Modelovaná (Open-Meteo)</th><th>Faktor</th></tr>
<tr><td>{rt:.0f} kWh</td><td>{mt:.0f} kWh</td><td style="font-weight:700">×{factor:.3f}</td></tr></table>
<p>Skutočná výroba je <b>{abs(pct):.1f} % {smer}</b> než modelovaná. Faktor <b>×{factor:.3f}</b> je uložený
a plán D-1 ho odteraz automaticky používa.</p>
<h3>Faktor po mesiacoch</h3><table class="tbl-compact"><tr><th>mesiac</th><th>faktor</th></tr>{mrows}</table>
<p class="muted" style="font-size:13px">Ak sa faktor po mesiacoch výrazne líši (sezónnosť), môžeme neskôr prejsť
na mesačnú/hodinovú kalibráciu namiesto jedného čísla.</p>"""
    return kalibracia_form("Kalibrácia hotová.", extra, request=request)


@app.get("/rt", response_class=HTMLResponse)
def rt_get(soc: float = None, margin: float = None, budget: float = None,
           mode: str = None, bchg: float = None, kdis: float = None, kchg: float = None,
           dtk: float = None, react: float = None, rboost: float = None,
           sock: float = None, case: str = None):
    import case_config as cc
    cc.ensure_default()
    saved = _ui_load("rt", {"soc": 50.0, "budget": 1.0, "case": "default"})
    prev_case = saved.get("case", "default")
    case = prev_case if case is None else case
    cfg = cc.load_case(case)
    rtc.apply_case(cfg)                       # jadro (batéria, event, σ, w_sys...) z prípadu
    switching = case != prev_case             # pri zmene prípadu načítaj jeho stratégiu

    def pick(qval, key, caseval):
        if qval is not None:
            return qval
        if switching:
            return caseval
        return saved.get(key, caseval)
    mode = pick(mode, "mode", "auto" if cfg.rt_auto else "manual")
    margin = pick(margin, "margin", cfg.prod_band_dis)
    bchg = pick(bchg, "bchg", cfg.prod_band_chg)
    kdis = pick(kdis, "kdis", cfg.rt_kdis)
    kchg = pick(kchg, "kchg", cfg.rt_kchg)
    dtk = pick(dtk, "dtk", cfg.dt_bias_k)
    if dtk is None: dtk = 0.0                 # robustný default pre live_decision (float vyžadovaný)
    react = pick(react, "react", 3.0)         # reakčné okno [min] pre rozhodnutie „teraz"
    rboost = pick(rboost, "rboost", getattr(cfg, "reversal_boost", 0.0))
    if rboost is None: rboost = 0.0
    sock = pick(sock, "sock", getattr(cfg, "soc_bias_k", 0.0))
    if sock is None: sock = 0.0
    soc = pick(soc, "soc", 50.0)
    budget = pick(budget, "budget", cfg.max_cycles)
    rtc.REVERSAL_BOOST = float(rboost)         # živé prepnutie boostu (nezávisle od prípadu)
    rtc.SOC_BIAS_K = float(sock)               # živé prepnutie SOC-citlivosti
    _ui_save("rt", {"soc": soc, "margin": margin, "budget": budget, "mode": mode,
                    "bchg": bchg, "kdis": kdis, "kchg": kchg, "dtk": dtk, "react": react,
                    "rboost": rboost, "sock": sock, "case": case})

    head = """<!doctype html><html lang="sk"><head><meta charset="utf-8"><title>RT poradca</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>body{font-family:-apple-system,Segoe UI,Arial;max-width:1800px;margin:18px auto;padding:0 28px;color:#222}
h1,h2{color:#1F4E78} h2{margin:14px 0 4px;font-size:18px} table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}
th,td{border:1px solid #e3e3e3;padding:4px 7px;text-align:right} th{background:#1F4E78;color:#fff}
td:first-child{text-align:left} .card{background:#f3f6fb;border-radius:10px;padding:10px 14px;min-width:118px}
.card .l{font-size:12px;color:#666} .card .v{font-size:20px;font-weight:600}
.big{font-size:28px;font-weight:800;padding:8px 18px;border-radius:12px;color:#fff;display:inline-block}
.mini{font-size:16px;font-weight:700;padding:5px 12px;border-radius:9px;color:#fff;display:inline-block}
input,select{padding:4px;border:1px solid #ccc;border-radius:6px} .wrap{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:10px 0}
.chartbox{height:230px;margin:4px 0 16px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:24px;align-items:start}
@media(max-width:1680px){.grid2{grid-template-columns:1fr}}</style></head><body>"""
    fetch_errors = {}
    try:
        today, now, isot, est, sysd, afrr, act, vdt, fetch_errors = _rt_fetch()
    except Exception as ex:
        # Krajný fallback: _rt_fetch ako celok zlyhal (nemalo by sa stávať — má per-zdroj try/except).
        # Zobrazíme warning ale pokračujeme s prázdnymi DF-mi a aspoň SK historian/SEPS dátami.
        print(f"[/rt] _rt_fetch top-level zlyhal: {ex}")
        fetch_errors = {"_rt_fetch (top-level)": str(ex)[:200]}
        today = dt.date.today()
        now = dt.datetime.now()
        isot = pd.DataFrame(columns=["interval", "ts", "cena_EUR"])
        est = pd.DataFrame(columns=["ts", "est_eur"])
        sysd = pd.DataFrame(columns=["time", "sys_MW"])
        afrr = pd.DataFrame(columns=["time", "aFRR_EUR"])
        act = pd.DataFrame(columns=["time", "aFRR_plus", "aFRR_minus", "mFRR_plus", "mFRR_minus", "mFRR5"])
        vdt = pd.DataFrame(columns=["ts", "cena_EUR"])
    # VDT (vnútrodenný trh) → mapa cien po 15-min periódach (čerstvejšia „aktuálna" cena DT)
    vdt_map = {}
    if vdt is not None and not vdt.empty and "ts" in vdt.columns:
        col = "cena_EUR" if "cena_EUR" in vdt.columns else None
        if col:
            vdt_map = {pd.Timestamp(t).floor("15min"): float(v)
                       for t, v in zip(vdt["ts"], vdt[col]) if pd.notna(v)}

    soc_min, soc_max = DEF["soc_min"], DEF["soc_max"]
    cur_ts = pd.Timestamp(now).floor("15min")
    w_sys, sorient = rtc.PROD_W_SYS, rtc.PROD_SYS_ORIENT

    # ---- MW signál z live aktivácií + systémovej odchýlky (žiadne ceny) ----
    _cz_lf = _rt_live_frame(today, isot, sysd, afrr, act)
    # Pre SK trh nahraď lf SK minútovými dátami (sys_MW zo SEPS + FRR proxy z CZ),
    # aby header, chart c4 a tabuľka šli z rovnakého zdroja.
    _is_sk_market_lf = False
    try:
        _is_sk_market_lf = (mk is not None and str(mk.get_active_market()).lower() == "sk")
    except Exception:
        pass
    if _is_sk_market_lf:
        try:
            import seps_sk as _seps_lf
            _sk_lf = _seps_lf.build_sk_live_minutes(today=pd.Timestamp(today), cz_lf=_cz_lf)
            if _sk_lf is not None and not _sk_lf.empty:
                # Trim na minulosť — build_sk_live_minutes vracia full-day grid 00:00→23:59,
                # ale CZ pipeline očakáva len past+current (per agg, sig_vals, recent...).
                # Bez trim-u mw_signal pre future minúty = nz(NaN)*w_sys + net = 0 → sig_vals
                # ukazuje 0 do konca dňa a header reco sa rozchádza s chart c4.
                _now_naive = pd.Timestamp(now)
                if _now_naive.tzinfo is not None:
                    _now_naive = _now_naive.tz_localize(None)
                _sk_lf = _sk_lf[_sk_lf["time"] <= _now_naive].copy()
                print(f"[/rt] SK trh: lf swapnutý na build_sk_live_minutes "
                      f"({len(_sk_lf)} riadkov, trim na ≤ {_now_naive})")
                lf = _sk_lf
            else:
                lf = _cz_lf
        except Exception as _e:
            print(f"[/rt] SK lf swap zlyhal: {_e}")
            lf = _cz_lf
    else:
        lf = _cz_lf
    have_lf = lf is not None and not lf.empty and any(
        c in lf.columns for c in ["sys_MW", "aFRR_plus", "aFRR_minus", "mFRR_plus", "mFRR_minus", "mFRR5"])
    per = pd.DataFrame(columns=["ts15", "sig", "sys", "net", "dt"])
    sig_std = float("nan")
    if have_lf:
        lf = rtc.prep(rtc._ensure_act(lf.copy()))
        lf["sig"] = [rtc.mw_signal(r._asdict(), w_sys, sorient) for r in lf.itertuples(index=False)]
        lf["net"] = (lf["aFRR_plus"].fillna(0) + lf["mFRR_plus"].fillna(0) + lf["mFRR5"].fillna(0)
                     - lf["aFRR_minus"].fillna(0) - lf["mFRR_minus"].fillna(0))
        sig_std = float(lf["sig"].std())

    # NOTE: SK sig_std override odstránený — sig_std je teraz počítaný priamo z lf["sig"]
    # (lf je už SK-aware vďaka build_sk_live_minutes swapu vyššie).

    # AUTO pásmo = podľa volatility signálu (vybíjanie selektívnejšie, nabíjanie agresívnejšie);
    # MANUÁL = hodnoty z formulára.
    if mode == "auto" and not pd.isna(sig_std) and sig_std > 0:
        band_dis, band_chg = rtc.auto_bands(sig_std)
    else:
        mode = mode if mode == "auto" else "manual"
        band_dis = float(margin); band_chg = float(bchg)
    band_dis *= float(kdis); band_chg *= float(kchg)   # škála (1=bez zmeny, 0.8=−20%)

    reco, power = "DRŽ", 0
    cur_sig = cur_sys = cur_net = cur_dt = cur_vdt = float("nan")
    if have_lf:
        per = (lf.groupby("ts15").agg(
                sig=("sig", "mean"), sys=("sys_MW", "mean"), net=("net", "mean"),
                dt=("isot_eur", "mean"),
                aFRR_plus=("aFRR_plus", "mean"), aFRR_minus=("aFRR_minus", "mean"),
                mFRR_plus=("mFRR_plus", "mean"), mFRR_minus=("mFRR_minus", "mean"),
                mFRR5=("mFRR5", "mean"),
                afrr_up_roll=("afrr_up_roll", "mean"), afrr_dn_roll=("afrr_dn_roll", "mean"),
                afrr_up_std=("afrr_up_std", "mean"), afrr_dn_std=("afrr_dn_std", "mean"),
                act_net_roll=("act_net_roll", "mean"))
               .reset_index())
        # aktuálna perióda; ak ešte nedorazili minútové dáta (CEPS lag), použi poslednú dostupnú
        dec_ts = cur_ts
        curlf = lf[lf.ts15 == cur_ts]
        if curlf.empty and not per.empty:
            dec_ts = per["ts15"].max()
            curlf = lf[lf.ts15 == dec_ts]
        if not curlf.empty:
            # REAKČNÉ OKNO: rozhoduj z posledných `react` minút (rýchle prepnutie smeru aj na nulu),
            # nie z priemeru celej 15-min periódy (to reagovalo pomaly, až ~15 min).
            # POZOR: lf pre SK (build_sk_live_minutes) má full-day grid (00:00→23:59), takže
            # lf["time"].max() je 23:59 nie "teraz". Cap-neme na pd.Timestamp(now).
            _tmax_raw = pd.Timestamp(lf["time"].max())
            _tnow = pd.Timestamp(now)
            if _tmax_raw.tzinfo is not None:
                _tmax_raw = _tmax_raw.tz_localize(None)
            if _tnow.tzinfo is not None:
                _tnow = _tnow.tz_localize(None)
            tmax = min(_tmax_raw, _tnow)
            recent = lf[(lf["time"] >= tmax - pd.Timedelta(minutes=float(react))) & (lf["time"] <= tmax)]
            if recent.empty:
                recent = curlf
            day_mean_dt = float(per["dt"].dropna().mean()) if per["dt"].notna().any() else 0.0
            row_dt = per[per.ts15 == dec_ts]
            cur_dt = float(row_dt.dt.iloc[0]) if (not row_dt.empty and pd.notna(row_dt.dt.iloc[0])) else day_mean_dt
            cur_vdt = vdt_map.get(pd.Timestamp(dec_ts), float("nan"))   # VDT pre aktuálnu periódu
            dt_for_bias = cur_vdt if pd.notna(cur_vdt) else cur_dt      # VDT aktualizuje DT (ak je)
            dt_rel = (dt_for_bias - day_mean_dt) if not pd.isna(dt_for_bias) else 0.0
            # Header reco POUŽÍVA ROVNAKÉ bandy ako chart c4 a tabuľka (band_dis, band_chg).
            # Žiadny rboost-band-shift ani SOC-bias — aby header bol konzistentný s grafom
            # a aby SOC neovplyvňoval čistý signál (limity rieši až livesim/simulátor).
            dec = rtc.live_decision(recent, band_dis, band_chg, w_sys, sorient, dt_rel=dt_rel, dt_bias_k=float(dtk))
            reco, power, cur_sig = dec["reco"], dec["power_pct"], dec["avg"]
            cur_sys = float(recent["sys_MW"].mean()); cur_net = float(recent["net"].mean())
            # Fallback: ak recent okno (typicky 3 min) je celé NaN (SEPS realtime gap),
            # použij poslednú validnú hodnotu v lf pokiaľ nie je staršia ako 10 min.
            _fallback_minutes = 10.0
            _last_ok_ts = None
            def _last_valid(col_name, target_var_is_nan):
                """Vráti poslednú validnú hodnotu z lf[col_name] do _fallback_minutes pred tmax."""
                if not target_var_is_nan or col_name not in lf.columns:
                    return None
                _ser = pd.to_numeric(lf[col_name], errors="coerce")
                _msk = _ser.notna() & (lf["time"] >= tmax - pd.Timedelta(minutes=_fallback_minutes))
                if _msk.any():
                    _last_idx = _ser[_msk].index[-1]
                    return float(_ser.loc[_last_idx]), lf["time"].loc[_last_idx]
                return None
            for _name, _var_name in (("sys_MW", "cur_sys"), ("net", "cur_net"), ("sig", "cur_sig")):
                _cur_val = locals().get(_var_name, float("nan"))
                if pd.isna(_cur_val):
                    _res = _last_valid(_name, True)
                    if _res is not None:
                        if _var_name == "cur_sys": cur_sys = _res[0]
                        elif _var_name == "cur_net": cur_net = _res[0]
                        elif _var_name == "cur_sig": cur_sig = _res[0]
                        _last_ok_ts = _res[1] if _last_ok_ts is None else _last_ok_ts
                        print(f"[/rt] {_var_name} fallback: posledná validná {_name}={_res[0]:.1f} z {_res[1]} (recent NaN)")
    # SOC / rozpočet limity sa v /rt NEUPLATŇUJÚ — odporúčanie je čistý signál
    # (limity rieši až livesim / reálny simulátor pri vykonaní akcie).
    if reco.startswith("DRŽ"):
        power = 0                                            # pri držaní je výkon vždy 0 %

    last_afrr = None
    if afrr is not None and not afrr.empty and "aFRR_EUR" in afrr.columns:
        _s = pd.to_numeric(afrr["aFRR_EUR"], errors="coerce").dropna()
        last_afrr = round(float(_s.iloc[-1]), 0) if len(_s) else None

    # NOTE: SK top stats + reco override odstránené — lf je už SK-aware (build_sk_live_minutes),
    # takže cur_sys/cur_sig/cur_dt z normálnej CZ pipeline + live_decision na SK dátach
    # produkujú konzistentný výsledok s chart c4 a tabuľkou.
    # Iba cur_vdt potrebuje SK override (lf má vdt_eur, ale per agg ju nepretvára).
    if _is_sk_market_lf and have_lf and "vdt_eur" in lf.columns:
        try:
            _ts_for_vdt = locals().get("dec_ts", cur_ts)
            _vdt_at_cur = lf.loc[lf["ts15"] == _ts_for_vdt, "vdt_eur"]
            if not _vdt_at_cur.empty and pd.notna(_vdt_at_cur.iloc[0]):
                cur_vdt = float(_vdt_at_cur.iloc[0])
        except Exception:
            pass

    def card(lab, val, unit=""):
        v = "—" if (val is None or (isinstance(val, float) and pd.isna(val))) else f"{val:.0f}{unit}"
        return f"<div class='card'><div class='l'>{lab}</div><div class='v'>{v}</div></div>"

    cards = (card("MW signál (priemer)", cur_sig) + card("Sys. odchýlka [MW]", cur_sys)
             + card("Net aktivácia [MW]", cur_net) + card("DT teraz [€/MWh]", cur_dt)
             + card("VDT teraz [€/MWh]", cur_vdt) + card("aFRR cena [€/MWh]", last_afrr))
    big = f"<span class='big' style='background:{_RECO_COL.get(reco, '#888')}'>{reco}</span>"
    powtxt = f"<span class='mini' style='background:#1F4E78'>výkon {power} %</span>"

    # per-perióda rozhodnutie cez plný decide() (event + DT-posun + pásmo) → tabuľka aj graf
    RULE_LABEL = {"mfrr_up": "mFRR+ (event)", "afrr_up": "aFRR+ skok",
                  "mfrr_dn": "mFRR− (event)", "afrr_dn": "aFRR− skok",
                  "flip_up": "flip↑ zmena služby", "flip_dn": "flip↓ zmena služby",
                  "band_vybi": "pásmo VYBI", "band_nabi": "pásmo NABI", "hold": "—",
                  "hold_sys": "DRŽ (proti odchýlke)"}
    RULE_COL = {"mfrr_up": "#C0392B", "afrr_up": "#E8A317",
                "mfrr_dn": "#7030A0", "afrr_dn": "#2E86C1",
                "flip_up": "#27AE60", "flip_dn": "#2980B9",
                "band_vybi": "#2E7D32", "band_nabi": "#1F4E78", "hold": "#cfd8e3",
                "hold_sys": "#cfd8e3"}
    day_mean_dt = float(per["dt"].dropna().mean()) if (not per.empty and per["dt"].notna().any()) else 0.0
    reco_by_ts, powsigned_by_ts, rule_by_ts = {}, {}, {}
    rows = ""
    for _, r in per.iterrows():
        rd = {"aFRR_plus": r.aFRR_plus, "aFRR_minus": r.aFRR_minus, "mFRR_plus": r.mFRR_plus,
              "mFRR_minus": r.mFRR_minus, "mFRR5": r.mFRR5, "sys_MW": r.sys,
              "afrr_up_roll": r.afrr_up_roll, "afrr_dn_roll": r.afrr_dn_roll,
              "afrr_up_std": r.afrr_up_std, "afrr_dn_std": r.afrr_dn_std,
              "act_net_roll": r.act_net_roll}
        dt_rel_p = (float(r["dt"]) - day_mean_dt) if not pd.isna(r["dt"]) else 0.0
        direction, frac, reason = rtc.decide_reason(rd, float(r.sig), band_dis, band_chg,
                                                     rtc.STRONG_MW, dt_rel_p, float(dtk))
        g = "VYBI" if direction > 0 else ("NABI" if direction < 0 else "DRŽ")
        pw = int(round(frac*100))
        reco_by_ts[pd.Timestamp(r.ts15)] = g
        powsigned_by_ts[pd.Timestamp(r.ts15)] = (pw if direction > 0 else (-pw if direction < 0 else 0))
        rule_by_ts[pd.Timestamp(r.ts15)] = reason
        col = _RECO_COL.get(g, "#888")
        rcol = RULE_COL.get(reason, "#888")
        hl = "background:#fff7e6;font-weight:700" if r.ts15 == cur_ts else ""
        rows += (f"<tr style='{hl}'><td>{pd.Timestamp(r.ts15).strftime('%H:%M')}</td>"
                 f"<td>{r.sys:+.0f}</td><td>{r.net:+.0f}</td><td>{r.sig:+.0f}</td>"
                 f"<td style='color:{col};font-weight:600'>{g}</td><td>{pw}%</td>"
                 f"<td style='color:{rcol};font-weight:600'>{RULE_LABEL.get(reason,'—')}</td></tr>")

    # ---- CELODENNÝ graf: 96 periód z DT (D-1), pásma posunuté per-perióda podľa DT ----
    ddt = isot.dropna(subset=["ts"]).drop_duplicates("ts").sort_values("ts")

    # ── SK trh: full grid 24h × 15-min z SK historian DT (96 slotov) ───────
    # ČEPS data sa zariezáva pri „teraz", SK historian má aj budúce DT pre celý deň.
    _is_sk_full_grid = False
    try:
        _is_sk_full_grid = (mk is not None and str(mk.get_active_market()).lower() == "sk")
    except Exception:
        pass

    if _is_sk_full_grid:
        # Generovať 96 naive timestamps od today 00:00 (matchuje string keys v historian map)
        full_ts = [pd.Timestamp(today) + pd.Timedelta(minutes=15*i) for i in range(96)]
        # dt_day z SK historian (preskočí None pre dis_line/chg_line výpočet)
        try:
            import seps_sk as _seps_grid
            _dt_sk_map = _seps_grid.load_okte_dt_for_day(today.isoformat())
            dt_day = []
            for t in full_ts:
                k = pd.Timestamp(t).strftime("%Y-%m-%d %H:%M:%S")
                dt_day.append(float(_dt_sk_map.get(k, np.nan)))
            # nahraď NaN priemerom (pre band výpočet)
            _valid = [x for x in dt_day if pd.notna(x)]
            _mean = float(np.mean(_valid)) if _valid else 0.0
            dt_day = [_mean if pd.isna(x) else x for x in dt_day]
        except Exception as _e:
            print(f"[/rt] SK full grid dt_day zlyhal: {_e}")
            full_ts = list(ddt["ts"]) if not ddt.empty else (list(per.ts15) if not per.empty else [])
            dt_day = [float(x) for x in ddt["cena_EUR"]] if not ddt.empty else []
    else:
        full_ts = list(ddt["ts"]) if not ddt.empty else (list(per.ts15) if not per.empty else [])
        dt_day = [float(x) for x in ddt["cena_EUR"]] if not ddt.empty else []

    labels = [pd.Timestamp(t).strftime("%H:%M") for t in full_ts]
    day_mean_dt2 = float(np.mean(dt_day)) if dt_day else 0.0
    dtk_eff = float(dtk)
    dis_line, chg_line = [], []
    for x in (dt_day if dt_day else [day_mean_dt2]*len(full_ts)):
        b = dtk_eff*(x - day_mean_dt2)
        dis_line.append(round(max(rtc.AUTO_MIN_DIS, band_dis - b), 1))      # + vybíjacia hranica
        chg_line.append(round(-max(rtc.AUTO_MIN_CHG, band_chg + b), 1))     # − nabíjacia hranica
    sig_map = {pd.Timestamp(t): float(s) for t, s in zip(per.ts15, per.sig)} if not per.empty else {}
    sig_vals = [round(sig_map[pd.Timestamp(t)], 0) if pd.Timestamp(t) in sig_map else None for t in full_ts]

    # ── SK trh detekcia (zdieľaná pre všetky SK overrides nižšie) ──────────
    try:
        _is_sk_prices = (mk is not None and str(mk.get_active_market()).lower() == "sk")
    except Exception:
        _is_sk_prices = False

    # NOTE: SK sig_vals (c0) override odstránený — per.sig je už SK MW signál
    # (lf má sys_MW zo SEPS + mw_signal = net + w_sys*sys_orient*sys_MW = -3*sys_MW pre SK).

    # celodenné aktivácie FRR (hore +, dole −) a výsledné odporúčanie (signed výkon %)
    def amap(col):
        m = {pd.Timestamp(t): float(v) for t, v in zip(per.ts15, per[col])} if not per.empty else {}
        return [round(m[pd.Timestamp(t)], 1) if pd.Timestamp(t) in m else None for t in full_ts]
    act_ap = amap("aFRR_plus"); act_mp = amap("mFRR_plus"); act_m5 = amap("mFRR5")
    act_an = [(-x if x is not None else None) for x in amap("aFRR_minus")]   # dole záporné
    act_mn = [(-x if x is not None else None) for x in amap("mFRR_minus")]
    reco_pow = [powsigned_by_ts.get(pd.Timestamp(t)) for t in full_ts]       # +VYBI / −NABI / 0
    reco_col = [RULE_COL.get(rule_by_ts.get(pd.Timestamp(t)), "#eef1f5") for t in full_ts]  # farba = pravidlo

    # cena odchýlky – odhad ZCO ako ho zverejňuje CEPS (€/MWh), ~30 min spätne
    zco_map = {}
    if est is not None and not est.empty and "ts" in est.columns and "est_eur" in est.columns:
        zco_map = {pd.Timestamp(t).floor("15min"): float(v)
                   for t, v in zip(est["ts"], est["est_eur"]) if pd.notna(v)}
    zco_vals = [round(zco_map[pd.Timestamp(t)], 0) if pd.Timestamp(t) in zco_map else None for t in full_ts]
    # VDT (vnútrodenný trh) zarovnaný na celý deň – ďalší priebeh do grafu cien odchýlky
    vdt_vals = [round(vdt_map[pd.Timestamp(t)], 0) if pd.Timestamp(t) in vdt_map else None for t in full_ts]

    # systémová odchýlka – 15-min priemer (stĺpce) zarovnaný na celý deň
    sys15_map = {pd.Timestamp(t): float(v) for t, v in zip(per.ts15, per.sys)} if not per.empty else {}

    # DT cena zarovnaná na celý deň (pre MW signál aj cenu odchýlky)
    # SK: preferuj per.dt (z lf.isot_eur = SK DT z OKTE historian) pred ddt (=CZ OTE).
    if _is_sk_market_lf and not per.empty:
        dt_by_ts = {pd.Timestamp(t): float(v) for t, v in zip(per.ts15, per.dt) if pd.notna(v)}
    elif not ddt.empty:
        dt_by_ts = {pd.Timestamp(t): float(c) for t, c in zip(ddt["ts"], ddt["cena_EUR"])}
    elif not per.empty:
        dt_by_ts = {pd.Timestamp(t): float(v) for t, v in zip(per.ts15, per.dt) if pd.notna(v)}
    else:
        dt_by_ts = {}
    dt_line = [round(dt_by_ts[pd.Timestamp(t)], 0) if pd.Timestamp(t) in dt_by_ts else None for t in full_ts]

    # VDT predbežné (kontinuálne, ide aj cez dnešok) — pre SK trh načítané z historianu,
    # pre CZ ostane všetky None (chart dataset sa nezobrazí).
    vdtp_vals = [None] * len(full_ts)

    # ── SK trh: nahraď DT/VDT/ZCO z historian CSV (15-min slots) ───────────
    # DÔLEŽITÉ: pre SK NIKDY nesmie ostať CZ ZCO/VDT (z ČEPS odhadu `est`/`vdt_map`).
    # Najprv vynulujeme CZ dáta, potom naplníme zo SK historianu. ZCO sa publikuje len D-1,
    # takže pre dnešok (a sloty bez dát) je hodnota 0 (per user — radšej 0 než CZ cena).
    if _is_sk_prices:
        try:
            import seps_sk as _seps
            # full_ts sú pd.Timestamp objekty; mapy majú string kľúče → konvert
            def _ts_key(t):
                tt = pd.Timestamp(t)
                tt = tt.tz_localize(None) if tt.tzinfo else tt
                return tt.strftime("%Y-%m-%d %H:%M:%S")
            _dt_sk_map  = _seps.load_okte_dt_for_day(today.isoformat())
            _vdt_sk_map = _seps.load_okte_vdt_for_day(today.isoformat())
            _zco_sk_map = _seps.load_okte_zco_for_day(today.isoformat())
            # DT: zo SK historianu (ak chýba slot → None, krivka sa preruší)
            dt_line = [round(_dt_sk_map[_ts_key(t)], 0) if _ts_key(t) in _dt_sk_map else None for t in full_ts]
            # VDT finálne: zo SK historianu (D+1, dnes typicky None)
            vdt_vals = [round(_vdt_sk_map[_ts_key(t)], 0) if _ts_key(t) in _vdt_sk_map else None for t in full_ts]
            # ZCO: SK zúčtovacia cena odchýlky. Dnes nepublikovaná → 0 (NIE CZ odhad).
            zco_vals = [round(_zco_sk_map[_ts_key(t)], 0) if _ts_key(t) in _zco_sk_map else 0.0 for t in full_ts]
            # VDT predbežné — kontinuálne aj cez dnešok
            try:
                _vdtp_sk_map = _seps.load_okte_vdt_preliminary_for_day(today.isoformat())
                vdtp_vals = [round(_vdtp_sk_map[_ts_key(t)], 0) if _ts_key(t) in _vdtp_sk_map else None for t in full_ts]
            except Exception as _ee:
                print(f"[/rt] SK VDT predbežné load zlyhal: {_ee}")
                vdtp_vals = [None] * len(full_ts)
            n_dt = sum(1 for v in dt_line if v is not None)
            n_vdt = sum(1 for v in vdt_vals if v is not None)
            n_vdtp = sum(1 for v in vdtp_vals if v is not None)
            n_zco = sum(1 for v in zco_vals if (v is not None and v != 0.0))
            print(f"[/rt] SK ceny z historian: DT {n_dt}, VDT-fin {n_vdt}, VDT-prelim {n_vdtp}, ZCO {n_zco} (nenulové) / {len(full_ts)} slotov")
        except Exception as _e:
            print(f"[/rt] SK ceny swap zlyhal: {_e}")
            # pri zlyhaní radšej vynuluj ZCO/VDT než nechať CZ leak
            zco_vals = [0.0] * len(full_ts)
            vdt_vals = [None] * len(full_ts)

    # 5-min os pre sys odchýlku: minútová (5-min) čiara + 15-min priemer ako stĺpce
    def _nv(t):
        t = pd.Timestamp(t)
        return t.tz_localize(None) if t.tzinfo else t
    m5_ts = pd.date_range(pd.Timestamp(today), periods=288, freq="5min")   # naive, celý deň
    m5_labels = [t.strftime("%H:%M") for t in m5_ts]
    sys5 = [None]*288
    if have_lf and "sys_MW" in lf.columns:
        s = lf[["time", "sys_MW"]].dropna().copy()
        tt = pd.to_datetime(s["time"])
        try:
            tt = tt.dt.tz_localize(None)
        except (TypeError, AttributeError):
            pass
        s["t5"] = tt.dt.floor("5min")
        m5map = {pd.Timestamp(k): float(v) for k, v in s.groupby("t5")["sys_MW"].mean().items()}
        sys5 = [round(m5map[t], 0) if t in m5map else None for t in m5_ts]
    sys15_nv = {_nv(t): v for t, v in sys15_map.items()}
    sys15rep = [round(sys15_nv[_nv(t.floor("15min"))], 0) if _nv(t.floor("15min")) in sys15_nv else None
                for t in m5_ts]
    sys15rep_col = ["#E0A800" if (v or 0) >= 0 else "#5DADE2" for v in sys15rep]

    # ── SK trh: nahraď ČEPS sys_MW SEPS reg.výkonom (SEPS-native, bez flipu) ───
    _is_sk_rt = False
    try:
        _is_sk_rt = (mk is not None and str(mk.get_active_market()).lower() == "sk")
    except Exception:
        pass
    if _is_sk_rt:
        try:
            import seps_sk as _seps
            _sys5_sk, _sys15rep_sk, _sys15col_sk = _seps.load_sys_arrays_for_rt(today.isoformat())
            # Sanity: aspoň nejaké hodnoty
            _n_have = sum(1 for v in _sys5_sk if v is not None)
            if _n_have > 0:
                sys5 = _sys5_sk
                sys15rep = _sys15rep_sk
                sys15rep_col = _sys15col_sk
                print(f"[/rt] SK trh: použil SEPS reg.výkon ({_n_have}/288 5-min slotov)")
        except Exception as _e:
            print(f"[/rt] SK SEPS swap zlyhal: {_e}")

    # minútový (5-min) priebeh NAVRHOVANÉHO výkonu (+VYBI / −NABI) – mení sa častejšie než raz za 15 min,
    # presne ako badge „výkon %". Pre každý 5-min bod sa prehrá rozhodnutie reakčného okna končiaceho v ňom.
    reco_min = [None]*288
    reco_min_col = ["rgba(0,0,0,0)"]*288
    if have_lf and "sig" in lf.columns:
        lt = pd.to_datetime(lf["time"])
        try:
            lt = lt.dt.tz_localize(None)
        except (TypeError, AttributeError):
            pass
        lfx = lf.copy(); lfx["t_nv"] = lt
        now_nv = pd.Timestamp(now)
        now_nv = now_nv.tz_localize(None) if now_nv.tzinfo else now_nv
        react_td = pd.Timedelta(minutes=max(1.0, float(react)))
        for i, t in enumerate(m5_ts):
            if t > now_nv:
                continue
            win = lfx[(lfx["t_nv"] > t - react_td) & (lfx["t_nv"] <= t)]
            if win.empty:
                continue
            dtrel = float(dt_by_ts.get(pd.Timestamp(t).floor("15min"), day_mean_dt)) - day_mean_dt
            res = rtc.live_decision(win, band_dis, band_chg, w_sys, sorient,
                                    dt_rel=dtrel, dt_bias_k=float(dtk))
            pw, rc = res["power_pct"], res["reco"]
            reco_min[i] = pw if rc == "VYBI" else (-pw if rc == "NABI" else 0)
            reco_min_col[i] = ("rgba(46,125,50,.8)" if rc == "VYBI"
                               else ("rgba(31,78,120,.8)" if rc == "NABI" else "rgba(150,150,150,.35)"))

    # aFRR cena – celý deň (15-min priemer), zarovnaná na os 00–24
    afrr_by_ts = {}
    if afrr is not None and not afrr.empty and "aFRR_EUR" in afrr.columns:
        a = afrr[["time", "aFRR_EUR"]].dropna().copy()
        tt = pd.to_datetime(a["time"])
        try:
            tt = tt.dt.tz_localize(None)
        except (TypeError, AttributeError):
            pass
        a["ts15"] = tt.dt.floor("15min")
        afrr_by_ts = {_nv(k): float(v) for k, v in a.groupby("ts15")["aFRR_EUR"].mean().items()}
    afrr_vals = [round(afrr_by_ts[_nv(t)], 0) if _nv(t) in afrr_by_ts else None for t in full_ts]

    warn = "" if have_lf else "<p style='color:#C0392B'>Zatiaľ žiadne live CEPS dáta na dnes.</p>"
    if fetch_errors:
        _err_items = "; ".join(f"<b>{k}</b>: {v}" for k, v in fetch_errors.items())
        warn += (f"<p style='color:#C0392B;background:#fff3f3;border:1px solid #f1c0c0;"
                 f"border-radius:8px;padding:8px 12px;margin:6px 0'>"
                 f"⚠ Niektoré zdroje dnes nedostupné (pokračujem s tým čo mám): {_err_items}</p>")

    auto_sel = "selected" if mode == "auto" else ""
    man_sel = "selected" if mode != "auto" else ""
    case_opts = "".join(f'<option value="{c}" {"selected" if c==case else ""}>{c}</option>'
                        for c in cc.list_cases())
    band_note = (f"AUTO podľa volatility (σ={sig_std:.0f} MW)" if mode == "auto"
                 else "MANUÁL (ručne)")
    body = f"""<h1>RT poradca — odchýlka {today}</h1>
{_nav("/rt")}
<p style="color:#666">Prípad: <b>{case}</b> · batéria {cfg.batt_kw:.0f} kW / {cfg.batt_kwh:.0f} kWh · využiteľná kapacita <b>{cfg.batt_kwh*(cfg.soc_max-cfg.soc_min):.0f} kWh</b> ({cfg.soc_min*100:.0f}–{cfg.soc_max*100:.0f}% SOC).
Riadené VEĽKOSŤAMI v MW: systémová odchýlka + aktivácia FRR. Žiadne ceny. Výkon spojitý (%). Obnova 1 min · reakčné okno {react:g} min{(' · reversal boost '+format(rboost,'g')) if float(rboost)>0 else ''}{(' · SOC-bias '+format(sock,'g')) if float(sock)>0 else ''}. {now.strftime('%H:%M:%S')}.</p>{warn}
<p><a href="/" style="color:#2E75B6">← Plán D-1</a> &nbsp; <a href="/rt?case={case}&soc={soc:g}&margin={margin:g}&budget={budget:g}&mode={mode}&bchg={bchg:g}&kdis={kdis:g}&kchg={kchg:g}&dtk={dtk:g}&react={react:g}&rboost={rboost:g}&sock={sock:g}">⟳ Obnoviť teraz</a></p>
<form method="get" action="/rt" class="wrap">
<label>Prípad <select name="case" onchange="this.form.submit()">{case_opts}</select></label>
<label>Režim pásma <select name="mode"><option value="auto" {auto_sel}>auto (podľa situácie)</option><option value="manual" {man_sel}>manuál</option></select></label>
<label>Vybíjacie pásmo [MW] <input name="margin" value="{margin:g}" type="number" step="any" style="width:75px"></label>
<label>Nabíjacie pásmo [MW] <input name="bchg" value="{bchg:g}" type="number" step="any" style="width:75px"></label>
<label>Škála vybíj. <input name="kdis" value="{kdis:g}" type="number" step="any" style="width:65px"></label>
<label>Škála nabíj. <input name="kchg" value="{kchg:g}" type="number" step="any" style="width:65px"></label>
<label>DT citlivosť <input name="dtk" value="{dtk:g}" type="number" step="any" style="width:65px"></label>
<label>Reakčné okno [min] <input name="react" value="{react:g}" type="number" step="any" style="width:65px"></label>
<label>Reversal boost <input name="rboost" value="{rboost:g}" type="number" step="any" style="width:65px"></label>
<label>SOC-bias <input name="sock" value="{sock:g}" type="number" step="any" style="width:65px"></label>
<label>SOC [%] <input name="soc" value="{soc:g}" type="number" step="any" style="width:70px"></label>
<label>Zvyšné cykly <input name="budget" value="{budget:g}" type="number" step="any" style="width:70px"></label>
<button type="submit" style="background:#1F4E78;color:#fff;border:0;padding:8px 14px;border-radius:8px;cursor:pointer">Použiť a zapamätať</button>
</form>
<p style="color:#666;font-size:13px;margin:0 0 6px">Aktívne pásmo: <b>vybíjanie nad +{band_dis:.0f} MW</b>, <b>nabíjanie pod −{band_chg:.0f} MW</b> &nbsp;({band_note}). Škála: 1 = bez zmeny, 0,8 = o 20 % nižšie. DT citlivosť = o koľko klesne vybíjací prah pri vysokých cenách. Prepnutie prípadu načíta jeho uložené nastavenia.</p>
<h2>Odporúčanie teraz ({cur_ts.strftime('%H:%M')})</h2>
<div class="wrap">{big}{powtxt}{cards}</div>
<h2>Odporúčanie batérie — minútový priebeh (5-min, VYBI hore zelená, NABI dole modrá, výška = výkon %)</h2><div class="chartbox"><canvas id="c4"></canvas></div>
<p style="font-size:13px;margin:4px 0 0">Farba stĺpca = smer:
<span style="color:#2E7D32;font-weight:600">■ VYBI (hore)</span> ·
<span style="color:#1F4E78;font-weight:600">■ NABI (dole)</span> ·
<span style="color:#999;font-weight:600">■ DRŽ</span>. Priebeh je v 5-min rozlíšení (rozhodnutie reakčného okna v každom bode) — mení sa častejšie než raz za 15 min. Detail pravidla (event/skok/flip) je v tabuľke nižšie.</p>
<h2>MW signál — celý deň (realita sa dopĺňa zľava); pásma sa vlnia podľa DT cien</h2><div class="chartbox"><canvas id="c0"></canvas></div>
<div class="grid2">
<div><h2>Cena odchýlky a trhu — ZCO · DT · VDT predbežné + finálne [€/MWh]</h2><div class="chartbox"><canvas id="c5"></canvas></div></div>
<div><h2>Aktivácia FRR služieb [MW] (hore = +, dole = −)</h2><div class="chartbox"><canvas id="c1"></canvas></div></div>
</div>
<div class="grid2">
<div><h2>Systémová odchýlka [MW] — 15-min priemer (stĺpce)</h2><div class="chartbox"><canvas id="c2"></canvas></div></div>
<div><h2>aFRR cena [€/MWh]</h2><div class="chartbox"><canvas id="c3"></canvas></div></div>
</div>
<h2>Periódy (15-min)</h2>
<table><tr><th>čas</th><th>sys [MW]</th><th>net akt. [MW]</th><th>MW signál</th><th>odporúčanie</th><th>výkon</th><th>pravidlo</th></tr>{rows}</table>
<p style="color:#666;font-size:13px">VYBI = predaj do odchýlky · NABI = nákup z odchýlky · DRŽ = drž plán.
Odporúčanie je <b>čistý signál</b> bez SOC/rozpočtových obmedzení (tie rieši až livesim/simulátor).
Výkon batérie sa riadi veľkosťou signálu (spojito); mFRR aktivované alebo aFRR nad svojím priemerom = plný výkon.</p>"""

    script = """<script>
const L=__L__,SIG=__SIG__,DIS=__DIS__,CHG=__CHG__,DTL=__DTL__,AFRRV=__AFRRV__;
const AAP=__AAP__,AMP=__AMP__,AM5=__AM5__,AAN=__AAN__,AMN=__AMN__,RPOW=__RPOW__,RCOL=__RCOL__,ZCO=__ZCO__,VDT=__VDT__,VDTP=__VDTP__;
const M5L=__M5L__,SYS5=__SYS5__,SYS15R=__SYS15R__,SYS15RCOL=__SYS15RCOL__,RMIN=__RMIN__,RMINCOL=__RMINCOL__;
function z0(labels){return {label:'0',data:labels.map(()=>0),borderColor:'#999',borderWidth:1,borderDash:[2,2],tension:0,fill:false};}
function mk(id,labels,ds,extra){const el=document.getElementById(id);if(!el)return;new Chart(el,{type:'line',data:{labels:labels,datasets:ds},options:Object.assign({responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},elements:{point:{radius:0}}},extra||{})});}
function mkmix(id,labels,ds,extra){const el=document.getElementById(id);if(!el)return;new Chart(el,{data:{labels:labels,datasets:ds},options:Object.assign({responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},elements:{point:{radius:0}}},extra||{})});}
function mkbar(id,labels,ds,extra){const el=document.getElementById(id);if(!el)return;new Chart(el,{type:'bar',data:{labels:labels,datasets:ds},options:Object.assign({responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false}},extra||{})});}
const RAX={type:'linear',position:'right',grid:{drawOnChartArea:false},title:{display:true,text:'€/MWh'}};
mk('c0',L,[{label:'MW signál (realita)',data:SIG,borderColor:'#C0392B',backgroundColor:'rgba(192,57,43,.12)',fill:true,tension:.2,spanGaps:false},{label:'+vybíjacia hranica',data:DIS,borderColor:'#2E7D32',borderDash:[5,4],tension:.2},{label:'−nabíjacia hranica',data:CHG,borderColor:'#1F4E78',borderDash:[5,4],tension:.2},{label:'DT cena €/MWh',data:DTL,borderColor:'#15803d',borderDash:[2,3],borderWidth:1.5,tension:.2,spanGaps:true,yAxisID:'y1'},z0(L)],{scales:{y:{title:{display:true,text:'MW'}},y1:RAX}});
mk('c5',L,[{label:'ZCO €/MWh',data:ZCO,borderColor:'#B8860B',backgroundColor:'rgba(184,134,11,.10)',fill:true,stepped:true,spanGaps:false},{label:'DT cena €/MWh',data:DTL,borderColor:'#15803d',borderDash:[4,3],tension:.2,spanGaps:true},{label:'VDT finálne €/MWh',data:VDT,borderColor:'#9b59b6',borderWidth:2,tension:.2,spanGaps:true},{label:'VDT predbežné €/MWh',data:VDTP,borderColor:'#E67E22',borderWidth:1.5,borderDash:[3,3],tension:.2,spanGaps:true},z0(L)]);
mkbar('c1',L,[{label:'aFRR+',data:AAP,backgroundColor:'#C0392B'},{label:'mFRR+',data:AMP,backgroundColor:'#E8A317'},{label:'mFRR5',data:AM5,backgroundColor:'#2E7D32'},{label:'aFRR−',data:AAN,backgroundColor:'#9aa3ad'},{label:'mFRR−',data:AMN,backgroundColor:'#2E86C1'}],{scales:{x:{stacked:true},y:{stacked:true,grid:{color:(c)=>c.tick.value===0?'#888':'#eee'}}}});
mkbar('c4',M5L,[{label:'navrhovaný výkon % (VYBI + / NABI −)',data:RMIN,backgroundColor:RMINCOL,borderWidth:0,categoryPercentage:1.0,barPercentage:1.0}],{plugins:{legend:{display:false}},scales:{y:{min:-100,max:100,ticks:{stepSize:20},grid:{color:(c)=>c.tick.value===0?'#333':'#eee',lineWidth:(c)=>c.tick.value===0?2:1},title:{display:true,text:'výkon %'}}}});
mkmix('c2',M5L,[{type:'bar',label:'15-min priemer',data:SYS15R,backgroundColor:SYS15RCOL,borderWidth:0,categoryPercentage:1.0,barPercentage:1.0,order:2},{type:'line',label:'okamžitá odchýlka',data:SYS5,borderColor:'#11243B',borderWidth:1.7,tension:.2,spanGaps:false,order:0}],{plugins:{legend:{display:true}},scales:{y:{grid:{color:(c)=>c.tick.value===0?'#888':'#eee'}}}});
mk('c3',L,[{label:'aFRR €/MWh',data:AFRRV,borderColor:'#7030A0',backgroundColor:'rgba(112,48,160,.10)',fill:true,tension:.2,spanGaps:false},z0(L)]);
</script></body></html>"""
    script = (script.replace("__L__", json.dumps(labels)).replace("__SIG__", json.dumps(sig_vals))
              .replace("__DIS__", json.dumps(dis_line)).replace("__CHG__", json.dumps(chg_line))
              .replace("__DTL__", json.dumps(dt_line))
              .replace("__AAP__", json.dumps(act_ap)).replace("__AMP__", json.dumps(act_mp))
              .replace("__AM5__", json.dumps(act_m5)).replace("__AAN__", json.dumps(act_an))
              .replace("__AMN__", json.dumps(act_mn)).replace("__RPOW__", json.dumps(reco_pow))
              .replace("__RCOL__", json.dumps(reco_col)).replace("__ZCO__", json.dumps(zco_vals))
              .replace("__VDT__", json.dumps(vdt_vals))
              .replace("__VDTP__", json.dumps(vdtp_vals))
              .replace("__M5L__", json.dumps(m5_labels)).replace("__SYS5__", json.dumps(sys5))
              .replace("__RMIN__", json.dumps(reco_min)).replace("__RMINCOL__", json.dumps(reco_min_col))
              .replace("__SYS15R__", json.dumps(sys15rep)).replace("__SYS15RCOL__", json.dumps(sys15rep_col))
              .replace("__AFRRV__", json.dumps(afrr_vals)))
    return head + body + script


@app.get("/download")
def download(date: str):
    path = f"out/plan_{date}.xlsx"
    if not os.path.exists(path):
        return HTMLResponse(f"Súbor pre {date} neexistuje, vygeneruj plán znova.", status_code=404)
    return FileResponse(path, filename=f"plan_D1_{date}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ── VPP fleet admin/monitor router (aditívny, web/ balík) ──────────────────
# Read-only zoznam batérií + status + enable/disable/tick/add. Dormantný kým
# nie sú fleet tabuľky/batérie; bežiacej appky sa inak nedotýka. Auth middleware
# (ak AUTH_REQUIRED=1) ho chráni rovnako ako ostatné routes.
try:
    from web.fleet import router as _fleet_router
    app.include_router(_fleet_router)
except Exception as _e_fleet:
    print(f"[fleet] router init zlyhal — beh bez /fleet: {_e_fleet}")


if __name__ == "__main__":
    # Spustenie cez `python app.py` — background úlohy (livesim loop, scheduler,
    # backfill) sa štartujú cez lifespan, takže fungujú aj keď spustíš app cez
    # `uvicorn app:app` (Docker štandard).
    #
    # Premenné prostredia:
    #   APP_HOST  → bind adresa (default 127.0.0.1 lokálne; v Dockeri 0.0.0.0)
    #   APP_PORT  → port (default 8000; legacy alias PORT stále funguje)
    import uvicorn
    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", os.environ.get("PORT", "8000")))
    print(f"[app] štartujem na {host}:{port}")
    uvicorn.run(app, host=host, port=port)
