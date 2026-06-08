# -*- coding: utf-8 -*-
"""tests/test_golden_optimizer.py — Golden snapshot test pre optimize_day.

Spustí `optimize_day` na fixed input a porovnáva s uloženým výsledkom v
`tests/golden/optimizer_<scenario>.json`. Akákoľvek zmena vo výstupe
(ZISK_EUR, batt_kw, schedule) spôsobí FAIL — vývojár musí explicitne
aktualizovať golden cez:

    UPDATE_GOLDEN=1 python3 -m tests.test_golden_optimizer

Scenáre:
  1. baseline — typický slnečný deň, žiadne extra constraints
  2. high_load — load > pv export (potrebuje import)
  3. zero_pv — nočný deň, iba spotreba zo siete
  4. constrained_battery — malá batéria, vyžaduje smart timing

Fáza D.1 — chráni proti regression keď refactorujeme joint_lp / combined_backtest.
"""
from __future__ import annotations
import os
import sys
import json
from typing import Any, Dict, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

# Import optimizera — z root-u
import optimizer


GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")
UPDATE = os.environ.get("UPDATE_GOLDEN", "").strip() in ("1", "true", "yes")


# ── scenár generátory (deterministické) ────────────────────────────────────

def _typical_day() -> Tuple[np.ndarray, np.ndarray]:
    """24h, bell-curve PV peak v poludnie, ceny vysoké večer."""
    hours = np.arange(24, dtype=float)
    # PV: bell curve centered at hour 12, max 100 kWh
    pv = 100.0 * np.exp(-((hours - 12) ** 2) / (2 * 3.0 ** 2))
    pv = np.round(pv, 2)
    # Price: 50 €/MWh base + 30 evening peak (hours 18-21)
    price = np.full(24, 50.0)
    price[18:22] = 100.0
    price[0:5] = 30.0   # cheap night
    return pv, price


def _high_load() -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """PV + price ako v _typical, plus stála load 50 kWh."""
    pv, pr = _typical_day()
    load = np.full(24, 50.0)
    return pv, pr, load


def _zero_pv() -> Tuple[np.ndarray, np.ndarray]:
    """Nočný deň — všetko z grid-u."""
    pv = np.zeros(24)
    price = np.full(24, 50.0)
    price[18:22] = 120.0
    return pv, price


# ── helper: deterministic dict porovnanie ──────────────────────────────────

def _normalize_for_compare(sch: Dict[str, Any], summary: Dict[str, Any]) -> Dict[str, Any]:
    """Konvertuje numpy arrays na lists, zaokrúhli floats — porovnanie stabilné."""
    out: Dict[str, Any] = {"schedule": {}, "summary": {}}
    for k, v in sch.items():
        if hasattr(v, "tolist"):
            out["schedule"][k] = [round(float(x), 4) for x in v.tolist()]
        else:
            out["schedule"][k] = v
    for k, v in summary.items():
        if isinstance(v, float):
            out["summary"][k] = round(v, 4)
        elif hasattr(v, "tolist"):
            out["summary"][k] = v.tolist()
        else:
            out["summary"][k] = v
    return out


def _compare_or_save(name: str, sch: Dict, summary: Dict) -> Tuple[bool, str]:
    """Porovná aktuálny výstup s golden. UPDATE_GOLDEN=1 → prepíše golden.
    Vracia (passed, diff_message)."""
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    path = os.path.join(GOLDEN_DIR, f"optimizer_{name}.json")
    actual = _normalize_for_compare(sch, summary)

    if UPDATE or not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(actual, f, indent=2, ensure_ascii=False)
        return True, f"  → golden saved/updated: {path}"

    with open(path) as f:
        expected = json.load(f)

    # Compare summary (key-by-key)
    diff: list = []
    for k, v_exp in expected["summary"].items():
        v_act = actual["summary"].get(k)
        if isinstance(v_exp, float) and isinstance(v_act, (int, float)):
            if abs(v_exp - v_act) > 0.01:
                diff.append(f"summary[{k}]: expected={v_exp} actual={v_act}")
        elif v_act != v_exp:
            diff.append(f"summary[{k}]: expected={v_exp} actual={v_act}")
    # Compare schedule (array sumy + key set)
    for k in expected["schedule"]:
        if k not in actual["schedule"]:
            diff.append(f"schedule key '{k}' missing in actual")
            continue
        v_exp = expected["schedule"][k]
        v_act = actual["schedule"][k]
        if len(v_exp) != len(v_act):
            diff.append(f"schedule[{k}] length: expected={len(v_exp)} actual={len(v_act)}")
            continue
        sum_exp = sum(v_exp); sum_act = sum(v_act)
        if abs(sum_exp - sum_act) > 0.1:
            diff.append(f"schedule[{k}] sum: expected={sum_exp:.2f} actual={sum_act:.2f}")
    if diff:
        return False, "\n    ".join(diff)
    return True, "OK"


# ── tests ──────────────────────────────────────────────────────────────────

def test_baseline_day():
    """Typický slnečný deň — batéria má nabíjať od PV a vybíjať na večernom peak-u."""
    pv, price = _typical_day()
    sch, summary = optimizer.optimize_day(
        pv, price,
        batt_kw=100, batt_kwh=200,
        eff_c=0.95, eff_d=0.95,
        soc_min_pct=5, soc_max_pct=95, soc_init_pct=50,
        grid_kw=100, grid_fee=22.0, cycle_cost=2.0,
        allow_grid_charge=True, allow_curtail=True,
        dt=1.0,
    )
    ok, msg = _compare_or_save("baseline_day", sch, summary)
    assert ok, f"baseline_day golden diff:\n    {msg}"


def test_high_load():
    """Deň s vysokou load — potrebuje import zo siete pri peak-och."""
    pv, price, load = _high_load()
    sch, summary = optimizer.optimize_day(
        pv, price,
        batt_kw=100, batt_kwh=200, soc_init_pct=30,
        grid_kw=100, grid_fee=22.0, cycle_cost=2.0,
        load_kwh=load,
        dt=1.0,
    )
    ok, msg = _compare_or_save("high_load", sch, summary)
    assert ok, f"high_load golden diff:\n    {msg}"


def test_zero_pv():
    """Žiadne PV — arbitráž čisto na cenách + grid-u."""
    pv, price = _zero_pv()
    sch, summary = optimizer.optimize_day(
        pv, price,
        batt_kw=100, batt_kwh=200, soc_init_pct=50,
        grid_kw=100, grid_fee=22.0, cycle_cost=2.0,
        allow_grid_charge=True,
        dt=1.0,
    )
    ok, msg = _compare_or_save("zero_pv", sch, summary)
    assert ok, f"zero_pv golden diff:\n    {msg}"


def test_constrained_battery():
    """Malá batéria (10 kW / 20 kWh) — optimalizátor musí timing-ovať."""
    pv, price = _typical_day()
    sch, summary = optimizer.optimize_day(
        pv, price,
        batt_kw=10, batt_kwh=20, soc_init_pct=50,
        grid_kw=100, grid_fee=22.0, cycle_cost=2.0,
        dt=1.0,
    )
    ok, msg = _compare_or_save("constrained_battery", sch, summary)
    assert ok, f"constrained_battery golden diff:\n    {msg}"


def test_curtail_disabled():
    """allow_curtail=False — PV plne export aj keď cena záporná (žiadne curtail)."""
    pv, price = _typical_day()
    price[10:14] = -10.0   # 4 hodiny záporné DT
    sch, summary = optimizer.optimize_day(
        pv, price,
        batt_kw=100, batt_kwh=200, soc_init_pct=50,
        grid_kw=100, grid_fee=22.0, cycle_cost=2.0,
        allow_curtail=False,
        dt=1.0,
    )
    ok, msg = _compare_or_save("curtail_disabled", sch, summary)
    assert ok, f"curtail_disabled golden diff:\n    {msg}"


if __name__ == "__main__":
    print(f"━━━ Golden tests (UPDATE={UPDATE}) ━━━\n")
    tests = sorted([t for t in dir() if t.startswith("test_")])
    passed = failed = 0
    for tname in tests:
        try:
            globals()[tname]()
            print(f"  ✓ {tname}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {tname}")
            print(f"    {e}")
            failed += 1
    print(f"\n━━━ {passed} OK, {failed} FAIL ━━━")
    if UPDATE:
        print(f"\n[golden files updated v {GOLDEN_DIR}]")
    sys.exit(0 if failed == 0 else 1)
