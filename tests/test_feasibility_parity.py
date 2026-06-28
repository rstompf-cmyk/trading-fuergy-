# -*- coding: utf-8 -*-
"""test_feasibility_parity.py — KROK 1 parity (2026-06-28).

Overuje, že core.feasibility.gate je BIT-EXACT extrakcia pôvodnej feasibility logiky
(zamrazená referenčná kópia `_reference_gate` = pôvodný vdt_soc_feasible.soc_feasible_vdt
pred refaktorom). Žiadna zmena správania v KROKU 1.

Zároveň overuje, že produkčný wrapper vdt_soc_feasible.soc_feasible_vdt == gate.
"""
import os, sys, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.feasibility import gate
from vdt_soc_feasible import soc_feasible_vdt


def _reference_gate(dam_batt_kw, vdt_batt_kw, *, soc_init_kwh, batt_kwh,
                    soc_min_frac=0.05, soc_max_frac=1.0, eff_c=0.95, eff_d=0.95,
                    dt_h=0.25, grid_export_kw=None, grid_import_kw=None, net_base_kw=None):
    """ZAMRAZENÁ kópia pôvodného soc_feasible_vdt (commit pred KROK 1). NEMENIŤ."""
    n = min(len(dam_batt_kw), len(vdt_batt_kw))
    smin = float(batt_kwh) * float(soc_min_frac)
    smax = float(batt_kwh) * float(soc_max_frac)
    eff_c = max(1e-6, float(eff_c)); eff_d = max(1e-6, float(eff_d))
    dt_h = max(1e-6, float(dt_h))
    _ge = float(grid_export_kw) if (grid_export_kw is not None and grid_export_kw > 0) else None
    _gi = float(grid_import_kw) if (grid_import_kw is not None and grid_import_kw > 0) else None
    soc = float(soc_init_kwh)
    vdt_allowed = []; soc_path = [soc]; clipped = 0
    for i in range(n):
        d = float(dam_batt_kw[i] or 0.0); v = float(vdt_batt_kw[i] or 0.0)
        target = d + v
        nb = float(net_base_kw[i]) if (net_base_kw is not None and i < len(net_base_kw)) else 0.0
        net = nb + target
        if _ge is not None and net > _ge:
            target -= (net - _ge)
        net = nb + target
        if _gi is not None and net < -_gi:
            target += (-_gi - net)
        if target > 0:
            max_dis_kw = max(0.0, (soc - smin)) * eff_d / dt_h
            target_f = min(target, max_dis_kw)
        elif target < 0:
            max_chg_kw = max(0.0, (smax - soc)) / (dt_h * eff_c)
            target_f = max(target, -max_chg_kw)
        else:
            target_f = 0.0
        v_eff = target_f - d
        if abs(v_eff - v) > 1e-6:
            clipped += 1
        if target_f > 0:
            soc -= (target_f * dt_h) / eff_d
        elif target_f < 0:
            soc += (-target_f * dt_h) * eff_c
        soc = min(smax, max(smin, soc))
        vdt_allowed.append(v_eff); soc_path.append(soc)
    return vdt_allowed, soc_path, clipped


def _assert_equal(a, b, ctx=""):
    va, sa, ca = a; vb, sb, cb = b
    assert ca == cb, f"clipped mismatch {ca}!={cb} {ctx}"
    assert len(va) == len(vb) and len(sa) == len(sb), f"len mismatch {ctx}"
    for i, (x, y) in enumerate(zip(va, vb)):
        assert abs(x - y) <= 1e-9, f"vdt[{i}] {x}!={y} {ctx}"
    for i, (x, y) in enumerate(zip(sa, sb)):
        assert abs(x - y) <= 1e-9, f"soc[{i}] {x}!={y} {ctx}"


def _cases():
    rnd = random.Random(42)
    out = []
    # deterministické edge cases
    out.append(dict(dam=[0]*96, vdt=[3000.0]*96, soc_init_kwh=300.0, batt_kwh=6000.0))   # nabíja plnú časom
    out.append(dict(dam=[0]*96, vdt=[-3000.0]*96, soc_init_kwh=5700.0, batt_kwh=6000.0)) # plná → nedovolí dokúpiť
    out.append(dict(dam=[6000.0]*96, vdt=[2000.0]*96, soc_init_kwh=6000.0, batt_kwh=6000.0,
                    grid_export_kw=4000.0, grid_import_kw=4000.0))                         # grid < batt
    out.append(dict(dam=[-2000.0,4000.0]*48, vdt=[1000.0,-1000.0]*48, soc_init_kwh=3000.0,
                    batt_kwh=6000.0, net_base_kw=[500.0]*96))                              # shared-meter net_base
    # náhodné case-y
    for _ in range(200):
        n = 96
        dam = [rnd.uniform(-6000, 6000) for _ in range(n)]
        vdt = [rnd.uniform(-3000, 3000) for _ in range(n)]
        c = dict(dam=dam, vdt=vdt,
                 soc_init_kwh=rnd.uniform(300, 5700), batt_kwh=6000.0,
                 soc_min_frac=0.05, soc_max_frac=rnd.choice([0.95, 1.0]),
                 eff_c=rnd.choice([0.9, 0.95]), eff_d=rnd.choice([0.9, 0.95]))
        if rnd.random() < 0.5:
            c["grid_export_kw"] = rnd.choice([3000.0, 4000.0, 6000.0])
            c["grid_import_kw"] = rnd.choice([3000.0, 4000.0, 6000.0])
        if rnd.random() < 0.4:
            c["net_base_kw"] = [rnd.uniform(-1000, 1000) for _ in range(n)]
        out.append(c)
    return out


def test_gate_matches_reference():
    for k, c in enumerate(_cases()):
        kw = {x: c[x] for x in c if x not in ("dam", "vdt")}
        ref = _reference_gate(c["dam"], c["vdt"], **kw)
        new = gate(c["dam"], c["vdt"], **kw)
        _assert_equal(ref, new, ctx=f"case {k}")


def test_wrapper_matches_gate():
    for k, c in enumerate(_cases()):
        kw = {x: c[x] for x in c if x not in ("dam", "vdt")}
        g = gate(c["dam"], c["vdt"], **kw)
        w = soc_feasible_vdt(c["dam"], c["vdt"], **kw)
        _assert_equal(g, w, ctx=f"wrapper case {k}")


if __name__ == "__main__":
    test_gate_matches_reference()
    test_wrapper_matches_gate()
    print("parity OK")
