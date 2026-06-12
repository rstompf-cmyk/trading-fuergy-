# -*- coding: utf-8 -*-
"""rt_engine_v2 — RT poradca 2.0 (SK trh).

Ekonomické rozhodnutie namiesto signálovej heuristiky v1 (kdis/kchg/margin/dtk):
každú minútu sa odhadne očakávaná ZCO cena z regulačného signálu (model
kalibrovaný z out/sk/imbalance_history.csv: spread = zco − isot vs sys_MW)
a RT zásah sa spraví len keď čistá marža prekročí prah:

    vybíjanie nad plán:  marža = E[ZCO] − DT − cycle_cost   (predaj odchýlky za ZCO)
    nabíjanie nad plán:  marža = DT − E[ZCO] − cycle_cost   (nákup odchýlky za ZCO)

Veľkosť zásahu: f ∈ (0,1] lineárne od margin_min_eur (prah) po margin_full_eur
(plný výkon). Všetky ochranné vrstvy v1 (no-worsen, lookahead, persistencia,
grid limity, inline SOC audit) ostávajú aplikované DOWNSTREAM v rt_controlleri —
v2 mení iba zdroj zámeru (d, f, reason).

Zapína sa per profil: rt.engine = "v2" (šablóna profilu). CZ trh: TODO — viac
dát + vyššia granularita, prispôsobíme po validácii na SK.
"""
from __future__ import annotations
import os
from typing import Dict, Any, Optional, Tuple

import numpy as np

_CALIB_CACHE: Dict[str, Any] = {}

# Defaults pre v2 parametre (profil rt sekcia ich môže prepísať)
DEFAULTS = dict(
    rt2_margin_min_eur=10.0,    # minimálna čistá marža €/MWh pre zásah
    rt2_margin_full_eur=60.0,   # marža pri ktorej ide RT na plný výkon
    rt2_zco_k=0.6,              # fallback slope €/MWh za MW signálu (keď kalibrácia chýba)
    rt2_cycle_cost=5.0,         # opotrebenie €/MWh pre RT cyklus
    rt2_eff_rt=0.9025,          # round-trip účinnosť (eff_c × eff_d) — dedí sa z profilu
    rt2_restore_weight=0.5,     # váha nákladu obnovy energie: 1.0 = plný round-trip
                                # (konzervatívne), 0.0 = zásah len posúva plán (optimistické).
                                # 0.5 = vyvážený default; ladiť podľa ex-post efektivity.
    rt2_restore_mode="fixed",   # "fixed" = rt2_restore_weight; "auto" = váha per smer
                                # z plán-kontextu (zostávajúce plánované nabíjanie/vybíjanie)
)


def _imbalance_csv_path() -> str:
    try:
        import market as _mk
        root = _mk.data_dir()                 # out/sk alebo out/cz
    except Exception:
        root = os.path.join("out", "sk")
    return os.path.join(root, "imbalance_history.csv")


def _daypart(hour: int) -> int:
    """0 = noc (22-06), 1 = ráno (06-10), 2 = deň (10-17), 3 = večer (17-22)."""
    h = int(hour) % 24
    if 6 <= h < 10:
        return 1
    if 10 <= h < 17:
        return 2
    if 17 <= h < 22:
        return 3
    return 0


def _load_calibration() -> Optional[Dict[str, Any]]:
    """ZCO spread model (bod 3 vylepšenia): 2D binned — daypart × sys_MW kvantilové
    biny, hodnota = medián(zco − isot). Riedke bunky (< 8 vzoriek) padajú na
    globálnu 1D krivku (len sys). Cache per mtime súboru."""
    p = _imbalance_csv_path()
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return None
    cached = _CALIB_CACHE.get(p)
    if cached is not None and cached.get("mtime") == mtime:
        return cached
    try:
        import pandas as pd
        df = pd.read_csv(p, usecols=["ts", "isot_eur", "zco_eur", "sys_MWh"])
        df = df.dropna(subset=["isot_eur", "zco_eur", "sys_MWh"])
        if len(df) < 50:
            return None
        sys_mw = (pd.to_numeric(df["sys_MWh"], errors="coerce") * 4.0).values
        spread = (pd.to_numeric(df["zco_eur"], errors="coerce")
                  - pd.to_numeric(df["isot_eur"], errors="coerce")).values
        hours = pd.to_datetime(df["ts"], errors="coerce").dt.hour.fillna(0).astype(int).values
        ok = np.isfinite(sys_mw) & np.isfinite(spread)
        sys_mw, spread, hours = sys_mw[ok], spread[ok], hours[ok]
        dparts = np.array([_daypart(h) for h in hours])
        # Globálna 1D krivka (fallback pre riedke bunky + chýbajúcu hodinu)
        qs = np.quantile(sys_mw, np.linspace(0.02, 0.98, 9))
        gx, gy = [], []
        for i in range(len(qs) - 1):
            m = (sys_mw >= qs[i]) & (sys_mw <= qs[i + 1])
            if int(m.sum()) >= 5:
                gx.append(float(np.median(sys_mw[m])))
                gy.append(float(np.median(spread[m])))
        if len(gx) < 3:
            return None
        # 2D: daypart × 5 sys binov (kvantily v rámci daypartu)
        parts2d = {}
        for dp in (0, 1, 2, 3):
            sel = dparts == dp
            if int(sel.sum()) < 40:
                continue
            sq = np.quantile(sys_mw[sel], np.linspace(0.05, 0.95, 6))
            cx, cy = [], []
            for i in range(len(sq) - 1):
                m = sel & (sys_mw >= sq[i]) & (sys_mw <= sq[i + 1])
                if int(m.sum()) >= 8:
                    cx.append(float(np.median(sys_mw[m])))
                    cy.append(float(np.median(spread[m])))
            if len(cx) >= 3:
                parts2d[dp] = (np.asarray(cx), np.asarray(cy))
        calib = {"mtime": mtime, "x": np.asarray(gx), "y": np.asarray(gy),
                 "parts": parts2d, "n": int(len(sys_mw))}
        _CALIB_CACHE[p] = calib
        print(f"[rt_engine_v2] kalibrácia ZCO spreadu: {calib['n']} vzoriek, "
              f"global {len(gx)} binov + dayparty {sorted(parts2d.keys())}, "
              f"spread {min(gy):.0f}..{max(gy):.0f} €/MWh")
        return calib
    except Exception as e:
        print(f"[rt_engine_v2] kalibrácia zlyhala: {e}")
        return None


def expected_zco_spread(sig_mw: float, zco_k: float,
                        hour: Optional[int] = None) -> Tuple[float, str]:
    """E[zco − isot] pre daný signál (+ voliteľne hodinu dňa).
    Daypart krivka → globálna krivka → lineárny fallback."""
    calib = _load_calibration()
    if calib is not None:
        if hour is not None:
            _pc = calib.get("parts", {}).get(_daypart(hour))
            if _pc is not None:
                return float(np.interp(sig_mw, _pc[0], _pc[1])), f"calib-dp{_daypart(hour)}"
        return float(np.interp(sig_mw, calib["x"], calib["y"])), "calib"
    return float(zco_k) * float(sig_mw), "lin"


def decide_v2(sig_avg_mw: float, dt_eur: float, soc_pct: float,
              params: Optional[Dict[str, Any]] = None,
              hour: Optional[int] = None,
              future_chg_kwh: Optional[float] = None,
              future_dis_kwh: Optional[float] = None,
              batt_kwh: Optional[float] = None) -> Tuple[int, float, str]:
    """Ekonomické RT rozhodnutie. Returns (d, f, reason) — kontrakt v1 decide_reason.

    d ∈ {-1, 0, +1} (−1 = nabíjaj nad plán, +1 = vybíjaj nad plán), f ∈ [0, 1].
    SOC/plán/grid ochrany rieši rt_controller downstream — tu LEN ekonomika.
    hour = hodina dňa (daypart kalibrácia); future_chg/dis_kwh = zostávajúce
    plánované nabíjanie/vybíjanie dnes (adaptívna váha obnovy)."""
    p = dict(DEFAULTS)
    if params:
        for k, v in params.items():
            if v is not None:
                p[k] = v
    spread, src = expected_zco_spread(float(sig_avg_mw), float(p["rt2_zco_k"]), hour=hour)
    cc = float(p["rt2_cycle_cost"])
    m_min = float(p["rt2_margin_min_eur"])
    m_full = max(m_min + 1e-6, float(p["rt2_margin_full_eur"]))
    # Bug RT2-EFF (2026-06-12, user: "pokuta každý deň, pravidlo nabíjania príliš
    # jemné"): marže MUSIA zahŕňať round-trip straty účinnosti — pri η≈0.90 a cene
    # ~130 €/MWh je to ~13 € skrytého nákladu na každý kWh, ktorý v marži chýbal.
    #   vybi 1 kWh teraz za E[ZCO]; obnova SOC neskôr stojí DT/η  → marža = ZCO − DT/η − cc
    #   nabi 1 kWh teraz za E[ZCO]; neskôr predáš η×DT            → marža = η×DT − ZCO − cc
    eff = max(0.5, min(1.0, float(p.get("rt2_eff_rt") or 0.9025)))
    w_fix = max(0.0, min(1.0, float(p.get("rt2_restore_weight", 0.5) or 0.0)))
    # Bod 4 (adaptívna váha, user: "malo by sa meniť v čase"): mode="auto" → váha
    # per smer z plán-kontextu. Keď plán dnes ešte NABÍJA dosť kWh, RT vybitie len
    # posunie energiu (obnovu spraví plán za plánovanú cenu) → w_dis → 0. Analogicky
    # plánované VYBÍJANIE robí lacný odbyt pre RT nabitie → w_chg → 0. Bez plánu
    # v okolí = plný round-trip náklad (w=1). Škála = 10 % kapacity batérie.
    w_dis = w_chg = w_fix
    if str(p.get("rt2_restore_mode", "fixed")) == "auto" and batt_kwh:
        _scale = max(1.0, 0.10 * float(batt_kwh))
        if future_chg_kwh is not None:
            w_dis = max(0.0, min(1.0, 1.0 - float(future_chg_kwh) / _scale))
        if future_dis_kwh is not None:
            w_chg = max(0.0, min(1.0, 1.0 - float(future_dis_kwh) / _scale))
    dtp = max(0.0, float(dt_eur or 0.0))
    zco_exp = dtp + spread
    margin_dis = zco_exp - dtp - w_dis * dtp * (1.0 / eff - 1.0) - cc
    margin_chg = dtp - zco_exp - w_chg * dtp * (1.0 - eff) - cc
    if margin_dis >= m_min:
        f = min(1.0, (margin_dis - m_min) / (m_full - m_min) + 0.15)
        return 1, round(f, 3), f"v2:dis m={margin_dis:.0f} ({src})"
    if margin_chg >= m_min:
        f = min(1.0, (margin_chg - m_min) / (m_full - m_min) + 0.15)
        return -1, round(f, 3), f"v2:chg m={margin_chg:.0f} ({src})"
    return 0, 0.0, f"v2:idle m={max(margin_dis, margin_chg):.0f}"


def params_from_profile_rt(rt_cfg: Dict[str, Any],
                           cycle_cost_plan: float = None,
                           eff_rt: float = None) -> Dict[str, Any]:
    """Vyber v2 parametre z profilu rt sekcie (generické, žiadny hardcode profilu)."""
    out = dict(DEFAULTS)
    if eff_rt is not None:
        try:
            out["rt2_eff_rt"] = float(eff_rt)
        except (TypeError, ValueError):
            pass
    for k in ("rt2_margin_min_eur", "rt2_margin_full_eur", "rt2_zco_k",
              "rt2_cycle_cost", "rt2_restore_weight"):
        v = (rt_cfg or {}).get(k)
        if v is not None:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    _mode = (rt_cfg or {}).get("rt2_restore_mode")
    if _mode in ("fixed", "auto"):
        out["rt2_restore_mode"] = _mode
    if cycle_cost_plan is not None and (rt_cfg or {}).get("rt2_cycle_cost") is None:
        try:
            out["rt2_cycle_cost"] = float(cycle_cost_plan)
        except (TypeError, ValueError):
            pass
    return out
