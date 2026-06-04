"""ZCO Backtest (Phase C3) — keby som zámerne obchodoval cez ZCO namiesto VDT.

Pre každý minulý deň vyhodnotí:
  - DAM nominácie z plan_store (cascade dentrh → plan)
  - Skutočná ZCO cena per slot (z historian CSV)
  - Skutočná VDT clearing cena per slot (z historian CSV)
  - Pre každý slot s DAM commitment porovná:
      A) "Splniť DAM cez VDT" — chýbajúce kWh nakúpiť/predať na VDT
      B) "Nesplniť cez ZCO" — pripustiť odchýlku, zaúčtovať za ZCO
  - Vyberie max(A, B) per slot a sumarizuje.

Output:
  {
    "ok": bool,
    "days": [
      {"date": "YYYY-MM-DD", "profile": str,
       "n_slots": int, "n_zco_wins": int,
       "dam_kwh_commit": float, "dam_kwh_actual": float,
       "profit_vdt_eur": float, "profit_zco_eur": float,
       "savings_eur": float},
      ...
    ],
    "summary": {
      "total_days": int,
      "total_savings_eur": float,
      "avg_savings_per_day_eur": float,
      "n_days_with_zco_wins": int,
    }
  }
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional


def _slot_idx_from_ts(ts) -> int:
    """Z timestamp string vráti 15-min slot index 0..95."""
    try:
        # ts môže byť "YYYY-MM-DD HH:MM:SS" alebo "HH:MM"
        s = str(ts)
        if len(s) >= 16:
            h = int(s[11:13])
            m = int(s[14:16])
        else:
            h = int(s[:2])
            m = int(s[3:5]) if len(s) >= 5 else 0
        return (h * 60 + m) // 15
    except Exception:
        return 0


def _ts_to_slot_idx_map(ts_value_map: Dict[str, float]) -> Dict[int, float]:
    """Konvertuje dict {"YYYY-MM-DD HH:MM:SS": v} → {slot_idx: v}."""
    out: Dict[int, float] = {}
    for ts_str, val in ts_value_map.items():
        try:
            idx = _slot_idx_from_ts(ts_str)
            if 0 <= idx < 96:
                out[idx] = float(val)
        except Exception:
            continue
    return out


def _evaluate_day(date_iso: str, profile: str, grid_fee: float = 22.0) -> Optional[Dict[str, Any]]:
    """Vyhodnotí jeden deň. Vracia None ak chýbajú dáta."""
    try:
        import seps_sk as _seps
        import d1_planner as _d1p
    except Exception:
        return None

    # DAM nominácie (batt-basis) cez cascade
    dam_commits_arr = _d1p.get_dam_commitments(
        dt.date.fromisoformat(date_iso),
        profile=profile, basis="batt"
    )
    if not dam_commits_arr:
        return None

    # Skutočné ceny z historian — ZCO, DAM, VDT
    zco_map = _ts_to_slot_idx_map(_seps.load_okte_zco_for_day(date_iso) or {})
    vdt_map = _ts_to_slot_idx_map(_seps.load_okte_vdt_for_day(date_iso) or {})
    # DAM clearing cena (15-min) — z DataFrame
    dam_clearing_per_slot: Dict[int, float] = {}
    try:
        df_dam = _seps.load_okte_dam_for_day(date_iso)
        if df_dam is not None and not df_dam.empty:
            import pandas as _pd
            # Pre každý 15-min slot priemerná cena
            df_dam = df_dam.copy()
            df_dam["slot_idx"] = df_dam["ts_local"].dt.hour * 4 + df_dam["ts_local"].dt.minute // 15
            for idx, g in df_dam.groupby("slot_idx"):
                vals = g["eur_mwh"].dropna()
                if len(vals) > 0:
                    dam_clearing_per_slot[int(idx)] = float(vals.mean())
    except Exception:
        pass

    if not zco_map or not vdt_map:
        return None   # Aspoň ZCO a VDT musia byť k dispozícii

    n_zco_wins = 0
    n_active = 0
    dam_kwh_commit = 0.0
    profit_vdt_eur = 0.0
    profit_zco_eur = 0.0
    optimal_eur = 0.0

    for slot_idx in range(min(96, len(dam_commits_arr))):
        commit = float(dam_commits_arr[slot_idx] or 0.0)
        if abs(commit) < 0.05:
            continue
        zco = zco_map.get(slot_idx)
        vdt = vdt_map.get(slot_idx)
        dam_clr = dam_clearing_per_slot.get(slot_idx)
        if zco is None or vdt is None:
            continue
        if dam_clr is None:
            # fallback — použijeme priemer ZCO+VDT
            dam_clr = (zco + vdt) / 2
        n_active += 1
        kwh_abs = abs(commit)
        # EXPORT (commit > 0): predáme DAM, ostatok musíme buď doplniť (VDT) alebo nesplniť (ZCO)
        # IMPORT (commit < 0): kupujeme DAM, prebytok musíme buď predať (VDT) alebo nesplniť (ZCO)
        if commit > 0:   # EXPORT commitment
            # A) Splniť: tržba = DAM*kwh, ak ale nemáme dosť → musíme dokúpiť VDT_ask
            #    Net: DAM*kwh - (VDT + fee)*kwh / 1000
            profit_a = (dam_clr - vdt - grid_fee) * kwh_abs / 1000.0
            # B) Nesplniť: tržba DAM*kwh, ale zaplatíme ZCO penalty
            #    Net: DAM*kwh - ZCO*kwh / 1000
            profit_b = (dam_clr - zco) * kwh_abs / 1000.0
        else:   # IMPORT commitment
            # A) Splniť: nákup DAM, ak prebytok → predáme VDT_bid
            #    Net: -DAM*kwh + (VDT - fee)*kwh / 1000
            profit_a = (-dam_clr + vdt - grid_fee) * kwh_abs / 1000.0
            # B) Nesplniť: predáme za ZCO
            profit_b = (-dam_clr + zco) * kwh_abs / 1000.0
        profit_vdt_eur += profit_a
        profit_zco_eur += profit_b
        if profit_b > profit_a:
            n_zco_wins += 1
        optimal_eur += max(profit_a, profit_b)
        dam_kwh_commit += kwh_abs

    savings = optimal_eur - profit_vdt_eur
    return {
        "date": date_iso,
        "profile": profile,
        "n_slots": n_active,
        "n_zco_wins": n_zco_wins,
        "dam_kwh_commit": round(dam_kwh_commit, 1),
        "profit_vdt_eur": round(profit_vdt_eur, 2),
        "profit_zco_eur": round(profit_zco_eur, 2),
        "optimal_eur": round(optimal_eur, 2),
        "savings_eur": round(savings, 2),
    }


def run_backtest(date_from: dt.date, date_to: dt.date,
                   profile: str = "default",
                   grid_fee: float = 22.0) -> Dict[str, Any]:
    """Spustí ZCO backtest pre rozsah dátumov."""
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    days = []
    cur = date_from
    while cur <= date_to:
        res = _evaluate_day(cur.isoformat(), profile, grid_fee=grid_fee)
        if res is not None:
            days.append(res)
        cur += dt.timedelta(days=1)

    if not days:
        return {"ok": False, "error": "Žiadne dni s plánom + ZCO/VDT dátami v rozsahu",
                "days": [], "summary": {}}

    total_savings = sum(d["savings_eur"] for d in days)
    total_vdt = sum(d["profit_vdt_eur"] for d in days)
    total_optimal = sum(d["optimal_eur"] for d in days)
    n_with_wins = sum(1 for d in days if d["n_zco_wins"] > 0)

    return {
        "ok": True,
        "days": days,
        "summary": {
            "total_days": len(days),
            "total_dam_kwh": round(sum(d["dam_kwh_commit"] for d in days), 0),
            "total_vdt_eur": round(total_vdt, 2),
            "total_optimal_eur": round(total_optimal, 2),
            "total_savings_eur": round(total_savings, 2),
            "avg_savings_per_day_eur": round(total_savings / max(1, len(days)), 2),
            "n_days_with_zco_wins": n_with_wins,
            "pct_days_with_wins": round(100 * n_with_wins / max(1, len(days)), 1),
        }
    }


if __name__ == "__main__":
    # Smoke
    today = dt.date.today()
    r = run_backtest(today - dt.timedelta(days=7), today - dt.timedelta(days=1),
                     profile="Simulacia_Coop")
    print("ok:", r.get("ok"))
    print("summary:", r.get("summary"))
    for d in r.get("days", [])[:5]:
        print(f"  {d['date']}: DAM={d['dam_kwh_commit']:.0f} kWh, "
              f"{d['n_zco_wins']}/{d['n_slots']} ZCO wins, savings=+{d['savings_eur']:.2f}€")
