# -*- coding: utf-8 -*-
"""core.schemas — Pydantic models pre kritické dátové štruktúry.

Fáza A architektúry (refactor-v2):
    Validácia vstupu/výstupu cez Pydantic — žiadny modul nemôže urobiť
    nesprávnu interpretáciu dát. Schemy zachytia type errors immediately,
    sú backward-compat (extra fields allowed, defaults na chýbajúce).

Moduly:
    profile.py  — ProfileConfig + sub-models (PlanConfig, JointLPConfig,
                  DistributionConfig, RTConfig, DentrhConfig)
    plan.py     — StoredPlan + PlanSlot (TODO Fáza A.2)
    vdt.py      — VDTTrade + gate validator (TODO Fáza A.3)
"""
from .profile import (
    ProfileConfig,
    PlanConfig,
    JointLPConfig,
    DistributionConfig,
    RTConfig,
    DentrhConfig,
)

__all__ = [
    "ProfileConfig",
    "PlanConfig",
    "JointLPConfig",
    "DistributionConfig",
    "RTConfig",
    "DentrhConfig",
]
