# -*- coding: utf-8 -*-
"""vdt_soc_feasible.py — SOC-feasibility VDT vrstvy (úloha #28/#59, 2026-06-26).

Problém: optimize_day spraví SOC forward-sweep clip → čistý DAM plán je SOC-feasibilný.
Ale VDT sa do plánu pridáva RAW (livesim: sch.batt_kw += VDT) len s výkonovým clipom (±batt_kw),
BEZ SOC kontroly → kombinovaný DAM+VDT plán môže kázať vybíjať prázdnu / nabíjať plnú batériu
→ realita to nedodá → falošná odchýlka + pokuta. (Reálne riadenie to robí cez audit_action,
preto tam funguje; sim to obchádzal.)

Riešenie: prepustiť VDT cez SOC audit voči trajektórii plánu — rovnaká konvencia ako optimizer.py
(forward sweep: discharge soc-=di/eff_d, charge soc+=ch*eff_c). Zachová sa DAM (bol feasibilný)
a oreže sa LEN VDT tam, kde DAM+VDT prekročí [soc_min, soc_max] — NIE all-or-nothing (to by
zmazalo VDT, čo bola predošlá chybná oprava).
"""
from __future__ import annotations
from typing import List, Sequence, Tuple


def soc_feasible_vdt(dam_batt_kw: Sequence[float],
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
    """Oreže VDT vrstvu tak, aby kombinovaný DAM+VDT plán bol REÁLNE dodateľný — v SOC AJ
    v limite grid prípojky. (Bez grid clipu plán nominuje viac než prejde cez prah → realita
    oreže → falošná odchýlka/pokuta; presne prípad WV_simulacia_4: batt 6000, grid 4000.)

    Konvencia SOC (zhodná s optimizer.py forward-sweep):
      batt_kw > 0 = vybíjanie → SOC -= (batt_kw·dt)/eff_d ; < 0 = nabíjanie → SOC += (|batt|·dt)·eff_c

    GRID feasibilita: net cez prah = net_base + batt. Pre batériový profil bez FTV/load je
    net_base=0 → net = batt → |batt| ≤ grid. Pre shared-meter daj net_base = load−FTV per slot
    (kW; net export +). Clip: net ∈ [−grid_import, +grid_export]. DAM ostáva, orezáva sa LEN VDT.

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
        #    Konvencia: batt +vybíja → +export; +import = nabíjanie. net export = +.
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
        if target_f > 0:
            soc -= (target_f * dt_h) / eff_d
        elif target_f < 0:
            soc += (-target_f * dt_h) * eff_c
        soc = min(smax, max(smin, soc))                   # numerická poistka
        vdt_allowed.append(v_eff)
        soc_path.append(soc)
    return vdt_allowed, soc_path, clipped


def soc_trajectory(batt_kw: Sequence[float], *, soc_init_kwh: float, batt_kwh: float,
                   eff_c: float = 0.95, eff_d: float = 0.95, dt_h: float = 0.25) -> List[float]:
    """Pomocná: SOC trajektória (kWh, N+1) pre daný batt plán BEZ clipu (na detekciu porušenia)."""
    eff_c = max(1e-6, float(eff_c)); eff_d = max(1e-6, float(eff_d)); dt_h = max(1e-6, float(dt_h))
    soc = float(soc_init_kwh); path = [soc]
    for b in batt_kw:
        b = float(b or 0.0)
        if b > 0:
            soc -= (b * dt_h) / eff_d
        elif b < 0:
            soc += (-b * dt_h) * eff_c
        path.append(soc)
    return path
