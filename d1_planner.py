# -*- coding: utf-8 -*-
"""
d1_planner.py — D-1 (day-ahead) plánovač pre VDT integráciu.

Wrapper okolo `optimizer.optimize_day` ktorý:
  1. Načíta profile parametre (batt, FTV, load) cez profiles.py
  2. Fetchne DAM clearing ceny pre vybraný dátum + trh (cez market.fetch_dam)
  3. Fetchne PVF predikciu cez open-meteo (helper z app.py / cache)
  4. Načíta load profile pre profile
  5. Spustí optimize_day s 15-min granularitou (96 slotov)
  6. Uloží výsledok do plan_store
  7. Vráti schedule DataFrame + summary

Použitie:
    res = compute_d1_plan(date=dt.date.today() + dt.timedelta(days=1),
                          market='sk', profile='Trakany_real')
    if res['ok']:
        print(f"Profit: {res['summary']['revenue_eur']:.2f} €")

Read-only: žiadne objednávky, len výpočet a uloženie plánu lokálne.
"""
from __future__ import annotations
import datetime as dt
from typing import Dict, Any, Optional, List
import numpy as np
import pandas as pd


DEFAULT_DT = 0.25   # 15-min granularita


def _load_profile_params(profile: str) -> Dict[str, Any]:
    """Načíta batt/FTV/grid parametre z profile.

    Profile JSON má štruktúru { plan: {batt_kw, batt_kwh, ...}, dentrh: {...}, ... }
    — params sú v `p['plan']` sekcii. Vraciame flat dict s merge plan + top-level.
    Aj namapuje legacy kľúče (soc_init → soc_init_pct, soc_min → soc_min_pct, ...).
    """
    try:
        import profiles as _pr
        p = _pr.load_profile(profile)
        if not isinstance(p, dict):
            return {}
        # Flat dict — najprv plan section, potom top-level (legacy fallback)
        out = {}
        if isinstance(p.get("plan"), dict):
            out.update(p["plan"])
        # Top-level kľúče (mode, name) môžu prepísať plan — len ak nie sú v plan
        for k, v in p.items():
            if k not in out and not isinstance(v, dict):
                out[k] = v
        # VDT engine voľba — z dentrh (preferované, reálne ceny) alebo plan šablóny.
        # Bez tohto compute_d1_plan vždy default 'lp' → nepárové stratové večerné nákupy
        # aj keď užívateľ vybral párový matcher (2026-06-24).
        for _sec in ("dentrh", "plan"):
            _s = p.get(_sec)
            if isinstance(_s, dict):
                for _k in ("vdt_engine", "vdt_pair_priority"):
                    if _s.get(_k) and not out.get(_k):
                        out[_k] = _s[_k]
        # Legacy → moderne názvy
        rename = {
            "soc_init": "soc_init_pct",
            "soc_min": "soc_min_pct",
            "soc_max": "soc_max_pct",
        }
        for old, new in rename.items():
            if old in out and new not in out:
                out[new] = out[old]
        return out
    except Exception:
        pass
    return {}


def _load_load_profile(profile: str, date: dt.date,
                        slots: int = 96) -> np.ndarray:
    """Load profile pre daný deň v kWh/perióda (15-min). Vracia zeros ak nič."""
    try:
        import load_profile as _lp
        # Skús get_day_profile (deň-specific)
        if hasattr(_lp, "get_day_profile"):
            arr = _lp.get_day_profile(profile, date)
            if arr is not None and len(arr) >= slots:
                return np.asarray(arr[:slots], dtype=float)
    except Exception:
        pass
    return np.zeros(slots, dtype=float)


def _build_pv_kwh(profile_params: Dict[str, Any], date: dt.date,
                   slots: int = 96) -> np.ndarray:
    """PVF predikcia v kWh/perióda (15-min). Použijem PVGIS solar geometry helper.

    Toto je zjednodušený fallback — ak helper z app.py neexistuje, vracia zeros.
    """
    try:
        # Pokus o open-meteo helper (rovnaký fetch ako v /plan endpoint)
        import data_sources as _ds
        if hasattr(_ds, "fetch_pv_forecast"):
            kwp = float(profile_params.get("kwp", 99.0))
            lat = float(profile_params.get("lat", 48.15))
            lon = float(profile_params.get("lon", 17.10))
            tilt = float(profile_params.get("tilt", 30.0))
            azim = float(profile_params.get("azimuth", 0.0))
            eff = float(profile_params.get("eff", 0.85))
            df = _ds.fetch_pv_forecast(lat=lat, lon=lon, kwp=kwp, tilt=tilt,
                                         azimuth=azim, eff=eff, date=date)
            # Očakávame 24-hod alebo 96-15min; ak hodinový, expand na 15-min
            if df is not None and not df.empty:
                kwh_col = "pv_kwh" if "pv_kwh" in df.columns else df.columns[0]
                vals = df[kwh_col].values
                if len(vals) == 24:
                    # rozšíri na 96 (každá hodina /4)
                    out = np.repeat(vals, 4) / 4.0
                    return out[:slots]
                elif len(vals) >= slots:
                    return np.asarray(vals[:slots], dtype=float)
    except Exception:
        pass
    return np.zeros(slots, dtype=float)


def _forecast_degenerate_reason(arr) -> Optional[str]:
    """ZERO-FORECAST GUARD (2026-07-07): vráti dôvod, ak je forecast cien degenerovaný
    (samé nuly / plochá krivka = pokazený cenový model alebo prázdna história), inak None.

    Koreň „strata ako hrom" (VW_simulacia_2, 07-05→07-07): autoplan o 09:00 dostal
    forecast = samé nuly → uložil bezcenný D-1 plán (0..0 ceny → žiadna DAM arbitráž →
    dt=0). Plán je immutable → nula sa zapiekla. Guard: degenerovaný forecast sa NIKDY
    nesmie skomitovať — volajúci má radšej zlyhať (plán sa neuloží, autoplan skúsi znova
    keď sú dáta/model pripravené) než uložiť plochý nulový plán.

    Kill-switch FORECAST_DEGENERATE_GUARD=0.
    """
    import os as _os_g
    if _os_g.environ.get("FORECAST_DEGENERATE_GUARD", "1") == "0":
        return None
    a = np.asarray(arr, float)
    fin = a[np.isfinite(a)]
    if fin.size < 24:
        return f"príliš málo platných hodnôt ({fin.size})"
    nz = int((np.abs(fin) > 0.01).sum())
    spread = float(fin.max() - fin.min())
    if nz == 0:
        return "samé nuly (0 nenulových cien)"
    if spread < 0.5:
        return f"plochá krivka (spread {spread:.3f} €/MWh) — žiadny arbitrážny signál"
    return None


def _forecast_prices_15m(pp: Dict[str, Any], date: dt.date, market: str = None) -> np.ndarray:
    """96 × €/MWh PREDIKOVANÝCH cien pre daný deň — IDENTICKÝ forecast ako _gen_one_plan
    (app.py): hodinová ISOT predikcia (market-aware model na _isot_history + počasie) →
    15-min tvar (price_model_15m). Poistka: flat upsample. NEČÍTA reálny DAM.
    `market` (cz/sk) vyberá samostatný cenový model + históriu (SK z OKTE, CZ z OTE)."""
    from core.caches import _model, _isot_history, _fetch_pv_cached
    d = date
    price_scale = float(pp.get("price_scale", 1.0) or 1.0)
    kwp = float(pp.get("kwp", 0) or 0)
    if kwp > 0.01:
        wx = _fetch_pv_cached(float(pp.get("lat", 48.7)), float(pp.get("lon", 19.1)), kwp,
                              float(pp.get("tilt", 30.0)), float(pp.get("azimuth", 180.0)),
                              float(pp.get("eff", 0.9)), start=d, end=d)
        wx = wx.copy(); wx["time"] = pd.to_datetime(wx["time"]); wx = wx[wx.time.dt.date == d].copy()
        if wx.empty:
            raise RuntimeError(f"PV/počasie forecast nedostupné pre {d}")
    else:
        wx = pd.DataFrame({"time": pd.date_range(pd.Timestamp(d), periods=24, freq="h"),
                           "gti": np.zeros(24), "temp": np.full(24, 15.0), "cloud": np.full(24, 50.0)})
    _wx2 = wx[["time", "gti", "temp", "cloud"]].copy(); _wx2["isot_eur"] = np.nan
    hist = _isot_history(d, days=8, market=market).copy()
    for _c in ["gti", "temp", "cloud"]:
        hist[_c] = np.nan
    ctx = pd.concat([hist[["time", "isot_eur", "gti", "temp", "cloud"]], _wx2], ignore_index=True)
    _pm = _model(market)
    pred = _pm.predict(ctx)
    dayp = pred[pred.time.dt.date == d].sort_values("time")
    ph = (np.asarray(dayp.pred_isot.values, float) * price_scale)[:24]
    if len(ph) < 24:
        raise RuntimeError(f"forecast predikcia neúplná pre {d} ({len(ph)}/24)")
    if getattr(_pm, "_clip", None):                              # anti-runaway clip
        ph = np.clip(ph, _pm._clip[0], _pm._clip[1])
    _out96 = None
    try:
        from price_model_15m import load_cached as _pm15_load
        _m15 = _pm15_load("out/price_model_15m.joblib")
        if _m15 is not None:
            p96 = np.asarray(_m15.predict_shape(ph, d, _wx2[["time", "gti", "temp", "cloud"]]), float)[:96]
            if len(p96) >= 96:
                print(f"[15-MIN] {d.isoformat()}: PREDIKOVANÝ plán (autoplan) → 15-min MODEL na ISOT predikcii")
                _out96 = p96
    except Exception as _e15:
        print(f"[15-MIN] {d.isoformat()}: 15-min model zlyhal ({_e15}) → flat upsample")
    if _out96 is None:
        _out96 = np.repeat(ph, 4)
    # ZERO-FORECAST GUARD (2026-07-07): degenerovaný forecast (samé nuly / plochá krivka)
    # sa NIKDY nesmie skomitovať ako plán. Fail-loud → compute_d1_plan vráti ok:False →
    # autoplan neuloží → skúsi znova neskôr keď sú dáta/model pripravené. Rieši dt=0.
    _deg = _forecast_degenerate_reason(_out96)
    if _deg:
        raise RuntimeError(
            f"ZERO-FORECAST GUARD: degenerovaný forecast cien pre {d.isoformat()} "
            f"({_deg}) → plán sa NEuloží (pokazený model/história). "
            f"Kill: FORECAST_DEGENERATE_GUARD=0")
    return _out96


def compute_d1_plan(date: dt.date, *, market: Optional[str] = None,
                     profile: Optional[str] = None,
                     save_to_store: bool = True,
                     dt_h: float = DEFAULT_DT,
                     price_kind: str = "real",
                     max_export_kwh_day: Optional[float] = None,
                     max_import_kwh_day: Optional[float] = None) -> Dict[str, Any]:
    """Spočíta D-1 plán pre konkrétny deň + trh + profile.

    Args:
        date: deň plánu (zvyčajne zajtra).
        market: 'cz' | 'sk' (None = aktívny).
        profile: meno profile (None = default/aktívny).
        save_to_store: uložiť do plan_store.
        dt_h: dĺžka kroku v hodinách (0.25 = 15-min, 1.0 = hodinový).

    Returns:
        {
          "ok": bool, "error": str,
          "date": ISO, "market": str, "profile": str,
          "n_slots": int, "dt_h": float,
          "schedule": [{"period","cena_EUR","ch_kwh","di_kwh","ex_kwh","im_kwh",
                        "cu_kwh","soc_pct","pv_kwh","load_kwh"}, ...],
          "summary": {revenue_eur, cost_eur, fees_eur, cycles, ...}
        }
    """
    import market as _mk
    import optimizer as _opt

    m = market if market else _mk.get_active_market()
    prof = profile or "default"

    # 1. Profile params
    pp = _load_profile_params(prof)
    batt_kw = float(pp.get("batt_kw", 500.0))
    batt_kwh = float(pp.get("batt_kwh", 800.0))
    eff_c = float(pp.get("eff_c", 0.95))
    eff_d = float(pp.get("eff_d", 0.95))
    soc_min_pct = float(pp.get("soc_min_pct", 5.0))
    soc_max_pct = float(pp.get("soc_max_pct", 95.0))
    soc_init_pct = float(pp.get("soc_init_pct", 20.0))
    # SOC-CARRYOVER-PLAN (2026-07-02, user: „clip len maskuje, plán káže nabíjať pri 100 %"):
    # plán MUSÍ štartovať z REÁLNEHO prenosového SOC (koniec predošlého dňa / aktuálny SOC),
    # nie z fixného soc_init — inak jeho SOC trajektória je posunutá voči realite a káže
    # nabíjať do plnej / vybíjať z prázdnej batérie (fantómové povely, ktoré SOC-clip len ticho
    # zje). carried_soc_for_date: pre budúci deň = aktuálny SOC (najlepší odhad), pre historický
    # backtest = reálny koniec predošlého dňa z CSV. Kill-switch SOC_CARRYOVER_PLAN=1 (DEFAULT OFF).
    import os as _os_co
    if _os_co.environ.get("SOC_CARRYOVER_PLAN", "0") == "1":
        try:
            import livesim as _lsim_co
            _port_co = _os_co.environ.get("PORT") or _os_co.environ.get("APP_PORT") or "8000"
            for _case_co in ("dt_15min", "plan_d1"):
                _cs = _lsim_co.carried_soc_for_date(_case_co, port=str(_port_co),
                                                    date=date, profile=prof)
                if _cs and _cs.get("soc_pct") is not None:
                    _si_new = max(soc_min_pct, min(soc_max_pct, float(_cs["soc_pct"])))
                    print(f"[SOC-CARRYOVER-PLAN] {prof} {date}: soc_init {soc_init_pct:.1f}% "
                          f"→ carryover {_si_new:.1f}% (case={_case_co}; {_cs.get('note','')})")
                    soc_init_pct = _si_new
                    break
        except Exception as _e_co:
            print(f"[SOC-CARRYOVER-PLAN] {prof} {date}: {_e_co} → ostávam na soc_init")
    grid_kw = float(pp.get("grid_kw", 200.0))
    grid_kw_import = float(pp.get("grid_kw_import", grid_kw))
    grid_kw_export = float(pp.get("grid_kw_export", grid_kw))
    grid_fee = float(pp.get("grid_fee", 22.0))
    cycle_cost = float(pp.get("cycle_cost", 2.0))
    # MIN-SPREAD-PLAN (2026-06-30): D-1 plán MUSÍ rešpektovať profilový `min_spread`
    # (€/MWh) — bez neho LP chytal tenké/šumové vnútrohodinové spready z predikcie
    # (napr. nabíjanie večer pri 152 na predaj 180). pen=(cycle_cost+min_spread)/2/1000
    # zvýši prah round-trip cyklu. Default 0 = golden bit-exact (back-compat).
    min_spread_eur = float(pp.get("min_spread", pp.get("min_spread_eur", 0.0)) or 0.0)
    # NEG-PRICE-FLOOR (2026-07-04, user): pre tento typ plánu ber mierne záporné ceny
    # (floor < cena < 0, napr. −40 €/MWh) ako 0 — „je jedno či je trochu záporná". Tým sa
    # plán neprilepí na najzápornejší slot a nabíjanie sa môže rozložiť/začať skôr; ceny
    # ≤ floor (silno záporné) ostávajú reálne → LP prirodzene preferuje plné nabitie (platia
    # nám za odber). Default 0.0 = vypnuté (golden bit-exact). Aktivuje sa len záporným prahom.
    neg_price_floor = float(pp.get("plan_neg_price_floor_eur", 0.0) or 0.0)
    # CHARGE-EARLY (2026-07-04): jemný tie-breaker preferujúci skoršie nabíjanie (0 = vyp).
    charge_early_w = float(pp.get("plan_charge_early_w", 0.0) or 0.0)
    # PLAN-SOC-RESERVE (2026-07-05): reserve pre plán (headroom pre VDT). 0 = vyp/golden.
    soc_reserve_pct = float(pp.get("soc_reserve_pct", 0.0) or 0.0)
    # VDT-HEADROOM (2026-07-07): stropový headroom v D-1 pláne vyhradený pre VDT dokupy
    # (SOC ≤ max−headroom) → dokup má miesto, nič nepretečie. 0 = vyp/golden.
    vdt_headroom_pct = float(pp.get("vdt_headroom_pct", 0.0) or 0.0)
    # Stropy denného obchodovania (kWh/deň). Override z volania má prednosť pred profile defaultom.
    # 0 alebo None znamená "bez stropu".
    def _opt_float(v):
        try:
            f = float(v)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None
    if max_export_kwh_day is None:
        max_export_kwh_day = _opt_float(pp.get("max_export_kwh_day"))
    else:
        max_export_kwh_day = _opt_float(max_export_kwh_day)
    if max_import_kwh_day is None:
        max_import_kwh_day = _opt_float(pp.get("max_import_kwh_day"))
    else:
        max_import_kwh_day = _opt_float(max_import_kwh_day)

    # 2. Ceny: PREDIKOVANÝ plán (price_kind="forecast") = VŽDY forecast (ISOT + 15-min model,
    #    15-min, nečíta reálny DAM); DENNÝ TRH (price_kind="real") = reálny DAM (fetch_dam).
    _forecast = (str(price_kind).lower() == "forecast")
    if _forecast:
        dt_h = 0.25
        slots = 96
        try:
            prices = _forecast_prices_15m(pp, date, market=m)
        except Exception as e:
            return {"ok": False,
                    "error": f"forecast cien zlyhal pre {date.isoformat()}: {e}",
                    "date": date.isoformat(), "market": m, "profile": prof}
    else:
        slots = int(round(24 / dt_h))   # 96 pre 15-min, 24 pre hodinový
        try:
            df_dam = _mk.fetch_dam(date, market=m)
        except Exception as e:
            return {"ok": False,
                    "error": f"DAM fetch zlyhal pre {m} {date.isoformat()}: {e}",
                    "date": date.isoformat(), "market": m, "profile": prof}

        if df_dam is None or df_dam.empty:
            return {"ok": False,
                    "error": f"DAM pre {m} {date.isoformat()} je prázdny "
                             f"(reálny denný trh ešte neexistuje)",
                    "date": date.isoformat(), "market": m, "profile": prof}

        # Načítame ceny do array (96 alebo 24 podľa dt_h)
        # df_dam má 'period' (1-based) a 'cena_EUR'. Pre 15-min je 96 slotov.
        if len(df_dam) >= slots:
            # 15-min ceny — jednoducho zoberieme prvých `slots`
            prices = df_dam.sort_values("period")["cena_EUR"].values[:slots]
        elif len(df_dam) == 24 and slots == 96:
            # Hodinové reálne ceny → 15-min: dedikovaný 15-min MODEL (tvar), poistka flat.
            _hourly = df_dam.sort_values("period")["cena_EUR"].values
            prices = None
            try:
                from price_model_15m import load_cached as _pm15_load
                _m15 = _pm15_load("out/price_model_15m.joblib")
                if _m15 is not None:
                    prices = np.asarray(_m15.predict_shape(_hourly, date, None), dtype=float)[:96]
                    if len(prices) >= 96:
                        print(f"[15-MIN] {date.isoformat()}: hodinová DAM → 15-min MODEL (tvar)")
                    else:
                        prices = None
            except Exception as _e15:
                print(f"[15-MIN] {date.isoformat()}: 15-min model zlyhal ({_e15}) → flat upsample")
            if prices is None:
                prices = np.repeat(_hourly, 4)
                print(f"[15-MIN] {date.isoformat()}: hodinová DAM → flat upsample (poistka)")
        else:
            # Iný formát — preindex podľa period
            prices = np.zeros(slots, dtype=float)
            for _, r in df_dam.iterrows():
                try:
                    p = int(r["period"]) - 1
                    if 0 <= p < slots:
                        prices[p] = float(r["cena_EUR"])
                except (ValueError, KeyError):
                    pass

    # 3. PVF predikcia
    pv_kwh = _build_pv_kwh(pp, date, slots=slots)

    # 4. Load profile
    load_kwh = _load_load_profile(prof, date, slots=slots)

    # NEG-PRICE-FLOOR: mierny mínus (floor, 0) → 0 (pred LP). Vypnuté keď floor >= 0.
    if neg_price_floor < 0.0:
        prices = np.asarray(prices, float).copy()
        _mask_np = (prices < 0.0) & (prices > neg_price_floor)
        _n_clamp = int(_mask_np.sum())
        prices[_mask_np] = 0.0
        if _n_clamp:
            print(f"[NEG-PRICE-FLOOR] {prof} {date}: {_n_clamp} slotov ({neg_price_floor:.0f}, 0) "
                  f"€/MWh → 0 (mierny mínus ignorovaný, nabíjanie sa môže rozložiť skôr)")

    # 5. Spusti optimize_day
    try:
        sched_df, summary = _opt.optimize_day(
            pv_kwh=pv_kwh, price_eur=prices,
            batt_kw=batt_kw, batt_kwh=batt_kwh,
            eff_c=eff_c, eff_d=eff_d,
            soc_min_pct=soc_min_pct, soc_max_pct=soc_max_pct,
            soc_init_pct=soc_init_pct,
            grid_kw=grid_kw,
            grid_kw_import=grid_kw_import, grid_kw_export=grid_kw_export,
            grid_fee=grid_fee, cycle_cost=cycle_cost,
            min_spread_eur=min_spread_eur,
            dt=dt_h, load_kwh=load_kwh,
            max_export_kwh_day=max_export_kwh_day,
            max_import_kwh_day=max_import_kwh_day,
            charge_early_w=charge_early_w,
            soc_reserve_pct=soc_reserve_pct,
            vdt_headroom_pct=vdt_headroom_pct,
        )
    except Exception as e:
        return {"ok": False, "error": f"optimize_day zlyhal: {e}",
                "date": date.isoformat(), "market": m, "profile": prof}

    # 6. Serializuj schedule pre JSON
    schedule = []
    for i in range(len(sched_df)):
        h, mn = divmod(int(i * dt_h * 60), 60)
        period_str = f"{h:02d}:{mn:02d}"
        end_min = int(i * dt_h * 60 + dt_h * 60)
        eh, em = divmod(end_min, 60)
        end_str = f"{eh:02d}:{em:02d}"
        # Mapovanie z optimize_day výstupných stĺpcov na náš serializačný formát
        ch_kw = sched_df["_charge_kw"].iloc[i] if "_charge_kw" in sched_df.columns else 0
        di_kw = sched_df["_discharge_kw"].iloc[i] if "_discharge_kw" in sched_df.columns else 0
        row_d = {
            "period_idx": i + 1,
            "period": f"{period_str}-{end_str}",
            "cena_EUR": float(prices[i]) if i < len(prices) else None,
            "pv_kwh": float(pv_kwh[i]) if i < len(pv_kwh) else 0.0,
            "load_kwh": float(load_kwh[i]) if i < len(load_kwh) else 0.0,
            "ch_kwh": float(ch_kw) * dt_h,   # kW × dt(h) = kWh
            "di_kwh": float(di_kw) * dt_h,
            "ex_kwh": float(sched_df["_export_kwh"].iloc[i]) if "_export_kwh" in sched_df.columns else 0.0,
            "im_kwh": float(sched_df["_import_kwh"].iloc[i]) if "_import_kwh" in sched_df.columns else 0.0,
            "cu_kwh": float(sched_df["curtail_kwh"].iloc[i]) if "curtail_kwh" in sched_df.columns else 0.0,
            "soc_pct": float(sched_df["soc_pct"].iloc[i]) if "soc_pct" in sched_df.columns else None,
            "soc_kwh": float(sched_df["soc_kwh"].iloc[i]) if "soc_kwh" in sched_df.columns else None,
            "batt_kw": float(sched_df["batt_kw"].iloc[i]) if "batt_kw" in sched_df.columns else None,
            "grid_kwh": float(sched_df["grid_kwh"].iloc[i]) if "grid_kwh" in sched_df.columns else None,
            "order_mwh": float(sched_df["order_mwh"].iloc[i]) if "order_mwh" in sched_df.columns else None,
        }
        schedule.append(row_d)

    # 7. Save to plan_store — schedule transponujeme na Dict[str, list]
    if save_to_store:
        try:
            import plan_store as _ps
            params = {
                "batt_kw": batt_kw, "batt_kwh": batt_kwh,
                "eff_c": eff_c, "eff_d": eff_d,
                "soc_min_pct": soc_min_pct, "soc_max_pct": soc_max_pct,
                "soc_init_pct": soc_init_pct,
                "grid_kw_import": grid_kw_import, "grid_kw_export": grid_kw_export,
                "grid_fee": grid_fee, "cycle_cost": cycle_cost,
                "max_export_kwh_day": max_export_kwh_day,
                "max_import_kwh_day": max_import_kwh_day,
                # VDT engine voľba do plan params — vdt_live_advisor + downstream ju čítajú
                # odtiaľto (inak default 'lp'). 2026-06-24.
                "vdt_engine": str(pp.get("vdt_engine", "lp") or "lp"),
                "vdt_pair_priority": str(pp.get("vdt_pair_priority", "closest") or "closest"),
            }
            step_min = int(dt_h * 60)
            # plan_store.save_plan očakáva schedule ako Dict[str, list]
            # — kde key je stĺpec a value je list dĺžky n_slots
            sched_dict = {}
            for col in ("period", "cena_EUR", "pv_kwh", "load_kwh",
                        "ch_kwh", "di_kwh", "ex_kwh", "im_kwh", "cu_kwh",
                        "soc_pct", "soc_kwh", "batt_kw", "grid_kwh", "order_mwh"):
                sched_dict[col] = [r.get(col) for r in schedule]
            # Ukladáme ako kind='dentrh' (15-min) — jednotný formát so stránkou /dentrh.
            # Tým VDT live advisor + /vdt/d1 viewer čítajú TEN ISTÝ plán cez cascade
            # (dentrh → plan). Scheduler autoplan_d1 ho cez tento path tiež produkuje.
            # PREDIKOVANÝ plán (forecast) = kind "plan"; reálny denný trh 15-min = "dentrh".
            save_kind = "plan" if _forecast else ("dentrh" if step_min == 15 else "plan")
            _ps.save_plan(date.isoformat(), step_min, save_kind,
                          params=params,
                          schedule=sched_dict,
                          summary=_serializable_summary(summary),
                          profile=prof,
                          meta={"market": m, "source": "d1_planner.compute_d1_plan",
                                "computed_at": dt.datetime.now().isoformat(timespec="seconds")})
        except Exception as e:
            print(f"[d1_planner] plan_store.save_plan zlyhalo: {e}")

    return {
        "ok": True,
        "date": date.isoformat(),
        "market": m,
        "profile": prof,
        "n_slots": slots,
        "dt_h": dt_h,
        "schedule": schedule,
        "summary": _serializable_summary(summary),
    }


def _serializable_summary(s):
    """Convert summary dict (môže obsahovať numpy types) na čisté Python types."""
    if not isinstance(s, dict):
        return {}
    out = {}
    for k, v in s.items():
        if isinstance(v, (int, float, str, bool)) or v is None:
            out[k] = v
        elif isinstance(v, (np.integer,)):
            out[k] = int(v)
        elif isinstance(v, (np.floating,)):
            out[k] = float(v)
        else:
            out[k] = str(v)
    return out


def get_dam_commitments(date: dt.date, *, market: Optional[str] = None,
                         profile: Optional[str] = None,
                         basis: str = "batt",
                         kinds: Optional[List[str]] = None,
                         step_min: int = 15) -> Optional[List[float]]:
    """Načíta DAM commitment per slot pre VDT optimizer.

    Cascade: skúsi viac kind plánov v poradí kým nenájde existujúci.
    Default poradie:
        1. "dentrh"  — 15-min D-1 plán zo stránky /dentrh (primárny zdroj)
        2. "plan"    — 60-min D-1 plán zo stránky /plan (expanded 24→96, fallback)

    Jediný plán generovaný cez /plan alebo /dentrh — VDT advisor + /vdt/d1
    viewer používajú tú istú cascade, takže VŠADE sa zobrazuje TEN ISTÝ plán.

    Args:
        basis: "batt" → vráti BATT účasť na arbitráži (di_kwh − ch_kwh).
                Toto je správna voľba pre lower bound v VDT live advisor
                (ktorý modeluje len batt, bez FTV/load).
               "grid" → vráti CELKOVÝ grid flow z D-1 plánu (ex_kwh − im_kwh).
                Toto je full DAM nominácia (zahŕňa FTV→export aj load→import).
                Použité pre reporting/diagnostiku.
        kinds: vlastný cascade order (default = ["dam_d1", "dentrh", "plan"]).
        step_min: krok hľadaného plánu — ignored, cascade rieši aj 24-slot expand.

    Vracia list of kWh per slot (96 hodnôt):
        - Kladné = sme zaviazaní DODAŤ (predaj — batt vybíja resp. grid export)
        - Záporné = sme zaviazaní ODOBRAŤ (nákup — batt nabíja resp. grid import)
        - Nula = žiadny záväzok / nulové saldo

    Vracia None ak žiadny z plánov neexistuje.
    """
    if kinds is None:
        kinds = ["dentrh", "plan"]

    # Field-name aliasy — schedule môže byť z d1_planner formátu (ch_kwh, ...) alebo
    # z optimize_day priamo (_charge_kw, ...). Pre kW polia musíme násobiť dt_h
    # aby sme dostali kWh za perióda.
    if basis == "batt":
        key_pos, key_neg = "di_kwh", "ch_kwh"      # batt discharge / charge (kWh)
        alias_pos, alias_neg = "_discharge_kw", "_charge_kw"   # (kW, treba × dt_h)
        alias_is_power = True   # aliasy sú kW → treba prepočet
    else:
        key_pos, key_neg = "ex_kwh", "im_kwh"      # grid export / import (kWh)
        alias_pos, alias_neg = "_export_kwh", "_import_kwh"   # (kWh, no conv)
        alias_is_power = False

    def _extract_from_schedule(schedule, dt_h: float = 1.0):
        """Z plan_store schedule (Dict[str,list] alebo list-of-dicts) vyrobí
        list kWh per slot (pos − neg). None ak chýba.
        dt_h: dĺžka periódy v hodinách (pre alias_is_power=True konverzia kW→kWh)."""
        if not schedule:
            return None
        # Helper: vyber pole, fallback na alias
        def _pick_arr(d, primary, alias):
            arr = d.get(primary)
            if arr is not None and any(arr):
                return arr, False   # primárny kľúč, no conversion
            arr2 = d.get(alias)
            if arr2 is not None and any(arr2):
                return arr2, alias_is_power
            return None, False

        if isinstance(schedule, dict):
            pos_arr, pos_conv = _pick_arr(schedule, key_pos, alias_pos)
            neg_arr, neg_conv = _pick_arr(schedule, key_neg, alias_neg)
            if pos_arr is None and neg_arr is None:
                return None
            pos_arr = pos_arr or []
            neg_arr = neg_arr or []
            n = max(len(pos_arr), len(neg_arr))
            out = []
            for i in range(n):
                p = float(pos_arr[i]) if i < len(pos_arr) and pos_arr[i] is not None else 0.0
                ng = float(neg_arr[i]) if i < len(neg_arr) and neg_arr[i] is not None else 0.0
                if pos_conv:
                    p *= dt_h
                if neg_conv:
                    ng *= dt_h
                out.append(p - ng)
            return out
        # Legacy: list of dicts
        out = []
        for r in schedule:
            p_val = r.get(key_pos)
            p_conv = False
            if p_val is None:
                p_val = r.get(alias_pos)
                p_conv = alias_is_power
            ng_val = r.get(key_neg)
            ng_conv = False
            if ng_val is None:
                ng_val = r.get(alias_neg)
                ng_conv = alias_is_power
            try:
                p = float(p_val or 0)
                ng = float(ng_val or 0)
            except (TypeError, ValueError):
                p = ng = 0.0
            if p_conv:
                p *= dt_h
            if ng_conv:
                ng *= dt_h
            out.append(p - ng)
        return out if out else None

    def _expand_24_to_96(arr_24):
        """Hourly plán (24 slotov, kWh/hodinu) → 15-min plán (96 slotov, kWh/15min).
        Rozloženie 1:4 — každú hodinu rozdelí na 4 rovnaké 15-min sloty."""
        if not arr_24 or len(arr_24) != 24:
            return arr_24
        out_96 = []
        for v in arr_24:
            quarter = float(v) / 4.0
            out_96.extend([quarter] * 4)
        return out_96

    try:
        import plan_store as _ps
    except Exception:
        return None

    # Cascade — prejdi kindy v poradí
    for kind in kinds:
        # Pre kind='plan' je krok 60-min (1 hod = 24 slotov), inak 15-min
        step = 60 if kind == "plan" else 15
        dt_h = step / 60.0
        try:
            plan = _ps.load_plan(date.isoformat(), step_min=step, kind=kind,
                                  profile=profile)
        except Exception:
            plan = None
        if not plan:
            continue
        out = _extract_from_schedule(plan.get("schedule"), dt_h=dt_h)
        if out is None:
            continue
        # Ak hourly (24 slotov), expand na 96
        if len(out) == 24:
            out = _expand_24_to_96(out)
        # Annotácia — niektoré volajúce miesta sledujú odkiaľ kaskáda vrátila
        # (môžeme to vložiť do print pre debug, vrátime samotný list)
        try:
            print(f"[d1_planner.get_dam_commitments] cascade hit: kind='{kind}' "
                  f"step={step}min slots={len(out)} basis={basis}")
        except Exception:
            pass
        return out

    return None


if __name__ == "__main__":
    import datetime as _dt
    # Smoke test pre dnes (lebo zajtrajšie DAM možno ešte nie je)
    today = _dt.date.today()
    print(f"D-1 plán pre {today}, market=sk, profile=default")
    res = compute_d1_plan(today, market="sk", profile="default", save_to_store=False)
    print(f"ok: {res['ok']}")
    if res["ok"]:
        s = res["summary"]
        print(f"  Revenue: {s.get('revenue_eur', '?')} €")
        print(f"  Cost: {s.get('cost_eur', '?')} €")
        print(f"  Profit/net: {s.get('profit_eur', s.get('net_eur', '?'))} €")
        sched = res["schedule"]
        n_ch = sum(1 for r in sched if (r.get('ch_kwh') or 0) > 1)
        n_di = sum(1 for r in sched if (r.get('di_kwh') or 0) > 1)
        print(f"  Slots: {res['n_slots']} · charge: {n_ch} · discharge: {n_di}")
    else:
        print(f"  FAIL: {res.get('error')}")
