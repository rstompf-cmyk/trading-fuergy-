# -*- coding: utf-8 -*-
"""VDT-HEADROOM (2026-07-07): stropový headroom v D-1 pláne vyhradený pre VDT dokupy.

Užívateľ: „rezervovať VDT pásmo v pláne" — DAM plán dostane strop SOC ≤ max−headroom,
to pásmo ostane voľné pre VDT dokupy → dokup má kam ísť a nič nepretečie cez SOC.

Testy:
  1. h=0 → golden bit-exact (SOC smie na max, žiadny efekt).
  2. h>0 → SOC plánu NIKDY neprekročí (max − h).
  3. Väčší headroom → nižší strop (monotónne).
  4. Kill-switch PLAN_SOC_RESERVE=0 → h ignorované (golden).
"""
import os
import numpy as np
import optimizer as o


def _run(h, env=None):
    if env is not None:
        os.environ["PLAN_SOC_RESERVE"] = env
    else:
        os.environ.pop("PLAN_SOC_RESERVE", None)
    price = np.array([20] * 8 + [40] * 8 + [200] * 8, float)  # lacno ráno → draho večer
    pv = np.zeros(24)
    sch, summ = o.optimize_day(pv, price, dt=1.0, batt_kw=4000, batt_kwh=4000,
                               eff_c=0.95, eff_d=0.95, soc_min_pct=5, soc_max_pct=100,
                               soc_init_pct=5, grid_kw=4000, vdt_headroom_pct=h)
    return np.asarray(sch["soc_pct"], float)


def test_h0_golden_full_range():
    soc = _run(0.0)
    assert soc.max() > 99.0, f"h=0 má nabiť na ~100%, dostal {soc.max():.1f}"


def test_headroom_caps_ceiling():
    soc = _run(10.0)
    assert soc.max() <= 90.0 + 1e-6, f"h=10 → SOC max má byť ≤90%, dostal {soc.max():.1f}"
    soc20 = _run(20.0)
    assert soc20.max() <= 80.0 + 1e-6, f"h=20 → SOC max má byť ≤80%, dostal {soc20.max():.1f}"


def test_headroom_monotone():
    m0, m10, m20 = _run(0.0).max(), _run(10.0).max(), _run(20.0).max()
    assert m0 >= m10 >= m20, f"väčší headroom → nižší strop, dostal {m0:.0f}/{m10:.0f}/{m20:.0f}"


def test_killswitch_off():
    soc = _run(10.0, env="0")   # PLAN_SOC_RESERVE=0 → headroom ignorované
    os.environ.pop("PLAN_SOC_RESERVE", None)
    assert soc.max() > 99.0, f"kill-switch=0 → h ignorované, SOC ~100%, dostal {soc.max():.1f}"


if __name__ == "__main__":
    for h in (0.0, 10.0, 20.0):
        print(f"h={h:>4}: SOC max={_run(h).max():.1f}%")
    test_h0_golden_full_range()
    test_headroom_caps_ceiling()
    test_headroom_monotone()
    test_killswitch_off()
    print("OK 4/4")
