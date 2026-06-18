"""RealExecutor per-battery (realio wiring) — mockovaný Bender (žiadna sieť).

Overuje: cfg z DB riadku batérie, read_soc cez _fetch_latest_via_bender + scale,
apply_setpoint dual-write (mode + setpoint kW→W) cez _send_tag_writes, a
BEZPEČNOSTNÚ poistku FLEET_REAL_WRITE (default DRY-RUN = NEpíše na HW).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import realio
from control.executor import RealExecutor, build_executor


def _battery():
    return {
        "id": 7, "name": "REAL7", "mode": "real",
        "batt_kw": 990, "batt_kwh": 2150, "eff": 0.95,
        "realio_host": "https://10.9.9.9",
        "realio_username": "u", "realio_password": "p",
        "realio_tags_read": {"batt_soc_pct": "SOC_TAG"},
        "realio_tags_write": {"batt_setpoint_kw": "SP_TAG", "batt_control_mode": "MODE_TAG"},
    }


class _Mock:
    """Patchne realio nízkoúrovňové funkcie (žiadna sieť). Reštauruje v __exit__."""
    def __init__(self, soc=55.0, send_ok=True):
        self.soc, self.send_ok = soc, send_ok
        self.sent = []
        self._orig = {}

    def __enter__(self):
        self._orig = {"send": realio._send_tag_writes, "fetch": realio._fetch_latest_via_bender}

        def fake_send(cfg, writes):
            self.sent.append({"host": cfg.get("host"), "writes": writes})
            return {"ok": self.send_ok, "writes_done": [w["tag"] for w in writes], "msg": "mock"}

        def fake_fetch(cfg, tags):
            return {tags[0]: self.soc}

        realio._send_tag_writes = fake_send
        realio._fetch_latest_via_bender = fake_fetch
        return self

    def __exit__(self, *a):
        realio._send_tag_writes = self._orig["send"]
        realio._fetch_latest_via_bender = self._orig["fetch"]


def test_build_executor_real_mode():
    ex = build_executor(_battery())
    assert type(ex).__name__ == "RealExecutor"
    assert ex.cfg["host"] == "https://10.9.9.9"
    assert ex.cfg["tags_read"]["batt_soc_pct"] == "SOC_TAG"
    assert ex.cfg["tags_write"]["batt_setpoint_kw"] == "SP_TAG"


def test_read_soc_uses_fetch_and_scale():
    ex = RealExecutor(_battery())
    with _Mock(soc=42.5):
        assert ex.read_soc(7) == 42.5


def test_apply_setpoint_real_write_dual():
    os.environ["FLEET_REAL_WRITE"] = "1"
    try:
        ex = RealExecutor(_battery())
        with _Mock(soc=60.0) as mk:
            out = ex.apply_setpoint(7, -400.0)   # nabíjanie 400 kW
        assert out["applied_kw"] == -400.0 and out["soc_pct"] == 60.0
        writes = mk.sent[0]["writes"]
        by_tag = {w["tag"]: w["value"] for w in writes}
        assert by_tag["SP_TAG"] == -400.0 * 1000.0          # kW → W
        assert "MODE_TAG" in by_tag                          # dual-write mode enable
        assert len({w["time"] for w in writes}) == 1         # rovnaký timestamp
    finally:
        os.environ.pop("FLEET_REAL_WRITE", None)


def test_apply_setpoint_dry_run_does_not_write():
    os.environ.pop("FLEET_REAL_WRITE", None)   # default = dry-run
    ex = RealExecutor(_battery())
    with _Mock(soc=50.0) as mk:
        out = ex.apply_setpoint(7, 300.0)
    assert mk.sent == [], "DRY-RUN nesmie zapísať na HW!"
    assert out["soc_pct"] == 50.0   # read je aj v dry-run (read-only)


def test_no_host_raises_fast():
    b = _battery(); b["realio_host"] = ""
    ex = RealExecutor(b)
    try:
        ex.read_soc(7)
        assert False, "malo padnúť bez hostu"
    except RuntimeError as e:
        assert "host" in str(e).lower()


if __name__ == "__main__":
    test_build_executor_real_mode()
    test_read_soc_uses_fetch_and_scale()
    test_apply_setpoint_real_write_dual()
    test_apply_setpoint_dry_run_does_not_write()
    test_no_host_raises_fast()
    print("✓ RealExecutor testy OK")
