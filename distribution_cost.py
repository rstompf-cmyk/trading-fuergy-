"""Distribučné tarify per profil (TOU + peak demand + monthly fix).

Použité v:
  - Joint LP (`joint_lp.optimize_joint_day` s `optimize_distribution=True`)
    → TOU sadzba sa pripočíta k DAM/VDT cene importu a optimizer ju zohľadní.
  - Settlement card (reporting `tou_cost_eur` per deň).

Per-profil konfigurácia v `profile.distribution`:
  {
    "enabled": bool,                          # ak False, optimize_distribution sa ignoruje
    "tou_mode": "tou" | "flat",               # "flat" = jednotná sadzba (high=low)
    "tou_high_eur_per_mwh": float,            # špička
    "tou_low_eur_per_mwh": float,             # mimo špičky
    "tou_high_hours": [int,...],              # zoznam hodín 0..23 v špičke (default 8-11, 17-19)
    "tou_weekend_low_only": bool,             # ak True, víkend = vždy low
    "peak_charge_eur_per_kw_month": float,    # rezerv. výkon €/kW/mesiac (0 = vypnuté)
    "monthly_fix_eur": float,                 # mesačný fix poplatok (reporting only)
  }

API:
  - `default_config() -> dict`              # default config (všetko 0/disabled)
  - `get_config(profile) -> dict`           # načíta z profilu
  - `save_config(profile, config) -> bool`  # uloží do profilu
  - `tou_prices_for_day(config, date, T=24, dt=1.0) -> np.ndarray`
        → pole TOU cien €/MWh per slot (24 alebo 96 hodnôt)
  - `compute_distribution_cost(import_kwh, tou_prices, peak_kw=0, monthly_fix=0, days_in_month=30) -> dict`
        → ekonomický rozklad za deň
"""
from __future__ import annotations
import datetime as _dt
from typing import Optional, Dict, Any, List

import numpy as np


# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

DEFAULT_TOU_HIGH_HOURS = [8, 9, 10, 11, 17, 18, 19]   # špička: 8-12 a 17-20


def default_config() -> Dict[str, Any]:
    """Default distribučný config — **orientačné hodnoty SK 2024-2025 podľa ÚRSO**.

    Predvyplnené pre najčastejší prípad (ZSD VO2, 200-500 kW, VN). Konkrétne čísla
    musí užívateľ overiť podľa svojho cenníka distribučnej spoločnosti.

    `enabled=False` — užívateľ musí explicitne zapnúť master toggle aby sa použili
    v LP. Predvyplnené hodnoty slúžia ako rozumný štart, nie ako auto-aktivácia.
    """
    return {
        "enabled": False,
        # Identifikácia — defaultne ZSD VO2 (najčastejší pre menšie FVE+batt prevádzky)
        "distribution_company": "ZSD",     # ZSD | SSD | VSD
        "tariff_group": "VO2",              # MO1..MO3, VO1..VO5, VTL, CUSTOM
        "voltage_level": "VN",              # NN | VN | VVN
        # TOU distribučná zložka (€/MWh) — orientačné hodnoty VO2 (ÚRSO 2024-2025)
        "tou_mode": "tou",                  # "tou" | "flat" | "hourly"
        "tou_high_eur_per_mwh": 25.0,       # VT (špička) — VO2 priemer
        "tou_low_eur_per_mwh": 12.0,        # NT (mimo) — VO2 priemer
        "tou_high_hours": list(DEFAULT_TOU_HIGH_HOURS),   # 6-21 pracovné dni
        "tou_weekend_low_only": True,        # víkend = NT celodenne
        # Per-hour custom (24 hodnôt €/MWh, override TOU; null/None = nepoužívať)
        "hourly_custom_eur_per_mwh": None,
        # Uniformné zložky (€/MWh, platí na každú kWh importu)
        "tps_eur_per_mwh": 28.0,             # Tarifa za prev. systému (ÚRSO ~28)
        "ss_eur_per_mwh": 10.0,              # Systémové služby (ÚRSO ~10)
        "oze_eur_per_mwh": 7.0,              # OZE odvod (Národný jadrový fond + OZE ~7)
        # Fixné mesačné — orientačné VO2
        "peak_charge_eur_per_kw_month": 5.0, # Rezervovaná kapacita VO2 ~5 €/kW/mes
        "monthly_fix_eur": 12.0,             # Mesačný fix za odberné miesto
    }


# ---------------------------------------------------------------------------
# Tarifné presets pre slovenské distribučné spoločnosti
# (orientačné hodnoty 2024–2025, ÚRSO regulácia; ceny aktualizovať podľa
#  konkrétneho cenníka distribučnej spoločnosti)
# ---------------------------------------------------------------------------

TARIFF_PRESETS = {
    # ── ZSD (Bratislava, Trnava, Nitra, Trenčín) ─────────────────────────
    "ZSD_MO1": {
        "distribution_company": "ZSD", "tariff_group": "MO1",
        "voltage_level": "NN", "tou_mode": "flat",
        "tou_high_eur_per_mwh": 0.0, "tou_low_eur_per_mwh": 35.0,
        "tou_high_hours": [], "tou_weekend_low_only": True,
        "tps_eur_per_mwh": 28.0, "ss_eur_per_mwh": 10.0, "oze_eur_per_mwh": 7.0,
        "peak_charge_eur_per_kw_month": 0.0, "monthly_fix_eur": 3.0,
        "_note": "ZSD MO1 — jednotarif, do 22 kW (malé odberné miesto)",
    },
    "ZSD_MO2": {
        "distribution_company": "ZSD", "tariff_group": "MO2",
        "voltage_level": "NN", "tou_mode": "tou",
        "tou_high_eur_per_mwh": 35.0, "tou_low_eur_per_mwh": 15.0,
        "tou_high_hours": [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
        "tou_weekend_low_only": True,
        "tps_eur_per_mwh": 28.0, "ss_eur_per_mwh": 10.0, "oze_eur_per_mwh": 7.0,
        "peak_charge_eur_per_kw_month": 0.0, "monthly_fix_eur": 4.0,
        "_note": "ZSD MO2 — dvojtarif, do 22 kW (typicky domácnosť/malý odber)",
    },
    "ZSD_MO3": {
        "distribution_company": "ZSD", "tariff_group": "MO3",
        "voltage_level": "NN", "tou_mode": "tou",
        "tou_high_eur_per_mwh": 22.0, "tou_low_eur_per_mwh": 10.0,
        "tou_high_hours": [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
        "tou_weekend_low_only": True,
        "tps_eur_per_mwh": 28.0, "ss_eur_per_mwh": 10.0, "oze_eur_per_mwh": 7.0,
        "peak_charge_eur_per_kw_month": 0.0, "monthly_fix_eur": 8.0,
        "_note": "ZSD MO3 — väčší odber 22-100 kW",
    },
    "ZSD_VO1": {
        "distribution_company": "ZSD", "tariff_group": "VO1",
        "voltage_level": "VN", "tou_mode": "tou",
        "tou_high_eur_per_mwh": 23.0, "tou_low_eur_per_mwh": 11.0,
        "tou_high_hours": [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
        "tou_weekend_low_only": True,
        "tps_eur_per_mwh": 28.0, "ss_eur_per_mwh": 10.0, "oze_eur_per_mwh": 7.0,
        "peak_charge_eur_per_kw_month": 4.5, "monthly_fix_eur": 10.0,
        "_note": "ZSD VO1 — veľký odberateľ 100-200 kW (VN)",
    },
    "ZSD_VO2": {
        "distribution_company": "ZSD", "tariff_group": "VO2",
        "voltage_level": "VN", "tou_mode": "tou",
        "tou_high_eur_per_mwh": 25.0, "tou_low_eur_per_mwh": 12.0,
        "tou_high_hours": [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
        "tou_weekend_low_only": True,
        "tps_eur_per_mwh": 28.0, "ss_eur_per_mwh": 10.0, "oze_eur_per_mwh": 7.0,
        "peak_charge_eur_per_kw_month": 5.0, "monthly_fix_eur": 12.0,
        "_note": "ZSD VO2 — 200-500 kW (najčastejší pre menšie FVE prevádzky)",
    },
    "ZSD_VO3": {
        "distribution_company": "ZSD", "tariff_group": "VO3",
        "voltage_level": "VN", "tou_mode": "tou",
        "tou_high_eur_per_mwh": 27.0, "tou_low_eur_per_mwh": 13.0,
        "tou_high_hours": [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
        "tou_weekend_low_only": True,
        "tps_eur_per_mwh": 28.0, "ss_eur_per_mwh": 10.0, "oze_eur_per_mwh": 7.0,
        "peak_charge_eur_per_kw_month": 5.5, "monthly_fix_eur": 15.0,
        "_note": "ZSD VO3 — 500-1000 kW",
    },
    "ZSD_VO4": {
        "distribution_company": "ZSD", "tariff_group": "VO4",
        "voltage_level": "VVN", "tou_mode": "tou",
        "tou_high_eur_per_mwh": 18.0, "tou_low_eur_per_mwh": 9.0,
        "tou_high_hours": [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
        "tou_weekend_low_only": True,
        "tps_eur_per_mwh": 28.0, "ss_eur_per_mwh": 10.0, "oze_eur_per_mwh": 7.0,
        "peak_charge_eur_per_kw_month": 6.0, "monthly_fix_eur": 20.0,
        "_note": "ZSD VO4 — > 1 MW (VVN)",
    },
    # ── SSD (Žilina, Banská Bystrica) ───────────────────────────────────
    "SSD_MO3": {
        "distribution_company": "SSD", "tariff_group": "MO3",
        "voltage_level": "NN", "tou_mode": "tou",
        "tou_high_eur_per_mwh": 23.0, "tou_low_eur_per_mwh": 10.0,
        "tou_high_hours": [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
        "tou_weekend_low_only": True,
        "tps_eur_per_mwh": 28.0, "ss_eur_per_mwh": 10.0, "oze_eur_per_mwh": 7.0,
        "peak_charge_eur_per_kw_month": 0.0, "monthly_fix_eur": 8.0,
        "_note": "SSD MO3 — 22-100 kW",
    },
    "SSD_VO2": {
        "distribution_company": "SSD", "tariff_group": "VO2",
        "voltage_level": "VN", "tou_mode": "tou",
        "tou_high_eur_per_mwh": 26.0, "tou_low_eur_per_mwh": 13.0,
        "tou_high_hours": [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
        "tou_weekend_low_only": True,
        "tps_eur_per_mwh": 28.0, "ss_eur_per_mwh": 10.0, "oze_eur_per_mwh": 7.0,
        "peak_charge_eur_per_kw_month": 5.2, "monthly_fix_eur": 12.0,
        "_note": "SSD VO2 — 200-500 kW",
    },
    # ── VSD (Košice, Prešov) ────────────────────────────────────────────
    "VSD_MO3": {
        "distribution_company": "VSD", "tariff_group": "MO3",
        "voltage_level": "NN", "tou_mode": "tou",
        "tou_high_eur_per_mwh": 21.0, "tou_low_eur_per_mwh": 9.0,
        "tou_high_hours": [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
        "tou_weekend_low_only": True,
        "tps_eur_per_mwh": 28.0, "ss_eur_per_mwh": 10.0, "oze_eur_per_mwh": 7.0,
        "peak_charge_eur_per_kw_month": 0.0, "monthly_fix_eur": 8.0,
        "_note": "VSD MO3 — 22-100 kW",
    },
    "VSD_VO2": {
        "distribution_company": "VSD", "tariff_group": "VO2",
        "voltage_level": "VN", "tou_mode": "tou",
        "tou_high_eur_per_mwh": 24.0, "tou_low_eur_per_mwh": 11.0,
        "tou_high_hours": [6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
        "tou_weekend_low_only": True,
        "tps_eur_per_mwh": 28.0, "ss_eur_per_mwh": 10.0, "oze_eur_per_mwh": 7.0,
        "peak_charge_eur_per_kw_month": 4.8, "monthly_fix_eur": 12.0,
        "_note": "VSD VO2 — 200-500 kW",
    },
    # ── CUSTOM (prázdny) ────────────────────────────────────────────────
    "CUSTOM": {
        "distribution_company": "CUSTOM", "tariff_group": "CUSTOM",
        "voltage_level": "NN", "tou_mode": "tou",
        "tou_high_eur_per_mwh": 0.0, "tou_low_eur_per_mwh": 0.0,
        "tou_high_hours": list(DEFAULT_TOU_HIGH_HOURS),
        "tou_weekend_low_only": True,
        "tps_eur_per_mwh": 0.0, "ss_eur_per_mwh": 0.0, "oze_eur_per_mwh": 0.0,
        "peak_charge_eur_per_kw_month": 0.0, "monthly_fix_eur": 0.0,
        "_note": "Vlastné hodnoty — vypln podľa aktuálneho cenníka.",
    },
}


def get_preset(name: str) -> Dict[str, Any]:
    """Vráti preset config (s `enabled=True`) alebo CUSTOM ak meno nie je v zozname."""
    p = TARIFF_PRESETS.get(name) or TARIFF_PRESETS["CUSTOM"]
    out = dict(p)
    out["enabled"] = True
    # Odstráň internú dokumentáciu
    out.pop("_note", None)
    # Hourly custom zostáva None (preset používa VT/NT)
    out["hourly_custom_eur_per_mwh"] = None
    return out


def list_presets() -> List[Dict[str, str]]:
    """Vráti zoznam dostupných presetov pre UI dropdown."""
    out = []
    for k, v in TARIFF_PRESETS.items():
        out.append({"key": k, "label": k.replace("_", " "),
                    "note": v.get("_note", "")})
    return out


def normalize_config(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Vyplní missing keys defaultami, type-safe."""
    out = default_config()
    if not cfg:
        return out
    out["enabled"] = bool(cfg.get("enabled", False))
    # Identifikácia
    out["distribution_company"] = str(cfg.get("distribution_company") or "ZSD")
    out["tariff_group"] = str(cfg.get("tariff_group") or "VO2")
    out["voltage_level"] = str(cfg.get("voltage_level") or "VN")
    # TOU
    out["tou_mode"] = str(cfg.get("tou_mode", "tou"))
    for k_in, k_out in [
        ("tou_high_eur_per_mwh", "tou_high_eur_per_mwh"),
        ("tou_low_eur_per_mwh", "tou_low_eur_per_mwh"),
        ("tps_eur_per_mwh", "tps_eur_per_mwh"),
        ("ss_eur_per_mwh", "ss_eur_per_mwh"),
        ("oze_eur_per_mwh", "oze_eur_per_mwh"),
        ("peak_charge_eur_per_kw_month", "peak_charge_eur_per_kw_month"),
        ("monthly_fix_eur", "monthly_fix_eur"),
    ]:
        try:
            out[k_out] = float(cfg.get(k_in, 0.0) or 0.0)
        except Exception:
            out[k_out] = 0.0
    h = cfg.get("tou_high_hours")
    if isinstance(h, list):
        try:
            out["tou_high_hours"] = [int(x) for x in h if 0 <= int(x) <= 23]
        except Exception:
            out["tou_high_hours"] = list(DEFAULT_TOU_HIGH_HOURS)
    out["tou_weekend_low_only"] = bool(cfg.get("tou_weekend_low_only", True))
    # Hourly custom (24 hodnôt €/MWh, override TOU)
    hc = cfg.get("hourly_custom_eur_per_mwh")
    if isinstance(hc, list) and len(hc) == 24:
        try:
            out["hourly_custom_eur_per_mwh"] = [float(x) for x in hc]
        except Exception:
            out["hourly_custom_eur_per_mwh"] = None
    else:
        out["hourly_custom_eur_per_mwh"] = None
    return out


# ---------------------------------------------------------------------------
# Per-profil persistencia
# ---------------------------------------------------------------------------

def get_config(profile: Optional[str] = None) -> Dict[str, Any]:
    """Načíta distribučný config z profilu (sekcia `distribution`).

    Ak profil neexistuje alebo nemá `distribution`, vráti default_config().
    """
    if not profile or profile == "default":
        return default_config()
    try:
        import profiles as _pr
        prof = _pr.load_profile(profile) or {}
        return normalize_config(prof.get("distribution"))
    except Exception:
        return default_config()


def save_config(profile: str, config: Dict[str, Any]) -> bool:
    """Uloží distribučný config do profilu (sekcia `distribution`)."""
    if not profile or profile == "default":
        return False
    try:
        import profiles as _pr
        prof = _pr.load_profile(profile) or {}
        prof["distribution"] = normalize_config(config)
        _pr.save_profile(profile, prof)
        return True
    except Exception as e:
        print(f"[distribution_cost.save_config] zlyhalo: {e}")
        return False


# ---------------------------------------------------------------------------
# TOU pricing per slot
# ---------------------------------------------------------------------------

def is_high_slot(config: Dict[str, Any], date: _dt.date, hour: int) -> bool:
    """Vráti True ak je hodina v špičke (TOU high) podľa configu pre konkrétny dátum."""
    cfg = normalize_config(config)
    if cfg["tou_mode"] != "tou":
        return False   # flat tarifa
    # Víkend
    if cfg["tou_weekend_low_only"] and date.weekday() >= 5:   # 5=sobota, 6=nedeľa
        return False
    return int(hour) in cfg["tou_high_hours"]


def tou_prices_for_day(config: Dict[str, Any],
                        date: _dt.date,
                        T: int = 24, dt: float = 1.0) -> np.ndarray:
    """Vráti pole CELKOVÝCH distribučných cien €/MWh per slot pre konkrétny deň.

    Celkové = TOU distribučná zložka (časovo-variabilná) + uniformné poplatky
    (TPS + SS + OZE odvod, ktoré sa platia z každej kWh importu bez ohľadu na čas).

    Args:
        config: distribučný config (z get_config alebo TARIFF_PRESETS)
        date: dátum
        T: počet slotov (24 pre 1-h, 96 pre 15-min)
        dt: dĺžka slotu v hodinách (1.0 alebo 0.25)

    Returns:
        np.ndarray dĺžky T s cenami €/MWh (TOU + tps + ss + oze).

    Priority pre TOU zložku:
        1. `hourly_custom_eur_per_mwh` (24 hodnôt, override) — ak je nastavené
        2. `tou_mode="flat"` → low_price pre všetky sloty
        3. `tou_mode="tou"` → high/low podľa hodín + weekend logic

    Pre disabled config (enabled=False) vracia nuly.
    """
    cfg = normalize_config(config)
    out = np.zeros(T, dtype=float)
    if not cfg["enabled"]:
        return out

    # Uniformné zložky — pripočítavajú sa na všetky sloty
    uniform_extra = (cfg["tps_eur_per_mwh"] + cfg["ss_eur_per_mwh"]
                      + cfg["oze_eur_per_mwh"])

    # TOU distribučná zložka — variabilná podľa hodiny
    hourly = cfg.get("hourly_custom_eur_per_mwh")
    if isinstance(hourly, list) and len(hourly) == 24:
        # Per-hour custom override — 24 hodnôt, expanduj na T slotov
        for t in range(T):
            hour = int(t * dt) if dt < 1.0 else int(t)
            hour = min(max(hour, 0), 23)
            out[t] = float(hourly[hour])
    elif cfg["tou_mode"] == "flat":
        out[:] = cfg["tou_low_eur_per_mwh"]
    else:
        # TOU — high pre niektoré hodiny, low pre ostatné
        high = cfg["tou_high_eur_per_mwh"]
        low = cfg["tou_low_eur_per_mwh"]
        if cfg["tou_weekend_low_only"] and date.weekday() >= 5:
            out[:] = low
        else:
            for t in range(T):
                hour = int(t * dt) if dt < 1.0 else int(t)
                if hour in cfg["tou_high_hours"]:
                    out[t] = high
                else:
                    out[t] = low

    # Pridaj uniformné zložky
    out += uniform_extra
    return out


def avg_eur_per_mwh(config: Optional[Dict[str, Any]],
                     date: Optional[_dt.date] = None) -> float:
    """Vráti **priemerný €/MWh** distribučného poplatku za celý deň.

    Použitie: keď je `distribution.enabled=True`, táto hodnota nahrádza ručne
    zadaný `grid_fee` v /plan formulári (single source of truth).

    Pre klasický `optimize_day` (kde je grid_fee jedna skalárna hodnota
    aplikovaná na všetky sloty) je priemer rozumný proxy. Pre joint_lp
    s `optimize_distribution=True` sa použijú presnejšie per-slot TOU ceny
    cez `tou_prices_for_day` a grid_fee sa nuluje (zabraňuje duplicite).

    Vracia 0.0 ak config je None/disabled.
    """
    if not config:
        return 0.0
    cfg = normalize_config(config)
    if not cfg.get("enabled"):
        return 0.0
    d = date or _dt.date.today()
    # Použi 24 hodín — denný priemer (víkend/pracovný deň sa rieši mimo)
    prices = tou_prices_for_day(cfg, d, T=24, dt=1.0)
    if prices is None or len(prices) == 0:
        return 0.0
    return float(np.mean(prices))


# ---------------------------------------------------------------------------
# Distribučný náklad (reporting)
# ---------------------------------------------------------------------------

def compute_distribution_cost(import_kwh: np.ndarray,
                                tou_prices: np.ndarray,
                                peak_kw: float = 0.0,
                                config: Optional[Dict[str, Any]] = None,
                                days_in_month: int = 30) -> Dict[str, Any]:
    """Vypočíta distribučný náklad za deň.

    Args:
        import_kwh: pole kWh importu per slot (≥0)
        tou_prices: pole TOU cien €/MWh per slot (rovnaká dĺžka ako import_kwh)
        peak_kw: maximálny import v kW per slot (auto-derive z import_kwh ak nie je)
        config: distribučný config (pre peak_charge_eur_per_kw_month + fix)
        days_in_month: koľko dní v mesiaci pre alokáciu mesačných poplatkov

    Returns dict:
        {
            "tou_cost_eur": float,    # Σ tou_prices × import_kwh / 1000
            "peak_kw": float,         # max(import_kwh) / dt (auto-derived)
            "peak_cost_eur_day": float,   # peak_charge × peak_kw / days_in_month
            "monthly_fix_eur_day": float, # monthly_fix / days_in_month
            "total_cost_eur_day": float,  # sum
        }
    """
    cfg = normalize_config(config)
    im = np.asarray(import_kwh, dtype=float)
    pr = np.asarray(tou_prices, dtype=float)
    T = len(im)
    if pr.size != T:
        pr = np.resize(pr, T)

    tou_cost = float(np.sum(im * pr) / 1000.0)

    if peak_kw <= 0:
        peak_kw = float(im.max()) * (T / 24.0)   # peak kW ≈ max kWh × slots/hour
        # Lepšia metóda: rozdeliť slot na hour-rate
        # Pre 24-slot (1h): max(im) je kW priemer cez hodinu
        # Pre 96-slot (15min): max(im)×4 je kW priemer cez 15-min slot
        # Tu používame T/24 ako prepočet kWh/slot → kW
    peak_cost = (cfg["peak_charge_eur_per_kw_month"] * peak_kw
                  / max(days_in_month, 1))
    fix_day = cfg["monthly_fix_eur"] / max(days_in_month, 1)

    return {
        "tou_cost_eur": round(tou_cost, 3),
        "peak_kw": round(peak_kw, 1),
        "peak_cost_eur_day": round(peak_cost, 3),
        "monthly_fix_eur_day": round(fix_day, 3),
        "total_cost_eur_day": round(tou_cost + peak_cost + fix_day, 3),
    }


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    print("=== distribution_cost smoke test ===")

    # 1. Default config
    print("\n1. default_config():")
    print(json.dumps(default_config(), indent=2))

    # 2. TOU prices pre weekday
    cfg = {
        "enabled": True,
        "tou_mode": "tou",
        "tou_high_eur_per_mwh": 25,
        "tou_low_eur_per_mwh": 12,
        "tou_high_hours": [8, 9, 10, 11, 17, 18, 19],
        "tou_weekend_low_only": True,
        "peak_charge_eur_per_kw_month": 4.0,
        "monthly_fix_eur": 5.0,
    }
    weekday = _dt.date(2026, 6, 4)   # štvrtok
    weekend = _dt.date(2026, 6, 7)   # nedeľa

    print("\n2. TOU prices weekday (1-h, 24 slotov):")
    p = tou_prices_for_day(cfg, weekday, T=24, dt=1.0)
    for h in range(24):
        print(f"   {h:02d}: {p[h]:.1f} €/MWh")

    print("\n3. TOU prices weekend (1-h):")
    p = tou_prices_for_day(cfg, weekend, T=24, dt=1.0)
    high_count = sum(1 for x in p if x > 12.5)
    print(f"   všetkých 24 = low (low_count: {sum(1 for x in p if x <= 12.5)}, high_count: {high_count})")

    print("\n4. TOU prices 15-min (96 slotov):")
    p15 = tou_prices_for_day(cfg, weekday, T=96, dt=0.25)
    print(f"   prvých 12 slotov (0-3h): {p15[:12].tolist()}")
    print(f"   sloty 32-44 (8-11h):     {p15[32:44].tolist()}")
    print(f"   sloty 68-80 (17-20h):    {p15[68:80].tolist()}")

    print("\n5. compute_distribution_cost:")
    import_kwh = np.array([10] * 24, dtype=float)   # 10 kWh/h × 24h
    tou_p = tou_prices_for_day(cfg, weekday, T=24, dt=1.0)
    res = compute_distribution_cost(import_kwh, tou_p, peak_kw=50.0,
                                       config=cfg, days_in_month=30)
    print(f"   {json.dumps(res, indent=2)}")
