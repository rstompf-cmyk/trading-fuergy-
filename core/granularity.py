# -*- coding: utf-8 -*-
"""core/granularity.py — pomocníci pre zjednotenie 60-min ↔ 15-min plánu (2026-06-18).

Cieľ (user): 15-min je canonical; hodinový pohľad = PRIEMER 15-min. D+1 predikcia:
hodinovú cenu rozkopírovať na 15-min (rovnaká cena v hodine, NIE /4). Reálne OTE
15-min ceny sa použijú keď sú dostupné.

Čisté funkcie, žiadny I/O — plne testovateľné. Konvencia:
  • CENA (€/MWh) sa pri upsample HODINA→15min KOPÍRUJE (np.repeat), nie delí.
  • ENERGIA/VÝKON priemer (resample 15min→hodina) = mean (kW) alebo sum (kWh).
"""
from __future__ import annotations
from typing import List, Sequence
import numpy as np


def upsample_price_h_to_15(price_h: Sequence[float]) -> List[float]:
    """24 hodinových cien → 96 × 15-min (cena rovnaká v rámci hodiny). NIE delenie."""
    arr = np.asarray(list(price_h), dtype=float)
    if arr.size == 0:
        return [0.0] * 96
    out = np.repeat(arr[:24], 4)
    if out.size < 96:
        out = np.concatenate([out, np.full(96 - out.size, out[-1] if out.size else 0.0)])
    return [float(x) for x in out[:96]]


def upsample_series_h_to_15(arr_h: Sequence[float], *, divide: bool = False) -> List[float]:
    """24 hodinových hodnôt → 96. divide=False (default) = kopíruj (kW, multiplikátory,
    ceny). divide=True = rozdeľ na 4 (kWh energia za hodinu → kWh za 15-min slot)."""
    arr = np.asarray(list(arr_h), dtype=float)
    if arr.size == 0:
        return [0.0] * 96
    out = np.repeat(arr[:24], 4)
    if divide:
        out = out / 4.0
    if out.size < 96:
        out = np.concatenate([out, np.full(96 - out.size, 0.0)])
    return [float(x) for x in out[:96]]


def upsample_mask_h_to_15(mask_h: Sequence) -> list:
    """24-prvková maska (RT on/off, bool/0-1) → 96 (každá hodina × 4)."""
    out = []
    for v in list(mask_h)[:24]:
        out.extend([v] * 4)
    while len(out) < 96:
        out.append(out[-1] if out else (mask_h[-1] if len(mask_h) else 1))
    return out[:96]


def resample_15_to_h_mean(arr15: Sequence[float]) -> List[float]:
    """96 × 15-min → 24 hodinových PRIEMEROV (pre kW, ceny, SOC %). Doplní 0 ak <96."""
    arr = np.asarray(list(arr15), dtype=float)
    if arr.size < 96:
        arr = np.concatenate([arr, np.zeros(96 - arr.size)])
    return [float(arr[h * 4:h * 4 + 4].mean()) for h in range(24)]


def resample_15_to_h_sum(arr15: Sequence[float]) -> List[float]:
    """96 × 15-min → 24 hodinových SÚČTOV (pre kWh energiu za slot)."""
    arr = np.asarray(list(arr15), dtype=float)
    if arr.size < 96:
        arr = np.concatenate([arr, np.zeros(96 - arr.size)])
    return [float(arr[h * 4:h * 4 + 4].sum()) for h in range(24)]


def resample_15_to_h_last(arr15: Sequence[float]) -> List[float]:
    """96 → 24, posledná hodnota v hodine (pre SOC stav na konci hodiny)."""
    arr = list(arr15)
    if len(arr) < 96:
        arr = arr + [arr[-1] if arr else 0.0] * (96 - len(arr))
    return [float(arr[h * 4 + 3]) for h in range(24)]
