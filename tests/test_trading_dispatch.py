"""trading dispatch: Order bloku → split → setpoint príkazy → control loop vykoná.

End-to-end plumbing kontraktov v SIM (žiadna ekonomika): batéria→AvailabilityReport,
agregácia, split, Allocation→command, control tick. Overuje, že objem obchodu sa
rozdelí na batérie a tie reálne dosiahnu alokovaný setpoint.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _setup_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DB_URL"] = f"sqlite:///{path}"
    # purge moduly držiace db binding (db/fleet/trading) — nech sa re-importujú
    # s novým engine pre nový DB_URL (inak FK na batériu v inej DB)
    for mod in list(sys.modules):
        if (mod in ("db", "fleet", "trading")
                or mod.startswith(("db.", "fleet.", "trading."))):
            del sys.modules[mod]
    import db
    db.init_db()
    return path


def _order(block_id, side="sell", vol=300.0, price=120.0):
    from core.schemas.vpp import Order
    return Order(order_id="o1", account_id="acc1", block_id=str(block_id), country="sk",
                 day="2026-06-18", slot_idx=40, side=side, volume_kwh=vol,
                 price_eur_mwh=price, source="vdt")


def test_availability_report_capacity():
    from trading.availability import battery_availability
    bat = {"id": 1, "batt_kw": 1000, "batt_kwh": 2000, "eff": 0.95}
    r = battery_availability(bat, soc_pct=80.0, day="2026-06-18", slot_idx=40, dt_h=0.25)
    # vybíjateľné: (80−5)% × 2000 = 1500 kWh / 0.25h = 6000 kW, strop výkon 1000
    assert r.free_discharge_kw == 1000.0
    # nabíjateľné: (95−80)% × 2000 = 300 kWh / 0.25 = 1200 → strop 1000
    assert r.free_charge_kw == 1000.0
    assert r.battery_id == "1"


def test_dispatch_splits_and_commands_then_control_executes():
    _setup_db()
    import fleet
    from trading.dispatch import dispatch_order
    from control.runner import build_fleet_executors, tick_fleet

    b1 = fleet.register_battery("B1", "sk", mode="simulation", batt_kw=1000, batt_kwh=2000, enabled=True)
    b2 = fleet.register_battery("B2", "sk", mode="simulation", batt_kw=1000, batt_kwh=2000, enabled=True)
    blk = fleet.create_block("BLK1", "sk", split_strategy="free_capacity")
    fleet.assign(b1, blk); fleet.assign(b2, blk)

    # SELL 300 kWh @ slot (dt_h=0.25). 2 batérie SOC 80 % → dosť kapacity → 150/150 kWh
    allocs = dispatch_order(_order(blk, side="sell", vol=300.0), dt_h=0.25)
    assert len(allocs) == 2
    assert abs(sum(a.share_kwh for a in allocs) - 300.0) < 0.01    # suma sedí
    assert all(a.share_kwh > 0 for a in allocs)                    # sell = +vybíja
    assert all(abs(a.setpoint_kw - a.share_kwh / 0.25) < 0.01 for a in allocs)

    # príkazy zaradené pre obe batérie
    assert len(fleet.pending_commands(b1)) == 1
    assert len(fleet.pending_commands(b2)) == 1

    # control loop ich vykoná → batérie vybíjajú → SOC klesne pod 50 (default štart)
    execs = build_fleet_executors()
    res = tick_fleet(execs, dt_h=0.25)
    assert res[b1]["health"] == "ok" and res[b2]["health"] == "ok"
    assert fleet.get_status(b1)["soc_pct"] < 50.0
    assert fleet.get_status(b2)["soc_pct"] < 50.0


def test_dispatch_buy_negative_setpoint():
    _setup_db()
    import fleet
    from trading.dispatch import dispatch_order

    b1 = fleet.register_battery("B1", "sk", mode="simulation", batt_kw=1000, batt_kwh=2000, enabled=True)
    blk = fleet.create_block("BLK1", "sk")
    fleet.assign(b1, blk)
    allocs = dispatch_order(_order(blk, side="buy", vol=100.0), dt_h=0.25, enqueue=False)
    assert len(allocs) == 1
    assert allocs[0].share_kwh < 0 and allocs[0].setpoint_kw < 0   # buy = −nabíja


def test_dispatch_empty_block():
    _setup_db()
    import fleet
    from trading.dispatch import dispatch_order
    blk = fleet.create_block("EMPTY", "sk")
    assert dispatch_order(_order(blk), dt_h=0.25) == []


def test_persist_order_and_allocations():
    _setup_db()
    import fleet, trading
    from trading.dispatch import dispatch_order

    b1 = fleet.register_battery("B1", "sk", mode="simulation", batt_kw=1000, batt_kwh=2000, enabled=True)
    b2 = fleet.register_battery("B2", "sk", mode="simulation", batt_kw=1000, batt_kwh=2000, enabled=True)
    blk = fleet.create_block("BLK1", "sk")
    fleet.assign(b1, blk); fleet.assign(b2, blk)

    allocs = dispatch_order(_order(blk, side="sell", vol=300.0), dt_h=0.25,
                            enqueue=False, persist=True)
    # Order v DB
    orders = trading.list_orders(day="2026-06-18")
    assert len(orders) == 1 and orders[0]["order_id"] == "o1" and orders[0]["side"] == "sell"
    # Allocations v DB (pending) pre obe batérie
    pend1 = trading.pending_allocations(battery_id=b1)
    pend2 = trading.pending_allocations(battery_id=b2)
    assert len(pend1) == 1 and len(pend2) == 1
    # mark applied → už nie pending
    trading.mark_allocation_applied(pend1[0]["id"])
    assert trading.pending_allocations(battery_id=b1) == []


def test_save_availability_row():
    _setup_db()
    import fleet, trading
    from trading.availability import battery_availability
    b1 = fleet.register_battery("B1", "sk", mode="simulation", batt_kw=1000, batt_kwh=2000, enabled=True)
    rep = battery_availability(fleet.get_battery(b1), 70.0, "2026-06-18", 40, dt_h=0.25)
    rid = trading.save_availability(rep)
    assert rid > 0


if __name__ == "__main__":
    test_availability_report_capacity()
    test_dispatch_splits_and_commands_then_control_executes()
    test_dispatch_buy_negative_setpoint()
    test_dispatch_empty_block()
    test_persist_order_and_allocations()
    test_save_availability_row()
    print("✓ trading dispatch testy OK")
