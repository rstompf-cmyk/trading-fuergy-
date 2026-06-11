# -*- coding: utf-8 -*-
"""
vdt_extras.py — Návrh extra obchodov nad DAM plánom.

Pre každý 15-min slot porovná aktuálny VDT orderbook (Ask/Bid) s DAM clearing
cenou a navrhne dodatočný obchod NAD rámec D-1 DAM nominácie — ak je výhodný
a batéria má voľnú kapacitu.

Dve metódy vedľa seba:
  - propose_greedy() — per-slot scoring, threshold profit margin (€/MWh)
  - propose_lp()     — globálny LP re-solve s VDT cenami + DAM ako lower bounds

Read-only: iba odporúčania, žiadne zápisy do OKTE.

Vstupy:
  - dam_commits: list[float] dĺžky 96 (kWh per slot na batt-basis, kladné=export)
  - dam_clearing: dict {slot_idx: eur_mwh} alebo list dĺžky 96
  - orderbook_df: snapshot DataFrame s ob_best_bid_eur/ask_eur/mw stĺpcami
  - profile_cfg: dict s batt_kw, batt_kwh, soc_min_pct, soc_max_pct, eff_c/d
  - start_soc_pct: aktuálny SOC v %

Výstup (per metóda):
  {
    "method": "greedy" | "lp",
    "ok": bool,
    "slots": [
      {"slot_idx": 56, "slot_label": "14:00",
       "direction": "BUY"|"SELL", "kwh": 25.0, "price_eur_mwh": 48.50,
       "dam_clearing_eur_mwh": 45.00, "delta_profit_eur": 1.85,
       "soc_before_pct": 60.0, "soc_after_pct": 73.0,
       "status": "LIVE"|"STALE", "age_seconds": 12},
      ...
    ],
    "summary": {"n_proposals": int, "total_delta_profit_eur": float,
                "total_buy_kwh": float, "total_sell_kwh": float}
  }
"""
from __future__ import annotations
import datetime as dt
from typing import Dict, Any, List, Optional, Tuple
import numpy as np


# ---------------------------------------------------------------------------
# Helpery
# ---------------------------------------------------------------------------

def _slot_label(idx: int) -> str:
    h, m = divmod(idx * 15, 60)
    return f"{h:02d}:{m:02d}"


def _norm_dam_clearing(dam_clearing: Any) -> Dict[int, float]:
    """Normalizuje vstup na dict {slot_idx: eur_mwh}."""
    if dam_clearing is None:
        return {}
    if isinstance(dam_clearing, dict):
        return {int(k): float(v) for k, v in dam_clearing.items()
                if v is not None and not (isinstance(v, float) and np.isnan(v))}
    # list/array
    out = {}
    for i, v in enumerate(dam_clearing):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        out[int(i)] = float(v)
    return out


def _norm_dam_commits(dam_commits: Any) -> List[float]:
    """Normalizuje DAM nominácie na list dĺžky 96 (kladné=export, záporné=import)."""
    if dam_commits is None:
        return [0.0] * 96
    arr = [0.0] * 96
    if isinstance(dam_commits, dict):
        for k, v in dam_commits.items():
            i = int(k)
            if 0 <= i < 96:
                arr[i] = float(v or 0.0)
    elif hasattr(dam_commits, "__len__"):
        for i in range(min(96, len(dam_commits))):
            try:
                arr[i] = float(dam_commits[i] or 0.0)
            except Exception:
                arr[i] = 0.0
    return arr


def _extract_orderbook_prices(orderbook_df) -> Dict[int, Dict[str, float]]:
    """Z snapshot DataFrame vytiahne per slot_idx dict s bid/ask cenami + MW.

    Returns: {slot_idx: {"bid_eur": x, "bid_mw": y, "ask_eur": z, "ask_mw": w,
                          "age_seconds": int}}
    """
    out: Dict[int, Dict[str, float]] = {}
    if orderbook_df is None or len(orderbook_df) == 0:
        return out
    for _, row in orderbook_df.iterrows():
        try:
            si = int(row.get("slot_idx", -1))
        except Exception:
            continue
        if si < 0 or si >= 96:
            continue
        bid_eur = row.get("ob_best_bid_eur")
        ask_eur = row.get("ob_best_ask_eur")
        bid_mw = row.get("ob_best_bid_mw")
        ask_mw = row.get("ob_best_ask_mw")
        rec = {}
        if bid_eur is not None and not (isinstance(bid_eur, float) and np.isnan(bid_eur)):
            rec["bid_eur"] = float(bid_eur)
            rec["bid_mw"] = float(bid_mw or 0.0)
        if ask_eur is not None and not (isinstance(ask_eur, float) and np.isnan(ask_eur)):
            rec["ask_eur"] = float(ask_eur)
            rec["ask_mw"] = float(ask_mw or 0.0)
        if rec:
            out[si] = rec
    return out


def _compute_soc_path(start_soc_pct: float,
                       dam_commits: List[float],
                       extras_by_slot: Dict[int, Tuple[str, float]],
                       batt_kwh: float,
                       eff_c: float, eff_d: float,
                       soc_min_pct: float, soc_max_pct: float
                       ) -> Tuple[List[float], List[float], bool]:
    """Sekvenčne vypočíta SOC pred a po každom slote.

    extras_by_slot: {slot_idx: ("BUY"|"SELL", kwh)}
       BUY = nabíjanie batérie (kupujem zo siete navyše)
       SELL = vybíjanie batérie (predávam navyše)

    Returns (soc_before, soc_after, feasible).
    """
    soc_kwh = (start_soc_pct / 100.0) * batt_kwh
    soc_min_kwh = (soc_min_pct / 100.0) * batt_kwh
    soc_max_kwh = (soc_max_pct / 100.0) * batt_kwh
    soc_before: List[float] = []
    soc_after: List[float] = []
    feasible = True

    for t in range(96):
        soc_before.append(soc_kwh / batt_kwh * 100.0)
        # DAM nominácia (batt-basis): kladné=discharge, záporné=charge
        dam = dam_commits[t]
        if dam > 0:
            soc_kwh -= dam / eff_d
        elif dam < 0:
            soc_kwh += abs(dam) * eff_c
        # Extra obchod
        extra = extras_by_slot.get(t)
        if extra:
            direction, kwh = extra
            if direction == "BUY":
                soc_kwh += kwh * eff_c
            elif direction == "SELL":
                soc_kwh -= kwh / eff_d
        if soc_kwh < soc_min_kwh - 1e-3 or soc_kwh > soc_max_kwh + 1e-3:
            feasible = False
        soc_kwh = max(0.0, min(batt_kwh, soc_kwh))
        soc_after.append(soc_kwh / batt_kwh * 100.0)

    return soc_before, soc_after, feasible


# ---------------------------------------------------------------------------
# Greedy návrh
# ---------------------------------------------------------------------------

def _load_load_per_slot(profile: Optional[str], date_iso: str) -> Dict[int, float]:
    """Vráti spotrebu (load) per 15-min slot v kWh.

    Z load_profile modulu — weekday/weekend pattern alebo exact-match.
    Vstup kW per slot × 0.25 h = kWh per slot.
    """
    try:
        import load_profile as _lp
        arr = _lp.load_for_date(date_iso, profile=profile)
        if arr is None or len(arr) == 0:
            return {}
        out = {}
        for i, kw in enumerate(arr[:96]):
            try:
                out[i] = float(kw or 0.0) * 0.25   # kW → kWh per 15min
            except Exception:
                out[i] = 0.0
        return out
    except Exception:
        return {}


def _load_ftv_per_slot(profile: Optional[str], date_iso: str) -> Dict[int, float]:
    """Načíta FTV plán per 15-min slot (kWh) z plan_store pre daný profil/deň.

    Returns dict {slot_idx: pv_kwh}. Prázdny dict ak plán neexistuje.
    """
    try:
        import plan_store as _ps
    except Exception:
        return {}
    try:
        # Preferuj 15-min (dentrh), fallback na 60-min (plan)
        plan = _ps.load_plan_safe(date_iso, step_min=15, kind="dentrh", profile=profile)
        if plan is None:
            plan = _ps.load_plan_safe(date_iso, step_min=60, kind="plan", profile=profile)
        if plan is None:
            return {}
        sched = plan.get("schedule") or {}
        if not isinstance(sched, dict) or "pv_kwh" not in sched:
            return {}
        arr = sched["pv_kwh"]
        step_min = int(plan.get("step_min", 15))
        out = {}
        if step_min == 60:
            # 24 hodín → rozšír na 96 slotov: každá hodina /4 (rovnomerné)
            for h, val in enumerate(arr[:24]):
                try:
                    v = float(val or 0) / 4.0
                except Exception:
                    v = 0.0
                for q in range(4):
                    out[h * 4 + q] = v
        else:
            for i, val in enumerate(arr[:96]):
                try:
                    out[i] = float(val or 0)
                except Exception:
                    out[i] = 0.0
        return out
    except Exception:
        return {}


def propose_greedy(orderbook_df,
                    dam_commits: Any,
                    dam_clearing: Any,
                    profile_cfg: Dict[str, Any],
                    start_soc_pct: float,
                    *,
                    threshold_eur_mwh: float = 5.0,
                    max_per_slot_kwh: Optional[float] = None,
                    grid_fee: float = 22.0,
                    now: Optional[dt.datetime] = None,
                    profile: Optional[str] = None,
                    date_iso: Optional[str] = None) -> Dict[str, Any]:
    """Greedy per-slot návrh extra obchodov.

    Logika per slot:
      - Ak Bid > DAM_clearing + threshold + fee → SELL navyše (vybíjanie)
        profit = (Bid - DAM_clearing - fee) * kwh / 1000
      - Ak Ask < DAM_clearing - threshold - fee → BUY navyše (nabíjanie)
        profit = (DAM_clearing - Ask - fee) * kwh / 1000

    Constraint:
      - kwh ≤ min(orderbook_mw*1000*dt, batt_kw*dt, max_per_slot_kwh)
      - SOC headroom (BUY potrebuje miesto, SELL potrebuje energiu)
      - Iba sloty v budúcnosti (start > now)
    """
    now = now or dt.datetime.now()
    batt_kw = float(profile_cfg.get("batt_kw", 500.0))
    batt_kwh = float(profile_cfg.get("batt_kwh", 800.0))
    # Bug VDT-SOC-RANGE (2026-06-11): rozsah batérie z profilu (soc_min/soc_max,
    # štandardne 5-100) — kľúče soc_min_pct/soc_max_pct v profile neexistujú.
    soc_min = float((profile_cfg.get("soc_min") if profile_cfg.get("soc_min") is not None
                     else profile_cfg.get("soc_min_pct", 5.0)) or 5.0)
    soc_max = float((profile_cfg.get("soc_max") if profile_cfg.get("soc_max") is not None
                     else profile_cfg.get("soc_max_pct", 100.0)) or 100.0)
    eff_c = float(profile_cfg.get("eff_c", 0.95))
    eff_d = float(profile_cfg.get("eff_d", 0.95))
    dt_h = 0.25  # 15-min

    dam_commits_arr = _norm_dam_commits(dam_commits)
    dam_clr_map = _norm_dam_clearing(dam_clearing)
    ob = _extract_orderbook_prices(orderbook_df)

    slot_cap_kwh = batt_kw * dt_h
    if max_per_slot_kwh is None:
        max_per_slot_kwh = slot_cap_kwh

    # FTV predikcia + Load (spotreba) per slot
    today = now.date()
    _date_iso = date_iso or today.isoformat()
    ftv_map = _load_ftv_per_slot(profile, _date_iso) if profile else {}
    load_map = _load_load_per_slot(profile, _date_iso) if profile else {}

    # Iteratívne: spočítaj SOC path po každom obchode aby sa kapacita posúvala
    extras: Dict[int, Tuple[str, float]] = {}
    slots_out: List[Dict[str, Any]] = []

    for t in range(96):
        # Iba future
        slot_start = dt.datetime.combine(today,
                                          dt.time(*divmod(t * 15, 60)))
        if slot_start <= now:
            continue
        if t not in ob:
            continue
        dam_clr = dam_clr_map.get(t)
        if dam_clr is None:
            continue
        ob_rec = ob[t]

        # Predbežný SOC pred týmto slotom (s doteraz schválenými extras)
        soc_before_arr, soc_after_arr, _ = _compute_soc_path(
            start_soc_pct, dam_commits_arr, extras,
            batt_kwh, eff_c, eff_d, soc_min, soc_max)
        soc_before = soc_before_arr[t]
        soc_after_baseline = soc_after_arr[t]

        # Voľná kapacita pre BUY (nabiť navyše)
        headroom_kwh = max(0.0, (soc_max - soc_after_baseline) / 100.0 * batt_kwh) / eff_c
        # Voľná energia pre SELL
        energy_avail_kwh = max(0.0, (soc_after_baseline - soc_min) / 100.0 * batt_kwh) * eff_d

        proposal = None

        # SELL kandidát
        bid_eur = ob_rec.get("bid_eur")
        bid_mw = ob_rec.get("bid_mw", 0.0)
        if bid_eur is not None:
            margin = bid_eur - dam_clr - grid_fee
            if margin >= threshold_eur_mwh:
                kwh_cap = min(bid_mw * 1000.0 * dt_h, slot_cap_kwh,
                              max_per_slot_kwh, energy_avail_kwh)
                if kwh_cap > 0.5:
                    profit = margin * kwh_cap / 1000.0
                    proposal = {
                        "direction": "SELL", "kwh": round(kwh_cap, 2),
                        "price_eur_mwh": round(bid_eur, 2),
                        "delta_profit_eur": round(profit, 2),
                        "margin_eur_mwh": round(margin, 2),
                    }

        # BUY kandidát
        ask_eur = ob_rec.get("ask_eur")
        ask_mw = ob_rec.get("ask_mw", 0.0)
        if ask_eur is not None:
            margin = dam_clr - ask_eur - grid_fee
            if margin >= threshold_eur_mwh:
                kwh_cap = min(ask_mw * 1000.0 * dt_h, slot_cap_kwh,
                              max_per_slot_kwh, headroom_kwh)
                if kwh_cap > 0.5:
                    profit = margin * kwh_cap / 1000.0
                    buy_prop = {
                        "direction": "BUY", "kwh": round(kwh_cap, 2),
                        "price_eur_mwh": round(ask_eur, 2),
                        "delta_profit_eur": round(profit, 2),
                        "margin_eur_mwh": round(margin, 2),
                    }
                    # Vyber lepší z dvoch (greedy = max profit)
                    if proposal is None or buy_prop["delta_profit_eur"] > proposal["delta_profit_eur"]:
                        proposal = buy_prop

        # LOAD_COVER kandidát — load-aware logika:
        # Ak má slot spotrebu a VDT Ask je výhodný (lacnejší než DAM clearing pre import),
        # navrhuj nákup cez VDT na pokrytie load — vyhne sa importu z DAM za vyššiu cenu.
        load_kwh_slot = load_map.get(t, 0.0)
        if load_kwh_slot > 1.0 and ask_eur is not None:
            # Net potreba: load - FTV (ak FTV pokrýva load, žiadne LOAD_COVER netreba)
            net_load_kwh = load_kwh_slot - ftv_kwh_slot
            if net_load_kwh > 1.0:
                # Margin = saving vs import cez DAM: (DAM clearing - Ask) × kwh
                # Ak Ask + fee < DAM clearing → výhodný VDT nákup pre load
                load_margin = dam_clr - ask_eur - grid_fee
                if load_margin >= threshold_eur_mwh:
                    load_kwh_cap = min(net_load_kwh, ask_mw * 1000.0 * dt_h,
                                       slot_cap_kwh, headroom_kwh)
                    if load_kwh_cap > 0.5:
                        load_saved = load_margin * load_kwh_cap / 1000.0
                        slots_out.append({
                            "slot_idx": t,
                            "slot_label": _slot_label(t),
                            "direction": "LOAD_COVER",
                            "kwh": round(load_kwh_cap, 1),
                            "price_eur_mwh": round(ask_eur, 2),
                            "dam_clearing_eur_mwh": round(dam_clr, 2),
                            "margin_eur_mwh": round(load_margin, 2),
                            "delta_profit_eur": round(load_saved, 2),
                            "soc_before_pct": round(soc_before, 1),
                            "soc_after_pct": round(soc_after_baseline, 1),
                            "status": "LIVE",
                            "age_seconds": 0,
                            "reason": f"Load {net_load_kwh:.1f} kWh — VDT Ask {ask_eur:.1f} vs DAM {dam_clr:.1f}",
                            "load_kwh": round(load_kwh_slot, 1),
                            "ftv_kwh": round(ftv_kwh_slot, 1),
                        })

        # CURTAIL_FTV kandidát — FTV-aware logika:
        # Ak je v tomto slote plánovaná FTV výroba a VDT Bid je tak nízky že predaj by stratil
        # (Bid - grid_fee < 0 alebo < threshold), navrhuj orezanie FTV.
        # "Strata" = ceniť FTV za reálny Bid namiesto plánovanej DAM ceny.
        ftv_kwh_slot = ftv_map.get(t, 0.0)
        if ftv_kwh_slot > 1.0:   # má zmysel iba ak je čo orezať
            # Cena ktorou by sa FTV predala "naivne": skutočná VDT Bid alebo DAM clearing
            # Ak Bid je k dispozícii a nižší než fee → strata
            curtail_kwh = 0.0
            curtail_reason = ""
            curtail_saved = 0.0
            if bid_eur is not None and (bid_eur - grid_fee) < -threshold_eur_mwh:
                # VDT Bid je tak nízky že predaj prináša stratu — odporúčaj curtail
                # Úspora = (-1) × (Bid - grid_fee) × ftv_kwh / 1000  (zápornú stratu konvertuj na úsporu)
                curtail_kwh = ftv_kwh_slot
                curtail_saved = (-1.0) * (bid_eur - grid_fee) * curtail_kwh / 1000.0
                curtail_reason = f"VDT Bid {bid_eur:.1f} − fee {grid_fee:.0f} = {bid_eur - grid_fee:+.1f} €/MWh strata"
            elif dam_clr < grid_fee - threshold_eur_mwh:
                # DAM clearing je nižší než fee — predaj FTV cez DAM by stratil. Curtail je vhodný.
                curtail_kwh = ftv_kwh_slot
                curtail_saved = (-1.0) * (dam_clr - grid_fee) * curtail_kwh / 1000.0
                curtail_reason = f"DAM clearing {dam_clr:.1f} − fee {grid_fee:.0f} = {dam_clr - grid_fee:+.1f} €/MWh strata"

            if curtail_kwh > 1.0 and curtail_saved > 0.05:
                slots_out.append({
                    "slot_idx": t,
                    "slot_label": _slot_label(t),
                    "direction": "CURTAIL_FTV",
                    "kwh": round(curtail_kwh, 1),
                    "price_eur_mwh": round(bid_eur if bid_eur is not None else dam_clr, 2),
                    "dam_clearing_eur_mwh": round(dam_clr, 2),
                    "margin_eur_mwh": round(-grid_fee, 2),   # informačne
                    "delta_profit_eur": round(curtail_saved, 2),
                    "soc_before_pct": round(soc_before, 1),
                    "soc_after_pct": round(soc_before, 1),   # curtail nemení SOC
                    "status": "LIVE",
                    "age_seconds": 0,
                    "reason": curtail_reason,
                    "ftv_kwh": round(ftv_kwh_slot, 1),
                })

        if proposal is None:
            continue

        # Aplikuj extras + prepočítaj SOC po
        extras[t] = (proposal["direction"], proposal["kwh"])
        _, soc_after_arr2, _ = _compute_soc_path(
            start_soc_pct, dam_commits_arr, extras,
            batt_kwh, eff_c, eff_d, soc_min, soc_max)
        soc_after_final = soc_after_arr2[t]

        slots_out.append({
            "slot_idx": t,
            "slot_label": _slot_label(t),
            "direction": proposal["direction"],
            "kwh": proposal["kwh"],
            "price_eur_mwh": proposal["price_eur_mwh"],
            "dam_clearing_eur_mwh": round(dam_clr, 2),
            "margin_eur_mwh": proposal["margin_eur_mwh"],
            "delta_profit_eur": proposal["delta_profit_eur"],
            "soc_before_pct": round(soc_before, 1),
            "soc_after_pct": round(soc_after_final, 1),
            "status": "LIVE",
            "age_seconds": 0,
        })

    total_buy = sum(s["kwh"] for s in slots_out if s["direction"] == "BUY")
    total_sell = sum(s["kwh"] for s in slots_out if s["direction"] == "SELL")
    total_profit = sum(s["delta_profit_eur"] for s in slots_out)
    total_curtail = sum(s["kwh"] for s in slots_out if s["direction"] == "CURTAIL_FTV")
    total_load_cover = sum(s["kwh"] for s in slots_out if s["direction"] == "LOAD_COVER")

    return {
        "method": "greedy",
        "ok": True,
        "slots": slots_out,
        "summary": {
            "n_proposals": len(slots_out),
            "total_delta_profit_eur": round(total_profit, 2),
            "total_buy_kwh": round(total_buy, 1),
            "total_sell_kwh": round(total_sell, 1),
            "total_curtail_ftv_kwh": round(total_curtail, 1),
            "total_load_cover_kwh": round(total_load_cover, 1),
        },
    }


# ---------------------------------------------------------------------------
# LP návrh (re-solve s VDT cenami + DAM ako lower bound)
# ---------------------------------------------------------------------------

def propose_lp(orderbook_df,
                dam_commits: Any,
                dam_clearing: Any,
                profile_cfg: Dict[str, Any],
                start_soc_pct: float,
                *,
                grid_fee: float = 22.0,
                cycle_cost: float = 2.0,
                threshold_eur_mwh: float = 5.0,
                now: Optional[dt.datetime] = None,
                profile: Optional[str] = None,
                date_iso: Optional[str] = None) -> Dict[str, Any]:
    """LP re-solve: volá vdt_optimizer.optimize_vdt_day s DAM ako commitments
    + iba budúce sloty s živým orderbook-om.

    Z výsledku odpočíta DAM nomináciu — to čo ostane = extra obchod nad DAM.
    """
    try:
        import vdt_optimizer as _opt
        import vdt_arbitrage as _arb
    except Exception as e:
        return {"method": "lp", "ok": False, "error": f"import zlyhal: {e}",
                "slots": [], "summary": {}}

    now = now or dt.datetime.now()
    dam_commits_arr = _norm_dam_commits(dam_commits)
    dam_clr_map = _norm_dam_clearing(dam_clearing)
    ob = _extract_orderbook_prices(orderbook_df)
    # Bug VDT-SOC-RANGE (2026-06-11): rozsah batérie z profilu (default 5-100)
    soc_min = float((profile_cfg.get("soc_min") if profile_cfg.get("soc_min") is not None
                     else profile_cfg.get("soc_min_pct", 5.0)) or 5.0)
    soc_max = float((profile_cfg.get("soc_max") if profile_cfg.get("soc_max") is not None
                     else profile_cfg.get("soc_max_pct", 100.0)) or 100.0)

    if orderbook_df is None or len(orderbook_df) == 0:
        return {"method": "lp", "ok": False, "error": "prázdny orderbook",
                "slots": [], "summary": {}}

    # Doplň chýbajúce stĺpce ktoré vdt_optimizer očakáva (start_local, period, price_eur).
    # Tým je propose_lp() robustný aj keď dostane mini DF z orderbook_per_slot.
    df_lp = orderbook_df.copy()
    today = now.date()
    if "start_local" not in df_lp.columns:
        starts = []
        periods = []
        for _, _row in df_lp.iterrows():
            try:
                _si = int(_row.get("slot_idx", -1))
            except Exception:
                _si = -1
            if 0 <= _si < 96:
                _h, _m = divmod(_si * 15, 60)
                _h2, _m2 = divmod((_si + 1) * 15, 60)
                starts.append(dt.datetime.combine(today, dt.time(_h, _m)))
                periods.append(f"{_h:02d}:{_m:02d}-{_h2:02d}:{_m2:02d}")
            else:
                starts.append(None)
                periods.append("")
        import pandas as _pd_lp
        df_lp["start_local"] = _pd_lp.to_datetime(starts)
        df_lp["period"] = periods
    if "price_eur" not in df_lp.columns:
        # Fallback price (priemer bid/ask alebo NaN)
        def _avg_price(row):
            b = row.get("ob_best_bid_eur")
            a = row.get("ob_best_ask_eur")
            vals = [float(x) for x in (b, a) if x is not None and not (isinstance(x, float) and np.isnan(x))]
            return sum(vals) / len(vals) if vals else None
        df_lp["price_eur"] = df_lp.apply(_avg_price, axis=1)

    # LP volanie
    try:
        res = _opt.optimize_vdt_day(
            df_lp,
            batt_kw=float(profile_cfg.get("batt_kw", 500.0)),
            batt_kwh=float(profile_cfg.get("batt_kwh", 800.0)),
            eff_c=float(profile_cfg.get("eff_c", 0.95)),
            eff_d=float(profile_cfg.get("eff_d", 0.95)),
            grid_fee=grid_fee, cycle_cost=cycle_cost,
            soc_min_pct=soc_min,
            soc_max_pct=soc_max,
            soc_start_pct=float(start_soc_pct),
            soc_end_min_pct=soc_min,
            future_only=True,
            dam_commitments=dam_commits_arr,
        )
    except Exception as e:
        return {"method": "lp", "ok": False, "error": f"LP zlyhal: {e}",
                "slots": [], "summary": {}}

    if not res.get("ok"):
        return {"method": "lp", "ok": False,
                "error": res.get("error", "LP infeasible"),
                "slots": [], "summary": {}}

    trades = res.get("trades") or []
    slots_out: List[Dict[str, Any]] = []
    extras: Dict[int, Tuple[str, float]] = {}

    for row in trades:
        # vdt_optimizer vracia slot_idx ako lokálny 0..n-1 (po reset_index po future-filter).
        # Globálny 0..95 si vyrátame zo start_local timestampu.
        sl_raw = row.get("start_local")
        si = -1
        if sl_raw is not None:
            try:
                _ts = sl_raw if hasattr(sl_raw, "hour") else dt.datetime.fromisoformat(str(sl_raw))
                si = (_ts.hour * 60 + _ts.minute) // 15
            except Exception:
                si = -1
        if si < 0 or si >= 96:
            continue
        slot_start = dt.datetime.combine(now.date(),
                                          dt.time(*divmod(si * 15, 60)))
        if slot_start <= now:
            continue

        # LP výstup: charge_kwh, discharge_kwh (kladné)
        ch = float(row.get("charge_kwh", 0.0) or 0.0)
        di = float(row.get("discharge_kwh", 0.0) or 0.0)
        net_kwh = di - ch  # kladné = export (SELL)

        # DAM nominácia pre slot (batt-basis: kladné=discharge)
        dam_net = dam_commits_arr[si]
        # Extra = LP - DAM
        extra_net = net_kwh - dam_net
        if abs(extra_net) < 0.5:
            continue

        if extra_net > 0:
            direction = "SELL"
            kwh = abs(extra_net)
            ob_rec = ob.get(si, {})
            price = ob_rec.get("bid_eur")
        else:
            direction = "BUY"
            kwh = abs(extra_net)
            ob_rec = ob.get(si, {})
            price = ob_rec.get("ask_eur")

        if price is None:
            continue

        dam_clr = dam_clr_map.get(si)
        if dam_clr is None:
            continue

        # Δ profit oproti DAM = (price - dam_clearing - fee) × kwh / 1000
        if direction == "SELL":
            margin = price - dam_clr - grid_fee
        else:
            margin = dam_clr - price - grid_fee
        delta_profit = margin * kwh / 1000.0

        # Filter: zobrazujeme len LP návrhy ktoré sú VÝHODNEJŠIE než DAM
        # (Δ > 0). LP samotný neoptimalizuje proti DAM clearing, len proti VDT
        # cenám — takže môže navrhnúť SELL pri slote kde VDT bid < DAM clearing
        # (lokálny VDT optimum, ale strata vs DAM benchmark).
        # Tým sa LP/Greedy stávajú porovnateľné — obe ukazujú "extras nad DAM".
        if delta_profit <= 0:
            continue

        extras[si] = (direction, kwh)
        slots_out.append({
            "slot_idx": si,
            "slot_label": _slot_label(si),
            "direction": direction,
            "kwh": round(kwh, 2),
            "price_eur_mwh": round(price, 2),
            "dam_clearing_eur_mwh": round(dam_clr, 2),
            "margin_eur_mwh": round(margin, 2),
            "delta_profit_eur": round(delta_profit, 2),
            "soc_before_pct": round(float(row.get("soc_pct", 0.0)), 1),
            "soc_after_pct": round(float(row.get("soc_pct", 0.0)), 1),  # bude prepísané
            "status": "LIVE",
            "age_seconds": 0,
        })

    # Prepočítaj reálne SOC pred/po (LP soc_pct môže byť ineé základ)
    if slots_out:
        eff_c = float(profile_cfg.get("eff_c", 0.95))
        eff_d = float(profile_cfg.get("eff_d", 0.95))
        batt_kwh = float(profile_cfg.get("batt_kwh", 800.0))
        soc_min = float(profile_cfg.get("soc_min_pct", 5.0))
        soc_max = float(profile_cfg.get("soc_max_pct", 95.0))
        soc_before_arr, soc_after_arr, _ = _compute_soc_path(
            start_soc_pct, dam_commits_arr, extras,
            batt_kwh, eff_c, eff_d, soc_min, soc_max)
        for s in slots_out:
            t = s["slot_idx"]
            s["soc_before_pct"] = round(soc_before_arr[t], 1)
            s["soc_after_pct"] = round(soc_after_arr[t], 1)

    # FTV curtail návrhy — pre future sloty kde VDT Bid < grid_fee
    # (predaj by stratil) a FTV plánovaná > 0
    _today = now.date()
    _date_iso = date_iso or _today.isoformat()
    ftv_map_lp = _load_ftv_per_slot(profile, _date_iso) if profile else {}
    if ftv_map_lp:
        ob = _extract_orderbook_prices(orderbook_df)
        dam_clr_map_lp = _norm_dam_clearing(dam_clearing)
        for t_lp in range(96):
            slot_start = dt.datetime.combine(_today,
                                              dt.time(*divmod(t_lp * 15, 60)))
            if slot_start <= now:
                continue
            ftv_kwh_slot = ftv_map_lp.get(t_lp, 0.0)
            if ftv_kwh_slot <= 1.0:
                continue
            ob_rec = ob.get(t_lp, {})
            bid_eur = ob_rec.get("bid_eur")
            dam_clr = dam_clr_map_lp.get(t_lp)
            curtail_saved = 0.0
            curtail_reason = ""
            if bid_eur is not None and (bid_eur - grid_fee) < -threshold_eur_mwh:
                curtail_saved = (-1.0) * (bid_eur - grid_fee) * ftv_kwh_slot / 1000.0
                curtail_reason = f"VDT Bid {bid_eur:.1f} − fee {grid_fee:.0f} = {bid_eur - grid_fee:+.1f} €/MWh strata"
            elif dam_clr is not None and dam_clr < grid_fee - threshold_eur_mwh:
                curtail_saved = (-1.0) * (dam_clr - grid_fee) * ftv_kwh_slot / 1000.0
                curtail_reason = f"DAM clearing {dam_clr:.1f} − fee {grid_fee:.0f} = {dam_clr - grid_fee:+.1f} €/MWh strata"
            if curtail_saved > 0.05:
                slots_out.append({
                    "slot_idx": t_lp,
                    "slot_label": _slot_label(t_lp),
                    "direction": "CURTAIL_FTV",
                    "kwh": round(ftv_kwh_slot, 1),
                    "price_eur_mwh": round(bid_eur if bid_eur is not None else (dam_clr or 0), 2),
                    "dam_clearing_eur_mwh": round(dam_clr or 0, 2),
                    "margin_eur_mwh": round(-grid_fee, 2),
                    "delta_profit_eur": round(curtail_saved, 2),
                    "soc_before_pct": None,
                    "soc_after_pct": None,
                    "status": "LIVE",
                    "age_seconds": 0,
                    "reason": curtail_reason,
                    "ftv_kwh": round(ftv_kwh_slot, 1),
                })

    total_buy = sum(s["kwh"] for s in slots_out if s["direction"] == "BUY")
    total_sell = sum(s["kwh"] for s in slots_out if s["direction"] == "SELL")
    total_curtail = sum(s["kwh"] for s in slots_out if s["direction"] == "CURTAIL_FTV")
    total_profit = sum(s["delta_profit_eur"] for s in slots_out)

    return {
        "method": "lp",
        "ok": True,
        "slots": slots_out,
        "summary": {
            "n_proposals": len(slots_out),
            "total_delta_profit_eur": round(total_profit, 2),
            "total_buy_kwh": round(total_buy, 1),
            "total_sell_kwh": round(total_sell, 1),
            "total_curtail_ftv_kwh": round(total_curtail, 1),
        },
    }


# ---------------------------------------------------------------------------
# DAM clearing helper
# ---------------------------------------------------------------------------

def load_dam_clearing_for_day(date: dt.date) -> Dict[int, float]:
    """Načíta DAM clearing ceny per 15-min slot pre daný deň.

    Z SK historian CSV (cez seps_sk.load_okte_dt_for_day).
    Returns {slot_idx: eur_mwh}.
    """
    try:
        import seps_sk as _seps
    except Exception:
        return {}
    try:
        dt_map = _seps.load_okte_dt_for_day(date.isoformat()) or {}
    except Exception:
        return {}
    out: Dict[int, float] = {}
    for ts_str, val in dt_map.items():
        try:
            s = str(ts_str)
            h = int(s[11:13])
            m = int(s[14:16])
            idx = (h * 60 + m) // 15
            if 0 <= idx < 96 and val is not None:
                out[idx] = float(val)
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import datetime as _dt
    print("vdt_extras smoke test")

    # Fixture: 4 sloty s orderbook + DAM commits + clearing
    import pandas as _pd
    today = _dt.date.today()
    rows = []
    for i in [40, 41, 56, 57]:
        h, m = divmod(i * 15, 60)
        rows.append({
            "slot_idx": i,
            "start_local": _dt.datetime.combine(today, _dt.time(h, m)),
            "period": f"{h:02d}:{m:02d}",
            "ob_best_bid_eur": 80.0 if i in (40, 41) else 90.0,
            "ob_best_bid_mw": 0.5,
            "ob_best_ask_eur": 30.0 if i in (56, 57) else 50.0,
            "ob_best_ask_mw": 0.3,
            "ob_spread_eur": 10.0,
            "ob_n_bids": 3, "ob_n_asks": 2,
        })
    snap = _pd.DataFrame(rows)

    dam_commits = [0.0] * 96
    dam_commits[40] = 50.0   # plánujem export 50 kWh
    dam_commits[41] = 50.0
    dam_commits[56] = -30.0  # plánujem import 30 kWh
    dam_commits[57] = -30.0

    dam_clearing = {40: 65.0, 41: 65.0, 56: 55.0, 57: 55.0}

    profile_cfg = {
        "batt_kw": 250.0, "batt_kwh": 500.0,
        "soc_min_pct": 10.0, "soc_max_pct": 90.0,
        "eff_c": 0.95, "eff_d": 0.95,
    }

    # Greedy
    res_g = propose_greedy(snap, dam_commits, dam_clearing, profile_cfg,
                            start_soc_pct=50.0, threshold_eur_mwh=5.0)
    print(f"\n[GREEDY] {res_g['summary']}")
    for s in res_g["slots"]:
        print(f"  {s['slot_label']} {s['direction']} {s['kwh']:.1f} kWh "
              f"@ {s['price_eur_mwh']:.2f} €/MWh → +{s['delta_profit_eur']:.2f} € "
              f"(SOC {s['soc_before_pct']:.0f}→{s['soc_after_pct']:.0f}%)")

    # LP
    res_lp = propose_lp(snap, dam_commits, dam_clearing, profile_cfg,
                         start_soc_pct=50.0)
    print(f"\n[LP] ok={res_lp['ok']} {res_lp.get('summary', {})}")
    if not res_lp["ok"]:
        print(f"  error: {res_lp.get('error')}")
    for s in res_lp["slots"]:
        print(f"  {s['slot_label']} {s['direction']} {s['kwh']:.1f} kWh "
              f"@ {s['price_eur_mwh']:.2f} €/MWh → +{s['delta_profit_eur']:.2f} €")
