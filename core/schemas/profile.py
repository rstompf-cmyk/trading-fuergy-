# -*- coding: utf-8 -*-
"""core/schemas/profile.py — Pydantic ProfileConfig.

Fáza A.1 architektúry — single source of truth pre profil.

Princípy:
  • Validate at boundary — pri každom čítaní/zápise (load_profile, save_profile)
  • Backward-compat — všetky polia voliteľné s defaults; chýbajúce sa doplnia
  • Forward-compat — `extra="allow"` aby nové fields nezlomili load
  • Toggle gates vstavané — napr. add_vdt_trade vyžaduje use_vdt:true

Použitie:
    from core.schemas import ProfileConfig
    cfg = ProfileConfig.model_validate(json_dict)   # validate + fill defaults
    cfg.plan.joint_lp.use_vdt    # type-safe access
    cfg.model_dump()             # späť do dict pre JSON serializáciu
"""
from __future__ import annotations
from typing import Optional, List, Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator


# ────────────────────────── Sub-models ──────────────────────────

class JointLPConfig(BaseModel):
    """Joint LP optimalizácia — toggle flags pre stratégiu obchodu."""
    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    trade_batt: bool = True                  # batt arbitráž cez DAM
    trade_ftv: bool = True                   # FTV export do 'obchod' aggregate
    trade_load: bool = True                  # load cover cez DAM
    use_vdt: bool = True                     # VDT intraday trades povolené
    optimize_distribution: bool = False      # TOU distribučná optimalizácia


class PlanConfig(BaseModel):
    """Plánovacie parametre (batt, FTV, load, ceny, RT). Hlavná sekcia profilu."""
    model_config = ConfigDict(extra="allow")

    # Lokácia + FTV
    lat: float = 48.5
    lon: float = 18.5
    kwp: float = Field(default=0.0, ge=0.0)
    tilt: float = Field(default=30.0, ge=0.0, le=90.0)
    azimuth: float = Field(default=0.0, ge=-180.0, le=180.0)
    eff: float = Field(default=0.95, ge=0.5, le=1.0)

    # Batéria
    batt_kw: float = Field(default=0.0, ge=0.0)
    batt_kwh: float = Field(default=0.0, ge=0.0)
    eff_c: float = Field(default=0.95, ge=0.5, le=1.0)
    eff_d: float = Field(default=0.95, ge=0.5, le=1.0)
    soc_min: float = Field(default=5.0, ge=0.0, le=100.0)
    soc_max: float = Field(default=100.0, ge=0.0, le=100.0)
    soc_init: float = Field(default=50.0, ge=0.0, le=100.0)
    terminal_soc: float = Field(default=50.0, ge=0.0, le=100.0)

    # Sieť
    grid_kw: float = Field(default=200.0, ge=0.0)
    grid_kw_import: Optional[float] = None
    grid_kw_export: Optional[float] = None
    grid_fee: float = Field(default=22.0, ge=0.0)
    cycle_cost: float = Field(default=2.0, ge=0.0)

    # Ekonomika plánu
    min_spread: float = Field(default=10.0, ge=0.0)
    min_trade: float = Field(default=0.01, ge=0.0)
    price_scale: float = Field(default=1.0, ge=0.0)
    pv_scale: float = Field(default=1.0, ge=0.0)

    # Flags
    allow_curtail: bool = True
    allow_grid_charge: bool = True
    block_neg_import: bool = True
    no_planned_discharge: bool = False
    zco_bias_w: float = Field(default=0.3, ge=0.0, le=1.0)

    # RT/FTV-balance
    rt_freedom: bool = True
    aggressive_rt: bool = False
    ftv_balance: bool = True
    ftv_lookahead_h: float = Field(default=4.0, ge=0.0)
    ftv_persistence_throttle: bool = True
    rt_no_worsen_dev: bool = True
    ftv_strict_plan: bool = False
    ftv_strict_deadband_kw: float = Field(default=5.0, ge=0.0)

    # Day caps (0 = bez stropu)
    max_export_kwh_day: float = Field(default=0.0, ge=0.0)
    max_import_kwh_day: float = Field(default=0.0, ge=0.0)
    max_cycles: float = Field(default=3.0, ge=0.0)

    # Baseline porovnanie
    baseline_im_mode: Literal["dt_x", "fix"] = "dt_x"
    baseline_im_value: float = Field(default=1.0, ge=0.0)
    baseline_ex_mode: Literal["dt_x", "fix"] = "dt_x"
    baseline_ex_value: float = Field(default=1.0, ge=0.0)

    # VDT advisor (Bug P)
    grid_fee_vdt: float = Field(default=22.0, ge=0.0)
    cycle_cost_vdt: float = Field(default=2.0, ge=0.0)
    min_spread_eur: float = Field(default=5.0, ge=0.0)
    soc_end_min_pct: float = Field(default=20.0, ge=0.0, le=100.0)
    max_cycles_per_day: float = Field(default=3.0, ge=0.0)
    soc_max_pct_operational: float = Field(default=95.0, ge=0.0, le=100.0)
    fallback_soc_pct: float = Field(default=50.0, ge=0.0, le=100.0)
    vdt_eff_c: Optional[float] = None        # None = inherit plan.eff_c
    vdt_eff_d: Optional[float] = None

    # Joint MPC (Bug CC5)
    joint_mpc_enabled: bool = False
    trade_batt: bool = True                  # MPC toggle — duplikát joint_lp.trade_batt
    trade_ftv: bool = True
    trade_load: bool = True
    use_vdt: bool = True
    allow_rt_correction: bool = True

    # Joint LP nested
    joint_lp: JointLPConfig = Field(default_factory=JointLPConfig)

    @field_validator("soc_max")
    @classmethod
    def _check_soc_range(cls, v, info):
        # Pydantic v2 — info.data má už validované polia
        sm = info.data.get("soc_min")
        if sm is not None and v < sm:
            raise ValueError(f"soc_max ({v}) musí byť ≥ soc_min ({sm})")
        return v


class DentrhConfig(BaseModel):
    """15-min /dentrh plán — zdiela polia s PlanConfig ale s vlastnou inštanciou."""
    model_config = ConfigDict(extra="allow")
    lat: float = 48.5
    lon: float = 18.5
    kwp: float = Field(default=0.0, ge=0.0)
    tilt: float = Field(default=30.0, ge=0.0, le=90.0)
    azimuth: float = Field(default=0.0, ge=-180.0, le=180.0)
    eff: float = Field(default=0.95, ge=0.5, le=1.0)
    batt_kw: float = Field(default=0.0, ge=0.0)
    batt_kwh: float = Field(default=0.0, ge=0.0)
    eff_c: float = Field(default=0.95, ge=0.5, le=1.0)
    eff_d: float = Field(default=0.95, ge=0.5, le=1.0)
    soc_min: float = Field(default=5.0, ge=0.0, le=100.0)
    soc_max: float = Field(default=100.0, ge=0.0, le=100.0)
    soc_init: float = Field(default=50.0, ge=0.0, le=100.0)
    terminal_soc: float = Field(default=50.0, ge=0.0, le=100.0)
    grid_kw: float = Field(default=200.0, ge=0.0)
    grid_kw_import: Optional[float] = None
    grid_kw_export: Optional[float] = None
    grid_fee: float = Field(default=22.0, ge=0.0)
    cycle_cost: float = Field(default=2.0, ge=0.0)
    min_spread: float = Field(default=10.0, ge=0.0)
    allow_grid_charge: bool = True
    allow_curtail: bool = True
    block_neg_import: bool = True
    no_planned_discharge: bool = False
    max_export_kwh_day: float = Field(default=0.0, ge=0.0)
    max_import_kwh_day: float = Field(default=0.0, ge=0.0)
    zco_bias_w: float = Field(default=0.3, ge=0.0, le=1.0)


class RTConfig(BaseModel):
    """RT poradca defaults (per-profile)."""
    model_config = ConfigDict(extra="allow")
    soc: float = Field(default=100.0, ge=0.0, le=100.0)
    margin: float = Field(default=20.0, ge=0.0)
    budget: float = Field(default=3.0, ge=0.0)
    mode: Literal["auto", "manual", "off"] = "auto"
    bchg: float = Field(default=10.0, ge=0.0)
    kdis: float = Field(default=0.3, ge=0.0)
    kchg: float = Field(default=0.3, ge=0.0)
    dtk: float = Field(default=1.5, ge=0.0)
    react: float = Field(default=3.0, ge=0.0)
    rboost: float = Field(default=0.7, ge=0.0)
    sock: float = Field(default=0.5, ge=0.0)
    case: str = "default"


class DistributionConfig(BaseModel):
    """Distribučný TOU + uniform poplatky config."""
    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    distribution_company: str = ""
    tariff_group: str = ""
    voltage_level: Literal["VN", "NN", ""] = ""
    tou_mode: Literal["flat", "tou"] = "tou"
    tou_high_eur_per_mwh: float = Field(default=25.0, ge=0.0)
    tou_low_eur_per_mwh: float = Field(default=12.0, ge=0.0)
    tou_high_hours: List[int] = Field(default_factory=list)
    tou_weekend_low_only: bool = True
    hourly_custom_eur_per_mwh: Optional[List[float]] = None
    tps_eur_per_mwh: float = Field(default=0.0, ge=0.0)
    ss_eur_per_mwh: float = Field(default=0.0, ge=0.0)
    oze_eur_per_mwh: float = Field(default=0.0, ge=0.0)
    peak_charge_eur_per_kw_month: float = Field(default=0.0, ge=0.0)
    monthly_fix_eur: float = Field(default=0.0, ge=0.0)

    @field_validator("tou_high_hours")
    @classmethod
    def _check_hours_range(cls, v):
        for h in v:
            if not (0 <= h <= 23):
                raise ValueError(f"tou_high_hours obsahuje neplatnú hodinu {h}")
        return v

    @field_validator("hourly_custom_eur_per_mwh")
    @classmethod
    def _check_hourly_24(cls, v):
        if v is not None and len(v) != 24:
            raise ValueError(f"hourly_custom_eur_per_mwh musí mať 24 hodnôt (má {len(v)})")
        return v


# ────────────────────────── Top-level ProfileConfig ──────────────────────────

class ProfileConfig(BaseModel):
    """Top-level profil. Single source of truth pre konfiguráciu klienta."""
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    name: str = Field(min_length=1, max_length=64)
    mode: Literal["simulation", "real"] = "simulation"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    note: str = ""

    plan: PlanConfig = Field(default_factory=PlanConfig)
    dentrh: DentrhConfig = Field(default_factory=DentrhConfig)
    rt: RTConfig = Field(default_factory=RTConfig)
    distribution: DistributionConfig = Field(default_factory=DistributionConfig)

    # Šablóna pre profil (96 slotov × 1 hodnota each)
    # Backward-compat: staré profily majú [None, None, ...] — None sa preloží
    # na default (mult96 → 1.0 = bez zmeny, rt_on96 → 1.0 = RT povolené).
    mult96: List[Optional[float]] = Field(default_factory=list)
    rt_on96: List[Optional[float]] = Field(default_factory=list)

    @field_validator("mult96", "rt_on96")
    @classmethod
    def _check_96_or_empty(cls, v):
        if v and len(v) != 96:
            raise ValueError(f"mult96/rt_on96 musí mať 96 hodnôt alebo byť prázdne (má {len(v)})")
        return v

    @field_validator("name")
    @classmethod
    def _check_name_chars(cls, v):
        import re
        if not re.match(r"^[A-Za-z0-9_\-]+$", v):
            raise ValueError(f"name '{v}' obsahuje neplatné znaky (povolené: A-Za-z0-9_-)")
        return v

    # ──────────────── High-level convenience methods ────────────────

    def uses_vdt(self) -> bool:
        """True ak profil má use_vdt:true (gate pre VDT trades / paper logger)."""
        return bool(self.plan.joint_lp.use_vdt and self.plan.joint_lp.enabled) \
               or bool(self.plan.use_vdt and self.plan.joint_mpc_enabled)

    def uses_tou(self) -> bool:
        """True ak profil má optimize_distribution:true + distribution.enabled."""
        return bool(self.plan.joint_lp.optimize_distribution
                    and self.distribution.enabled)

    def is_real(self) -> bool:
        return self.mode == "real"
