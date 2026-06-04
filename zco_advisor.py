"""ZCO Advisor (Phase C1) — analyzuje kedy je výhodné NESPLNIŤ DAM commitment
a obchodovať odchýlku cez ZCO namiesto VDT.

Logika (SK, jednosmenná ZCO konvencia):
  ZCO = Zúčtovacia cena odchýlky (€/MWh) — jedna cena za 15-min slot, použitá
  pre obojstrannú zúčtovaciu cenu odchýlky subjektu.

  Subjekt s DAM kontraktom má 2 cesty pri nedodaní/predoddaní:
    A) "Splniť DAM" — chýbajúce kWh nakúpiť na VDT (ak deficit) alebo predať
       (ak prebytok). Cena: VDT_ask / VDT_bid.
    B) "Nesplniť DAM" — pripustiť odchýlku, zaúčtovať za ZCO.

  Pre EXPORT commitment N kWh (DAM predaj):
    Profit z nesplnenia = N × (VDT_ask − ZCO) / 1000 €
    Ak VDT_ask > ZCO → oplatí sa nesplniť (lacnejšie zaplatiť ZCO penalty
    ako nakúpiť drahé kWh na VDT).

  Pre IMPORT commitment N kWh (DAM nákup):
    Profit z nesplnenia = N × (ZCO − VDT_bid) / 1000 €
    Ak ZCO > VDT_bid → oplatí sa nesplniť (predáme prebytok drahšie cez ZCO).

ZCO predikcia: jednoduchý priemer cez posledných N dní v rovnakom slote
(0-95 indexov). Pre dnes/zajtra nie sú aktuálne ZCO ceny — len predikcia.
"""
from __future__ import annotations

import datetime as _dt
import os
from typing import Any, Dict, List, Optional


def _slot_idx_from_ts(ts) -> int:
    """Z timestamp (datetime, string, pd.Timestamp) vráti 15-min slot index 0..95."""
    if hasattr(ts, "hour") and hasattr(ts, "minute"):
        return (int(ts.hour) * 60 + int(ts.minute)) // 15
    try:
        import pandas as _pd
        t = _pd.Timestamp(ts)
        return (int(t.hour) * 60 + int(t.minute)) // 15
    except Exception:
        # fallback: parse "HH:MM" alebo "HH:MM-HH:MM"
        s = str(ts)[:5]
        try:
            h, m = int(s[:2]), int(s[3:5])
            return (h * 60 + m) // 15
        except Exception:
            return 0


def _predict_from_deviation_profile_sk(date_iso: str) -> Optional[Dict[int, float]]:
    """Použije out/sk/deviation_profile.json (PV-bucket + weekday split) na
    predikciu ZCO per 15-min slot. Algoritmus:
      expected_zco_dt = estimate_day(prof, date) — očakávaný rozdiel ZCO − DT
      predicted_zco = DT_clearing[slot] + expected_zco_dt[slot]

    Vracia None ak profile chýba alebo DT clearing nie je dostupný."""
    try:
        import deviation_stats as _ds
        import os as _os
        prof_path = _ds.PROFILE_PATH_SK
        if not _os.path.exists(prof_path):
            return None
        prof = _ds.load_profile(prof_path)
    except Exception:
        return None

    # Skús získať DT clearing pre cieľový deň
    try:
        import seps_sk as _seps
        dt_map_raw = _seps.load_okte_dt_for_day(date_iso) or {}
    except Exception:
        dt_map_raw = {}
    # ts → slot_idx
    dt_per_slot: Dict[int, float] = {}
    for ts_str, val in dt_map_raw.items():
        try:
            idx = _slot_idx_from_ts(ts_str[11:16] if len(ts_str) >= 16 else ts_str)
            if 0 <= idx < 96:
                dt_per_slot[idx] = float(val)
        except Exception:
            continue

    # estimate_day vráti pole expected_zco_dt po 15-min slotoch
    try:
        target_date = _dt.date.fromisoformat(date_iso) if isinstance(date_iso, str) else date_iso
        # pv_forecast_kwh = None → použijeme bucket="vše"
        exp_arr, _bucket, _dtype = _ds.estimate_day(prof, target_date,
                                                     pv_forecast_kwh=None, step_min=15)
    except Exception:
        return None

    out: Dict[int, float] = {}
    for slot_idx in range(min(96, len(exp_arr))):
        # Ak nemáme DT pre daný deň, použijeme priemer DT z profilu (hour_only.dt_mean)
        if slot_idx in dt_per_slot:
            dt_val = dt_per_slot[slot_idx]
        else:
            # Fallback: dt_mean z profilu pre tú hodinu
            hour = (slot_idx * 15) // 60
            dt_val = next((float(r["dt_mean"]) for r in prof.get("hour_only", [])
                           if int(r["hour"]) == hour), None)
            if dt_val is None:
                continue
        out[slot_idx] = float(dt_val + exp_arr[slot_idx])
    return out if out else None


def predict_zco_for_slots(date_iso: str, days_window: int = 7,
                            method: str = "auto") -> Dict[int, float]:
    """Predikcia ZCO ceny per 15-min slot (0..95).

    Args:
        date_iso: cieľový dátum.
        days_window: počet historických dní pre fallback 7-day mean.
        method: "auto" (SK profile → fallback 7-day),
                "sk_profile" (len SK profile, prázdne ak nie je),
                "mean" (len 7-day mean).

    Vracia:
        Dict[slot_idx, predicted_zco_eur_mwh]. Prázdny dict ak nie sú dáta.
    """
    # Pokus o SK deviation profile (lepšia metóda)
    if method in ("auto", "sk_profile"):
        sk_pred = _predict_from_deviation_profile_sk(date_iso)
        if sk_pred:
            return sk_pred
        if method == "sk_profile":
            return {}
    # Fallback: 7-day mean (jednoduchá metóda)
    try:
        import seps_sk as _seps
    except Exception:
        return {}

    today = _dt.date.fromisoformat(date_iso) if isinstance(date_iso, str) else date_iso
    # ZCO je publikované len pre D-1 deň → začneme od (today - 2)
    samples: Dict[int, List[float]] = {}
    for offset in range(1, days_window + 1):
        d = today - _dt.timedelta(days=offset)
        try:
            zco_map = _seps.load_okte_zco_for_day(d.isoformat())
        except Exception:
            continue
        if not zco_map:
            continue
        for ts_str, val in zco_map.items():
            try:
                idx = _slot_idx_from_ts(ts_str[11:16] if len(ts_str) >= 16 else ts_str)
                v = float(val)
                samples.setdefault(idx, []).append(v)
            except Exception:
                continue

    if not samples:
        return {}

    out: Dict[int, float] = {}
    for idx, arr in samples.items():
        if arr:
            out[idx] = float(sum(arr) / len(arr))
    return out


def compute_zco_opportunities(snapshot, dam_commits: List[float],
                                full_plan: List[Dict[str, Any]],
                                date_iso: str,
                                grid_fee: float = 22.0,
                                days_window: int = 7,
                                top_n: int = 8,
                                ) -> Dict[str, Any]:
    """Pre každý future slot s DAM commitment ≠ 0 vyhodnotí, či je výhodnejšie
    NESPLNIŤ DAM (zaúčtovať cez ZCO) ako splniť cez VDT.

    Args:
        snapshot: market snapshot DataFrame (s ob_best_bid/ask_eur_mwh, start_local).
        dam_commits: kWh per slot (export>0, import<0) zo snapshotu.
        full_plan: výstup advisora s polom slotov (na referenciu cien).
        date_iso: dnešný dátum (pre ZCO predikciu).
        grid_fee: €/MWh fee, použije sa pri prepočte VDT ceny (importné/exportné poplatky).
        days_window: koľko dní spätne použiť pre ZCO predikciu.
        top_n: koľko najlepších opportunities vrátiť (sorted by profit desc).

    Vracia:
        {
          "ok": bool,
          "zco_pred_status": str,                # diagnostika predikcie
          "n_predicted_slots": int,
          "opportunities": [
              {"slot": "HH:MM-HH:MM", "slot_idx": int,
               "dam_kwh": float, "dam_price": float,
               "zco_pred": float, "vdt_bid": float, "vdt_ask": float,
               "profit_eur": float, "side": "export"|"import"},
              ...
          ],
          "total_profit_eur": float,            # suma top-N profitov
        }
    """
    try:
        import pandas as _pd
    except Exception:
        return {"ok": False, "error": "pandas nedostupný", "opportunities": []}

    zco_pred = predict_zco_for_slots(date_iso, days_window=days_window)
    if not zco_pred:
        return {"ok": False,
                "zco_pred_status": f"(žiadne ZCO dáta v {days_window} dňoch späť)",
                "n_predicted_slots": 0,
                "opportunities": [],
                "total_profit_eur": 0.0}

    n = min(len(snapshot), len(dam_commits) if dam_commits else 0,
            len(full_plan) if full_plan else 0)
    opps: List[Dict[str, Any]] = []
    for i in range(n):
        commit = float(dam_commits[i] or 0.0)
        if abs(commit) < 0.05:
            continue
        try:
            row = snapshot.iloc[i]
            start = row["start_local"]
            slot_idx = _slot_idx_from_ts(start)
            if slot_idx not in zco_pred:
                continue
            zco = float(zco_pred[slot_idx])
            # VDT bid/ask z snapshotu (orderbook): môže byť NaN.
            bid = row.get("ob_best_bid_eur_mwh") if hasattr(row, "get") else None
            ask = row.get("ob_best_ask_eur_mwh") if hasattr(row, "get") else None
            try:
                bid = float(bid) if bid is not None and not _pd.isna(bid) else None
            except Exception:
                bid = None
            try:
                ask = float(ask) if ask is not None and not _pd.isna(ask) else None
            except Exception:
                ask = None
            # Fallback: ak VDT chýba, použijeme DAM cenu z full_plan ako proxy
            fp_row = full_plan[i] if i < len(full_plan) else {}
            dam_price = fp_row.get("sell_price") if commit > 0 else fp_row.get("buy_price")
            if dam_price is None or (isinstance(dam_price, float) and _pd.isna(dam_price)):
                dam_price = fp_row.get("price") or 0.0
            try:
                dam_price = float(dam_price)
            except Exception:
                dam_price = 0.0

            # Profit z nesplnenia (€)
            kwh_abs = abs(commit)
            if commit > 0:   # EXPORT — porovnaj VDT_ask vs ZCO
                # Splniť: nakúpime chýbajúce na VDT za ask, profit = (DAM - VDT_ask - fee) * kwh / 1000
                # Nesplniť: zaplatíme ZCO, profit = (DAM - ZCO) * kwh / 1000 (žiadny grid fee navyše)
                # Rozdiel: (VDT_ask + fee - ZCO) * kwh / 1000
                vdt_proxy = ask if ask is not None else dam_price
                rozdiel = (vdt_proxy + grid_fee) - zco
                profit = rozdiel * kwh_abs / 1000.0
                side = "export"
            else:   # IMPORT — porovnaj ZCO vs VDT_bid
                # Splniť: predáme prebytok na VDT za bid
                # Nesplniť: predáme za ZCO
                # Profit z nesplnenia: (ZCO - VDT_bid - fee) * kwh / 1000
                vdt_proxy = bid if bid is not None else dam_price
                rozdiel = zco - (vdt_proxy + grid_fee)
                profit = rozdiel * kwh_abs / 1000.0
                side = "import"

            # Slot label "HH:MM-HH:MM"
            try:
                t = _pd.Timestamp(start)
                slot_label = (t.strftime("%H:%M") + "-"
                              + (t + _pd.Timedelta(minutes=15)).strftime("%H:%M"))
            except Exception:
                slot_label = fp_row.get("slot", "")

            opps.append({
                "slot": slot_label, "slot_idx": slot_idx,
                "dam_kwh": commit, "dam_price": dam_price,
                "zco_pred": zco, "vdt_bid": bid, "vdt_ask": ask,
                "profit_eur": float(profit), "side": side,
            })
        except Exception:
            continue

    # Sort: najziskovejšie (profit > 0) hore
    opps_pos = [o for o in opps if o["profit_eur"] > 0.01]
    opps_pos.sort(key=lambda x: x["profit_eur"], reverse=True)
    top = opps_pos[:top_n]
    total = sum(o["profit_eur"] for o in top)

    return {
        "ok": True,
        "zco_pred_status": f"✓ ZCO predikcia z {days_window} dní späť ({len(zco_pred)} slotov)",
        "n_predicted_slots": len(zco_pred),
        "n_checked": len(opps),
        "n_profitable": len(opps_pos),
        "opportunities": top,
        "total_profit_eur": total,
    }


if __name__ == "__main__":
    # Smoke test
    import datetime as dt
    today = dt.date.today()
    print(f"=== ZCO predikcia pre {today} (7 dní späť) ===")
    pred = predict_zco_for_slots(today.isoformat(), days_window=7)
    if pred:
        print(f"  {len(pred)} slotov má predikciu")
        # Ukáž 5 vzorkových
        for idx in sorted(pred.keys())[:5]:
            h, m = (idx * 15) // 60, (idx * 15) % 60
            print(f"  slot {idx:2d} ({h:02d}:{m:02d}): {pred[idx]:.1f} €/MWh")
    else:
        print("  Žiadne ZCO dáta — chýba historian backfill?")
