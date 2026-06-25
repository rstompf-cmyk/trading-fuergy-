"""Denný settlement card — sumarizuje hospodárenie konkrétneho profilu za deň.

Rozkladá denný čistý výsledok na tri zložky:
1. **D-1 DAM plán**: záväzná nominácia × reálna DAM clearing cena.
2. **VDT extras (paper trades)**: intraday obchody (BUY/SELL/CURTAIL_FTV/LOAD_COVER)
   uzavreté nad rámec D-1 plánu.
3. **Odchýlka × ZCO**: settlement reality vs. obchodu (deficit/surplus × ZCO).

Pre dnešný deň sú niektoré zložky stále priebežné (paper trades nakaďjú, ZCO
ešte nie je publikovaná — orientačne z predikcie). Pre minulé dni je settlement
finálny.
"""
from __future__ import annotations
import csv
import datetime as dt
import os
from typing import Optional, Dict, List, Any


def _data_root() -> str:
    try:
        import market as _mk
        return os.path.dirname(_mk.data_dir().rstrip("/").rstrip(os.sep)) or "out"
    except Exception:
        return "out"


def _market() -> str:
    try:
        import market as _mk
        return (_mk.active_market() or "sk").lower()
    except Exception:
        return "sk"


# ---------------------------------------------------------------------------
# 1) D-1 DAM plán — záväzný objem × reálna DAM cena
# ---------------------------------------------------------------------------

def _dam_settlement(date_iso: str, profile: str) -> Dict[str, Any]:
    """Vypočíta zisk z D-1 DAM plánu pre profil + dátum.

    Zdroje:
      - DAM nominácia per slot: d1_planner.get_dam_commitments (basis="grid")
      - DAM reálne ceny per 15-min slot: settlement.get_dt_real_quarterly

    Vracia:
      {
        "ok": bool,
        "kwh_sell": float,      # celkový export (kladné kWh)
        "kwh_buy": float,       # celkový import (kladné kWh; v plán je záporné)
        "revenue_eur": float,   # tržba z predaja
        "cost_eur": float,      # náklad na nákup
        "net_eur": float,       # revenue - cost
        "price_avg_sell": float | None,
        "price_avg_buy": float | None,
        "diag": str,
      }
    """
    out = {"ok": False, "kwh_sell": 0.0, "kwh_buy": 0.0, "revenue_eur": 0.0,
           "cost_eur": 0.0, "net_eur": 0.0,
           "price_avg_sell": None, "price_avg_buy": None,
           "diag": ""}
    try:
        import d1_planner as _dp
        import settlement as _sett
        import numpy as _np
        date = dt.date.fromisoformat(date_iso)
        commits = _dp.get_dam_commitments(date, profile=profile, basis="grid")
        if commits is None or len(commits) == 0:
            out["diag"] = "D-1 plán nedostupný"
            return out
        # Reálne DAM ceny pre slot (15-min granularita, ak SK/CZ má hodinovku → repeat 4×)
        prices = _sett.get_dt_real_quarterly(date_iso)
        has_prices = prices is not None and len(prices) > 0
        if not has_prices:
            # Fallback: predikované DAM ceny z plánu (predpoklad)
            out["diag"] = "Reálne DAM ceny ešte nie sú publikované — orientačne."
            try:
                import plan_store as _ps
                pdata = _ps.load_plan_safe(date_iso, 15, "plan") \
                          or _ps.load_plan_safe(date_iso, 15, "dentrh") \
                          or _ps.load_plan_safe(date_iso, 60, "plan")
                if pdata is not None:
                    sch = pdata.get("schedule") or {}
                    price_h = sch.get("price_eur") or []
                    # Expand 24→96 ak treba
                    if len(price_h) == 24:
                        prices = [p for p in price_h for _ in range(4)]
                    elif len(price_h) == 96:
                        prices = list(price_h)
                    has_prices = prices is not None and len(prices) > 0
            except Exception:
                pass
        if not has_prices or len(prices) < 96:
            out["diag"] = out["diag"] or "DAM ceny nedostupné"
            return out
        # Normalizuj prices na list
        if isinstance(prices, _np.ndarray):
            prices = prices.tolist()

        sell_kwh = sell_rev = buy_kwh = buy_cost = 0.0
        for i, kwh in enumerate(commits[:96]):
            if i >= len(prices) or prices[i] is None:
                continue
            try:
                p = float(prices[i])
            except Exception:
                continue
            try:
                v = float(kwh or 0)
            except Exception:
                continue
            if v > 0:
                sell_kwh += v
                sell_rev += v * p / 1000.0
            elif v < 0:
                buy_kwh += -v
                buy_cost += -v * p / 1000.0
        out["ok"] = True
        out["kwh_sell"] = sell_kwh
        out["kwh_buy"] = buy_kwh
        out["revenue_eur"] = sell_rev
        out["cost_eur"] = buy_cost
        out["net_eur"] = sell_rev - buy_cost
        if sell_kwh > 0:
            out["price_avg_sell"] = (sell_rev * 1000.0) / sell_kwh
        if buy_kwh > 0:
            out["price_avg_buy"] = (buy_cost * 1000.0) / buy_kwh
    except Exception as e:
        out["diag"] = f"chyba: {e}"
    return out


# ---------------------------------------------------------------------------
# 2) VDT extras (paper trades)
# ---------------------------------------------------------------------------

def _vdt_settlement(date_iso: str, profile: str) -> Dict[str, Any]:
    """Vypočíta zisk z VDT extras (paper trades) pre profil + dátum.

    Zdroj: out/sk/vdt_paper_trades.csv filtrovaný na profile + dátum.

    Vracia:
      {
        "ok": bool,
        "buy_kwh": float, "sell_kwh": float,
        "curtail_ftv_kwh": float, "load_cover_kwh": float,
        "revenue_eur": float,   # zo SELL/LOAD_COVER
        "cost_eur": float,      # z BUY (= výdaj za nákup z VDT)
        "saved_eur": float,     # z CURTAIL_FTV (úspora — neminul si grid_fee)
        "net_eur": float,       # revenue - cost + saved
        "n_trades": int,
        "diag": str,
      }
    """
    out = {"ok": False, "buy_kwh": 0.0, "sell_kwh": 0.0,
           "curtail_ftv_kwh": 0.0, "load_cover_kwh": 0.0,
           "revenue_eur": 0.0, "cost_eur": 0.0, "saved_eur": 0.0,
           "net_eur": 0.0, "n_trades": 0, "diag": ""}
    if _market() == "cz":
        out["diag"] = "CZ — VDT nie je dostupné"
        return out
    path = os.path.join(_data_root(), "sk", "vdt_paper_trades.csv")
    if not os.path.exists(path):
        out["diag"] = "Žiadne paper trades zatiaľ"
        return out

    try:
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        out["diag"] = f"chyba čítania CSV: {e}"
        return out

    for r in rows:
        ts = (r.get("ts", "") or "")[:10]
        if ts != date_iso:
            continue
        if (r.get("profile") or "") != profile:
            continue
        try:
            kwh = float(r.get("kwh", 0) or 0)
        except Exception:
            kwh = 0.0
        if abs(kwh) < 0.5:
            continue
        try:
            price = float(r.get("price_predicted_eur", 0) or 0)
        except Exception:
            price = 0.0
        action = (r.get("action") or "").upper()
        out["n_trades"] += 1
        # CHARGE = BUY z VDT (náklad), DISCHARGE = SELL na VDT (príjem)
        if action in ("BUY", "CHARGE"):
            out["buy_kwh"] += kwh
            out["cost_eur"] += kwh * price / 1000.0
        elif action in ("SELL", "DISCHARGE"):
            out["sell_kwh"] += kwh
            out["revenue_eur"] += kwh * price / 1000.0
        elif action == "CURTAIL_FTV":
            out["curtail_ftv_kwh"] += kwh
            # úspora: neminul si grid_fee za FTV ktoré by si predal pod nákladmi
            # (delta_profit_eur je v reason, ale tu len kwh × small)
            out["saved_eur"] += 0.0   # konzervatívne — bez extra cost
        elif action == "LOAD_COVER":
            out["load_cover_kwh"] += kwh
            # úspora: namiesto kúpy z DAM kúpil si lacnejšie z VDT
            out["saved_eur"] += 0.0   # konzervatívne — bez extra cost
    out["ok"] = (out["n_trades"] > 0) or (path is not None)
    out["net_eur"] = out["revenue_eur"] - out["cost_eur"] + out["saved_eur"]
    return out


# ---------------------------------------------------------------------------
# 3) Odchýlka × ZCO — settlement reality vs. obchodu
# ---------------------------------------------------------------------------

def _deviation_settlement(date_iso: str, profile: str) -> Dict[str, Any]:
    """Vypočíta náklad/výnos z odchýlky voči Obchodu × ZCO.

    Zdroj: livesim trace pre dnes (z r["today_trace"]) alebo z livesim CSV pre minulé dni.
    ZCO: settlement.get_zco_for_day (publikuje sa s D+1 oneskorením pre SK).

    Vracia:
      {
        "ok": bool,
        "kwh_deficit": float,    # |dev_kwh| pri dev<0 (nedodali sme)
        "kwh_surplus": float,    # dev_kwh pri dev>0 (preplnili sme)
        "zco_avg": float | None,
        "cost_eur": float,       # vždy negatívny pre operátora (penalty)
        "settled": bool,         # True ak ZCO real je publikovaná (minulé dni)
        "diag": str,
      }
    """
    out = {"ok": False, "kwh_deficit": 0.0, "kwh_surplus": 0.0,
           "zco_avg": None, "cost_eur": 0.0, "settled": False, "diag": ""}
    try:
        import settlement as _sett
        zco_map = _sett.get_zco_for_day(date_iso)
    except Exception:
        zco_map = {}

    if not zco_map:
        out["diag"] = "ZCO real ešte nepublikovaná (orientačne sa použije predikcia)"
        try:
            import zco_advisor as _zaa
            zpred = _zaa.predict_zco_for_slots(date_iso, method="auto")
            # zpred: slot_idx → ZCO eur/MWh
            zco_map = {f"{i//4:02d}:{(i%4)*15:02d}": v for i, v in zpred.items()}
        except Exception:
            zco_map = {}
    else:
        out["settled"] = True

    if not zco_map:
        out["diag"] = "ZCO úplne nedostupné"
        return out

    # Konverzia kľúčov: prijímam dict ako {slot_idx: val} alebo {"HH:MM": val}
    # alebo {iso_ts: val}. Normalizuj na slot_idx 0..95.
    slot_zco: Dict[int, float] = {}
    for k, v in zco_map.items():
        try:
            if isinstance(k, int):
                slot_zco[k] = float(v)
            elif isinstance(k, str):
                if len(k) >= 5 and k[2] == ":":
                    hh = int(k[:2]); mm = int(k[3:5])
                    slot_zco[(hh*60+mm)//15] = float(v)
                elif "T" in k:
                    hh = int(k[11:13]); mm = int(k[14:16])
                    slot_zco[(hh*60+mm)//15] = float(v)
        except Exception:
            continue

    if not slot_zco:
        out["diag"] = "ZCO formát neznámy"
        return out

    out["zco_avg"] = sum(slot_zco.values()) / max(len(slot_zco), 1)

    # Odchýlka per slot — agreguj z livesim trace
    # Skús dnes z `r["today_trace"]` — to však nemáme priamo, takže ideme z livesim CSV.
    try:
        import livesim as lsim
        import pandas as pd
        # Pokus rôzne case-y
        kwh_def = 0.0; kwh_sur = 0.0; total_pen = 0.0
        for case in ("dt_15min", "plan_d1"):
            try:
                df = lsim.load_series(case, port=os.environ.get("APP_PORT", "8000"),
                                       day=date_iso, max_points=10**9)
            except Exception:
                continue
            if df is None or df.empty:
                continue
            # Po slotoch — zoskupuj minúty do 15-min slotov
            df = df.copy()
            df["slot_idx"] = (df["time"].dt.hour * 4 + df["time"].dt.minute // 15)
            grp = df.groupby("slot_idx")
            for slot_idx, sub in grp:
                if slot_idx not in slot_zco:
                    continue
                zco = slot_zco[slot_idx]
                # dev_kw — preferuj reálnu odchýlku ak existuje; fallback na plan vs threshold
                dev_col = None
                for c in ("dev_kw", "dev_min_kw", "dev_15min_kw"):
                    if c in sub.columns and sub[c].notna().any():
                        dev_col = c
                        break
                if dev_col is None:
                    continue
                dev_avg_kw = float(sub[dev_col].mean())
                dev_kwh = dev_avg_kw * 0.25   # priemerný dev × 0.25h
                if dev_kwh < -0.1:
                    kwh_def += -dev_kwh
                    total_pen += (-dev_kwh) * zco / 1000.0   # penalty za deficit
                elif dev_kwh > 0.1:
                    kwh_sur += dev_kwh
                    # surplus — môže byť aj príjem ak ZCO ide proti
                    total_pen += dev_kwh * zco / 1000.0
            break   # nájdený case, končíme
        out["kwh_deficit"] = kwh_def
        out["kwh_surplus"] = kwh_sur
        # Konvencia: cost je vždy záporný (deficit aj surplus stojí — orientačne)
        out["cost_eur"] = -abs(total_pen)
        out["ok"] = True
    except Exception as e:
        out["diag"] = f"trace agreg. zlyhalo: {e}"

    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_daily_settlement(profile: str,
                              date_iso: Optional[str] = None) -> Dict[str, Any]:
    """Vráti kompletný settlement pre profil + deň.

    Args:
        profile: meno profilu (case sensitive)
        date_iso: 'YYYY-MM-DD' alebo None pre dnes

    Returns dict:
        {
          "ok": bool,
          "date": str,
          "profile": str,
          "market": "sk" | "cz",
          "is_today": bool,
          "dam":       <_dam_settlement output>,
          "vdt":       <_vdt_settlement output>,
          "deviation": <_deviation_settlement output>,
          "total_eur": float,        # dam.net + vdt.net + deviation.cost
          "diag_summary": str,
        }
    """
    if not date_iso:
        date_iso = dt.date.today().isoformat()
    is_today = (date_iso == dt.date.today().isoformat())

    dam_r = _dam_settlement(date_iso, profile)
    vdt_r = _vdt_settlement(date_iso, profile)
    dev_r = _deviation_settlement(date_iso, profile)

    total = (float(dam_r.get("net_eur", 0))
             + float(vdt_r.get("net_eur", 0))
             + float(dev_r.get("cost_eur", 0)))

    diag = []
    if dam_r.get("diag"): diag.append(f"DAM: {dam_r['diag']}")
    if vdt_r.get("diag"): diag.append(f"VDT: {vdt_r['diag']}")
    if dev_r.get("diag"): diag.append(f"DEV: {dev_r['diag']}")

    return {
        "ok": True,
        "date": date_iso,
        "profile": profile,
        "market": _market(),
        "is_today": is_today,
        "dam": dam_r,
        "vdt": vdt_r,
        "deviation": dev_r,
        "total_eur": total,
        "diag_summary": " · ".join(diag),
    }


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    prof = sys.argv[1] if len(sys.argv) > 1 else "Trakany_real"
    dt_iso = sys.argv[2] if len(sys.argv) > 2 else None
    res = compute_daily_settlement(prof, dt_iso)
    import json
    print(json.dumps(res, indent=2, ensure_ascii=False))
