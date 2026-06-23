"""CDC backend (centrálny CDC server) + Zákazník — offline (bez siete) testy.

Overuje: poskladanie tagov z prefixu, dry-run zápis (žiadny HTTP), dispatch
build_executor → CdcExecutor, a fleet customer CRUD + väzba batéria↔zákazník.
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


def _sample_cfg():
    import cdc
    cfg = cdc._fresh_default()
    cfg["tags_read"]["batt_soc_pct"] = "_I_BMS_SOC_1m"
    return cfg


def test_tag_resolution_prefix():
    import cdc
    cfg = _sample_cfg()
    rt = cdc.resolve_read_tags("VW-BA", cfg)
    assert rt["load_power_kw"] == "VW-BA_I_EL1_Power_1m"
    assert rt["load_power_kw_1h"] == "VW-BA_I_EL1_Power_1h"
    assert rt["batt_power_kw"] == "VW-BA_C_BAT_StoragePower_1m"
    assert rt["batt_soc_pct"] == "VW-BA_I_BMS_SOC_1m"
    wt = cdc.resolve_write_tags("VW-BA", cfg)
    assert wt["cons_plan_kw"] == "VW-BA_U_REG_ConsumptionPlan_Manual_1h"
    # prázdny suffix sa preskočí
    cfg["tags_read"]["ftv_power_kw"] = ""
    assert "ftv_power_kw" not in cdc.resolve_read_tags("VW-BA", cfg)


def test_write_value_dry_run_no_network():
    """Bez enabled/control_enabled/FLEET_REAL_WRITE → DRY-RUN, žiadny HTTP."""
    import cdc
    cfg = _sample_cfg()  # enabled=False default
    res = cdc.write_value("VW-BA", "cons_plan_kw", -50.0, cfg=cfg)
    assert res["ok"] is True
    assert res["dry_run"] is True
    # payload: kW × scale_write(1000) → W, správny tag
    tag = "VW-BA_U_REG_ConsumptionPlan_Manual_1h"
    assert tag in res["payload"]
    assert res["payload"][tag][0]["value"] == -50000.0


def test_build_executor_dispatch_cdc():
    import control.executor as ex
    b = {"name": "VW-BA", "country": "sk", "mode": "real",
         "backend": "cdc", "cdc_prefix": "VW-BA"}
    e = ex.build_executor(b)
    assert type(e).__name__ == "CdcExecutor"
    assert e.prefix == "VW-BA"
    # default backend → RealExecutor (spätná kompatibilita)
    b2 = {"name": "Trakany", "country": "sk", "mode": "real"}
    assert type(ex.build_executor(b2)).__name__ == "RealExecutor"
    # sim
    b3 = {"name": "x", "mode": "simulation", "batt_kw": 10, "batt_kwh": 20}
    assert type(ex.build_executor(b3)).__name__ == "SimExecutor"


def test_customer_crud_and_battery_link():
    _setup_db()
    import fleet
    cid = fleet.create_customer("Muller", "sk", note="2 baterie")
    assert cid > 0
    b1 = fleet.register_battery("VW-BA", "sk", mode="real", backend="cdc",
                                cdc_prefix="VW-BA", customer_id=cid, enabled=True)
    b2 = fleet.register_battery("Muller-SE", "sk", mode="real", backend="cdc",
                                cdc_prefix="Muller-SE", customer_id=cid)
    names = {x["name"] for x in fleet.batteries_for_customer(cid)}
    assert names == {"VW-BA", "Muller-SE"}
    # battery dict nesie nové polia
    bd = fleet.get_battery(b1)
    assert bd["backend"] == "cdc" and bd["cdc_prefix"] == "VW-BA" and bd["customer_id"] == cid
    # odpojenie
    fleet.set_battery_customer(b2, None)
    assert {x["name"] for x in fleet.batteries_for_customer(cid)} == {"VW-BA"}
    # default backend = realio
    b3 = fleet.register_battery("Trakany", "sk", mode="real")
    assert fleet.get_battery(b3)["backend"] == "realio"


def test_rl_bounds_directional():
    from cdc_reg_plan import rl_bounds
    # pevné = striktne plán
    assert rl_bounds(-0.3, "fixed") == (-0.3, -0.3)
    assert rl_bounds(0.4, "fixed") == (0.4, 0.4)
    # smerové: nabíja (base<0) → RT smie len viac nabíjať
    assert rl_bounds(-0.3, "band") == (-1.0, -0.3)
    # vybíja (base>0) → RT smie len viac vybíjať
    assert rl_bounds(0.4, "band") == (0.4, 1.0)
    # nečinné → RT voľné
    assert rl_bounds(0.0, "band") == (-1.0, 1.0)


def test_plan_source_no_profile_returns_none():
    _setup_db()
    import fleet
    from control.plan_source import planned_setpoint_kw, profile_name_for
    bid = fleet.register_battery("SIMX", "sk", mode="simulation",
                                 batt_kw=100, batt_kwh=200, enabled=True)
    b = fleet.get_battery(bid)
    assert profile_name_for(b) is None
    assert planned_setpoint_kw(b) is None   # bez profilu → žiadny plán


def test_run_single_sim_writes_status():
    """run_single (proces-per-batéria) odtiká SIM batériu bez pádu a zapíše status."""
    _setup_db()
    import fleet
    from workers import control_loop
    bid = fleet.register_battery("SIMRUN", "sk", mode="simulation",
                                 batt_kw=100, batt_kwh=200, eff=0.95, enabled=True)
    # 2 ticky, žiadny spánok, veľký dt_h len pre test
    control_loop.run_single(bid, tick_sec=0, max_ticks=2, dt_h=0.25)
    st = fleet.get_status(bid)
    assert st is not None
    assert st.get("health") in ("ok", "degraded")
