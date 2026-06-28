# -*- coding: utf-8 -*-
"""test_feasibility_gate_extras.py — KROK 3 (2026-06-28).

gate_extras = jedno volanie gate, ktoré nahrádza clip_extras_to_grid + 2× clip_extras_to_capacity.
Testy overujú jadro pravidla majiteľa: VDT nesmie kúpiť keď je SOC ~100%, ani predať keď ~0%,
a nesmie pretlačiť grid prípojku. DAM (baseline) ostáva.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.feasibility import gate_extras, soc_trajectory

BATT_KWH = 6000.0
DT = 0.25


def _soc_after(dam_kw, extras_out):
    """Zrekonštruuj batt_kw = dam + vdt(z extras) a vráť SOC trajektóriu (%)."""
    vdt = [0.0] * len(dam_kw)
    for t, (d, kwh) in extras_out.items():
        vdt[t] = (kwh / DT) if d == "SELL" else -(kwh / DT)
    batt = [dam_kw[i] + vdt[i] for i in range(len(dam_kw))]
    path = soc_trajectory(batt, soc_init_kwh=_soc0, batt_kwh=BATT_KWH, dt_h=DT)
    return [p / BATT_KWH * 100.0 for p in path]


_soc0 = 0.0  # nastaví sa per test


def test_no_buy_when_full():
    global _soc0
    _soc0 = BATT_KWH * 1.0  # 100 %
    dam = [0.0] * 96
    extras = {10: ("BUY", 1500.0)}  # chce nabiť, ale je plno
    out, rep = gate_extras(dam, extras, soc_start_kwh=_soc0, batt_kwh=BATT_KWH,
                           soc_min_frac=0.05, soc_max_frac=1.0)
    assert 10 not in out or out[10][1] <= 1e-6, f"nabíjanie pri 100% sa malo zmazať: {out}"


def test_no_sell_when_empty():
    global _soc0
    _soc0 = BATT_KWH * 0.05  # na podlahe
    dam = [0.0] * 96
    extras = {20: ("SELL", 1500.0)}  # chce predať, ale prázdne
    out, rep = gate_extras(dam, extras, soc_start_kwh=_soc0, batt_kwh=BATT_KWH,
                           soc_min_frac=0.05, soc_max_frac=1.0)
    assert 20 not in out or out[20][1] <= 1e-6, f"vybíjanie pri 5% sa malo zmazať: {out}"


def test_grid_limit_clips_sell():
    global _soc0
    _soc0 = BATT_KWH * 0.8
    dam = [0.0] * 96
    dam[30] = 4000.0  # DAM už vybíja 4000 kW (grid export limit 4000)
    extras = {30: ("SELL", 1000.0)}  # +1000 kW navrch → cez prípojku
    out, rep = gate_extras(dam, extras, soc_start_kwh=_soc0, batt_kwh=BATT_KWH,
                           soc_min_frac=0.05, soc_max_frac=1.0,
                           grid_export_kw=4000.0, grid_import_kw=4000.0)
    assert 30 not in out or out[30][1] <= 1e-6, f"SELL nad grid sa mal orezať: {out}"


def test_feasible_cycle_passes_and_soc_in_band():
    global _soc0
    _soc0 = BATT_KWH * 0.5
    dam = [0.0] * 96
    extras = {10: ("BUY", 1500.0), 40: ("SELL", 1400.0)}  # ziskový pár, kapacita je
    out, rep = gate_extras(dam, extras, soc_start_kwh=_soc0, batt_kwh=BATT_KWH,
                           soc_min_frac=0.05, soc_max_frac=1.0)
    assert 10 in out and out[10][0] == "BUY" and out[10][1] > 1e-6
    assert 40 in out and out[40][0] == "SELL" and out[40][1] > 1e-6
    soc = _soc_after(dam, out)
    assert min(soc) >= 5.0 - 1e-6 and max(soc) <= 100.0 + 1e-6, f"SOC mimo pásma: {min(soc)}..{max(soc)}"


if __name__ == "__main__":
    test_no_buy_when_full()
    test_no_sell_when_empty()
    test_grid_limit_clips_sell()
    test_feasible_cycle_passes_and_soc_in_band()
    print("gate_extras OK")
