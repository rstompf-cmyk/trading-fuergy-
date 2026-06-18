# -*- coding: utf-8 -*-
"""markets/sk.py — SK adaptér (OKTE / SEPS). Deleguje na existujúce moduly."""
from __future__ import annotations
from typing import Any, Dict, Optional
import datetime as dt

from .base import MarketAdapter


class SKAdapter(MarketAdapter):
    country = "sk"

    def dam_clearing(self, day_iso: str) -> Dict[str, float]:
        import seps_sk
        return seps_sk.load_okte_dt_for_day(str(day_iso)[:10]) or {}

    def vdt_closed(self, day_iso: str) -> Dict[str, float]:
        import seps_sk
        return seps_sk.load_okte_vdt_for_day(str(day_iso)[:10]) or {}

    def vdt_orderbook(self, delivery_duration: int = 15) -> Dict[str, Any]:
        import okte_vdt
        return okte_vdt.get_orderbook(delivery_duration=delivery_duration) or {}

    def dam_dayahead(self, date: dt.date) -> Optional[Any]:
        import okte_sk
        return okte_sk.fetch_okte_dayahead(date)

    def idm_intraday(self, date: dt.date) -> Optional[Any]:
        import okte_sk
        return okte_sk.fetch_okte_intraday(date)

    def imbalance(self, date: dt.date) -> Optional[Any]:
        # SK imbalance/ZCO zdroj je dnes rozdrobený v seps_sk realtime + imbalance_history.
        # TODO: zjednotiť za jednu funkciu a sem delegovať. Zatiaľ None (volajúci si poradí).
        return None
