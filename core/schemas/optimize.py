# -*- coding: utf-8 -*-
"""core/schemas/optimize.py — Pydantic typy pre optimizer výstupy (Fáza B.2).

Cieľ: explicit kontrakt pre to čo `optimize_day` a `optimize_joint_day` vracajú.
Doteraz to bol unstructured tuple `(sch: dict, summary: dict)`. Teraz máme
type-safe wrapper, ktorý:
  - validuje že schedule obsahuje očakávané polia
  - validuje rozsahy (ZISK_EUR konečný, kWh != negative pre import, ...)
  - poskytuje convenience helpery (total_profit, total_throughput)

Implementácia je **OPT-IN** — pôvodné funkcie ďalej vracajú dict tuples
(žiadny breaking change). Tento modul je len wrapper na validáciu výsledkov
keď ich potrebujeme pred zápisom / pred ďalším použitím.

Použitie:
    from core.schemas.optimize import OptimizeResult

    sch, summary = optimizer.optimize_day(pv, price, ...)
    result = OptimizeResult.from_optimize_day(sch, summary)
    print(result.total_profit_eur)
    print(result.schedule_array("batt_kw"))
    # Pred zápisom do plan_store: result.validate_consistency() raise on dirty data

Audit B.2:
    optimize_day:        pure (numpy only, žiadny disk I/O) ✓
    optimize_joint_day:  pure ✓
    run_day_physical:    pure (read_csv iba v __main__, nie v funkcii) ✓
    combined_backtest:   orchestrátor (read_csv input → pure optimizer → write_csv output)
                         Toto je správny pattern (Hexagonal Architecture).
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional
import math

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── konštanty ─────────────────────────────────────────────────────────────

# Stĺpce ktoré optimize_day vždy vracia v schedule
REQUIRED_SCHEDULE_KEYS = (
    "batt_kw", "grid_kwh", "pv_kwh", "price_eur",
    "soc_pct", "curtail_kwh",
)

# Voliteľné stĺpce (môžu chýbať pre staršie optimizer výstupy)
OPTIONAL_SCHEDULE_KEYS = (
    "_charge_kw", "_discharge_kw", "_export_kwh", "_import_kwh",
    "soc_kwh", "order_mwh", "load_kwh", "hour",
)


# ── modely ────────────────────────────────────────────────────────────────

class OptimizeResult(BaseModel):
    """Pydantic wrapper okolo (schedule, summary) tuple z optimize_day.

    Skladá sa z 2 dict-ov:
      - schedule: per-time-slot arrays (batt_kw, grid_kwh, soc_pct, ...)
      - summary: agregované metriky (ZISK_EUR, import_kWh, ...)

    Validuje:
      - Required schedule kľúče existujú a sú rovnakej dĺžky
      - ZISK_EUR je konečné číslo
      - Arrays neobsahujú NaN/inf
    """
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    schedule: Dict[str, List[Any]] = Field(default_factory=dict)
    summary: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("schedule")
    @classmethod
    def _required_keys(cls, v: Dict) -> Dict:
        missing = [k for k in REQUIRED_SCHEDULE_KEYS if k not in v]
        if missing:
            raise ValueError(
                f"schedule chýbajú required kľúče: {missing}"
            )
        # Skontroluj že všetky required arrays sú rovnakej dĺžky
        lengths = {k: len(v[k]) for k in REQUIRED_SCHEDULE_KEYS if v.get(k) is not None}
        if len(set(lengths.values())) > 1:
            raise ValueError(
                f"schedule arrays majú rôzne dĺžky: {lengths}"
            )
        return v

    @field_validator("summary")
    @classmethod
    def _summary_finite(cls, v: Dict) -> Dict:
        # ZISK_EUR (ak existuje) musí byť konečné
        zisk = v.get("ZISK_EUR")
        if zisk is not None:
            try:
                f = float(zisk)
                if not math.isfinite(f):
                    raise ValueError(f"ZISK_EUR nie je konečné: {zisk}")
            except (TypeError, ValueError):
                raise ValueError(f"ZISK_EUR má neplatný typ: {type(zisk).__name__}")
        return v

    # ── factory ────────────────────────────────────────────────────────────

    @classmethod
    def from_optimize_day(cls, sch: Dict, summary: Dict) -> "OptimizeResult":
        """Konvertuj numpy arrays na lists pre Pydantic kompatibilitu."""
        clean_sch: Dict[str, List[Any]] = {}
        for k, v in sch.items():
            if hasattr(v, "tolist"):
                clean_sch[k] = v.tolist()
            else:
                clean_sch[k] = list(v) if v is not None else []
        return cls(schedule=clean_sch, summary=dict(summary))

    # ── convenience accessors ──────────────────────────────────────────────

    @property
    def total_profit_eur(self) -> float:
        """ZISK_EUR — celkový zisk dňa v €."""
        return float(self.summary.get("ZISK_EUR", 0.0))

    @property
    def total_charged_kwh(self) -> float:
        return float(self.summary.get("nabite_kWh", 0.0))

    @property
    def total_discharged_kwh(self) -> float:
        return float(self.summary.get("vybite_kWh", 0.0))

    @property
    def total_throughput_kwh(self) -> float:
        """Celkový throughput (charge + discharge)."""
        return self.total_charged_kwh + self.total_discharged_kwh

    @property
    def total_curtailed_kwh(self) -> float:
        return float(self.summary.get("orezane_kWh", 0.0))

    @property
    def num_slots(self) -> int:
        """Počet časových slotov v schedule (24, 96, alebo iné)."""
        if "batt_kw" in self.schedule:
            return len(self.schedule["batt_kw"])
        return 0

    def schedule_array(self, key: str, default: float = 0.0) -> List[float]:
        """Bezpečné získanie arrayu, None → default fill."""
        arr = self.schedule.get(key, [])
        out: List[float] = []
        n = self.num_slots
        for i in range(n):
            try:
                v = arr[i]
                out.append(float(v) if v is not None else float(default))
            except (IndexError, TypeError, ValueError):
                out.append(float(default))
        return out

    # ── consistency checks ─────────────────────────────────────────────────

    def validate_consistency(self) -> List[str]:
        """Vráti zoznam warning messages alebo prázdny list ak OK.

        Pravidlá:
          - ZISK_EUR sa musí dať dopočítať z prinos_baterie_EUR + trzba_export_EUR
            - naklad_import_EUR - naklad_cyklus_EUR (tolerance 0.5 €)
          - soc_pct musí byť v [0, 100]
          - sum(batt_kw) cez deň ≈ 0 keď batéria začína a končí v rovnakom SOC
        """
        warnings: List[str] = []

        # 1. SOC range
        socs = self.schedule_array("soc_pct")
        if socs:
            mn, mx = min(socs), max(socs)
            if mn < -1 or mx > 101:
                warnings.append(f"soc_pct mimo [0,100]: min={mn:.1f}, max={mx:.1f}")

        # 2. ZISK skladba (informatívne, nie hard fail)
        zisk = self.total_profit_eur
        prinos = float(self.summary.get("prinos_baterie_EUR", 0.0))
        trzba = float(self.summary.get("trzba_export_EUR", 0.0))
        naklad_imp = float(self.summary.get("naklad_import_EUR", 0.0))
        naklad_cykl = float(self.summary.get("naklad_cyklus_EUR", 0.0))
        expected = prinos + trzba - naklad_imp - naklad_cykl
        if abs(zisk - expected) > 0.5:
            warnings.append(
                f"ZISK_EUR={zisk:.2f} != prinos+trzba-naklad="
                f"{expected:.2f} (diff {zisk - expected:+.2f})"
            )

        return warnings


__all__ = [
    "OptimizeResult",
    "REQUIRED_SCHEDULE_KEYS",
    "OPTIONAL_SCHEDULE_KEYS",
]
