# -*- coding: utf-8 -*-
"""core/effect.py — Single source of truth pre výpočet efektu (zisk/strata).

Cieľ konsolidácie (2026-06-08):
    Pred refactorom sa "efekt z odchýlky (RT)" počítal v 6+ rôznych miestach
    app.py + livesim.py, každé s vlastnou logikou ternary pattern alebo iným
    fallbackom. Výsledok: UI karta, Excel report, graf "Po dňoch" a "Detail dňa"
    ukazovali rôzne čísla pre ten istý deň.

    Tento modul je JEDINÝ zdroj pravdy pre 3 finančné zložky:

        ZISK_SPOLU [€] = DT_zisk + RT_efekt + VDT_arbitráž

    Všetko sa počíta z livesim DataFrame `df` (minútová granularita).
    Cesty:
      - `compute_effect_totals(df, profile, day=None)` — agregát (Excel, karta)
      - `compute_effect_cumulative(df, profile, day=None)` — kumulatívne stĺpce
      - `resolve_rt_col(df)` — meno stĺpca pre RT efekt (audit / debug)

Vzorce
------

DT_zisk_min[€]   = plan_grid_kwh[kWh] × dt_real_eur[€/MWh] / 1000
                   (nominovaný tok × OKTE clearing cena)

dev_kw           = (ftv_real - load_real + batt_REAL) - plan_grid_kwh_per_kw
batt_REAL        = plan_batt_kw + rt_dir × rt_power_pct/100 × batt_kw_max
                   (Bug #607: zahrňuje RT zásah, nie len D-1 plán)

RT_efekt_min[€]  = dev_kw / 60 × ZCO[€/MWh] / 1000
                   (preferuje rt_rev_realistic_min v CSV; fallback runtime
                    výpočet ak stĺpec chýba; fallback rt_rev_min iba ako
                    LAST RESORT s audit warning)

VDT_arbitráž_min[€] = vdt_kwh × (vdt_cena - dt_clearing) / 1000
                      (z vdt_paper_trades.csv pre profil/deň, distribuovaný
                       cez 15-min slot do minút)
"""
from __future__ import annotations
from typing import Dict, Optional, Tuple
import os
import datetime as dt
import numpy as np
import pandas as pd


# ── public API ────────────────────────────────────────────────────────────

def resolve_rt_col(df: pd.DataFrame) -> str:
    """Vráti meno stĺpca s RT efektom (€/min) ktorý sa má použiť pre agregát.

    Priorita:
      1. `rt_rev_realistic_min` (skutočný financial impact cez dev × ZCO)
      2. `rt_rev_min` (legacy theoretical engine output, fallback IBA ak chýba)

    Volajte tento helper ZAKAŽDÝM keď ide o sumarizáciu RT do € pre UI/Excel.
    Nikdy nepoužívajte `df["rt_rev_min"]` priamo, lebo to ukáže fiktívnu pokutu
    aj keď reálne dev × ZCO = 0 (Bug #603).
    """
    if "rt_rev_realistic_min" in df.columns:
        return "rt_rev_realistic_min"
    return "rt_rev_min"


def get_rt_eur_series(df: pd.DataFrame, *, warn_legacy: bool = False,
                       joint_flags: Optional[Dict] = None) -> pd.Series:
    """Vráti pd.Series s RT efektom v €/min indexovanú podľa df.

    Bug #650 (2026-06-09): ak `joint_flags` zadané a df má atribučné stĺpce
    (rt_rev_batt_min, rt_rev_ftv_min, rt_rev_load_min) → vráti **filtrovaný**
    súčet len tých komponentov ktoré profil reálne obchoduje:
      - trade_batt=True → zahrň rt_rev_batt_min
      - trade_ftv=True  → zahrň rt_rev_ftv_min
      - trade_load=True → zahrň rt_rev_load_min
    Pre profil len-batt (Simulacia_Coop) tak Zisk RT odráža LEN efekt riadenia
    batt vs plán, nie FTV/Load drift (= šum prostredia, nie systému).

    Pre joint_flags=None alebo bez atribučných stĺpcov: backward compat,
    vráti `rt_rev_realistic_min` (total trh settlement = cash flow ČEPS/OKTE).
    """
    # Bug #650: filtrovaný súčet podľa joint LP toggles ak sú dostupné komponenty
    has_decomp = ("rt_rev_batt_min" in df.columns and
                   "rt_rev_ftv_min" in df.columns and
                   "rt_rev_load_min" in df.columns)
    # Bug #650-B (2026-06-10): runtime fallback pre staré CSVky bez decomp stĺpcov.
    # Historické dni (pred Bug #649) sa neprepočítavajú; effect.py musí vedieť
    # dopočítať komponenty z primárnych stĺpcov ktoré CSV vždy obsahuje:
    #   batt_kw / batt_kw_realistic + plan_batt_kw + zco_eur + ftv_*_kw + load_*_kw
    if joint_flags is not None and not has_decomp:
        # Skontroluj že máme základné stĺpce na runtime decomp
        _need_batt = ("batt_kw_realistic" in df.columns or "batt_kw" in df.columns) \
                      and "plan_batt_kw" in df.columns
        _need_ftv = ("ftv_min_real_kw" in df.columns) \
                     and ("ftv_hour_plan_kw" in df.columns or "ftv_plan_kw" in df.columns)
        _need_load = ("load_min_real_kw" in df.columns) \
                      and ("load_plan_kw" in df.columns or "plan_load_kw" in df.columns)
        _need_zco = "zco_eur" in df.columns
        # Stačí ak vieme aspoň batt komponentu (najčastejší prípad — len-batt profil)
        if _need_zco and (_need_batt or _need_ftv or _need_load):
            _tb = bool(joint_flags.get("trade_batt", True))
            _tf = bool(joint_flags.get("trade_ftv", False))
            _tl = bool(joint_flags.get("trade_load", False))
            _zco = pd.to_numeric(df["zco_eur"], errors="coerce").fillna(0.0)
            s = pd.Series([0.0] * len(df), index=df.index)
            if _tb and _need_batt:
                _br = pd.to_numeric(df.get("batt_kw_realistic", df.get("batt_kw")),
                                     errors="coerce").fillna(0.0)
                _bp = pd.to_numeric(df["plan_batt_kw"], errors="coerce").fillna(0.0)
                s = s + ((_br - _bp) / 60.0) * _zco / 1000.0
            if _tf and _need_ftv:
                _fr = pd.to_numeric(df["ftv_min_real_kw"], errors="coerce").fillna(0.0)
                _fp_col = "ftv_hour_plan_kw" if "ftv_hour_plan_kw" in df.columns else "ftv_plan_kw"
                _fp = pd.to_numeric(df[_fp_col], errors="coerce").fillna(0.0)
                s = s + ((_fr - _fp) / 60.0) * _zco / 1000.0
            if _tl and _need_load:
                _lr = pd.to_numeric(df["load_min_real_kw"], errors="coerce").fillna(0.0)
                _lp_col = "load_plan_kw" if "load_plan_kw" in df.columns else "plan_load_kw"
                _lp = pd.to_numeric(df[_lp_col], errors="coerce").fillna(0.0)
                # +load = viac spotreby = under-export = záporná odchýlka pre exportujúceho
                s = s + ((-(_lr - _lp)) / 60.0) * _zco / 1000.0
            return s
    if joint_flags is not None and has_decomp:
        # Default trade_batt=True (batt je vždy obchodované cez D-1 plán),
        # trade_ftv/load default False (vstup do trhu len ak je explicit toggle).
        _tb = bool(joint_flags.get("trade_batt", True))
        _tf = bool(joint_flags.get("trade_ftv", False))
        _tl = bool(joint_flags.get("trade_load", False))
        s = pd.Series([0.0] * len(df), index=df.index)
        if _tb:
            s = s + pd.to_numeric(df["rt_rev_batt_min"], errors="coerce").fillna(0.0)
        if _tf:
            s = s + pd.to_numeric(df["rt_rev_ftv_min"], errors="coerce").fillna(0.0)
        if _tl:
            s = s + pd.to_numeric(df["rt_rev_load_min"], errors="coerce").fillna(0.0)
        return s
    # Backward compat: total dev × ZCO (= trh settlement)
    col = resolve_rt_col(df)
    if col in df.columns:
        s = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    else:
        s = pd.Series([0.0] * len(df), index=df.index)
    if col == "rt_rev_min" and warn_legacy:
        print("[core.effect] WARNING: rt_rev_realistic_min chyba v DataFrame, "
              "fallback na rt_rev_min (theoretical engine output, moze ukazovat "
              "fiktivnu pokutu)")
    return s


def get_vdt_arb_series(df: pd.DataFrame, profile: Optional[str] = None,
                        day: Optional[str] = None) -> pd.Series:
    """Vráti pd.Series s VDT arbitráž v €/min indexovanú podľa df.

    Priorita:
      1. Ak df má stĺpec `vdt_arb_min` (zapísaný livesim.advance pri novom kóde)
         → vráti ho.
      2. Inak runtime výpočet z `vdt_paper_trades.csv` per profil/deň
         (cesta cez core.paths.vdt_trades_csv_path).
      3. Ak nič → vráti seriu núl rovnakej dĺžky ako df.
    """
    if "vdt_arb_min" in df.columns:
        return pd.to_numeric(df["vdt_arb_min"], errors="coerce").fillna(0.0)

    # Runtime výpočet
    if not profile or not day:
        return pd.Series([0.0] * len(df), index=df.index)
    try:
        from core.paths import vdt_trades_csv_path
        vdt_csv = vdt_trades_csv_path(profile=profile)
        if not os.path.exists(vdt_csv):
            return pd.Series([0.0] * len(df), index=df.index)
        return _compute_vdt_arb_from_trades(df, vdt_csv, profile, day)
    except Exception as e:
        print(f"[core.effect.get_vdt_arb_series] {profile}/{day} chyba: {e}")
        return pd.Series([0.0] * len(df), index=df.index)


def compute_effect_totals(df: pd.DataFrame,
                            profile: Optional[str] = None,
                            day: Optional[str] = None) -> Dict[str, float]:
    """Vráti agregované zložky efektu za daný DataFrame.

    Returns: {
        "dt_eur":       float (suma DT zisku),
        "rt_eur":       float (suma RT efektu = dev × ZCO realistic),
        "vdt_arb_eur":  float (suma VDT arbitráže),
        "total_eur":    float (dt + rt + vdt_arb),
        "baseline_eur": float (referencia bez batt+plánu, ak je v df),
        "prinos_eur":   float (total - baseline),
        "rt_col_used":  str   (audit: "rt_rev_realistic_min" alebo "rt_rev_min"),
    }
    """
    # Pomocný helper: bezpečne načítaj stĺpec ako Series (vždy správna dĺžka).
    # df.get(col, 0) vráti scalar ak stĺpec chýba → .fillna() padne. Treba Series.
    def _col_or_zeros(col: str) -> pd.Series:
        if col in df.columns:
            return pd.to_numeric(df[col], errors="coerce").fillna(0)
        return pd.Series([0.0] * len(df), index=df.index)

    dt_eur = float(_col_or_zeros("dt_rev_min").sum())
    rt_series = get_rt_eur_series(df, warn_legacy=False)
    rt_eur = float(rt_series.sum())
    vdt_series = get_vdt_arb_series(df, profile=profile, day=day)
    vdt_arb_eur = float(vdt_series.sum())
    baseline_eur = float(_col_or_zeros("baseline_per_min_eur").sum())
    total_eur = dt_eur + rt_eur + vdt_arb_eur
    prinos_eur = total_eur - baseline_eur
    return {
        "dt_eur": dt_eur,
        "rt_eur": rt_eur,
        "vdt_arb_eur": vdt_arb_eur,
        "total_eur": total_eur,
        "baseline_eur": baseline_eur,
        "prinos_eur": prinos_eur,
        "rt_col_used": resolve_rt_col(df),
    }


def compute_effect_cumulative(df: pd.DataFrame,
                                profile: Optional[str] = None,
                                cum_dt_done: float = 0.0,
                                cum_rt_done: float = 0.0,
                                cum_vdt_done: float = 0.0) -> pd.DataFrame:
    """Vráti df rozšírený o stĺpce: cum_dt, cum_rt, cum_vdt_arb, cum_total.

    Použiteľný pri livesim.advance kde sa pridávajú nové minúty k bežiacemu
    kumulatívu. `cum_dt_done` / `cum_rt_done` / `cum_vdt_done` sú stav pred
    pridaním tohto dataframe.
    """
    out = df.copy()
    rt_series = get_rt_eur_series(out, warn_legacy=True)
    out["cum_rt"] = cum_rt_done + rt_series.cumsum()
    out["cum_dt"] = cum_dt_done + pd.to_numeric(out.get("dt_rev_min", 0),
                                                  errors="coerce").fillna(0).cumsum()
    vdt_series = get_vdt_arb_series(out, profile=profile,
                                      day=None)   # day=None → použije vdt_arb_min ak je v df
    out["cum_vdt_arb"] = cum_vdt_done + vdt_series.cumsum()
    out["cum_total"] = out["cum_dt"] + out["cum_rt"] + out["cum_vdt_arb"]
    return out


# ── internal helpers ──────────────────────────────────────────────────────

def _compute_vdt_arb_from_trades(df: pd.DataFrame, vdt_csv: str,
                                    profile: str, day: str) -> pd.Series:
    """Načíta vdt_paper_trades CSV, filter na profil+deň, vyráta arbitráž per
    15-min slot a distribuuje do minút.
    """
    vt = pd.read_csv(vdt_csv, low_memory=False)
    if "profile" in vt.columns:
        vt = vt[vt["profile"].astype(str) == str(profile)]
    if "ts" in vt.columns:
        vt = vt[vt["ts"].astype(str).str[:10] == day]
    # SELL/DISCHARGE = +kwh, BUY/CHARGE = -kwh
    act_sign = {"SELL": +1.0, "DISCHARGE": +1.0, "BUY": -1.0, "CHARGE": -1.0}
    vt = vt[vt["action"].astype(str).str.upper().isin(act_sign.keys())]

    dt_per_min = pd.to_numeric(df.get("dt_real_eur", 0), errors="coerce").fillna(0).values
    arb_min = np.zeros(len(df), dtype=float)
    t_arr = pd.to_datetime(df["time"], errors="coerce")

    for _, row in vt.iterrows():
        slot = str(row.get("slot", "") or "")
        start = slot.split("-")[0].strip()
        if len(start) < 5:
            continue
        try:
            hh = int(start[:2]); mm = int(start[3:5])
        except Exception:
            continue
        sign = act_sign.get(str(row.get("action", "")).upper(), 0.0)
        kwh = float(row.get("kwh", 0) or 0) * sign
        vprice = float(row.get("price_predicted_eur", 0) or 0)
        if kwh == 0 or vprice == 0:
            continue
        slot_start = pd.Timestamp(f"{day} {hh:02d}:{mm:02d}:00")
        slot_end = slot_start + pd.Timedelta(minutes=15)
        mask = (t_arr >= slot_start) & (t_arr < slot_end)
        n = int(mask.sum())
        if n <= 0:
            continue
        dt_avg = float(np.mean(dt_per_min[mask.values])) if n > 0 else 0.0
        arb_slot = (kwh * (vprice - dt_avg)) / 1000.0
        arb_min[mask.values] += arb_slot / n

    return pd.Series(np.round(arb_min, 4), index=df.index)


__all__ = [
    "resolve_rt_col",
    "get_rt_eur_series",
    "get_vdt_arb_series",
    "compute_effect_totals",
    "compute_effect_cumulative",
]
