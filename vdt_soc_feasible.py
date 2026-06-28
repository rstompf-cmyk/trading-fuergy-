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

    KROK 1 (2026-06-28): táto funkcia je teraz TENKÝ WRAPPER nad core.feasibility.gate
    (single source of truth). Správanie bit-exact (parity: tests/test_feasibility_parity.py).
    """
    from core.feasibility import gate as _gate
    return _gate(dam_batt_kw, vdt_batt_kw,
                 soc_init_kwh=soc_init_kwh, batt_kwh=batt_kwh,
                 soc_min_frac=soc_min_frac, soc_max_frac=soc_max_frac,
                 eff_c=eff_c, eff_d=eff_d, dt_h=dt_h,
                 grid_export_kw=grid_export_kw, grid_import_kw=grid_import_kw,
                 net_base_kw=net_base_kw)


def soc_trajectory(batt_kw: Sequence[float], *, soc_init_kwh: float, batt_kwh: float,
                   eff_c: float = 0.95, eff_d: float = 0.95, dt_h: float = 0.25) -> List[float]:
    """Pomocná: SOC trajektória (kWh, N+1) pre daný batt plán BEZ clipu (na detekciu porušenia).

    KROK 1: wrapper nad core.feasibility.soc_trajectory.
    """
    from core.feasibility import soc_trajectory as _st
    return _st(batt_kw, soc_init_kwh=soc_init_kwh, batt_kwh=batt_kwh,
               eff_c=eff_c, eff_d=eff_d, dt_h=dt_h)
