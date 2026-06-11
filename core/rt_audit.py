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
                   step_min: int = 15,
                   current_soc_pct: Optional[float] = None,
                   rt_persistence_slots: int = 4) -> Dict[str, Any]:
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

    dt_h = max(float(step_min), 1.0) / 60.0

    # Bug RT-AUDIT-OPPOSITE (2026-06-10): ak RT ide v opacnom smere nez plán,
    # ZOSLABUJE celkovy batt zatazenie — to je vzdy bezpecnejsie pre SOC
    # trajektoriu (assuming D-1 plán bol feasible). Auditovat netreba.
    # Pred fixom: plán -3000 (charge) + RT +1000 (discharge) → total=-2000 →
    # direction="charge" → audit povolí 2000 kW charge. allowed_rt_abs =
    # max(0, 2000 - 3000) = 0 → RT vybíjanie orezané na 0!
    # User report: "ignoruje aj vybijania ce RT len nabijania" — toto.
    _plan_sign = 1.0 if plan_batt_kw > 0 else (-1.0 if plan_batt_kw < 0 else 0.0)
    _rt_sign = 1.0 if rt_intent_kw > 0 else -1.0
    if _plan_sign != 0.0 and _plan_sign != _rt_sign:
        out["decision"] = "accept"
        out["allowed_rt_kw"] = float(rt_intent_kw)
        out["scale_factor"] = 1.0
        out["reason"] = (f"fail-open: RT smer opacny voci planu "
                          f"(plan={plan_batt_kw:.0f}, rt={rt_intent_kw:.0f}) "
                          f"— zoslabuje zatazenie, audit netreba")
        return out

    # Bug RT-AUDIT-DOUBLECOUNT (2026-06-10): pôvodne sa do audit_action posielal
    # total_kwh = |plan+RT|, ale audit_action ho pripočítava k scheduled_kwh
    # ktoré UŽ obsahuje plán → trial[si] = plan + (plan+RT) = 2×plan + RT.
    # User report: "11:00 plán -3000 + RT -3000 ide na -6000 max ale na grafe
    # SOC ide na 95% napriek plánu 52%".
    # Fix: posielaj LEN RT zložku (= |rt_intent|), nie total. Audit ju spočíta
    # k scheduled[si] sám.
    total_kw = float(plan_batt_kw) + float(rt_intent_kw)
    direction = "discharge" if total_kw > 0 else "charge"
    rt_only_kwh = abs(float(rt_intent_kw)) * dt_h    # iba RT zložka, nie total
    rt_direction = "charge" if float(rt_intent_kw) < 0 else "discharge"

    if rt_only_kwh < 0.5:
        return out

    # Bug AUDIT-CAPACITY (2026-06-10): nový deterministický audit cez kapacitu
    # v reserve pásme. Žiadna binárka, žiadne 96-slot simulácie. User postreh:
    # "audit zrata maximalny mozny vykon pri ktorm je dodrzane podmienky uz
    # zobchodvanych 15min. tolerancia by to mohla odfiltrovat".
    try:
        from core.soc_use_audit import audit_capacity as _cap_audit
        import vdt_state as _vs
        import datetime as _dt
        import profiles as _pr
        _d_obj = _dt.date.fromisoformat(day)
        _today_state = _vs.compute_current_state(profile, today=_d_obj) or {}
        _prof = _pr.load_profile(profile) or {}
        _reserve = float((_prof.get("plan") or {}).get("soc_reserve_pct", 0.0) or 0.0)
        _soc_use = current_soc_pct if current_soc_pct is not None else float(
            _today_state.get("current_soc_pct") or 50.0)
        cap_res = _cap_audit(_soc_use, plan_batt_kw, rt_intent_kw,
                              today_state=_today_state,
                              step_min=step_min,
                              soc_reserve_pct=_reserve,
                              si=int(slot_idx),
                              rt_persistence_slots=int(rt_persistence_slots or 4))
        out["decision"] = cap_res["decision"]
        out["allowed_rt_kw"] = cap_res["allowed_rt_kw"]
        out["scale_factor"] = cap_res["scale_factor"]
        out["reason"] = cap_res["reason"]
        return out
    except Exception as e:
        out["reason"] = f"audit_capacity raised: {e}"
        # fail-open — RT prejde
        return out
    # legacy audit_action zakomentovaný (zostáva pre VDT/auto_control)
    try:
        from core.soc_use_audit import audit_action as _soc_audit
        sa = _soc_audit(profile, day, int(slot_idx), rt_direction, rt_only_kwh,
                          source="rt", step_min=step_min,
                          current_soc_pct_at_si=current_soc_pct)
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
    # Bug RT-AUDIT-FAILOPEN-STRICTER (2026-06-10): odstránené "aj 0 kWh by" a
    # "infeasible" z fail-open. Po SOC-AUDIT-DELTA logike audit_action ignoruje
    # violácie z plánu — vráti "downscale" alebo "reject" LEN keď RT pridáva
    # NOVÚ violation. Ak audit povie reject → musíme reject, nie fail-open.
    # Fail-open keywords ostávajú LEN pre skutočné runtime chyby (exception,
    # compute_current_state crash, atď).
    _fail_keywords = ("zlyhalo", "raised", "exception", "Error",
                       "not ok", "compute_current_state")
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
        # eff_min/eff_max fallback — ak audit_action nevratil tieto polia,
        # citaj priamo z profile.plan (soc_min_pct + soc_reserve_pct).
        # Hard-coded 20/80 by bol bug (uzivatelovo nastavenie sa stratí).
        # Bug SOC-NULL-KEY (2026-06-10): dict.get(k, default) NEvrati default
        # ked key existuje s hodnotou None. Treba "or" pattern. Plus start_soc
        # hard-coded 50% bol bug — citaj soc_init z profile.
        _soc_init_default = 5.0  # posledny safety net ak profile zlyha
        try:
            import profiles as _pr_au
            _prof_au = _pr_au.load_profile(profile) or {}
            _pl_au = _prof_au.get("plan") or {}
            _soc_min_raw = float(_pl_au.get("soc_min_pct") or _pl_au.get("soc_min") or 5.0)
            _soc_max_raw = float(_pl_au.get("soc_max_pct") or _pl_au.get("soc_max") or 100.0)
            _soc_res = float(_pl_au.get("soc_reserve_pct") or 0.0)
            _soc_res = max(0.0, min(50.0, _soc_res))
            _eff_min_default = _soc_min_raw + _soc_res
            _eff_max_default = max(_eff_min_default, _soc_max_raw - _soc_res)
            _soc_init_default = float(_pl_au.get("soc_init_pct") or _pl_au.get("soc_init") or _soc_min_raw)
        except Exception:
            _eff_min_default = 5.0
            _eff_max_default = 100.0
        _start_soc = float(sa.get("current_soc_pct") or _soc_init_default)
        _eff_min = float(sa.get("soc_min_eff_pct") or _eff_min_default)
        _eff_max = float(sa.get("soc_max_eff_pct") or _eff_max_default)
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

    # downscale alebo reject — audit_action vratil koľko RT zložky je povolene
    # (audit dostal LEN rt_only_kwh, nie total). Premenovat na kW.
    allowed_rt_kwh_audit = float(sa.get("allowed_kwh", 0.0))
    allowed_rt_abs = allowed_rt_kwh_audit / max(dt_h, 0.01)

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
