"""control_loop proces: perzistencia setpointu medzi tickmi + izolácia real batérie.

Overuje, že setpoint zadaný príkazom sa DRŽÍ naprieč tickmi (plynulé riadenie, nie
one-shot) → SOC sa mení každý tick; real batéria bez wiringu ostáva degraded a
NEzhodí slučku.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _setup_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DB_URL"] = f"sqlite:///{path}"
    for mod in list(sys.modules):
        if mod == "db" or mod.startswith("db.") or mod == "fleet" or mod.startswith("fleet."):
            del sys.modules[mod]
    import db
    db.init_db()
    return path


def test_setpoint_persists_across_ticks():
    _setup_db()
    import fleet
    from workers import control_loop

    bid = fleet.register_battery("SIM1", "sk", mode="simulation",
                                 batt_kw=1000, batt_kwh=2000, eff=0.95, enabled=True)
    # nabíjací príkaz (−400 kW) — JEDEN príkaz; musí sa držať naprieč tickmi
    fleet.enqueue_command(bid, "setpoint", {"kw": -400.0})

    # tick_sec malý (rýchly test), dt_h=0.25 (15-min energetický krok) → SOC sa hýbe
    control_loop.run(tick_sec=0.001, max_ticks=4, dt_h=0.25)

    st = fleet.get_status(bid)
    # po 4 tickoch (každý nabíjal) SOC výrazne stúpol nad štart 50 %
    assert st["health"] == "ok"
    assert st["soc_pct"] > 50.5, f"setpoint sa nedržal? SOC={st['soc_pct']}"
    # príkaz bol skonzumovaný hneď v 1. ticku (drží sa cez perzistenciu, nie cez DB)
    assert fleet.pending_commands(bid) == []


def test_real_battery_degraded_does_not_crash_loop():
    _setup_db()
    import fleet
    from workers import control_loop

    sid = fleet.register_battery("SIM2", "sk", mode="simulation",
                                 batt_kw=500, batt_kwh=1000, enabled=True)
    rid = fleet.register_battery("REAL1", "sk", mode="real",
                                 batt_kw=990, batt_kwh=2150, enabled=True, realio_host="x")

    control_loop.run(tick_sec=0.001, max_ticks=2, dt_h=0.25)   # nesmie vyhodiť

    assert fleet.get_status(sid)["health"] == "ok"
    assert fleet.get_status(rid)["health"] == "degraded"


if __name__ == "__main__":
    test_setpoint_persists_across_ticks()
    test_real_battery_degraded_does_not_crash_loop()
    print("✓ control_loop testy OK")
