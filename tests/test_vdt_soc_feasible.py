# -*- coding: utf-8 -*-
"""Test SOC-feasibility VDT vrstvy (úloha #28/#59).

Reprodukuje: DAM plán (feasibilný) + VDT pridané raw → SOC trajektória prekročí limity.
Overuje fix: soc_feasible_vdt oreže LEN VDT (DAM ostane) → SOC v [smin,smax], VDT sa nezmaže celý.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vdt_soc_feasible import soc_feasible_vdt, soc_trajectory

BATT_KWH = 6000.0
SMIN_F, SMAX_F = 0.05, 1.0
EFFC = EFFD = 0.95
DT = 0.25
SMIN = BATT_KWH * SMIN_F
SMAX = BATT_KWH * SMAX_F


def _violates(path):
    return any(p < SMIN - 1e-6 or p > SMAX + 1e-6 for p in path)


def test_repro_raw_vdt_violates_soc():
    """REPRO: DAM vybíja batériu skoro na dno, VDT pridá ďalšie vybíjanie → SOC pod soc_min."""
    n = 96
    dam = [0.0] * n
    # DAM vybíja 4000 kW v slotoch 0..10 (vyčerpá z plnej ~ na dno)
    for i in range(11):
        dam[i] = 4000.0
    vdt = [0.0] * n
    # VDT pridá vybíjanie 4000 kW v slotoch 8..14 (keď je už skoro prázdna)
    for i in range(8, 15):
        vdt[i] = 4000.0
    raw = [d + v for d, v in zip(dam, vdt)]
    raw_path = soc_trajectory(raw, soc_init_kwh=SMAX, batt_kwh=BATT_KWH, eff_c=EFFC, eff_d=EFFD, dt_h=DT)
    assert _violates(raw_path), "repro má porušiť SOC (vybíjanie prázdnej)"


def test_fix_keeps_dam_and_clips_only_vdt():
    """ČIASTOČNE feasibilný VDT: DAM mierne vybíja (nechá ~47% SOC), VDT vybíja navyše —
    časť sa zmestí, časť (keď narazí na dno) nie. Helper má zachovať feasibilnú časť VDT."""
    n = 96
    dam = [0.0] * n
    for i in range(6):
        dam[i] = 2000.0          # mierne vybíjanie z plnej → ~47% SOC
    vdt = [0.0] * n
    for i in range(6, 13):
        vdt[i] = 2000.0          # VDT vybíja ďalej → časť sa zmestí, časť narazí na dno
    raw = [d + v for d, v in zip(dam, vdt)]
    assert _violates(soc_trajectory(raw, soc_init_kwh=SMAX, batt_kwh=BATT_KWH,
                                     eff_c=EFFC, eff_d=EFFD, dt_h=DT)), "repro má porušiť SOC"
    v_allowed, soc_path, clipped = soc_feasible_vdt(
        dam, vdt, soc_init_kwh=SMAX, batt_kwh=BATT_KWH,
        soc_min_frac=SMIN_F, soc_max_frac=SMAX_F, eff_c=EFFC, eff_d=EFFD, dt_h=DT)
    # 1) DAM+VDT_allowed už NEporušuje SOC
    assert not _violates(soc_path), f"po fixe SOC stále porušuje: min={min(soc_path):.0f} max={max(soc_path):.0f}"
    # 2) DAM ostal nedotknutý v slotoch 0..5 (tam VDT=0)
    for i in range(6):
        assert abs(v_allowed[i]) < 1e-6, f"DAM slot {i} sa nemal dotknúť (VDT=0)"
    # 3) ČASŤ VDT ostala (feasibilná časť) — NEzmazalo sa celé
    kept = sum(a for a in v_allowed if a > 0)
    assert kept > 0.0, "VDT sa zmazal celý (chyba ako predošlý pokus)"
    # 4) ČASŤ VDT bola orezaná (infeasibilná časť po dosiahnutí dna)
    assert clipped > 0


def test_charge_above_max_clipped():
    """VDT nabíja nad 100 % → orezané; DAM ostane."""
    n = 96
    dam = [-4000.0] * 11 + [0.0] * (n - 11)   # nabíja z dna ~ na plno
    vdt = [0.0] * n
    for i in range(8, 15):
        vdt[i] = -4000.0                       # ďalšie nabíjanie pri ~plnej
    v_allowed, soc_path, clipped = soc_feasible_vdt(
        dam, vdt, soc_init_kwh=SMIN, batt_kwh=BATT_KWH,
        soc_min_frac=SMIN_F, soc_max_frac=SMAX_F, eff_c=EFFC, eff_d=EFFD, dt_h=DT)
    assert not _violates(soc_path), "nabíjanie nad max neorezané"
    assert clipped > 0


def test_feasible_vdt_unchanged():
    """Ak je VDT feasibilné (malé), nič sa neoreže (žiadne falošné mazanie)."""
    n = 96
    dam = [0.0] * n
    vdt = [0.0] * n
    vdt[40] = 1000.0   # malé vybíjanie z plnej — feasibilné
    vdt[41] = -1000.0
    v_allowed, soc_path, clipped = soc_feasible_vdt(
        dam, vdt, soc_init_kwh=BATT_KWH * 0.5, batt_kwh=BATT_KWH,
        soc_min_frac=SMIN_F, soc_max_frac=SMAX_F, eff_c=EFFC, eff_d=EFFD, dt_h=DT)
    assert clipped == 0, "feasibilné VDT sa nemalo orezať"
    assert abs(v_allowed[40] - 1000.0) < 1e-6 and abs(v_allowed[41] + 1000.0) < 1e-6


def test_grid_limit_clips_vdt():
    """Presný prípad WV_simulacia_4: batt výkon 6000, grid 4000. DAM vybíja 4000 (=grid),
    VDT chce +2000 navyše (z voľného výkonu batérie) → cez prah sa nezmestí → orezané na grid."""
    n = 96
    GRID = 4000.0
    dam = [0.0] * n
    vdt = [0.0] * n
    for i in range(20, 23):   # len 3 sloty → DAM sám je SOC-feasibilný (z plnej)
        dam[i] = 4000.0      # DAM vybíja na plný grid
        vdt[i] = 2000.0      # VDT chce ďalšie vybíjanie (batt by dalo 6000, grid nie)
    v_allowed, soc_path, clipped = soc_feasible_vdt(
        dam, vdt, soc_init_kwh=SMAX, batt_kwh=BATT_KWH,
        soc_min_frac=SMIN_F, soc_max_frac=SMAX_F, eff_c=EFFC, eff_d=EFFD, dt_h=DT,
        grid_export_kw=GRID, grid_import_kw=GRID)
    combined = [d + a for d, a in zip(dam, v_allowed)]
    # kombinované (=net pre batt-only) NEsmie prekročiť grid 4000
    assert max(combined) <= GRID + 1e-6, f"plán prekračuje grid: max={max(combined)}"
    # DAM (4000) ostal, VDT navyše v rovnakom smere bolo orezané (grid plný)
    for i in range(20, 30):
        assert abs(v_allowed[i]) < 1e-6, f"VDT slot {i} sa malo orezať (grid plný DAMom)"
    assert clipped > 0


def test_grid_opposite_direction_ok():
    """VDT v OPAČNOM smere než DAM sa zmestí (grid sa neplní v tom istom smere)."""
    n = 96
    GRID = 4000.0
    dam = [0.0] * n
    vdt = [0.0] * n
    dam[40] = 4000.0     # DAM vybíja (export 4000)
    vdt[40] = -1500.0    # VDT nabíja (net = 4000-1500 = 2500 export, v grid) → OK
    v_allowed, soc_path, clipped = soc_feasible_vdt(
        dam, vdt, soc_init_kwh=BATT_KWH * 0.6, batt_kwh=BATT_KWH,
        soc_min_frac=SMIN_F, soc_max_frac=SMAX_F, eff_c=EFFC, eff_d=EFFD, dt_h=DT,
        grid_export_kw=GRID, grid_import_kw=GRID)
    assert abs(v_allowed[40] + 1500.0) < 1e-6, "opačný VDT sa nemal orezať (grid OK)"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
