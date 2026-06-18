# -*- coding: utf-8 -*-
"""markets/ — krajinové adaptéry trhových dát za jednotným MarketAdapter rozhraním.

Použitie:
    from markets import get_adapter
    adapter = get_adapter("sk")
    ceny = adapter.dam_clearing("2026-06-18")
    book = adapter.vdt_orderbook(15)
"""
from __future__ import annotations
from typing import Dict, Type

from .base import MarketAdapter
from .sk import SKAdapter
from .cz import CZAdapter

_ADAPTERS: Dict[str, Type[MarketAdapter]] = {"sk": SKAdapter, "cz": CZAdapter}
_cache: Dict[str, MarketAdapter] = {}


def get_adapter(country: str) -> MarketAdapter:
    """Vráti (cached) adaptér pre krajinu 'sk'|'cz'. ValueError pri neznámej."""
    c = str(country or "").lower()
    if c not in _ADAPTERS:
        raise ValueError(f"neznáma krajina '{country}', povolené: {list(_ADAPTERS)}")
    if c not in _cache:
        _cache[c] = _ADAPTERS[c]()
    return _cache[c]


__all__ = ["MarketAdapter", "SKAdapter", "CZAdapter", "get_adapter"]
