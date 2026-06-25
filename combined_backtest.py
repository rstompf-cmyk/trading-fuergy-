# -*- coding: utf-8 -*-
"""
combined_backtest.py – KOMBINOVANÝ backtest D-1 + RT (odchýlka), zdieľajúci JEDNU batériu.
Tak ako to pôjde v realite:
  1) D-1: predikcia ceny (model len z predošlých dní) → optimalizácia → nominácia,
     zúčtované proti SKUTOČNÝM cenám denného trhu.
  2) RT: arbitráž odchýlky batériou, časovaná živým odhadom CEPS, zúčtovaná proti SKUTOČNEJ ZCO,
     len v rámci ZVYŠNÉHO rozpočtu cyklov po D-1 (asset sa delí).

Použiteľné z CLI (python combined_backtest.py) aj z appky (run_combined()).
Dáta: out/price_train_2026.csv (D-1) a out/imbalance_history.csv (RT).
"""
from __future__ import annotations
import numpy as np, pandas as pd
from price_model import PriceModel
from optimizer import optimize_day
import rt_controller as rtc

# ---- VÝCHODISKOVÉ PARAMETRE (CLI; appka ich posiela z formulára) ----
BATT_KW, BATT_KWH = 100.0, 200.0
DPARAMS = dict(batt_kw=BATT_KW, batt_kwh=BATT_KWH, eff_c=0.95, eff_d=0.95,
               soc_min_pct=5, soc_max_pct=95, soc_init_pct=50, grid_kw=100.0,
               grid_fee=22.0, cycle_cost=2.0, allow_grid_charge=True, allow_curtail=True,
               terminal_soc_pct=50, min_spread_eur=30.0, block_neg_import=True)
RT_MARGIN = 20.0
MAX_CYCLES = 2.0
RETRAIN_EVERY = 7
DT_H = 0.25


def settle_d1(sch, actual_price, grid_fee, cycle_cost):
    ex, im = sch["_export_kwh"].values, sch["_import_kwh"].values
    ch, di = sch["_charge_kw"].values, sch["_discharge_kw"].values
    p = np.asarray(actual_price, float)
    return float((p*ex).sum()/1000 - ((p+grid_fee)*im).sum()/1000
                 - (cycle_cost*(ch+di)/2).sum()/1000)


def _dt15_for_day(mn_day):
    """96 reálnych 15-min DT cien pre deň, zarovnané 00:00..23:45 (z minútových dát)."""
    base = pd.Timestamp(mn_day["time"].iloc[0]).normalize()
    if pd.Timestamp(mn_day["time"].iloc[0]).tzinfo is not None:
        base = base.tz_localize(None)
    g = mn_day[["ts15", "isot_eur"]].dropna().copy()
    t = pd.to_datetime(g["ts15"])
    try:
        t = t.dt.tz_localize(None)
    except (TypeError, AttributeError):
        pass
    g["k"] = t
    s = g.groupby("k")["isot_eur"].first()
    idx = pd.date_range(base, periods=96, freq="15min")
    return s.reindex(idx).ffill().bfill().values


def _factor_for(month, pv_cal):
    if pv_cal is None:
        return 1.0
    if isinstance(pv_cal, dict):
        return float(pv_cal.get(month, pv_cal.get("_default", 1.0)))
    return float(pv_cal)


def run_combined(dparams=None, rt_margin=None, max_cycles=MAX_CYCLES,
                 retrain_every=RETRAIN_EVERY, start=None, end=None, pv_cal=None,
                 price_csv="out/price_train_2026.csv", imb_csv="out/imbalance_history.csv",
                 imb_min_csv="out/imbalance_minute.csv", rt_strong=None, rt_auto=True,
                 rt_kdis=1.0, rt_kchg=1.0, rt_dt_bias_k=None, use_rt=True, d1_step_min=60,
                 physical=True, plan_cycles=None, zco_bias_w=0.0, aggressive_rt=False,
                 ftv_balance_on=True, ftv_lookahead_h=4.0, ftv_persistence_throttle=True,
                 rt_no_worsen_dev=True, ftv_strict_plan=True, ftv_strict_deadband_kw=5.0,
                 use_scenarios=True):
    """Vráti DataFrame po dňoch. RT vrstva = POCTIVÝ MW-riadený minútový regulátor.
    use_rt=False → iba denný trh (bez odchýlky). d1_step_min=15 → D-1 arbitráž na reálnych 15-min cenách.
    rt_auto=True → adaptívne pásmo z volatility predošlých dní; inak rt_margin = VYBÍJACIE pásmo [MW].
    zco_bias_w>0 → plán sa rozhoduje na cene DT+váha·očakávaná_odchýlka (z deviation_profile); zúčtovanie ostáva na reálnej DT.
    aggressive_rt=True → RT engine obíde cycle dev_budget (limitovaný len SOC a výkonom batérie).
    ftv_balance_on/ftv_lookahead_h/ftv_persistence_throttle/rt_no_worsen_dev/ftv_strict_plan: FTV-balance pravidlá
        (rovnaké ako v /livesim) — batéria sa snaží neutralizovať threshold odchýlku, lookahead chráni budúce
        plánované akcie, no-worsen zabraňuje zhoršeniu, strict_plan vždy aktívne aj pri súladných znakoch.
    use_scenarios=True → pre dni s uloženým scenárom v ftv_scenarios sa použije scenár ako "FTV realita"
        (rovnaké ako v /livesim); inak sa generuje z plánu cez ftv_minute.
    pv_cal: None | float | {mesiac: faktor, '_default': faktor}."""
    try:
        import deviation_stats as _dstats
        _prof = _dstats.load_profile() if zco_bias_w else None
    except Exception:
        _dstats = None; _prof = None
    dp = dict(DPARAMS); dp.update(dparams or {})
    band_dis_fix = rtc.PROD_BAND_DIS if rt_margin is None else float(rt_margin)
    band_chg_fix = rtc.PROD_BAND_CHG

    # Market-aware DT real loader: pre SK skladá pr DataFrame zo seps_sk historianu
    # (cez settlement.get_dt_real_hourly per deň), pre CZ číta klasický CSV.
    _is_sk_market = False
    try:
        import market as _mk_cb
        _is_sk_market = (str(_mk_cb.get_active_market()).lower() == "sk")
    except Exception:
        pass

    if _is_sk_market:
        try:
            import settlement as _stl_cb
            import datetime as _dt_cb
            # Skladaj pr DataFrame zo SK historian — posledných 180 dní
            today_cb = _dt_cb.date.today()
            sk_rows = []
            for _delta in range(180):
                _d = today_cb - _dt_cb.timedelta(days=_delta)
                _h24 = _stl_cb.get_dt_real_hourly(_d.isoformat(), market="sk")
                if _h24 is None:
                    continue
                for _h, _val in enumerate(_h24):
                    if not (isinstance(_val, float) and np.isnan(_val)):
                        sk_rows.append({
                            "time": pd.Timestamp(_d) + pd.Timedelta(hours=_h),
                            "isot_eur": float(_val),
                        })
            if not sk_rows:
                raise RuntimeError("Žiadne SK DT historian dáta — overiť seps_sk extend job")
            pr = pd.DataFrame(sk_rows).sort_values("time").reset_index(drop=True)
            print(f"[combined_backtest SK] DT real loaded: {len(pr)} riadkov, "
                  f"{pr['time'].dt.date.nunique()} dní")
        except Exception as _e_sk:
            print(f"[combined_backtest SK] fallback na CZ CSV: {_e_sk}")
            pr = pd.read_csv(price_csv)
            pr["time"] = pd.to_datetime(pr["time"], errors="coerce")
            pr = pr.dropna(subset=["time"]).reset_index(drop=True)
    else:
        pr = pd.read_csv(price_csv)
        pr["time"] = pd.to_datetime(pr["time"], errors="coerce")
        pr = pr.dropna(subset=["time"]).reset_index(drop=True)
    pr["date"] = pr["time"].dt.date
    # Robustné parsovanie: jeden zlý riadok (napr. time=0) by inak rozbil celý stĺpec
    # (pd.read_csv s parse_dates= pri zlyhaní vráti object dtype potichu → .dt padne).
    mn = pd.read_csv(imb_min_csv)
    mn["time"] = pd.to_datetime(mn["time"], errors="coerce")
    mn["ts15"] = pd.to_datetime(mn["ts15"], errors="coerce")
    _n_before = len(mn)
    mn = mn.dropna(subset=["time", "ts15"]).reset_index(drop=True)
    if len(mn) < _n_before:
        print(f"[combined_backtest] imbalance_minute: dropnutých {_n_before - len(mn)} "
              f"riadkov s nečitateľným časom (zostáva {len(mn)})")
    mn["date"] = mn["time"].dt.date
    mn = rtc.prep(rtc._ensure_act(mn))   # kĺzavý priemer aFRR (spike), doplnenie stĺpcov
    sys_orient = rtc.PROD_SYS_ORIENT
    # MW signál po minútach + adaptívne pásmo z volatility PREDOŠLÝCH dní (kauzálne)
    mn["sig"] = [rtc.mw_signal(r._asdict(), rtc.PROD_W_SYS, sys_orient) for r in mn.itertuples(index=False)]
    day_sigma = mn.groupby("date")["sig"].std()
    sigma_trail = day_sigma.shift(1).rolling(3, min_periods=1).mean()   # σ z minulých dní
    have_price = {d for d, g in pr.groupby("date") if len(g) >= 24 and g.isot_eur.notna().all()}
    days = sorted(have_price & set(mn.date.unique()))
    if start:
        days = [d for d in days if d >= start]
    if end:
        days = [d for d in days if d <= end]
    bkwh = dp["batt_kwh"]; gf, cyc = dp["grid_fee"], dp["cycle_cost"]
    soc0 = dp["soc_init_pct"]/100*bkwh
    # strict mode: čítaj plány z plan_store; ak chýbajú, deň sa preskočí
    try:
        import plan_store as _ps
    except ImportError:
        _ps = None
    # FTV minútová realita + scenáre (rovnaké ako v /livesim)
    try:
        import ftv_minute as _fm
    except ImportError:
        _fm = None
    try:
        import ftv_scenarios as _fs
    except ImportError:
        _fs = None
    pm = None; rows = []; skipped = []
    for i, d in enumerate(days):
        train = pr[pr.date < d]; day = pr[pr.date == d].sort_values("time")
        if len(train) < 24*20:
            continue
        mth = pd.Timestamp(d).strftime("%Y-%m")
        fac = _factor_for(mth, pv_cal)                       # kalibrácia výroby
        d_iso = pd.Timestamp(d).date().isoformat()
        step_min_now = 15 if int(d1_step_min) == 15 else 60
        # ── STRICT MODE: čítaj plán z plan_store; bez plánu deň preskočíme ──
        # Cascade: 15-min → PREDIKOVANÝ (plan) → reálny DENNÝ TRH (dentrh); 60 → plan.
        if _ps is None:
            sch_disk = None
        elif step_min_now == 15:
            sch_disk = (_ps.load_plan_safe(d_iso, 15, "plan")
                        or _ps.load_plan_safe(d_iso, 15, "dentrh"))
        else:
            sch_disk = _ps.load_plan_safe(d_iso, 60, "plan")
        if sch_disk is None:
            skipped.append(d_iso)
            continue
        sched_dict = sch_disk["schedule"]
        sch = pd.DataFrame(sched_dict)
        # FTV kWp škálovanie: price_train_2026.csv má 'kw' pre 99 kWp inštaláciu (default kwp),
        # ale užívateľ môže mať iný profil (napr. Laugaricio 1200 kWp). Škálujeme lineárne.
        # Bez toho by /simulacia produkovala 12× nižší baseline/zisk než /livesim.
        _kwp_scale = float(dp.get("kwp", 99.0)) / 99.0
        # zarovnaj veličiny: pv pre baseline, actual = reálne DT ceny pre tento deň
        if int(d1_step_min) == 15:
            pv_h = day.kw.values * fac * _kwp_scale
            pv = np.repeat(pv_h, 4) / 4.0
            actual = _dt15_for_day(mn[mn.date == d])
            n = min(len(pv), len(actual)); pv, actual = pv[:n], actual[:n]
        else:
            pv = day.kw.values * fac * _kwp_scale
            actual = day.isot_eur.values
        d1 = settle_d1(sch, actual, gf, cyc)
        base = float((np.where(actual > 0, actual, 0)*pv).sum()/1000)
        d1_cycles = (sch["_charge_kw"].sum() + sch["_discharge_kw"].sum())/2/bkwh
        # zvyšný rozpočet pre RT; aggressive_rt → de facto bez stropu (limit len SOC/výkon)
        budget = (bkwh * 1000.0) if aggressive_rt else max(0.0, (max_cycles - d1_cycles))*bkwh
        # ---- RT vrstva (len ak use_rt): MW-riadený minútový regulátor; pásmo AUTO ----
        if use_rt:
            if rt_auto:
                bd, bc = rtc.auto_bands(sigma_trail.get(d, float("nan")))
            else:
                bd, bc = band_dis_fix, band_chg_fix
            bd *= rt_kdis; bc *= rt_kchg                      # škála (ako vo /rt)
            if physical:
                # JEDNA fyzická batéria: plán + odchýlka zdieľajú SOC/výkon; odchýlka = realita − plán na ZCO
                _dth = 0.25 if int(d1_step_min) == 15 else 1.0
                gkw = np.asarray(sch["grid_kwh"].values, float) / _dth   # nominácia do siete [kW] (s FTV)
                # rt_mask zo zapečeného plánu (strict mode) — rovnaké správanie ako v livesim
                _rtm_disk = sch_disk.get("rt_mask")
                _rt_mask_arr = np.asarray(_rtm_disk, float) if (_rtm_disk and len(_rtm_disk) == len(sch)) else None
                # ── FTV minútová realita + scenár (ako v livesim) ──
                _pv_min_kw = None; _pv_plan_kw = None
                if ftv_balance_on and _fm is not None:
                    # pôvodný PVF plán per perióda (= čo bolo nominované D-1)
                    _pv_plan_arr = np.asarray(sch.get("pv_kwh", []), float) / _dth   # kWh/perióda → kW
                    if _pv_plan_arr.size == 0:
                        _pv_plan_arr = np.asarray(pv, float)
                    _pv_plan_kw = _pv_plan_arr
                    # hodinová krivka pre minútový generátor: zo scenára (ak je uložený) alebo z plánu
                    _hourly_pv = None
                    if use_scenarios and _fs is not None and _fs.has_scenario(d_iso):
                        _sc = _fs.load_scenario(d_iso)
                        if _sc is not None:
                            _hourly_pv = np.asarray(_sc["hourly_kw"], float)
                    if _hourly_pv is None:
                        # konvertuj _pv_plan_arr (npn periód) na 24 hodinových
                        if _pv_plan_arr.size == 24:
                            _hourly_pv = _pv_plan_arr
                        elif _pv_plan_arr.size == 96:
                            _hourly_pv = _pv_plan_arr.reshape(24, 4).mean(axis=1)
                        elif _pv_plan_arr.size > 0:
                            _idx = np.linspace(0, _pv_plan_arr.size - 1, 24).round().astype(int)
                            _hourly_pv = _pv_plan_arr[_idx]
                    if _hourly_pv is not None and _hourly_pv.size == 24:
                        try:
                            _pv_min_kw = _fm.hourly_to_minute(_hourly_pv)
                        except Exception:
                            _pv_min_kw = None
                # asymetrické grid limity z plan_store params (ak prítomné), default = grid_kw
                _p_params = sch_disk.get("params", {}) or {}
                _gki = _p_params.get("grid_kw_import", None)
                _gke = _p_params.get("grid_kw_export", None)
                _gki = float(_gki) if _gki is not None else None
                _gke = float(_gke) if _gke is not None else None
                # ── LOAD: per-perióda + minútová realita ──
                _load_min_kw = None; _load_plan_per = None
                try:
                    import load_profile as _lp
                    import load_minute as _lm
                    if _lp.has_data():
                        _l_15min = _lp.load_for_date(d_iso)                                # 96 × kW
                        _npn = len(sch)
                        _load_plan_per = (_l_15min.reshape(24, 4).mean(axis=1) if _npn == 24
                                          else _l_15min)
                        _load_min_kw = _lm.fifteen_to_minute(_l_15min)
                except Exception:
                    _load_min_kw = None; _load_plan_per = None
                rt, rt_cycles = rtc.run_day_physical(
                    mn[mn.date == d].sort_values("time"),
                    np.asarray(sch["batt_kw"].values, float), pd.Timestamp(d),
                    int(d1_step_min) if int(d1_step_min) == 15 else 60,
                    bd, bc, rtc.PROD_W_SYS, sys_orient, soc0=soc0, dev_budget_kwh=budget,
                    dt_bias_k=rt_dt_bias_k, grid_kw_arr=gkw, grid_cap=dp["grid_kw"],
                    grid_cap_import=_gki, grid_cap_export=_gke,
                    rt_mask=_rt_mask_arr,
                    pv_min_kw=_pv_min_kw,
                    pv_plan_kw=_pv_plan_kw,
                    ftv_balance_on=(ftv_balance_on and _pv_min_kw is not None),
                    ftv_lookahead_h=ftv_lookahead_h,
                    ftv_persistence_throttle=ftv_persistence_throttle,
                    rt_no_worsen_dev=rt_no_worsen_dev,
                    ftv_strict_plan=ftv_strict_plan,
                    ftv_strict_deadband_kw=ftv_strict_deadband_kw,
                    load_min_kw=_load_min_kw,
                    load_plan_kw=_load_plan_per)
            else:
                rt, rt_cycles = rtc.run_day(mn[mn.date == d].sort_values("time"), bd, bc,
                                            rtc.PROD_W_SYS, sys_orient, budget_kwh=budget, soc0=soc0,
                                            dt_bias_k=rt_dt_bias_k, return_cycles=True)
        else:
            rt, rt_cycles = 0.0, 0.0                          # iba denný trh
        rows.append(dict(date=d, month=mth, baseline=base, d1=d1, rt=rt, combined=d1+rt,
                         d1_cycles=round(d1_cycles, 2), rt_cycles=round(rt_cycles, 2),
                         cycles=round(d1_cycles + rt_cycles, 2)))
    R = pd.DataFrame(rows)
    if not R.empty:
        R["prinos_baterie"] = R.combined - R.baseline
    if skipped:
        R.attrs["skipped_dates"] = skipped
        print(f"[run_combined] STRICT MODE: preskočených {len(skipped)} dní bez plánu v plan_store. "
              f"Prvé: {skipped[:5]}{' …' if len(skipped) > 5 else ''}")
    return R


# RT nastavenia berie z profilu prípadu (case_config). Prípad: CASE=<nazov> python combined_backtest.py


def main():
    import os
    import case_config as cc
    cc.ensure_default()
    name = os.environ.get("CASE", "default")
    cfg = cc.load_case(name)
    rtc.apply_case(cfg)                          # nastaví jadro (batéria, event, σ...) z prípadu
    pv_cal = None
    try:
        import json
        with open("out/pv_calibration.json") as fh:
            c = json.load(fh)
        pv_cal = dict(c.get("by_month", {})); pv_cal["_default"] = c.get("factor", 1.0)
        print(f"Kalibrácia výroby zapojená: {pv_cal}")
    except Exception:
        print("Bez kalibrácie výroby (out/pv_calibration.json nenájdený) – modelovaná výroba.")
    print(f"PRÍPAD: {cfg.name} | batéria {cfg.batt_kw:.0f}kW/{cfg.batt_kwh:.0f}kWh | "
          f"RT: auto={cfg.rt_auto}, škála vybíj.={cfg.rt_kdis}, škála nabíj.={cfg.rt_kchg}, DT citlivosť={cfg.dt_bias_k}")
    print(f"        REALIZMUS (aplikované): haircut={rtc.RT_HAIRCUT:g} (1.0=strop), "
          f"latencia={rtc.RT_LATENCY} min, flip={rtc.FLIP_EVENT}, "
          f"reversal_boost={rtc.REVERSAL_BOOST:g}" + (f" (scale {rtc.REVERSAL_SCALE:g} min)" if rtc.REVERSAL_BOOST > 0 else ""))
    physical = os.environ.get("TWOFLOW") != "1"      # DEFAULT = fyzika (jedna batéria); TWOFLOW=1 = starý model
    plan_cap = getattr(cfg, "plan_cycles", None)
    zb = float(os.environ.get("ZBIAS", "0") or 0)    # váha korekcie plánu z odhadu odchýlky (0=vyp)

    # ── ZB sweep: váha korekcie plánu z odhadu odchýlky (potrebuje out/deviation_profile.json) ──
    if os.environ.get("ZB") == "1":
        if not os.path.exists("out/deviation_profile.json"):
            print("CHÝBA out/deviation_profile.json — najprv spusti:  python deviation_stats.py")
            return
        ws = [float(x) for x in os.environ.get("WS", "0,0.2,0.4,0.6,0.8,1").split(",")]
        print("\n" + "="*92 + "\nZB: váha korekcie plánu z odhadu odchýlky (0=bez korekcie); fyzika, "
              f"strop plánu={plan_cap}\n" + "="*92)
        print(f"{'váha':>6}{'D-1 €':>9}{'RT €':>9}{'spolu €':>10}{'prínos €':>11}{'€/deň':>9}"
              f"{'TRAIN/d':>9}{'TEST/d':>8}{'TEST%':>7}")
        best = None
        for w in ws:
            Rs = run_combined(dparams=cfg.dparams(), max_cycles=cfg.max_cycles, pv_cal=pv_cal,
                              rt_auto=cfg.rt_auto, rt_kdis=cfg.rt_kdis, rt_kchg=cfg.rt_kchg,
                              rt_dt_bias_k=cfg.dt_bias_k, use_rt=cfg.use_rt, d1_step_min=cfg.d1_step_min,
                              physical=True, plan_cycles=plan_cap, zco_bias_w=w)
            if Rs.empty:
                continue
            d1 = Rs.d1.sum()-Rs.baseline.sum(); rt = Rs.rt.sum(); prin = Rs.prinos_baterie.sum(); nd = len(Rs)
            cut = int(len(Rs)*0.7); tr, te = Rs.iloc[:cut], Rs.iloc[cut:]
            trd = tr.prinos_baterie.sum()/max(1, len(tr)); ted = te.prinos_baterie.sum()/max(1, len(te))
            tpct = ted/trd*100 if trd else 0.0
            if best is None or prin > best[-1]:
                best = (w, prin/nd, ted, tpct, prin)
            print(f"{w:>6g}{d1:9.0f}{rt:9.0f}{Rs.combined.sum():10.0f}{prin:11.0f}{prin/nd:9.1f}"
                  f"{trd:9.1f}{ted:8.1f}{tpct:6.0f}%")
        if best:
            print("-"*92)
            print(f"NAJLEPŠIE in-sample: váha {best[0]:g} → {best[1]:.1f} €/deň (TEST {best[2]:.1f}, {best[3]:.0f}% TRAIN).")
            print("Pozn.: priemerná odchýlka po hodinách je MALÁ → čakaj malý efekt. Ak váha>0 nezlepší TEST/d,")
            print("       nechaj 0 (korekcia sa neoplatí). Korekcia mení len rozhodnutie plánu, nie zúčtovanie (reálna DT).")
        return

    print(f"        REŽIM: {'D-1 ('+str(cfg.d1_step_min)+'-min) + RT odchýlka' if cfg.use_rt else 'IBA DENNÝ TRH ('+str(cfg.d1_step_min)+'-min), bez RT'}"
          f" | odber zo siete: {'povolený' if cfg.allow_grid_charge else 'zakázaný'}"
          f" | batéria: {'JEDNA fyzická (plán+odchýlka zdieľajú SOC/výkon)' if physical else 'dvojtoková (starý model)'}"
          f"{' | strop plánu '+str(plan_cap)+' cyklov' if plan_cap else ''}")

    # ── SPLIT sweep: delenie batérie medzi PLÁN a ODCHÝLKU (strop cyklov plánu) ──
    if os.environ.get("SPLIT") == "1":
        caps = [float(x) for x in os.environ.get("PLAN", "0.5,1,1.5,2,2.5,3").split(",")]
        print("\n" + "="*86 + "\nSPLIT: strop cyklov D-1 PLÁNU (zvyšok kapacity/cyklov ide ODCHÝLKE); fyzika, "
              f"max_cycles={cfg.max_cycles:g}\n" + "="*86)
        print(f"{'plán≤cyk':>9}{'D-1 €':>9}{'RT €':>9}{'spolu €':>10}{'prínos €':>11}{'€/deň':>9}{'cykly/d':>9}{'najlepšie':>11}")
        best = None
        for pc in caps:
            Rs = run_combined(dparams=cfg.dparams(), max_cycles=cfg.max_cycles, pv_cal=pv_cal,
                              rt_auto=cfg.rt_auto, rt_kdis=cfg.rt_kdis, rt_kchg=cfg.rt_kchg,
                              rt_dt_bias_k=cfg.dt_bias_k, use_rt=cfg.use_rt, d1_step_min=cfg.d1_step_min,
                              physical=True, plan_cycles=pc)
            if Rs.empty:
                continue
            d1 = Rs.d1.sum()-Rs.baseline.sum(); rt = Rs.rt.sum(); prin = Rs.prinos_baterie.sum()
            nd = len(Rs); cpd = Rs.cycles.mean()
            row = (pc, d1, rt, Rs.combined.sum(), prin, prin/nd, cpd)
            if best is None or prin > best[4]:
                best = row
            print(f"{pc:>9g}{d1:9.0f}{rt:9.0f}{Rs.combined.sum():10.0f}{prin:11.0f}{prin/nd:9.1f}{cpd:9.2f}{'':>11}")
        if best:
            print("-"*86)
            print(f"OPTIMUM: strop plánu {best[0]:g} cyklov → prínos {best[4]:.0f} € ({best[5]:.1f} €/deň), "
                  f"D-1 {best[1]:.0f} € + RT {best[2]:.0f} €.")
            print("Pozn.: nižší strop plánu uvoľní viac batérie pre odchýlku, ale plán zarobí menej – hľadáme vrchol súčtu.")
        return

    # ── RTK sweep: agresivita RT pásiem (mierka na rt_kdis/rt_kchg); nižšie = agresívnejšia odchýlka ──
    if os.environ.get("RTK") == "1":
        scales = [float(x) for x in os.environ.get("KS", "0.4,0.6,0.8,1,1.3,1.6").split(",")]
        print("\n" + "="*92 + f"\nRTK: agresivita RT pásiem (mierka × rt_kdis={cfg.rt_kdis:g}/rt_kchg={cfg.rt_kchg:g}); "
              f"fyzika, strop plánu={plan_cap}, max_cycles={cfg.max_cycles:g}\n" + "="*92)
        print(f"{'mierka':>7}{'kdis':>7}{'kchg':>7}{'spolu €':>9}{'prínos €':>10}{'€/deň':>8}"
              f"{'TRAIN/d':>9}{'TEST/d':>8}{'TEST%':>7}{'cykly/d':>9}")
        best = None
        for s in scales:
            kd, kc = cfg.rt_kdis*s, cfg.rt_kchg*s
            Rs = run_combined(dparams=cfg.dparams(), max_cycles=cfg.max_cycles, pv_cal=pv_cal,
                              rt_auto=cfg.rt_auto, rt_kdis=kd, rt_kchg=kc,
                              rt_dt_bias_k=cfg.dt_bias_k, use_rt=cfg.use_rt, d1_step_min=cfg.d1_step_min,
                              physical=True, plan_cycles=plan_cap)
            if Rs.empty:
                continue
            prin = Rs.prinos_baterie.sum(); nd = len(Rs); cpd = Rs.cycles.mean()
            cut = int(len(Rs)*0.7)
            tr, te = Rs.iloc[:cut], Rs.iloc[cut:]
            trd = tr.prinos_baterie.sum()/max(1, len(tr))
            ted = te.prinos_baterie.sum()/max(1, len(te))
            tpct = ted/trd*100 if trd else 0.0
            # robustné optimum: rozhoduje TEST €/deň (generalizácia), nie in-sample
            if best is None or ted > best[-1]:
                best = (s, kd, kc, prin/nd, ted, tpct, ted)
            print(f"{s:>7g}{kd:>7.2f}{kc:>7.2f}{Rs.combined.sum():9.0f}{prin:10.0f}{prin/nd:8.1f}"
                  f"{trd:9.1f}{ted:8.1f}{tpct:6.0f}%{cpd:9.2f}")
        if best:
            print("-"*92)
            print(f"NAJROBUSTNEJŠIE (podľa TEST €/deň): mierka {best[0]:g} (kdis={best[1]:.2f}, kchg={best[2]:.2f}) "
                  f"→ TEST {best[4]:.1f} €/deň ({best[5]:.0f} % TRAIN-u), spolu {best[3]:.1f} €/deň.")
            print("Pozn.: širšie pásmo = vyberavejšia odchýlka (len silné signály). Rozhoduj podľa TEST/d (nevidené dni),")
            print("       nie podľa in-sample maxima – ploché plató v šume ber ako rovnocenné a zvoľ menej agresívne.")
        return

    # ── PPS: rozdelenie výkonu medzi REZERVU (podporné služby) a ARBITRÁŽ + SOC-dostupnosť ──
    if os.environ.get("PPS") == "1":
        import copy
        price = float(os.environ.get("PPS_PRICE", "14.6"))     # kapacitná cena [€/MW/h]
        comm = float(os.environ.get("PPS_COMM", "0.30"))       # provízia agregátora (podiel)
        hours = float(os.environ.get("PPS_HOURS", "24"))       # hodín/deň v rezerve
        dur = float(os.environ.get("PPS_DUR", "0.5"))          # energetický vankúš na rezervu [h na kW]
        rtype = os.environ.get("PPS_TYPE", "sym")              # sym | pos | neg
        levels = [float(x) for x in os.environ.get("LV", "0,25,50,75,100").split(",")]
        BK0, BKWH0 = cfg.batt_kw, cfg.batt_kwh
        print("\n" + "="*96 + f"\nPPS: rezerva (podporné služby) ↔ arbitráž — cena {price:g} €/MW/h, "
              f"provízia {comm*100:.0f}%, {hours:g} h/deň, vankúš {dur:g} h, typ {rtype}\n" + "="*96)
        print(f"{'rezerva kW':>10}{'arb. kW':>9}{'SOC okno %':>11}{'kapacita €/d':>13}"
              f"{'arbitráž €/d':>13}{'SPOLU €/d':>11}{'cykly/d':>9}")
        best = None
        for R in levels:
            R = min(R, BK0)
            res_frac = (R*dur)/BKWH0                             # podiel kapacity držaný ako vankúš (na stranu)
            mcfg = copy.deepcopy(cfg)
            mcfg.batt_kw = max(0.0, BK0 - R)                     # rezerva ukrojí výkon z arbitráže
            lo, hi = cfg.soc_min, cfg.soc_max
            if rtype in ("sym", "pos"):
                lo = cfg.soc_min + res_frac                      # drž náboj na vybíjaciu rezervu
            if rtype in ("sym", "neg"):
                hi = cfg.soc_max - res_frac                      # drž miesto na nabíjaciu rezervu
            mcfg.soc_min, mcfg.soc_max = lo, hi
            window = (hi - lo)*100
            cap_day = (R/1000.0)*price*hours*(1.0-comm)          # kapacitný príjem/deň (po provízii)
            if window <= 2 or mcfg.batt_kw <= 0:                 # arbitráž nemožná (okno/ výkon vyčerpané)
                arb_day, cpd, nd = 0.0, 0.0, 0
                arb_tot = 0.0
            else:
                rtc.apply_case(mcfg)
                Rs = run_combined(dparams=mcfg.dparams(), max_cycles=mcfg.max_cycles, pv_cal=pv_cal,
                                  rt_auto=cfg.rt_auto, rt_kdis=cfg.rt_kdis, rt_kchg=cfg.rt_kchg,
                                  rt_dt_bias_k=cfg.dt_bias_k, use_rt=cfg.use_rt, d1_step_min=cfg.d1_step_min,
                                  physical=True, plan_cycles=plan_cap)
                if Rs.empty:
                    continue
                nd = len(Rs); arb_tot = Rs.prinos_baterie.sum(); arb_day = arb_tot/nd; cpd = Rs.cycles.mean()
            total_day = cap_day + arb_day
            if best is None or total_day > best[-1]:
                best = (R, mcfg.batt_kw, window, cap_day, arb_day, total_day)
            print(f"{R:>10g}{mcfg.batt_kw:>9.0f}{window:>11.0f}{cap_day:>13.1f}{arb_day:>13.1f}{total_day:>11.1f}{cpd:>9.2f}")
        rtc.apply_case(cfg)                                     # obnov pôvodný prípad
        if best:
            print("-"*96)
            print(f"OPTIMUM: rezerva {best[0]:g} kW (arbitráž na {best[1]:.0f} kW, SOC okno {best[2]:.0f} %) "
                  f"→ SPOLU {best[5]:.1f} €/deň (kapacita {best[3]:.1f} + arbitráž {best[4]:.1f}).")
            print("Pozn.: kapacita = R·cena·hodiny·(1−provízia). Rezerva ukrojí výkon AJ SOC okno z arbitráže (SOC-dostupnosť).")
            print("       Aktivačnú energiu (príjem/náklad pri reálnom vyvolaní) tu NEpočítame – je to konzervatívny odhad.")
            print(f"       Cena {price:g} €/MW/h je volatilná (vianoce 2025 ~2–3, dlhodobý aFRR+ 2026 ~14,6). Skús viac cien (PPS_PRICE).")
        return

    if os.environ.get("SWEEP") == "1":
        caps = [float(x) for x in os.environ.get("CYCLES", "1.5,2,2.5,3,4,5").split(",")]
        print("\n" + "="*78 + "\nSWEEP denného stropu cyklov (ostatné parametre prípadu nezmenené)\n" + "="*78)
        print(f"{'cyklov/deň':>11}{'D-1 €':>9}{'RT €':>9}{'spolu €':>10}{'prínos €':>11}{'€/deň':>9}{'vs 2c':>9}")
        base = None
        for mc in caps:
            Rs = run_combined(dparams=cfg.dparams(), max_cycles=mc, pv_cal=pv_cal,
                              rt_auto=cfg.rt_auto, rt_kdis=cfg.rt_kdis, rt_kchg=cfg.rt_kchg,
                              rt_dt_bias_k=cfg.dt_bias_k, use_rt=cfg.use_rt, d1_step_min=cfg.d1_step_min,
                              physical=physical, plan_cycles=plan_cap)
            if Rs.empty:
                continue
            d1 = Rs.d1.sum() - Rs.baseline.sum(); rt = Rs.rt.sum()
            prin = Rs.prinos_baterie.sum(); nd = len(Rs)
            if abs(mc - 2.0) < 1e-9 or base is None:
                base = prin
            print(f"{mc:>11g}{d1:9.0f}{rt:9.0f}{Rs.combined.sum():10.0f}{prin:11.0f}"
                  f"{prin/nd:9.1f}{prin-base:+9.0f}")
        print("Pozn.: viac cyklov = viac výnosu, ale aj viac opotrebenia batérie (modelované cez cycle_cost).")
        print("       Rozhodni podľa reálnej degradácie/záruky tvojej batérie, koľko cyklov/deň dovolíš.")
        return

    R = run_combined(dparams=cfg.dparams(), max_cycles=cfg.max_cycles, pv_cal=pv_cal,
                     rt_auto=cfg.rt_auto, rt_kdis=cfg.rt_kdis, rt_kchg=cfg.rt_kchg,
                     rt_dt_bias_k=cfg.dt_bias_k, use_rt=cfg.use_rt, d1_step_min=cfg.d1_step_min,
                     physical=physical, plan_cycles=plan_cap, zco_bias_w=zb)
    if R.empty:
        print("Žiadne spoločné dni s D-1 aj RT dátami. Stiahni imbalance_history pre rovnaké obdobie."); return
    R.to_csv("out/combined_backtest.csv", index=False)
    print(f"Kombinovaný backtest: {len(R)} dní ({R.date.min()} … {R.date.max()})")
    print("\n" + "="*78)
    print("PO DŇOCH (€):  baseline=bez batérie  D-1=denný trh  RT=odchýlka  spolu=kombinovaný")
    print("="*78)
    print(f"{'dátum':12}{'baseline':>10}{'D-1':>9}{'RT':>8}{'spolu':>9}{'prínos bat.':>13}{'cykly':>8}")
    for mth, g in R.groupby("month"):
        for _, r in g.iterrows():
            print(f"{str(r.date):12}{r.baseline:10.1f}{r.d1:9.1f}{r.rt:8.1f}{r.combined:9.1f}{r.prinos_baterie:13.1f}{r.cycles:8.2f}")
        s = g[["baseline", "d1", "rt", "combined", "prinos_baterie"]].sum()
        print(f"{'  ── '+mth+' spolu':12}{s.baseline:10.1f}{s.d1:9.1f}{s.rt:8.1f}{s.combined:9.1f}{s.prinos_baterie:13.1f}{g.cycles.mean():8.2f}")
        print("-"*86)
    print("\n" + "="*78 + "\nPO MESIACOCH (€)\n" + "="*78)
    print(f"{'mesiac':10}{'dní':>5}{'baseline':>11}{'D-1':>10}{'RT':>9}{'spolu':>10}{'prínos bat.':>13}{'€/deň':>9}{'cykly/d':>9}")
    M = R.groupby("month").agg(dni=("date", "count"), baseline=("baseline", "sum"), d1=("d1", "sum"),
                               rt=("rt", "sum"), combined=("combined", "sum"), prinos=("prinos_baterie", "sum"),
                               cykly=("cycles", "mean"))
    for mth, r in M.iterrows():
        print(f"{mth:10}{int(r.dni):5d}{r.baseline:11.1f}{r.d1:10.1f}{r.rt:9.1f}{r.combined:10.1f}{r.prinos:13.1f}{r.prinos/r.dni:9.1f}{r.cykly:9.2f}")
    t = M[["baseline", "d1", "rt", "combined", "prinos"]].sum(); nd = int(M.dni.sum())
    print("-"*92)
    print(f"{'SPOLU':10}{nd:5d}{t.baseline:11.1f}{t.d1:10.1f}{t.rt:9.1f}{t.combined:10.1f}{t.prinos:13.1f}{t.prinos/nd:9.1f}{R.cycles.mean():9.2f}")
    print("="*92)
    print(f"Prínos batérie: D-1 {t.d1-t.baseline:.0f} € + RT {t.rt:.0f} € = {t.prinos:.0f} € "
          f"za {nd} dní  →  ~{t.prinos/nd*30:.0f} €/mesiac")

    # ── OUT-OF-SAMPLE kontrola: rozdelenie na nalaď (train) vs otestuj naslepo (test) ──
    Rs = R.sort_values("date").reset_index(drop=True)
    cut = int(len(Rs)*0.7)
    tr, te = Rs.iloc[:cut], Rs.iloc[cut:]
    print("\n" + "="*78 + "\nOUT-OF-SAMPLE (rovnaké pevné parametre na oboch častiach)\n" + "="*78)
    print(f"{'časť':22}{'dní':>5}{'D-1 €/d':>10}{'RT €/d':>10}{'prínos €/d':>13}")
    for lab, x in [(f"TRAIN {tr.date.min()}..{tr.date.max()}", tr),
                   (f"TEST  {te.date.min()}..{te.date.max()}", te)]:
        n = max(len(x), 1)
        print(f"{lab:22}{len(x):5d}{(x.d1-x.baseline).sum()/n:10.1f}{x.rt.sum()/n:10.1f}{x.prinos_baterie.sum()/n:13.1f}")
    if len(te) and len(tr):
        rd = te.prinos_baterie.sum()/len(te); rdtr = tr.prinos_baterie.sum()/len(tr)
        print(f"→ TEST prínos {rd:.1f} €/deň je {rd/rdtr*100:.0f} % TRAIN-u "
              f"({'drží — nezdá sa preučené' if rd >= 0.8*rdtr else 'výrazne nižšie — možné preučenie / iný režim'}).")
    print("Pozn.: parametre sú pevné a kauzálne (auto-pásma zo σ minulých dní, D-1 model trénuje len na minulosti);")
    print("       ak TEST ≈ TRAIN, výnos je dôveryhodný aj na nevidených dňoch.")
    print("Detail po dňoch: out/combined_backtest.csv")


if __name__ == "__main__":
    main()
