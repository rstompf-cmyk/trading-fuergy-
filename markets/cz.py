# -*- coding: utf-8 -*-
"""markets/cz.py — CZ adaptér (OTE / ČEPS). Deleguje na data_sources."""
from __future__ import annotations
from typing import Any, Dict, Optional
import datetime as dt

from .base import MarketAdapter


class CZAdapter(MarketAdapter):
    country = "cz"

    def dam_clearing(self, day_iso: str) -> Dict[str, float]:
        # CZ nemá SEPS-štýl cache {ts:price}; clearing sa berie cez dam_dayahead().
        # TODO: ak treba {ts:price} cache, derivovať z fetch_ote_dayahead. Zatiaľ {}.
        return {}

    def vdt_closed(self, day_iso: str) -> Dict[str, float]:
        return {}   # CZ VDT (uzavreté ceny) zatiaľ neimplementované

    def vdt_orderbook(self, delivery_duration: int = 15) -> Dict[str, Any]:
        return {}   # CZ VDT order-book zatiaľ neimplementované

    def dam_dayahead(self, date: dt.date) -> Optional[Any]:
        import data_sources as ds
        return ds.fetch_ote_dayahead(date)

    def idm_intraday(self, date: dt.date) -> Optional[Any]:
        import data_sources as ds
        return ds.fetch_ote_intraday(date)

    def imbalance(self, date: dt.date) -> Optional[Any]:
        import data_sources as ds
        return ds.fetch_ote_imbalance(date)
