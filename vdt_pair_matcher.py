# -*- coding: utf-8 -*-
"""
vdt_pair_matcher.py — VDT arbitráž ako PÁROVÉ CYKLY (greedy).

Špec (user 2026-06-20): „nájdu sa dva zaujímavé body, jeden nákup a druhý predaj a
realizujú sa v jednom cykle spolu — ak je splnený spread; hľadať čo najbližšie; a toto sa
opakuje pokiaľ je v pohode SOC a výkon batérie. Večerné NEPÁROVÉ predaje rezidua sú OK
(rieši residual selloff). DAM záväzky sú posvätné."

Model jedného cyklu = (charge_slot c, discharge_slot d):
  • nabi S kWh do SOC v slote c  → grid kúpi S/eff_c kWh za buy[c]
  • vybi S kWh zo SOC v slote d  → grid predá S*eff_d kWh za sell[d]
  marža na 1 SOC-kWh (EUR/MWh):  m = eff_d*sell[d] − (buy[c]+grid_fee)/eff_c − cycle_cost
  pár je platný len ak  m ≥ min_spread.
  Smer: c<d = nákup→predaj; d<c = predaj-teraz→spätný-nákup (z existujúcej SOC). Oba povolené.

Feasibilita:
  • SOC (base DAM + už aplikované VDT + tento cyklus) ostane v [soc_lo, soc_hi] vo VŠETKÝCH slotoch
    medzi c a d (cyklus dvíha/znižuje SOC na celom intervale, mimo neho je netto 0).
  • Výkon: batériový prietok per slot (|base| + |VDT|) ≤ batt_kwh_per_slot v c aj d.

Priorita výberu páru (profilová voľba):
  • "closest"  — minimálny |c−d| (najmenej držania), tie-break vyššia marža
  • "profit"   — maximálna marža m, tie-break menší |c−d|
  • "balanced" — maximálna marža/vzdialenosť (m / |c−d|)

DAM sa NEdotýka — vstupuje len ako base_soc_delta (rezervovaná kapacita/výkon per slot).
Konvencia výstupu vdt_soc_delta[i]: + = nabíjanie (SOC ↑), − = vybíjanie (SOC ↓).
"""
from __future__ import annotations
from typing import List, Optional, Dict, Any
import numpy as np

_PRIORITIES = ("closest", "profit", "balanced")


def match_pairs(
    buy_price: List[float],
    sell_price: Optional[List[float]] = None,
    *,
    soc0_kwh: float,
    soc_lo_kwh: float,
    soc_hi_kwh: float,
    batt_kwh_per_slot: float,
    eff_c: float = 0.95,
    eff_d: float = 0.95,
    cycle_cost: float = 0.0,
    grid_fee: float = 0.0,
    min_spread: float = 5.0,
    priority: str = "closest",
    base_soc_delta: Optional[List[float]] = None,
    cap_charge_soc: Optional[List[float]] = None,
    cap_discharge_soc: Optional[List[float]] = None,
    max_cycles: int = 500,
    allow_buyback: bool = True,
) -> Dict[str, Any]:
    """Greedy párový matcher. Ceny v EUR/MWh, energie v kWh.

    Returns dict:
      vdt_soc_delta : list[float]  (+nabíjanie / −vybíjanie, SOC-kWh per slot, LEN VDT extra)
      cycles        : list[dict]   (charge_slot, discharge_slot, soc_kwh, margin_eur_mwh, profit_eur, direction)
      profit_eur    : float        (súčet ziskov párov)
      soc_kwh       : list[float]   (výsledná SOC trajektória vrátane base+VDT, dĺžka N+1)
    """
    n = len(buy_price)
    buy = np.asarray(buy_price, dtype=float)
    sell = np.asarray(sell_price if sell_price is not None else buy_price, dtype=float)
    base = (np.asarray(base_soc_delta, dtype=float) if base_soc_delta is not None
            else np.zeros(n))
    if priority not in _PRIORITIES:
        priority = "closest"

    eff_c = max(1e-3, float(eff_c)); eff_d = max(1e-3, float(eff_d))
    cap_slot = max(0.0, float(batt_kwh_per_slot))
    # Smerové per-slot stropy (SOC-kWh). Default = batt cap. Orderbook likvidita / DAM
    # rezervácia ich môže znížiť. Charge slot limituje cap_chg, discharge slot cap_dis.
    cap_chg = (np.asarray(cap_charge_soc, dtype=float) if cap_charge_soc is not None
               else np.full(n, cap_slot))
    cap_dis = (np.asarray(cap_discharge_soc, dtype=float) if cap_discharge_soc is not None
               else np.full(n, cap_slot))
    cap_chg = np.minimum(cap_chg, cap_slot)
    cap_dis = np.minimum(cap_dis, cap_slot)

    vdt = np.zeros(n)                       # SOC-kWh per slot (len VDT)
    # použitá kapacita per slot a smer (batériový prietok). base DAM rezervuje vopred.
    used_chg = np.maximum(base, 0.0).copy()    # base>0 = nabíjanie
    used_dis = np.maximum(-base, 0.0).copy()   # base<0 = vybíjanie
    _blocked = set()                        # páry (c,d) ktoré sú nefeasibilné (SOC/power)

    def _soc_traj():
        s = np.empty(n + 1); s[0] = float(soc0_kwh)
        for t in range(n):
            s[t + 1] = s[t] + base[t] + vdt[t]
        return s

    # Účtovanie (grid-side kWh): pre 1 SOC-kWh cyklu je grid nákup = 1/eff_c kWh,
    # grid predaj = eff_d kWh. POPLATOK (grid_fee) LEN NA NABÍJANIE/IMPORT (user 2026-06-20:
    # „počítať poplatky len pri nabíjaní"; distribučný poplatok = import-only, zhodné s
    # realizovanou ekonomikou effect_db). cycle_cost = amortizácia batérie na prebehnutú
    # energiu (obe strany /2).
    _thru = (1.0 / eff_c) + eff_d        # grid kWh prebehnutej energie na 1 SOC-kWh
    def _margin(c, d):
        # marža na 1 SOC-kWh (EUR/MWh): predaj v d, nákup v c
        return (eff_d * sell[d] - buy[c] / eff_c
                - grid_fee / eff_c - cycle_cost * _thru / 2.0)

    cycles: List[Dict[str, Any]] = []
    total_profit = 0.0

    for _ in range(int(max_cycles)):
        # 1) vyber najlepší KANDIDÁTSKY pár podľa priority (m ≥ min_spread)
        best = None  # (score, c, d, m)
        for c in range(n):
            if used_chg[c] >= cap_chg[c] - 1e-9:
                continue
            for d in range(n):
                if d == c or used_dis[d] >= cap_dis[d] - 1e-9:
                    continue
                # allow_buyback=False (user 2026-06-27): KAŽDÝ nákup musí mať NESKORŠÍ predaj
                # → povolené len nákup-pred-predajom (c < d). Zakázané „predaj→spätný nákup"
                # (d < c) = koncové nezmyselné nákupy bez neskoršieho predaja.
                if (not allow_buyback) and d <= c:
                    continue
                if (c, d) in _blocked:
                    continue
                m = _margin(c, d)
                if m < min_spread:
                    continue
                dist = abs(c - d)
                if priority == "closest":
                    score = (-dist, m)            # min dist, potom max m
                elif priority == "profit":
                    score = (m, -dist)            # max m, potom min dist
                else:  # balanced
                    score = (m / dist, m)         # max marža/vzdialenosť
                if best is None or score > best[0]:
                    best = (score, c, d, m)
        if best is None:
            break
        _, c, d, m = best

        # 2) max feasibilné S (SOC-kWh) pre tento cyklus
        lo, hi = c, d
        if lo > hi:
            lo, hi = hi, lo
        sign = +1.0 if c < d else -1.0   # c<d: charge najprv (SOC ↑ na intervale); d<c: discharge najprv (SOC ↓)
        soc = _soc_traj()
        # SOC na slotoch (lo+1 .. hi) sa posunie o sign*S (medzi udalosťami c a d)
        # power headroom v c (nabíjanie) a d (vybíjanie)
        s_power = min(cap_chg[c] - used_chg[c], cap_dis[d] - used_dis[d])
        # SOC headroom: pre každý vnútorný bod t∈[lo+1, hi]
        s_soc = float("inf")
        for t in range(lo + 1, hi + 1):
            if sign > 0:   # SOC sa dvíha → strop hi
                s_soc = min(s_soc, soc_hi_kwh - soc[t])
            else:          # SOC klesá → podlaha lo
                s_soc = min(s_soc, soc[t] - soc_lo_kwh)
        S = max(0.0, min(s_power, s_soc))
        if S <= 1e-6:
            # pár je momentálne nefeasibilný (SOC/power) → zablokuj a skús ďalší najlepší
            _blocked.add((c, d))
            continue

        # 3) aplikuj cyklus: charge v c (+S), discharge v d (−S)
        vdt[c] += S
        vdt[d] -= S
        used_chg[c] += S
        used_dis[d] += S
        profit = S * m / 1000.0    # m v EUR/MWh, S v kWh → EUR
        total_profit += profit
        cycles.append({
            "charge_slot": int(c), "discharge_slot": int(d),
            "soc_kwh": float(S), "margin_eur_mwh": float(m),
            "profit_eur": float(profit),
            "direction": "buy_then_sell" if c < d else "sell_then_buyback",
        })

    return {
        "vdt_soc_delta": vdt.tolist(),
        "cycles": cycles,
        "profit_eur": float(total_profit),
        "soc_kwh": _soc_traj().tolist(),
        "priority": priority,
    }
