# -*- coding: utf-8 -*-
"""core/feasibility.py — JEDNA feasibility vrstva (KROK 1 zjednotenia, 2026-06-28).

Cieľ (viď ARCHITEKTURA_planovanie_obchodovanie_analyza.md + IMPLEMENTACNY_PLAN_feasibility_unify.md):
zjednotiť rozsypanú SOC/grid/výkon feasibilitu (9 implementácií, 5 SOC zdrojov, 3 zápisy
fyziky) do jedného modulu so single source of truth pre fyziku batérie.

KROK 1 (tento súbor) = ČISTÁ EXTRAKCIA. `gate()` je bit-exact extrakt z
`vdt_soc_feasible.soc_feasible_vdt` — žiadna zmena správania. Staré funkcie sa stanú
tenkými wrappermi (parity kryté `tests/test_feasibility_parity.py`).

KONVENCIA FYZIKY (definovaná RAZ tu, zhodná s optimizer.py forward-sweep):
  batt_kw > 0 = vybíjanie → SOC -= (batt_kw·dt)/eff_d
  batt_kw < 0 = nabíjanie → SOC += (|batt_kw|·dt)·eff_c
"""
from __future__ import annotations
from typing import List, Sequence, Tuple


def battery_step(soc_kwh: float, batt_kw: float, dt_h: float,
                 eff_c: float, eff_d: float) -> float:
    """Jeden krok SOC (kWh) pre daný batériový výkon. JEDINÁ definícia fyziky batérie.

    +batt_kw = vybíjanie (SOC↓, delené eff_d), −batt_kw = nabíjanie (SOC↑, násobené eff_c).
    """
    if batt_kw > 0:
        return soc_kwh - (batt_kw * dt_h) / eff_d
    if batt_kw < 0:
        return soc_kwh + (-batt_kw * dt_h) * eff_c
    return soc_kwh


def clip_to_soc_feasible(batt_kw: float, soc_kwh: float, *, dt_h: float,
                         eff_c: float, eff_d: float,
                         soc_min_kwh: float, soc_max_kwh: float) -> float:
    """Oreže JEDEN batériový povel tak, aby výsledný SOC ostal v [soc_min, soc_max].
    JEDINÝ fyzikálny strop pre REALIZOVANÚ batériu (nezávisle od cesty: run_day_physical,
    joint_lp, _batt_real fallback…). Energiu nedá uložiť do plnej ani vziať z prázdnej.

    Konvencia (zhodná s battery_step): +batt_kw = vybíjanie (/eff_d), −batt_kw = nabíjanie (·eff_c).
    Zrkadlí run_day_physical r.712–714:
      vybíjanie:  batt ≤ (soc − soc_min)·eff_d / dt_h
      nabíjanie:  |batt| ≤ (soc_max − soc) / (dt_h·eff_c)
    """
    dt_h = max(1e-9, float(dt_h)); eff_c = max(1e-6, float(eff_c)); eff_d = max(1e-6, float(eff_d))
    b = float(batt_kw or 0.0)
    if b > 0:      # vybíjanie — obmedzené dostupným SOC nad dnom
        room = max(0.0, float(soc_kwh) - float(soc_min_kwh))
        return min(b, room * eff_d / dt_h)
    if b < 0:      # nabíjanie — obmedzené voľným miestom pod stropom
        head = max(0.0, float(soc_max_kwh) - float(soc_kwh))
        return -min(-b, head / (dt_h * eff_c))
    return 0.0


def soc_trajectory(batt_kw: Sequence[float], *, soc_init_kwh: float, batt_kwh: float = 0.0,
                   eff_c: float = 0.95, eff_d: float = 0.95, dt_h: float = 0.25) -> List[float]:
    """SOC trajektória (kWh, dĺžka N+1) pre daný batt plán BEZ clipu (na detekciu porušenia).

    `batt_kwh` parameter sa neignoruje kvôli spätnej kompatibilite podpisu, ale do výpočtu
    nevstupuje (trajektória bez clipu).
    """
    eff_c = max(1e-6, float(eff_c)); eff_d = max(1e-6, float(eff_d)); dt_h = max(1e-6, float(dt_h))
    soc = float(soc_init_kwh)
    path = [soc]
    for b in batt_kw:
        soc = battery_step(soc, float(b or 0.0), dt_h, eff_c, eff_d)
        path.append(soc)
    return path


def gate(dam_batt_kw: Sequence[float],
         vdt_batt_kw: Sequence[float],
         *,
         soc_init_kwh: float,
         batt_kwh: float,
         soc_min_frac: float = 0.05,
         soc_max_frac: float = 1.0,
         eff_c: float = 0.95,
         eff_d: float = 0.95,
         dt_h: float = 0.25,
         grid_export_kw: float = None,
         grid_import_kw: float = None,
         net_base_kw: Sequence[float] = None) -> Tuple[List[float], List[float], int]:
    """JEDNA feasibility brána: oreže VDT vrstvu tak, aby kombinovaný DAM+VDT plán bol
    REÁLNE dodateľný — v SOC AJ v limite grid prípojky. DAM ostáva nedotknutý.

    Bit-exact extrakcia z vdt_soc_feasible.soc_feasible_vdt (KROK 1, žiadna zmena správania).

    GRID feasibilita: net cez prah = net_base + batt ∈ [−grid_import, +grid_export].
      Batériový profil bez FTV/load → net_base=0 → net = batt → |batt| ≤ grid.
      Shared-meter → net_base = load−FTV per slot (kW; net export +).

    Vracia: (vdt_allowed_kw[N], soc_path_kwh[N+1], clipped_slots).
    """
    n = min(len(dam_batt_kw), len(vdt_batt_kw))
    smin = float(batt_kwh) * float(soc_min_frac)
    smax = float(batt_kwh) * float(soc_max_frac)
    eff_c = max(1e-6, float(eff_c)); eff_d = max(1e-6, float(eff_d))
    dt_h = max(1e-6, float(dt_h))
    _ge = float(grid_export_kw) if (grid_export_kw is not None and grid_export_kw > 0) else None
    _gi = float(grid_import_kw) if (grid_import_kw is not None and grid_import_kw > 0) else None
    soc = float(soc_init_kwh)
    vdt_allowed: List[float] = []
    soc_path: List[float] = [soc]
    clipped = 0
    for i in range(n):
        d = float(dam_batt_kw[i] or 0.0)
        v = float(vdt_batt_kw[i] or 0.0)
        target = d + v                                    # kombinovaný batt kW (+vybíja/−nabíja)
        # 1) GRID limit: net cez prah = net_base + batt ∈ [−grid_import, +grid_export].
        nb = float(net_base_kw[i]) if (net_base_kw is not None and i < len(net_base_kw)) else 0.0
        net = nb + target
        if _ge is not None and net > _ge:                 # príliš veľký export → zníž vybíjanie
            target -= (net - _ge)
        net = nb + target
        if _gi is not None and net < -_gi:                # príliš veľký import → zníž nabíjanie
            target += (-_gi - net)
        # 2) SOC limit
        if target > 0:                                    # vybíjanie
            max_dis_kw = max(0.0, (soc - smin)) * eff_d / dt_h
            target_f = min(target, max_dis_kw)
        elif target < 0:                                  # nabíjanie
            max_chg_kw = max(0.0, (smax - soc)) / (dt_h * eff_c)
            target_f = max(target, -max_chg_kw)
        else:
            target_f = 0.0
        # DAM ostáva; VDT = zvyšok do feasibilného targetu
        v_eff = target_f - d
        if abs(v_eff - v) > 1e-6:
            clipped += 1
        soc = battery_step(soc, target_f, dt_h, eff_c, eff_d)
        soc = min(smax, max(smin, soc))                   # numerická poistka
        vdt_allowed.append(v_eff)
        soc_path.append(soc)
    return vdt_allowed, soc_path, clipped


def gate_extras(dam_batt_kw: Sequence[float],
                extras: dict,
                *,
                soc_start_kwh: float,
                batt_kwh: float,
                soc_min_frac: float = 0.05,
                soc_max_frac: float = 1.0,
                eff_c: float = 0.95,
                eff_d: float = 0.95,
                dt_h: float = 0.25,
                start_slot: int = 0,
                grid_export_kw: float = None,
                grid_import_kw: float = None,
                net_base_kw: Sequence[float] = None,
                reserve_frac: float = 0.0):
    """Adaptér: oreže VDT `extras` (dict {slot:('BUY'|'SELL', kwh)}) cez JEDNU bránu `gate`
    z JEDNÉHO baseline (reálny SOC v štartovacom slote). Nahrádza reťaz
    clip_extras_to_grid + 2× clip_extras_to_capacity (rôzne baseline = baseline-mismatch).

    Konvencia: SELL = vybíjanie (+ na batt), BUY = nabíjanie (− na batt). DAM ostáva,
    orezáva sa LEN VDT. Iteruje sloty [start_slot .. 95]; extras mimo tohto okna ostávajú.

    Vracia (clipped_extras, report).
    """
    n = len(dam_batt_kw)
    s0 = max(0, int(start_slot))
    # rezerva zúži pásmo (zhodné s clip_extras_to_capacity reserve_pct)
    smin_f = float(soc_min_frac) + float(reserve_frac)
    smax_f = float(soc_max_frac) - float(reserve_frac)
    # extras → vdt_batt_kw (kW): SELL = +vybíja, BUY = −nabíja
    vdt_kw = [0.0] * n
    for t, ev in (extras or {}).items():
        if t is None or not (s0 <= int(t) < n):
            continue
        direction, kwh = ev
        kw = float(kwh) / max(1e-6, dt_h)
        vdt_kw[int(t)] = kw if str(direction).upper() in ("SELL", "DISCHARGE") else -kw
    dam_win = [float(dam_batt_kw[i] or 0.0) for i in range(s0, n)]
    vdt_win = [vdt_kw[i] for i in range(s0, n)]
    nb_win = None
    if net_base_kw is not None:
        nb_win = [float(net_base_kw[i]) if i < len(net_base_kw) else 0.0 for i in range(s0, n)]
    vdt_allowed, _soc_path, _clip = gate(
        dam_win, vdt_win, soc_init_kwh=soc_start_kwh, batt_kwh=batt_kwh,
        soc_min_frac=smin_f, soc_max_frac=smax_f, eff_c=eff_c, eff_d=eff_d, dt_h=dt_h,
        grid_export_kw=grid_export_kw, grid_import_kw=grid_import_kw, net_base_kw=nb_win)
    # vdt_allowed (kW) → extras dict (kWh)
    out = {}
    report = []
    for j, va in enumerate(vdt_allowed):
        t = s0 + j
        kwh = abs(va) * dt_h
        if kwh <= 1e-6:
            new_ev = None
        elif va > 0:
            new_ev = ("SELL", kwh)
        else:
            new_ev = ("BUY", kwh)
        old_ev = (extras or {}).get(t)
        if new_ev is not None:
            out[t] = new_ev
        # report ak sa zmenilo (orezané/zmazané) oproti pôvodnému
        if old_ev is not None:
            old_kwh = float(old_ev[1])
            new_kwh = (new_ev[1] if new_ev else 0.0)
            if new_kwh < old_kwh - 1e-6 or (new_ev and new_ev[0] != old_ev[0]):
                report.append({"slot": t, "orig": old_ev,
                               "clip": new_ev, "reason": "gate(soc∧grid∧vykon)"})
    return out, report


__all__ = ["battery_step", "soc_trajectory", "gate", "gate_extras"]
