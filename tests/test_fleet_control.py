"""VPP fleet + control SIM cesta end-to-end (proti reálnej DB schéme cez init_db).

Overuje: registrácia batérie (fleet repo) → build_fleet_executors → tick_fleet →
status v DB. SIM batéria vykoná setpoint (SOC sa zmení), REAL batéria bez realio
wiringu spadne do fail-safe (degraded) ale NEzhodí flotilu (izolácia per batéria).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _setup_db():
    """Čistá dočasná SQLite s plnou schémou (create_all)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DB_URL"] = f"sqlite:///{path}"
    # re-import db modulov s novým DB_URL
    for mod in list(sys.modules):
        if mod == "db" or mod.startswith("db.") or mod == "fleet" or mod.startswith("fleet."):
            del sys.modules[mod]
    import db
    db.init_db()
    return path


def test_sim_path_end_to_end():
    _setup_db()
    import fleet
    from control.runner import build_fleet_executors, tick_fleet

    sim_id = fleet.register_battery("SIM1", "sk", mode="simulation",
                                    batt_kw=1000, batt_kwh=2000, eff=0.95, enabled=True)
    # real batéria BEZ hostu → RealExecutor padne rýchlo ("host nenastavený"),
    # bez siete → deterministicky degraded (overuje fail-safe izoláciu)
    real_id = fleet.register_battery("REAL1", "sk", mode="real",
                                     batt_kw=990, batt_kwh=2150, enabled=True)

    assert {b["name"] for b in fleet.list_batteries(enabled_only=True)} == {"SIM1", "REAL1"}

    execs = build_fleet_executors(soc_default=50.0)
    assert type(execs[sim_id]).__name__ == "SimExecutor"
    assert type(execs[real_id]).__name__ == "RealExecutor"

    # SIM batéria: nabíjaj (−600 kW) na 15 min
    fleet.enqueue_command(sim_id, "setpoint", {"kw": -600.0})
    res = tick_fleet(execs, dt_h=0.25)

    # SIM vykonal → ok + SOC stúpol nad 50 %
    assert res[sim_id]["health"] == "ok"
    assert res[sim_id]["applied"] is True
    st_sim = fleet.get_status(sim_id)
    assert st_sim["soc_pct"] > 50.0

    # REAL bez wiringu → fail-safe degraded, ale fleet nespadol (vrátil výsledok)
    assert res[real_id]["health"] == "degraded"
    assert res[real_id]["applied"] is False
    st_real = fleet.get_status(real_id)
    assert st_real["health"] == "degraded"


def test_command_consumed_once():
    _setup_db()
    import fleet
    from control.runner import build_fleet_executors, tick_fleet

    bid = fleet.register_battery("SIM2", "cz", mode="simulation",
                                 batt_kw=500, batt_kwh=1000, enabled=True)
    fleet.enqueue_command(bid, "setpoint", {"kw": 200.0})
    assert len(fleet.pending_commands(bid)) == 1

    execs = build_fleet_executors()
    tick_fleet(execs, dt_h=0.1)
    # príkaz skonzumovaný → druhý tick už nemá pending
    assert fleet.pending_commands(bid) == []


def test_stop_command_zeroes_setpoint():
    _setup_db()
    import fleet
    from control.runner import build_fleet_executors, tick_fleet

    bid = fleet.register_battery("SIM3", "sk", mode="simulation",
                                 batt_kw=500, batt_kwh=1000, enabled=True)
    fleet.enqueue_command(bid, "stop", {})
    execs = build_fleet_executors()
    res = tick_fleet(execs, dt_h=0.1)
    assert res[bid]["stopped"] is True
    assert res[bid]["target_kw"] == 0.0


if __name__ == "__main__":
    test_sim_path_end_to_end()
    test_command_consumed_once()
    test_stop_command_zeroes_setpoint()
    print("✓ fleet+control SIM cesta OK")
