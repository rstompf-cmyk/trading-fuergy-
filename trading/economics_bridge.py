# -*- coding: utf-8 -*-
"""trading/economics_bridge.py — preklad výstupu EKONOMIKY na VPP Order kontrakty.

GOLDEN-BEZPEČNÉ: iba ČÍTA výstup existujúceho VDT advisora (obchody, ktoré už dnes
počíta vdt_optimizer / vdt_live_advisor) a prekladá ho na Order kontrakty. NEMENÍ
ekonomiku — žiadny dotyk LP/optimizera/cien. VDT obchod → Order 1:1:
  • action='charge'    → side='buy',  volume=charge_kwh,    price=buy_price_eur_mwh
  • action='discharge' → side='sell', volume=discharge_kwh, price=sell_price_eur_mwh

Pravidlá (project-vdt-trading-rules): obchod LEN s reálnou cenou — cena 0/None/NaN
→ PRESKOČIŤ (placeholder). Záporné ceny platné. idle/both/nulový objem → preskočiť.

Výsledné Order → dispatch_order (split → Allocation → control). Tým je ekonomika
napojená na flotilu bez prepisovania samotného rozhodovania.
"""
from __future__ import annotations
from typing import List, Optional
import math

from core.schemas.vpp import Order


def _valid_price(p) -> bool:
    if p is None:
        return False
    try:
        f = float(p)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f != 0.0   # 0 = placeholder (real BID/ASK only)


def vdt_trades_to_orders(trades: List[dict], *, block_id, account_id: str, country: str,
                         day: str, source: str = "vdt",
                         order_prefix: Optional[str] = None) -> List[Order]:
    """Preloží VDT obchody (vdt_optimizer trades) na Order kontrakty. Preskočí
    idle / nulový objem / neplatnú cenu (0/None/NaN)."""
    pref = order_prefix or source
    day = str(day)[:10]
    out: List[Order] = []
    for t in trades:
        action = str(t.get("action") or "")
        if action == "charge":
            side, vol, price = "buy", float(t.get("charge_kwh") or 0.0), t.get("buy_price_eur_mwh")
        elif action == "discharge":
            side, vol, price = "sell", float(t.get("discharge_kwh") or 0.0), t.get("sell_price_eur_mwh")
        else:
            continue  # idle / both → preskočiť
        if vol <= 0.0 or not _valid_price(price):
            continue
        try:
            slot = int(t.get("slot_idx"))
        except (TypeError, ValueError):
            continue   # bez platného slotu sa obchod nedá zaradiť
        out.append(Order(
            order_id=f"{pref}-{day}-{slot:02d}-{side}",
            account_id=str(account_id),
            block_id=str(block_id),
            country=country,
            day=day,
            slot_idx=slot,
            side=side,
            volume_kwh=round(vol, 3),
            price_eur_mwh=float(price),
            source=source,
        ))
    return out


def dispatch_vdt_trades(trades: List[dict], *, block_id, account_id: str, country: str,
                        day: str, dt_h: float = 0.25, enqueue: bool = True,
                        persist: bool = False) -> dict:
    """End-to-end: VDT obchody → Order → split → Allocation (+enqueue/persist).
    Vráti {'orders': n, 'allocations': [Allocation...], 'skipped': n}."""
    from .dispatch import dispatch_order
    orders = vdt_trades_to_orders(trades, block_id=block_id, account_id=account_id,
                                  country=country, day=day)
    all_allocs = []
    for o in orders:
        all_allocs.extend(dispatch_order(o, dt_h=dt_h, enqueue=enqueue, persist=persist))
    n_trade_rows = sum(1 for t in trades if str(t.get("action") or "") in ("charge", "discharge"))
    return {"orders": len(orders), "allocations": all_allocs,
            "skipped": max(0, n_trade_rows - len(orders))}
