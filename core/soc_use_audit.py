# -*- coding: utf-8 -*-
"""soc_use_audit.py — Bug #637 (2026-06-09)

Pred KAŽDOU akciou ktorá ovplyvní chod batérie (VDT trade, RT zásah, auto_control
setpoint) simulujeme SOC trajektóriu na 24h dopredu vrátane všetkých už zazmluvnených
commitmentov (D-1 plán + VDT realized). Ak by navrhovaná akcia spôsobila SOC violation
v ktoromkoľvek z budúcich zazmluvnených slotov, akciu **downscale** na max dovolené,
prípadne **reject**.

Asymetria hraníc:
- discharge: SOC musí ostať >= soc_min + soc_reserve_pct vo VŠETKÝCH budúcich slotoch
- charge:    SOC musí ostať <= soc_max - soc_reserve_pct vo VŠETKÝCH budúcich slotoch

Reason (z user feedback):
- "ak má pri vybíjaní väčšiu kapacitu nevadí" → SOC > expected NEVADÍ pre discharge
- "pri nabíjaní nižšiu kapacitu tiež nevadí" → SOC < expected NEVADÍ pre charge

Iba dolná hranica je kritická pre vybíjanie (batt sa nezvládne vybiť pod soc_min),
iba horná pre nabíjanie (batt sa nezvládne nabiť nad soc_max).
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Any, Optional, Tuple


# ─────────────── Helpery: SOC simulácia bez clip-u ───────────────

def simulate_soc_unclipped(start_soc_pct: float,
                             batt_kwh_per_slot: List[float],
                             batt_kwh_capacity: float,
                             eff_c: float = 0.95,
                             eff_d: float = 0.95) -> List[float]:
    """Simulácia SOC trajektórie BEZ clip-u na soc_min/soc_max.

    Bez clip-u potrebné pre audit aby sme videli violations. _integrate_soc_path
    v vdt_state.py clipuje a tak NEVIDÍME kde by simulácia narazila do dna.

    Args:
        start_soc_pct: počiatočný SOC v %
        batt_kwh_per_slot: 96 hodnôt kWh per 15-min slot (+ = vybíjať, − = nabíjať)
        batt_kwh_capacity: kapacita batérie v kWh
        eff_c, eff_d: efektivity

    Returns: 97 hodnôt (start + 96 endov slotu), neclipnuté.
    """
    cap = max(1.0, float(batt_kwh_capacity))
    soc_path = [float(start_soc_pct)]
    cur = float(start_soc_pct)
    eff_d_safe = max(0.01, eff_d)
    for t in range(min(96, len(batt_kwh_per_slot))):
        v = float(batt_kwh_per_slot[t] or 0.0)
        if v >= 0:                                                # vybíjanie
            delta_kwh = -v / eff_d_safe
        else:                                                      # nabíjanie
            delta_kwh = (-v) * eff_c
        cur += (delta_kwh / cap) * 100.0
        soc_path.append(cur)
    # Doplniť ak vstup bol kratší ako 96
    while len(soc_path) < 97:
        soc_path.append(cur)
    return soc_path


def check_violations(soc_path: List[float],
                       direction_per_slot: List[str],
                       *,
                       soc_min_eff_pct: float,
                       soc_max_eff_pct: float,
                       from_slot: int = 0) -> List[Tuple[int, str, float]]:
    """Per-slot asymetrický check.

    Vráti zoznam violation tuples: (slot_idx, kind, soc_value).
    kind: "below_min" (discharge slot, SOC pod soc_min_eff) alebo "above_max"
    (charge slot, SOC nad soc_max_eff).

    SOC path má 97 hodnôt: index t+1 = koniec slotu t.

    Args:
        soc_path: 97 SOC hodnôt z simulate_soc_unclipped
        direction_per_slot: 96 stringov per slot ("charge"|"discharge"|"idle")
        soc_min_eff_pct: minimálne SOC = soc_min + soc_reserve_pct
        soc_max_eff_pct: maximálne SOC = soc_max - soc_reserve_pct
        from_slot: štartovací slot pre kontrolu (audit od now, ignoruj minulé)
    """
    violations: List[Tuple[int, str, float]] = []
    for t in range(max(0, from_slot), min(96, len(direction_per_slot))):
        soc_end = soc_path[t + 1] if t + 1 < len(soc_path) else soc_path[-1]
        dir_t = direction_per_slot[t]
        if dir_t == "discharge" and soc_end < soc_min_eff_pct - 0.01:
            violations.append((t, "below_min", soc_end))
        elif dir_t == "charge" and soc_end > soc_max_eff_pct + 0.01:
            violations.append((t, "above_max", soc_end))
    return violations


def _scheduled_to_direction(scheduled_kwh: List[float]) -> List[str]:
    """Per slot direction string podľa znamienka.

    + = discharge, − = charge, 0 = idle.
    """
    out: List[str] = []
    for v in scheduled_kwh:
        vv = float(v or 0.0)
        if vv > 0.01:
            out.append("discharge")
        elif vv < -0.01:
            out.append("charge")
        else:
            out.append("idle")
    return out


# ─────────────── Bug AUDIT-CAPACITY (2026-06-10) ───────────────
# Deterministický výpočet max RT podľa kapacity v reserve pásme [eff_min, eff_max].
# User: "audit zrata maximalny mozny vykon pri ktorm je dodrzane podmienky uz
# zobchodvanych 15min. tolerancia by to mohla odfiltrovat".
#
# Rozdiel oproti audit_action():
# - audit_action() robí binárku + 96-slot SOC simuláciu (drahé, oscile na hrane)
# - audit_capacity() = jeden výpočet O(1) podľa headroom v reserve pásme:
#     headroom_charge   = (eff_max - SOC_kwh) − Σ(future planned net charge)
#     headroom_discharge = (SOC_kwh − eff_min) + Σ(future planned net charge)
#   Plus eff_c/eff_d a dt_h prevedú na max RT v kW.
#
# Tým keď SOC narazí na hranu pásma → audit deterministicky vráti 0, žiadna píla.

def audit_capacity(current_soc_pct: float,
                    plan_kw_min: float,
                    rt_intent_kw: float,
                    *,
                    today_state: Dict[str, Any],
                    step_min: int = 15,
                    soc_reserve_pct: float = 0.0,
                    si: int = 0,
                    rt_persistence_slots: int = 4) -> Dict[str, Any]:
    """Vráti max povolený RT (v rovnakom smere ako rt_intent_kw) podľa kapacity.

    Args:
        current_soc_pct: aktuálny SOC v %
        plan_kw_min: plánovaný batt v tejto minúte (+ vybíja, − nabíja)
        rt_intent_kw: navrhované RT navyše (+ vybíja, − nabíja)
        today_state: dict s batt_kwh, eff_c, eff_d, soc_min_pct, soc_max_pct,
                      dam_nomination_kwh (96 slotov), vdt_realized_kwh (96 slotov)
        step_min: 15 alebo 60 (default 15)
        soc_reserve_pct: rezerva pásma (typicky 15)
        si: aktuálny 15-min slot index (0..95) — pre výpočet future planned
        rt_persistence_slots: Bug AUDIT-RT-PERSISTENCE (2026-06-11) — počet 15-min slotov
            (vrátane si) cez ktoré audit predpokladá perzistenciu RT v rovnakom smere.
            User postreh 11:00 hodinu: "neskoro" — audit per slot povolil plný RT lebo
            nevidel že signal pokračuje N slotov + plán nabi N slotov → batt sa preplní
            za 36 min. Fix: budúci plán + RT (× N) tvoria spoločný headroom budget.
            Default 4 = 1h (= predpokladá že RT signál vydrží hodinu).
            Hodnota 1 = pôvodné per-slot správanie.

    Returns:
        {
            "allowed_rt_kw": float (signed, rovnaký smer ako rt_intent_kw),
            "scale_factor": float (0.0..1.0),
            "decision": "accept"|"downscale"|"reject",
            "headroom_charge_kwh": float,
            "headroom_discharge_kwh": float,
            "reason": str,
        }
    """
    out = {"allowed_rt_kw": float(rt_intent_kw), "scale_factor": 1.0,
           "decision": "accept", "headroom_charge_kwh": 0.0,
           "headroom_discharge_kwh": 0.0, "reason": ""}
    if abs(rt_intent_kw) < 1.0:
        return out

    cap = float(today_state.get("batt_kwh") or 1.0)
    eff_c = float(today_state.get("eff_c") or 0.95)
    eff_d = float(today_state.get("eff_d") or 0.95)
    soc_min = float(today_state.get("soc_min_pct") or 5.0)
    soc_max = float(today_state.get("soc_max_pct") or 100.0)
    reserve = max(0.0, min(50.0, float(soc_reserve_pct or 0.0)))
    eff_min = soc_min + reserve     # 20%
    eff_max = soc_max - reserve     # 85%

    dt_h = max(float(step_min), 1.0) / 60.0
    soc_kwh = current_soc_pct / 100.0 * cap
    eff_min_kwh = eff_min / 100.0 * cap
    eff_max_kwh = eff_max / 100.0 * cap

    # Future planned NET charge (signed kWh): + = vybíjanie, − = nabíjanie
    # Pre headroom: nabíjanie spotrebuje "voľnú" hornu kapacitu, vybíjanie ju vráti.
    dam_kwh = list(today_state.get("dam_nomination_kwh") or [0.0] * 96)
    vdt_kwh = list(today_state.get("vdt_realized_kwh") or [0.0] * 96)
    while len(dam_kwh) < 96: dam_kwh.append(0.0)
    while len(vdt_kwh) < 96: vdt_kwh.append(0.0)
    # Bug AUDIT-FUTURE-CHARGE (2026-06-11): pôvodný `future_charge = -min(0, NET)`
    # IGNOROVAL nabíjanie keď v budúcnosti je dostatok vybi (=net positive). Príklad:
    # 11:00 plán -3631 (nabi), 19:00 plán +4000 (vybi). NET = +369 → future_charge = 0 →
    # audit povolí RT nabi navyše plánu → batt prekročí soc_max v 11:00. Užívateľ:
    # "ten usek okolo 11 sice reagoval ale neskoro".
    # Fix: future_charge = ΣUΣ záporných slotov (= celkový plánovaný objem nabi). Symetricky
    # pre discharge. Tým headroom správne odráža kapacitu potrebnú pre PLÁN cez deň.
    future_net_kwh = sum(dam_kwh[i] + vdt_kwh[i] for i in range(int(si), 96))
    future_charge_kwh = sum(-min(0.0, dam_kwh[i] + vdt_kwh[i]) for i in range(int(si), 96))
    future_discharge_kwh = sum(max(0.0, dam_kwh[i] + vdt_kwh[i]) for i in range(int(si), 96))

    # Headroom pre RT charge (=nabíjanie navyše k plánu):
    # SOC_now + RT_charge_kwh + plan_future_net_charge ≤ eff_max_kwh
    # → RT_charge_kwh ≤ eff_max_kwh - SOC_now - future_charge_kwh
    headroom_chg = max(0.0, eff_max_kwh - soc_kwh - future_charge_kwh)

    # Headroom pre RT discharge (=vybíjanie navyše k plánu):
    # SOC_now − RT_discharge_kwh − plan_future_net_discharge ≥ eff_min_kwh
    # → RT_discharge_kwh ≤ SOC_now − eff_min_kwh − future_discharge_kwh
    headroom_dis = max(0.0, soc_kwh - eff_min_kwh - future_discharge_kwh)

    out["headroom_charge_kwh"] = headroom_chg
    out["headroom_discharge_kwh"] = headroom_dis

    # Bug AUDIT-RT-PERSISTENCE (2026-06-11): RT signal je perzistentný cez sloty.
    # Per-slot audit (rt_persistence_slots=1) povolí plný RT pre slot 44 (11:00) napr.
    # -2369 kW × 0.25h / eff_c = 623 kWh nabíjania ⊂ 2730 headroom → accept.
    # ALE RT pokračuje 4 sloty (1h) + plán nabíja 4 sloty → spolu 6124 kWh nabi,
    # batt dosiahne soc_max za 36 min, zvyšok RT je "márny".
    # Fix: násobiť rt_intent kWh × rt_persistence_slots (= predpokladaný horizon RT).
    # Audit potom vidí "RT cez N slotov spotrebuje X kWh" a oreže ak X > headroom.
    persistence = max(1, int(rt_persistence_slots or 1))

    # Max RT v kW (smerom rt_intent_kw)
    if rt_intent_kw < 0:    # nabíjanie navyše
        # Predpokladaná RT spotreba cez N slotov (DC batt): |rt| × dt_h × N / eff_c
        # Ak prekročí headroom_chg, oreže rt_kw tak aby N-slot kumulatívne RT = headroom_chg.
        max_rt_abs_kwh = headroom_chg / max(eff_c, 0.01)  # AC→batt cez eff_c
        max_rt_abs_kw = max_rt_abs_kwh / (dt_h * persistence)
        allowed_signed = -min(abs(rt_intent_kw), max_rt_abs_kw)
    else:                   # vybíjanie navyše
        max_rt_abs_kwh = headroom_dis * eff_d              # batt→AC cez eff_d
        max_rt_abs_kw = max_rt_abs_kwh / (dt_h * persistence)
        allowed_signed = +min(abs(rt_intent_kw), max_rt_abs_kw)

    out["allowed_rt_kw"] = allowed_signed
    scale = (abs(allowed_signed) / abs(rt_intent_kw)) if abs(rt_intent_kw) > 0.01 else 0.0
    out["scale_factor"] = scale
    if scale >= 0.999:
        out["decision"] = "accept"
    elif scale < 0.01:
        out["decision"] = "reject"
        out["reason"] = (f"headroom={headroom_chg:.0f}/{headroom_dis:.0f} kWh "
                          f"SOC={current_soc_pct:.1f}% [{eff_min:.0f}-{eff_max:.0f}], "
                          f"future_net={future_net_kwh:.0f} kWh")
    else:
        out["decision"] = "downscale"
        out["reason"] = (f"capacity limit: max_rt={max_rt_abs_kw:.0f} kW "
                          f"vs intent {abs(rt_intent_kw):.0f} kW "
                          f"(SOC={current_soc_pct:.1f}%, headroom={max_rt_abs_kwh:.0f} kWh)")
    return out


# ─────────────── Main API ───────────────

def audit_action(profile: str,
                  day: str,
                  slot_idx: int,
                  direction: str,
                  kwh_proposed: float,
                  *,
                  source: str = "vdt",
                  today_state: Optional[Dict[str, Any]] = None,
                  step_min: int = 15,
                  current_soc_pct_at_si: Optional[float] = None) -> Dict[str, Any]:
    """Audit navrhovanej akcie pred zápisom.

    Args:
        profile: meno profilu
        day: ISO date (napr. "2026-06-08")
        slot_idx: 15-min slot index 0..95
        direction: "charge" | "discharge"
        kwh_proposed: absolútna hodnota kWh ktorú akcia chce použiť (>= 0)
        source: kategória akcie ("vdt", "rt", "auto_control") pre logging
        today_state: voliteľne predpočítaný compute_current_state dict (cache reuse)
        step_min: 15 alebo 60 (default 15)

    Returns:
        {
            "decision": "accept" | "downscale" | "reject",
            "requested_kwh": float,
            "allowed_kwh": float,
            "reason": str (popis ak nie accept),
            "violations": List[(slot_idx, kind, soc_value)],
            "soc_path_proposed": List[float] (97 hodnôt),
            "soc_min_eff_pct": float,
            "soc_max_eff_pct": float,
            "current_soc_pct": float,
        }

    Konvencie:
    - direction="discharge", kwh_proposed=500 → batt vybíja 500 kWh v slot_idx
    - direction="charge", kwh_proposed=500 → batt nabíja 500 kWh v slot_idx
    """
    out = {"decision": "accept",
           "requested_kwh": float(kwh_proposed),
           "allowed_kwh": float(kwh_proposed),
           "reason": "",
           "violations": [],
           "soc_path_proposed": [],
           "soc_min_eff_pct": 0.0,
           "soc_max_eff_pct": 100.0,
           "current_soc_pct": 0.0,
           "source": source}
    kwh_req = abs(float(kwh_proposed or 0.0))
    if kwh_req <= 0.0:
        out["decision"] = "reject"
        out["reason"] = "kwh_proposed <= 0"
        out["allowed_kwh"] = 0.0
        return out
    direction = (direction or "").lower().strip()
    if direction not in ("charge", "discharge"):
        out["decision"] = "reject"
        out["reason"] = f"unknown direction: {direction!r}"
        out["allowed_kwh"] = 0.0
        return out

    # Načítaj baseline state (D-1 plán + VDT realized + current SOC)
    try:
        if today_state is None:
            import vdt_state as _vs
            d_obj = dt.date.fromisoformat(day)
            today_state = _vs.compute_current_state(profile, today=d_obj)
    except Exception as e:
        out["decision"] = "reject"
        out["reason"] = f"compute_current_state zlyhalo: {e}"
        out["allowed_kwh"] = 0.0
        return out

    if not today_state or not today_state.get("ok"):
        out["decision"] = "reject"
        out["reason"] = "today_state not ok"
        out["allowed_kwh"] = 0.0
        return out

    # Profil parametre
    cap = float(today_state.get("batt_kwh") or 1.0)
    eff_c = float(today_state.get("eff_c") or 0.95)
    eff_d = float(today_state.get("eff_d") or 0.95)
    soc_min = float(today_state.get("soc_min_pct") or 5.0)
    soc_max = float(today_state.get("soc_max_pct") or 100.0)
    # soc_reserve_pct z profile.plan.soc_reserve_pct (Bug #624) — single source of truth
    soc_reserve = 0.0
    try:
        import profiles as _pr
        _prof_obj = _pr.load_profile(profile) or {}
        soc_reserve = float((_prof_obj.get("plan") or {}).get("soc_reserve_pct", 0.0) or 0.0)
    except Exception:
        pass
    soc_reserve = max(0.0, min(50.0, soc_reserve))
    soc_min_eff = soc_min + soc_reserve
    soc_max_eff = soc_max - soc_reserve
    out["soc_min_eff_pct"] = soc_min_eff
    out["soc_max_eff_pct"] = soc_max_eff
    out["current_soc_pct"] = float(today_state.get("current_soc_pct") or 50.0)

    # Scheduled batt-pohľad kWh per slot (D-1 + VDT realized)
    dam_kwh = list(today_state.get("dam_nomination_kwh") or [0.0] * 96)
    vdt_kwh = list(today_state.get("vdt_realized_kwh") or [0.0] * 96)
    # Doplniť na 96 ak menej (safety)
    while len(dam_kwh) < 96: dam_kwh.append(0.0)
    while len(vdt_kwh) < 96: vdt_kwh.append(0.0)
    scheduled_kwh = [float(dam_kwh[i]) + float(vdt_kwh[i]) for i in range(96)]

    # Pridaj navrhovanú akciu do slot_idx
    si = int(max(0, min(95, slot_idx)))
    proposed_sign = +1.0 if direction == "discharge" else -1.0
    proposed_kwh_signed = proposed_sign * kwh_req

    # Bug SOC-AUDIT-START (2026-06-10): pôvodne audit simuloval SOC trajektoriu
    # od slot 0 s `start_soc_pct` (= SOC na 00:00 dňa zo D-1 plánu). Ignoroval
    # všetky historické RT zásahy z livesim — ak ráno RT nabilo z 5% na 42%,
    # audit ďalej myslel že o 07:00 je SOC=20% a zamietol RT vybíjanie.
    # Fix: ak volajúci pošle current_soc_pct_at_si, simuluj LEN budúce sloty
    # (slot si vyššie) od tohto SOC. Minulosť sa nemení.
    if current_soc_pct_at_si is not None:
        _sim_start_soc = float(current_soc_pct_at_si)
        # Sloty 0..si-1 → 0 kWh (nemení SOC, simulácia začína v si)
        _sim_offset = si
    else:
        _sim_start_soc = float(today_state["start_soc_pct"])
        _sim_offset = 0

    def _trial(kwh_try: float) -> Tuple[List[float], List[str]]:
        """Vráti soc_path + direction_per_slot s navrhovanou akciou kwh_try."""
        signed_try = proposed_sign * abs(kwh_try)
        trial_kwh = list(scheduled_kwh)
        trial_kwh[si] = trial_kwh[si] + signed_try
        if _sim_offset > 0:
            # Bypass minulosti — sloty 0..si-1 na 0 (start_soc reprezentuje stav v si)
            sim_kwh = [0.0] * _sim_offset + list(trial_kwh[_sim_offset:])
        else:
            sim_kwh = trial_kwh
        soc_path = simulate_soc_unclipped(
            _sim_start_soc, sim_kwh, cap, eff_c=eff_c, eff_d=eff_d)
        dirs = _scheduled_to_direction(trial_kwh)
        return soc_path, dirs

    # Bug SOC-AUDIT-DELTA (2026-06-10): audit kontroluje SOC trajektoriu na celých
    # 24h dopredu (96 slotov od si). User: "audit sa nerobi na 24 hodinach" =
    # audit sa MÁ robiť na 24h.
    # ALE: porovnávame trajektoriu S RT vs BEZ RT (= len plán + scheduled).
    # Violácia v slot X sa pripíše RT len ak ju RT spôsobil (nebola tam bez RT).
    # Violácie ktoré existujú aj bez RT zásahu → vina plánu (D-1 LP), nie RT,
    # ignoruj. Pre 04:00 RT nabíjanie ak plán už spôsobí violation v 20:45
    # (lebo plán vybíja viac než SOC dovolí), RT 04:00 to nezhoršuje — naopak,
    # RT nabíjanie pridáva SOC.
    # Baseline: 96-slot soc_path s len scheduled (D-1 + VDT), žiadna RT akcia.
    _base_path = simulate_soc_unclipped(
        _sim_start_soc,
        ([0.0]*_sim_offset + list(scheduled_kwh[_sim_offset:])) if _sim_offset > 0 else list(scheduled_kwh),
        cap, eff_c=eff_c, eff_d=eff_d)
    _base_dirs = _scheduled_to_direction(scheduled_kwh)
    _base_viols_set = {(v[0], v[1]) for v in check_violations(
        _base_path, _base_dirs,
        soc_min_eff_pct=soc_min_eff, soc_max_eff_pct=soc_max_eff,
        from_slot=si)}

    # Pokus s plnou požadovanou hodnotou
    soc_path_full, dirs_full = _trial(kwh_req)
    # Filter: ignoruj violácie ktoré existovali aj v baseline (= plán je infeasible,
    # nie RT vina). RT prispel k violation len ak (slot, kind) nie je v baseline.
    violations_full = [v for v in check_violations(
        soc_path_full, dirs_full,
        soc_min_eff_pct=soc_min_eff, soc_max_eff_pct=soc_max_eff,
        from_slot=si)
        if (v[0], v[1]) not in _base_viols_set]
    out["soc_path_proposed"] = soc_path_full

    if not violations_full:
        out["decision"] = "accept"
        return out

    # Binárne hľadanie max_allowed_kwh
    lo, hi = 0.0, kwh_req
    max_iter = 24
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        if mid <= 0.001:
            break
        soc_path_mid, dirs_mid = _trial(mid)
        viol_mid = [v for v in check_violations(
            soc_path_mid, dirs_mid,
            soc_min_eff_pct=soc_min_eff, soc_max_eff_pct=soc_max_eff,
            from_slot=si)
            if (v[0], v[1]) not in _base_viols_set]   # DELTA: ignoruj plán-spôsobené
        if viol_mid:
            hi = mid
        else:
            lo = mid
    allowed = max(0.0, lo)
    # Re-eval final soc_path
    soc_path_final, dirs_final = _trial(allowed)
    out["soc_path_proposed"] = soc_path_final
    out["violations"] = [v for v in check_violations(
        soc_path_final, dirs_final,
        soc_min_eff_pct=soc_min_eff, soc_max_eff_pct=soc_max_eff,
        from_slot=si)
        if (v[0], v[1]) not in _base_viols_set]   # DELTA: ignoruj plán-spôsobené
    if allowed < 0.5:           # menej ako 0.5 kWh nemá zmysel
        out["decision"] = "reject"
        out["allowed_kwh"] = 0.0
        out["reason"] = (f"SOC violation: aj 0 kWh by spôsobilo violation "
                          f"({len(violations_full)} slotov mimo limitov). "
                          f"Možno už existujúci D-1/VDT plán je infeasible.")
    else:
        out["decision"] = "downscale"
        out["allowed_kwh"] = float(allowed)
        v0 = violations_full[0]
        out["reason"] = (f"SOC violation v slot {v0[0]} ({v0[1]} @ {v0[2]:.1f}%). "
                          f"Downscale {kwh_req:.1f}→{allowed:.1f} kWh "
                          f"(limity {soc_min_eff:.1f}–{soc_max_eff:.1f}%).")
    return out


def audit_action_simple(profile: str, day: str, slot_idx: int,
                          direction: str, kwh: float,
                          source: str = "vdt") -> Tuple[str, float]:
    """Skrátený wrapper — vráti len (decision, allowed_kwh) pre rýchle volanie."""
    r = audit_action(profile, day, slot_idx, direction, kwh, source=source)
    return r["decision"], r["allowed_kwh"]


__all__ = [
    "audit_action",
    "audit_action_simple",
    "simulate_soc_unclipped",
    "check_violations",
]
