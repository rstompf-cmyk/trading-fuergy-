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
    """Reálny executor — PER BATÉRIA Bender (Realio) cez DB realio_* polia.

    Číta SOC + dual-write setpoint cez realio NÍZKOÚROVŇOVÉ funkcie
    (_fetch_latest_via_bender / _send_tag_writes) s per-batéria `cfg` postaveným z
    DB riadku batérie — REUSE, BEZ dotyku globálneho realio configu či prod
    write_setpoint(). Rieši single-config blocker (každá batéria vlastný host/creds/tagy).

    BEZPEČNOSŤ — dvojitá poistka:
      1. build_executor stavia RealExecutor LEN pre mode=='real',
      2. WRITE na HW iba ak env FLEET_REAL_WRITE=1; inak DRY-RUN (zostaví write +
         zaloguje, ale NEpošle). Default = dry-run → real batéria sa nedá omylom
         ovládať pred commissioningom.
    read_soc je read-only (bezpečné). Bez dosiahnuteľného Bendera → raise →
    control loop fail-safe degraded (NEzhodí flotilu).

    Konvencia setpointu: + vybíja / − nabíja (kW). Zápis = kW × 1000 → W do
    REG_Regulator_Param3 + REG_Regulator_Manual_Plan=enable (dual-write, 1 minúta).
    POZOR: ZNAMIENKO W voči Benderu treba OVERIŤ pri commissioningu reálnej batérie
    (ak Bender používa opačné, doplniť per-batéria sign flag).
    POZNÁMKA (multi-battery): realio._get_session cachuje 1 session per host globálne
    — pre veľa batérií na RÔZNYCH hostoch to re-loginuje pri prepnutí (korektné, ale
    pomalé); pri 20-30 doplniť per-host session pool v realio (samostatný krok)."""

    def __init__(self, battery: dict):
        self.battery = battery
        self.cfg = self._build_cfg(battery)

    @staticmethod
    def _build_cfg(battery: dict) -> dict:
        """Per-batéria realio cfg = realio defaulty + override z DB (host/creds/tagy)."""
        import realio
        cfg = realio._fresh_default()
        host = (battery.get("realio_host") or "").strip()
        cfg["host"] = host.rstrip("/") if host else ""
        if battery.get("realio_username"):
            cfg["username"] = battery["realio_username"]
        if battery.get("realio_password"):
            cfg["password"] = battery["realio_password"]
        if battery.get("realio_tags_read"):
            cfg["tags_read"] = {**cfg["tags_read"], **battery["realio_tags_read"]}
        if battery.get("realio_tags_write"):
            cfg["tags_write"] = {**cfg["tags_write"], **battery["realio_tags_write"]}
        cfg["enabled"] = True
        cfg["control_enabled"] = True
        return cfg

    @staticmethod
    def _real_write_enabled() -> bool:
        import os
        return os.environ.get("FLEET_REAL_WRITE", "0") == "1"

    def _build_setpoint_writes(self, setpoint_kw: float) -> list:
        """Dual-write list (mode enable + setpoint W), rovnaký minútový timestamp."""
        import realio
        tw = self.cfg.get("tags_write") or {}
        sp_tag = tw.get("batt_setpoint_kw")
        if not sp_tag:
            raise RuntimeError("batt_setpoint_kw tag nie je nakonfigurovaný (realio_tags_write)")
        ts = realio._minute_aligned_ms()
        writes = []
        mode_tag = tw.get("batt_control_mode")
        if mode_tag:
            writes.append({"tag": mode_tag,
                           "value": float(self.cfg.get("control_mode_enable_value", 2)),
                           "time": ts})
        writes.append({"tag": sp_tag, "value": float(setpoint_kw) * 1000.0, "time": ts})  # kW→W
        return writes

    def apply_setpoint(self, battery_id: int, setpoint_kw: float, dt_h: float = 1.0 / 60.0) -> Dict:
        import realio
        if not self.cfg.get("host"):
            raise RuntimeError("realio_host nie je nastavený pre batériu (real mód)")
        writes = self._build_setpoint_writes(setpoint_kw)
        if self._real_write_enabled():
            res = realio._send_tag_writes(self.cfg, writes)
            if not res.get("ok"):
                raise RuntimeError(f"Bender write zlyhal: {res.get('msg')}")
        else:
            print(f"[RealExecutor bat {battery_id}] DRY-RUN setpoint {float(setpoint_kw):+.1f} kW "
                  f"(FLEET_REAL_WRITE!=1 → nezapísané) tags={[w['tag'] for w in writes]}", flush=True)
        soc = self.read_soc(battery_id)
        return {"soc_pct": soc, "applied_kw": float(setpoint_kw)}

    def read_soc(self, battery_id: int) -> float:
        import realio
        if not self.cfg.get("host"):
            raise RuntimeError("realio_host nie je nastavený pre batériu (real mód)")
        tag = (self.cfg.get("tags_read") or {}).get("batt_soc_pct")
        if not tag:
            raise RuntimeError("batt_soc_pct tag nie je nakonfigurovaný (realio_tags_read)")
        raw = realio._fetch_latest_via_bender(self.cfg, [tag])
        v = raw.get(tag)
        if v is None:
            raise RuntimeError("SOC nečitateľný z Bendera")
        scale = float((self.cfg.get("scale_read") or {}).get("batt_soc_pct", 1.0))
        return float(v) * scale


class CdcExecutor(Executor):
    """Reálny executor — PER BATÉRIA cez centrálny CDC server (modul `cdc.py`).

    Na rozdiel od RealExecutor (Trakany Bender, per-batéria host/creds) ide CDC cez
    JEDEN systémový config na krajinu (`out/<market>/cdc_system.json` — host, auth,
    tag KORENE/suffixy, scale). Batéria sa odlišuje len PREFIXOM (`cdc_prefix`),
    reálny tag = prefix + suffix. Plány/RT ostávajú v Profile (ako dnes).

    BEZPEČNOSŤ — rovnaký princíp ako RealExecutor:
      1. build_executor stavia CdcExecutor len pre mode=='real' a backend=='cdc',
      2. zápis na HW iba ak env FLEET_REAL_WRITE=1 (gate v cdc.write_value); inak
         DRY-RUN (zostaví payload, zaloguje, NEpošle).
    read_soc je read-only. Bez dosiahnuteľného servera → raise → fail-safe degraded.

    Konvencia setpointu: + vybíja / − nabíja (kW). Zápis ide do write tagu
    `cons_plan_kw` (`_U_REG_ConsumptionPlan_Manual_1h`), kW × scale_write → W.
    POZOR: ZNAMIENKO voči CDC serveru OVERIŤ pri commissioningu (ak opačné, doplniť
    per-batéria sign flag, prípadne scale_write záporné)."""

    def __init__(self, battery: dict):
        self.battery = battery
        self.prefix = (battery.get("cdc_prefix") or "").strip()
        self.country = (battery.get("country") or "").strip() or None
        self.cfg = self._build_cfg(battery)

    @staticmethod
    def _build_cfg(battery: dict) -> dict:
        """Systémový CDC config pre krajinu batérie; enabled/control_enabled
        vynútené True (finálnou poistkou zápisu ostáva env FLEET_REAL_WRITE)."""
        import cdc
        cfg = cdc.load_system_config((battery.get("country") or "").strip() or None)
        cfg["enabled"] = True
        cfg["control_enabled"] = True
        return cfg

    def apply_setpoint(self, battery_id: int, setpoint_kw: float, dt_h: float = 1.0 / 60.0) -> Dict:
        import cdc
        if not self.prefix:
            raise RuntimeError("cdc_prefix nie je nastavený pre batériu (CDC real mód)")
        res = cdc.write_value(self.prefix, "cons_plan_kw", float(setpoint_kw),
                              cfg=self.cfg, source="fleet_control")
        if not res.get("ok"):
            raise RuntimeError(f"CDC write zlyhal: {res.get('error')}")
        if res.get("dry_run"):
            print(f"[CdcExecutor {self.prefix}] DRY-RUN setpoint {float(setpoint_kw):+.1f} kW "
                  f"(FLEET_REAL_WRITE!=1 → nezapísané) tag={res.get('tag')}", flush=True)
        soc = self.read_soc(battery_id)
        return {"soc_pct": soc, "applied_kw": float(setpoint_kw)}

    def read_soc(self, battery_id: int) -> float:
        import cdc
        if not self.prefix:
            raise RuntimeError("cdc_prefix nie je nastavený pre batériu (CDC real mód)")
        data = cdc.fetch_latest(self.prefix, cfg=self.cfg) or {}
        soc = data.get("batt_soc_pct")
        if soc is None:
            soc = data.get("batt_soc_pct_15m")
        if soc is None:
            raise RuntimeError(f"SOC nečitateľný z CDC (prefix {self.prefix})")
        return float(soc)


def build_executor(battery: dict, soc_pct: float = 50.0) -> Executor:
    """Postaví executor pre batériu podľa jej módu + backendu (z DB battery dict).
      • mode='simulation'              → SimExecutor (fyzikálny model)
      • mode='real', backend='cdc'     → CdcExecutor (centrálny CDC server, prefix)
      • mode='real', backend=ostatné   → RealExecutor (Trakany Bender per-batéria)
    """
    mode = str(battery.get("mode", "simulation"))
    if mode == "real":
        backend = str(battery.get("backend") or "realio").strip().lower()
        if backend == "cdc":
            return CdcExecutor(battery)
        return RealExecutor(battery)
    return SimExecutor(
        batt_kw=float(battery.get("batt_kw", 1000.0) or 1000.0),
        batt_kwh=float(battery.get("batt_kwh", 2000.0) or 2000.0),
        soc_pct=float(soc_pct),
        eff=float(battery.get("eff", 0.95) or 0.95),
    )
