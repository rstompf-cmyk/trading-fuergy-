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
)


def _imbalance_csv_path() -> str:
    try:
        import market as _mk
        root = _mk.data_dir()                 # out/sk alebo out/cz
    except Exception:
        root = os.path.join("out", "sk")
    return os.path.join(root, "imbalance_history.csv")


def _load_calibration() -> Optional[Dict[str, Any]]:
    """Binned model: sys_MW → median(zco − isot). Cache per mtime súboru."""
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
        df = pd.read_csv(p, usecols=["isot_eur", "zco_eur", "sys_MWh"])
        df = df.dropna(subset=["isot_eur", "zco_eur", "sys_MWh"])
        if len(df) < 50:
            return None
        sys_mw = pd.to_numeric(df["sys_MWh"], errors="coerce") * 4.0   # 15-min MWh → MW
        spread = (pd.to_numeric(df["zco_eur"], errors="coerce")
                  - pd.to_numeric(df["isot_eur"], errors="coerce"))
        ok = sys_mw.notna() & spread.notna()
        sys_mw, spread = sys_mw[ok].values, spread[ok].values
        # Bin edges podľa kvantilov — robustné voči outlierom, min 8 binov
        qs = np.quantile(sys_mw, np.linspace(0.02, 0.98, 9))
        centers, medians = [], []
        for i in range(len(qs) - 1):
            m = (sys_mw >= qs[i]) & (sys_mw <= qs[i + 1])
            if int(m.sum()) >= 5:
                centers.append(float(np.median(sys_mw[m])))
                medians.append(float(np.median(spread[m])))
        if len(centers) < 3:
            return None
        calib = {"mtime": mtime, "x": np.asarray(centers), "y": np.asarray(medians),
                 "n": int(len(sys_mw))}
        _CALIB_CACHE[p] = calib
        print(f"[rt_engine_v2] kalibrácia ZCO spreadu: {calib['n']} vzoriek, "
              f"{len(centers)} binov, rozsah sys {centers[0]:.0f}..{centers[-1]:.0f} MW, "
              f"spread {min(medians):.0f}..{max(medians):.0f} €/MWh")
        return calib
    except Exception as e:
        print(f"[rt_engine_v2] kalibrácia zlyhala: {e}")
        return None


def expected_zco_spread(sig_mw: float, zco_k: float) -> Tuple[float, str]:
    """E[zco − isot] pre daný signál. Kalibrácia → interpolácia; fallback lineárny."""
    calib = _load_calibration()
    if calib is not None:
        x, y = calib["x"], calib["y"]
        return float(np.interp(sig_mw, x, y)), "calib"
    return float(zco_k) * float(sig_mw), "lin"


def decide_v2(sig_avg_mw: float, dt_eur: float, soc_pct: float,
              params: Optional[Dict[str, Any]] = None) -> Tuple[int, float, str]:
    """Ekonomické RT rozhodnutie. Returns (d, f, reason) — kontrakt v1 decide_reason.

    d ∈ {-1, 0, +1} (−1 = nabíjaj nad plán, +1 = vybíjaj nad plán), f ∈ [0, 1].
    SOC/plán/grid ochrany rieši rt_controller downstream — tu LEN ekonomika.
    """
    p = dict(DEFAULTS)
    if params:
        for k, v in params.items():
            if v is not None:
                p[k] = v
    spread, src = expected_zco_spread(float(sig_avg_mw), float(p["rt2_zco_k"]))
    cc = float(p["rt2_cycle_cost"])
    m_min = float(p["rt2_margin_min_eur"])
    m_full = max(m_min + 1e-6, float(p["rt2_margin_full_eur"]))
    margin_dis = spread - cc          # E[ZCO] − DT − cycle  (spread = E[ZCO] − DT)
    margin_chg = -spread - cc         # DT − E[ZCO] − cycle
    if margin_dis >= m_min:
        f = min(1.0, (margin_dis - m_min) / (m_full - m_min) + 0.15)
        return 1, round(f, 3), f"v2:dis m={margin_dis:.0f} ({src})"
    if margin_chg >= m_min:
        f = min(1.0, (margin_chg - m_min) / (m_full - m_min) + 0.15)
        return -1, round(f, 3), f"v2:chg m={margin_chg:.0f} ({src})"
    return 0, 0.0, f"v2:idle m={max(margin_dis, margin_chg):.0f}"


def params_from_profile_rt(rt_cfg: Dict[str, Any],
                           cycle_cost_plan: float = None) -> Dict[str, Any]:
    """Vyber v2 parametre z profilu rt sekcie (generické, žiadny hardcode profilu)."""
    out = dict(DEFAULTS)
    for k in ("rt2_margin_min_eur", "rt2_margin_full_eur", "rt2_zco_k", "rt2_cycle_cost"):
        v = (rt_cfg or {}).get(k)
        if v is not None:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    if cycle_cost_plan is not None and (rt_cfg or {}).get("rt2_cycle_cost") is None:
        try:
            out["rt2_cycle_cost"] = float(cycle_cost_plan)
        except (TypeError, ValueError):
            pass
    return out
