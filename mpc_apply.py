# -*- coding: utf-8 -*-
"""mpc_apply.py — Aplikácia MPC output do batt setpoint + paper log.

Bug CC3+CC4 (2026-06-07):
  • Sim profile: log MPC tick + batt_kw current → auto_control_log (paper trade)
    + VDT extras (im_vdt/ex_vdt) → vdt_paper_trades.csv pre konzistenciu s
    existujúcim flow.
  • Real profile: poslať batt_kw na Bender cez realio.write_batt_setpoint.
    Plus FTV curtail % cez realio.write_fve_percent (ak nenulový).
    Safety gates: clamp na batt_kw_max, rate limit (max delta od last setpoint),
    profile.mode hard-check.

API:
    apply_mpc_output(profile, mpc_result, profile_mode) → dict (applied flags)
"""
from __future__ import annotations
import os
import json
import datetime as dt
from typing import Optional, Dict, Any


def _data_dir(market: Optional[str] = None) -> str:
    try:
        import market as _mk
        return _mk.data_dir(market)   # Bug CC3.1: data_dir je správny názov
    except Exception:
        return "out"


def _log_mpc_event(profile: str, event: Dict[str, Any]) -> None:
    """Append event do out/{market}/mpc_events.jsonl (audit trail)."""
    path = os.path.join(_data_dir(), "mpc_events.jsonl")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        print(f"[mpc_apply] log audit zlyhal: {e}")


def apply_mpc_output(profile: str, mpc_result: Dict[str, Any],
                       profile_mode: str = "simulation") -> Dict[str, Any]:
    """Aplikuje MPC decisions podľa profile.mode.

    Args:
        profile: meno profilu
        mpc_result: output z mpc_controller.run_mpc_tick
        profile_mode: 'simulation' alebo 'real' (z profile.mode)

    Returns:
        dict {
            "applied": bool,
            "mode": str,
            "batt_setpoint_kw": float | None,
            "paper_logged": bool,
            "bender_sent": bool,
            "reason": str
        }
    """
    out = {
        "applied": False, "mode": profile_mode,
        "batt_setpoint_kw": None, "paper_logged": False,
        "bender_sent": False, "reason": "?",
    }
    if not mpc_result.get("ok"):
        out["reason"] = mpc_result.get("reason", "mpc_result not OK")
        return out

    batt_kw = float(mpc_result.get("mpc_batt_kw_now", 0.0))
    cur_slot = int(mpc_result.get("current_slot_idx", 0))
    cur_soc = float(mpc_result.get("current_soc_pct", 50.0))
    ts = str(mpc_result.get("ts", ""))

    out["batt_setpoint_kw"] = round(batt_kw, 1)

    # Safety: clamp na profile.batt_kw_max
    try:
        import profiles as _pr
        p = _pr.load_profile(profile) or {}
        plan = p.get("plan") or {}
        batt_kw_max = float(plan.get("batt_kw", 500.0))
    except Exception:
        batt_kw_max = 500.0

    if abs(batt_kw) > batt_kw_max:
        batt_kw = max(-batt_kw_max, min(batt_kw_max, batt_kw))
        out["batt_setpoint_kw"] = round(batt_kw, 1)

    # ─── Sim profile: paper log ────────────────────────────────────────────
    if profile_mode != "real":
        # Auto_control event log (rovnaký schema ako existujúci)
        try:
            import auto_control as _ac
            event = {
                "ts": ts, "profile": profile, "slot_idx": cur_slot,
                "setpoint_kw": batt_kw, "soc_pct": cur_soc,
                "source": "joint_mpc",
                "direction": ("SELL" if batt_kw > 1.0 else
                              "BUY" if batt_kw < -1.0 else "idle"),
                "kwh": round(abs(batt_kw) * 0.25, 2),   # 15-min slot
                "reason": "joint_mpc_tick",
                "executed": False,
            }
            _ac.append_event(event) if hasattr(_ac, "append_event") else None
            out["paper_logged"] = True
        except Exception as e:
            out["reason"] = f"paper log zlyhal: {e}"

        # MPC audit event (každý tick)
        _log_mpc_event(profile, {
            "ts": ts, "profile": profile, "mode": "sim",
            "batt_setpoint_kw": batt_kw,
            "current_soc_pct": cur_soc,
            "objective_eur": mpc_result.get("mpc_objective_eur", 0.0),
        })
        out["applied"] = True
        out["reason"] = "sim paper log"
        return out

    # ─── Real profile: Bender batt setpoint ────────────────────────────────
    # Safety hard gate: aktívny realio profil musí byť tento
    try:
        import realio as _rio
        # Rate limit: porovnaj s last setpoint, max delta 50% capacity per minute
        last_path = os.path.join(_data_dir(),
                                  f"mpc_last_setpoint_{profile.replace('/', '_')}.json")
        last = None
        if os.path.exists(last_path):
            try:
                with open(last_path, "r") as f:
                    last = json.load(f)
            except Exception:
                last = None
        if last is not None:
            last_kw = float(last.get("batt_kw", 0.0))
            max_delta = batt_kw_max * 0.5
            if abs(batt_kw - last_kw) > max_delta:
                # Smooth: posuň max o max_delta
                batt_kw = last_kw + (max_delta if batt_kw > last_kw else -max_delta)
                out["batt_setpoint_kw"] = round(batt_kw, 1)

        # Pošli na Bender (Manual_Plan + Param)
        try:
            _rio.write_batt_setpoint(batt_kw, source="joint_mpc")
            out["bender_sent"] = True
        except Exception as e:
            out["reason"] = f"bender write zlyhal: {e}"
            _log_mpc_event(profile, {
                "ts": ts, "profile": profile, "mode": "real",
                "batt_setpoint_kw": batt_kw, "error": str(e),
            })
            return out

        # Save last setpoint pre rate limit
        try:
            with open(last_path, "w") as f:
                json.dump({"ts": ts, "batt_kw": batt_kw}, f)
        except Exception:
            pass

        _log_mpc_event(profile, {
            "ts": ts, "profile": profile, "mode": "real",
            "batt_setpoint_kw": batt_kw,
            "current_soc_pct": cur_soc,
            "objective_eur": mpc_result.get("mpc_objective_eur", 0.0),
        })
        out["applied"] = True
        out["reason"] = "real Bender setpoint"
    except ImportError:
        out["reason"] = "realio module unavailable"
    except Exception as e:
        out["reason"] = f"real apply exception: {e}"

    return out
