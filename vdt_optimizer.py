# -*- coding: utf-8 -*-
"""
vdt_optimizer.py — LP optimalizácia denného obchodovania na VDT.

Cieľ: maximalizovať denný profit z arbitráže s batériou pri obmedzeniach
fyziky batérie a likvidity orderbook.

Premenné per slot t (15-min):
  charge[t]    — kWh ktoré vezmeme zo siete a uložíme do batérie (po stratách)
  discharge[t] — kWh ktoré vyberieme z batérie a predáme na sieť (po stratách)

Constraints:
  SOC dynamika:
    SOC[t+1] = SOC[t] + η_c · charge[t] − discharge[t] / η_d

  SOC limity:
    SOC_min · batt_kwh ≤ SOC[t] ≤ SOC_max · batt_kwh

  Výkon batérie:
    0 ≤ charge[t] ≤ batt_kw · dt
    0 ≤ discharge[t] ≤ batt_kw · dt

  Likvidita orderbook:
    charge[t] ≤ ask_mw[t] · 1000 · dt   (nemôžem kúpiť viac než je v knihe)
    discharge[t] ≤ bid_mw[t] · 1000 · dt (nemôžem predať viac než je dopyt)

  Cyklický koniec (voliteľné):
    SOC[T] ≥ SOC_end_min · batt_kwh

Cieľ (€):
  Σ ( bid[t]·discharge[t]/1000  −  ask[t]·charge[t]/1000
      − fee·(charge[t]+discharge[t])/1000
      − cycle_cost·(charge[t]+discharge[t])/1000/2 )

Vstup: snapshot DataFrame z vdt_arbitrage.get_market_snapshot + add_orderbook
       (riadky musia mať start_local, ob_best_bid_eur, ob_best_ask_eur,
        ob_best_bid_mw, ob_best_ask_mw, period)

Read-only: tento modul iba počíta, neposiela žiadne objednávky.
"""
from __future__ import annotations
import datetime as dt
from typing import Dict, Any, List, Optional, Tuple
import pandas as pd
import numpy as np


def _build_vdt_result(df, charges, discharges, *, n, soc_start_kwh, soc_start_pct,
                      batt_kwh, eff_c, eff_d, buy_price_arr, sell_price_arr,
                      grid_fee, cycle_cost, dam_dis, dam_chg, lp_status="pairs"):
    """Postaví výsledok (trades + summary) z grid-side charges/discharges polí.
    Zdieľané s pairs-engine. POPLATOK LEN NA NABÍJANIE (user 2026-06-20: distribučný
    poplatok = import-only; zhodné s realizovanou ekonomikou effect_db)."""
    trades = []
    soc_trajectory = []
    soc_kwh = soc_start_kwh
    total_charged = total_discharged = 0.0
    revenue = cost = fees = cycle_cost_total = 0.0
    n_ch_slots = n_d_slots = 0
    soc_min_observed = soc_max_observed = soc_kwh
    for t in range(n):
        c_kwh = float(charges[t]); d_kwh = float(discharges[t])
        if c_kwh < 0.01: c_kwh = 0.0
        if d_kwh < 0.01: d_kwh = 0.0
        soc_kwh = soc_kwh + eff_c * c_kwh - d_kwh / eff_d
        soc_trajectory.append(soc_kwh)
        soc_min_observed = min(soc_min_observed, soc_kwh)
        soc_max_observed = max(soc_max_observed, soc_kwh)
        if c_kwh > 0 and d_kwh > 0:
            action = "both"
        elif c_kwh > 0:
            action = "charge"; n_ch_slots += 1; total_charged += c_kwh
            cost += buy_price_arr[t] * c_kwh / 1000.0
            fees += grid_fee * c_kwh / 1000.0                 # poplatok LEN na nabíjaní
            cycle_cost_total += cycle_cost * c_kwh / 1000.0 / 2.0
        elif d_kwh > 0:
            action = "discharge"; n_d_slots += 1; total_discharged += d_kwh
            revenue += sell_price_arr[t] * d_kwh / 1000.0
            # žiadny grid_fee na vybíjaní/exporte
            cycle_cost_total += cycle_cost * d_kwh / 1000.0 / 2.0
        else:
            action = "idle"
        row = df.iloc[t]
        trades.append({
            "slot_idx": t, "slot": row["period"], "start_local": row["start_local"],
            "action": action, "charge_kwh": c_kwh, "discharge_kwh": d_kwh,
            "buy_price_eur_mwh": float(buy_price_arr[t]) if c_kwh > 0 else None,
            "sell_price_eur_mwh": float(sell_price_arr[t]) if d_kwh > 0 else None,
            "soc_after_kwh": soc_kwh,
            "soc_after_pct": soc_kwh / batt_kwh * 100.0 if batt_kwh > 0 else 0,
        })
    profit_eur = revenue - cost - fees - cycle_cost_total
    cycles = total_discharged / batt_kwh if batt_kwh > 0 else 0.0
    dam_dis_total = float(np.sum(dam_dis)); dam_chg_total = float(np.sum(dam_chg))
    return {
        "ok": True, "n_slots": n, "profit_eur": profit_eur, "trades": trades,
        "soc_trajectory": soc_trajectory,
        "dam_committed_export_kwh": dam_dis_total,
        "dam_committed_import_kwh": dam_chg_total,
        "vdt_extra_discharge_kwh": max(0.0, total_discharged - dam_dis_total),
        "vdt_extra_charge_kwh": max(0.0, total_charged - dam_chg_total),
        "summary": {
            "total_charged_kwh": total_charged, "total_discharged_kwh": total_discharged,
            "n_charge_slots": n_ch_slots, "n_discharge_slots": n_d_slots, "cycles": cycles,
            "soc_min_kwh": soc_min_observed, "soc_max_kwh": soc_max_observed,
            "soc_min_pct": soc_min_observed / batt_kwh * 100.0 if batt_kwh > 0 else 0,
            "soc_max_pct": soc_max_observed / batt_kwh * 100.0 if batt_kwh > 0 else 0,
            "soc_start_pct": soc_start_pct,
            "soc_end_pct": soc_kwh / batt_kwh * 100.0 if batt_kwh > 0 else 0,
            "revenue_eur": revenue, "cost_eur": cost, "fees_eur": fees,
            "cycle_cost_eur": cycle_cost_total, "lp_status": lp_status,
        },
    }


def optimize_vdt_day(snapshot: pd.DataFrame, *,
                       batt_kw: float = 500.0,
                       batt_kwh: float = 800.0,
                       eff_c: float = 0.95,
                       eff_d: float = 0.95,
                       grid_fee: float = 22.0,
                       cycle_cost: float = 2.0,
                       min_spread: float = 0.0,
                       soc_min_pct: float = 5.0,
                       soc_max_pct: float = 95.0,
                       soc_start_pct: float = 20.0,
                       soc_end_min_pct: Optional[float] = 20.0,
                       max_cycles_per_day: Optional[float] = None,
                       slot_minutes: int = 15,
                       use_orderbook: bool = True,
                       future_only: bool = True,
                       dam_commitments: Optional[list] = None,
                       soc_neutral: bool = True,
                       soc_neutral_tol_pct: float = 1.0,
                       residual_cost_basis_eur: Optional[float] = None,
                       engine: str = "lp",
                       pair_priority: str = "closest") -> Dict[str, Any]:
    """LP optimalizácia denného obchodovania.

    Args:
        snapshot: DataFrame z vdt_arbitrage.get_market_snapshot s pridanými
                  orderbook stĺpcami (ob_best_bid_eur, ob_best_ask_eur, ...).
        batt_kw: max výkon batérie [kW].
        batt_kwh: užitočná kapacita batérie [kWh].
        eff_c, eff_d: účinnosť nabíjania/vybíjania.
        grid_fee: poplatok za sieť [€/MWh].
        cycle_cost: amortizácia batérie [€/MWh prebehnutej energie].
        min_spread: minimum profit/MWh aby sa pár oplatil (gate).
        soc_min/max_pct: SOC limity (% z batt_kwh).
        soc_start_pct: počiatočný SOC [% z batt_kwh].
        soc_end_min_pct: minimum SOC na konci dňa (None = bez constraint).
        slot_minutes: dĺžka slotu (15 alebo 60).
        use_orderbook: True = obmedzuje MW podľa orderbook bid/ask.
        future_only: True = zahŕňa iba sloty od now vpred.

    Returns:
        dict {
            "ok": bool,
            "n_slots": int,
            "profit_eur": float,
            "trades": [{"slot": "HH:MM-HH:MM", "action": "charge"/"discharge"/"idle",
                        "kwh": float, "price_eur_mwh": float, "soc_after_kwh": float,
                        "soc_after_pct": float}, ...],
            "soc_trajectory": [float, ...],   # SOC v kWh pre každý slot end
            "summary": {
                "total_charged_kwh": float,
                "total_discharged_kwh": float,
                "n_charge_slots": int, "n_discharge_slots": int,
                "cycles": float,
                "soc_min": float, "soc_max": float,
                "revenue_eur": float, "cost_eur": float,
                "fees_eur": float, "cycle_cost_eur": float,
            },
            "error": str (iba ak ok=False),
        }
    """
    from scipy.optimize import linprog

    if snapshot is None or snapshot.empty:
        return {"ok": False, "error": "Prázdny snapshot", "trades": []}

    dt_h = slot_minutes / 60.0
    df = snapshot.copy().reset_index(drop=True)

    # Filter future-only
    if future_only:
        now = pd.Timestamp.now()
        df = df[df["start_local"] >= now.floor(f"{slot_minutes}min")].copy().reset_index(drop=True)

    n = len(df)
    if n == 0:
        return {"ok": False, "error": "Žiadne budúce sloty v snapshote", "trades": []}

    # Per-slot ceny (€/MWh) a MW limity z orderbooku alebo z DAM/IDM fallback
    if use_orderbook:
        buy_price = df["ob_best_ask_eur"].values   # za toľko môžem kúpiť
        sell_price = df["ob_best_bid_eur"].values  # za toľko môžem predať
        max_buy_mw = df["ob_best_ask_mw"].fillna(0).values
        max_sell_mw = df["ob_best_bid_mw"].fillna(0).values
    else:
        # Fallback: použiť clearing price (DAM/IDM) ako oboje (bez bid/ask spread)
        buy_price = df["price_eur"].values
        sell_price = df["price_eur"].values
        max_buy_mw = np.full(n, batt_kw / 1000.0)
        max_sell_mw = np.full(n, batt_kw / 1000.0)

    # Identifikuj sloty kde nemáme cenu (NaN) — vyradíme z obchodu (price 0, mw 0)
    # Bug VDT-ZERO-PRICE (2026-06-17, user: "množstvo na VDT za 0,0 neakceptujem;
    # záporné áno, to je bežná prax"): cena presne 0 = placeholder/chýbajúca (nie
    # reálny trh) → vyradiť z DOBROVOĽNÉHO VDT. Záporné ceny sú PLATNÉ (prebytok na
    # trhu). DAM commitment prejde aj tak (nižšie buy_valid|dam_chg>0, cena cez
    # dam_clearing fallback). Zarovnané s loggerom (scheduler `if _px == 0: continue`).
    buy_valid = np.array([(p is not None and not pd.isna(p) and float(p) != 0.0) for p in buy_price])
    sell_valid = np.array([(p is not None and not pd.isna(p) and float(p) != 0.0) for p in sell_price])
    # DAM clearing cena ako fallback ak orderbook nemá ask/bid (forced DAM trade).
    # Bez tohto fallbacku by LP videl trade za 0 €/MWh (nesprávne free profit) a
    # render by ukázal "0.00 €/MWh" čo zavádza užívateľa.
    # #30-B (user 2026-06-18: "len reálne BID/ASK, žiadny forecast"): tento fallback NIE je
    # forecast-leak. Forecast/chýbajúce ceny → price_eur=NaN → fillna(0.0) → dam_clearing=0
    # → VDT-ZERO-PRICE (nižšie buy_valid/sell_valid: float(p)!=0.0) ho VYRADÍ → netradeable.
    # Reálny DAM (>0) sa použije len pre DNEŠNÉ záväzky (DAM dnes publikované). Zajtrajšie
    # forecast záväzky sú vynulované v advisore (#30-A). VOĽNÝ obchod = LEN reálny order-book.
    if "price_eur" in df.columns:
        dam_clearing = pd.to_numeric(df["price_eur"], errors="coerce").fillna(0.0).values
    else:
        dam_clearing = np.zeros(n, dtype=float)
    buy_price_arr = np.array([float(p) if v else float(dc)
                                for p, v, dc in zip(buy_price, buy_valid, dam_clearing)])
    sell_price_arr = np.array([float(p) if v else float(dc)
                                 for p, v, dc in zip(sell_price, sell_valid, dam_clearing)])

    # Konvertuj MW limit → kWh per slot
    max_buy_kwh = np.minimum(np.array(max_buy_mw, dtype=float) * 1000.0 * dt_h,
                              batt_kw * dt_h)
    max_sell_kwh = np.minimum(np.array(max_sell_mw, dtype=float) * 1000.0 * dt_h,
                                batt_kw * dt_h)
    # Vyradiť sloty bez ceny
    max_buy_kwh = np.where(buy_valid, max_buy_kwh, 0.0)
    max_sell_kwh = np.where(sell_valid, max_sell_kwh, 0.0)

    # DAM commitments — lower bounds na charge/discharge.
    # Konvencia: dam_commitments[t] = ex_kwh − im_kwh per slot
    #   > 0 → batt musí vybiť (predaj cez DAM) — discharge[t] ≥ |value|
    #   < 0 → batt musí nabiť (nákup cez DAM) — charge[t] ≥ |value|
    # Lower bounds prepíšu max_*_kwh ak je DAM commitment > VDT likvidita
    # (DAM kontrakt je už zazmluvnený, nesúvisí s aktuálnym orderbookom).
    dam_dis = np.zeros(n, dtype=float)   # kWh ktoré batt musí vybiť per slot
    dam_chg = np.zeros(n, dtype=float)   # kWh ktoré batt musí nabiť per slot
    if dam_commitments is not None:
        for t in range(min(n, len(dam_commitments))):
            v = float(dam_commitments[t]) if dam_commitments[t] is not None else 0.0
            if v > 0:
                dam_dis[t] = v
                # Discharge bound musí dovoliť aspoň DAM commitment
                max_sell_kwh[t] = max(max_sell_kwh[t], v)
            elif v < 0:
                dam_chg[t] = -v
                max_buy_kwh[t] = max(max_buy_kwh[t], -v)
        # Aj keď nemáme cenu (np. orderbook chýba pre niektorý slot),
        # DAM časť musí prejsť. Označíme tieto sloty ako valid (buy_price_arr
        # už používa DAM clearing fallback pre invalid sloty — viď vyššie).
        buy_valid = buy_valid | (dam_chg > 0)
        sell_valid = sell_valid | (dam_dis > 0)

    # SOC limity — auto-adjust ak start je mimo [min, max]
    # (napr. real SOC = 100% ale soc_max=95% → infeasible bez zarovnania)
    eff_soc_min_pct = min(soc_min_pct, soc_start_pct)
    eff_soc_max_pct = max(soc_max_pct, soc_start_pct)
    soc_min_kwh = batt_kwh * (eff_soc_min_pct / 100.0)
    soc_max_kwh = batt_kwh * (eff_soc_max_pct / 100.0)
    soc_start_kwh = batt_kwh * (soc_start_pct / 100.0)
    soc_end_min_kwh = (batt_kwh * (soc_end_min_pct / 100.0)
                        if soc_end_min_pct is not None else None)
    # Ak end_min > start_soc, LP musí nabíjať aby skončil ≥ end_min,
    # ale ak je dosť času + drahé sloty, ide to. Necháme.
    # VDT-RESIDUAL-SELLOFF: pri výpredaji rezidua povolíme skončiť až na soc_min
    # (inak by koncový floor blokoval predaj nabitej energie vo večeri).
    if residual_cost_basis_eur is not None:
        soc_end_min_kwh = soc_min_kwh

    # ── ENGINE: PÁROVÝ MATCHER (vdt.engine="pairs") ─────────────────────────
    # Alternatíva k LP: greedy párové cykly (nákup↔predaj, spread, oba smery, priorita
    # closest/profit/balanced). DAM = base (posvätný), per-slot stropy z orderbook likvidity,
    # poplatok len na nabíjaní. Early-return → LP cesta (golden) ostáva NEDOTKNUTÁ.
    if str(engine).lower() == "pairs":
        try:
            from vdt_pair_matcher import match_pairs as _mp
        except Exception as _e_imp:
            return {"ok": False, "error": f"pair matcher import zlyhal: {_e_imp}", "trades": []}
        _base = [float(eff_c * dam_chg[t] - dam_dis[t] / eff_d) for t in range(n)]   # SOC-kWh
        _cap_chg = [float(max_buy_kwh[t] * eff_c) for t in range(n)]                  # SOC-kWh
        _cap_dis = [float(max_sell_kwh[t] / eff_d) for t in range(n)]                 # SOC-kWh
        _mr = _mp(
            [float(x) for x in buy_price_arr], [float(x) for x in sell_price_arr],
            soc0_kwh=float(soc_start_kwh), soc_lo_kwh=float(soc_min_kwh), soc_hi_kwh=float(soc_max_kwh),
            batt_kwh_per_slot=float(batt_kw) * dt_h,
            eff_c=float(eff_c), eff_d=float(eff_d), cycle_cost=float(cycle_cost),
            grid_fee=float(grid_fee), min_spread=float(min_spread), priority=str(pair_priority),
            base_soc_delta=_base, cap_charge_soc=_cap_chg, cap_discharge_soc=_cap_dis,
        )
        _vdt = _mr["vdt_soc_delta"]
        # grid-side = DAM + VDT (SOC→grid: nabíjanie /eff_c import, vybíjanie ×eff_d export)
        charges = np.array([float(dam_chg[t]) + (max(_vdt[t], 0.0) / eff_c) for t in range(n)])
        discharges = np.array([float(dam_dis[t]) + (max(-_vdt[t], 0.0) * eff_d) for t in range(n)])
        return _build_vdt_result(
            df, charges, discharges, n=n, soc_start_kwh=soc_start_kwh, soc_start_pct=soc_start_pct,
            batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
            buy_price_arr=buy_price_arr, sell_price_arr=sell_price_arr,
            grid_fee=grid_fee, cycle_cost=cycle_cost, dam_dis=dam_dis, dam_chg=dam_chg,
            lp_status=f"pairs:{pair_priority} ({len(_mr['cycles'])} cyklov)")

    # ──────────────────────────────────────────────────────────────────────
    # LP formulácia
    #   x = [c_0, d_0, c_1, d_1, ..., c_{n-1}, d_{n-1}]   (2n premenných)
    # Cieľ (minimalizujeme zápornú profit):
    #   profit[t] = sell·d/1000 − buy·c/1000 − fee·(c+d)/1000 − cycle·(c+d)/1000/2
    #             = (sell/1000 − fee/1000 − cycle/2000) · d
    #             − (buy/1000 + fee/1000 + cycle/2000) · c
    # ──────────────────────────────────────────────────────────────────────
    # Bug VDT-HARD-SPREAD (2026-06-12, user: "obchody by sa mali uzatvárať vždy
    # so ziskom, min spread je v pláne — ako môže prerobiť?"): min_spread je teraz
    # TVRDÝ gate zapečený do LP účelovej funkcie, nie len post-hoc filter. Pridáme
    # min_spread/2 ako extra prekážku na KAŽDÚ nohu → dobrovoľný round-trip sa
    # oplatí LP iba ak sell − buy ≥ 2·fee + cycle + min_spread. Vynútené DAM
    # commitments (lower bounds nižšie) prejdú tak či tak — gate platí len pre
    # VDT obchody NAD rámec záväzku (presne to, čo prerábalo: koncové vybitia
    # a páry tesne nad nulou). Predaj DAM-energie ostáva povolený (lacný buyback
    # neskôr), ale len keď spread prekročí prah — to zabráni stratovým re-tradom.
    # VDT-HARD-SPREAD (2026-06-12): min_spread ako TVRDÝ gate v účelovej funkcii.
    # Prah min_spread/2 na každú nohu → dobrovoľný round-trip sa oplatí iba ak
    # sell − buy ≥ 2·fee + cycle + min_spread. Cross-slot arbitráž (kúp v lacnom
    # slote, predaj v drahom) funguje normálne. Dumping uskladnenej DAM-energie
    # bez kúpy späť rieši SOC-neutralita nižšie (VDT-SOC-NEUTRAL).
    # Bug VDT-GATE-DOUBLECOUNT (2026-06-13, user: "zrazu žiadne VDT obchody"):
    # objektív UŽ obsahuje 2·fee + cycle. Keď je vdt_breakeven_auto ON, min_spread
    # = efektivita + 2·fee + cycle (≈63 €/MWh pri DT 130) → prah min_spread/2 na
    # nohu PRIDAL fee+cycle DRUHÝ raz → požadovaný spread ~112 €/MWh → 0 obchodov.
    # Fix: prah = ČISTÁ marža nad rámec toho, čo objektív už účtuje:
    #   pure = max(0, min_spread − 2·fee − cycle). Fixný min_spread=5 → pure=0
    #   (fee+cycle v objektíve stačia); breakeven_auto 63 → pure≈14 (efektivita).
    _pure_spread = max(0.0, float(min_spread) - 2.0 * float(grid_fee) - float(cycle_cost))
    _hurdle = _pure_spread / 2000.0
    coeff_d = sell_price_arr / 1000.0 - grid_fee / 1000.0 - cycle_cost / 2000.0 - _hurdle
    coeff_c = -(buy_price_arr / 1000.0 + grid_fee / 1000.0 + cycle_cost / 2000.0 + _hurdle)
    # c[t] na párnych pozíciách, d[t] na nepárnych
    c_obj = np.zeros(2 * n)
    c_obj[0::2] = -coeff_c   # min = -max, but coeff_c je už negative → -coeff_c je positive cost
    c_obj[1::2] = -coeff_d   # min: chceme maximalizovať d → c_obj záporné pre d

    # VDT-RESIDUAL-SELLOFF (2026-06-19, user: "nenechať batériu zbytočne nabitú; predaj večer
    # drahšie ako boli nabíjania"): ak je daná nákladová báza, povolíme ZISKOVÝ výpredaj rezidua
    # (skončiť nižšie). Pridáme cost_basis na NET zmenu SOC: profit += cost_basis·Σ(ηc·c − d/ηd).
    # → discharge dostane náklad cost_basis (predá len ak sell > cost_basis + fee + spread),
    # charge dostane kredit (drží ho jednostranný net-constraint nižšie = žiadny buy-and-hold).
    # Párované VDT round-tripy: buy noha = kredit, sell noha = náklad → netto ~0 (nepenalizované).
    # cost_basis = váž. priemer nabíjacích cien dňa (DAM+RT), dodá volajúci. Lineárne, golden-safe (opt-in).
    _residual_on = residual_cost_basis_eur is not None
    if _residual_on:
        _cb = float(residual_cost_basis_eur) / 1000.0
        c_obj[0::2] += -_cb * eff_c
        c_obj[1::2] += _cb / eff_d

    # Min-spread gate je teraz v účelovej funkcii (viď VDT-HARD-SPREAD vyššie).
    # Post-hoc filter ostáva ako poistka pri klipovaní šumu.

    # Bounds: dam_chg[t] ≤ c[t] ≤ max_buy_kwh[t], dam_dis[t] ≤ d[t] ≤ max_sell_kwh[t]
    # Lower bound = DAM commitment (kontrakt musí prejsť cez batt).
    bounds = []
    for t in range(n):
        # Defensive: ak max < lower (orderbook ne-má likviditu pre DAM), uprav max
        lb_c = float(dam_chg[t])
        ub_c = max(float(max_buy_kwh[t]), lb_c)
        lb_d = float(dam_dis[t])
        ub_d = max(float(max_sell_kwh[t]), lb_d)
        bounds.append((lb_c, ub_c))
        bounds.append((lb_d, ub_d))

    # Constraints: SOC dynamika
    # SOC[t] = soc_start + Σ_{i=0..t-1} (η_c · c[i] − d[i]/η_d)
    # SOC_min ≤ SOC[t+1] ≤ SOC_max pre t = 0..n-1
    # Premenné: 2n, riadky constraint matrix: 2n + 1 (soc_min na konci)
    A_ub = []
    b_ub = []
    for t in range(n):
        # SOC po slote t = soc_start + Σ_{i=0..t} (η_c·c[i] − d[i]/η_d)
        # SOC ≤ soc_max → Σ (η_c·c[i] − d[i]/η_d) ≤ soc_max − soc_start
        row_max = np.zeros(2 * n)
        for i in range(t + 1):
            row_max[2 * i] = eff_c
            row_max[2 * i + 1] = -1.0 / eff_d
        A_ub.append(row_max)
        b_ub.append(soc_max_kwh - soc_start_kwh)

        # SOC ≥ soc_min → −Σ(η_c·c[i] − d[i]/η_d) ≤ soc_start − soc_min
        A_ub.append(-row_max)
        b_ub.append(soc_start_kwh - soc_min_kwh)

    # Koncový SOC ≥ soc_end_min (ak je)
    if soc_end_min_kwh is not None:
        row = np.zeros(2 * n)
        for i in range(n):
            row[2 * i] = -eff_c
            row[2 * i + 1] = 1.0 / eff_d
        A_ub.append(row)
        b_ub.append(soc_start_kwh - soc_end_min_kwh)


    # Max počet cyklov za deň: Σ discharge[t] ≤ max_cycles × batt_kwh
    # Cykly počítame ako sumu vybitej energie / batt_kwh (= 1 cyklus = full discharge).
    if max_cycles_per_day is not None and max_cycles_per_day > 0:
        row = np.zeros(2 * n)
        for i in range(n):
            row[2 * i + 1] = 1.0   # discharge
        A_ub.append(row)
        b_ub.append(float(max_cycles_per_day) * batt_kwh)

    # Bug VDT-SOC-NEUTRAL (2026-06-12, user: "obchody by sa mali uzatvárať vždy so
    # ziskom; predaj DT-energiu a kúp ju späť lacnejšie inokedy"): VDT je OVERLAY
    # nad DAM plánom — jeho čistá zmena SOC za deň musí byť ≈ 0 (nad rámec DAM
    # záväzkov). Bez tohto LP DUMPOVAL uskladnenú DAM-energiu (predaj bez kúpy
    # späť) za hocijakú cenu nad nulou → koncové stratové vybitia (−472 €). S
    # neutralitou je KAŽDÝ predaj spárovaný s kúpou → round-trip → platí min_spread
    # gate. DAM čistá zmena ostáva povolená (kontrakt). PRIDANÉ AKO POSLEDNÉ 2
    # riadky → fallback ich vie odstrániť pri infeasible. Tolerancia ±tol% kapacity.
    if soc_neutral:
        _dam_net = float(np.sum(eff_c * dam_chg - dam_dis / eff_d))
        _tol = max(1.0, float(soc_neutral_tol_pct) / 100.0 * batt_kwh)
        _row_net = np.zeros(2 * n)
        for i in range(n):
            _row_net[2 * i] = eff_c
            _row_net[2 * i + 1] = -1.0 / eff_d
        A_ub.append(_row_net.copy()); b_ub.append(_dam_net + _tol)
        # VDT-RESIDUAL-SELLOFF: pri zapnutom výpredaji rezidua DROPneme dolnú hranicu
        # (net ≥ DAM−tol) → povolíme ČISTÝ PREDAJ pod DAM net (skončiť nižšie). Horná
        # hranica ostáva → žiadny buy-and-hold (nekúpi a nedrží navyše). Ziskovosť riadi
        # cost_basis v účelovej funkcii + min_spread; SOC feasibilitu SOC bounds.
        if not _residual_on:
            A_ub.append(-_row_net); b_ub.append(-(_dam_net - _tol))

    A_ub = np.array(A_ub) if A_ub else None
    b_ub = np.array(b_ub) if b_ub else None

    # Solve
    try:
        res = linprog(
            c_obj,
            A_ub=A_ub, b_ub=b_ub,
            bounds=bounds,
            method="highs",
        )
    except Exception as e:
        return {"ok": False, "error": f"LP solver zlyhal: {e}", "trades": []}

    # VDT-SOC-NEUTRAL: ŽIADNY fallback bez neutrality (2026-06-18, user: "pár musí
    # sedieť aj z pohľadu energie; saldo musí byť 0 na konci, nech sa nestane že kúpi
    # 1000 a predá 100"). Neutralita je VŽDY splniteľná (triviálne riešenie = iba DAM
    # baseline: c=dam_chg, d=dam_dis → net = dam_net), takže infeasible NIKDY
    # nespôsobí samotná neutralita, ale iné limity (SOC/grid/výkon). Pôvodný fallback
    # neutralitu pri infeasible ZAHODIL → LP potom dovolil NEVYVÁŽENÝ dump (kúp 1000 /
    # predaj 100, visiaca pozícia) — to je presne zakázané. Preto fallback rušíme;
    # pri infeasible vrátime chybu a volajúci degraduje na "len DAM, žiadne VDT extra".

    if not res.success:
        return {"ok": False, "error": f"LP nemá riešenie: {res.message}",
                "trades": []}

    x = res.x
    charges = x[0::2]
    discharges = x[1::2]

    # VDT-SOC-NEUTRAL hard guard (2026-06-18): defenzívne over, že čisté VDT saldo je
    # ~0 (čistá zmena SOC = DAM baseline). Ak by solver tol alebo akákoľvek cesta
    # nechala VDT-extra nevyvážené (kúp 1000 / predaj 100), extra ZAHODÍME a necháme
    # len DAM baseline (c=dam_chg, d=dam_dis) → vyvážené. Radšej nič ako visiaca
    # pozícia, ktorú nemáme ako uzavrieť. (Mal by byť no-op, ale je to poistka.)
    if soc_neutral and not _residual_on:
        _net_solved = float(np.sum(eff_c * charges - discharges / eff_d))
        _dam_net_g = float(np.sum(eff_c * dam_chg - dam_dis / eff_d))
        _tol_g = max(1.0, float(soc_neutral_tol_pct) / 100.0 * batt_kwh) * 1.5
        if abs(_net_solved - _dam_net_g) > _tol_g:
            print(f"[VDT-SOC-NEUTRAL] saldo {_net_solved:.1f} vs DAM {_dam_net_g:.1f} kWh "
                  f"mimo tol {_tol_g:.1f} → VDT extra zahodené (len DAM, vyvážené)")
            charges = np.asarray(dam_chg, dtype=float).copy()
            discharges = np.asarray(dam_dis, dtype=float).copy()

    # Build trades + SOC trajectory
    trades = []
    soc_trajectory = []
    soc_kwh = soc_start_kwh
    total_charged = total_discharged = 0.0
    revenue = cost = fees = cycle_cost_total = 0.0
    n_ch_slots = n_d_slots = 0
    soc_min_observed = soc_kwh
    soc_max_observed = soc_kwh

    for t in range(n):
        c_kwh = float(charges[t])
        d_kwh = float(discharges[t])
        # Klipovať šum
        if c_kwh < 0.01: c_kwh = 0.0
        if d_kwh < 0.01: d_kwh = 0.0

        # Min-spread gate — ak slot je nabíjanie/vybíjanie ale celkový profit
        # by bol pod prahom, môžeme tieto sloty preskočiť. Tu len logujeme.

        soc_kwh = soc_kwh + eff_c * c_kwh - d_kwh / eff_d
        soc_trajectory.append(soc_kwh)
        soc_min_observed = min(soc_min_observed, soc_kwh)
        soc_max_observed = max(soc_max_observed, soc_kwh)

        if c_kwh > 0 and d_kwh > 0:
            # Zriedkavé — LP zvyčajne nedovolí súčasne (nemá zmysel)
            action = "both"
        elif c_kwh > 0:
            action = "charge"
            n_ch_slots += 1
            total_charged += c_kwh
            cost += buy_price_arr[t] * c_kwh / 1000.0
            fees += grid_fee * c_kwh / 1000.0
            cycle_cost_total += cycle_cost * c_kwh / 1000.0 / 2.0
        elif d_kwh > 0:
            action = "discharge"
            n_d_slots += 1
            total_discharged += d_kwh
            revenue += sell_price_arr[t] * d_kwh / 1000.0
            fees += grid_fee * d_kwh / 1000.0
            cycle_cost_total += cycle_cost * d_kwh / 1000.0 / 2.0
        else:
            action = "idle"

        row = df.iloc[t]
        trades.append({
            "slot_idx": t,
            "slot": row["period"],
            "start_local": row["start_local"],
            "action": action,
            "charge_kwh": c_kwh,
            "discharge_kwh": d_kwh,
            "buy_price_eur_mwh": float(buy_price_arr[t]) if c_kwh > 0 else None,
            "sell_price_eur_mwh": float(sell_price_arr[t]) if d_kwh > 0 else None,
            "soc_after_kwh": soc_kwh,
            "soc_after_pct": soc_kwh / batt_kwh * 100.0 if batt_kwh > 0 else 0,
        })

    profit_eur = revenue - cost - fees - cycle_cost_total
    cycles = total_discharged / batt_kwh if batt_kwh > 0 else 0.0

    # DAM commitment metriky (ak boli zadané)
    dam_dis_total = float(np.sum(dam_dis))
    dam_chg_total = float(np.sum(dam_chg))
    vdt_extra_dis = max(0.0, total_discharged - dam_dis_total)
    vdt_extra_chg = max(0.0, total_charged - dam_chg_total)

    return {
        "ok": True,
        "n_slots": n,
        "profit_eur": profit_eur,
        "trades": trades,
        "soc_trajectory": soc_trajectory,
        "dam_committed_export_kwh": dam_dis_total,
        "dam_committed_import_kwh": dam_chg_total,
        "vdt_extra_discharge_kwh": vdt_extra_dis,
        "vdt_extra_charge_kwh": vdt_extra_chg,
        "summary": {
            "total_charged_kwh": total_charged,
            "total_discharged_kwh": total_discharged,
            "n_charge_slots": n_ch_slots,
            "n_discharge_slots": n_d_slots,
            "cycles": cycles,
            "soc_min_kwh": soc_min_observed,
            "soc_max_kwh": soc_max_observed,
            "soc_min_pct": soc_min_observed / batt_kwh * 100.0 if batt_kwh > 0 else 0,
            "soc_max_pct": soc_max_observed / batt_kwh * 100.0 if batt_kwh > 0 else 0,
            "soc_start_pct": soc_start_pct,
            "soc_end_pct": soc_kwh / batt_kwh * 100.0 if batt_kwh > 0 else 0,
            "revenue_eur": revenue,
            "cost_eur": cost,
            "fees_eur": fees,
            "cycle_cost_eur": cycle_cost_total,
            "lp_status": res.message,
        },
    }


if __name__ == "__main__":
    # Smoke test: mock snapshot s arbitrážnou príležitosťou
    import datetime as dt
    rows = []
    now = pd.Timestamp.now().floor("15min").to_pydatetime()
    for i in range(20):   # 5 hodín dopredu
        start = now + dt.timedelta(minutes=15 * i)
        end = start + dt.timedelta(minutes=15)
        h = start.hour
        # Cena: cheap rano (h<12), drahé večer (h>=18)
        if h < 14:
            bid, ask = 50.0 + i, 55.0 + i
        elif h < 18:
            bid, ask = 80.0, 85.0
        else:
            bid, ask = 250.0, 260.0
        rows.append({
            "date": start.date().isoformat(),
            "period": f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}",
            "start_local": start, "end_local": end,
            "ob_best_bid_eur": bid, "ob_best_bid_mw": 0.5,
            "ob_best_ask_eur": ask, "ob_best_ask_mw": 0.5,
            "price_eur": (bid + ask) / 2,
        })
    df = pd.DataFrame(rows)
    res = optimize_vdt_day(df, batt_kw=500, batt_kwh=800, soc_start_pct=20,
                              soc_end_min_pct=20, future_only=False)
    print(f"OK: {res['ok']}, profit: {res['profit_eur']:.2f} €")
    s = res["summary"]
    print(f"Charged: {s['total_charged_kwh']:.0f} kWh, Discharged: {s['total_discharged_kwh']:.0f} kWh")
    print(f"Cycles: {s['cycles']:.2f}, SOC min/max: {s['soc_min_pct']:.0f}%/{s['soc_max_pct']:.0f}%")
    print(f"Charge slots: {s['n_charge_slots']}, Discharge slots: {s['n_discharge_slots']}")
    print("Akcie (top 5 charge + 5 discharge):")
    for t in [x for x in res["trades"] if x["action"] != "idle"][:10]:
        print(f"  {t['slot']} {t['action']:9} {t['charge_kwh']:.0f}c {t['discharge_kwh']:.0f}d SOC={t['soc_after_pct']:.0f}%")
