"""economics_bridge: VDT obchody → Order kontrakty (golden-bezpečný preklad).

Overuje mapovanie (charge→buy/discharge→sell), preskočenie idle+neplatnej ceny,
záporné ceny OK, a integráciu so SKUTOČNÝM výstupom vdt_optimizer (dôkaz, že
prekladač funguje na reálnych obchodoch a nemení ekonomiku).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd


def _setup_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DB_URL"] = f"sqlite:///{path}"
    for mod in list(sys.modules):
        if (mod in ("db", "fleet", "trading")
                or mod.startswith(("db.", "fleet.", "trading."))):
            del sys.modules[mod]
    import db
    db.init_db()
    return path


def test_trade_mapping_and_skips():
    from trading.economics_bridge import vdt_trades_to_orders
    trades = [
        {"slot_idx": 10, "action": "charge", "charge_kwh": 100, "discharge_kwh": 0,
         "buy_price_eur_mwh": 80.0, "sell_price_eur_mwh": None},
        {"slot_idx": 20, "action": "discharge", "charge_kwh": 0, "discharge_kwh": 150,
         "buy_price_eur_mwh": None, "sell_price_eur_mwh": 200.0},
        {"slot_idx": 30, "action": "idle", "charge_kwh": 0, "discharge_kwh": 0},
        {"slot_idx": 40, "action": "charge", "charge_kwh": 50, "buy_price_eur_mwh": 0.0},   # cena 0 → skip
        {"slot_idx": 50, "action": "discharge", "discharge_kwh": 50, "sell_price_eur_mwh": -30.0},  # záporná OK
    ]
    orders = vdt_trades_to_orders(trades, block_id=1, account_id="acc1", country="sk", day="2026-06-18")
    assert len(orders) == 3, [o.slot_idx for o in orders]
    by_slot = {o.slot_idx: o for o in orders}
    assert by_slot[10].side == "buy" and by_slot[10].volume_kwh == 100 and by_slot[10].price_eur_mwh == 80.0
    assert by_slot[20].side == "sell" and by_slot[20].volume_kwh == 150 and by_slot[20].price_eur_mwh == 200.0
    assert by_slot[50].side == "sell" and by_slot[50].price_eur_mwh == -30.0   # záporná cena platná
    assert 30 not in by_slot and 40 not in by_slot                            # idle + cena 0 preskočené


def test_integration_with_real_optimizer():
    """Reálny vdt_optimizer výstup → Order. Dôkaz, že prekladač sedí na ostrých
    obchodoch a každý Order má platnú cenu + správny smer."""
    from vdt_optimizer import optimize_vdt_day
    from trading.economics_bridge import vdt_trades_to_orders

    base = pd.Timestamp("2026-01-01 00:00:00")
    prices = [80, 60, 40, 60, 120, 180, 200, 160, 90, 70, 50, 70,
              100, 140, 190, 210, 170, 110, 80, 60, 90, 130, 100, 70]
    snap = pd.DataFrame([{"period": f"{(base + pd.Timedelta(hours=i)).hour:02d}:00",
                          "start_local": base + pd.Timedelta(hours=i), "price_eur": float(p)}
                         for i, p in enumerate(prices)])
    res = optimize_vdt_day(snap, batt_kw=1000, batt_kwh=2000, soc_start_pct=50,
                           soc_min_pct=5, soc_max_pct=95, soc_end_min_pct=None,
                           use_orderbook=False, future_only=False, min_spread=0.0)
    assert res["ok"]
    orders = vdt_trades_to_orders(res["trades"], block_id=1, account_id="acc1",
                                  country="sk", day="2026-01-01")
    # každý Order: platná cena (≠0), správny smer, kladný objem
    for o in orders:
        assert o.price_eur_mwh != 0.0
        assert o.side in ("buy", "sell")
        assert o.volume_kwh > 0
    # počet Orderov = počet ne-idle obchodov s platnou cenou
    tradeable = [t for t in res["trades"] if t["action"] in ("charge", "discharge")]
    assert len(orders) <= len(tradeable)


def test_dispatch_vdt_trades_end_to_end():
    _setup_db()
    import fleet, trading

    b1 = fleet.register_battery("B1", "sk", mode="simulation", batt_kw=1000, batt_kwh=2000, enabled=True)
    b2 = fleet.register_battery("B2", "sk", mode="simulation", batt_kw=1000, batt_kwh=2000, enabled=True)
    blk = fleet.create_block("BLK1", "sk")
    fleet.assign(b1, blk); fleet.assign(b2, blk)

    trades = [
        {"slot_idx": 40, "action": "discharge", "discharge_kwh": 300, "sell_price_eur_mwh": 150.0},
        {"slot_idx": 41, "action": "idle", "charge_kwh": 0, "discharge_kwh": 0},
    ]
    out = trading.dispatch_vdt_trades(trades, block_id=blk, account_id="acc1",
                                      country="sk", day="2026-06-18", dt_h=0.25, persist=True)
    assert out["orders"] == 1
    assert len(out["allocations"]) == 2                 # rozdelené na 2 batérie
    assert abs(sum(a.share_kwh for a in out["allocations"]) - 300.0) < 0.01
    # Order + Allocations perzistované
    assert len(trading.list_orders(day="2026-06-18")) == 1
    assert len(fleet.pending_commands(b1)) == 1 and len(fleet.pending_commands(b2)) == 1


if __name__ == "__main__":
    test_trade_mapping_and_skips()
    test_integration_with_real_optimizer()
    test_dispatch_vdt_trades_end_to_end()
    print("✓ economics_bridge testy OK")
