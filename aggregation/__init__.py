# -*- coding: utf-8 -*-
"""aggregation/ — VPP agregačná vrstva (blok ↔ batérie).

aggregate_block: Σ dostupností bloku → BlockAggregate (pre trading).
split_order: objem obchodu bloku → [Allocation] per batéria (podľa stratégie).
"""
from .split import aggregate_block, split_order

__all__ = ["aggregate_block", "split_order"]
