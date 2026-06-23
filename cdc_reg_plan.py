# -*- coding: utf-8 -*-
"""cdc_reg_plan.py — prepis plánu batérie na 15-min regulačné pásma (GL/RL/SL).

Vstup = D-1 plán profilu batérie (schedule.batt_kw [+vybíja/−nabíja], schedule.soc_pct).
Výstup = tabuľka 96 (alebo 24×4) slotov s pásmami pre regulačné filtre:
  SL_min/SL_max          — povolený rozsah SOC (%) v danom slote
  GL_active/GL_min/base/max — prah odberného miesta (kW) — len ak riadime celé OM
  RL_active/RL_min/base/max — výkon batérie (normalizovaný −1…+1, ×Pnom = kW)

Mapovanie (v1 — ľahko upraviteľné):
  • RL_base = clip(batt_kw_slot / Pnom, −1, +1)   (+ = vybíja, − = nabíja)
  • RT režim:
      'fixed' → RL_min = RL_base = RL_max  (žiadna RT závislosť — pevný výkon)
      'band'  → RL_min = clip(base − w, −1, 1), RL_max = clip(base + w, −1, 1)
                (RT signál −1…+1 plynule škáluje v rámci pásma)
  • SL = plánovaný SOC ± soc_margin (clip 0…100). Ak plán SOC nemá → 0…100.
  • mode='battery' → GL_active=0 (riadime len batériu),
    mode='point'   → GL_active=1 (riadime celé odberné miesto; GL hodnoty TODO podľa prahu).

POZN.: pravidlá sú v JEDNEJ funkcii — keď Radoslav doladí presný prepis, mení sa tu.
"""
from __future__ import annotations
import datetime as dt
from typing import Optional, List, Dict, Any


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def rl_bounds(base: float, rt: str):
    """Vráti (rl_min, rl_max) pre danú bázu a režim RT.
      'fixed' → (base, base)  — striktne plán.
      'band'  → smerové asymetrické (plán je floor, RT len bezpečným smerom):
                 nabíja (base<0): (-1, base); vybíja (base>0): (base, +1);
                 nečinné (base=0): (-1, +1)."""
    if rt == "band":
        if base < 0:
            return -1.0, base
        if base > 0:
            return base, 1.0
        return -1.0, 1.0
    return base, base


def _plan_arrays(battery: Dict[str, Any], day_iso: str):
    """Vráti (batt_kw[96], soc_pct[96]) z plánu profilu batérie pre daný deň.
    Chýbajúce → None polia. 96 = 15-min sloty (60-min plán sa upsampluje ×4)."""
    n = 96
    batt = [None] * n
    soc = [None] * n
    try:
        from control.plan_source import profile_name_for
        import plan_store as _ps
    except Exception:
        return batt, soc
    name = profile_name_for(battery)
    if not name:
        return batt, soc
    plan = _ps.load_plan_safe(day_iso, step_min=15, kind="dentrh", profile=name)
    step = 15
    if plan is None:
        plan = _ps.load_plan_safe(day_iso, step_min=60, kind="plan", profile=name)
        step = 60
    if plan is None:
        return batt, soc
    sched = plan.get("schedule") or {}
    bk = sched.get("batt_kw") or sched.get("batt") or []
    sc = sched.get("soc_pct") or []
    for i in range(n):
        idx = (i // 4) if step == 60 else i
        if idx < len(bk) and bk[idx] is not None:
            try:
                batt[i] = float(bk[idx])
            except (TypeError, ValueError):
                pass
        if idx < len(sc) and sc[idx] is not None:
            try:
                soc[i] = float(sc[idx])
            except (TypeError, ValueError):
                pass
    return batt, soc


def build_band_table(battery: Dict[str, Any], day_iso: Optional[str] = None, *,
                     mode: str = "battery", rt: str = "fixed",
                     rl_band_w: float = 1.0, soc_margin: float = 7.0
                     ) -> List[Dict[str, Any]]:
    """Postaví 15-min tabuľku regulačných pásiem z plánu batérie.

    mode: 'battery' (len RL) | 'point' (aj GL). rt: 'fixed' | 'band'.
    """
    if day_iso is None:
        day_iso = dt.date.today().isoformat()
    pnom = float(battery.get("batt_kw") or 0.0)
    batt, soc = _plan_arrays(battery, day_iso)
    point = (mode == "point")
    rows: List[Dict[str, Any]] = []
    for i in range(96):
        h, m = divmod(i * 15, 60)
        # RL z plánovaného výkonu
        if pnom > 0 and batt[i] is not None:
            base = _clip(batt[i] / pnom, -1.0, 1.0)
        else:
            base = 0.0
        # smerové asymetrické pásmo (plán floor) alebo pevné — viď rl_bounds()
        rmin, rmax = rl_bounds(base, rt)
        # SL z plánovaného SOC ± margin
        if soc[i] is not None:
            slmin = _clip(soc[i] - soc_margin, 0.0, 100.0)
            slmax = _clip(soc[i] + soc_margin, 0.0, 100.0)
        else:
            slmin, slmax = 0.0, 100.0
        rows.append({
            "slot": i,
            "time": f"{h:02d}:{m:02d}",
            "sl_min": round(slmin, 1), "sl_max": round(slmax, 1),
            "gl_active": 1 if point else 0,
            "gl_min": 0.0, "gl_base": 0.0, "gl_max": 0.0,
            "rl_active": 1,
            "rl_min": round(rmin, 3), "rl_base": round(base, 3), "rl_max": round(rmax, 3),
            "batt_kw": None if batt[i] is None else round(batt[i], 1),
            "soc_pct": None if soc[i] is None else round(soc[i], 1),
        })
    return rows


# Mapovanie logical RL/GL/SL → CDC write tag kľúč (z cdc.tags_write)
BAND_TO_TAG = {
    "gl_active": "reg_gl_act_plan", "gl_min": "reg_gl_min_plan",
    "gl_base": "reg_gl_base_plan", "gl_max": "reg_gl_max_plan",
    "rl_active": "reg_rl_act_plan", "rl_min": "reg_rl_min_plan",
    "rl_base": "reg_rl_base_plan", "rl_max": "reg_rl_max_plan",
    "sl_min": "reg_sl_min_plan", "sl_max": "reg_sl_max_plan",
}


__all__ = ["build_band_table", "BAND_TO_TAG"]
