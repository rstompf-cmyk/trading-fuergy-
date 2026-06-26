# -*- coding: utf-8 -*-
"""core/holidays_skcz.py — štátne sviatky SK a CZ (2026-06-26).

Cenový model rozlišuje víkendy (dow), ale sviatky vo všedný deň mali doteraz cenový
profil pracovného dňa → chyba predikcie. Tento modul dáva binárny príznak `is_holiday`
(sviatok = nepracovný deň s nízkym priemyselným dopytom, cenovo bližšie k víkendu).

Bez novej závislosti: pohyblivé sviatky (Veľký piatok, Veľkonočný pondelok) z Computus
(anonymný gregoriánsky algoritmus), zvyšok fixné dátumy. Per-trh (SK/CZ sa líšia).
"""
from __future__ import annotations
from datetime import date, timedelta
from functools import lru_cache
from typing import Iterable, List


def _easter_sunday(year: int) -> date:
    """Veľkonočná nedeľa (gregoriánsky Computus / Anonymous Gregorian algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=64)
def holidays_for_year(year: int, market: str = "sk") -> frozenset:
    """Množina date objektov = štátne sviatky daného roka a trhu (sk/cz)."""
    easter = _easter_sunday(year)
    good_friday = easter - timedelta(days=2)
    easter_monday = easter + timedelta(days=1)
    days = {good_friday, easter_monday}
    mk = (market or "sk").lower()
    if mk == "cz":
        # České štátne sviatky (zákon 245/2000 Sb.)
        fixed = [(1, 1), (5, 1), (5, 8), (7, 5), (7, 6),
                 (9, 28), (10, 28), (11, 17), (12, 24), (12, 25), (12, 26)]
    else:
        # Slovenské štátne sviatky + dni pracovného pokoja (zákon 241/1993 Z.z.)
        fixed = [(1, 1), (1, 6), (5, 1), (5, 8), (7, 5), (8, 29),
                 (9, 1), (9, 15), (11, 1), (11, 17), (12, 24), (12, 25), (12, 26)]
    for mo, da in fixed:
        days.add(date(year, mo, da))
    return frozenset(days)


def is_holiday(d, market: str = "sk") -> bool:
    """True ak dátum d (date/datetime/str) je štátny sviatok na danom trhu."""
    import pandas as pd
    dd = pd.to_datetime(d).date()
    return dd in holidays_for_year(dd.year, market)


def is_holiday_series(dates: Iterable, market: str = "sk") -> List[int]:
    """Vektorovo: pre iterovateľné dátumy vráti list 0/1 (sviatok)."""
    import pandas as pd
    ts = pd.to_datetime(list(dates))
    out = []
    for t in ts:
        dd = t.date()
        out.append(1 if dd in holidays_for_year(dd.year, market) else 0)
    return out
