# -*- coding: utf-8 -*-
"""
baseline_calc.py — výpočet baseline scenára BEZ batérie a BEZ plánovania.

Účel
----
Pri každom pláne / simulácii potrebujeme vidieť aj **referenčný** scenár, kde:
  - Batéria neexistuje (nemá sa kde uložiť energia)
  - Žiadne D-1 plánovanie (nominácie sa neoptimalizujú)
  - FTV najprv pokryje load (self-consumption, zadarmo)
  - Zostatok sa obchoduje za DT cenu × konštanta, ALEBO za pevnú cenu €/MWh

Tým vidíme **prínos batérie + plánovania** = (aktuálny zisk) − (baseline zisk).

Cenové režimy
-------------
  - 'dt_x'  : cena = DT × multiplier (napr. DT × 1.05 = 5 % markup pre dodávateľa)
  - 'fix'   : cena = konštanta €/MWh (napr. 80 €/MWh pevná tarifa)

Funkcie
-------
  compute_baseline_day(pv_arr, load_arr, dt_price_arr, im_mode, im_val, ex_mode, ex_val, dt=1.0)
    → dict(revenue, cost, net_profit, per_slot)
"""
from __future__ import annotations
import numpy as np
from typing import Dict, Any


def _price_eur_per_mwh(dt_price: float, mode: str, value: float) -> float:
    """Vráti cenu €/MWh podľa režimu.
    mode='dt_x': dt_price × value
    mode='fix':  value (konštanta)
    """
    if mode == "fix":
        return float(value)
    # default dt_x
    return float(dt_price) * float(value)


def compute_baseline_day(pv_arr, load_arr, dt_price_arr,
                          im_mode: str = "dt_x", im_val: float = 1.0,
                          ex_mode: str = "dt_x", ex_val: float = 1.0,
                          dt: float = 1.0,
                          tou_eur_per_mwh=None,
                          grid_fee_eur_per_mwh: float = 0.0) -> Dict[str, Any]:
    """Spočíta baseline scenár pre jeden deň (BEZ batérie, BEZ plánu, net-meter).

    Parametre
    ---------
    pv_arr : array-like
        FTV produkcia [kWh per perióda] (24 hodinových alebo 96 × 15-min)
    load_arr : array-like
        Spotreba zákazníka [kWh per perióda] (rovnaká dĺžka ako pv_arr)
    dt_price_arr : array-like
        DT clearing cena [€/MWh] per perióda (rovnaká dĺžka)
    im_mode, im_val : str, float
        Cena ODBERU (import). 'dt_x' → DT×val. 'fix' → val €/MWh.
    ex_mode, ex_val : str, float
        Cena DODÁVKY (export). 'dt_x' → DT×val. 'fix' → val €/MWh.
    dt : float
        Krok periódy v hodinách (1.0 = hodinový, 0.25 = 15-min).
        Iba informačné — pv_arr a load_arr sú v kWh per perióda už.

    Vracia
    ------
    dict:
        revenue : float  — celkový príjem za predaj prebytkov (€)
        cost    : float  — celkový náklad za odbery siete (€)
        net_profit : float — revenue − cost (€)
        export_kwh : float — celková dodávka do siete (kWh)
        import_kwh : float — celkový odber zo siete (kWh)
        self_cons_kwh : float — energia samospotrebovaná (= min(pv, load) per slot)
        per_slot : list[dict] — detail per slot pre audit/debug
    """
    pv = np.asarray(pv_arr, dtype=float).ravel()
    ld = np.asarray(load_arr, dtype=float).ravel()
    pr = np.asarray(dt_price_arr, dtype=float).ravel()
    n = min(len(pv), len(ld), len(pr))
    pv = pv[:n]; ld = ld[:n]; pr = pr[:n]
    # Net-meter: net = pv - load (per slot, kWh)
    net = pv - ld
    self_cons = np.minimum(pv, ld)
    export_kwh_arr = np.maximum(net, 0.0)
    import_kwh_arr = -np.minimum(net, 0.0)
    # ceny per slot (€/MWh)
    p_imp = np.array([_price_eur_per_mwh(p, im_mode, im_val) for p in pr], dtype=float)
    p_exp = np.array([_price_eur_per_mwh(p, ex_mode, ex_val) for p in pr], dtype=float)
    # Bug VV (2026-06-08): pridať distribučné poplatky (TOU + grid_fee) k cene importu.
    # Baseline scenár bez batt aj tak musí platiť distribútorovi za odber zo siete —
    # tie isté sadzby ako plán scenár (TOU + grid_fee). Bez toho by sa "rozdiel TOU saving"
    # cez import shifting v Joint LP optimalizácii NIKDY neprejavil v Prínose.
    if tou_eur_per_mwh is not None:
        tou_arr = np.asarray(tou_eur_per_mwh, dtype=float).ravel()[:n]
        if len(tou_arr) == n:
            p_imp = p_imp + tou_arr
    if grid_fee_eur_per_mwh and grid_fee_eur_per_mwh > 0:
        p_imp = p_imp + float(grid_fee_eur_per_mwh)
    # finančný príspevok per slot (€)
    rev_arr = export_kwh_arr * p_exp / 1000.0
    cost_arr = import_kwh_arr * p_imp / 1000.0
    revenue = float(rev_arr.sum())
    cost = float(cost_arr.sum())
    per_slot = [
        {"i": int(i), "pv_kwh": float(pv[i]), "load_kwh": float(ld[i]),
         "dt_price": float(pr[i]),
         "import_kwh": float(import_kwh_arr[i]), "export_kwh": float(export_kwh_arr[i]),
         "import_price_eur_mwh": float(p_imp[i]), "export_price_eur_mwh": float(p_exp[i]),
         "rev_eur": float(rev_arr[i]), "cost_eur": float(cost_arr[i])}
        for i in range(n)
    ]
    return {
        "revenue": revenue,
        "cost": cost,
        "net_profit": revenue - cost,
        "export_kwh": float(export_kwh_arr.sum()),
        "import_kwh": float(import_kwh_arr.sum()),
        "self_cons_kwh": float(self_cons.sum()),
        "per_slot": per_slot,
    }


def compute_dist_fee_savings(df, grid_fee_eur_per_mwh: float) -> Dict[str, float]:
    """Distribučná úspora = grid_fee × (baseline_import − skutočný_import), z trace df.

    User (2026-06-15): distribučné poplatky počítať ZVLÁŠŤ ako samostatnú zložku efektu.
    Kontrakt: batéria dodávateľa agregovaná na flexibilitu; zákazníkov benefit = ušetrený
    distribučný poplatok zo samospotreby (FTV→batéria→spotreba zníži odber zo siete).

    baseline_import (bez batérie, net-meter) = max(load − pv, 0) per minúta.
    skutočný_import (s batériou)            = max(load − pv − batt_real, 0) per minúta,
        kde batt_real > 0 = vybíjanie (pridá zdroj), < 0 = nabíjanie (pridá odber).
    Pozitívne = batéria znížila odber (samospotreba); negatívne = batéria zvýšila odber
    (nabíjanie zo siete pre arbitráž = reálny distribučný náklad).

    Vracia {dist_fee_eur, baseline_import_kwh, actual_import_kwh, import_reduction_kwh}.
    """
    import numpy as _np
    out = {"dist_fee_eur": 0.0, "baseline_import_kwh": 0.0,
           "actual_import_kwh": 0.0, "import_reduction_kwh": 0.0}
    try:
        if df is None or len(df) == 0:
            return out
        gf = float(grid_fee_eur_per_mwh or 0.0)
        n = len(df)

        def _col(*names):
            for nm in names:
                if nm in df.columns:
                    return _np.asarray(
                        __import__("pandas").to_numeric(df[nm], errors="coerce").fillna(0.0).values,
                        dtype=float)
            return _np.zeros(n, dtype=float)

        pv = _col("ftv_min_real_kw", "ftv_kw")                 # kW
        load = _col("load_min_real_kw", "load_plan_kw")        # kW
        batt = _col("batt_kw_realistic", "plan_batt_kw")       # kW (+vybíja / −nabíja)
        # per-minútový krok (trace je 1-min); ak by bol iný, /60 ostáva konzistentné s €/MWh
        base_imp_kw = _np.maximum(load - pv, 0.0)
        act_imp_kw = _np.maximum(load - pv - batt, 0.0)
        base_imp_kwh = float(base_imp_kw.sum() / 60.0)
        act_imp_kwh = float(act_imp_kw.sum() / 60.0)
        out["baseline_import_kwh"] = round(base_imp_kwh, 1)
        out["actual_import_kwh"] = round(act_imp_kwh, 1)
        out["import_reduction_kwh"] = round(base_imp_kwh - act_imp_kwh, 1)
        out["dist_fee_eur"] = round(gf * (base_imp_kwh - act_imp_kwh) / 1000.0, 2)
    except Exception as _e:
        print(f"[compute_dist_fee_savings] {_e}")
    return out


def parse_baseline_params(ui_plan: Dict[str, Any]) -> Dict[str, Any]:
    """Vytiahne 4 baseline parametre z ui_settings.plan dict s defaultmi (dt_x, 1.0)."""
    return {
        "im_mode": str(ui_plan.get("baseline_im_mode", "dt_x")),
        "im_val": float(ui_plan.get("baseline_im_value", 1.0)),
        "ex_mode": str(ui_plan.get("baseline_ex_mode", "dt_x")),
        "ex_val": float(ui_plan.get("baseline_ex_value", 1.0)),
    }
