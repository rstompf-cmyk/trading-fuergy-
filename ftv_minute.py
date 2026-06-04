# -*- coding: utf-8 -*-
"""
ftv_minute.py — konverzia hodinovej FTV predikcie na realistický 1-min priebeh.

Motivácia
---------
Plán D-1 sa robí na hodinových priemeroch FTV (`kw [kW]`). Realita ale nie je
skoková — počas hodiny PV výroba kolíše kvôli mračnám, slnečnej geometrii a
náhodným poruchám. Tento rozdiel medzi hodinovým plánom a minútovou realitou
generuje odchýlku (ZCO), ktorú v simulácii chceme:
  1) modelovať fyzikálne (minútový priebeh blízky realite),
  2) eliminovať batériou cez RT vrstvu.

Tento modul vytvára minútový priebeh z hodinových priemerov tak, aby:
  - hodinová ENERGIA (kWh) sa **zachovala** (renormalizácia per hodina),
  - krivka bola plynulá medzi hodinami (lineárna interpolácia stredmi hodín),
  - obsahovala realistický **mračnový šum** (AR(1) multiplikatívny),
  - bola fyzikálne rozumná (clip na nezáporné, max ~1.4× hladkej krivky).

Profil "búrlivý" (default):
  - base_noise = 0.15   (σ ~ 15 % aktuálnej hodnoty)
  - phi = 0.95          (AR(1) persistencia ~10–15 min)
  - clip [0.10, 1.40]   (búrlivé počasie môže ísť silne dole/hore)

Použitie:
    import ftv_minute as fm
    hourly_kw = np.array([... 24 hodnôt v kW ...])
    minute_kw = fm.hourly_to_minute(hourly_kw)            # náhodný (default)
    # reprodukovateľný:
    minute_kw = fm.hourly_to_minute(hourly_kw, seed=42)
"""
from __future__ import annotations
import numpy as np
from typing import Optional


# Profil "búrlivý" — užívateľská preferencia (silnejšie kolísanie pre konzervatívny stress-test).
DEFAULT_BASE_NOISE = 0.15
DEFAULT_PHI = 0.95
DEFAULT_CLIP_LO = 0.10
DEFAULT_CLIP_HI = 1.40


def hourly_to_minute(hour_kw_24, base_noise: float = DEFAULT_BASE_NOISE,
                     phi: float = DEFAULT_PHI, seed: Optional[int] = None,
                     clip_lo: float = DEFAULT_CLIP_LO,
                     clip_hi: float = DEFAULT_CLIP_HI) -> np.ndarray:
    """Vytvorí minútový (1440 hodnôt) FTV priebeh z hodinových priemerov.

    Parametre
    ---------
    hour_kw_24 : array-like dĺžky 24
        Hodinové priemery FTV výroby [kW] (00–23 h).
    base_noise : float
        Štandardná odchýlka šumu ako podiel aktuálnej hodnoty (default 0.15 = 15 %).
    phi : float
        AR(1) persistencia šumu (default 0.95 → ~10–15 min decorrelation).
    seed : int alebo None
        Ak None (default), každé volanie dá iný náhodný priebeh.
        Pre reprodukovateľnosť posli pevný integer.
    clip_lo, clip_hi : float
        Spodný a horný limit mračnového faktora.

    Vracia
    ------
    np.ndarray dĺžky 1440 — minútový FTV priebeh [kW]. Hodinová energia sa zachová.
    """
    h = np.asarray(hour_kw_24, dtype=float).ravel()
    if h.size != 24:
        raise ValueError(f"hourly_to_minute: očakávam 24 hodnôt, dostal {h.size}")
    # 1) baseline = lineárna interpolácia stredmi hodín (xx:30) na 1-min grid
    hour_centers = np.arange(24) + 0.5                   # 0.5, 1.5, …, 23.5
    minute_axis = np.arange(1440) / 60.0                 # v hodinách
    base_kw = np.interp(minute_axis, hour_centers, h, left=h[0], right=h[-1])

    # 2) AR(1) "cloud" šum
    rng = np.random.default_rng(seed)
    sigma_innov = np.sqrt(max(0.0, 1.0 - phi * phi))     # stacionárny σ = 1
    eps = rng.standard_normal(1440)
    z = np.empty(1440)
    z[0] = eps[0]
    for i in range(1, 1440):
        z[i] = phi * z[i - 1] + sigma_innov * eps[i]
    cloud = np.clip(1.0 + base_noise * z, clip_lo, clip_hi)
    # cez noc (base_kw ~ 0) žiadne mračná — výroba musí ostať 0
    cloud = np.where(base_kw > 1e-6, cloud, 1.0)
    minute_kw = np.maximum(0.0, base_kw * cloud)

    # 3) renormalizácia per hodina aby hodinová energia presne zodpovedala vstupu
    # (priemer minútových hodnôt v hodine i = h[i])
    M = minute_kw.reshape(24, 60)
    new_mean = M.mean(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        # h[i] = 0 → minútové hodnoty v tej hodine = 0 (noc / žiadna produkcia)
        # h[i] > 0 → škálovanie tak aby priemer = h[i]
        scale = np.where((new_mean > 1e-6) & (h > 1e-6), h / np.where(new_mean > 1e-6, new_mean, 1.0), 0.0)
        # ak h[i] > 0 ale new_mean = 0 (extrém), fallback na konštantu h[i]
        const_fallback = (h > 1e-6) & (new_mean <= 1e-6)
    M = M * scale[:, None]
    # extrémny prípad: nahraď konštantou h[i]
    if const_fallback.any():
        for i in np.where(const_fallback)[0]:
            M[i, :] = h[i]
    minute_kw = M.flatten()
    # ešte raz orežeme nezápornosť (numericky)
    return np.maximum(0.0, minute_kw)


def minute_to_15min(minute_kw) -> np.ndarray:
    """Pomocná: agreguje 1-min priebeh na 96 × 15-min priemerov [kW]."""
    arr = np.asarray(minute_kw, float).ravel()
    if arr.size != 1440:
        raise ValueError(f"minute_to_15min: očakávam 1440 hodnôt, dostal {arr.size}")
    return arr.reshape(96, 15).mean(axis=1)


def minute_to_hourly(minute_kw) -> np.ndarray:
    """Pomocná: agreguje 1-min priebeh na 24 hodinových priemerov [kW]."""
    arr = np.asarray(minute_kw, float).ravel()
    if arr.size != 1440:
        raise ValueError(f"minute_to_hourly: očakávam 1440 hodnôt, dostal {arr.size}")
    return arr.reshape(24, 60).mean(axis=1)
