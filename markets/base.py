# -*- coding: utf-8 -*-
"""markets/base.py — MarketAdapter rozhranie (krajinová abstrakcia SK/CZ).

Jednotné rozhranie pre trhové dáta krajiny. Zvyšok appky (trading, engine, data
fetcher) má volať `adapter.<metóda>`, NIE `okte_sk` / `data_sources` priamo →
nový trh = nový adaptér, žiadny zásah do jadra; SK a CZ sa vyvíjajú paralelne.

Konvencia: metódy ktoré daná krajina/trh nepodporuje vracajú PRÁZDNE dáta
({} alebo None) — NIE výnimku (volajúci sa nemusí vetviť per krajinu).

Aditívne + dormantné: zatiaľ nič neimportuje markets/ — je to hranica pre budúce
moduly (project-modularizacia-skalovanie). Adaptéry delegujú na existujúce funkcie
cez lazy import (žiadne import-time cykly ani réžia).
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
import datetime as dt


class MarketAdapter(ABC):
    """Rozhranie trhových dát jednej krajiny ('sk'|'cz')."""
    country: str = ""

    @abstractmethod
    def dam_clearing(self, day_iso: str) -> Dict[str, float]:
        """Uzavreté DAM clearing ceny dňa: {'YYYY-MM-DD HH:MM:SS': €/MWh}. {} ak nie sú."""

    @abstractmethod
    def vdt_closed(self, day_iso: str) -> Dict[str, float]:
        """Reálne UZAVRETÉ VDT ceny dňa (ex-post): {ts: €/MWh}. {} ak nie sú."""

    @abstractmethod
    def vdt_orderbook(self, delivery_duration: int = 15) -> Dict[str, Any]:
        """Živý VDT order-book (bid/ask + likvidita). {} ak trh nepodporuje."""

    @abstractmethod
    def dam_dayahead(self, date: dt.date) -> Optional[Any]:
        """Raw DAM day-ahead fetch → DataFrame (alebo None)."""

    @abstractmethod
    def idm_intraday(self, date: dt.date) -> Optional[Any]:
        """Raw IDM intraday fetch → DataFrame (alebo None)."""

    @abstractmethod
    def imbalance(self, date: dt.date) -> Optional[Any]:
        """Imbalance / odchýlka systému (DataFrame alebo None)."""
