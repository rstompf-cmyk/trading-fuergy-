# -*- coding: utf-8 -*-
"""test_clip_to_soc_feasible.py — fyzikálny SOC strop na realizovanej batérii (úloha #66).

Energia sa nedá uložiť do plnej batérie ani vziať z prázdnej. clip_to_soc_feasible
musí orezať povel tak, aby SOC ostal v [min,max]. Zhoda s run_day_physical r.712-714.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.feasibility import clip_to_soc_feasible, battery_step

BKWH = 1000.0; SMIN = 50.0; SMAX = 1000.0; DT = 0.25; EC = 0.95; ED = 0.95


def test_charge_into_full_is_zero():
    # SOC=100% (1000 kWh), povel nabíjať -6000 kW → musí byť 0 (niet kam)
    out = clip_to_soc_feasible(-6000.0, 1000.0, dt_h=DT, eff_c=EC, eff_d=ED,
                               soc_min_kwh=SMIN, soc_max_kwh=SMAX)
    assert abs(out) < 1e-6, f"nabíjanie plnej malo byť 0, je {out}"


def test_discharge_from_empty_is_zero():
    out = clip_to_soc_feasible(6000.0, 50.0, dt_h=DT, eff_c=EC, eff_d=ED,
                               soc_min_kwh=SMIN, soc_max_kwh=SMAX)
    assert abs(out) < 1e-6, f"vybíjanie prázdnej malo byť 0, je {out}"


def test_feasible_passes_unchanged():
    # SOC=50%, mierne nabíjanie -100 kW → feasibilné, nemení sa
    out = clip_to_soc_feasible(-100.0, 500.0, dt_h=DT, eff_c=EC, eff_d=ED,
                               soc_min_kwh=SMIN, soc_max_kwh=SMAX)
    assert abs(out - (-100.0)) < 1e-6, f"feasibilné malo prejsť, je {out}"


def test_partial_clip_charge():
    # SOC blízko stropu (990/1000) → nabíjanie sa oreže na presný zvyšok miesta
    soc = 990.0
    out = clip_to_soc_feasible(-6000.0, soc, dt_h=DT, eff_c=EC, eff_d=ED,
                               soc_min_kwh=SMIN, soc_max_kwh=SMAX)
    soc_next = battery_step(soc, out, DT, EC, ED)
    assert soc_next <= SMAX + 1e-6, f"SOC pretiekol: {soc_next}"
    assert soc_next >= SMAX - 1e-6, f"malo dobiť presne po strop: {soc_next}"


def test_clipped_soc_never_exceeds_bounds_sweep():
    soc = 500.0
    for b in [-6000, -3000, -100, 0, 100, 3000, 6000, -6000, 6000]:
        c = clip_to_soc_feasible(float(b), soc, dt_h=DT, eff_c=EC, eff_d=ED,
                                 soc_min_kwh=SMIN, soc_max_kwh=SMAX)
        soc = battery_step(soc, c, DT, EC, ED)
        assert SMIN - 1e-6 <= soc <= SMAX + 1e-6, f"SOC mimo pásma: {soc} (b={b})"


if __name__ == "__main__":
    for f in [test_charge_into_full_is_zero, test_discharge_from_empty_is_zero,
              test_feasible_passes_unchanged, test_partial_clip_charge,
              test_clipped_soc_never_exceeds_bounds_sweep]:
        f()
    print("clip_to_soc_feasible OK")
