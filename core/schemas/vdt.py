# -*- coding: utf-8 -*-
"""core/schemas/vdt.py — Pydantic schema pre VDT paper-trading záznamy.

Single source of truth pre tvar riadku v `out/sk/vdt_paper_trades.csv`:
  ts, profile, slot, action, kw, kwh, price_predicted_eur,
  soc_before_pct, soc_after_pct, soc_source, profit_eur_rest_of_day

Validuje:
  - profile NIE je prázdny (Bug 441 — riadky bez profile sa zhlukli a leakovali graf)
  - slot vo formáte "HH:MM" alebo "HH:MM-HH:MM"
  - action ∈ {charge, discharge, idle, buy, sell, curtail_ftv, load_cover, both, vdt_buy, vdt_sell}
  - kw, kwh, price_predicted_eur sú konečné float (žiadne NaN/inf)
  - soc_before_pct, soc_after_pct ∈ [0, 100] (s toleranciou)

Gate Bug UU:
  `should_log_vdt_for_profile(profile)` vracia False keď profile.plan.joint_lp.use_vdt:false
  → built-in protekcia pred zápisom fake VDT trades pri vypnutom obchodovaní.
"""
from __future__ import annotations
from typing import Any, Optional, Literal
import math
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── konštanty ──────────────────────────────────────────────────────────────
ALLOWED_ACTIONS = (
    "charge", "discharge", "idle", "both",
    "buy", "sell",
    "curtail_ftv", "load_cover",
    "vdt_buy", "vdt_sell",
)

# "HH:MM" alebo "HH:MM-HH:MM"
_SLOT_SINGLE_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_SLOT_RANGE_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


def _is_finite(v: Any) -> bool:
    try:
        f = float(v)
        return math.isfinite(f)
    except Exception:
        return False


# ── VDTTrade model ─────────────────────────────────────────────────────────
class VDTTrade(BaseModel):
    """Jeden riadok v vdt_paper_trades.csv.

    Použitie:
        from core.schemas.vdt import VDTTrade
        trade = VDTTrade.model_validate({...row...})
        trade.is_significant()   # filter pre idle / 0-kWh záznamy
    """
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    ts: str = Field(min_length=1, description="ISO timestamp YYYY-MM-DDTHH:MM:SS")
    profile: str = Field(min_length=1, max_length=64,
                          description="Per-profile gate (Bug 441)")
    slot: str = Field(min_length=4, description="HH:MM alebo HH:MM-HH:MM")
    action: str = Field(min_length=1)
    kw: float = 0.0
    kwh: float = 0.0
    price_predicted_eur: float = 0.0          # €/MWh
    soc_before_pct: float = 50.0
    soc_after_pct: float = 50.0
    soc_source: str = ""
    profit_eur_rest_of_day: float = 0.0

    # ── validátory polí ─────────────────────────────────────────────────────
    @field_validator("profile")
    @classmethod
    def _profile_chars(cls, v: str) -> str:
        v = str(v).strip()
        if not v:
            raise ValueError("profile nesmie byť prázdny (Bug 441)")
        # Toleranter ako StoredPlan — povolíme aj diakritiku v custom profil menách
        if not re.match(r"^[\w\-\.]+$", v, re.UNICODE):
            raise ValueError(
                f"profile smie obsahovať len písmená/číslice/_/-/., dostal '{v}'"
            )
        return v

    @field_validator("slot")
    @classmethod
    def _slot_format(cls, v: str) -> str:
        v = str(v).strip()
        if _SLOT_SINGLE_RE.match(v) or _SLOT_RANGE_RE.match(v):
            return v
        raise ValueError(
            f"slot musí byť HH:MM alebo HH:MM-HH:MM, dostal '{v}'"
        )

    @field_validator("action")
    @classmethod
    def _action_known(cls, v: str) -> str:
        v = str(v).strip().lower()
        if v not in ALLOWED_ACTIONS:
            raise ValueError(
                f"action='{v}' neznáma; allowed={list(ALLOWED_ACTIONS)}"
            )
        return v

    @field_validator("kw", "kwh", "price_predicted_eur", "profit_eur_rest_of_day")
    @classmethod
    def _finite_floats(cls, v: float) -> float:
        if not _is_finite(v):
            raise ValueError(f"hodnota musí byť konečná, dostal {v}")
        return float(v)

    @field_validator("soc_before_pct", "soc_after_pct")
    @classmethod
    def _soc_range(cls, v: float) -> float:
        if not _is_finite(v):
            raise ValueError(f"SOC musí byť konečné číslo, dostal {v}")
        f = float(v)
        # Tolerancia ±5% (legacy zaznamy s prekročením kraja)
        if f < -5.0 or f > 105.0:
            raise ValueError(f"SOC mimo rozsah [0, 100], dostal {f}")
        return max(0.0, min(100.0, f))   # clamp

    # ── convenience helpery ────────────────────────────────────────────────
    def is_significant(self) -> bool:
        """True ak trade nie je no-op (idle / <0.5 kWh)."""
        if self.action == "idle":
            return False
        return abs(self.kwh) >= 0.5

    def is_buy(self) -> bool:
        return self.action in ("charge", "buy", "vdt_buy", "load_cover")

    def is_sell(self) -> bool:
        return self.action in ("discharge", "sell", "vdt_sell", "curtail_ftv")


# ── Gate helper pre Bug UU (use_vdt:false) ─────────────────────────────────
def should_log_vdt_for_profile(profile: str) -> bool:
    """True ak smieme zapísať VDT paper trade pre tento profil.

    Čítame `profile.plan.joint_lp.use_vdt` cez Pydantic schemu z `core.schemas.profile`.
    Semantika (mirror existujúceho Bug UU gate v auto_control):
      - explicit `joint_lp.use_vdt:false`  → False (blokovať)
      - explicit `joint_lp.use_vdt:true`   → True
      - chýba (legacy bez joint_lp)        → True (default, back-compat)

    POZOR: úmyselne NEvoláme `ProfileConfig.uses_vdt()` ktorá je striktná
    (vyžaduje aj `enabled=True`). Tu chceme len blokovať keď je užívateľ
    explicitne proti VDT obchodovaniu — nie keď nemá joint_lp zapnuté.

    Bug UU (2026-06-08):
        Simulacia_Coop má joint_lp.use_vdt:false ale auto_control predtým logoval VDT
        extras. Tieto fake trades sa neskôr cez Bug V/X aggregát skreslili plan_batt_kw
        v /livesim. Gate to zachytí pred zápisom — bez ohľadu na to ktorý writer volá.
    """
    result = True   # default = allow (back-compat pre legacy profily)
    try:
        import profiles as _pr
        # Pydantic schema iba pre type-safe access (extra=allow zachová use_vdt:false)
        try:
            cfg = _pr.load_profile_validated(profile)
            if cfg is not None:
                # Explicit False = block; inak True (default + legacy)
                result = cfg.plan.joint_lp.use_vdt is not False
                _maybe_audit_gate(profile, result, source="pydantic")
                return result
        except Exception:
            pass
        # Fallback: legacy dict čítanie
        p = _pr.load_profile(profile) or {}
        plan = p.get("plan") or {}
        jlp = plan.get("joint_lp") or {}
        use_vdt = jlp.get("use_vdt")
        if use_vdt is None:
            result = True   # legacy default
        else:
            result = bool(use_vdt)
        _maybe_audit_gate(profile, result, source="dict_fallback")
        return result
    except Exception as e:
        # Bezpečnejšie pokračovať (legacy profily bez joint_lp.use_vdt)
        print(f"[vdt.should_log_vdt_for_profile] {profile}: gate check zlyhal: {e}")
        return True


def _maybe_audit_gate(profile: str, allowed: bool, source: str) -> None:
    """Loguje IBA blokované eventy (rare path, low-volume).

    Volajúci writer zapisuje sám seba — gate sám sa hlási iba ak BLOKUJE
    (= action=vdt_blocked_by_gate). Cieľ: zviditeľniť tichý gating bez
    spam-u v allow-prípade.
    """
    if allowed:
        return
    try:
        from core.audit_log import log_event
        log_event(actor="vdt_gate", action="vdt_blocked_by_gate",
                   profile=profile, source=source,
                   reason="joint_lp.use_vdt:false")
    except Exception:
        pass


def validate_paper_trade_row(row: dict) -> Optional[VDTTrade]:
    """Validuje dict pred zápisom. None ak invalid (gracefully — žiadny crash).

    Použitie z writer kódu:
        from core.schemas.vdt import validate_paper_trade_row, should_log_vdt_for_profile
        if not should_log_vdt_for_profile(profile):
            return
        validated = validate_paper_trade_row(row_dict)
        if validated is None:
            return   # invalid — preskoč zápis
        write_csv_row(validated.model_dump())
    """
    try:
        return VDTTrade.model_validate(row)
    except Exception as e:
        print(f"[vdt.validate_paper_trade_row] invalid row, skipping: {e}")
        return None


__all__ = [
    "VDTTrade",
    "ALLOWED_ACTIONS",
    "should_log_vdt_for_profile",
    "validate_paper_trade_row",
]
