"""Živý simulátor (paper-trading) pre zvolený PRÍPAD.
- D-1 plán sa generuje IDENTICKY ako v simulácii (optimize_day, riadené prípadom: d1_step_min, allow_*…).
- RT vrstva = minútový regulátor (rt_controller.run_day s minútovým trace) so zvyšným rozpočtom batérie.
- Stav (SOC, kumulatívy) je perzistentný: minútové riadky sa APPENDUJÚ do CSV, meta do JSON.
- Po reštarte sa pokračuje od poslednej spracovanej minúty; ak súbor nie je, štartuje od `start_date`.
SOC drží simulátor (to, čo by si inak zadával v /rt). Všetky vstupy/parametre/výstupy sú v CSV pre spätnú analýzu.
"""
from __future__ import annotations
import os, json, math, datetime as dt
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


def _real_dt_hourly(date_iso: str):
    """Vráti 24-prvkový rad reálnych hodinových DT clearing cien pre dátum, alebo None.

    Market-aware cez settlement.get_dt_real_hourly():
      - CZ → price_train_2026.csv (isot_eur)
      - SK → seps_sk historian (load_okte_dt_for_day, agreguje 15-min na hodiny)
    """
    try:
        import settlement as _settlement
        return _settlement.get_dt_real_hourly(date_iso)
    except Exception:
        return None


def _real_dt_quarterly(date_iso: str):
    """Vráti 96-prvkový rad reálnych 15-min DT clearing cien pre dátum, alebo None.

    Pre CZ rozšíri 24h ceny na 4× quarter; pre SK použije autentické 15-min ceny.
    """
    try:
        import settlement as _settlement
        return _settlement.get_dt_real_quarterly(date_iso)
    except Exception:
        return None

CSV_COLS = ["time", "date", "ts15",
            "sys_MW", "zco_eur", "dt_eur", "dt_real_eur", "vdt_eur", "ftv_kw",
            "ftv_min_real_kw", "ftv_hour_plan_kw", "ftv_min_curtailed_kw",
            "load_plan_kw", "load_min_real_kw",
            "mw_sig", "avg_react", "band_dis", "band_chg",
            "rt_dir", "rt_power_pct", "rt_reason",
            "plan_batt_kw", "plan_grid_kwh", "plan_curtail_kwh",
            "batt_kw_realistic", "rt_rev_realistic_min",
            "soc_kwh", "soc_pct", "budget_left_kwh",
            "dt_rev_min", "rt_rev_min", "cum_dt", "cum_rt", "cum_total"]


def paths(case: str, port: str = "8000"):
    """Market-aware livesim CSV/meta paths — out/<market>/livesim_<case>[_<port>].csv.

    SK trh má vlastnú simuláciu (SEPS sys_MW + OKTE ceny + ZCO),
    CZ má svoje (ČEPS sys_MW + OTE ceny). Súbory NESMÚ byť zdieľané.
    Env var LIVESIM_DIR má vyššiu prioritu (test isolation).
    """
    tag = case + ("" if str(port) == "8000" else f"_{port}")
    env = os.environ.get("LIVESIM_DIR")
    if env:
        return os.path.join(env, f"livesim_{tag}.csv"), os.path.join(env, f"livesim_{tag}.meta.json")
    try:
        import market as _mk
        d = _mk.data_dir()                                                # out/cz alebo out/sk
    except Exception:
        d = os.path.join("out", "cz")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"livesim_{tag}.csv"), os.path.join(d, f"livesim_{tag}.meta.json")


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


def _minute_all(live_minutes=None, min_from_date=None) -> pd.DataFrame:
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
        if cached is not None and (now_ts - cached.get("ts", 0)) < 300:
            mn = cached["df"]
        else:
            try:
                import seps_sk as _seps_h
                mn = _seps_h.build_sk_minute_history(from_date=from_d, to_date=to_d)
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
    dtprof = price*ex/1000 - (price + grid_fee)*im/1000 - cycle_cost*(ch+di)/2/1000
    d1_cycles = float((ch.sum() + di.sum())/2/bkwh) if bkwh > 0 else 0.0
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
                       load_min_kw=None, load_plan_kw=None):
    """Jedna fyzická batéria – deleguje na rt_controller.run_day_physical (jeden zdroj pravdy)."""
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
                                load_plan_kw=load_plan_kw)


def _load_meta(meta_path):
    try:
        with open(meta_path) as f:
            return json.load(f)
    except Exception:
        return None


def carried_soc_for_date(case: str, port: str = "8000", date=None) -> dict:
    """Vráti dict so SOC ktoré livesim použije ako soc_init pre daný dátum (carried z predošlého dňa).
    Pri date=None vráti aktuálny soc_after_done. Pri date=zajtra vráti predpoklad = soc_after_done.
    Pri date=minulosť (už spočítané v CSV) vráti SOC z konca predošlého dňa.

    Vracia: {'soc_kwh': float, 'soc_pct': float, 'as_of_date': str, 'note': str} alebo None ak nie sú dáta.
    """
    csv_path, meta_path = paths(case, port)
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
            live_minutes=None, rt_params=None, plan_params=None, use_rt_override=None) -> dict:
    """Posunie simuláciu po teraz (alebo `now`). Dopočíta nové minúty, APPENDuje do CSV, uloží meta.
    base_case: z ktorého prípadu vziať NASTAVENIA (cfg); `case` ostáva kľúčom logu/súboru.
    d1_step_min: prepíše granularitu plánu (60=D-1 hodinový, 15=denný trh 15-min).
    live_minutes: dnešné ŽIVÉ minúty (provizórna ZCO=odhad) na real-time sledovanie dneška.
    use_rt_override: ak nie None, prepíše cfg.use_rt (True/False). Pre čistý plán bez RT vrstvy → False.
    Vracia súhrn pre stránku."""
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
        print(f"[livesim.advance] cfg merge z plan_params: "
              f"batt={cfg.batt_kw:.0f}kW/{cfg.batt_kwh:.0f}kWh, "
              f"FTV={cfg.kwp:.0f}kWp, "
              f"SOC={cfg.soc_min*100:.0f}–{cfg.soc_max*100:.0f}%, "
              f"grid={cfg.grid_kw:.0f}kW")
    rtc.apply_case(cfg)
    csv_path, meta_path = paths(case, port)
    now = pd.Timestamp(now) if now is not None else pd.Timestamp(dt.datetime.now())
    now = now.tz_localize(None) if now.tzinfo else now
    today = now.normalize()
    start_date = pd.Timestamp(start_date).normalize()
    prov_date = None
    if live_minutes is not None and len(live_minutes):
        prov_date = today.date()           # dnešok je provizórny (odhad ZCO)

    bkwh = cfg.batt_kwh
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
    # podpis OBSAHU uložených plánov v rozsahu start_date → today (strict mode):
    # ak sa plán pre niektorý deň regeneruje, settings_sig sa zmení → log sa prepočíta.
    plan_sigs = {}
    try:
        for _d in pd.date_range(pd.Timestamp(start_date), today, freq="D"):
            _diso = _d.date().isoformat()
            _p = ps.load_plan_safe(_diso, step_min_now, kind_now)
            if _p:
                plan_sigs[_diso] = _p.get("generated_at", "?")
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
           "csv_cols_v": "13"}  # bump: rt_rev_realistic_min (reality vs plan settlement cez ZCO)
    sig_s = json.dumps(sig, sort_keys=True, default=str)
    meta = _load_meta(meta_path)
    if meta is not None and meta.get("settings_sig") != sig_s:
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
            # (3) Row-count sanity check — chráni proti #600 (CSV poškodený / partial)
            if not _need_reset:
                try:
                    _meta_through = meta.get("done_through")
                    if _meta_through:
                        _through_ts = pd.Timestamp(_meta_through).normalize()
                        _days_expected = max(1, (_through_ts - _user_start).days + 1)
                        _rows_expected_min = int(_days_expected * 1440 * 0.5)   # tolerancia 50%
                        # Rýchly počet riadkov (nezávisle od CSV obsahu)
                        try:
                            with open(csv_path, "rb") as _f:
                                _rows_actual = sum(1 for _ in _f) - 1   # -header
                            if _rows_actual < _rows_expected_min:
                                _need_reset = True
                                _reset_reason = (
                                    f"CSV má {_rows_actual} riadkov ale očakávame "
                                    f"≥{_rows_expected_min} (od {_user_start.date()} po "
                                    f"{_through_ts.date()}, ~{_days_expected} dní) — "
                                    f"meta out-of-sync (#600)"
                                )
                        except (FileNotFoundError, OSError):
                            pass
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
    mn = _minute_all(live_minutes, min_from_date=start_date)
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
    while day <= today:
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
                        dtprof = (_price_real*_ex/1000.0 - (_price_real + _gf)*_im/1000.0
                                  - _cc*(_ch + _di)/2/1000.0)
                        # Bug VV (2026-06-08): pripočítaj TOU distribučný náklad k importu
                        # ak profile má joint_lp.optimize_distribution:true. Joint LP optimizer
                        # to už zaratáva v plánovacej fáze (joint_lp.py line 269-271), ale
                        # settlement za reálne ceny dovtedy TOU ignoroval → cum_dt v livesim
                        # CSV nezahŕňal TOU saving → "Prínos vs baseline" sa stratil pre VŠETKY
                        # profily s optimize_distribution:true (Simulacia_Coop, Trakany, ...).
                        try:
                            import settlement as _stl_tou
                            from core.profile_resolver import get_active as _ga_tou
                            _prof_tou = _ga_tou()
                            if _stl_tou.profile_uses_tou(_prof_tou):
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
            try:
                import vdt_state as _vs_sch
                from core.profile_resolver import get_active as _ga_sch
                _prof_sch = _ga_sch()
                if _prof_sch:
                    _vdt_kw_96 = _vs_sch.get_realized_batt_kw(_prof_sch,
                                                              today_iso=day.isoformat(),
                                                              dt_h=0.25)
                    if isinstance(_vdt_kw_96, list) and len(_vdt_kw_96) >= 96 and any(_vdt_kw_96):
                        if step == 15 and len(sch) >= 96:
                            # 1:1 mapping — slot i v sch zodpovedá slot i vo VDT
                            for _i in range(min(96, len(sch))):
                                sch.at[_i, "batt_kw"] = float(sch.at[_i, "batt_kw"]) + float(_vdt_kw_96[_i] or 0.0)
                        elif step == 60 and len(sch) >= 24:
                            # 60-min: každá hodina = priemer 4 VDT 15-min slotov
                            for _h in range(min(24, len(sch))):
                                _h_avg = sum(_vdt_kw_96[_h*4:_h*4+4]) / 4.0
                                sch.at[_h, "batt_kw"] = float(sch.at[_h, "batt_kw"]) + _h_avg
                        print(f"[livesim Bug BB] sch.batt_kw += VDT pre {_prof_sch} ({day.isoformat()}): "
                              f"{sum(1 for v in _vdt_kw_96 if abs(v)>0.01)} nenulových slotov")
            except Exception as _e_sch_vdt:
                print(f"[livesim Bug BB] aplikácia VDT do sch zlyhala: {_e_sch_vdt}")
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
                                             load_plan_kw=_load_plan_per)
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
                dam_per_min = [float(sch["batt_kw"].values[i]) for i in tr["pidx"]]
                vdt_per_min = [0.0] * len(dam_per_min)
                try:
                    import vdt_state as _vs
                    from core.profile_resolver import get_active as _ga
                    _profile = _ga()
                    if _profile:
                        # VDT je VŽDY 15-min granularita → pidx15 nezávislé od `step`.
                        pidx15 = [min(95, max(0, _period_index(t, day, 15))) for t in tr["ts15"]]
                        vdt_arr_kw = _vs.get_realized_batt_kw(_profile,
                                                              today_iso=day.isoformat(),
                                                              dt_h=0.25)
                        if isinstance(vdt_arr_kw, list) and len(vdt_arr_kw) >= 96:
                            vdt_per_min = [float(vdt_arr_kw[j] or 0.0) for j in pidx15]
                except Exception:
                    pass
                tr["plan_batt_dam_kw"] = dam_per_min
                tr["plan_batt_vdt_kw"] = vdt_per_min
                tr["plan_batt_kw"] = [d + v for d, v in zip(dam_per_min, vdt_per_min)]
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
                        _prof2 = _ga2()
                        if _prof2:
                            _vdt_st = _vs2._load_vdt_realized(_prof2, day.isoformat())
                            _vdt_kwh_arr = (_vdt_st or {}).get("kwh_batt_view") or [0.0] * 96
                            pidx15_grid = [min(95, max(0, _period_index(t, day, 15)))
                                            for t in tr["ts15"]]
                            vdt_kwh_per_min = [float(_vdt_kwh_arr[j] or 0.0) for j in pidx15_grid]
                except Exception:
                    pass
                _dam_grid = [float(sch["grid_kwh"].values[i]) for i in tr["pidx"]]
                tr["plan_grid_dam_kwh"] = _dam_grid
                tr["plan_grid_vdt_kwh"] = vdt_kwh_per_min
                tr["plan_grid_kwh"] = [g + v for g, v in zip(_dam_grid, vdt_kwh_per_min)]
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
                    _batt_p = np.asarray(tr["plan_batt_kw"].values, float)        # kW (+= vybíja, −= nabíja)
                    # Rozdelíme plán na nabíjanie (≤0) a vybíjanie (≥0)
                    _plan_chg = np.maximum(-_batt_p, 0.0)                          # kW nabíjanie
                    _plan_dis = np.maximum(_batt_p, 0.0)                           # kW vybíjanie
                    # Fyzicky dostupný výkon na nabíjanie = FTV (po pokrytí load) + grid import
                    _avail_for_chg = np.maximum(_ftv_r - _load_r, 0.0) + _grid_kw_import
                    # Fyzicky dostupný výkon na vybíjanie = grid export + load (po odpočítaní FTV)
                    _avail_for_dis = _grid_kw_export + np.maximum(_load_r - _ftv_r, 0.0)
                    # Clipnúť plán na fyzicky možné
                    _chg_real = np.minimum(_plan_chg, _avail_for_chg)
                    _dis_real = np.minimum(_plan_dis, _avail_for_dis)
                    _batt_real = _dis_real - _chg_real                              # ±kW
                    tr["batt_kw_realistic"] = _batt_real.round(1)
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
                    # rt_rev_realistic = dev × ZCO (€/min)
                    #   + dev = "long" (over-export, dodali viac než nominované) → ZCO > 0 → get paid
                    #   − dev = "short" (under-export / over-import) → ZCO > 0 → pay
                    _rt_rev_real = (_dev_kwh_min * _zco) / 1000.0
                    tr["rt_rev_realistic_min"] = _rt_rev_real.round(4)
                except Exception as _e:
                    tr["batt_kw_realistic"] = tr["plan_batt_kw"]
                    tr["ftv_min_curtailed_kw"] = 0.0
                    tr["rt_rev_realistic_min"] = 0.0
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
                # cum_rt = MERGE z dvoch zdrojov:
                # 1) rt_rev_min  — RT engine zúčtoval (s use_rt=True; v 15-min móde ≈ 0)
                # 2) rt_rev_realistic_min — odchýlka reality vs plan nominácia × ZCO
                # User (2026-05-30): "aj do vypoctu ekonomiky sa musi ukldatat ralita".
                # Berieme MAX absolútnej hodnoty — uprednostníme realistic v 15-min, RT engine v plan_d1.
                _rt_engine = tr["rt_rev_min"].fillna(0)
                _rt_real = tr.get("rt_rev_realistic_min", pd.Series([0.0]*len(tr), index=tr.index)).fillna(0)
                # Ak RT engine reálne pracoval (nejaké non-zero akcie) → použij ho; inak realistic
                _use_realistic = (_rt_engine.abs().sum() < 1e-6) and (_rt_real.abs().sum() > 1e-6)
                _rt_used = _rt_real if _use_realistic else _rt_engine
                tr["cum_rt"] = cum_rt_done + _rt_used.cumsum()
                tr["cum_dt"] = cum_dt_done + tr["dt_rev_min"].fillna(0).cumsum()
                tr["cum_total"] = tr["cum_dt"] + tr["cum_rt"]
                if d < today.date():
                    # DOKONČENÝ deň (skutočná ZCO) → zapíš do logu a fixuj kumulatívy
                    new = tr if last_min is None else tr[tr["time"] > last_min]
                    if not new.empty:
                        out = new.reindex(columns=CSV_COLS).copy()
                        out["time"] = pd.to_datetime(out["time"]).dt.strftime("%Y-%m-%d %H:%M:%S")
                        out.to_csv(csv_path, mode="a", header=False, index=False)
                        appended += len(new)
                        last_min = pd.Timestamp(new["time"].max())
                        soc = float(new["soc_kwh"].iloc[-1])   # spoločné SOC (plán + RT)
                    cum_dt_done += day_dt_total
                    # cum_rt_done: prefer realistic (dev×ZCO) keď RT engine bol pasívny (15-min mód)
                    _day_rt_engine = float(rev)
                    _day_rt_real = float(tr.get("rt_rev_realistic_min",
                                                  pd.Series([0.0]*len(tr))).fillna(0).sum())
                    cum_rt_done += _day_rt_real if (abs(_day_rt_engine) < 1e-6 and abs(_day_rt_real) > 1e-6) else _day_rt_engine
                    done_through = day
                else:
                    # DNEŠOK = provizórny (odhad ZCO) → LEN zobrazenie, NEukladá sa
                    today_dt = float(tr["dt_rev_min"].sum())
                    _today_rt_engine = float(rev)
                    _today_rt_real = float(tr.get("rt_rev_realistic_min",
                                                    pd.Series([0.0]*len(tr))).fillna(0).sum())
                    today_rt = (_today_rt_real if (abs(_today_rt_engine) < 1e-6 and abs(_today_rt_real) > 1e-6)
                                else _today_rt_engine)
                    tr["is_live"] = 1                       # živé minúty (po teraz)
                    # rozšír na CELÝ deň (0–24h): budúce minúty = LEN plán (DT/obchod/FTV/projekcia SOC), bez RT/live
                    try:
                        end_day = day + pd.Timedelta(days=1)
                        last_t = tr["time"].max() if not tr.empty else (day - pd.Timedelta(minutes=1))
                        fut_idx = pd.date_range(pd.Timestamp(last_t) + pd.Timedelta(minutes=1),
                                                end_day - pd.Timedelta(minutes=1), freq="1min")
                        if len(fut_idx):
                            pj = [min(len(dtprof)-1, max(0, _period_index(t, day, step))) for t in fut_idx]
                            soc_proj = soc; socs = []; socs_kwh = []
                            for i in pj:
                                soc_proj = min(bkwh, max(0.0, soc_proj - float(sch["batt_kw"].values[i])/60.0))
                                socs.append(soc_proj/bkwh*100.0); socs_kwh.append(soc_proj)
                            last_cum_dt = float(tr["cum_dt"].iloc[-1]) if not tr.empty else cum_dt_done
                            last_cum_rt = float(tr["cum_rt"].iloc[-1]) if not tr.empty else cum_rt_done
                            # Bug V (2026-06-07): pripočítaj VDT realized aj k projekcii
                            # (môže existovať VDT trade pre future slot ktorý už uzavrel).
                            _fut_dam = [float(sch["batt_kw"].values[i]) for i in pj]
                            _fut_vdt = [0.0] * len(_fut_dam)
                            try:
                                import vdt_state as _vs_fut
                                from core.profile_resolver import get_active as _ga_fut
                                _prof_fut = _ga_fut()
                                if _prof_fut:
                                    _vdt_kw_arr = _vs_fut.get_realized_batt_kw(_prof_fut,
                                                                                today_iso=day.isoformat(),
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
                                _prof_g = _ga_fut() if _ga_fut else None
                                if _prof_g:
                                    _vdt_st_fut = _vs_fut._load_vdt_realized(_prof_g, day.isoformat())
                                    _vdt_kwh_fut = (_vdt_st_fut or {}).get("kwh_batt_view") or [0.0] * 96
                                    _pidx15_grid = [min(95, max(0, _period_index(t, day, 15)))
                                                    for t in fut_idx]
                                    _fut_grid_vdt = [float(_vdt_kwh_fut[j] or 0.0) for j in _pidx15_grid]
                            except Exception:
                                pass
                            fut = pd.DataFrame({
                                "time": fut_idx, "ts15": fut_idx.floor("15min"),
                                "plan_batt_dam_kw": _fut_dam,
                                "plan_batt_vdt_kw": _fut_vdt,
                                "plan_batt_kw": [d2 + v2 for d2, v2 in zip(_fut_dam, _fut_vdt)],
                                "plan_grid_dam_kwh": _fut_grid_dam,
                                "plan_grid_vdt_kwh": _fut_grid_vdt,
                                "plan_grid_kwh": [g + v for g, v in zip(_fut_grid_dam, _fut_grid_vdt)],
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

    meta.update(done_through=done_through.strftime("%Y-%m-%d") if done_through is not None else None,
                soc_after_done=soc, cum_dt_done=cum_dt_done, cum_rt_done=cum_rt_done,
                last_min=last_min.strftime("%Y-%m-%d %H:%M:%S") if last_min is not None else None,
                skipped_no_sys_mw=skipped_no_sys_mw,        # #600: pre UI banner
                skipped_no_data=skipped_no_data,
                settings_sig=sig_s)
    with open(meta_path, "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1, default=str)

    cum_dt = cum_dt_done + today_dt
    cum_rt = cum_rt_done + today_rt
    soc_disp = soc
    if today_trace is not None and not today_trace.empty:
        _liv = today_trace[today_trace.get("is_live", 1) == 1] if "is_live" in today_trace.columns else today_trace
        if not _liv.empty:
            soc_disp = float(_liv["soc_kwh"].iloc[-1])       # SOC teraz = posledný ŽIVÝ stav (nie projekcia)
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


def _read_csv(case: str, port: str = "8000"):
    csv_path, _ = paths(case, port)
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


def available_days(case: str, port: str = "8000"):
    """Zoznam dní prítomných v logu (na prehliadanie histórie)."""
    df = _read_csv(case, port)
    if df is None or df.empty:
        return []
    return sorted(set(df["time"].dt.date))


def load_series(case: str, port: str = "8000", day=None, max_points: int = 2000):
    """Načíta rady z CSV pre grafy. day=None → celé (decimované); inak len daný deň (jemné).

    Dedup: ak CSV obsahuje viacero riadkov pre tú istú minútu (= rôzne advance() behy
    pre rovnaké nastavenia, znak že settings_sig reset zlyhal), ponecháme **POSLEDNÝ**
    výskyt (= najnovší výpočet). Tým sa grafy nezdvojnásobia.
    """
    df = _read_csv(case, port)
    if df is None or df.empty:
        return df
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
