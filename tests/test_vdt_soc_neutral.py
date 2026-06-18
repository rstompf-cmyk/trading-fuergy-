"""Testy VDT energetického salda (SOC-neutralita).

Pravidlo (user 2026-06-18): "pár musí sedieť aj z pohľadu energie — nech sa
nestane, že kúpi 1000 a predá 100. Saldo musí byť 0 na konci."
VDT je overlay nad DAM → čistá zmena SOC nad rámec DAM musí byť ≈ 0.
Bez opravy (fallback dropoval neutralitu) LP pri lákavých cenách DUMPOVAL SOC
(predaj bez spätného nákupu) → nevyvážená pozícia. Tu overujeme, že saldo drží.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
from vdt_optimizer import optimize_vdt_day

EFF = 0.95


def _snapshot(prices):
    """24 hodinových slotov s danými cenami (€/MWh), bez orderbooku."""
    base = pd.Timestamp("2026-01-01 00:00:00")
    rows = []
    for i, p in enumerate(prices):
        st = base + pd.Timedelta(hours=i)
        rows.append({"period": f"{st.hour:02d}:00-{(st.hour+1)%24:02d}:00",
                     "start_local": st, "price_eur": float(p)})
    return pd.DataFrame(rows)


def _net_soc_kwh(res):
    s = res["summary"]
    return EFF * s["total_charged_kwh"] - s["total_discharged_kwh"] / EFF


def test_neutral_no_dump_high_soc():
    """Vysoký štart SOC + klesajúce ceny = lákadlo dumpovať. Saldo musí byť ~0."""
    prices = list(np.linspace(200, 50, 24))   # klesá → "predaj teraz draho"
    snap = _snapshot(prices)
    res = optimize_vdt_day(snap, batt_kw=1000, batt_kwh=2000, soc_start_pct=90,
                           soc_min_pct=5, soc_max_pct=95, soc_end_min_pct=None,
                           use_orderbook=False, future_only=False, min_spread=0.0)
    assert res["ok"], res.get("error")
    net = _net_soc_kwh(res)
    assert abs(net) <= 0.015 * 2000 * 1.5 + 1, f"VDT saldo nevyvážené: {net:.1f} kWh"


def test_neutral_balanced_each_trade_paired():
    """Každý VDT nákup musí byť spárovaný predajom rovnakej energie (extra net ~0)."""
    prices = [80, 60, 40, 60, 120, 180, 200, 160, 90, 70, 50, 70,
              100, 140, 190, 210, 170, 110, 80, 60, 90, 130, 100, 70]
    snap = _snapshot(prices)
    res = optimize_vdt_day(snap, batt_kw=1000, batt_kwh=2000, soc_start_pct=50,
                           soc_min_pct=5, soc_max_pct=95, soc_end_min_pct=None,
                           use_orderbook=False, future_only=False, min_spread=0.0)
    assert res["ok"], res.get("error")
    # dam=0 → vdt_extra = celkový obchod; charge a discharge musia sedieť energeticky
    extra_c = res["vdt_extra_charge_kwh"]
    extra_d = res["vdt_extra_discharge_kwh"]
    net = EFF * extra_c - extra_d / EFF
    assert abs(net) <= 0.015 * 2000 * 1.5 + 1, \
        f"nepárové VDT: charge {extra_c:.0f} vs discharge {extra_d:.0f} (net {net:.1f})"


def test_neutral_terminal_free_still_balanced():
    """Voľný koniec SOC (#30) nesmie viesť k nevyváženému koncovému vybitiu."""
    prices = list(np.linspace(60, 220, 24))   # rastie → "drž a predaj na konci"
    snap = _snapshot(prices)
    res = optimize_vdt_day(snap, batt_kw=1000, batt_kwh=2000, soc_start_pct=80,
                           soc_min_pct=5, soc_max_pct=95, soc_end_min_pct=None,
                           use_orderbook=False, future_only=False, min_spread=0.0)
    assert res["ok"], res.get("error")
    net = _net_soc_kwh(res)
    assert abs(net) <= 0.015 * 2000 * 1.5 + 1, f"koncové saldo nevyvážené: {net:.1f} kWh"


if __name__ == "__main__":
    test_neutral_no_dump_high_soc()
    test_neutral_balanced_each_trade_paired()
    test_neutral_terminal_free_still_balanced()
    print("✓ všetky 3 testy salda prešli")
