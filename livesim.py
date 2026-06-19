"""Živý simulátor (paper-trading) pre zvolený PRÍPAD.
- D-1 plán sa generuje IDENTICKY ako v simulácii (optimize_day, riadené prípadom: d1_step_min, allow_*…).
- RT vrstva = minútový regulátor (rt_controller.run_day s minútovým trace) so zvyšným rozpočtom batérie.
- Stav (SOC, kumulatívy) je perzistentný: minútové riadky sa APPENDUJÚ do CSV, meta do JSON.
- Po reštarte sa pokračuje od poslednej spracovanej minúty; ak súbor nie je, štartuje od `start_date`.
SOC drží simulátor (to, čo by si inak zadával v /rt). Všetky vstupy/parametre/výstupy sú v CSV pre spätnú analýzu.
"""
from __future__ import annotations
import os, json, math, time, datetime as dt
from typing import Optional
import numpy as np, pandas as pd
import case_config as cc, rt_controller as rtc, data_sources as ds
from optimizer import optimize_day
import combined_backtest as cb
import deviation_stats as dstats
try:
    import plan_overrides as po
except ImportError:                                           # ak modul nie je dostupný, override sa nepoužije
    po = None
import plan_store as ps                                       # strict-mode: plány MUSIA byť na disku


def _apply_zco_bias(price_arr, date, pv_total_kwh, step_min, zco_bias_w):
    """Ak zco_bias_w > 0, upraví cenu o očakávanú odchýlku.
    Cieľ: LP plán uvažuje ZCO predikciu → preferuje predaj/nákup v slotoch
    kde sa očakáva výhodná odchýlka (zco_pred ≠ dt_clearing).

    Cascade zdrojov predikcie:
        1. zco_advisor.predict_zco_for_slots(date) — 7-dňový priemer per 15-min slot
           (modernejšie, aktuálnejšie dáta z historianu).
        2. deviation_stats — starý hodinový profil odchýlky (legacy fallback).

    Formula: biased_price = dt_price + w × (zco_pred − dt_price)
        w=0   → bez biasu (čistá DT cena)
        w=1   → úplne podľa ZCO predikcie
        w=0.3 → mierne posunutie (odporúčané)

    Vracia biased_price array. Pri chybe / 0 weight vracia pôvodnú cenu.
    """
    w = float(zco_bias_w or 0.0)
    arr = np.asarray(price_arr, float)
    if w <= 0:
        return arr

    # PRIMÁRNE: zco_advisor (7-day per-slot mean)
    try:
        import zco_advisor as _zco
        import datetime as _dt
        date_iso = date.isoformat() if hasattr(date, "isoformat") else str(date)
        zco_pred_map = _zco.predict_zco_for_slots(date_iso, days_window=7)
        if zco_pred_map and len(zco_pred_map) >= 24:
            # Konvertuj na pole zodpovedajúce price_arr (96 alebo 24 slotov)
            n = len(arr)
            slots_per_step = max(1, step_min // 15) if step_min >= 15 else 1
            zco_arr = np.zeros(n, dtype=float)
            valid = 0
            for i in range(n):
                # i-tý slot v price_arr → odpovedá 15-min slot indexu
                # Pre step_min=60: i=hodina → slot_idx_15 = i*4 (priemer cez 4 sloty)
                # Pre step_min=15: i = slot_idx_15 priamo
                if step_min == 60:
                    # Priemer cez 4 × 15-min sloty hodiny
                    vals = [zco_pred_map.get(i*4 + k) for k in range(4)]
                    vals = [v for v in vals if v is not None]
                    if vals:
                        zco_arr[i] = sum(vals) / len(vals)
                        valid += 1
                else:
                    v = zco_pred_map.get(i)
                    if v is not None:
                        zco_arr[i] = float(v)
                        valid += 1
            # Aspoň 70% slotov musí byť pokrytých, inak fallback
            if valid >= int(0.7 * n):
                exp_zco_dt = zco_arr - arr   # rozdiel ZCO − DT per slot
                return dstats.biased_price(arr, exp_zco_dt, w)
    except Exception:
        pass

    # FALLBACK: legacy deviation_stats
    try:
        prof = dstats.load_profile()
        if not prof:
            return arr
        exp_zco_dt, _b, _t = dstats.estimate_day(prof, date, float(pv_total_kwh), step_min=step_min)
        exp_zco_dt = np.asarray(exp_zco_dt, float)[:len(arr)]
        return dstats.biased_price(arr, exp_zco_dt, w)
    except Exception:
        return arr

MIN_CSV = "out/imbalance_minute.csv"
CAL_JSON = "out/pv_calibration.json"
PRICE_TRAIN_CSV = "out/price_train_2026.csv"      # reálne hodinové clearing DT ceny (CZ — rovnako ako /simulacia)


# Perf DT-PRICE-CACHE (2026-06-13, user: "nepočítajú sa veci zbytočne?"): DT
# clearing ceny pre dátum sú NEMENNÉ (D-1 známe, market-level, nie per profil).
# Predtým sa _real_dt_hourly/_quarterly volali až 4× za deň × 44 dní a zakaždým
# čítali historian. Cache per (market, date) → načíta raz, zdieľané naprieč
# profilmi aj volaniami. Cache len NON-None (None = dáta ešte nie sú, retry).
_DT_HOURLY_CACHE = {}
_DT_QUARTERLY_CACHE = {}


def _dt_cache_key(date_iso: str) -> str:
    try:
        import market as _mk
        return f"{_mk.get_active_market()}:{date_iso}"
    except Exception:
        return f"?:{date_iso}"


def _real_dt_hourly(date_iso: str):
    """Vráti 24-prvkový rad reálnych hodinových DT clearing cien pre dátum, alebo None.

    Market-aware cez settlement.get_dt_real_hourly():
      - CZ → price_train_2026.csv (isot_eur)
      - SK → seps_sk historian (load_okte_dt_for_day, agreguje 15-min na hodiny)
    """
    _k = _dt_cache_key(date_iso)
    if _k in _DT_HOURLY_CACHE:
        return _DT_HOURLY_CACHE[_k]
    try:
        import settlement as _settlement
        _v = _settlement.get_dt_real_hourly(date_iso)
    except Exception:
        _v = None
    if _v is not None:
        _DT_HOURLY_CACHE[_k] = _v
    return _v


def _real_dt_quarterly(date_iso: str):
    """Vráti 96-prvkový rad reálnych 15-min DT clearing cien pre dátum, alebo None.

    Pre CZ rozšíri 24h ceny na 4× quarter; pre SK použije autentické 15-min ceny.
    """
    _kq = _dt_cache_key(date_iso)
    if _kq in _DT_QUARTERLY_CACHE:
        return _DT_QUARTERLY_CACHE[_kq]
    try:
        import settlement as _settlement
        _vq = _settlement.get_dt_real_quarterly(date_iso)
        if _vq is not None:
            _DT_QUARTERLY_CACHE[_kq] = _vq
        return _vq
    except Exception:
        return None

CSV_COLS = ["time", "date", "ts15",
            "sys_MW", "zco_eur", "dt_eur", "dt_real_eur", "vdt_eur", "ftv_kw",
            "ftv_min_real_kw", "ftv_hour_plan_kw", "ftv_min_curtailed_kw",
            "load_plan_kw", "load_min_real_kw",
            "mw_sig", "avg_react", "band_dis", "band_chg",
            "rt_dir", "rt_power_pct", "rt_reason",
            "plan_batt_kw", "plan_grid_kwh", "plan_curtail_kwh",
            # Bug VDT-DOUBLE (2026-06-11): D-1 baseline + VDT zložka MUSIA byť v CSV.
            # Bez nich render (app.py Bug X) nevie odlíšiť D-1 od D-1+VDT a pripočíta
            # VDT druhýkrát (plan_batt_kw už po Bug VDT-DATE-ISO VDT obsahuje).
            "plan_batt_dam_kw", "plan_batt_vdt_kw",
            "plan_grid_dam_kwh", "plan_grid_vdt_kwh",
            "batt_kw_realistic", "rt_rev_realistic_min",
            # Bug VDT-ARB-ORDER (2026-06-11): vdt_arb_min do CSV — efekt z CSV
            # (chC export, karty cez get_vdt_arb_series priorita 1) bez runtime
            # prepočtu z trades. Pôvodné vylúčenie (Bug #608 back-compat) padá
            # s csv_cols_v=15 resetom.
            "vdt_arb_min",
            "soc_kwh", "soc_pct", "budget_left_kwh",
            "dt_rev_min", "rt_rev_min", "cum_dt", "cum_rt", "cum_total"]
# Bug #608 (2026-06-08 hot-fix): vdt_arb_min + cum_vdt_arb sa NEZAPISUJÚ do CSV
# (zachovaná back-compat s 33-stĺpcovým historickým súborom bez headera). Sú to
# runtime stĺpce v `tr` DataFrame a používajú sa pri agregácii efektu v RAM.
# Reindex na CSV_COLS pri zápise ich automaticky odfiltruje.


def _safe_prof_tag(name: str) -> str:
    """Bezpečný kúsok mena profilu do názvu súboru (alfanum + _- )."""
    import re as _re
    return _re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "default"))[:48]


def paths(case: str, port: str = "8000", profile=None):
    """Market-aware + PER-PROFIL livesim CSV/meta paths —
    out/<market>/livesim_<case>__<profile>[_<port>].csv.

    Bug LIVESIM-PER-PROFILE (2026-06-13, krok 1 migrácie CSV→DB): livesim CSV bol
    ZDIEĽANÝ per (trh, case) → viac profilov do jedného súboru = full-backfill
    thrashing pri prepnutí (settings_sig je per-profil). Teraz je súbor PER-PROFIL
    → každý profil má vlastnú trajektóriu, dá sa posúvať na pozadí nezávisle
    (odomyká úlohu BG-ALL-PROFILES). profile=None → aktívny profil (back-compat:
    každý existujúci caller dostane súbor aktívneho profilu).

    SK/CZ trh má vlastné súbory (NESMÚ byť zdieľané). LIVESIM_DIR má prioritu (testy).
    """
    try:
        from core.profile_resolver import get_active as _ga_p
        _prof = _safe_prof_tag(_ga_p(profile))
    except Exception:
        _prof = _safe_prof_tag(profile)
    tag = f"{case}__{_prof}" + ("" if str(port) == "8000" else f"_{port}")
    env = os.environ.get("LIVESIM_DIR")
    if env:
        _csv = os.path.join(env, f"livesim_{tag}.csv")
        _meta = os.path.join(env, f"livesim_{tag}.meta.json")
        # migrácia self-checkne, či legacy dáta patria práve tomuto profilu
        _migrate_legacy_livesim(env, case, port, _csv, _meta, _prof)
        return _csv, _meta
    try:
        import market as _mk
        d = _mk.data_dir()                                                # out/cz alebo out/sk
    except Exception:
        d = os.path.join("out", "cz")
    os.makedirs(d, exist_ok=True)
    _csv = os.path.join(d, f"livesim_{tag}.csv")
    _meta = os.path.join(d, f"livesim_{tag}.meta.json")
    _migrate_legacy_livesim(d, case, port, _csv, _meta, _prof)
    return _csv, _meta


def _migrate_legacy_livesim(d, case, port, new_csv, new_meta, want_prof):
    """Jednorázová migrácia starého ZDIEĽANÉHO súboru (livesim_<case>[_<port>].csv)
    na per-profil názov — ALE len pre profil, ktorému dáta SKUTOČNE patria.

    Bug LEGACY-MIGRATE-WRONG-PROFILE (2026-06-13, user: "Simulacia_Coop mala obsah
    WV_simulacia"): zdieľaný súbor patril NAPOSLEDY simulovanému profilu, nie
    aktuálne aktívnemu. Pôvodná migrácia ho priradila aktívnemu → cross-kontaminácia.
    Fix: prečítaj profil zo settings_sig v legacy meta a migruj IBA ak sa zhoduje s
    `want_prof`. Inak legacy NEPREMENUJ (nový profil začne čerstvo = bezpečné)."""
    try:
        if os.path.exists(new_csv):
            return
        legacy_tag = case + ("" if str(port) == "8000" else f"_{port}")
        legacy_csv = os.path.join(d, f"livesim_{legacy_tag}.csv")
        legacy_meta = os.path.join(d, f"livesim_{legacy_tag}.meta.json")
        if not os.path.exists(legacy_csv):
            return
        # over, komu legacy dáta patria — profil zo settings_sig v legacy meta
        _legacy_prof = None
        try:
            if os.path.exists(legacy_meta):
                with open(legacy_meta) as _f:
                    _lm = json.load(_f)
                _sig = _lm.get("settings_sig") or ""
                import re as _re
                _m = _re.search(r'"profile"\s*:\s*"([^"]+)"', _sig if isinstance(_sig, str) else json.dumps(_sig))
                if _m:
                    _legacy_prof = _safe_prof_tag(_m.group(1))
        except Exception:
            _legacy_prof = None
        # migruj len keď dáta patria práve tomuto profilu (alebo profil sa nedá zistiť
        # → legacy je neistý, radšej NEpremenuj). want_prof je už safe-tag.
        if _legacy_prof is not None and _legacy_prof == want_prof:
            os.rename(legacy_csv, new_csv)
            if os.path.exists(legacy_meta):
                os.rename(legacy_meta, new_meta)
    except Exception:
        pass


def _cal_factor(month: str) -> float:
    try:
        with open(CAL_JSON) as f:
            cal = json.load(f)
        return float(cal.get(month, cal.get("_default", 1.0)))
    except Exception:
        return 1.0


# Cache imbalance_minute.csv — re-parsuje sa LEN ak sa zmenil na disku (mtime).
# Pre 15 MB CSV ušetríme ~1-2 s pri každom advance() volaní.
_MN_CACHE = {"mtime": None, "df": None}


def _minute_all(live_minutes=None, min_from_date=None, progress_cb=None) -> pd.DataFrame:
    """Všetky dostupné minútové dáta (sys, aktivácia, zco, isot=DT).

    CZ trh: zo `out/imbalance_minute.csv` (s mtime cache).
    SK trh: zo seps_sk.build_sk_minute_history() — SEPS reg.výkon (3-min historian +
            1-min realtime) + OKTE DT/VDT/ZCO (15-min). FRR aktivácie NaN
            (SK nepublikuje, použije sa CZ proxy z live_minutes).

    live_minutes: voliteľný DataFrame s dnešnými ŽIVÝMI minútami — primerge sa,
    pre prekrývajúce časy má prednosť (čerstvejšie). Použité na real-time sledovanie dneška.
    min_from_date: voliteľný spodný dátum — SK SEPS historian sa stiahne minimálne od tohto
    dátumu (default = today-90d). Použité keď chce užívateľ simulovať dlhší rozsah (napr.
    livesim start = 1.1.).

    Cache: per-market kľúčované; pre CZ mtime-based, pre SK timestamp-based (TTL 5 min).
    """
    # ── SK trh: build z historianu, bypass MIN_CSV ────────────────────────
    is_sk = False
    try:
        import market as _mk
        is_sk = (str(_mk.get_active_market()).lower() == "sk")
    except Exception:
        pass
    if is_sk:
        # Cache kľúč závisí od from_date — jinak by sa stará 90-d cache vrátila aj keď user chce väčší rozsah
        to_d = pd.Timestamp.now(tz="Europe/Bratislava").normalize().tz_localize(None)
        from_d = to_d - pd.Timedelta(days=90)
        if min_from_date is not None:
            try:
                _user_from = pd.Timestamp(min_from_date).normalize()
                if _user_from < from_d:
                    from_d = _user_from
            except Exception:
                pass
        sk_key = f"sk_minute_all_from_{from_d.strftime('%Y%m%d')}"
        cached = _MN_CACHE.get(sk_key)
        import time as _tt
        now_ts = _tt.time()
        # PERF SK-HIST-TTL (2026-06-14): build_sk_minute_history prechádza ~90 dní a každý
        # prebuduje z historianu (~4.4 s). Historické dni sú nemenné, dnešok aj tak prebíja
        # `live_minutes` merge nižšie → 5-min TTL prebudovával celé zbytočne každých 5 min.
        # Default 1 h (settlement včerajšej ZCO ~11:30 D+1 sa zachytí do hodiny). Env override.
        _sk_ttl = int(os.environ.get("SK_MINUTE_TTL_S", "3600"))
        if cached is not None and (now_ts - cached.get("ts", 0)) < _sk_ttl:
            mn = cached["df"]
        else:
            try:
                import seps_sk as _seps_h
                mn = _seps_h.build_sk_minute_history(from_date=from_d, to_date=to_d,
                                                     progress_cb=progress_cb)
                if mn is not None and not mn.empty:
                    if "time" in mn.columns:
                        mn["time"] = pd.to_datetime(mn["time"], errors="coerce")
                        try: mn["time"] = mn["time"].dt.tz_localize(None)
                        except (TypeError, AttributeError): pass
                    if "ts15" in mn.columns:
                        mn["ts15"] = pd.to_datetime(mn["ts15"], errors="coerce")
                        try: mn["ts15"] = mn["ts15"].dt.tz_localize(None)
                        except (TypeError, AttributeError): pass
                    mn["date"] = mn["time"].dt.date
                    for c in rtc.ACT_COLS:
                        if c in mn.columns:
                            mn[c] = pd.to_numeric(mn[c], errors="coerce").abs().fillna(0)
                _MN_CACHE[sk_key] = {"df": mn, "ts": now_ts}
            except Exception as _e:
                print(f"[livesim._minute_all SK] zlyhalo, fallback na CZ: {_e}")
                mn = None
        if mn is not None and not mn.empty:
            # Merge live_minutes (čerstvejšie majú prednosť)
            if live_minutes is not None and len(live_minutes):
                lm = live_minutes.copy()
                for col in ("time", "ts15"):
                    if col in lm.columns:
                        lm[col] = pd.to_datetime(lm[col], errors="coerce")
                mn = pd.concat([mn[~mn["time"].isin(lm["time"])], lm], ignore_index=True)
                mn = mn.sort_values("time").reset_index(drop=True)
                mn["date"] = mn["time"].dt.date
                for c in rtc.ACT_COLS:
                    if c in mn.columns:
                        mn[c] = pd.to_numeric(mn[c], errors="coerce").abs().fillna(0)
            return mn

    # ── CZ trh: pôvodný flow z MIN_CSV ────────────────────────────────────
    try:
        mtime = os.path.getmtime(MIN_CSV)
    except OSError:
        mtime = None
    if _MN_CACHE.get("df") is None or _MN_CACHE.get("mtime") != mtime:
        raw = pd.read_csv(MIN_CSV, parse_dates=["time", "ts15"])
        for col in ("time", "ts15"):
            if col in raw.columns:
                s = pd.to_datetime(raw[col], errors="coerce")
                try:
                    s = s.dt.tz_localize(None)
                except (TypeError, AttributeError):
                    pass
                raw[col] = s
        raw = raw.sort_values("time").reset_index(drop=True)
        raw["date"] = raw["time"].dt.date
        for c in rtc.ACT_COLS:
            if c in raw.columns:
                raw[c] = pd.to_numeric(raw[c], errors="coerce").abs().fillna(0)
        _MN_CACHE["df"] = raw
        _MN_CACHE["mtime"] = mtime
    mn = _MN_CACHE["df"]
    if live_minutes is not None and len(live_minutes):
        lm = live_minutes.copy()
        for col in ("time", "ts15"):
            if col in lm.columns:
                lm[col] = pd.to_datetime(lm[col], errors="coerce")
        # živé minúty majú prednosť pred prípadnými historickými s rovnakým časom
        mn = pd.concat([mn[~mn["time"].isin(lm["time"])], lm], ignore_index=True)
        mn = mn.sort_values("time").reset_index(drop=True)
        mn["date"] = mn["time"].dt.date
        for c in rtc.ACT_COLS:
            if c in mn.columns:
                mn[c] = pd.to_numeric(mn[c], errors="coerce").abs().fillna(0)
    # prep VŽDY na (možno) doplnených dátach — zabezpečí že sig sa spočíta aj pre live minúty
    mn = rtc.prep(rtc._ensure_act(mn))
    return mn


def _pv_hourly(cfg, date) -> np.ndarray:
    """24 hodinových kW výroby FTV (predikcia podľa nastavení prípadu). Fallback: nuly."""
    try:
        wx = ds.fetch_pv_forecast(cfg.lat, cfg.lon, cfg.kwp, cfg.tilt, cfg.azimuth, cfg.eff,
                                  date, date)
        wx = wx.copy()
        wx["h"] = pd.to_datetime(wx["time"]).dt.hour
        return wx.groupby("h")["kw"].mean().reindex(range(24)).fillna(0.0).values.astype(float)
    except Exception:
        return np.zeros(24, float)


def _plan_override(pp):
    """Mapuje nastavenia z formulára PLÁNU na parametre optimize_day (aby livesim plán = generátor)."""
    if not pp:
        return {}
    M = {"batt_kw": "batt_kw", "batt_kwh": "batt_kwh", "eff_c": "eff_c", "eff_d": "eff_d",
         "soc_min": "soc_min_pct", "soc_max": "soc_max_pct", "terminal_soc": "terminal_soc_pct",
         "grid_kw": "grid_kw", "grid_fee": "grid_fee", "cycle_cost": "cycle_cost",
         "min_spread": "min_spread_eur", "min_trade": "min_trade_mwh"}
    out = {}
    for k, opt in M.items():
        if k in pp and pp[k] is not None:
            try:
                out[opt] = float(pp[k])
            except (TypeError, ValueError):
                pass
    if "allow_curtail" in pp:
        out["allow_curtail"] = bool(pp["allow_curtail"])
    if "allow_grid_charge" in pp:
        out["allow_grid_charge"] = bool(pp["allow_grid_charge"])
    if "block_neg_import" in pp:
        out["block_neg_import"] = bool(pp["block_neg_import"])
    if "no_planned_discharge" in pp:
        out["block_planned_discharge"] = bool(pp["no_planned_discharge"])
    if "zco_bias_w" in pp:
        try:
            out["_zco_bias_w"] = float(pp["zco_bias_w"])             # _ prefix → konzument vie že to nejde do optimize_day
        except (TypeError, ValueError):
            pass
    return out


def _decompose_dtprof(price, pv, ex, im, ch, di, cu, grid_fee, cycle_cost, flags, cons_only=False, dt=1.0):
    """DT efekt rozložený na NEZÁVISLÉ per-element členy (BAT / FTV / LOAD).

    User (2026-06-16, TBB): toggle = čo sa počíta do obchodu. Vypnutie ktorejkoľvek
    zložky len odoberie jej člen — nesmie pokaziť ostatné (predtým monolitický
    net-meter vzorec sa „rozbil" keď vypadol LOAD → záporný efekt z nákladu spotreby).

    Rekonštrukcia sub-streamov z trace (bilancia uzla: pv+di+im = load+ch+ex+cu):
      load z bilancie; PV→load (free); batt→load (DI_LOAD); export split FTV/batt
      (clamp na dané `ex`); import split load/batt (clamp na dané `im`).
    Tým je zaručené ex_ftv+ex_batt=ex a im_load+im_batt=im → pri VŠETKÝCH zapnutých
    je súčet IDENTICKÝ s pôvodným vzorcom (golden-overené).

    Členy:
      e_batt (trade_batt): predaj vybitia (ex_batt + di_load LEN ak LOAD mimo zmluvy)
                           − nákup grid-nabíjania (im_batt × cena+poplatok) − cyklus
      e_ftv  (trade_ftv):  predaj FTV prebytku do siete (ex_ftv × cena)
      e_load (trade_load): náklad odberu zo siete (im_load × cena+poplatok)
    cons_only: distribučný poplatok len na spotrebu (nie na nabíjanie batérie).
    """
    import numpy as _np
    price = _np.asarray(price, float); pv = _np.asarray(pv, float)
    ex = _np.asarray(ex, float); im = _np.asarray(im, float)
    ch = _np.asarray(ch, float); di = _np.asarray(di, float); cu = _np.asarray(cu, float)
    # BUG 15-MIN-DTPROF (2026-06-18): ch/di sú _charge_kw/_discharge_kw v kW, kým
    # ex/im/pv/cu sú kWh ZA SLOT. Pri 60-min kW == kWh/h (numericky), ale pri 15-min
    # je kW 4× väčšie než kWh za 15-min slot → batériový DT člen 4× nafúknutý.
    # Prepočet na kWh za slot cez dt (=step/60). dt=1.0 → no-op (golden nezmenené).
    _dt = float(dt) if dt else 1.0
    if abs(_dt - 1.0) > 1e-9:
        ch = ch * _dt; di = di * _dt
    load = _np.maximum(pv + di + im - ch - ex - cu, 0.0)
    pv_load = _np.minimum(pv, load)
    pv_batt = _np.minimum(_np.maximum(pv - pv_load, 0.0), ch)
    di_load = _np.minimum(di, _np.maximum(load - pv_load, 0.0))
    ex_ftv = _np.minimum(ex, _np.maximum(pv - pv_load - pv_batt - cu, 0.0))
    ex_batt = _np.maximum(ex - ex_ftv, 0.0)
    im_load = _np.minimum(im, _np.maximum(load - pv_load - di_load, 0.0))
    im_batt = _np.maximum(im - im_load, 0.0)
    tb = bool(flags.get("trade_batt", True)); tf = bool(flags.get("trade_ftv", True)); tl = bool(flags.get("trade_load", True))
    _chg_fee = 0.0 if cons_only else float(grid_fee)
    batt_sell = ex_batt + (0.0 if tl else 1.0) * di_load
    e_batt = ((price * batt_sell - (price + _chg_fee) * im_batt - cycle_cost * (ch + di) / 2.0) / 1000.0) if tb else _np.zeros_like(price)
    e_ftv = ((price * ex_ftv) / 1000.0) if tf else _np.zeros_like(price)
    e_load = ((-(price + float(grid_fee)) * im_load) / 1000.0) if tl else _np.zeros_like(price)
    return e_batt + e_ftv + e_load


def _day_plan(cfg, date, mn_day, soc_init_pct=None, plan_params=None):
    """STRICT MODE: D-1 plán pre daný deň sa NIKDY negeneruje za behu — číta sa z plan_store.
    Ak plán pre (date, step_min, kind) neexistuje na disku, vyhodí PlanMissingError.
    Plán treba najprv vygenerovať cez /plan, /dentrh alebo /plan_batch.

    Vracia: sch, dtprof_per_period, period_step_min, d1_cycles, price_per_period, pv_per_period."""
    step_min = int(getattr(cfg, "d1_step_min", 60))
    kind = "dentrh" if step_min == 15 else "plan"
    date_iso = pd.Timestamp(date).date().isoformat()
    plan = ps.load_plan(date_iso, step_min, kind)            # PlanMissingError ak chyba
    schedule = plan["schedule"]
    n = 96 if step_min == 15 else 24
    # rekonštrukcia DataFrame so správnym poradím stĺpcov
    sch = pd.DataFrame(schedule)
    if "hour" not in sch.columns:
        sch["hour"] = list(range(n))
    if "soc_kwh" not in sch.columns:
        # ak chýba (staršie plány) — dopočítame zo soc_pct a batt_kwh
        bkwh = float(plan.get("params", {}).get("batt_kwh", cfg.batt_kwh))
        sch["soc_kwh"] = np.asarray(sch.get("soc_pct", [50.0]*n), float) / 100.0 * bkwh
    # finančný stream a kumulatívy z uloženého plánu (settle_price)
    price = np.asarray(schedule.get("price_eur", [0.0]*n), float)
    pvper = np.asarray(schedule.get("pv_kwh", [0.0]*n), float)
    ex = np.asarray(schedule.get("_export_kwh", [0.0]*n), float)
    im = np.asarray(schedule.get("_import_kwh", [0.0]*n), float)
    ch = np.asarray(schedule.get("_charge_kw", [0.0]*n), float)
    di = np.asarray(schedule.get("_discharge_kw", [0.0]*n), float)
    p_params = plan.get("params", {})
    grid_fee = float(p_params.get("grid_fee", getattr(cfg, "grid_fee", 22.0)))
    cycle_cost = float(p_params.get("cycle_cost", getattr(cfg, "cycle_cost", 2.0)))
    bkwh = float(p_params.get("batt_kwh", cfg.batt_kwh))
    # DIST-FEE-CONSUMPTION-ONLY (2026-06-15): poplatok len na spotrebný import
    # (nabíjanie z FTV aj grid-arbitráž vyňaté). Default vypnuté → golden nezmenené.
    # KOMERČNÁ SKUPINA: DT efekt = súčet NEZÁVISLÝCH per-element členov (BAT/FTV/LOAD).
    # Vypnutie zložky (toggle) len odoberie jej člen; všetko zapnuté = pôvodný net-meter
    # vzorec (golden-overené). Viď _decompose_dtprof.
    _cu_dp = np.asarray(schedule.get("_curtail_kwh", schedule.get("plan_curtail_kwh", [0.0]*n)), float)
    _jlf_dp = (p_params.get("joint_lp", {}) or {})
    _flags_dp = ({"trade_batt": bool(_jlf_dp.get("trade_batt", True)),
                  "trade_ftv": bool(_jlf_dp.get("trade_ftv", True)),
                  "trade_load": bool(_jlf_dp.get("trade_load", True))}
                 if _jlf_dp.get("enabled") else {"trade_batt": True, "trade_ftv": True, "trade_load": True})
    _dt_dp = float(step_min) / 60.0
    dtprof = _decompose_dtprof(price, pvper, ex, im, ch, di, _cu_dp, grid_fee, cycle_cost,
                               _flags_dp, cons_only=bool(p_params.get("dist_fee_consumption_only", False)),
                               dt=_dt_dp)
    # BUG 15-MIN-DTPROF: ch/di sú kW → cykly = kWh throughput = sum(kW)·dt /2 /bkwh.
    d1_cycles = float((ch.sum() + di.sum()) * _dt_dp / 2 / bkwh) if bkwh > 0 else 0.0
    # RT mask zo zapečeného plánu (preferované) — ak rt_freedom=False v čase ukladania, je už upravená
    _rtm = plan.get("rt_mask")
    rt_mask_eff = np.asarray(_rtm, float) if (_rtm and len(_rtm) == n) else None
    return sch, dtprof, step_min, d1_cycles, price, pvper, rt_mask_eff


def _period_index(ts15, day_start, step):
    d = (pd.Timestamp(ts15) - pd.Timestamp(day_start))
    return int(d.total_seconds() // (step*60))


def _run_physical_day(cfg, mn_day, sch, day, step, bd, bc, soc0, dev_budget_kwh, dt_bias_k=None,
                       rt_mask=None, pv_min_kw=None, ftv_balance_on=False,
                       ftv_lookahead_h=4.0, ftv_persistence_throttle=True,
                       rt_no_worsen_dev=True, ftv_strict_plan=True,
                       pv_plan_kw=None, ftv_strict_deadband_kw=5.0,
                       grid_kw_import=None, grid_kw_export=None,
                       load_min_kw=None, load_plan_kw=None,
                       enforce_realistic=True, audit_today_state=None,
                       audit_soc_reserve_pct=0.0,
                       audit_rt_persistence_slots=4,
                       audit_future_horizon_slots=96,
                       rt_engine="v1", rt2_params=None):
    """Jedna fyzická batéria – deleguje na rt_controller.run_day_physical (jeden zdroj pravdy).

    Bug RT-INLINE-AUDIT (2026-06-11): defaultne `enforce_realistic=True` → rt_controller
    integruje soc s ORZANÝM batt (= fyzická realita: grid_export/import + FTV + load).
    Tým sa rt_controller-internal soc ZHODUJE s realitou, žiadna divergencia cez deň,
    žiadne predčasné SOC clip aktivácie v drahých hodinách (= žiadna píla 20-21h).
    audit_today_state: dict pre audit_capacity per minútu (chráni SOC pre budúce zazmluvnené sloty).
    """
    gkw = np.asarray(sch["grid_kwh"].values, float) / (step/60.0)
    return rtc.run_day_physical(mn_day, np.asarray(sch["batt_kw"].values, float), day, step,
                                bd, bc, cfg.w_sys, cfg.sys_orient, soc0=soc0,
                                dev_budget_kwh=dev_budget_kwh,
                                dt_bias_k=(cfg.dt_bias_k if dt_bias_k is None else float(dt_bias_k)),
                                grid_kw_arr=gkw, grid_cap=cfg.grid_kw, return_trace=True,
                                grid_cap_import=grid_kw_import, grid_cap_export=grid_kw_export,
                                rt_mask=rt_mask, pv_min_kw=pv_min_kw,
                                ftv_balance_on=ftv_balance_on,
                                ftv_lookahead_h=ftv_lookahead_h,
                                ftv_persistence_throttle=ftv_persistence_throttle,
                                rt_no_worsen_dev=rt_no_worsen_dev,
                                ftv_strict_plan=ftv_strict_plan,
                                pv_plan_kw=pv_plan_kw,
                                ftv_strict_deadband_kw=ftv_strict_deadband_kw,
                                load_min_kw=load_min_kw,
                                load_plan_kw=load_plan_kw,
                                enforce_realistic=enforce_realistic,
                                audit_today_state=audit_today_state,
                                audit_soc_reserve_pct=audit_soc_reserve_pct,
                                audit_rt_persistence_slots=audit_rt_persistence_slots,
                                audit_future_horizon_slots=audit_future_horizon_slots,
                                rt_engine=rt_engine, rt2_params=rt2_params,
                                # RT-PRIORITY: z cfg (plan_params), default False = staré správanie
                                rt_overrides_plan=bool(getattr(cfg, "rt_overrides_plan", False)))


def _load_meta(meta_path):
    # Bug META-PARTIAL-READ (2026-06-13): ak sa meta práve zapisuje neatomicky, json.load
    # padne na polovičnom súbore → None → TICHÝ reset → wipe CSV → backfill. So _save_meta_atomic
    # je to ošetrené, ale retry na 1 pokus je lacná poistka (FileNotFound = skutočne chýba → None).
    for _attempt in range(2):
        try:
            with open(meta_path) as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except Exception:
            if _attempt == 0:
                import time as _t
                _t.sleep(0.05)
                continue
            return None
    return None


def _save_meta_atomic(meta_path, meta):
    """Atomický zápis meta.json — temp súbor + os.replace (atomic rename na rovnakom FS).
    Bez tohto open(path,'w') hneď truncatne súbor → súbežný čitateľ (GET/backfill) dostane
    prázdny/polovičný JSON → _load_meta None → reset → wipe CSV → kaskáda backfillov
    ("druhé otvorenie počíta od začiatku"). os.replace = čitateľ vidí starý ALEBO nový celý."""
    import tempfile
    _d = os.path.dirname(meta_path) or "."
    fd, tmp = tempfile.mkstemp(dir=_d, prefix=".meta_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, meta_path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def carried_soc_for_date(case: str, port: str = "8000", date=None, profile=None) -> dict:
    """Vráti dict so SOC ktoré livesim použije ako soc_init pre daný dátum (carried z predošlého dňa).
    Pri date=None vráti aktuálny soc_after_done. Pri date=zajtra vráti predpoklad = soc_after_done.
    Pri date=minulosť (už spočítané v CSV) vráti SOC z konca predošlého dňa.

    Vracia: {'soc_kwh': float, 'soc_pct': float, 'as_of_date': str, 'note': str} alebo None ak nie sú dáta.
    """
    csv_path, meta_path = paths(case, port, profile)
    meta = _load_meta(meta_path)
    if meta is None:
        return None
    bkwh = float(meta.get("params", {}).get("batt_kwh", 200.0))
    done_through = meta.get("done_through")
    soc_after = float(meta.get("soc_after_done", bkwh * 0.5))

    if date is None:
        return dict(soc_kwh=soc_after, soc_pct=soc_after / bkwh * 100,
                    as_of_date=done_through or "?", note="aktuálny SOC po done_through")

    try:
        target = pd.Timestamp(date).date()
    except (TypeError, ValueError):
        return None
    # ak target > done_through (budúce): livesim štartuje so soc_after_done (najlepší dostupný odhad)
    if done_through is None or target > pd.Timestamp(done_through).date():
        return dict(soc_kwh=soc_after, soc_pct=soc_after / bkwh * 100,
                    as_of_date=done_through or "?",
                    note=f"predpoklad pre {target}: SOC ako po poslednom dokončenom dni ({done_through})")
    # ak target je v dokončenom rozsahu: prečítaj koniec predošlého dňa z CSV
    try:
        df = pd.read_csv(csv_path, parse_dates=["time"], usecols=["time", "date", "soc_kwh"])
    except (FileNotFoundError, ValueError):
        return None
    if df.empty:
        return None
    prev = (target - pd.Timedelta(days=1)).isoformat()
    sub = df[df["date"] == prev]
    if sub.empty:
        # ak nemáme predošlý deň, vrátime soc_after_done s poznámkou
        return dict(soc_kwh=soc_after, soc_pct=soc_after / bkwh * 100,
                    as_of_date=done_through or "?",
                    note=f"pre {target} nemáme {prev} v logu — fallback na soc_after_done")
    soc_kwh = float(sub["soc_kwh"].iloc[-1])
    return dict(soc_kwh=soc_kwh, soc_pct=soc_kwh / bkwh * 100,
                as_of_date=prev,
                note=f"koniec dňa {prev}, ako začalo {target}")


def advance(case: str, start_date, port: str = "8000", now=None, base_case=None, d1_step_min=None,
            live_minutes=None, rt_params=None, plan_params=None, use_rt_override=None,
            profile: Optional[str] = None, progress_cb=None) -> dict:
    """Posunie simuláciu po teraz (alebo `now`). Dopočíta nové minúty, APPENDuje do CSV, uloží meta.
    base_case: z ktorého prípadu vziať NASTAVENIA (cfg); `case` ostáva kľúčom logu/súboru.
    d1_step_min: prepíše granularitu plánu (60=D-1 hodinový, 15=denný trh 15-min).
    live_minutes: dnešné ŽIVÉ minúty (provizórna ZCO=odhad) na real-time sledovanie dneška.
    use_rt_override: ak nie None, prepíše cfg.use_rt (True/False). Pre čistý plán bez RT vrstvy → False.
    Vracia súhrn pre stránku."""
    # ADVANCE-TIMING (krok 0 merania): ľahké perf_counter accumulators, summary print
    # gatovaný env LIVESIM_TIMING=1. Žiadna zmena správania — len meranie kde sa tratí čas.
    _T_ADV0 = time.perf_counter()
    _TMR = {"minload": 0.0, "io": 0.0, "effectdb": 0.0, "loop": 0.0}
    cfg = cc.load_case(base_case or case)
    if d1_step_min is not None:
        cfg.d1_step_min = int(d1_step_min)
    if use_rt_override is not None:
        cfg.use_rt = bool(use_rt_override)
    # CRITICAL: užívateľské nastavenia z /plan template/profile musia override-ovať cfg defaulty
    # z case_config PRED rtc.apply_case (RT engine používa cfg.batt_kw, cfg.batt_kwh, cfg.kwp atď.
    # ako globály — ak ich nezmeníme, simulácia bude bežať s defaultami JSON-u, nie s profilom).
    # Cieľ: dva rozdielne profile (napr. "Laugaricio" 600/600/1200 vs "default" 100/200/99) musia
    # produkovať rôzne simulácie aj keď používajú ten istý case_config base.
    if plan_params:
        # Plain float overrides (form a cfg majú rovnaký formát)
        _PLAIN_KEYS = ["lat", "lon", "kwp", "tilt", "azimuth", "eff",
                       "batt_kw", "batt_kwh", "eff_c", "eff_d",
                       "soc_init", "terminal_soc",
                       "grid_kw", "grid_fee", "cycle_cost",
                       "min_spread", "min_trade", "price_scale", "pv_scale",
                       "zco_bias_w"]
        for _k in _PLAIN_KEYS:
            if _k in plan_params and plan_params[_k] is not None:
                try:
                    setattr(cfg, _k, float(plan_params[_k]))
                except (TypeError, ValueError):
                    pass
        # SOC limity: form má % (0-100), cfg má zlomok (0-1) — konvert
        for _k in ("soc_min", "soc_max"):
            if _k in plan_params and plan_params[_k] is not None:
                try: setattr(cfg, _k, float(plan_params[_k]) / 100.0)
                except (TypeError, ValueError): pass
        # Niektoré form polia sú v % a v cfg tiež v % (soc_init, terminal_soc môžu byť rôzne v rôznych verziách)
        # Tu predpokladáme cfg = zlomok, form = % — over to:
        # → soc_init/terminal_soc v case_config sú float (50.0 = 50% alebo 0.5?). Pozri default.json:
        #   "soc_init": 0.50 → zlomok. Form má 50 → potrebuje /100.
        # Korekcia: ak je hodnota > 1, predpokladaj % a podeľ 100.
        for _k in ("soc_init", "terminal_soc"):
            v = getattr(cfg, _k, None)
            if v is not None and v > 1.0:
                setattr(cfg, _k, v / 100.0)
        # Boolean flag
        if "allow_curtail" in plan_params and plan_params["allow_curtail"] is not None:
            try: cfg.allow_curtail = bool(plan_params["allow_curtail"])
            except Exception: pass
        # RT-PRIORITY (2026-06-13): keď True, RT povel preváži plán (audit jediná brzda) —
        # dev_budget strop neobmedzuje RT odchýlku. Default False = staré správanie.
        if "rt_overrides_plan" in plan_params and plan_params["rt_overrides_plan"] is not None:
            try: cfg.rt_overrides_plan = bool(plan_params["rt_overrides_plan"])
            except Exception: pass
        # ENV fallback pre rýchly test na deve (RT_OVERRIDES_PLAN=1) bez UI toggle
        if os.environ.get("RT_OVERRIDES_PLAN") == "1":
            cfg.rt_overrides_plan = True
        print(f"[livesim.advance] cfg merge z plan_params: "
              f"batt={cfg.batt_kw:.0f}kW/{cfg.batt_kwh:.0f}kWh, "
              f"FTV={cfg.kwp:.0f}kWp, "
              f"SOC={cfg.soc_min*100:.0f}–{cfg.soc_max*100:.0f}%, "
              f"grid={cfg.grid_kw:.0f}kW")
    # Bug PROFILE-CFG-SSOT (2026-06-11 v noci): PROFIL je zdroj pravdy pre fyziku.
    # plan_params prichádzajú z UI stavu — ak v momente (re)simulácie nebol uložený
    # (čerstvý profil / po resete), cfg ostal na case defaultoch (batt 100 kW!) a deň
    # sa NAVŽDY zapísal s orezanou batériou: real=±100 kW, rt_power_pct numericky
    # = kW (dev/e_h/BK*100 pri BK=100) → graf ukázal ±230 000 kW a výkon "zmizol"
    # v odchýlke. Fyzické parametre profilu prebijú UI/case defaulty VŽDY.
    try:
        from core.profile_resolver import get_active as _ga_cfg
        import profiles as _pr_cfg
        _prof_cfg_name = _ga_cfg(profile)
        _prof_plan_cfg = (((_pr_cfg.load_profile(_prof_cfg_name) or {}).get("plan") or {})
                          if _prof_cfg_name else {})
        _PHYS_KEYS = ["batt_kw", "batt_kwh", "eff_c", "eff_d", "kwp",
                      "grid_kw", "grid_kw_import", "grid_kw_export"]
        _overridden = []
        for _k in _PHYS_KEYS:
            _v = _prof_plan_cfg.get(_k)
            if _v is None:
                continue
            try:
                _v_f = float(_v)
            except (TypeError, ValueError):
                continue
            if _v_f <= 0 and _k in ("batt_kw", "batt_kwh"):
                continue                                   # prázdny profil → nechaj cfg
            _cur = getattr(cfg, _k, None)
            if _cur is None or abs(float(_cur) - _v_f) > 1e-6:
                setattr(cfg, _k, _v_f)
                _overridden.append(f"{_k}:{_cur}→{_v_f:g}")
        for _k in ("soc_min", "soc_max"):
            _v = _prof_plan_cfg.get(_k)
            if _v is not None:
                try:
                    setattr(cfg, _k, float(_v) / 100.0)
                except (TypeError, ValueError):
                    pass
        if _overridden:
            print(f"[livesim PROFILE-CFG-SSOT] {_prof_cfg_name}: cfg prepísané z profilu: "
                  f"{', '.join(_overridden[:6])}")
    except Exception as _e_ssot:
        print(f"[livesim PROFILE-CFG-SSOT] zlyhal: {_e_ssot}")
    rtc.apply_case(cfg)
    csv_path, meta_path = paths(case, port, profile)
    now = pd.Timestamp(now) if now is not None else pd.Timestamp(dt.datetime.now())
    now = now.tz_localize(None) if now.tzinfo else now
    today = now.normalize()
    start_date = pd.Timestamp(start_date).normalize()
    prov_date = None
    if live_minutes is not None and len(live_minutes):
        prov_date = today.date()           # dnešok je provizórny (odhad ZCO)

    bkwh = cfg.batt_kwh
    # #27: VDT closed-price LEN PRE HISTÓRIU vo zvolenom rozsahu dní (od–do). Reálny
    # dnešok + budúcnosť VŽDY na živej ceste (nikdy neprepisovať). Prázdny rozsah =
    # živá cesta nezmenená pre celú históriu (default → golden bez zmeny).
    _vdt_cl_from = str((plan_params or {}).get("vdt_closed_from", "") or "")[:10]
    _vdt_cl_to = str((plan_params or {}).get("vdt_closed_to", "") or "")[:10]
    try:
        _vdt_real_today = today.date().isoformat()
    except Exception:
        import datetime as _dtc27
        _vdt_real_today = _dtc27.date.today().isoformat()
    def _vdt_use_closed_for(_day_iso):
        if not _vdt_cl_from or not _vdt_cl_to:
            return False
        _di = str(_day_iso)[:10]
        if _di >= _vdt_real_today:          # dnešok + budúcnosť VŽDY živé
            return False
        return _vdt_cl_from <= _di <= _vdt_cl_to
    def _vdt_batt_kw_for(_prof, _day_iso, _dt_h=0.25):
        import vdt_state as _vsx
        if _vdt_use_closed_for(_day_iso):
            return _vsx.get_closedprice_batt_kw(_prof, _day_iso, dt_h=_dt_h)
        return _vsx.get_realized_batt_kw(_prof, today_iso=_day_iso, dt_h=_dt_h)
    def _vdt_kwh_view_for(_prof, _day_iso):
        import vdt_state as _vsx
        if _vdt_use_closed_for(_day_iso):
            return ((_vsx._compute_closedprice_day(_prof, _day_iso) or {}).get("kwh_batt_view")
                    or [0.0] * 96)
        return ((_vsx._load_vdt_realized(_prof, _day_iso) or {}).get("kwh_batt_view")
                or [0.0] * 96)
    # PODPIS nastavení – ak sa zmenia (plán/RT/granularita), log je neaktuálny → prepočítaj odznova
    _po = _plan_override(plan_params)
    # podpis šablóny overridov (per-day override sa berie ako "súčasť dnešného stavu" — log dneška ho aplikuje
    # živo cez `_day_plan`; pri zmene globálnej šablóny resetneme aj historické dni, aby boli v sync)
    # kind šablóny pre livesim závisí od d1_step_min (15 → dentrh, 60 → plan)
    step_min_now = int(getattr(cfg, "d1_step_min", 60))
    kind_now = "dentrh" if step_min_now == 15 else "plan"
    po_sig = ""
    if po is not None:
        try:
            tmpl_mult = np.round(po.load_template(kind_now), 3).tolist()
            tmpl_rt = po.load_template_rt(kind_now).tolist()
            po_sig = ("mult:" + ",".join(f"{x:g}" for x in tmpl_mult)
                       + ";rt:" + "".join("0" if (np.isfinite(v) and v < 0.5) else "1" for v in tmpl_rt))
        except Exception:
            po_sig = ""
    # podpis OBSAHU uložených plánov v rozsahu start_date → VČERA (strict mode):
    # ak sa plán pre niektorý MINULÝ deň regeneruje, settings_sig sa zmení → log sa prepočíta.
    # Bug PLAN-SIG-TODAY (2026-06-11): dnešok NESMIE byť v plan_sigs — dnešný deň sa pri
    # každom advance prepočítava čerstvo z plan_store, takže zmena dnešného plánu reset
    # nepotrebuje. Pôvodne tu bol aj today → každý regen dnešného plánu (vrátane
    # SOC-CONT-V3 auto-regenu PO advance!) zmenil sig → FULL backfill celej histórie
    # pri ďalšom requeste → /livesim sa otváral "neskutočne dlho", opakovane.
    # Bug PLAN-SIG-CONTENT (2026-06-13, user: "výpočty sa robia znova po reštarte"):
    # plan_sigs predtým používal `generated_at` (timestamp regenu). Auto-regen
    # (SOC-CONT-V3) počas backfillu regeneroval plány → generated_at sa zmenil →
    # pri ďalšom advance/reštarte sig NESEDEL → FULL reset → backfill → auto-regen →
    # nekonečná slučka, hotový backfill sa zahodil. Fix: sig = CONTENT HASH plánu
    # (bez timestamp polí). Regen s rovnakým OBSAHOM → rovnaký hash → žiadny reset.
    plan_sigs = {}
    try:
        import hashlib as _hl
        # Bug PLAN-SIG-CONSUMED (2026-06-14): predtým sa hashoval CELÝ plán (mínus pár
        # timestampov). Pre VDT profily (napr. VW) auto-regen/VDT advisor prepisoval do DB
        # `summary` (prepočítaná ekonomika) a iné NEkonzumované polia → content hash sa menil
        # každý advance → SIG-RESET na plan_store → FULL re-sim celej histórie (~23 s) každý
        # tick (Coop bez VDT to nemal). Fix: hashuj LEN polia, ktoré `_day_plan` reálne
        # konzumuje a ktoré menia simuláciu — schedule nominácia + params poplatky + rt_mask,
        # floaty zaokrúhlené na 6 desat. Kozmetika/regen churn (summary, mults, meta,
        # generated_at) reset NEvyvolá. Korektné: zmena, čo ovplyvní sim, sig stále zmení.
        _SCHED_KEYS = ("batt_kw", "price_eur", "pv_kwh", "_export_kwh", "_import_kwh",
                       "_charge_kw", "_discharge_kw", "soc_pct")
        _PARAM_KEYS = ("grid_fee", "cycle_cost", "batt_kwh")
        def _r6(_v):
            try:
                return round(float(_v), 6)
            except (TypeError, ValueError):
                return _v
        def _plan_sim_sig(_p):
            _sch = _p.get("schedule") or {}
            _par = _p.get("params") or {}
            _core = {
                "sched": {_k: [_r6(_x) for _x in (_sch.get(_k) or [])]
                          for _k in _SCHED_KEYS if _k in _sch},
                "params": {_k: _r6(_par.get(_k)) for _k in _PARAM_KEYS if _k in _par},
                "rt_mask": _p.get("rt_mask"),
            }
            _pj = json.dumps(_core, sort_keys=True, default=str)
            return _hl.md5(_pj.encode("utf-8")).hexdigest()[:16]
        _sig_end = pd.Timestamp(today).normalize() - pd.Timedelta(days=1)
        if _sig_end >= pd.Timestamp(start_date).normalize():
            for _d in pd.date_range(pd.Timestamp(start_date), _sig_end, freq="D"):
                _diso = _d.date().isoformat()
                _p = ps.load_plan_safe(_diso, step_min_now, kind_now)
                if _p:
                    plan_sigs[_diso] = _plan_sim_sig(_p)
    except Exception:
        pass
    # FTV scenáre per-dátum — keď user pridá/zmení scenár, log sa resetuje a livesim ho prevezme
    ftv_scen_sig = {}
    try:
        import ftv_scenarios as _fs
        for _d in pd.date_range(pd.Timestamp(start_date), today, freq="D"):
            _diso = _d.date().isoformat()
            if _fs.has_scenario(_diso):
                _sc = _fs.load_scenario(_diso)
                if _sc:
                    ftv_scen_sig[_diso] = _sc.get("saved_at", "?")
    except ImportError:
        pass
    # Bug LIVESIM-PROFILE-SIG (2026-06-11): livesim CSV je per trh+case (ZDIEĽANÝ medzi
    # profilmi), ale simulácia beží s parametrami + VDT paper trades AKTÍVNEHO profilu
    # (Bug BB). Bez profilu v sig prepnutie profilu nechá v CSV históriu nasimulovanú pod
    # INÝM profilom (vrátane SOC + soc_after_done) → "SOC z ničoho" na hranici dní
    # (06-10: večerné VDT nabíjanie _3 v CSV chýbalo, deň končil 16 % namiesto rastu).
    # Profil v sig ⇒ prepnutie profilu vyvolá reset + backfill pod novým profilom.
    try:
        from core.profile_resolver import get_active as _ga_sig
        _prof_sig = str(_ga_sig(profile) or "")
    except Exception:
        _prof_sig = ""
    # Bug PROFILE-RT2-SIG (2026-06-12): sig obsahoval len MENO profilu — zmena rt
    # parametrov profilu (engine v1/v2, rt2_* prahy) reset nevyvolala → história v CSV
    # ostala nasimulovaná so STARÝMI parametrami (dnešok sa pritom počíta čerstvo →
    # nekonzistencia karta vs história). Obsah rt sekcie do sig ⇒ zmena vyvolá backfill.
    # Plan sekciu NEpridávame: tá tečie cez plan_params/_po (už v sig) a profil sa
    # auto-ukladá pri každom /plan POST — celý profil v sig by spúšťal backfill zbytočne.
    _prof_rt_sig = {}
    if _prof_sig:
        try:
            import profiles as _pr_sig
            _pdata_sig = _pr_sig.load_profile(_prof_sig) or {}
            _rt_sec = _pdata_sig.get("rt") or {}
            _prof_rt_sig = {k: _rt_sec.get(k) for k in sorted(_rt_sec)}
        except Exception:
            _prof_rt_sig = {}
    sig = {"plan": {k: _po.get(k) for k in sorted(_po)},
           "rt": {k: (round(float(v), 4) if isinstance(v, (int, float)) else v)
                  for k, v in sorted((rt_params or {}).items())},
           "d1_step": int(getattr(cfg, "d1_step_min", 60)),
           "use_rt": bool(getattr(cfg, "use_rt", True)),
           "batt_kwh": float(bkwh), "batt_kw": float(cfg.batt_kw),
           "curtail_case": bool(cfg.allow_curtail),
           "po_template": po_sig,
           "plan_store": plan_sigs,
           "ftv_scenarios": ftv_scen_sig,
           "profile": _prof_sig,
           "profile_rt": _prof_rt_sig,   # Bug PROFILE-RT2-SIG
           # #27: rozsah closed-price VDT (zmena → re-sim historických dní v rozsahu)
           "vdt_closed": f"{_vdt_cl_from}|{_vdt_cl_to}",
           "csv_cols_v": "16"}  # bump: #27 vdt_closed range (+ Bug VDT-DOUBLE dam/vdt stĺpce)
    sig_s = json.dumps(sig, sort_keys=True, default=str)
    meta = _load_meta(meta_path)
    if meta is not None and meta.get("settings_sig") != sig_s:
        # Bug SIG-RESET-DIAG (2026-06-13, user: "prepnem profil a späť → ide od znova"):
        # log KTORÝ kľúč sigu sa líši, nech vidíme príčinu zbytočného full resetu
        # (typicky plan_store generated_at po regeneracii, alebo plan/rt z UI stavu).
        try:
            _old = json.loads(meta.get("settings_sig") or "{}")
            _new = sig if isinstance(sig, dict) else {}
            _diff = [k for k in set(_old) | set(_new) if _old.get(k) != _new.get(k)]
            print(f"[livesim SIG-RESET] {_prof_sig}: full reset — líšia sa kľúče sigu: {_diff}")
            for _k in _diff[:4]:
                print(f"    {_k}: stored={str(_old.get(_k))[:120]} != new={str(_new.get(_k))[:120]}")
        except Exception as _e_sd:
            print(f"[livesim SIG-RESET] diag zlyhal: {_e_sd}")
        meta = None                                   # nastavenia sa zmenili → reset
    # BACKFILL: užívateľ posunul start_date dozadu (napr. z 1.3. na 1.1.) — meta má done_through
    # neskorší než nový start, ale CSV nemá tie staršie dni. advance() forward-fillne iba
    # od `done_through+1` ďalej, takže staré dni by chýbali navždy. Reset = bezpečnejšie ako
    # pokus o incremental prepend (musíme prepočítať SOC chain od začiatku).
    #
    # DETEKCIA — tri podmienky (stačí jedna):
    # (1) user_start < meta.start_date — explicitné posunutie štartu dozadu cez UI
    # (2) prvý riadok v CSV je neskoršie než user_start_date — CSV je out-of-sync s deklarovaným
    #     start_date (napr. meta hovorí start=Jan 1, ale CSV začína Feb 28 lebo predchádzajúci
    #     beh fungoval s 90-d capom z _minute_all). Toto pokrýva užívateľov ktorí majú stale stav.
    # (3) CSV row count << očakávaná hodnota podľa start_date..today (#600 fix). Detekuje keď
    #     CSV bol predtým skrátený / poškodený / kompletovaný iba čiastočne (chýbajúce historian
    #     dáta), takže advance() iba pridáva minútu po minúte bez full backfill loopu.
    if meta is not None:
        _need_reset = False
        _reset_reason = ""
        try:
            _meta_start = pd.Timestamp(meta.get("start_date")).normalize()
            _user_start = pd.Timestamp(start_date).normalize()
            if _user_start < _meta_start:
                _need_reset = True
                _reset_reason = f"start posunutý dozadu ({_meta_start.date()} → {_user_start.date()})"
            elif _user_start > _meta_start:
                # Druhá kontrola: CSV začína neskôr ako user deklaroval — IBA ak meta.start_date
                # bol iný (stale stav z minulého behu). Ak meta.start_date == user_start (po reset
                # alebo prvý beh), CSV začiatok > user_start znamená iba že staré dni nemali dáta
                # (sys_MW chýba pre dávnu históriu) → NIE je to dôvod resetovať, lebo nový reset
                # by viedol k tomu istému výsledku (loop).
                try:
                    _head = pd.read_csv(csv_path, nrows=1, usecols=["time"])
                    if not _head.empty:
                        _csv_first = pd.Timestamp(_head["time"].iloc[0]).normalize()
                        if _csv_first > _user_start:
                            _need_reset = True
                            _reset_reason = (f"CSV začína {_csv_first.date()} ale user_start={_user_start.date()} "
                                              f"a meta.start_date={_meta_start.date()} (stale stav)")
                except (FileNotFoundError, ValueError, KeyError, pd.errors.EmptyDataError):
                    pass
            # (3) Sanity check proti #600 (CSV odseknutý/partial). Bug #600-GAP (2026-06-16):
            # PREDTÝM sa resetovalo podľa POČTU riadkov (days×1440×0.5). Lenže pri HISTORIAN
            # GAP-och (dni bez zdrojových minútových dát) má CSV legitímne MENEJ riadkov →
            # falošný reset → re-backfill na KAŽDOM requeste → nekonečná slučka (nikdy nedosiahne
            # počet, dáta chýbajú) + brutálne spomalenie. FIX: reset len keď CSV NEDOSAHUJE
            # done_through (skutočne odseknutý log), NIE keď je len „riedky" kvôli gapom.
            if not _need_reset:
                try:
                    _meta_through = meta.get("done_through")
                    if _meta_through:
                        _through_ts = pd.Timestamp(_meta_through).normalize()
                        # posledný dátum v CSV (lacno — tail posledných ~8 KB)
                        _last_csv_date = None
                        try:
                            with open(csv_path, "rb") as _f:
                                _f.seek(0, 2); _sz = _f.tell()
                                _f.seek(max(0, _sz - 8192))
                                _tail = _f.read().decode("utf-8", "ignore")
                            _ls = [l for l in _tail.strip().splitlines() if l.strip()]
                            if _ls:
                                _cells = _ls[-1].split(",")
                                # CSV_COLS: [time, date, ...] → date je stĺpec index 1
                                if len(_cells) > 1:
                                    _last_csv_date = pd.Timestamp(_cells[1]).normalize()
                        except (FileNotFoundError, OSError, ValueError, IndexError):
                            _last_csv_date = None
                        # reset len ak CSV končí > 2 dni PRED done_through (odseknutý), nie pri gapoch
                        if _last_csv_date is not None and _last_csv_date < (_through_ts - pd.Timedelta(days=2)):
                            _need_reset = True
                            _reset_reason = (
                                f"CSV končí {_last_csv_date.date()} ale done_through="
                                f"{_through_ts.date()} → odseknutý log (#600)"
                            )
                except Exception:
                    pass
        except Exception:
            pass
        if _need_reset:
            print(f"[livesim.advance] {_reset_reason} — reset logu pre backfill")
            meta = None
    if meta is None:
        meta = dict(case=case, start_date=start_date.strftime("%Y-%m-%d"),
                    done_through=None, soc_after_done=cfg.soc_init*bkwh,
                    cum_dt_done=0.0, cum_rt_done=0.0, last_min=None,
                    params=cfg.to_dict(), settings_sig=sig_s)
        pd.DataFrame(columns=CSV_COLS).to_csv(csv_path, index=False)

    # Posuň SK historian lower bound aspoň po start_date užívateľa (default je today-90d).
    _t_ml = time.perf_counter()
    mn = _minute_all(live_minutes, min_from_date=start_date)
    _TMR["minload"] += time.perf_counter() - _t_ml
    sys_orient = rtc.PROD_SYS_ORIENT
    day_sigma = mn.groupby("date")["sig"].std() if "sig" in mn.columns else None
    if "sig" not in mn.columns:
        mn["sig"] = [rtc.mw_signal(r._asdict(), rtc.PROD_W_SYS, sys_orient)
                     for r in mn.itertuples(index=False)]
        day_sigma = mn.groupby("date")["sig"].std()
    sigma_trail = day_sigma.shift(1).rolling(3, min_periods=1).mean()

    last_min = pd.Timestamp(meta["last_min"]) if meta.get("last_min") else None
    soc = float(meta["soc_after_done"])
    cum_dt_done = float(meta["cum_dt_done"]); cum_rt_done = float(meta["cum_rt_done"])
    done_through = pd.Timestamp(meta["done_through"]).normalize() if meta.get("done_through") else None

    first_day = (done_through + pd.Timedelta(days=1)) if done_through is not None else start_date
    day = first_day
    today_dt = today_rt = 0.0
    today_trace = None
    appended = 0
    last_err = None
    skipped_no_data = []                # #600: zoznam dní bez minute dát
    skipped_no_sys_mw = []              # #600: zoznam dní bez sys_MW (historian gap)
    # Progress reporting (BG-PROGRESS, 2026-06-13, user: "chýba info koľko sa má
    # ešte prepočítať, ideálne progress bar"). Spočítaj celkový počet dní backfillu
    # a hlás postup cez progress_cb(done_days, total_days, day_iso).
    try:
        _total_days = max(1, (today.normalize() - pd.Timestamp(first_day).normalize()).days + 1)
    except Exception:
        _total_days = 1
    _done_days = 0
    _t_loop0 = time.perf_counter()
    # LIVESIM-PROFILE (krok diagnostiky 2026-06-14): cProfile slučky, gated LIVESIM_PROFILE=1,
    # dump len pri pomalých behoch (>5 s = VW) — nájde hotspot v per-deň engine bez hádania.
    _lprof = None
    if os.environ.get("LIVESIM_PROFILE") == "1":
        import cProfile as _cP
        _lprof = _cP.Profile(); _lprof.enable()
    while day <= today:
        if progress_cb is not None:
            try:
                progress_cb(_done_days, _total_days, day.date().isoformat())
            except Exception:
                pass
        _done_days += 1
        d = day.date()
        mn_day_full = mn[mn.date == d].sort_values("time")   # CELÝ deň (DT známe D-1) – pre plán
        mn_day = mn_day_full
        if d == today.date():
            mn_day = mn_day_full[mn_day_full["time"] <= now]  # fyzika len po „teraz“
        if mn_day.empty:
            skipped_no_data.append(str(d))
            day += pd.Timedelta(days=1); continue
        # Pre HISTORICKÉ dni ZCO ideálne dostupný (RT settlement). Ak chýba (SK trh
        # publikuje ZCO až D+1 ~11:30, alebo OKTE backend zlyhal), nech sa deň
        # PREDSA spracuje s DT-only zúčtovaním: CSV bude obsahovať sys_MW, plánový
        # DT zisk a RT akciu z rt_controller (ten už akceptuje ZCO=NaN). Reálny RT
        # settlement sa dopočíta keď ZCO príde (zatiaľ rt_rev_min ≈ 0).
        # Skip IBA ak nemáme NIČ (ani sys_MW) — vtedy fyzika nebeží.
        _has_sys_mw = mn_day.get("sys_MW")
        if d < today.date() and (_has_sys_mw is None or _has_sys_mw.notna().sum() == 0):
            skipped_no_sys_mw.append(str(d))
            day += pd.Timedelta(days=1); continue
        try:
            try:
                sch, dtprof, step, d1_cycles, price, pvper, rt_mask_plan = _day_plan(
                    cfg, d, mn_day_full, soc_init_pct=soc/bkwh*100, plan_params=plan_params)
            except ps.PlanMissingError as _pe:
                last_err = f"deň {d}: {_pe}"
                day += pd.Timedelta(days=1); continue
            # ── REAL-PRICE SETTLEMENT pre dokončené dni (zhoda so /simulacia) ──
            # _day_plan vráti dtprof na PREDIKOVANÝCH cenách. Pre historické dni s reálnymi
            # clearingovými cenami (z price_train_2026.csv) prepočítame dtprof za reálne ceny,
            # rovnako ako combined_backtest.settle_d1 → výsledky /livesim a /simulacia sú konzistentné.
            # Dnes (provizórny) ostáva na predikcii (reálna ZCO ešte nie je vyrovnaná).
            if d < today.date():
                try:
                    # Market-aware DT real settlement: CZ z price_train_2026.csv,
                    # SK z OKTE 15-min historian. Pre 15-min mode prednostne quarterly
                    # (SK má autentické 15-min ceny, nie 4× upsample z hodín).
                    if step == 15:
                        _real_arr = _real_dt_quarterly(d.isoformat())
                        if _real_arr is None:
                            _real_h = _real_dt_hourly(d.isoformat())
                            _real_arr = np.repeat(_real_h, 4) if _real_h is not None else None
                    else:
                        _real_arr = _real_dt_hourly(d.isoformat())
                    if _real_arr is not None and len(_real_arr) == len(sch):
                        # fallback na predikciu kde reálne chýba (NaN)
                        _real_arr = np.asarray(_real_arr, float)
                        _price_real = np.where(np.isfinite(_real_arr), _real_arr, price)
                        _ex = np.asarray(sch.get("_export_kwh", [0.0]*len(sch)), float)
                        _im = np.asarray(sch.get("_import_kwh", [0.0]*len(sch)), float)
                        _ch = np.asarray(sch.get("_charge_kw", [0.0]*len(sch)), float)
                        _di = np.asarray(sch.get("_discharge_kw", [0.0]*len(sch)), float)
                        _gf = float(plan_params.get("grid_fee", cfg.grid_fee)) if plan_params else float(cfg.grid_fee)
                        _cc = float(plan_params.get("cycle_cost", cfg.cycle_cost)) if plan_params else float(cfg.cycle_cost)
                        # DIST-FEE-CONSUMPTION-ONLY (2026-06-15, user): distribučný poplatok
                        # patrí len na REÁLNU spotrebu zo siete, nie na nabíjanie batérie
                        # (z FTV ani grid-arbitráž). Spotrebný import = max(import − export −
                        # nabíjanie − curtail, 0) (z bilancie uzla load−pv−di = im−ex−ch−cu).
                        # Default vypnuté → ostatné (golden) profily nezmenené.
                        # KOMERČNÁ SKUPINA: DT efekt = súčet nezávislých per-element členov
                        # (BAT/FTV/LOAD). Vypnutie zložky len odoberie jej člen; all-on = pôvodný
                        # vzorec (golden). Viď _decompose_dtprof.
                        _jlf_rp = ((plan_params or {}).get("joint_lp", {}) or {})
                        _load_in_scope_rp = (not _jlf_rp.get("enabled")) or bool(_jlf_rp.get("trade_load", True))
                        _cons_only = bool((plan_params or {}).get("dist_fee_consumption_only", False))
                        _flags_rp = ({"trade_batt": bool(_jlf_rp.get("trade_batt", True)),
                                      "trade_ftv": bool(_jlf_rp.get("trade_ftv", True)),
                                      "trade_load": bool(_jlf_rp.get("trade_load", True))}
                                     if _jlf_rp.get("enabled") else {"trade_batt": True, "trade_ftv": True, "trade_load": True})
                        _cu_rp = np.asarray(sch.get("_curtail_kwh", sch.get("plan_curtail_kwh", [0.0]*len(sch))), float)
                        _pv_rp = np.asarray(sch.get("pv_kwh", [0.0]*len(sch)), float)
                        dtprof = _decompose_dtprof(_price_real, _pv_rp, _ex, _im, _ch, _di, _cu_rp,
                                                   _gf, _cc, _flags_rp, cons_only=_cons_only,
                                                   dt=float(step) / 60.0)  # BUG 15-MIN-DTPROF: ch/di kW→kWh/slot
                        # Bug VV (2026-06-08): pripočítaj TOU distribučný náklad k importu
                        # ak profile má joint_lp.optimize_distribution:true. Joint LP optimizer
                        # to už zaratáva v plánovacej fáze (joint_lp.py line 269-271), ale
                        # settlement za reálne ceny dovtedy TOU ignoroval → cum_dt v livesim
                        # CSV nezahŕňal TOU saving → "Prínos vs baseline" sa stratil pre VŠETKY
                        # profily s optimize_distribution:true (Simulacia_Coop, Trakany, ...).
                        try:
                            import settlement as _stl_tou
                            from core.profile_resolver import get_active as _ga_tou
                            _prof_tou = _ga_tou(profile)
                            if _stl_tou.profile_uses_tou(_prof_tou) and _load_in_scope_rp:
                                # TOU na spotrebný import — len keď je LOAD v zmluve. Pri LOAD-off
                                # (obchod = len batéria) by TOU×celý_import znova pridal náklad spotreby.
                                _tou_arr = _stl_tou.get_tou_for_day(
                                    _prof_tou, d.isoformat(),
                                    T=len(sch), dt_h=step/60.0)
                                if _tou_arr is not None and len(_tou_arr) == len(sch):
                                    dtprof -= np.asarray(_tou_arr, float) * _im / 1000.0
                        except Exception as _e_tou:
                            print(f"[livesim TOU settlement] {d}: {_e_tou}")
                        price = _price_real                          # tiež pre ďalšie použitia (tabuľka, trace)
                except Exception as _re:
                    print(f"[livesim] real-price settle pre {d}: {_re}")
            # Agresívne RT: keď je flag aktívny, dev_budget = "nekonečno" (limit len SOC + výkon).
            # Inak: max_cycles - d1_cycles (default 3 - d1).
            _aggressive = bool((plan_params or {}).get("aggressive_rt", False))
            if cfg.use_rt:
                dev_budget = (bkwh * 1000.0) if _aggressive else max(0.0, (cfg.max_cycles - d1_cycles)) * bkwh
            else:
                dev_budget = 0.0
            bd, bc = rtc.auto_bands(sigma_trail.get(d, float("nan")))
            if not np.isfinite(bd):
                bd, bc = rtc.PROD_BAND_DIS, rtc.PROD_BAND_CHG
            _kd = float(rt_params.get("kdis", cfg.rt_kdis)) if rt_params else cfg.rt_kdis
            _kc = float(rt_params.get("kchg", cfg.rt_kchg)) if rt_params else cfg.rt_kchg
            _dtk = float(rt_params.get("dtk", cfg.dt_bias_k)) if rt_params else cfg.dt_bias_k
            bd *= _kd; bc *= _kc                            # škála pásiem ako v RT poradcovi
            # JEDNA fyzická batéria: plán + RT odchýlka, spoločné SOC (aj keď use_rt=False → len plán)
            # rt_mask preferuje zapečenú verziu zo strict-mode plánu; fallback na plan_overrides
            _rt_mask = rt_mask_plan
            if _rt_mask is None and po is not None:
                _rt_mask = po.effective_rt_for_step(d.isoformat(), step)
            # ── FTV minútová realita: VŽDY generujeme (chart, curtail, baseline) ──
            # Predtým bolo gated cez `cfg.use_rt and _ftv_balance_on` — to spôsobovalo že v
            # dt_15min móde (use_rt vždy False) chýbala oranžová "FTV minútová realita" v grafe
            # a kúrtail/batéria sa nedali viazať na realitu. Teraz generujeme priebeh vždy keď
            # plán má reálnu FTV produkciu; rt_controller potom dostane flag ftv_balance_on=False
            # a NEROBÍ RT zásah, ale priebeh sa uloží do trace pre chart aj curtail prepočet.
            _ftv_balance_on = bool((plan_params or {}).get("ftv_balance", True))
            _pv_min_kw = None
            _scenario_shift_min = 0
            _hourly_pv = None   # MUSÍ byť reset každú iteráciu (inak pretrváva starý FTV z predošlého dňa do trace)
            try:
                import ftv_minute as _fm
                # 1) PLÁN má prednosť: ak plán explicitne nemá FTV (sum < 1 kWh), scenár sa ignoruje.
                #    Vychádzame z toho, že plán je zdroj pravdy — užívateľ ho generoval a uložil ako "tento deň".
                _pvarr = np.asarray(pvper, float)
                _plan_pv_sum = float(_pvarr.sum()) if _pvarr.size else 0.0
                # 2) ak má plán reálnu FTV produkciu A existuje scenár, scenár prebíja (= "what-if realita ≠ predikcia")
                if _plan_pv_sum > 1.0:
                    try:
                        import ftv_scenarios as _fs
                        if _fs.has_scenario(d.isoformat()):
                            _sc = _fs.load_scenario(d.isoformat())
                            if _sc is not None:
                                _hourly_pv = np.asarray(_sc["hourly_kw"], float)
                                # scenár môže obsahovať časový posun reality (napr. mraky prišli skôr)
                                _scenario_shift_min = int(_sc.get("time_shift_min", 0))
                    except ImportError:
                        pass
                # 3) fallback: hodinové z pvper (z plánu — buď user-disabled = 0, alebo predikcia)
                if _hourly_pv is None:
                    if _pvarr.size == 24:
                        _hourly_pv = _pvarr
                    elif _pvarr.size == 96:
                        _hourly_pv = _pvarr.reshape(24, 4).mean(axis=1)
                    elif _pvarr.size > 0:
                        _idx = np.linspace(0, _pvarr.size - 1, 24).round().astype(int)
                        _hourly_pv = _pvarr[_idx]
                if _hourly_pv is not None and _hourly_pv.size == 24:
                    _pv_min_kw = _fm.hourly_to_minute(_hourly_pv)
                    # Aplikuj časový posun (cyklicky preto že priebeh je 1440 min day)
                    if _scenario_shift_min and _pv_min_kw is not None:
                        _pv_min_kw = np.roll(_pv_min_kw, int(_scenario_shift_min))
            except Exception:
                _pv_min_kw = None
            _ftv_lookah = float((plan_params or {}).get("ftv_lookahead_h", 4.0))
            _ftv_persth = bool((plan_params or {}).get("ftv_persistence_throttle", True))
            _rt_no_worsen = bool((plan_params or {}).get("rt_no_worsen_dev", True))
            _ftv_strict = bool((plan_params or {}).get("ftv_strict_plan", True))
            _ftv_deadband = float((plan_params or {}).get("ftv_strict_deadband_kw", 5.0))
            # asymetrické grid limity z plan_params (alebo z cfg, alebo z grid_kw default)
            _gki = (plan_params or {}).get("grid_kw_import", None)
            _gke = (plan_params or {}).get("grid_kw_export", None)
            _gki = float(_gki) if _gki is not None else None
            _gke = float(_gke) if _gke is not None else None
            # pv_plan_kw = pôvodný PVF plán per perióda (= čo bolo nominované D-1, nemenné scenárom)
            _pv_plan_arr = np.asarray(pvper, float)
            # ── LOAD: minútová realita + per-perióda plán z naimportovaného load profilu ──
            # threshold = FTV_min − load_min + battery; pre_dev zahŕňa aj load deviation.
            _load_min_kw = None
            _load_plan_per = None
            try:
                import load_profile as _lp
                import load_minute as _lm
                if _lp.has_data():
                    _load_15min_kw = _lp.load_for_date(d.isoformat())            # 96 × kW
                    # per-perióda plán (24 alebo 96 podľa step)
                    if step == 60:
                        _load_plan_per = _load_15min_kw.reshape(24, 4).mean(axis=1)   # 24 × priemer kW
                    else:
                        _load_plan_per = _load_15min_kw                              # 96 × kW
                    # minútová realita load (1440) — pridáme šum cez load_minute
                    _load_min_kw = _lm.fifteen_to_minute(_load_15min_kw)
            except Exception:
                _load_min_kw = None
                _load_plan_per = None
            # Bug BB (2026-06-07): pred _run_physical_day pripočítaj VDT realized k sch.batt_kw
            # (= target pre rt_controller). RT engine bude vidieť D-1 + VDT ako target plánu
            # a robiť RT decisions na základe odchýlky voči TÝMTO commit-om (nie len D-1).
            # Pre 60-min plán: sumuj 4 VDT 15-min sloty na hodinu. Pre 96-slot plán: 1:1.
            # No-op pre profil bez VDT trade-ov (vdt_state vráti nuly).
            # Bug #625-A (2026-06-09): clip na ±batt_kw_max — plán target NESMIE prekročiť
            # fyzický limit batérie. Ak by D-1+VDT presiahlo max, rt_controller dostane
            # nedosiahnuteľný target a vznikne odchýlka voči nominácii obchodu = pokuta.
            # Bug FUT-DAM-DOUBLE (2026-06-11): odlož ČISTÚ D-1 batt trajektóriu PRED
            # Bug BB (ten robí sch.batt_kw += VDT hodinový priemer). Stĺpce
            # plan_batt_dam_kw (živé minúty aj fut projekcia) MUSIA stavať z čistého
            # D-1 — inak "dam" obsahuje VDT a render/plan_batt_kw pripočíta VDT
            # druhýkrát (symptóm: tabuľka dam 5706 pri pláne 4000 = 4000 + hodinový
            # priemer obchodov 1706).
            _sch_batt_dam_pure = sch["batt_kw"].astype(float).copy()
            try:
                import vdt_state as _vs_sch
                from core.profile_resolver import get_active as _ga_sch
                _prof_sch = _ga_sch(profile)
                _bkw_max_clip = float(getattr(cfg, "batt_kw", 0.0) or 0.0)
                if _prof_sch:
                    _vdt_kw_96 = _vdt_batt_kw_for(_prof_sch, d.isoformat())   # #27: closed pre históriu v rozsahu
                    if isinstance(_vdt_kw_96, list) and len(_vdt_kw_96) >= 96 and any(_vdt_kw_96):
                        _clipped_slots = 0
                        if step == 15 and len(sch) >= 96:
                            # 1:1 mapping — slot i v sch zodpovedá slot i vo VDT
                            for _i in range(min(96, len(sch))):
                                _new = float(sch.at[_i, "batt_kw"]) + float(_vdt_kw_96[_i] or 0.0)
                                # Bug #625-A: hard clip na fyzický limit batérie
                                if _bkw_max_clip > 0 and abs(_new) > _bkw_max_clip:
                                    _clipped_slots += 1
                                    _new = max(-_bkw_max_clip, min(_bkw_max_clip, _new))
                                sch.at[_i, "batt_kw"] = _new
                        elif step == 60 and len(sch) >= 24:
                            # 60-min: každá hodina = priemer 4 VDT 15-min slotov
                            for _h in range(min(24, len(sch))):
                                _h_avg = sum(_vdt_kw_96[_h*4:_h*4+4]) / 4.0
                                _new = float(sch.at[_h, "batt_kw"]) + _h_avg
                                # Bug #625-A: hard clip na fyzický limit batérie
                                if _bkw_max_clip > 0 and abs(_new) > _bkw_max_clip:
                                    _clipped_slots += 1
                                    _new = max(-_bkw_max_clip, min(_bkw_max_clip, _new))
                                sch.at[_h, "batt_kw"] = _new
                        _vdt_nonzero = sum(1 for v in _vdt_kw_96 if abs(v)>0.01)
                        if _clipped_slots > 0:
                            print(f"[livesim Bug BB+#625-A] sch.batt_kw += VDT pre {_prof_sch} ({d.isoformat()}): "
                                  f"{_vdt_nonzero} nenulových slotov, CLIPPED {_clipped_slots} slotov "
                                  f"(prekročili ±{_bkw_max_clip:.0f} kW)")
                        else:
                            print(f"[livesim Bug BB] sch.batt_kw += VDT pre {_prof_sch} ({d.isoformat()}): "
                                  f"{_vdt_nonzero} nenulových slotov")
            except Exception as _e_sch_vdt:
                print(f"[livesim Bug BB] aplikácia VDT do sch zlyhala: {_e_sch_vdt}")

            # Bug RT-INLINE-AUDIT (2026-06-11): zostav audit_today_state pre rt_controller
            # per-minute audit_capacity. Audit chráni SOC pre budúce zazmluvnené sloty
            # (D-1 plán + VDT realized) tak, aby RT nevyčerpal kapacitu predčasne.
            _audit_today_state = None
            _audit_reserve = 0.0
            # Bug AUDIT-HORIZON-PARAM (2026-06-11): nový plan param `rt_audit_horizon_h`
            # — koľko hodín dopredu audit_capacity sleduje plán pre obmedzenie RT zložky.
            # Default 1.0 h (= 4 × 15-min slotov). Nezdieľa sa s ftv_lookahead_h (= ten má
            # iný účel: lookahead clip rt_action proti future plánu v rt_controlleri).
            # User môže nastaviť v šablóne plánu, napr. 2.0 alebo 0.5.
            try:
                _audit_h_val = (plan_params or {}).get("rt_audit_horizon_h", 1.0)
                _audit_horizon_slots = max(1, int(round(float(_audit_h_val) * 4)))
            except (TypeError, ValueError):
                _audit_horizon_slots = 4
            try:
                import vdt_state as _vs_a
                from core.profile_resolver import get_active as _ga_a
                _prof_a = _ga_a(profile)
                if _prof_a:
                    _audit_today_state = _vs_a.compute_current_state(_prof_a, today=day.date()) or {}
                    _audit_reserve = float((plan_params or {}).get("soc_reserve_pct", 0.0) or 0.0)
            except Exception as _e_audit_state:
                print(f"[RT-INLINE-AUDIT] compute_current_state zlyhal: {_e_audit_state}")

            # RT poradca 2.0 (2026-06-11): voľba enginu z profilu rt.engine ("v1"|"v2").
            # v2 = ekonomické rozhodnutie z kalibrovaného E[ZCO] spreadu (SK trh).
            _rt_engine_sel, _rt2_params = "v1", None
            try:
                from core.profile_resolver import get_active as _ga_rte
                import profiles as _pr_rte
                _rt_cfg_sel = ((_pr_rte.load_profile(_ga_rte(profile)) or {}).get("rt") or {})
                _eng_sel = str(_rt_cfg_sel.get("engine", "v1"))
                if _eng_sel in ("v2", "v3"):
                    import rt_engine_v2 as _rte2
                    _rt_engine_sel = _eng_sel
                    _rt2_params = _rte2.params_from_profile_rt(
                        _rt_cfg_sel, cycle_cost_plan=getattr(cfg, "cycle_cost", None),
                        eff_rt=(float(getattr(cfg, "eff_c", 0.95) or 0.95)
                                * float(getattr(cfg, "eff_d", 0.95) or 0.95)))
                    print(f"[RT-ENGINE] {d}: {_eng_sel} (params={_rt2_params})")
            except Exception as _e_rte:
                print(f"[RT-ENGINE] výber zlyhal ({_e_rte}) → v1")

            rev, cyc, tr = _run_physical_day(cfg, mn_day, sch, day, step, bd, bc,
                                             soc0=soc, dev_budget_kwh=dev_budget, dt_bias_k=_dtk,
                                             rt_mask=_rt_mask, pv_min_kw=_pv_min_kw,
                                             ftv_balance_on=(_ftv_balance_on and _pv_min_kw is not None),
                                             ftv_lookahead_h=_ftv_lookah,
                                             ftv_persistence_throttle=_ftv_persth,
                                             rt_no_worsen_dev=_rt_no_worsen,
                                             ftv_strict_plan=_ftv_strict,
                                             pv_plan_kw=_pv_plan_arr,
                                             ftv_strict_deadband_kw=_ftv_deadband,
                                             grid_kw_import=_gki, grid_kw_export=_gke,
                                             load_min_kw=_load_min_kw,
                                             load_plan_kw=_load_plan_per,
                                             enforce_realistic=True,
                                             audit_today_state=_audit_today_state,
                                             audit_soc_reserve_pct=_audit_reserve,
                                             audit_rt_persistence_slots=_audit_horizon_slots,
                                             # Bug AUDIT-SOC-TRAJECTORY: rezervácia SOC
                                             # ide na CELÝ deň (forward trajektória, vidí
                                             # celý committed vybíjací/nabíjací blok). Nie
                                             # je príliš konzervatívna — dobíjanie sa ráta.
                                             audit_future_horizon_slots=96,
                                             rt_engine=_rt_engine_sel, rt2_params=_rt2_params)
            day_dt_total = float(np.nansum(dtprof))
            # SK fallback odstránený — rt_controller.run_day_physical teraz akceptuje
            # ZCO=NaN (= žiadne zúčtovanie odchýlky), takže bežný flow funguje aj pre SK
            # bez D-1 ZCO. Žiadne špeciálne SK vetvy v livesim.
            if not tr.empty:
                tr = tr.copy()
                tr["pidx"] = [min(len(dtprof)-1, max(0, _period_index(t, day, step))) for t in tr["ts15"]]
                cnt = tr.groupby("pidx")["time"].transform("count")
                tr["dt_rev_min"] = pd.Series([float(dtprof[i]) for i in tr["pidx"]], index=tr.index) / cnt
                # Bug V (2026-06-07): plan_batt_kw = D-1 schedule + VDT realized (paper trades dňa)
                # — žiadny LP recalc, čisto agregát persistovaných zdrojov.
                # Plus dva diagnostické stĺpce pre transparenciu v UI grafe.
                dam_per_min = [float(_sch_batt_dam_pure.values[i]) for i in tr["pidx"]]   # Bug FUT-DAM-DOUBLE: čistý D-1
                vdt_per_min = [0.0] * len(dam_per_min)
                try:
                    import vdt_state as _vs
                    from core.profile_resolver import get_active as _ga
                    _profile = _ga(profile)
                    if _profile:
                        # VDT je VŽDY 15-min granularita → pidx15 nezávislé od `step`.
                        pidx15 = [min(95, max(0, _period_index(t, day, 15))) for t in tr["ts15"]]
                        vdt_arr_kw = _vdt_batt_kw_for(_profile, d.isoformat())   # #27: closed pre históriu v rozsahu
                        if isinstance(vdt_arr_kw, list) and len(vdt_arr_kw) >= 96:
                            vdt_per_min = [float(vdt_arr_kw[j] or 0.0) for j in pidx15]
                except Exception:
                    pass
                tr["plan_batt_dam_kw"] = dam_per_min
                tr["plan_batt_vdt_kw"] = vdt_per_min
                # Bug #625-A (2026-06-09): plan_batt_kw = D-1 + VDT clipnuté na ±batt_kw_max.
                # Plán target NESMIE prekročiť fyzický limit batérie — inak vznikne nereálna
                # nominácia voči ktorej sa meria odchýlka (= pokuta).
                _bkw_max_pb = float(getattr(cfg, "batt_kw", 0.0) or 0.0)
                _plan_batt_raw = [d + v for d, v in zip(dam_per_min, vdt_per_min)]
                if _bkw_max_pb > 0:
                    tr["plan_batt_kw"] = [max(-_bkw_max_pb, min(_bkw_max_pb, x)) for x in _plan_batt_raw]
                else:
                    tr["plan_batt_kw"] = _plan_batt_raw
                # Bug V (2026-06-07): VDT trade ide cez sieť (predaj batt→grid = export +;
                # nákup grid→batt = import −). Plus VDT kWh má rovnakú konvenciu ako plan_grid_kwh
                # (+ export, − import). Pripočítame VDT kWh per 15-min slot ku každej minúte slotu.
                vdt_kwh_per_min = [0.0] * len(dam_per_min)
                try:
                    import vdt_state as _vs2
                    _ga2 = None
                    try:
                        from core.profile_resolver import get_active as _ga2
                    except Exception:
                        pass
                    if _ga2 is not None:
                        _prof2 = _ga2(profile)
                        if _prof2:
                            _vdt_kwh_arr = _vdt_kwh_view_for(_prof2, d.isoformat())   # #27: closed pre históriu v rozsahu
                            pidx15_grid = [min(95, max(0, _period_index(t, day, 15)))
                                            for t in tr["ts15"]]
                            vdt_kwh_per_min = [float(_vdt_kwh_arr[j] or 0.0) for j in pidx15_grid]
                except Exception:
                    pass
                _dam_grid = [float(sch["grid_kwh"].values[i]) for i in tr["pidx"]]
                tr["plan_grid_dam_kwh"] = _dam_grid
                tr["plan_grid_vdt_kwh"] = vdt_kwh_per_min
                # Bug #625-B (2026-06-09): plan_grid_kwh clip na fyzický limit siete.
                # _dam_grid je kWh za periódu (60 alebo 15 min). max export per perióda =
                # grid_kw_export × step_h. Nad to = nominácia ktorú batt fyzicky nedodá.
                _step_h_pg = max(int(step), 1) / 60.0
                _gke_kwh_max = (float(_gke) * _step_h_pg) if (_gke is not None and _gke > 0) else None
                _gki_kwh_max = (float(_gki) * _step_h_pg) if (_gki is not None and _gki > 0) else None
                _plan_grid_raw = [g + v for g, v in zip(_dam_grid, vdt_kwh_per_min)]
                if _gke_kwh_max is not None or _gki_kwh_max is not None:
                    _clip_hi = _gke_kwh_max if _gke_kwh_max is not None else float("inf")
                    _clip_lo = -_gki_kwh_max if _gki_kwh_max is not None else float("-inf")
                    tr["plan_grid_kwh"] = [max(_clip_lo, min(_clip_hi, x)) for x in _plan_grid_raw]
                else:
                    tr["plan_grid_kwh"] = _plan_grid_raw
                tr["plan_curtail_kwh"] = [float(sch["curtail_kwh"].values[i]) for i in tr["pidx"]]
                tr["dt_eur"] = [float(price[i]) for i in tr["pidx"]]
                tr["ftv_kw"] = [float(pvper[i]) for i in tr["pidx"]]
                # minútová REALITA FTV (zo scenára alebo auto-generovaná z PVGIS) — pre graf a threshold.
                # PVGIS model je legitímna simulácia FTV aj na SK, preto ju necháme aj pri SK fallback.
                if _pv_min_kw is not None and len(_pv_min_kw) == 1440:
                    _mi = [int((pd.Timestamp(t) - pd.Timestamp(day)).total_seconds() // 60) for t in tr["time"]]
                    _mi = [min(1439, max(0, x)) for x in _mi]
                    tr["ftv_min_real_kw"] = [float(_pv_min_kw[i]) for i in _mi]
                else:
                    tr["ftv_min_real_kw"] = tr["ftv_kw"]
                # 24 hodinových hodnôt FTV ktoré sa použili (scenár alebo plán) — pre stepped chart
                if _hourly_pv is not None and _hourly_pv.size == 24:
                    tr["ftv_hour_plan_kw"] = [float(_hourly_pv[min(23, pd.Timestamp(t).hour)]) for t in tr["time"]]
                else:
                    tr["ftv_hour_plan_kw"] = tr["ftv_kw"]
                # ── LOAD: per-perióda plánovaná spotreba + minútová realita ──
                if _load_plan_per is not None and len(_load_plan_per) > 0:
                    tr["load_plan_kw"] = [float(_load_plan_per[i]) for i in tr["pidx"]]
                else:
                    tr["load_plan_kw"] = 0.0
                if _load_min_kw is not None and len(_load_min_kw) == 1440:
                    _mi = [int((pd.Timestamp(t) - pd.Timestamp(day)).total_seconds() // 60) for t in tr["time"]]
                    _mi = [min(1439, max(0, x)) for x in _mi]
                    tr["load_min_real_kw"] = [float(_load_min_kw[i]) for i in _mi]
                else:
                    tr["load_min_real_kw"] = tr["load_plan_kw"]
                # ── PER-MINUTE REALITA: clipnúť plán batérie + orezanie + zúčtovať odchýlku ──
                # PLÁN bol vygenerovaný na PREDIKOVANEJ FTV (LP). Reálne minútové FTV môže byť výrazne
                # nižšie (oblačnosť) → plán napr. povie "nabíjaj 500 kW", ale reálne FTV+grid_import
                # nedáva toľko energie. Tu vypočítame čo by sa skutočne stalo fyzicky:
                #
                #   power_balance: ftv_real + grid_in + batt_dis = load_real + grid_out + batt_chg + curtail
                #   Vyriešime pre dane plan_batt, FTV_real, load_real, grid limits:
                #     pre nabíjanie:    batt_chg_real = min(plan_chg, ftv_real + grid_kw_import − load_real)
                #     pre vybíjanie:    batt_dis_real = min(plan_dis, grid_kw_export + load_real − ftv_real)
                #     curtail:          curtail = max(0, ftv_real − load_real + batt_p_real − grid_kw_export)
                #
                # User report (2026-05-30): "Vyzera to akoby riadenie islo dtale podla planovanej FTV"
                # — plán chce nabíjať 500 kW, FTV reality 30 kW, grid_in 200 → max 230 kW reálne.
                #
                # EKONOMIKA Z REALITY (user 2026-05-30):
                #   "aj do vypoctu ekonomiky sa musi ukldatat ralita. chcem to v buducnosti pustit aj
                #    na realne riesenie ni len na simulaciu" — odchýlka real_grid vs nominovaný plan_grid
                #   sa zúčtuje cez ZCO. Plný real-world settlement:
                #     dev_kw   = real_grid_kw − plan_grid_kw (nominácia)
                #     rt_rev   = dev_kwh × ZCO / 1000 EUR/min
                try:
                    _grid_kw_export = (float(_gke) if _gke is not None
                                        else float(getattr(cfg, "grid_kw_export", getattr(cfg, "grid_kw", 1e9))))
                    _grid_kw_import = (float(_gki) if _gki is not None
                                        else float(getattr(cfg, "grid_kw_import", getattr(cfg, "grid_kw", 1e9))))
                    _ftv_r = np.asarray(tr["ftv_min_real_kw"].values, float)
                    _load_r = np.asarray(tr["load_min_real_kw"].values, float)
                    # Bug #607: plan_batt_kw je iba D-1 plán. RT engine zasahuje cez
                    # rt_dir × rt_power_pct, čo D-1 plán nezachytáva. Pre realistic
                    # dev kalkuláciu musíme použiť RT-finalný batt výkon (rovnaký
                    # vzorec ako app.py:4277 batt_kw_actual). Bez toho je batt_real=0
                    # vždy keď D-1 bol idle a RT engine vykonal arbitráž (sys_MW signál),
                    # napr. nabíjanie -6000 kW v slot kde plan_batt_kw=0.
                    _batt_p_d1 = np.asarray(tr["plan_batt_kw"].values, float)     # kW (+= vybíja, −= nabíja)
                    try:
                        _rt_dir_arr = pd.to_numeric(tr.get("rt_dir", 0), errors="coerce").fillna(0).values
                        _rt_pct_arr = pd.to_numeric(tr.get("rt_power_pct", 0), errors="coerce").fillna(0).values
                        _bkw_max_rt = float(getattr(cfg, "batt_kw", 0.0) or 0.0)

                        # Faza C Bug RT-PRE-AUDIT (2026-06-10): per-slot RT clip
                        # User: 'baterka sa nabije s RT a ked sa ma nabijat z uz
                        # zobchodovanej casti tak nema kam'. Riesenie: pred kazdym
                        # 15-min slotom zavolat audit_rt_slot ktory simuluje SOC
                        # trajektoriu cez VSETKY buduce zazmluvnene sloty (D-1+VDT)
                        # a ak by RT minul SOC pre neskor, znizit rt_power_pct.
                        try:
                            from core.rt_audit import audit_rt_slot as _audit_rt
                            from core.profile_resolver import get_active as _ga_rt
                            _prof_rt = _ga_rt(profile)
                            if _prof_rt and _bkw_max_rt > 0:
                                # ts15 = 15-min slot timestamp v tr
                                _ts15_arr = tr["ts15"].values if "ts15" in tr.columns else None
                                if _ts15_arr is not None and len(_ts15_arr) > 0:
                                    # Bug RT-AUDIT-DATE (2026-06-10): day.isoformat()
                                    # pre pd.Timestamp vráti "2026-06-10T00:00:00".
                                    # date.fromisoformat() v stášom Pythone to nezvládne.
                                    # Orezať na YYYY-MM-DD.
                                    _day_iso = day.isoformat()[:10]
                                    # Bug AUDIT-RUNNING-SOC (2026-06-10): audit per minutu so
                                    # *running* SOC. User postreh: "treba to otocit ked pride
                                    # poziadavk z RT najprv ju prevri audit az potom ide na
                                    # vystup. audit ju moxe orezat potom to nebude robit pilu".
                                    # Predchádzajúca verzia brala SOC z trace (vypočítane
                                    # rt_controllerom s NEorezaným RT) → audit dostával
                                    # nereálne (nafúknuté) SOC. Fix: integrovať SOC sami za
                                    # behu, brať predchádzajúce orezané RT do úvahy. Per minútu:
                                    # 1) navrhnuté RT prejde auditom so SOC ktoré vzniklo
                                    #    z UŽ orezaných predchádzajúcich minút
                                    # 2) audit vráti scale → RT sa orezáva
                                    # 3) update running_soc už s orezaným RT
                                    # Tým žiadna píla — každá ďalšia minúta vidí reálny SOC.
                                    _soc_arr = (pd.to_numeric(tr.get("soc_pct", 50.0),
                                                                errors="coerce").fillna(50.0).values
                                                if "soc_pct" in tr.columns else None)
                                    _bkwh_run = float(getattr(cfg, "batt_kwh", 0.0) or 0.0)
                                    _eff_c_run = float(getattr(cfg, "eff_c", 0.95) or 0.95)
                                    _eff_d_run = float(getattr(cfg, "eff_d", 0.95) or 0.95)
                                    _dt_min_h = 1.0 / 60.0   # 1 minúta v hodinách
                                    _ts_min_arr = pd.to_datetime(_ts15_arr)
                                    # Štartovací SOC z trace (prvá minúta)
                                    _running_soc = (float(_soc_arr[0]) if _soc_arr is not None
                                                      else 50.0)
                                    _per_min_scale = np.ones(len(_ts15_arr), dtype=float)
                                    _dn_cnt = 0
                                    for _i in range(len(_ts15_arr)):
                                        _rt_kw_proposed = float(_rt_dir_arr[_i] * _rt_pct_arr[_i] / 100.0 * _bkw_max_rt)
                                        _plan_kw_min = float(_batt_p_d1[_i])
                                        _ts_min = _ts_min_arr[_i]
                                        _sidx = _ts_min.hour * 4 + (_ts_min.minute // 15)
                                        _scale_i = 1.0
                                        if abs(_rt_kw_proposed) >= 1.0:
                                            _ax = _audit_rt(_prof_rt, _day_iso, int(_sidx),
                                                              _plan_kw_min, _rt_kw_proposed,
                                                              step_min=15,
                                                              current_soc_pct=_running_soc,
                                                              today_state=_audit_today_state,
                                                              rt_persistence_slots=4,
                                                              soc_reserve_pct=_audit_reserve)
                                            _scale_i = float(_ax.get("scale_factor", 1.0))
                                            if _scale_i < 0.99:
                                                _per_min_scale[_i] = _scale_i
                                                _dn_cnt += 1
                                                if _dn_cnt <= 5:
                                                    print(f"[RT-PRE-AUDIT min] {_prof_rt} "
                                                          f"{_ts_min.strftime('%H:%M')} "
                                                          f"SOC={_running_soc:.1f}% plan={_plan_kw_min:.0f} "
                                                          f"rt={_rt_kw_proposed:.0f} kW scale={_scale_i:.2f}")
                                        # Update running SOC s ORZANÝM batt (plán + orezané RT)
                                        # eff: pri nabíjaní (záporné batt) eff_c, pri vybíjaní (kladné) /eff_d
                                        if _bkwh_run > 0:
                                            _rt_kw_actual = _rt_kw_proposed * _scale_i
                                            _batt_kw_total = _plan_kw_min + _rt_kw_actual
                                            if _batt_kw_total < 0:   # nabíjanie
                                                _delta_pct = (-_batt_kw_total) * _dt_min_h * _eff_c_run / _bkwh_run * 100.0
                                            else:                    # vybíjanie
                                                _delta_pct = -(_batt_kw_total / max(_eff_d_run, 0.01)) * _dt_min_h / _bkwh_run * 100.0
                                            _running_soc = max(0.0, min(100.0, _running_soc + _delta_pct))
                                    if _dn_cnt > 0:
                                        _rt_pct_arr = _rt_pct_arr * _per_min_scale
                                        print(f"[RT-PRE-AUDIT] {_prof_rt} {_day_iso}: "
                                              f"clipnutých {_dn_cnt}/{len(_ts15_arr)} minút (running SOC)")
                        except Exception as _e_rta:
                            print(f"[RT-PRE-AUDIT] zlyhal: {_e_rta} → pokračujem fail-open")

                        _batt_p_raw = _batt_p_d1 + _rt_dir_arr * _rt_pct_arr / 100.0 * _bkw_max_rt
                        # Bug #610 (bezpečnostný most do #611-#613): RT engine môže
                        # generovať rt_power_pct > 100% (proporcionálna reakcia na
                        # sys_MW), čo by viedlo k _batt_p > batt_kw_max (napr. -15000 kW
                        # na 6000 kW batt). Fyzika batérie to ohraničí v batt_real cez
                        # _avail_for_chg/_dis, ale GRAF a DEV kalk by ukázali nereálne
                        # hodnoty. Clip ich na fyzické limity batérie ±batt_kw_max.
                        # Po nasadení capacity ledger (#611-#613) bude RT engine
                        # vystavený LEN voľnej kapacite (D-1 plán + VDT už zarezervujú
                        # svoju časť) a tento clip sa odstráni.
                        if _bkw_max_rt > 0:
                            _batt_p = np.clip(_batt_p_raw, -_bkw_max_rt, +_bkw_max_rt)
                        else:
                            _batt_p = _batt_p_raw
                    except Exception:
                        _batt_p = _batt_p_d1
                    # Rozdelíme plán na nabíjanie (≤0) a vybíjanie (≥0)
                    _plan_chg = np.maximum(-_batt_p, 0.0)                          # kW nabíjanie
                    _plan_dis = np.maximum(_batt_p, 0.0)                           # kW vybíjanie
                    # SHARED-METER (2026-06-16, user): jedno odberné miesto, jeden prah.
                    # net cez prah = FTV − odber + batéria ∈ [−import_limit, +export_limit].
                    #   nabíjanie ≤ import + FTV − odber  (odber AJ nabíjanie čerpajú z import;
                    #     predtým max(FTV−odber,0)+import → neodpočítaval odber > FTV = chyba)
                    #   vybíjanie ≤ export + odber − FTV  (FTV prebytok čerpá z export)
                    # Oboje clip ≥ 0. FTV sa pridá do vzorca vždy (aj keď je 0).
                    _avail_for_chg = np.maximum(_grid_kw_import + _ftv_r - _load_r, 0.0)
                    _avail_for_dis = np.maximum(_grid_kw_export + _load_r - _ftv_r, 0.0)
                    # Bug #648 (2026-06-09): ODSTRÁNENÝ capacity ledger constraint
                    # zo livesim simulácie. Bug #613 mal logiku zle: capacity_ledger.available()
                    # vráti `batt_kw_max - reserved`. Ale rezervovaná kapacita ZAHŔŇA D-1 plán
                    # ktorý LP nominoval. Tým sa D-1 sám sebe oreže:
                    #   D-1 nominoval 4000 kW vybíjanie → ledger reserved=4000
                    #   ledger.available(discharge) = batt_kw_max - 4000 = 2000
                    #   _avail_for_dis = min(grid_kw, 2000) = 2000
                    #   _dis_real = min(plan_dis=4000, _avail_for_dis=2000) = 2000
                    #   = batt vybíja len 50% plánu → 50% strata + odchýlka pokuta.
                    # Capacity ledger ostáva v audit_vdt_order (Bug #612) — tam zarezervuje
                    # voľnú kapacitu pred VDT trade. Pre livesim simuláciu nie je potrebný,
                    # fyzikálny clip ±batt_kw_max (riadok 1055) + grid/FTV constraint stačí.
                    # User report (2026-06-09): plán 4000 kW vybíjanie, realita 2000 kW
                    # (50% využitia) v 21:00-22:00 — presne tento bug.
                    # Clipnúť plán na fyzicky možné
                    _chg_real = np.minimum(_plan_chg, _avail_for_chg)
                    _dis_real = np.minimum(_plan_dis, _avail_for_dis)
                    _batt_real = _dis_real - _chg_real                              # ±kW
                    # Bug RT-INLINE-AUDIT (2026-06-11): batt_kw_realistic preferuje act_batt_kw
                    # z rt_controllera (= už integruje realistic clip dovnútra) ak je k dispozícii.
                    # Bývalý Bug #607 _batt_p_raw post-loop je teraz nadbytočný — necháme len
                    # ako sekundárny fallback pre profily bez enforce_realistic.
                    if "act_batt_kw" in tr.columns:
                        tr["batt_kw_realistic"] = tr["act_batt_kw"].round(1)
                    else:
                        tr["batt_kw_realistic"] = _batt_real.round(1)
                    # Bug SOC-FROM-REALISTIC (2026-06-10): DISABLED 2026-06-11.
                    # rt_controller teraz integruje soc s ORZANÝM batt (= enforce_realistic),
                    # takže tr["soc_kwh"]/soc_pct sú už realistické. Post-hoc recompute by
                    # spôsobil double-integration (= zlé hodnoty).
                    # User postreh 2026-06-11: "vnutorne sa soc vycerpala pricom realne nie"
                    # — fix = audit/clip dovnútra rt_controllera, nie post-hoc.
                    # Real grid flow (po batérii, pred curtailom)
                    _real_grid_kw_pre = _ftv_r - _load_r + _batt_real               # kW (+= export)
                    # CURTAIL: VÝLUČNE z reality. Plán curtail sa ignoruje.
                    _excess = np.maximum(_real_grid_kw_pre - _grid_kw_export, 0.0)
                    tr["ftv_min_curtailed_kw"] = _excess.round(1)
                    # Real grid AFTER curtail (toto je čo skutočne ide do siete)
                    _real_grid_kw = _real_grid_kw_pre - _excess
                    # PLAN nominovaný grid flow (kW) — to bola D-1 nominácia, fixné
                    _plan_grid_kw = (np.asarray(tr["plan_grid_kwh"].values, float) /
                                      (max(step, 1) / 60.0))
                    # Odchýlka reality vs nominácia → settlement cez ZCO
                    _dev_kw = _real_grid_kw - _plan_grid_kw
                    _dev_kwh_min = _dev_kw / 60.0                                   # kWh za minútu
                    # ZCO môže byť NaN pre dnešok (SK D+1) — vtedy 0 (zúčtovanie po príde ZCO)
                    _zco = pd.to_numeric(tr.get("zco_eur",
                                                  pd.Series([np.nan]*len(tr))),
                                          errors="coerce").fillna(0.0).values
                    # rt_rev_realistic = dev × ZCO (€/min) — TOTAL settlement (= čo platí na trhu)
                    #   + dev = "long" (over-export, dodali viac než nominované) → ZCO > 0 → get paid
                    #   − dev = "short" (under-export / over-import) → ZCO > 0 → pay
                    _rt_rev_real = (_dev_kwh_min * _zco) / 1000.0
                    tr["rt_rev_realistic_min"] = _rt_rev_real.round(4)
                    # Bug #649 (2026-06-09): rozklad dev na komponenty pre atribúciu efektu.
                    # User: 'efekt by sa mal pocitat z komodity a odchylky len z toho co je v
                    # grafe riadenie' — odchýlka RT z grafu Riadenie = LEN batt drift, nie
                    # FTV+Load+Batt drift. Pridať dev_batt/dev_ftv/dev_load + ich € €.
                    try:
                        # Plánovaný batt v kW per minútu (D-1 plán + VDT realized po Bug X)
                        _plan_batt_kw = np.asarray(tr["plan_batt_kw"].values, float)
                        # FTV plánované = pv_plan_kw (hodinový plán PVF) — fallback na ftv_kw
                        _plan_ftv_kw = (np.asarray(tr["ftv_hour_plan_kw"].values, float)
                                          if "ftv_hour_plan_kw" in tr.columns
                                          else np.asarray(tr["ftv_kw"].values, float))
                        # Load plánované = load_plan_kw (priemer per perióda)
                        _plan_load_kw = (np.asarray(tr["load_plan_kw"].values, float)
                                          if "load_plan_kw" in tr.columns
                                          else np.zeros(len(tr)))
                        # Drift per komponenta (kW)
                        _dev_batt_kw = _batt_real - _plan_batt_kw
                        _dev_ftv_kw = _ftv_r - _plan_ftv_kw
                        _dev_load_kw = _load_r - _plan_load_kw     # +load = viac spotreby = under-export
                        # Curtail drift: orezanie znižuje export (= short dev)
                        _dev_curtail_kw = -_excess                  # curtail je strata exportu
                        # rt_rev per komponenta — atribučná RT odchýlka
                        tr["rt_rev_batt_min"] = ((_dev_batt_kw / 60.0) * _zco / 1000.0).round(4)
                        tr["rt_rev_ftv_min"] = ((_dev_ftv_kw / 60.0) * _zco / 1000.0).round(4)
                        # POZOR: load_r vyšší ako plán = viac importu = SHORT dev (mínus)
                        # preto −dev_load_kw vo formule
                        tr["rt_rev_load_min"] = ((-_dev_load_kw / 60.0) * _zco / 1000.0).round(4)
                        tr["rt_rev_curtail_min"] = ((_dev_curtail_kw / 60.0) * _zco / 1000.0).round(4)
                        # Diagnostické stĺpce kW pre graf (Excel + chC)
                        tr["dev_batt_kw"] = _dev_batt_kw.round(1)
                        tr["dev_ftv_kw"] = _dev_ftv_kw.round(1)
                        tr["dev_load_kw"] = _dev_load_kw.round(1)
                        tr["dev_curtail_kw"] = _dev_curtail_kw.round(1)
                    except Exception as _e_dev:
                        print(f"[livesim #649] dev decomposition zlyhalo: {_e_dev}")

                    # Bug #608: VDT arbitráž = (VDT_cena - DT_clearing) × VDT_kwh / 1000
                    # VDT realized objemy sa pripočítavajú do plan_grid_kwh (Bug X), takže
                    # dt_rev_min ich valuuje za DT clearing. Skutočný cash z VDT obchodu
                    # ide za VDT cenu (price_predicted_eur v paper trades). Rozdiel je
                    # arbitrážny profit, ktorý sa doteraz strácal v karte "Zisk SPOLU".
                    try:
                        from core.profile_resolver import get_active as _ga_vdt
                        from core.paths import vdt_trades_csv_path as _vdtpath
                        _prof_vdt = _ga_vdt(profile)
                        _vdt_csv = _vdtpath(profile=_prof_vdt) if _prof_vdt else None
                        _vt = _load_vdt_trades_cached(_vdt_csv) if _vdt_csv else None
                        if _vt is not None:
                            # Filter na profil + dnešný deň + iba BUY/SELL/CHARGE/DISCHARGE (nie IDLE)
                            if "profile" in _vt.columns and _prof_vdt:
                                _vt = _vt[_vt["profile"].astype(str) == str(_prof_vdt)]
                            if "ts" in _vt.columns:
                                _vt = _vt[_vt["ts"].astype(str).str[:10] == d.isoformat()]
                            # SELL/DISCHARGE = +kwh (predaj), BUY/CHARGE = −kwh (nákup)
                            _act_sign = {"SELL": +1.0, "DISCHARGE": +1.0,
                                          "BUY": -1.0, "CHARGE": -1.0}
                            _vt = _vt[_vt["action"].astype(str).str.upper().isin(_act_sign.keys())]
                            # Bug VDT-ARB-ORDER (2026-06-11): tr["dt_real_eur"] sa plní až
                            # NIŽŠIE (po tomto bloku) → tr.get(...) tu vracal skalár 0 →
                            # .fillna() na skalári = exception → vdt_arb_min ticho = 0.0
                            # VŽDY. VDT arbitráž sa tým NIKDY nezapočítala do efektu.
                            # DT clearing berieme priamo z _real_dt_hourly (rovnaký zdroj
                            # ako settlement + neskorší zápis dt_real_eur).
                            if "dt_real_eur" in tr.columns:
                                _dt_per_min = pd.to_numeric(tr["dt_real_eur"], errors="coerce").fillna(0).values
                            else:
                                _rdt24_arb = _real_dt_hourly(d.isoformat())
                                if _rdt24_arb is not None:
                                    _dt_per_min = np.array([float(_rdt24_arb[min(23, pd.Timestamp(t).hour)])
                                                             for t in tr["time"]], dtype=float)
                                else:
                                    _dt_per_min = np.zeros(len(tr), dtype=float)
                            _vdt_arb_min = np.zeros(len(tr), dtype=float)
                            # Mapuj každý trade do 15-min slotu (HH:MM-HH:MM alebo HH:MM)
                            for _, _row in _vt.iterrows():
                                _slot = str(_row.get("slot", "") or "")
                                _start = _slot.split("-")[0].strip()
                                if len(_start) < 5:
                                    continue
                                try:
                                    _hh = int(_start[:2]); _mm = int(_start[3:5])
                                except Exception:
                                    continue
                                _sign = _act_sign.get(str(_row.get("action","")).upper(), 0.0)
                                _kwh = float(_row.get("kwh", 0) or 0) * _sign
                                _vprice = float(_row.get("price_predicted_eur", 0) or 0)
                                if _kwh == 0 or _vprice == 0:
                                    continue
                                # Distribuuj rovnomerne do 15 minút slotu
                                _slot_start_iso = f"{d.isoformat()} {_hh:02d}:{_mm:02d}:00"
                                _slot_start_ts = pd.to_datetime(_slot_start_iso)
                                _slot_end_ts = _slot_start_ts + pd.Timedelta(minutes=15)
                                _t_arr = pd.to_datetime(tr["time"], errors="coerce")
                                _mask = (_t_arr >= _slot_start_ts) & (_t_arr < _slot_end_ts)
                                _n = int(_mask.sum())
                                if _n <= 0:
                                    continue
                                # DT clearing priemer cez slot (€/MWh)
                                _dt_slot_avg = float(np.mean(_dt_per_min[_mask.values])) if _n > 0 else 0.0
                                # Arbitráž za celý slot: kwh × (vdt_price - dt_clearing) / 1000
                                _arb_slot_eur = (_kwh * (_vprice - _dt_slot_avg)) / 1000.0
                                _vdt_arb_min[_mask.values] += _arb_slot_eur / _n
                            tr["vdt_arb_min"] = np.round(_vdt_arb_min, 4)
                        else:
                            tr["vdt_arb_min"] = 0.0
                    except Exception as _e_vdt:
                        tr["vdt_arb_min"] = 0.0
                    # #27: pre HISTÓRIU vo zvolenom rozsahu prepíš vdt_arb_min arbitrážou
                    # z REÁLNYCH UZAVRETÝCH cien (closed sim), nie z paper trades CSV.
                    if _vdt_use_closed_for(d.isoformat()):
                        try:
                            import vdt_state as _vscl27
                            from core.profile_resolver import get_active as _ga27
                            _prof27 = _ga27(profile) or profile
                            _cl27 = _vscl27._compute_closedprice_day(_prof27, d.isoformat())
                            _cl_kwh27 = _cl27.get("kwh_batt_view") or [0.0] * 96
                            _cl_px27 = _cl27.get("prices_per_slot") or [float("nan")] * 96
                            if "dt_real_eur" in tr.columns:
                                _dtpm27 = pd.to_numeric(tr["dt_real_eur"], errors="coerce").fillna(0).values
                            else:
                                _rdt27 = _real_dt_hourly(d.isoformat())
                                _dtpm27 = (np.array([float(_rdt27[min(23, pd.Timestamp(t).hour)])
                                                     for t in tr["time"]], dtype=float)
                                           if _rdt27 is not None else np.zeros(len(tr), dtype=float))
                            _arbmin27 = np.zeros(len(tr), dtype=float)
                            _tarr27 = pd.to_datetime(tr["time"], errors="coerce")
                            for _ci in range(96):
                                _ck = float(_cl_kwh27[_ci] or 0.0)
                                _cp = _cl_px27[_ci]
                                if abs(_ck) < 0.01 or not np.isfinite(_cp):
                                    continue
                                _chh, _cmm = divmod(_ci * 15, 60)
                                _css = pd.to_datetime(f"{d.isoformat()} {_chh:02d}:{_cmm:02d}:00")
                                _cmask = (_tarr27 >= _css) & (_tarr27 < _css + pd.Timedelta(minutes=15))
                                _cn = int(_cmask.sum())
                                if _cn <= 0:
                                    continue
                                _cdt_avg = float(np.mean(_dtpm27[_cmask.values]))
                                _arbmin27[_cmask.values] += (_ck * (_cp - _cdt_avg)) / 1000.0 / _cn
                            tr["vdt_arb_min"] = np.round(_arbmin27, 4)
                        except Exception:
                            pass
                except Exception as _e:
                    tr["batt_kw_realistic"] = tr["plan_batt_kw"]
                    tr["ftv_min_curtailed_kw"] = 0.0
                    tr["rt_rev_realistic_min"] = 0.0
                    tr["vdt_arb_min"] = 0.0   # Bug #608 default
                # reálny clearovaný day-ahead — primárne z price_train_2026.csv (rovnaký zdroj ako settlement),
                # fallback na mn_day_full ak by tam bol (zvyčajne nie je).
                # Pre-init _rm aby bol vždy definovaný (používa sa neskôr vo fut block).
                _rm = (mn_day_full.drop_duplicates("time").set_index("time")["dt_real_eur"]
                       if "dt_real_eur" in mn_day_full.columns else None)
                _real_dt_24h = _real_dt_hourly(d.isoformat())             # 24 hodinových DT cien alebo None
                if _real_dt_24h is not None:
                    tr["dt_real_eur"] = [float(_real_dt_24h[min(23, pd.Timestamp(t).hour)]) for t in tr["time"]]
                else:
                    tr["dt_real_eur"] = tr["time"].map(_rm) if _rm is not None else np.nan
                _vm = (mn_day_full.drop_duplicates("time").set_index("time")["vdt_eur"]
                       if "vdt_eur" in mn_day_full.columns else None)
                tr["vdt_eur"] = tr["time"].map(_vm) if _vm is not None else np.nan
                tr["date"] = d.isoformat()
                # cum_rt = REAL financial impact (Bug #603 — 2026-06-08):
                # 1) rt_rev_realistic_min — PRIORITY: skutočná odchýlka (dev) × ZCO settlement
                # 2) rt_rev_min — fallback iba ak realistic stĺpec chýba (staré CSV verzie)
                #
                # Pôvodná logika "ak engine != 0 použij engine" bola nesprávna:
                # ak RT engine chce reagovať (signal silný) ale fyzicky to neprejde
                # (SOC limit, grid limit, gate), engine počíta TEORETICKÚ stratu ktorá
                # v realite neexistuje. Karty Zisk SPOLU potom ukazujú fiktívne -387 €
                # pre deň kedy fyzicky NIČ nepreplynulo a dev = 0 → realistic = 0.
                #
                # Užívateľov spôsob: 2026-05-30 "do vypoctu ekonomiky sa musi ukladat realita".
                # 2026-06-08 znova potvrdené keď RT nastavenia nemali žiadny vplyv na "stratu"
                # — lebo strata bola fiktívna z engine kalkulácie, fyzicky realistic=0.
                _rt_engine = tr["rt_rev_min"].fillna(0)
                _rt_real = tr.get("rt_rev_realistic_min", None)
                if _rt_real is None:
                    # Stará CSV verzia bez realistic stĺpca → fallback na engine
                    _rt_used = _rt_engine
                else:
                    _rt_used = pd.Series(_rt_real, index=tr.index).fillna(0)
                # Konsolidácia (2026-06-08): cum_rt cez core.effect.get_rt_eur_series
                # — rovnaký vzorec ako Excel + UI karty + grafy. Jediný zdroj pravdy.
                try:
                    from core.effect import get_rt_eur_series
                    _rt_used_consolidated = get_rt_eur_series(tr, warn_legacy=False)
                except Exception:
                    _rt_used_consolidated = _rt_used   # legacy fallback
                tr["cum_rt"] = cum_rt_done + _rt_used_consolidated.cumsum()
                tr["cum_dt"] = cum_dt_done + tr["dt_rev_min"].fillna(0).cumsum()
                # Bug #608: kumulatív VDT arbitráž (delta vs DT clearing)
                if "vdt_arb_min" in tr.columns:
                    tr["cum_vdt_arb"] = tr["vdt_arb_min"].fillna(0).cumsum()
                else:
                    tr["cum_vdt_arb"] = 0.0
                tr["cum_total"] = tr["cum_dt"] + tr["cum_rt"] + tr["cum_vdt_arb"]
                if d < today.date():
                    # DOKONČENÝ deň (skutočná ZCO) → zapíš do logu a fixuj kumulatívy
                    new = tr if last_min is None else tr[tr["time"] > last_min]
                    if not new.empty:
                        out = new.reindex(columns=CSV_COLS).copy()
                        out["time"] = pd.to_datetime(out["time"]).dt.strftime("%Y-%m-%d %H:%M:%S")
                        _t_io = time.perf_counter()
                        out.to_csv(csv_path, mode="a", header=False, index=False)
                        _TMR["io"] += time.perf_counter() - _t_io
                        appended += len(new)
                        last_min = pd.Timestamp(new["time"].max())
                        soc = float(new["soc_kwh"].iloc[-1])   # spoločné SOC (plán + RT)
                    cum_dt_done += day_dt_total
                    # Bug #603: cum_rt_done = REALISTIC (dev × ZCO), engine iba ako fallback
                    _day_rt_engine = float(rev)
                    _rt_real_col = tr.get("rt_rev_realistic_min", None)
                    if _rt_real_col is None:
                        cum_rt_done += _day_rt_engine
                    else:
                        cum_rt_done += float(pd.Series(_rt_real_col).fillna(0).sum())
                    done_through = day
                    # Bug LIVESIM-META-INCREMENTAL (2026-06-13, user: "každý profil sa
                    # má uložiť a načítať komplet s dňami; nie prepočítavať od znova pri
                    # návrate"): meta (done_through) sa predtým zapisovala až PO CELEJ
                    # slučke → keď sa dlhý backfill (napr. 44 dní) prerušil (reštart/
                    # deploy/prepnutie), meta nevznikla → done_through stratený → ďalší
                    # pohľad full backfill OD NULY. Teraz persistuj meta PO KAŽDOM
                    # dokončenom dni → backfill je resumovateľný, profil sa "uloží".
                    try:
                        meta.update(
                            done_through=done_through.strftime("%Y-%m-%d"),
                            soc_after_done=soc, cum_dt_done=cum_dt_done,
                            cum_rt_done=cum_rt_done,
                            last_min=(last_min.strftime("%Y-%m-%d %H:%M:%S")
                                      if last_min is not None else None),
                            skipped_no_sys_mw=skipped_no_sys_mw,
                            skipped_no_data=skipped_no_data, settings_sig=sig_s)
                        _save_meta_atomic(meta_path, meta)
                    except Exception as _e_meta_inc:
                        print(f"[livesim meta-inc] {_e_meta_inc}")
                    # DB unify F2 (2026-06-10): paralelný zápis do DB (effect_minute + effect_daily).
                    # Jediný zdroj pravdy pre UI (karty, chC graf, Excel, PDF). CSV zostáva pre
                    # interný incremental state. Fail-soft — DB chyba neblokuje livesim.
                    try:
                        _t_db = time.perf_counter()
                        from core import effect_db as _eff_db
                        from core.profile_resolver import get_active as _ga_db
                        import market as _mk_db
                        _prof_db = _ga_db(profile)
                        _market_db = str(_mk_db.get_active_market()).lower()
                        if _prof_db and not tr.empty:
                            _eff_db.upsert_minute_batch(_prof_db, _market_db, tr)
                            _day_iso = str(d)
                            _totals = _eff_db.compute_day_totals_from_df(tr)
                            _eff_db.upsert_daily(_prof_db, _day_iso, _market_db, _totals)
                        _TMR["effectdb"] += time.perf_counter() - _t_db
                    except Exception as _e_dbup:
                        print(f"[livesim DB F2] upsert pre {d} zlyhal: {_e_dbup}")
                else:
                    # DNEŠOK = provizórny (odhad ZCO) → LEN zobrazenie, NEukladá sa
                    today_dt = float(tr["dt_rev_min"].sum())
                    _today_rt_engine = float(rev)
                    # Bug #603: today_rt = REALISTIC ak existuje
                    _rt_real_col = tr.get("rt_rev_realistic_min", None)
                    if _rt_real_col is None:
                        today_rt = _today_rt_engine
                    else:
                        today_rt = float(pd.Series(_rt_real_col).fillna(0).sum())
                    tr["is_live"] = 1                       # živé minúty (po teraz)
                    # DISPLAY-FROM-DB Fáza A (2026-06-14): zapíš aj DNEŠNÝ provizórny trace +
                    # parciálne daily totals do effect_db, nech GET /livesim vie zobraziť dnešok
                    # čítaním z DB (get_minute_series + get_period_effect zahrnie dnešok) bez
                    # volania advance v requeste. Upsert = prepisuje dnešok pri každom bg ticku.
                    # Fail-soft — DB chyba neblokuje livesim. (Dokončené dni píše vetva d<today vyššie.)
                    try:
                        from core import effect_db as _eff_db_t
                        from core.profile_resolver import get_active as _ga_db_t
                        import market as _mk_db_t
                        _prof_db_t = _ga_db_t(profile)
                        _market_db_t = str(_mk_db_t.get_active_market()).lower()
                        if _prof_db_t and not tr.empty:
                            _eff_db_t.upsert_minute_batch(_prof_db_t, _market_db_t, tr)
                            _totals_t = _eff_db_t.compute_day_totals_from_df(tr)
                            _eff_db_t.upsert_daily(_prof_db_t, str(d), _market_db_t, _totals_t)
                    except Exception as _e_dbup_t:
                        print(f"[livesim DB F2-today] upsert dnešok zlyhal: {_e_dbup_t}")
                    # rozšír na CELÝ deň (0–24h): budúce minúty = LEN plán (DT/obchod/FTV/projekcia SOC), bez RT/live
                    try:
                        end_day = day + pd.Timedelta(days=1)
                        last_t = tr["time"].max() if not tr.empty else (day - pd.Timedelta(minutes=1))
                        fut_idx = pd.date_range(pd.Timestamp(last_t) + pd.Timedelta(minutes=1),
                                                end_day - pd.Timedelta(minutes=1), freq="1min")
                        if len(fut_idx):
                            pj = [min(len(dtprof)-1, max(0, _period_index(t, day, step))) for t in fut_idx]
                            # Bug SOC-V (2026-06-10): SOC projekcia musí použiť SKUTOČNÝ
                            # plánovaný batt_kw (D-1 + VDT realized + clip), nie len sch["batt_kw"]
                            # z D-1. Pre profil s use_vdt=True boli VDT trades v plan_batt_kw
                            # stĺpci CSV ale SOC ich ignoroval → graf SOC bol roztiahnutý.
                            # Tu načítame _fut_plan_batt VOPRED (rovnaký kód ako nižšie ale skôr).
                            _fut_dam_socpre = [float(_sch_batt_dam_pure.values[i]) for i in pj]   # Bug FUT-DAM-DOUBLE
                            _fut_vdt_socpre = [0.0] * len(_fut_dam_socpre)
                            try:
                                import vdt_state as _vs_socpre
                                from core.profile_resolver import get_active as _ga_socpre
                                _prof_socpre = _ga_socpre(profile)
                                if _prof_socpre:
                                    _vdt_kw_arr_pre = _vs_socpre.get_realized_batt_kw(
                                        _prof_socpre, today_iso=d.isoformat(), dt_h=0.25)
                                    if isinstance(_vdt_kw_arr_pre, list) and len(_vdt_kw_arr_pre) >= 96:
                                        _pidx15_pre = [min(95, max(0, _period_index(t, day, 15)))
                                                        for t in fut_idx]
                                        _fut_vdt_socpre = [float(_vdt_kw_arr_pre[j] or 0.0)
                                                            for j in _pidx15_pre]
                            except Exception:
                                pass
                            _bkw_max_socpre = float(getattr(cfg, "batt_kw", 0.0) or 0.0)
                            _fut_plan_batt_socpre = [d + v for d, v in zip(_fut_dam_socpre,
                                                                              _fut_vdt_socpre)]
                            if _bkw_max_socpre > 0:
                                _fut_plan_batt_socpre = [max(-_bkw_max_socpre, min(_bkw_max_socpre, x))
                                                          for x in _fut_plan_batt_socpre]
                            soc_proj = soc; socs = []; socs_kwh = []
                            for k, _eff_batt_kw in enumerate(_fut_plan_batt_socpre):
                                soc_proj = min(bkwh, max(0.0, soc_proj - _eff_batt_kw / 60.0))
                                socs.append(soc_proj/bkwh*100.0); socs_kwh.append(soc_proj)
                            last_cum_dt = float(tr["cum_dt"].iloc[-1]) if not tr.empty else cum_dt_done
                            last_cum_rt = float(tr["cum_rt"].iloc[-1]) if not tr.empty else cum_rt_done
                            # Bug V (2026-06-07): pripočítaj VDT realized aj k projekcii
                            # (môže existovať VDT trade pre future slot ktorý už uzavrel).
                            _fut_dam = [float(_sch_batt_dam_pure.values[i]) for i in pj]   # Bug FUT-DAM-DOUBLE
                            _fut_vdt = [0.0] * len(_fut_dam)
                            try:
                                import vdt_state as _vs_fut
                                from core.profile_resolver import get_active as _ga_fut
                                _prof_fut = _ga_fut(profile)
                                if _prof_fut:
                                    _vdt_kw_arr = _vs_fut.get_realized_batt_kw(_prof_fut,
                                                                                today_iso=d.isoformat(),
                                                                                dt_h=0.25)
                                    if isinstance(_vdt_kw_arr, list) and len(_vdt_kw_arr) >= 96:
                                        _pidx15_fut = [min(95, max(0, _period_index(t, day, 15)))
                                                        for t in fut_idx]
                                        _fut_vdt = [float(_vdt_kw_arr[j] or 0.0) for j in _pidx15_fut]
                            except Exception:
                                pass
                            # Bug X: pripočítaj VDT realized aj k plan_grid_kwh (rovnaký pidx15)
                            _fut_grid_dam = [float(sch["grid_kwh"].values[i]) for i in pj]
                            _fut_grid_vdt = [0.0] * len(_fut_grid_dam)
                            try:
                                _prof_g = _ga_fut(profile) if _ga_fut else None
                                if _prof_g:
                                    _vdt_st_fut = _vs_fut._load_vdt_realized(_prof_g, d.isoformat())
                                    _vdt_kwh_fut = (_vdt_st_fut or {}).get("kwh_batt_view") or [0.0] * 96
                                    _pidx15_grid = [min(95, max(0, _period_index(t, day, 15)))
                                                    for t in fut_idx]
                                    _fut_grid_vdt = [float(_vdt_kwh_fut[j] or 0.0) for j in _pidx15_grid]
                            except Exception:
                                pass
                            # Bug #625-A/B (2026-06-09): clip D-1+VDT na fyzické limity
                            # aj v projekcii budúcich minút — plán nesmie pretiecť ani v UI.
                            _bkw_max_fut = float(getattr(cfg, "batt_kw", 0.0) or 0.0)
                            _step_h_fut = max(int(step), 1) / 60.0
                            _gke_kwh_fut = (float(_gke) * _step_h_fut) if (_gke is not None and _gke > 0) else None
                            _gki_kwh_fut = (float(_gki) * _step_h_fut) if (_gki is not None and _gki > 0) else None
                            _fut_plan_batt = [d2 + v2 for d2, v2 in zip(_fut_dam, _fut_vdt)]
                            if _bkw_max_fut > 0:
                                _fut_plan_batt = [max(-_bkw_max_fut, min(_bkw_max_fut, x)) for x in _fut_plan_batt]
                            _fut_plan_grid = [g + v for g, v in zip(_fut_grid_dam, _fut_grid_vdt)]
                            if _gke_kwh_fut is not None or _gki_kwh_fut is not None:
                                _hi_f = _gke_kwh_fut if _gke_kwh_fut is not None else float("inf")
                                _lo_f = -_gki_kwh_fut if _gki_kwh_fut is not None else float("-inf")
                                _fut_plan_grid = [max(_lo_f, min(_hi_f, x)) for x in _fut_plan_grid]
                            fut = pd.DataFrame({
                                "time": fut_idx, "ts15": fut_idx.floor("15min"),
                                "plan_batt_dam_kw": _fut_dam,
                                "plan_batt_vdt_kw": _fut_vdt,
                                "plan_batt_kw": _fut_plan_batt,
                                "plan_grid_dam_kwh": _fut_grid_dam,
                                "plan_grid_vdt_kwh": _fut_grid_vdt,
                                "plan_grid_kwh": _fut_plan_grid,
                                "plan_curtail_kwh": [float(sch["curtail_kwh"].values[i]) for i in pj],
                                "dt_eur": [float(price[i]) for i in pj],
                                "ftv_kw": [float(pvper[i]) for i in pj],
                                "soc_pct": socs, "soc_kwh": socs_kwh,
                                "rt_dir": 0.0, "rt_power_pct": 0.0, "rt_rev_min": 0.0, "dt_rev_min": 0.0,
                                "rt_reason": "plán", "date": d.isoformat(),
                                "cum_dt": last_cum_dt, "cum_rt": last_cum_rt,
                                "cum_total": last_cum_dt + last_cum_rt,
                            })
                            fut = fut.reindex(columns=tr.columns)   # mw_sig/band_*/zco/... → NaN (žiadny live signál)
                            if _rm is not None:
                                fut["dt_real_eur"] = fut["time"].map(_rm)   # reálny day-ahead aj do budúcich periód (známy D-1)
                            if _vm is not None:
                                fut["vdt_eur"] = fut["time"].map(_vm)
                            # Doplň projekčné polia ktoré app.py čaká pre THR1/DEV1/FTM graf — pre budúce
                            # minúty nemáme reálnu meteo/load realitu, použijeme plán (DEV ≈ 0 v projekcii).
                            if "ftv_min_real_kw" in fut.columns and fut["ftv_min_real_kw"].isna().all():
                                fut["ftv_min_real_kw"] = fut["ftv_kw"]
                            if "ftv_hour_plan_kw" in fut.columns and fut["ftv_hour_plan_kw"].isna().all():
                                fut["ftv_hour_plan_kw"] = fut["ftv_kw"]
                            try:
                                if "load_kwh" in sch.columns:
                                    _ph = max(step, 1) / 60.0           # kWh za period → kW
                                    _load_plan_proj = [float(sch["load_kwh"].values[i]) / _ph for i in pj]
                                else:
                                    _load_plan_proj = [0.0] * len(pj)
                                if "load_plan_kw" in fut.columns and fut["load_plan_kw"].isna().all():
                                    fut["load_plan_kw"] = _load_plan_proj
                                if "load_min_real_kw" in fut.columns and fut["load_min_real_kw"].isna().all():
                                    fut["load_min_real_kw"] = _load_plan_proj
                            except Exception:
                                pass                                       # bez load polí — app.py default-uje na 0
                            # minúty PO poslednom signáli, ale PRED reálnym TERAZ = plán už beží (RT sa dopočíta,
                            # keď ČEPS zverejní signál+ZCO) → označ ich ako živé; až za reálnym TERAZ je projekcia
                            fut["is_live"] = (fut["time"] <= now).astype(int)
                            today_trace = pd.concat([tr, fut], ignore_index=True)
                        else:
                            today_trace = tr.copy()
                    except Exception:
                        import traceback as _tb
                        print(f"[livesim.advance] {d}: fut construction zlyhal — fallback na tr.copy():\n{_tb.format_exc()[:1200]}")
                        today_trace = tr.copy()
                    appended += len(tr[tr["time"] > last_min]) if last_min is not None else len(tr)
        except Exception:
            import traceback as _tb
            last_err = f"deň {d}: " + _tb.format_exc()
            print(f"[livesim.advance] EXC pre {d}:\n{last_err[:1500]}")
        day += pd.Timedelta(days=1)

    _TMR["loop"] = time.perf_counter() - _t_loop0
    if _lprof is not None:
        _lprof.disable()
        if _TMR["loop"] > 5.0:
            import pstats as _pst, io as _iox
            _ss = _iox.StringIO()
            _pst.Stats(_lprof, stream=_ss).sort_stats("cumulative").print_stats(25)
            print(f"[LIVESIM-PROFILE] {case}/{profile} loop={_TMR['loop']*1000:.0f}ms appended={appended} "
                  f"TOP cumulative:\n" + _ss.getvalue()[:4500])
    if appended == 0 and last_err is not None:
        raise RuntimeError(last_err)                         # nič sa nepodarilo → ukáž skutočnú chybu

    # #600: zaznamenaj zoznam skipnutých dní pre user-facing warning
    if skipped_no_sys_mw or skipped_no_data:
        gap_msg = []
        if skipped_no_sys_mw:
            gap_msg.append(f"{len(skipped_no_sys_mw)} dní bez sys_MW (historian gap)")
        if skipped_no_data:
            gap_msg.append(f"{len(skipped_no_data)} dní bez minute dát")
        print(f"[livesim.advance] ⚠ HISTORIAN GAP: {' + '.join(gap_msg)}. "
              f"Spusti `python -m historian_backfill --tag <tag> --from {start_date.date()} "
              f"--to {today.date()}` pre kompletný backfill.")
        if skipped_no_sys_mw[:5]:
            print(f"  Prvé skipnuté dni (sys_MW): {skipped_no_sys_mw[:5]}")

    # Bug SOC-UNIFY-TODAY (2026-06-14): exponuj DNEŠNÝ aktuálny SOC z engine (= posledná
    # živá minúta tr = plán/DT + VDT + RT po clipe) do meta. Dnešok sa do CSV neukladá
    # (provizórny), takže bez tohto /vdt, /rt aj VDT advisor naň "nevideli" a počítali si
    # vlastnú vdt_state integráciu BEZ RT → rôzne SOC na každej stránke. Toto je jediný
    # zdroj aktuálneho SOC pre dnešok (RT+DT+VDT) — číta ho compute_current_state.
    soc_disp = soc
    today_soc_ts = None
    if today_trace is not None and not today_trace.empty:
        _liv = today_trace[today_trace.get("is_live", 1) == 1] if "is_live" in today_trace.columns else today_trace
        if not _liv.empty:
            soc_disp = float(_liv["soc_kwh"].iloc[-1])       # SOC teraz = posledný ŽIVÝ stav (nie projekcia)
            try:
                today_soc_ts = pd.Timestamp(_liv["time"].iloc[-1]).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                today_soc_ts = None

    meta.update(done_through=done_through.strftime("%Y-%m-%d") if done_through is not None else None,
                soc_after_done=soc, cum_dt_done=cum_dt_done, cum_rt_done=cum_rt_done,
                last_min=last_min.strftime("%Y-%m-%d %H:%M:%S") if last_min is not None else None,
                skipped_no_sys_mw=skipped_no_sys_mw,        # #600: pre UI banner
                skipped_no_data=skipped_no_data,
                today_soc_kwh=round(soc_disp, 3),           # SOC-UNIFY-TODAY: engine dnešný SOC (RT+DT+VDT)
                today_soc_pct=round(soc_disp / bkwh * 100, 2) if bkwh else None,
                today_soc_ts=today_soc_ts,
                d1_step_min=int(getattr(cfg, "d1_step_min", 60)),  # SOC-UNIFY-PLAN: kind plánu enginu (60→plan,15→dentrh) — advisor musí čítať ten istý
                settings_sig=sig_s)
    _save_meta_atomic(meta_path, meta)

    if os.environ.get("LIVESIM_TIMING") == "1":
        _adv_total = time.perf_counter() - _T_ADV0
        _other = max(0.0, _adv_total - _TMR["loop"] - _TMR["minload"])
        print(f"[ADVANCE-TIMING] case={case} profile={profile} appended={appended} "
              f"| total={_adv_total*1000:.0f}ms minload={_TMR['minload']*1000:.0f}ms "
              f"loop={_TMR['loop']*1000:.0f}ms (z toho io={_TMR['io']*1000:.0f}ms "
              f"effectdb={_TMR['effectdb']*1000:.0f}ms) setup+ostatne={_other*1000:.0f}ms")

    cum_dt = cum_dt_done + today_dt
    cum_rt = cum_rt_done + today_rt
    return dict(case=case, start_date=start_date.strftime("%Y-%m-%d"),
                last_min=meta["last_min"], appended=appended,
                cum_dt=round(cum_dt, 2), cum_rt=round(cum_rt, 2),
                cum_total=round(cum_dt + cum_rt, 2),
                soc_pct=round(soc_disp/bkwh*100, 1), csv=csv_path, meta=meta_path,
                use_rt=cfg.use_rt, batt_kw=cfg.batt_kw, batt_kwh=bkwh,
                today_trace=today_trace, prov_date=(prov_date.isoformat() if prov_date else None),
                plan_used=_po, rt_used=(rt_params or {}),
                step_min=int(getattr(cfg, "d1_step_min", 60)))


_LIVESIM_CSV_CACHE = {}                                   # (csv_path) → (mtime, DataFrame)
_VDT_TRADES_CACHE = {}                                    # path → (mtime, DataFrame)


def _load_vdt_trades_cached(path):
    """Načíta VDT paper trades CSV s cache per mtime. Perf PLAN-VDT-READ-ONCE
    (2026-06-13, user: "nepočítajú sa veci zbytočne?"): predtým sa CSV čítal a
    parsoval V KAŽDOM dni backfillu (44×), hoci sa počas behu nemení. Teraz raz."""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    c = _VDT_TRADES_CACHE.get(path)
    if c is not None and c[0] == mt:
        return c[1]
    try:
        df = pd.read_csv(path, low_memory=False)
    except Exception:
        return None
    _VDT_TRADES_CACHE[path] = (mt, df)
    return df


def _read_csv(case: str, port: str = "8000", profile=None):
    csv_path, _ = paths(case, port, profile)
    try:
        mtime = os.path.getmtime(csv_path)
    except OSError:
        return None
    cached = _LIVESIM_CSV_CACHE.get(csv_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]                                  # in-memory, žiadne IO
    try:
        df = pd.read_csv(csv_path)
    except Exception:
        return None
    if df is None or df.empty or "time" not in df.columns:
        _LIVESIM_CSV_CACHE[csv_path] = (mtime, df)
        return df
    df["time"] = pd.to_datetime(df["time"], format="mixed", errors="coerce")
    df = df.dropna(subset=["time"]).reset_index(drop=True)
    _LIVESIM_CSV_CACHE[csv_path] = (mtime, df)
    return df


def available_days(case: str, port: str = "8000", profile=None):
    """Zoznam dní prítomných v logu (na prehliadanie histórie).

    Bug AVAILABLE-DAYS-NO-TIME (2026-06-13, user log: KeyError 'time'): _read_csv
    môže vrátiť df BEZ stĺpca 'time' (prázdny/rozpísaný/poškodený CSV počas backfillu)
    — vtedy crashol celý /livesim GET handler a nezobrazilo sa NIČ. Guard: ak 'time'
    chýba alebo je df prázdny, vráť [] (= zobrazí sa progress page, nie 500)."""
    df = _read_csv(case, port, profile)
    if df is None or df.empty or "time" not in getattr(df, "columns", []):
        return []
    try:
        return sorted(set(df["time"].dt.date))
    except Exception:
        return []


def load_series(case: str, port: str = "8000", day=None, max_points: int = 2000):
    """Načíta rady z CSV pre grafy. day=None → celé (decimované); inak len daný deň (jemné).

    Dedup: ak CSV obsahuje viacero riadkov pre tú istú minútu (= rôzne advance() behy
    pre rovnaké nastavenia, znak že settings_sig reset zlyhal), ponecháme **POSLEDNÝ**
    výskyt (= najnovší výpočet). Tým sa grafy nezdvojnásobia.
    """
    df = _read_csv(case, port)
    # Bug AVAILABLE-DAYS-NO-TIME: df môže prísť bez stĺpca 'time' (rozpísaný CSV
    # počas backfillu) → nepadni, vráť None (volajúci to zvládne / ukáže progress).
    if df is None or df.empty or "time" not in getattr(df, "columns", []):
        return None
    if day is not None:
        dd = pd.Timestamp(day).date()
        df = df[df["time"].dt.date == dd]
    # Dedup pred decimáciou — duplikáty by inak deformovali grafy aj decimation step.
    if "time" in df.columns:
        n_before = len(df)
        df = df.drop_duplicates(subset=["time"], keep="last").reset_index(drop=True)
        if len(df) < n_before:
            print(f"[load_series] {case} port={port} day={day}: dedup odstránil "
                  f"{n_before - len(df)} duplikátov ({len(df)} unique riadkov ostáva)")
    if len(df) > max_points:
        df = df.iloc[:: max(1, len(df)//max_points)]
    return df
