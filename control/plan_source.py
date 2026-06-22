# -*- coding: utf-8 -*-
"""control/plan_source.py — plánovaný setpoint per batéria (z jej profilu).

Most medzi fleet (Battery → profile_id) a existujúcim plánovačom
(`auto_control.compute_setpoint_for_now`). Každá inštancia (proces per batéria) si
takto sama vypočíta cieľový setpoint z D-1 plánu svojho profilu pre aktuálny slot.
Manuálny príkaz z DB (instance_command 'setpoint') má v control.loop.tick prednosť.

Golden jadro (optimize_day / run_day_physical) sa NEvolá — len sa ČÍTA hotový plán.
"""
from __future__ import annotations
from typing import Optional, Any, Dict


def profile_name_for(battery: Dict[str, Any]) -> Optional[str]:
    """Z battery dict (profile_id) vyrieši názov profilu cez DB. None ak nie je."""
    pid = battery.get("profile_id")
    if not pid:
        return None
    try:
        from db import get_session
        from db.models import Profile as _DbProfile
        with get_session() as s:
            row = s.get(_DbProfile, int(pid))
            return row.name if row else None
    except Exception:
        return None


def planned_setpoint_kw(battery: Dict[str, Any]) -> Optional[float]:
    """Plánovaný batt setpoint [kW] pre aktuálny slot z profilu batérie.
    +vybíja / −nabíja. None ak batéria nemá profil / plán / nastala chyba
    (volajúci si drží predošlú hodnotu = plynulé riadenie, nie skok)."""
    name = profile_name_for(battery)
    if not name:
        return None
    try:
        import auto_control as _ac
        r = _ac.compute_setpoint_for_now(profile=name, market=battery.get("country"))
        sp = (r or {}).get("setpoint_kw")
        return float(sp) if sp is not None else None
    except Exception:
        return None


__all__ = ["profile_name_for", "planned_setpoint_kw"]
