# -*- coding: utf-8 -*-
"""control/executor.py — vykonávač setpointu batérie (pluggable: sim / real).

Executor je abstrakcia VYKONANIA: dostane setpoint (kW, +vybíja/−nabíja) a aplikuje
ho — v simulácii cez fyzikálny model, v realite cez Realio→Bender. Control loop
(control/loop.py) volá len `apply_setpoint` / `read_soc`, nevie ako sa to deje.

  • SimExecutor = fyzikálny model z parametrov batérie (DummyExecutor model).
  • RealExecutor = realio per-battery wiring (ĎALŠÍ integračný krok — zatiaľ raise).

Konvencia: setpoint_kw + = vybíja, − = nabíja.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Dict


class Executor(ABC):
    """Vykonávač setpointu pre jednu batériu."""

    @abstractmethod
    def apply_setpoint(self, battery_id: int, setpoint_kw: float, dt_h: float = 1.0 / 60.0) -> Dict:
        """Aplikuj setpoint na `dt_h` hodín. Vráti {'soc_pct', 'applied_kw'} (reálne
        vykonané po clipe/limitoch). +vybíja / −nabíja."""

    @abstractmethod
    def read_soc(self, battery_id: int) -> float:
        """Aktuálny SOC [%]."""


class DummyExecutor(Executor):
    """In-memory SOC model pre testy/dry-run. Žiadne I/O. Integruje SOC z setpointu
    s účinnosťou a clipom na [soc_min, soc_max] a ±batt_kw."""

    def __init__(self, batt_kw: float = 1000.0, batt_kwh: float = 2000.0,
                 soc_pct: float = 50.0, eff: float = 0.95,
                 soc_min: float = 5.0, soc_max: float = 95.0):
        self.batt_kw = float(batt_kw)
        self.batt_kwh = float(batt_kwh)
        self.eff = float(eff)
        self.soc_min = float(soc_min)
        self.soc_max = float(soc_max)
        self._soc = float(soc_pct)

    def apply_setpoint(self, battery_id: int, setpoint_kw: float, dt_h: float = 1.0 / 60.0) -> Dict:
        # clip na fyzický výkon
        kw = max(-self.batt_kw, min(self.batt_kw, float(setpoint_kw)))
        soc_kwh = self._soc / 100.0 * self.batt_kwh
        if kw >= 0:                                   # vybíja
            energy = kw * dt_h / max(self.eff, 1e-6)  # z batérie odíde viac (strata)
            soc_kwh = max(self.batt_kwh * self.soc_min / 100.0, soc_kwh - energy)
        else:                                          # nabíja
            energy = (-kw) * dt_h * self.eff           # do batérie príde menej (strata)
            soc_kwh = min(self.batt_kwh * self.soc_max / 100.0, soc_kwh + energy)
        self._soc = soc_kwh / self.batt_kwh * 100.0 if self.batt_kwh > 0 else 0.0
        return {"soc_pct": round(self._soc, 3), "applied_kw": round(kw, 3)}

    def read_soc(self, battery_id: int) -> float:
        return round(self._soc, 3)


class SimExecutor(DummyExecutor):
    """Simulačný executor pre VPP control loop — fyzikálny model batérie (SOC
    integrácia z DB parametrov). Pre real-time tick aplikáciu setpointu (NIE
    full-day livesim plánovanie, to je iná vrstva). Dnes = DummyExecutor model
    parametrizovaný z DB batérie; neskôr možno napojiť na livesim fyziku 1:1."""
    pass


class RealExecutor(Executor):
    """Reálny executor — setpoint cez Realio→Bender, SOC z reálneho merania, PER
    BATÉRIA (host/creds/tagy z DB battery). NAPOJENIE NA realio JE ĎALŠÍ INTEGRAČNÝ
    KROK (vyžaduje realio per-battery refactor — dnes je realio single-config).
    Zatiaľ vyhadzuje, aby sa real mód omylom nespustil bez wiringu."""

    def __init__(self, battery: dict):
        self.battery = battery

    def apply_setpoint(self, battery_id: int, setpoint_kw: float, dt_h: float = 1.0 / 60.0) -> Dict:
        raise NotImplementedError(
            "RealExecutor: realio per-battery wiring ešte nie je hotový "
            "(integračný krok). Batéria nesmie ísť do real módu bez neho.")

    def read_soc(self, battery_id: int) -> float:
        raise NotImplementedError("RealExecutor: realio per-battery wiring chýba.")


def build_executor(battery: dict, soc_pct: float = 50.0) -> Executor:
    """Postaví executor pre batériu podľa jej módu (z DB battery dict).
      • mode='simulation' → SimExecutor (fyzikálny model z parametrov batérie)
      • mode='real'       → RealExecutor (realio wiring = ďalší krok)
    """
    mode = str(battery.get("mode", "simulation"))
    if mode == "real":
        return RealExecutor(battery)
    return SimExecutor(
        batt_kw=float(battery.get("batt_kw", 1000.0) or 1000.0),
        batt_kwh=float(battery.get("batt_kwh", 2000.0) or 2000.0),
        soc_pct=float(soc_pct),
        eff=float(battery.get("eff", 0.95) or 0.95),
    )
