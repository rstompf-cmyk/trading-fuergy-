# -*- coding: utf-8 -*-
"""core/schemas/plan.py — Pydantic schema pre uložené plány (D-1 + dentrh).

Single source of truth pre tvar StoredPlan dictu, ktorý `plan_store` číta a píše.
Validuje:
  - kind ∈ {"plan", "dentrh", "dam_d1"}
  - step_min ∈ {15, 60}
  - schedule[*] dĺžka == 24 (60-min) alebo 96 (15-min) — pre tie polia ktoré sú prítomné
  - mults / rt_mask dĺžka == N alebo None / []
  - date je YYYY-MM-DD

Forward-compat:
  - `extra="allow"` na všetkých modeloch (príde nové pole → nepadne)
  - schedule kľúče sú voľné (allow_extra) — môžu pribudnúť nové optimizer výstupy

Backward-compat:
  - Všetky polia okrem date/step_min/kind/schedule majú default — staré plány bez
    `mults`, `rt_mask`, `block_planned_discharge` atď. sa validujú bez problému.
  - `params` / `summary` / `meta` sú dict s `extra="allow"` (žiadne pole nevynucujeme).

Použitie:
    from core.schemas.plan import StoredPlan
    plan = StoredPlan.model_validate(json_dict)
    plan.expected_slots()        # 24 alebo 96
    plan.has_field("batt_kw")    # True / False
"""
from __future__ import annotations
from typing import Any, Dict, List, Literal, Optional
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ── konštanty ──────────────────────────────────────────────────────────────
ALLOWED_KINDS = ("plan", "dentrh", "dam_d1")
ALLOWED_STEPS = (15, 60)

# Schedule polia ktoré poznáme — `extra="allow"` v `_PlanSchedule` umožní pridať nové.
KNOWN_SCHEDULE_KEYS = (
    "pv_kwh", "load_kwh", "price_eur", "batt_kw", "grid_kwh",
    "order_mwh", "curtail_kwh", "soc_pct", "soc_kwh",
    "_charge_kw", "_discharge_kw", "_export_kwh", "_import_kwh",
)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ── PlanSlot — semantický pohľad na jeden časový slot ──────────────────────
class PlanSlot(BaseModel):
    """Jeden časový slot v pláne.

    Používa sa keď chceme robiť per-slot operácie. `plan_store` interne ukladá
    schedule ako dict[str, list[float]], ale `StoredPlan.iter_slots()` umožní
    iterovať `PlanSlot` inštancie.
    """
    model_config = ConfigDict(extra="allow")

    slot: int = Field(ge=0, le=95)
    pv_kwh: float = 0.0
    load_kwh: float = 0.0
    price_eur: float = 0.0
    batt_kw: float = 0.0
    grid_kwh: float = 0.0
    order_mwh: float = 0.0
    curtail_kwh: float = 0.0
    soc_pct: float = 0.0
    soc_kwh: float = 0.0
    charge_kw: float = 0.0
    discharge_kw: float = 0.0
    export_kwh: float = 0.0
    import_kwh: float = 0.0


class _PlanSchedule(BaseModel):
    """Wrapper okolo schedule dict-u. Neukladáme ako BaseModel pole-by-pole
    (príliš striktné), ale validujeme cez koreňový model_validator."""
    model_config = ConfigDict(extra="allow")


# ── StoredPlan — top-level model ───────────────────────────────────────────
class StoredPlan(BaseModel):
    """Pydantic schema pre `plan_store.save_plan` / `load_plan` JSON.

    Polia zodpovedajú JSON-u na disku 1:1 + sú zoradené tak ako ich tvorí save_plan.
    Spätne kompatibilné — staršie plány bez `mults` / `rt_mask` / `meta` prejdú.
    """
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    # Identita plánu
    date: str
    step_min: Literal[15, 60]
    kind: Literal["plan", "dentrh", "dam_d1"]
    profile: str = "default"
    generated_at: Optional[str] = None

    # Telo plánu
    # `schedule` má voľný value-type (List[Any]) — staršie plány majú "period"
    # ako list stringov ("00:00-00:15"), iné majú cena_EUR / ch_kwh / cu_kwh /
    # di_kwh / ex_kwh / im_kwh (shorthand kľúče). Validujeme len dĺžky.
    params: Dict[str, Any] = Field(default_factory=dict)
    schedule: Dict[str, List[Any]] = Field(default_factory=dict)
    summary: Dict[str, Any] = Field(default_factory=dict)
    meta: Dict[str, Any] = Field(default_factory=dict)

    # Šablóny použité pri behu
    mults: Optional[List[Optional[float]]] = None
    rt_mask: Optional[List[Optional[float]]] = None

    # Top-level flagy z form-u
    block_planned_discharge: bool = False
    zco_bias_w: float = 0.0
    rt_freedom: bool = True

    # ── validátory polí ─────────────────────────────────────────────────────
    @field_validator("date")
    @classmethod
    def _date_format(cls, v: str) -> str:
        v = str(v).strip()
        if not _DATE_RE.match(v):
            raise ValueError(f"date musí byť YYYY-MM-DD, dostal '{v}'")
        return v

    @field_validator("profile")
    @classmethod
    def _profile_chars(cls, v: str) -> str:
        v = str(v).strip() or "default"
        if not re.match(r"^[A-Za-z0-9_\-]+$", v):
            raise ValueError(
                f"profile smie obsahovať len A-Za-z0-9_-, dostal '{v}'"
            )
        return v

    # ── cross-field validácia ──────────────────────────────────────────────
    @model_validator(mode="after")
    def _check_consistency(self):
        n = self.expected_slots()

        # 1) Schedule dĺžka — pre každý prítomný kľúč
        #    Prázdne pole [] tolerujeme ako "kľúč nie je v praxi prítomný"
        #    (rovnaká sémantika ako pre mults/rt_mask).
        for k, arr in (self.schedule or {}).items():
            if arr is None or len(arr) == 0:
                continue
            if len(arr) != n:
                raise ValueError(
                    f"schedule['{k}'] má dĺžku {len(arr)}, očakávam {n} "
                    f"(step_min={self.step_min})"
                )

        # 2) mults / rt_mask — buď None/[] alebo dĺžka n
        for name in ("mults", "rt_mask"):
            arr = getattr(self, name)
            if arr is None or len(arr) == 0:
                continue
            if len(arr) != n:
                raise ValueError(
                    f"{name} má dĺžku {len(arr)}, očakávam {n} "
                    f"(step_min={self.step_min})"
                )

        return self

    # ── convenience helpery ────────────────────────────────────────────────
    def expected_slots(self) -> int:
        """Očakávaný počet slotov pre `step_min` (24 pre 60-min, 96 pre 15-min)."""
        return 96 if int(self.step_min) == 15 else 24

    def has_field(self, key: str) -> bool:
        """True ak `schedule` obsahuje kľúč s ne-prázdnym poľom."""
        arr = (self.schedule or {}).get(key)
        return arr is not None and len(arr) > 0

    def get_array(self, key: str, default: float = 0.0) -> List[float]:
        """Vráti list dĺžky `expected_slots`. Chýbajúce / None hodnoty nahradí default."""
        n = self.expected_slots()
        arr = (self.schedule or {}).get(key) or []
        out: List[float] = []
        for i in range(n):
            try:
                v = arr[i]
            except IndexError:
                v = None
            out.append(float(v) if v is not None else float(default))
        return out

    def iter_slots(self) -> List[PlanSlot]:
        """Vytvor `PlanSlot` inštancie z `schedule` dict-u (per-slot view)."""
        n = self.expected_slots()
        out: List[PlanSlot] = []
        for i in range(n):
            kwargs: Dict[str, Any] = {"slot": i}
            for sch_key in KNOWN_SCHEDULE_KEYS:
                arr = (self.schedule or {}).get(sch_key) or []
                if i >= len(arr):
                    continue
                v = arr[i]
                if v is None:
                    continue
                # Mapping _charge_kw → charge_kw atď. (DB-friendly mená)
                slot_key = sch_key.lstrip("_")
                kwargs[slot_key] = float(v)
            out.append(PlanSlot(**kwargs))
        return out


__all__ = [
    "PlanSlot",
    "StoredPlan",
    "ALLOWED_KINDS",
    "ALLOWED_STEPS",
    "KNOWN_SCHEDULE_KEYS",
]
