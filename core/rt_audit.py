# -*- coding: utf-8 -*-
"""Faza C: RT pre-slot audit — chrani SOC zazmluvnenu pre buduce sloty.

Problem (uzivatel 2026-06-10): RT zasah v slot N nabije/vybije bateriu nad
ramcom planu. Ked pride zazmluvneny D-1 alebo VDT slot K>N, baterka uz
nema kam nabit (SOC=full) / vybit (SOC=empty) → odchylka + ZCO pokuta.

Riesenie: pre kazdy 15-min slot sa zavola `audit_action(source='rt')` ktory
simuluje SOC trajektoriu cez VSETKY zazmluvnene buduce sloty (D-1 + VDT realized).
Ak by RT intent v tomto slote sposobil SOC violation v budúcom slote, vrati
`allowed_kwh < requested`. Volajúci (livesim.advance) potom znizi rt_power_pct
multiplikatívne pre dany slot.

API:
    audit_rt_slot(profile, day, slot_idx, plan_batt_kw, rt_intent_kw) → dict:
        {
            "decision": "accept" | "downscale" | "reject" | "skip",
            "requested_rt_kw": float,    # povodne navrhnuty RT
            "allowed_rt_kw": float,      # po SOC audit
            "scale_factor": float,       # 0.0..1.0 (1.0 = beze zmeny)
            "reason": str,
        }

Pre kazdy slot za den (96 volani) = znesitelne (kazdy slot 1 audit).
Per-minute audit (1440 volani) by bol pridrahý — odmietnute.

NEDOTYKA SA effect_db ani SOC integracie v livesim.advance.
"""
from __future__ import annotations
from typing import Dict, Any, Optional


def audit_rt_slot(profile: str,
                   day: str,
                   slot_idx: int,
                   plan_batt_kw: float,
                   rt_intent_kw: float,
                   *,
                   step_min: int = 15) -> Dict[str, Any]:
    """Audit RT zasahu pred aplikaciou.

    Args:
        profile: meno profilu
        day: ISO datum (napr. '2026-06-10')
        slot_idx: 15-min slot index 0..95
        plan_batt_kw: D-1 + VDT planovany batt v tomto slote (signed kW;
                       + = vybijanie, - = nabijanie)
        rt_intent_kw: RT navrh navyse k planu (signed kW). Total batt =
                       plan + rt.
        step_min: dlžka slotu v minutach (default 15)

    Returns dict (vid module docstring).

    Logika:
    1. Ak |rt_intent_kw| < 1 kW → skip (RT je sumovo nulovy, nie je co auditovat)
    2. Total batt v slote = plan + rt
    3. direction = 'discharge' ak total > 0, 'charge' ak total < 0
    4. Cele |total| ide do audit_action ako "navrhnuta akcia"
       - audit_action vie odlisit ze plan je uz zazmluvneny (cez scheduled_kwh)
       - vrati allowed_kwh = max povolene total
    5. Z allowed odpocitame |plan| → allowed_rt = max(0, allowed - |plan|)
    6. scale_factor = allowed_rt / |rt_intent| (0..1)
    """
    out = {
        "decision": "skip",
        "requested_rt_kw": float(rt_intent_kw),
        "allowed_rt_kw": float(rt_intent_kw),
        "scale_factor": 1.0,
        "reason": "",
    }
    # Skip drobne RT
    if abs(rt_intent_kw) < 1.0:
        return out

    # Total batt v tomto slote (plan + rt)
    total_kw = float(plan_batt_kw) + float(rt_intent_kw)
    direction = "discharge" if total_kw > 0 else "charge"
    abs_total_kw = abs(total_kw)
    dt_h = max(float(step_min), 1.0) / 60.0
    total_kwh = abs_total_kw * dt_h

    if total_kwh < 0.5:
        return out

    try:
        from core.soc_use_audit import audit_action as _soc_audit
        sa = _soc_audit(profile, day, int(slot_idx), direction, total_kwh,
                          source="rt", step_min=step_min)
    except Exception as e:
        out["reason"] = f"audit_action raised: {e}"
        return out

    decision = sa.get("decision", "accept")
    reason = sa.get("reason", "")
    if decision == "accept":
        out["decision"] = "accept"
        out["allowed_rt_kw"] = float(rt_intent_kw)
        out["scale_factor"] = 1.0
        return out

    # Bug RT-AUDIT-FAILOPEN (2026-06-10): ak audit_action zlyhal kvôli
    # internej chybe (compute_current_state, parse, atď), nesprávne by sme
    # vrátili scale=0 a vypli RT pre celý deň. Lepšie fail-open: vrátiť
    # accept (žiadny clip), nech aspoň base RT engine bežia s plnou silou.
    # Užívateľ uvidí v logu "[RT-PRE-AUDIT] ... fail-open" že audit nemohol
    # rozhodnúť, ale RT sa neumelo zablokuje.
    _fail_keywords = ("zlyhalo", "raised", "exception", "Error",
                       "not ok", "compute_current_state",
                       # Plán už je infeasible — audit hovorí "aj 0 kWh by
                       # porušilo SOC". To znamená že D-1 plán + current SOC
                       # sú nekonzistentné. RT nemá ako pomôcť pri tom, čo už
                       # bolo zle naplánované. Lepšie nechať RT bežať
                       # (rt_controller má per-minute SOC clip ako safety net).
                       "aj 0 kWh by",
                       "infeasible")
    if any(k in reason for k in _fail_keywords):
        out["decision"] = "accept"
        out["allowed_rt_kw"] = float(rt_intent_kw)
        out["scale_factor"] = 1.0
        out["reason"] = f"fail-open: {reason[:120]}"
        return out

    # Bug RT-AUDIT-STARTSOC (2026-06-10): ak start_soc je už pod eff_min,
    # audit_action každú akciu označí ako violation (lebo SOC trajektória
    # začína mimo limitov). V tom prípade:
    # - pre charge (nabíjanie) → fail-open (akcia zlepší SOC)
    # - pre discharge (vybíjanie) → ostáva downscale (akcia zhorší)
    # Analogicky ak start_soc nad eff_max:
    # - discharge → fail-open
    # - charge → downscale
    try:
        _start_soc = float(sa.get("current_soc_pct", 50.0))
        _eff_min = float(sa.get("soc_min_eff_pct", 20.0))
        _eff_max = float(sa.get("soc_max_eff_pct", 80.0))
        _is_charge = (rt_intent_kw < 0)
        if _start_soc < _eff_min and _is_charge:
            out["decision"] = "accept"
            out["allowed_rt_kw"] = float(rt_intent_kw)
            out["scale_factor"] = 1.0
            out["reason"] = (f"fail-open: start_soc {_start_soc:.1f}% < eff_min "
                              f"{_eff_min:.1f}%, charge je legit oprava")
            return out
        if _start_soc > _eff_max and (not _is_charge):
            out["decision"] = "accept"
            out["allowed_rt_kw"] = float(rt_intent_kw)
            out["scale_factor"] = 1.0
            out["reason"] = (f"fail-open: start_soc {_start_soc:.1f}% > eff_max "
                              f"{_eff_max:.1f}%, discharge je legit oprava")
            return out
    except Exception:
        pass

    # downscale alebo reject — spočítaj povolenu časť RT navyše k planu
    allowed_total_kwh = float(sa.get("allowed_kwh", 0.0))
    allowed_total_kw = allowed_total_kwh / max(dt_h, 0.01)
    abs_plan_kw = abs(float(plan_batt_kw))

    # Plan ostáva chranenz (audit_action garantuje že plan sam je feasible —
    # ak nie, ide o sirsi bug v D-1 LP). Tu obmedzime LEN RT zložku.
    allowed_rt_abs = max(0.0, allowed_total_kw - abs_plan_kw)

    # Sign preserve: RT v rovnakom smere ako original
    sign_rt = 1.0 if rt_intent_kw > 0 else -1.0
    allowed_rt_signed = sign_rt * min(abs(rt_intent_kw), allowed_rt_abs)

    out["allowed_rt_kw"] = float(allowed_rt_signed)
    out["scale_factor"] = (abs(allowed_rt_signed) / abs(rt_intent_kw)
                            if abs(rt_intent_kw) > 0.01 else 0.0)
    out["decision"] = decision   # downscale / reject
    out["reason"] = sa.get("reason", "")
    return out


__all__ = ["audit_rt_slot"]
