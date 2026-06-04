# -*- coding: utf-8 -*-
"""
load_minute.py — konverzia 15-min spotreby zákazníka na realistický 1-min priebeh.

Analógia k ftv_minute.py, ale pre LOAD (spotrebu). Rozdiely oproti FTV:
  - menší šum (load je stabilnejší ako PV; chladnička/HVAC majú dlhé cykly, ale globálne ~5–10 % σ),
  - vyššia persistencia (load drží trend ~20 min vs ~10 min pri PV),
  - žiadne "nočné nuly" — load môže byť 0 v 15-min slote a vyskočiť (napr. termo),
    ale celkovo žiadne strict-zero gating ako pri FTV.

Použitie:
    import load_minute as lm
    load_15min = np.array([... 96 hodnôt v kW ...])
    load_1min = lm.fifteen_to_minute(load_15min)
"""
from __future__ import annotations
import numpy as np
from typing import Optional


# Profil "domácnosť / malá komerciálka" — pomerne stabilná spotreba
DEFAULT_BASE_NOISE = 0.05            # σ ~ 5 % aktuálnej hodnoty
DEFAULT_PHI = 0.90                   # AR(1) persistencia ~10 min decorrelation
DEFAULT_CLIP_LO = 0.50               # spotreba môže ísť na polovicu
DEFAULT_CLIP_HI = 1.50               # alebo 1.5× hore


def fifteen_to_minute(slot15_kw_96, base_noise: float = DEFAULT_BASE_NOISE,
                       phi: float = DEFAULT_PHI, seed: Optional[int] = None,
                       clip_lo: float = DEFAULT_CLIP_LO,
                       clip_hi: float = DEFAULT_CLIP_HI) -> np.ndarray:
    """Vytvorí minútový (1440 hodnôt) load priebeh z 15-min priemerov.

    Parametre
    ---------
    slot15_kw_96 : array-like dĺžky 96
        15-min priemery spotreby [kW] (00:00–23:45).
    base_noise : float
        Štandardná odchýlka šumu ako podiel aktuálnej hodnoty (default 0.05 = 5 %).
    phi : float
        AR(1) persistencia šumu (default 0.90).
    seed : int alebo None
        Pre reprodukovateľnosť posli pevný integer.

    Vracia
    ------
    np.ndarray dĺžky 1440 — minútový load priebeh [kW]. 15-min energia sa zachová.
    """
    s = np.asarray(slot15_kw_96, dtype=float).ravel()
    if s.size != 96:
        raise ValueError(f"fifteen_to_minute: očakávam 96 hodnôt, dostal {s.size}")
    # 1) baseline = lineárna interpolácia stredmi 15-min slotov na 1-min grid
    slot_centers_min = np.arange(96) * 15 + 7.5          # stred slotu v minútach
    minute_axis = np.arange(1440, dtype=float)
    base_kw = np.interp(minute_axis, slot_centers_min, s, left=s[0], right=s[-1])

    # 2) AR(1) šum
    rng = np.random.default_rng(seed)
    sigma_innov = np.sqrt(max(0.0, 1.0 - phi * phi))
    eps = rng.standard_normal(1440)
    z = np.empty(1440)
    z[0] = eps[0]
    for i in range(1, 1440):
        z[i] = phi * z[i - 1] + sigma_innov * eps[i]
    factor = np.clip(1.0 + base_noise * z, clip_lo, clip_hi)
    # ak je base_kw ~ 0, šum drž na 1.0 (žiadny "fantom load" v prázdnom slote)
    factor = np.where(base_kw > 1e-6, factor, 1.0)
    minute_kw = np.maximum(0.0, base_kw * factor)

    # 3) renormalizácia per 15-min slot aby energia zostala
    M = minute_kw.reshape(96, 15)
    new_mean = M.mean(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = np.where((new_mean > 1e-6) & (s > 1e-6),
                          s / np.where(new_mean > 1e-6, new_mean, 1.0), 0.0)
        const_fallback = (s > 1e-6) & (new_mean <= 1e-6)
    M = M * scale[:, None]
    if const_fallback.any():
        for i in np.where(const_fallback)[0]:
            M[i, :] = s[i]
    return np.maximum(0.0, M.flatten())


def minute_to_15min(minute_kw) -> np.ndarray:
    """Pomocná: agreguje 1-min priebeh na 96 × 15-min priemerov [kW]."""
    arr = np.asarray(minute_kw, float).ravel()
    if arr.size != 1440:
        raise ValueError(f"minute_to_15min: očakávam 1440 hodnôt, dostal {arr.size}")
    return arr.reshape(96, 15).mean(axis=1)


def fifteen_to_hourly(slot15_kw_96) -> np.ndarray:
    """Pomocná: agreguje 96 × 15-min priemerov na 24 hodinových priemerov [kW]."""
    arr = np.asarray(slot15_kw_96, float).ravel()
    if arr.size != 96:
        raise ValueError(f"fifteen_to_hourly: očakávam 96 hodnôt, dostal {arr.size}")
    return arr.reshape(24, 4).mean(axis=1)
