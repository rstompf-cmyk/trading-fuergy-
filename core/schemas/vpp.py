# -*- coding: utf-8 -*-
"""core/schemas/vpp.py — VPP kontrakty (asset ↔ trading ↔ agregácia).

Single source of truth pre komunikáciu medzi modulmi cieľovej VPP architektúry
(viď memory project-modularizacia-skalovanie). Moduly (RT, VDT, DT, agregácia,
inštancie batérií) si NEodovzdávajú dáta cez CSV ani priame volania funkcií —
LEN cez tieto Pydantic kontrakty (typicky perzistované/IPC cez DB).

Smer tokov:
  • AvailabilityReport  — batéria → trading   (čo batéria vie spraviť v slote)
  • BlockAggregate      — agregácia → trading  (Σ dostupností bloku)
  • Order               — trading → trh/split  (zrealizovaný/plánovaný obchod)
  • Allocation          — trading/split → batéria (podiel bloku + setpoint)
  • ControlTick         — RT → batéria          (real-time setpoint s TTL)

Konvencia znamienka výkonu/energie (zhodná so zvyškom systému, napr.
plan_batt_kw / kwh_batt_view): **+ = vybíjanie (predaj/do siete), − = nabíjanie
(nákup/zo siete)**.
"""
from __future__ import annotations
from typing import Any, Dict, Optional, Literal
import math
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── konštanty / regexy ───────────────────────────────────────────────────────
COUNTRIES = ("sk", "cz")
TRADE_SOURCES = ("dt", "rt", "vdt")
ORDER_SIDES = ("buy", "sell")
ORDER_STATUSES = ("planned", "submitted", "filled", "cancelled", "rejected")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_finite(v: Any) -> bool:
    try:
        return math.isfinite(float(v))
    except Exception:
        return False


class _VppBase(BaseModel):
    """Spoločná konfigurácia: extra=allow (back-compat + budúce polia),
    validácia pri priradení."""
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    @field_validator("*", check_fields=False)
    @classmethod
    def _no_nan(cls, v: Any) -> Any:
        # Float polia nesmú byť NaN/inf (tichý zdroj nezmyslov v LP/efekte).
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError(f"float hodnota musí byť konečná, dostal {v}")
        return v


# ── 1. AvailabilityReport (batéria → trading) ────────────────────────────────
class AvailabilityReport(_VppBase):
    """Čo daná batéria vie spraviť v danom 15-min slote. Batéria HLÁSI; sama
    neobchoduje. Trading/agregácia z toho skladá BlockAggregate."""
    battery_id: str = Field(min_length=1)
    day: str = Field(description="YYYY-MM-DD (deň dodávky slotu)")
    slot_idx: int = Field(ge=0, le=95)
    soc_pct: float = Field(ge=0.0, le=100.0)
    free_charge_kw: float = Field(ge=0.0, description="koľko kW vie ešte NABIŤ")
    free_discharge_kw: float = Field(ge=0.0, description="koľko kW vie ešte VYBIŤ")
    free_kwh: float = Field(ge=0.0, description="voľná energia/headroom v slote")
    eff: float = Field(gt=0.0, le=1.0, default=0.95, description="round-trip účinnosť")
    limits: Dict[str, Any] = Field(default_factory=dict, description="grid/SOC limity")
    ts: str = Field(default="", description="kedy nahlásené (ISO)")

    @field_validator("day")
    @classmethod
    def _day_fmt(cls, v: str) -> str:
        v = str(v)[:10]
        if not _DAY_RE.match(v):
            raise ValueError(f"day musí byť YYYY-MM-DD, dostal '{v}'")
        return v


# ── 2. BlockAggregate (agregácia → trading) ──────────────────────────────────
class BlockAggregate(_VppBase):
    """Agregovaná dostupnosť BLOKU (Σ batérií) per slot — obchodovateľný objem."""
    block_id: str = Field(min_length=1)
    day: str
    slot_idx: int = Field(ge=0, le=95)
    agg_free_charge_kw: float = Field(ge=0.0)
    agg_free_discharge_kw: float = Field(ge=0.0)
    agg_free_kwh: float = Field(ge=0.0)
    n_batteries: int = Field(ge=0)
    ts: str = ""

    @field_validator("day")
    @classmethod
    def _day_fmt(cls, v: str) -> str:
        v = str(v)[:10]
        if not _DAY_RE.match(v):
            raise ValueError(f"day musí byť YYYY-MM-DD, dostal '{v}'")
        return v


# ── 3. Order (trading → trh / split) ─────────────────────────────────────────
class Order(_VppBase):
    """Obchod za blok na konkrétnom účte. Nesie account_id (multi-account!) +
    block_id. Po realizácii ho Split rozdelí na Allocation per batéria."""
    order_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1, description="obchodný účet (multi/krajina)")
    block_id: str = Field(min_length=1)
    country: Literal["sk", "cz"]
    day: str
    slot_idx: int = Field(ge=0, le=95)
    side: Literal["buy", "sell"]
    volume_kwh: float = Field(ge=0.0, description="objem (vždy ≥0; smer cez side)")
    price_eur_mwh: float = Field(description="cena; záporné OK, 0 NEakceptovať (real BID/ASK)")
    source: Literal["dt", "rt", "vdt"]
    status: Literal["planned", "submitted", "filled", "cancelled", "rejected"] = "planned"
    submitted_at: Optional[str] = None

    @field_validator("day")
    @classmethod
    def _day_fmt(cls, v: str) -> str:
        v = str(v)[:10]
        if not _DAY_RE.match(v):
            raise ValueError(f"day musí byť YYYY-MM-DD, dostal '{v}'")
        return v

    @field_validator("price_eur_mwh")
    @classmethod
    def _price_real(cls, v: float) -> float:
        # Pravidlo (project-vdt-trading-rules): obchod LEN s reálnou cenou; cena
        # presne 0,0 = placeholder/chýbajúca → neakceptovať. Záporné platné.
        if not _is_finite(v):
            raise ValueError("price_eur_mwh musí byť konečné")
        if float(v) == 0.0:
            raise ValueError("price_eur_mwh = 0 nie je platná reálna cena (placeholder)")
        return float(v)


# ── 4. Allocation (trading/split → batéria) ──────────────────────────────────
class Allocation(_VppBase):
    """Podiel batérie na obchode bloku + výsledný setpoint. Batéria toto VYKONÁ.
    Konvencia: + = vybíjanie, − = nabíjanie."""
    battery_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    order_id: Optional[str] = None
    day: str
    slot_idx: int = Field(ge=0, le=95)
    share_kwh: float = Field(description="podiel energie (+vybíja / −nabíja)")
    setpoint_kw: float = Field(description="setpoint (+vybíja / −nabíja)")
    source: Literal["dt", "rt", "vdt"]
    ts: str = ""

    @field_validator("day")
    @classmethod
    def _day_fmt(cls, v: str) -> str:
        v = str(v)[:10]
        if not _DAY_RE.match(v):
            raise ValueError(f"day musí byť YYYY-MM-DD, dostal '{v}'")
        return v


# ── 5. ControlTick (RT → batéria, real-time s TTL) ───────────────────────────
class ControlTick(_VppBase):
    """Real-time setpoint z RT vrstvy. Má TTL — po expirácii sa batéria vráti na
    DT/VDT plánový setpoint (RT nesmie ostať „zaseknutý"). Konvencia + vybíja / − nabíja."""
    battery_id: str = Field(min_length=1)
    ts: str = Field(min_length=1, description="ISO timestamp vydania")
    setpoint_kw: float = Field(description="+vybíja / −nabíja")
    ttl_sec: int = Field(gt=0, default=120, description="platnosť v sekundách")
    source: Literal["rt"] = "rt"


__all__ = [
    "AvailabilityReport",
    "BlockAggregate",
    "Order",
    "Allocation",
    "ControlTick",
    "COUNTRIES",
    "TRADE_SOURCES",
    "ORDER_SIDES",
    "ORDER_STATUSES",
]
