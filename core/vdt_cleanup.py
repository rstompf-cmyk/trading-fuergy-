# -*- coding: utf-8 -*-
"""core/vdt_cleanup.py — VDT „Upratovanie" (bezpečnostná sieť, 2026-07-02).

ÚČEL: poistka. Normálne VDT + audit NEMAJÚ vytvárať nedodateľné obchody. Ale AK sa
budúci slot stane nedodateľným (realita nedodá nomináciu — SOC/limit), tento modul
skúsi škodu zmenšiť korekčným obchodom. Nie je to ziskový motor — je to loss-avoidance.

KĽÚČ (user 2026-07-02): akceptovateľná marža zásahu KLESÁ podľa času do problému τ:
  • τ ≥ horizont  → vyžaduj čistý zisk (+min_spread)  [je čas počkať, možno zarobíš]
  • τ → 0         → akceptuj stratu až po `max_loss`   [vyhnutá pokuta > malá strata]
  • medzi tým lineárne.

Referencia ceny: cena obchodu ak známa; inak DT (DAM) cena v čase problému. Bez
referencie sa marža nedá určiť → NEZASAHUJ.

BEZPEČNOSŤ (najdôležitejšie): zásah len ZMENŠUJE odchýlku, cap na veľkosť, pod deadband
sa nič nerobí, vždy sa clipuje na feasibility (volajúci dodá max_action_kw z reálneho
SOC/gridu). Modul je ČISTÝ (bez I/O) — testovateľný. Kill-switch rieši volajúci.

RT sa NEDOTÝKA — RT beží ako doteraz; upratuje VDT vrstva.
"""
from __future__ import annotations
from typing import Optional, Dict, Any


def required_margin_eur(tau_h: float, horizon_h: float,
                        min_spread_eur: float, max_loss_eur: float) -> float:
    """Požadovaná marža [€/MWh] podľa času do problému τ (lineárny decay).

    tau_h ≥ horizon_h → +min_spread (len zisk).
    tau_h ≤ 0         → −max_loss   (akceptuj stratu po strop).
    medzi tým lineárne. max_loss ≥ 0 (kladné číslo = koľko straty dovolíme).
    """
    ms = float(min_spread_eur)
    ml = abs(float(max_loss_eur))
    h = float(horizon_h)
    if h <= 0.0:
        return ms
    f = max(0.0, min(1.0, float(tau_h) / h))     # 1.0 ďaleko → 0.0 blízko
    return -ml + f * (ms + ml)


def decide_cleanup(
    deviation_kw: float,
    tau_h: float,
    *,
    direction: str,                       # 'buy' (nabíjanie) | 'sell' (vybíjanie) — korekčná akcia
    action_price_eur: Optional[float],    # cena, za ktorú by sa korekcia vykonala (VDT bid/ask)
    ref_price_eur: Optional[float],       # referencia: cena obchodu, inak DT cena v čase problému
    horizon_h: float,
    min_spread_eur: float,
    max_loss_eur: float,
    deadband_kw: float,
    max_action_kw: float,                 # feasibility cap (voľný výkon z reálneho SOC/gridu)
) -> Dict[str, Any]:
    """Rozhodne, či a koľko upratať. Vráti {act, kw, margin, req_margin, reason}.

    Zásah LEN zmenšuje odchýlku (kw ≤ |deviation| aj ≤ max_action_kw). Bez referencie
    alebo pod deadband → nerob nič. margin = ziskovosť korekcie voči referencii;
    zasiahne sa len ak margin ≥ required_margin(τ) (ktorá klesá s časom až do −max_loss).
    """
    out = {"act": False, "kw": 0.0, "margin": None, "req_margin": None, "reason": ""}
    _dir = str(direction or "").lower().strip()
    if _dir not in ("buy", "sell"):
        out["reason"] = f"neznámy smer: {direction!r}"
        return out
    if abs(float(deviation_kw)) < float(deadband_kw):
        out["reason"] = "pod deadband — nič"
        return out
    if action_price_eur is None or ref_price_eur is None:
        out["reason"] = "niet referencie ceny → nezasahuj"
        return out
    # Marža korekcie: predaj chce cenu NAD referenciou, nákup POD referenciou.
    if _dir == "sell":
        margin = float(action_price_eur) - float(ref_price_eur)
    else:
        margin = float(ref_price_eur) - float(action_price_eur)
    req = required_margin_eur(tau_h, horizon_h, min_spread_eur, max_loss_eur)
    out["margin"] = round(margin, 3)
    out["req_margin"] = round(req, 3)
    if margin < req - 1e-9:
        out["reason"] = (f"marža {margin:.1f} < požiadavka {req:.1f} €/MWh "
                          f"(τ={tau_h:.1f}h) → počkaj")
        return out
    kw = min(abs(float(deviation_kw)), abs(float(max_action_kw)))
    if kw < 1e-6:
        out["reason"] = "žiadna voľná kapacita (max_action_kw≈0)"
        return out
    out.update(act=True, kw=round(kw, 3),
               reason=(f"upratanie {_dir} {kw:.0f} kW · marža {margin:.1f} ≥ "
                       f"req {req:.1f} €/MWh (τ={tau_h:.1f}h)"))
    return out


def detect_undeliverable_target(
    soc_path,
    cur_slot: int,
    *,
    soc_min_pct: float,
    soc_max_pct: float,
    batt_kwh: float,
    batt_kw: float,
    dt_h: float = 0.25,
) -> Optional[Dict[str, Any]]:
    """Nájdi PRVÝ budúci slot, kde SOC trajektória (committed nominácia simulovaná z REÁLNEHO
    SOC) vyjde mimo [soc_min, soc_max] = nedodateľný obchod. Vráti dict alebo None (všetko OK).

    soc_path = výstup simulate_soc_unclipped (97 hodnôt: štart + 96 koncov slotov).
    direction = korekcia TERAZ: 'sell' (budúci over-charge → uvoľni SOC teraz),
                'buy' (budúci over-discharge, SOC pod min → doplň teraz).
    deviation_kw = koľko treba korigovať (excess energia / dt, cap na batt_kw).
    """
    n = min(96, max(0, len(soc_path) - 1))
    bk = float(batt_kwh)
    for i in range(max(0, int(cur_slot)), n):
        s = float(soc_path[i + 1])
        if s > soc_max_pct + 1e-6:
            excess_kwh = (s - soc_max_pct) / 100.0 * bk
            dev_kw = excess_kwh / dt_h
            if batt_kw > 0:
                dev_kw = min(float(batt_kw), dev_kw)
            return {"problem_slot": i, "tau_h": (i - int(cur_slot)) * dt_h,
                    "direction": "sell", "deviation_kw": dev_kw, "kind": "over_charge"}
        if s < soc_min_pct - 1e-6:
            excess_kwh = (soc_min_pct - s) / 100.0 * bk
            dev_kw = excess_kwh / dt_h
            if batt_kw > 0:
                dev_kw = min(float(batt_kw), dev_kw)
            return {"problem_slot": i, "tau_h": (i - int(cur_slot)) * dt_h,
                    "direction": "buy", "deviation_kw": dev_kw, "kind": "over_discharge"}
    return None


__all__ = ["required_margin_eur", "decide_cleanup", "detect_undeliverable_target"]
