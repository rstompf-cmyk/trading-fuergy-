# -*- coding: utf-8 -*-
"""db.models — SQLAlchemy ORM modely pre celú aplikáciu.

Štruktúra:
    AUTH (Fáza 2): User, UserProfileAccess, AuthSession, AuditLog
    PROFILE: Profile (s embedded JSON pre plan/dentrh/rt/distribution/mult96/rt_on96)
    PLAN: Plan, PlanSlot, PlanOverride
    DATA: LoadProfile, FtvScenario, VdtPaperTrade, AutoControlEvent
    SYSTEM: UiSettings, Case, RealioConfig

Konvencie:
    • Markety oddelené stĺpcom `market` ('cz'|'sk') — žiadne separate tabuľky
    • Per-profile dáta majú FK na Profile.id (ON DELETE CASCADE pre lokálne dáta,
      RESTRICT pre globálne ako Plan)
    • JSON stĺpce (sqlalchemy.JSON) pre rýchle, nepravidelné dáta (mult96, rt_on96,
      summary, params); štruktúrované polia majú vlastné stĺpce
    • Timestamp stĺpce: ISO string (TEXT) v lokálnej tz (kvôli SQLite kompatibilite);
      pre time-series s vysokou frekvenciou (livesim, realio) sa použije TEXT time_iso
      + epoch ms time_ms pre fast range query

POZN. RealioMeasurement zostáva v existujúcom realio_db.py SQLite súbore — neintegruje
sa do hlavnej DB (objem 1-min time-series, vlastný backup režim).
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    Integer, String, Float, Boolean, Text, JSON, DateTime, ForeignKey,
    UniqueConstraint, Index, CheckConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .session import Base


# ════════════════════════════════════════════════════════════════════════════
# AUTH (Fáza 2 — schémy treba teraz pre migration baseline)
# ════════════════════════════════════════════════════════════════════════════

class User(Base):
    """Užívateľ s rolou. 4 role: admin / obchodnik / trading / zakaznik.

    Mode-gated permissions a per-profile access cez `UserProfileAccess`.
    """
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)   # bcrypt
    role: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)        # ISO local
    last_login: Mapped[Optional[str]] = mapped_column(String(32))
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("user.id"))

    __table_args__ = (
        CheckConstraint("role IN ('admin','obchodnik','trading','zakaznik')",
                          name="ck_user_role"),
    )

    # vzťahy
    profile_accesses: Mapped[List["UserProfileAccess"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
        foreign_keys="UserProfileAccess.user_id"
    )
    sessions: Mapped[List["AuthSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserProfileAccess(Base):
    """Mapovanie užívateľ ↔ profile + flagi (read/write/HW write).

    Admin nepotrebuje záznam (auto-full access). Obchodník nepotrebuje (vidí
    všetky profily read-only). Trading + Zákazník musia mať explicitné záznamy.
    """
    __tablename__ = "user_profile_access"

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"),
                                          primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profile.id", ondelete="CASCADE"),
                                              primary_key=True)
    can_read: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_write: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_write_hw: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    granted_at: Mapped[str] = mapped_column(String(32), nullable=False)
    granted_by: Mapped[Optional[int]] = mapped_column(ForeignKey("user.id"))

    user: Mapped["User"] = relationship(back_populates="profile_accesses",
                                          foreign_keys=[user_id])
    profile: Mapped["Profile"] = relationship(back_populates="user_accesses")


class AuthSession(Base):
    """Session cookie tokeny. Expirujú po 7 dňoch (configurable)."""
    __tablename__ = "auth_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"),
                                          nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    last_seen: Mapped[Optional[str]] = mapped_column(String(32))
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))
    user_agent: Mapped[Optional[str]] = mapped_column(Text)

    user: Mapped["User"] = relationship(back_populates="sessions")


class AuditLog(Base):
    """Audit log pre všetky write akcie (profile_save, hw_write, login, role_change)."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("user.id"))
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource: Mapped[Optional[str]] = mapped_column(String(255))   # profile name, plan date
    details: Mapped[Optional[dict]] = mapped_column(JSON)            # delta JSON
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))


# ════════════════════════════════════════════════════════════════════════════
# PROFILE — centrum všetkého
# ════════════════════════════════════════════════════════════════════════════

class Profile(Base):
    """Konfigurácia zákazníka. Mode (sim/real) je FIXNÉ pri vzniku.

    Embedded JSON pre plan/dentrh/rt sekcie — sú heterogénne (50+ polí každá)
    a nehodia sa do plochých stĺpcov. Distribution je tiež JSON (nested
    štruktúra s TOU + uniform fees + presets).

    mult96 a rt_on96 sú JSON arrays dĺžky 96 (shápra pre 15-min sloty).
    """
    __tablename__ = "profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)         # 'simulation' | 'real'
    # PLAN-SOURCE-DB: 'predicted' (D-1 predikcia) | 'dentrh' (reálny denný trh 15-min).
    # FIXNÉ pri vzniku (ako mode). Bez tohto stĺpca load_profile spadol na 'predicted' pre
    # simuláciu → voľba „Reálny denný trh 15-min" sa po uložení stratila.
    plan_source: Mapped[str] = mapped_column(String(16), default="predicted",
                                              server_default="predicted", nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    # JSON sekcie (rovnaký formát ako pôvodný profile.json)
    plan: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    dentrh: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    rt: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    distribution: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # Šablóny pre 96 15-min slotov
    mult96: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    rt_on96: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    __table_args__ = (
        CheckConstraint("mode IN ('simulation','real')", name="ck_profile_mode"),
    )

    # vzťahy
    user_accesses: Mapped[List["UserProfileAccess"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    plans: Mapped[List["Plan"]] = relationship(back_populates="profile")
    plan_overrides: Mapped[List["PlanOverride"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    load_profiles: Mapped[List["LoadProfile"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    vdt_trades: Mapped[List["VdtPaperTrade"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class ActiveProfile(Base):
    """Aktívny profil per-port (a per-market).

    Replace pre `out/profiles/_active.json` + `_active_<PORT>.json` súbory.
    """
    __tablename__ = "active_profile"

    port: Mapped[str] = mapped_column(String(8), primary_key=True)         # '8000', '8001', ...
    market: Mapped[str] = mapped_column(String(4), primary_key=True)       # 'cz' | 'sk' | 'realio'
    profile_id: Mapped[Optional[int]] = mapped_column(ForeignKey("profile.id"))
    set_at: Mapped[str] = mapped_column(String(32), nullable=False)


# ════════════════════════════════════════════════════════════════════════════
# PLAN — D-1 60-min a Denný trh 15-min
# ════════════════════════════════════════════════════════════════════════════

class Plan(Base):
    """Jeden plán pre konkrétny deň. Schedule riadky v PlanSlot (FK relácia).

    UNIQUE(profile_id, market, date, kind, step_min) — žiadne duplikáty.
    """
    __tablename__ = "plan"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profile.id"), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(4), nullable=False, index=True)   # cz|sk
    date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)    # YYYY-MM-DD
    kind: Mapped[str] = mapped_column(String(16), nullable=False)                # 'plan'|'dentrh'
    step_min: Mapped[int] = mapped_column(Integer, nullable=False)               # 60|15
    generated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # Aplikované šablóny ako snapshot (pre reprodukciu)
    mults: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    rt_mask: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # Flagy z form (kvôli back-compat — duplikujú sa v params ale top-level pre query)
    block_planned_discharge: Mapped[bool] = mapped_column(Boolean, default=False)
    zco_bias_w: Mapped[float] = mapped_column(Float, default=0.0)
    rt_freedom: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("profile_id", "market", "date", "kind", "step_min",
                          name="uq_plan_per_day"),
        Index("idx_plan_lookup", "market", "profile_id", "date"),
    )

    profile: Mapped["Profile"] = relationship(back_populates="plans")
    slots: Mapped[List["PlanSlot"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan",
        order_by="PlanSlot.slot_idx"
    )


class PlanSlot(Base):
    """Jeden časový slot v pláne (hodinový alebo 15-min)."""
    __tablename__ = "plan_slot"

    plan_id: Mapped[int] = mapped_column(ForeignKey("plan.id", ondelete="CASCADE"),
                                          primary_key=True)
    slot_idx: Mapped[int] = mapped_column(Integer, primary_key=True)             # 0..23 alebo 0..95

    pv_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    load_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    price_eur: Mapped[float] = mapped_column(Float, default=0.0)
    batt_kw: Mapped[float] = mapped_column(Float, default=0.0)
    grid_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    order_mwh: Mapped[float] = mapped_column(Float, default=0.0)
    curtail_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    soc_pct: Mapped[float] = mapped_column(Float, default=0.0)
    soc_kwh: Mapped[float] = mapped_column(Float, default=0.0)

    # Internal optimizer výstupy (oddelený nabíja/vybíja)
    charge_kw: Mapped[float] = mapped_column(Float, default=0.0)
    discharge_kw: Mapped[float] = mapped_column(Float, default=0.0)
    export_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    import_kwh: Mapped[float] = mapped_column(Float, default=0.0)

    plan: Mapped["Plan"] = relationship(back_populates="slots")


class PlanOverride(Base):
    """Per-profile šablóna mult96 + rt_on96 (replace pre out/plan_overrides/<profile>/_template.json).

    Per-day override (legacy) môže byť sub-rad ak treba — zatial necháme len template.
    """
    __tablename__ = "plan_override"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profile.id"), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(4), nullable=False)
    date: Mapped[Optional[str]] = mapped_column(String(10))           # NULL = template; YYYY-MM-DD = per-day
    mult96: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    rt_on96: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        UniqueConstraint("profile_id", "market", "date", name="uq_plan_override"),
    )

    profile: Mapped["Profile"] = relationship(back_populates="plan_overrides")


# ════════════════════════════════════════════════════════════════════════════
# DATA — Load profile, FTV scenarios, VDT, Auto control
# ════════════════════════════════════════════════════════════════════════════

class LoadProfile(Base):
    """Priemerná spotreba zákazníka (weekday + weekend, 96 × 15-min)."""
    __tablename__ = "load_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profile.id"), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(4), nullable=False)

    weekday_kw: Mapped[list] = mapped_column(JSON, default=list, nullable=False)    # 96 hodnôt
    weekend_kw: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    imported_dates: Mapped[list] = mapped_column(JSON, default=list)
    unit: Mapped[str] = mapped_column(String(8), default="kW")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        UniqueConstraint("profile_id", "market", name="uq_load_profile"),
    )

    profile: Mapped["Profile"] = relationship(back_populates="load_profiles")


class FtvScenario(Base):
    """Ručný override hodinového FTV pre konkrétny deň (globálne per market, NIE per profile)."""
    __tablename__ = "ftv_scenario"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market: Mapped[str] = mapped_column(String(4), nullable=False)
    date: Mapped[str] = mapped_column(String(10), nullable=False)
    hourly_kw: Mapped[list] = mapped_column(JSON, nullable=False)        # 24 hodnôt
    smooth_sigma: Mapped[float] = mapped_column(Float, default=0.0)
    offset_h: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str] = mapped_column(Text, default="")
    saved_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        UniqueConstraint("market", "date", name="uq_ftv_scenario"),
    )


class VdtPaperTrade(Base):
    """Virtuálne intraday obchody (VDT extras). UNIQUE(profile,slot,action) — UPSERT logika.

    `slot` je čas slotu '12:15' string (rovnaký formát ako v paper trades CSV).
    """
    __tablename__ = "vdt_paper_trade"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profile.id", ondelete="CASCADE"),
                                              nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(4), nullable=False)
    date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    slot: Mapped[str] = mapped_column(String(5), nullable=False)            # 'HH:MM'
    action: Mapped[str] = mapped_column(String(16), nullable=False)         # 'charge'|'discharge'|'curtail_ftv'|'load_cover'
    kwh: Mapped[float] = mapped_column(Float, nullable=False)
    price_eur_mwh: Mapped[float] = mapped_column(Float, default=0.0)
    dam_clearing_eur_mwh: Mapped[Optional[float]] = mapped_column(Float)
    soc_before_pct: Mapped[Optional[float]] = mapped_column(Float)
    soc_after_pct: Mapped[Optional[float]] = mapped_column(Float)
    delta_profit_eur: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="paper")
    source: Mapped[str] = mapped_column(String(32), default="advisor")
    timestamp: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        UniqueConstraint("profile_id", "date", "slot", "action", name="uq_vdt_paper_trade"),
        Index("idx_vdt_lookup", "market", "profile_id", "date"),
    )

    profile: Mapped["Profile"] = relationship(back_populates="vdt_trades")


class AutoControlEvent(Base):
    """Log auto_control rozhodnutia (per 15-min cron tick)."""
    __tablename__ = "auto_control_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    profile_id: Mapped[Optional[int]] = mapped_column(ForeignKey("profile.id"), index=True)
    market: Mapped[str] = mapped_column(String(4), nullable=False)

    soc_pct: Mapped[Optional[float]] = mapped_column(Float)
    batt_kw_setpoint: Mapped[Optional[float]] = mapped_column(Float)
    mode: Mapped[str] = mapped_column(String(16), default="dry_run")    # dry_run|paper|real
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[Optional[str]] = mapped_column(Text)

    # Diagnostic flags
    margin_check: Mapped[Optional[bool]] = mapped_column(Boolean)
    soc_terminal_ok: Mapped[Optional[bool]] = mapped_column(Boolean)
    grid_capacity_ok: Mapped[Optional[bool]] = mapped_column(Boolean)
    plan_available: Mapped[Optional[bool]] = mapped_column(Boolean)
    setpoint_clipped: Mapped[Optional[bool]] = mapped_column(Boolean)

    grid_kw_min: Mapped[Optional[float]] = mapped_column(Float)
    grid_kw_max: Mapped[Optional[float]] = mapped_column(Float)
    price_eur_mwh: Mapped[Optional[float]] = mapped_column(Float)
    qty_kwh: Mapped[Optional[float]] = mapped_column(Float)
    notes: Mapped[Optional[str]] = mapped_column(Text)


class BattCapacityReservation(Base):
    """Bug #611: Capacity ledger — rezervácie kapacity batérie per 15-min slot.

    Drží poradie nasadenia D-1 → VDT → RT s explicitným audit trailom.
    Pre každý (profile, day, slot, source, direction) môže existovať najviac
    jeden záznam s aktívnou rezerváciou. Po VDT audite (#612) sa nové trades
    obmedzia na voľnú kapacitu (= batt_kw_max − Σ existujúce rezervácie).

    Vzťahy:
        - D-1 plán pri uložení do plan_store → reserve(source='d1', kw=plan_batt)
        - VDT order pred zápisom → audit_vdt_order → reserve(source='vdt', trade_id=X)
        - RT engine v livesim → available(slot, dir) → vrátiť voľnú kapacitu

    `slot_idx` 0..95 (15-min sloty dňa). `direction`: 'charge'|'discharge'.
    `kw` vždy POSITIVE — direction určuje znamienko v agregácii.
    """
    __tablename__ = "batt_capacity_reservation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profile.id", ondelete="CASCADE"),
                                              nullable=False, index=True)
    day: Mapped[str] = mapped_column(String(10), nullable=False, index=True)   # YYYY-MM-DD
    slot_idx: Mapped[int] = mapped_column(Integer, nullable=False)             # 0..95
    source: Mapped[str] = mapped_column(String(16), nullable=False)            # 'd1'|'vdt'|'rt'|'auto_control'
    direction: Mapped[str] = mapped_column(String(10), nullable=False)         # 'charge'|'discharge'
    kw: Mapped[float] = mapped_column(Float, nullable=False)                   # positive
    trade_id: Mapped[Optional[str]] = mapped_column(String(64))                # link na vdt_paper_trade pre VDT zdroj
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)        # ISO timestamp
    note: Mapped[Optional[str]] = mapped_column(Text)                          # audit poznámka (downscale, reject reason)

    __table_args__ = (
        UniqueConstraint("profile_id", "day", "slot_idx", "source", "direction", "trade_id",
                          name="uq_batt_reservation"),
        Index("idx_batt_lookup", "profile_id", "day", "slot_idx"),
        CheckConstraint("source IN ('d1','vdt','rt','auto_control')", name="ck_batt_source"),
        CheckConstraint("direction IN ('charge','discharge')", name="ck_batt_direction"),
        CheckConstraint("slot_idx >= 0 AND slot_idx <= 95", name="ck_batt_slot_idx"),
        CheckConstraint("kw >= 0", name="ck_batt_kw_positive"),
    )

    profile: Mapped["Profile"] = relationship()


class EffectMinute(Base):
    """Bug #650 / DB unify F1: per-minute € hodnoty livesim výpočtu.

    Jediný zdroj pravdy pre všetky agregácie efektu. UI (karty, chC graf,
    Excel, PDF) číta z tejto tabuľky (= effect_daily pre obdobie alebo
    effect_minute pre 15-min detail dňa). Žiadne paralelné výpočty
    v core/effect.py compute_effect_totals() — všetko cez SQL agregát.

    Atribučný rozklad (Bug #649): rt_batt + rt_ftv + rt_load + rt_curtail
    samostatné stĺpce, joint LP toggle aplikovaný v UI query (NOT v zápise).
    """
    __tablename__ = "effect_minute"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profile.id", ondelete="CASCADE"),
                                              nullable=False, index=True)
    time_iso: Mapped[str] = mapped_column(String(20), nullable=False)        # 'YYYY-MM-DD HH:MM:SS' local
    time_ms: Mapped[int] = mapped_column(Integer, nullable=False, index=True)  # epoch ms UTC pre fast range
    market: Mapped[str] = mapped_column(String(2), nullable=False)            # 'cz'|'sk'

    # € hodnoty per minútu (1-min cadence)
    dt_rev_eur: Mapped[float] = mapped_column(Float, default=0.0)             # D-1 trh settle
    rt_batt_eur: Mapped[float] = mapped_column(Float, default=0.0)            # batt drift × ZCO
    rt_ftv_eur: Mapped[float] = mapped_column(Float, default=0.0)             # FTV drift × ZCO
    rt_load_eur: Mapped[float] = mapped_column(Float, default=0.0)            # load drift × ZCO
    rt_curtail_eur: Mapped[float] = mapped_column(Float, default=0.0)         # curtail kompenzácia
    vdt_arb_eur: Mapped[float] = mapped_column(Float, default=0.0)            # VDT arbitráž (Bug #608)
    baseline_eur: Mapped[float] = mapped_column(Float, default=0.0)           # bez batt+plánu reference

    # Pomocné stĺpce pre Excel "Vsetky_15min" sheet (kW + ceny)
    batt_kw_real: Mapped[Optional[float]] = mapped_column(Float)              # realita
    plan_batt_kw: Mapped[Optional[float]] = mapped_column(Float)              # D-1 plán
    ftv_kw_real: Mapped[Optional[float]] = mapped_column(Float)
    load_kw_real: Mapped[Optional[float]] = mapped_column(Float)
    soc_pct: Mapped[Optional[float]] = mapped_column(Float)
    zco_eur: Mapped[Optional[float]] = mapped_column(Float)                   # €/MWh
    dt_eur_mwh: Mapped[Optional[float]] = mapped_column(Float)                # €/MWh

    __table_args__ = (
        UniqueConstraint("profile_id", "time_ms", name="uq_effect_minute_ptime"),
        Index("idx_effect_minute_range", "profile_id", "time_ms"),
        CheckConstraint("market IN ('cz','sk')", name="ck_effect_minute_market"),
    )

    profile: Mapped["Profile"] = relationship()


class EffectDaily(Base):
    """Bug #650 / DB unify F1: denný agregát z effect_minute pre rýchle obdobie queries.

    Aktualizovaný UPSERT-om po každom dokončenom dni v livesim.advance.
    UI karty + chC graf "Po dňoch" čítajú odtiaľto (jediný query namiesto
    sumarizácie 1440 riadkov × N dní z effect_minute).
    """
    __tablename__ = "effect_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profile.id", ondelete="CASCADE"),
                                              nullable=False, index=True)
    day: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    market: Mapped[str] = mapped_column(String(2), nullable=False)

    # Denné € agregáty
    dt_rev_eur: Mapped[float] = mapped_column(Float, default=0.0)
    rt_batt_eur: Mapped[float] = mapped_column(Float, default=0.0)
    rt_ftv_eur: Mapped[float] = mapped_column(Float, default=0.0)
    rt_load_eur: Mapped[float] = mapped_column(Float, default=0.0)
    rt_curtail_eur: Mapped[float] = mapped_column(Float, default=0.0)
    vdt_arb_eur: Mapped[float] = mapped_column(Float, default=0.0)
    baseline_eur: Mapped[float] = mapped_column(Float, default=0.0)

    # Denné kWh/SOC agregáty pre Excel + chC "FTV výroba za deň"
    ftv_kwh: Mapped[Optional[float]] = mapped_column(Float)
    load_kwh: Mapped[Optional[float]] = mapped_column(Float)
    soc_end_pct: Mapped[Optional[float]] = mapped_column(Float)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)       # ISO timestamp

    __table_args__ = (
        UniqueConstraint("profile_id", "day", name="uq_effect_daily_pday"),
        Index("idx_effect_daily_range", "profile_id", "day"),
        CheckConstraint("market IN ('cz','sk')", name="ck_effect_daily_market"),
    )

    profile: Mapped["Profile"] = relationship()


# ════════════════════════════════════════════════════════════════════════════
# SYSTEM — UI settings, Cases, Realio config
# ════════════════════════════════════════════════════════════════════════════

class UiSettings(Base):
    """Per-port UI state (plan form values, dentrh, rt, last_date).

    Replace pre out/ui_settings_<PORT>.json.
    """
    __tablename__ = "ui_settings"

    port: Mapped[str] = mapped_column(String(8), primary_key=True)
    key: Mapped[str] = mapped_column(String(32), primary_key=True)        # 'plan'|'dentrh'|'rt'|'misc'
    data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class Case(Base):
    """RT poradca konfigurácia (rt_kdis, rt_kchg, allow_curtail, …).

    Default cases: 'default', 'denny_trh_15m', 'realistic' (locked).
    """
    __tablename__ = "case"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class RealioConfig(Base):
    """Realio konfigurácia per market (host, user, tag mappings, fve_control, …).

    Citlivé polia (password, cookies) sa NEverziujú do gitu (rovnako ako pôvodný JSON).
    """
    __tablename__ = "realio_config"

    market: Mapped[str] = mapped_column(String(4), primary_key=True)
    host: Mapped[Optional[str]] = mapped_column(String(255))
    username: Mapped[Optional[str]] = mapped_column(String(64))
    password: Mapped[Optional[str]] = mapped_column(Text)                  # encrypted at rest (TODO)
    cookies: Mapped[Optional[dict]] = mapped_column(JSON)
    tags_read: Mapped[dict] = mapped_column(JSON, default=dict)
    tags_write: Mapped[dict] = mapped_column(JSON, default=dict)
    fve_control: Mapped[dict] = mapped_column(JSON, default=dict)
    poll_interval_sec: Mapped[int] = mapped_column(Integer, default=60)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


# ════════════════════════════════════════════════════════════════════════════
# MARKET STATE — per-port active market selector
# ════════════════════════════════════════════════════════════════════════════

class ActiveMarket(Base):
    """Per-port aktívny market (cz|sk). Replace pre out/_active_market*.json."""
    __tablename__ = "active_market"

    port: Mapped[str] = mapped_column(String(8), primary_key=True)
    market: Mapped[str] = mapped_column(String(4), nullable=False, default="cz")
    set_at: Mapped[str] = mapped_column(String(32), nullable=False)


# ════════════════════════════════════════════════════════════════════════════
# LIVESIM STORAGE — migrácia CSV→DB (krok 2). Nahrádza per-profil livesim CSV +
# meta.json. Flag-gated (LIVESIM_STORE=db) — kým off, tieto tabuľky sa nepoužívajú.
# ════════════════════════════════════════════════════════════════════════════

class LivesimMeta(Base):
    """Meta stav livesim per (profil, trh, case) — nahrádza livesim_*.meta.json.

    Štruktúrované polia (done_through, settings_sig, soc_after_done) v DB →
    žiadny súborový race (KeyError 'time' z rozpísaného CSV), čistá perzistencia
    cez reštart, atomický UPSERT. params/skipped ako JSON."""
    __tablename__ = "livesim_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profile.id", ondelete="CASCADE"),
                                              nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(2), nullable=False)
    case: Mapped[str] = mapped_column(String(32), nullable=False)

    start_date: Mapped[Optional[str]] = mapped_column(String(10))
    done_through: Mapped[Optional[str]] = mapped_column(String(10))
    last_min: Mapped[Optional[str]] = mapped_column(String(20))
    soc_after_done: Mapped[Optional[float]] = mapped_column(Float)
    cum_dt_done: Mapped[float] = mapped_column(Float, default=0.0)
    cum_rt_done: Mapped[float] = mapped_column(Float, default=0.0)
    settings_sig: Mapped[Optional[str]] = mapped_column(Text)
    params: Mapped[Optional[dict]] = mapped_column(JSON)
    skipped: Mapped[Optional[dict]] = mapped_column(JSON)        # {no_data:[], no_sys_mw:[]}
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        UniqueConstraint("profile_id", "market", "case", name="uq_livesim_meta_pmc"),
        CheckConstraint("market IN ('cz','sk')", name="ck_livesim_meta_market"),
    )
    profile: Mapped["Profile"] = relationship()


class LivesimTraceDay(Base):
    """Minútový livesim trace per (profil, trh, case, deň) — nahrádza livesim_*.csv.

    payload = gzip+base64 JSON {cols:[...], rows:[[...],...]} celého dňa (≤1440 min).
    Per-deň blob (nie 40-stĺpcová tabuľka × 1440 riadkov) — kompaktné, perzistentné,
    bez súborových race-ov; UI číta po dňoch (load_series(day)) tak či tak.
    soc_end = SOC na konci dňa (carry pre ďalší deň)."""
    __tablename__ = "livesim_trace_day"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profile.id", ondelete="CASCADE"),
                                              nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(2), nullable=False)
    case: Mapped[str] = mapped_column(String(32), nullable=False)
    day: Mapped[str] = mapped_column(String(10), nullable=False)        # 'YYYY-MM-DD'

    payload: Mapped[str] = mapped_column(Text, nullable=False)          # gzip+b64 JSON
    n_rows: Mapped[int] = mapped_column(Integer, default=0)
    soc_end: Mapped[Optional[float]] = mapped_column(Float)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        UniqueConstraint("profile_id", "market", "case", "day", name="uq_livesim_trace_pmcd"),
        Index("idx_livesim_trace_range", "profile_id", "market", "case", "day"),
        CheckConstraint("market IN ('cz','sk')", name="ck_livesim_trace_market"),
    )
    profile: Mapped["Profile"] = relationship()


# ════════════════════════════════════════════════════════════════════════════
# VPP FLEET — multi-batéria / reálne riadenie (project-modularizacia-skalovanie).
# Backbone pre 20-30 reálne riadených batérií. ADITÍVNE + DORMANTNÉ kým fleet mód
# nie je zapnutý (ako LivesimMeta). Topológia (battery/block/account/assignment) +
# IPC jadro↔inštancia (instance_status/command). Kontrakty viď core/schemas/vpp.py.
# ════════════════════════════════════════════════════════════════════════════

class Customer(Base):
    """Zákazník — organizačné zoskupenie batérií (1 zákazník = N batérií, napr.
    Muller = Muller-SE + Muller2-SE). Nezávislé od Block (ten je obchodná agregácia).
    Slúži na správu a agregovaný pohľad po zákazníkoch (Manager dashboard)."""
    __tablename__ = "customer"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(96), unique=True, nullable=False, index=True)
    country: Mapped[str] = mapped_column(String(4), nullable=False)            # 'sk'|'cz'
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        CheckConstraint("country IN ('sk','cz')", name="ck_customer_country"),
    )


class Battery(Base):
    """Asset / inštancia batérie. Samostatný proces (real-time vykonanie + safety).
    Per-batéria realio config (rieši single-config blocker — viac Bender hostov).
    profile_id = odkaz na Profile pre plán/parametre/mód (znovupoužitie).
    customer_id = odkaz na Zákazníka (zoskupenie). backend = realio (Bender) | cdc
    (centrálny CDC server); pre cdc je tag = cdc_prefix + zdieľaný suffix z cdc_system.json."""
    __tablename__ = "battery"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    country: Mapped[str] = mapped_column(String(4), nullable=False)            # 'sk'|'cz'
    profile_id: Mapped[Optional[int]] = mapped_column(ForeignKey("profile.id"), index=True)
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customer.id"), index=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="simulation")  # simulation|real
    backend: Mapped[str] = mapped_column(String(16), nullable=False, default="realio")   # realio|cdc
    cdc_prefix: Mapped[Optional[str]] = mapped_column(String(64))              # CDC: prefix tagu (napr. 'VW-BA')
    batt_kw: Mapped[float] = mapped_column(Float, default=0.0)
    batt_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    eff: Mapped[float] = mapped_column(Float, default=0.95)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)   # „spustiť ako inštanciu"
    # Per-batéria realio (real mód) — vlastný Bender host/creds/tagy
    realio_host: Mapped[Optional[str]] = mapped_column(String(255))
    realio_username: Mapped[Optional[str]] = mapped_column(String(64))
    realio_password: Mapped[Optional[str]] = mapped_column(Text)               # encrypted at rest (TODO)
    realio_tags_read: Mapped[dict] = mapped_column(JSON, default=dict)
    realio_tags_write: Mapped[dict] = mapped_column(JSON, default=dict)
    realio_fve_control: Mapped[dict] = mapped_column(JSON, default=dict)
    realio_poll_sec: Mapped[int] = mapped_column(Integer, default=60)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        CheckConstraint("mode IN ('simulation','real')", name="ck_battery_mode"),
        CheckConstraint("country IN ('sk','cz')", name="ck_battery_country"),
    )


class Block(Base):
    """Agregačný blok — skupina batérií idúcich na trh SPOLU. Split stratégia
    určuje delenie objemu na batérie."""
    __tablename__ = "block"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    country: Mapped[str] = mapped_column(String(4), nullable=False)
    split_strategy: Mapped[str] = mapped_column(String(24), default="free_capacity")  # free_capacity|soc_headroom|eff
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        CheckConstraint("country IN ('sk','cz')", name="ck_block_country"),
    )


class Account(Base):
    """Obchodný účet (multi-account!). V jednej krajine ich VIAC — každý vlastné
    prihlasovacie údaje; batérie/bloky sa priradia ku konkrétnemu účtu."""
    __tablename__ = "account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    country: Mapped[str] = mapped_column(String(4), nullable=False)
    product: Mapped[str] = mapped_column(String(8), default="vdt")             # 'vdt'|'dam'|...
    username: Mapped[Optional[str]] = mapped_column(String(64))
    password: Mapped[Optional[str]] = mapped_column(Text)                      # encrypted at rest (TODO)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        CheckConstraint("country IN ('sk','cz')", name="ck_account_country"),
    )


class Assignment(Base):
    """Versioned mapovanie batéria → blok → účet. valid_to=NULL = aktuálne platné.
    Pravidlo: jedna batéria má v danom čase max jeden AKTÍVNY VDT účet."""
    __tablename__ = "assignment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    battery_id: Mapped[int] = mapped_column(ForeignKey("battery.id", ondelete="CASCADE"),
                                             nullable=False, index=True)
    block_id: Mapped[Optional[int]] = mapped_column(ForeignKey("block.id"))
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("account.id"))
    valid_from: Mapped[str] = mapped_column(String(32), nullable=False)
    valid_to: Mapped[Optional[str]] = mapped_column(String(32))                # NULL = aktuálne

    __table_args__ = (
        Index("idx_assignment_active", "battery_id", "valid_to"),
    )


class InstanceStatus(Base):
    """IPC: inštancia → jadro. Posledný stav per batéria (UPSERT). Fleet Monitor číta."""
    __tablename__ = "instance_status"

    battery_id: Mapped[int] = mapped_column(ForeignKey("battery.id", ondelete="CASCADE"),
                                            primary_key=True)
    ts: Mapped[str] = mapped_column(String(32), nullable=False)
    pid: Mapped[Optional[int]] = mapped_column(Integer)
    alive: Mapped[bool] = mapped_column(Boolean, default=False)
    health: Mapped[str] = mapped_column(String(16), default="unknown")         # ok|degraded|down
    soc_pct: Mapped[Optional[float]] = mapped_column(Float)
    last_setpoint_kw: Mapped[Optional[float]] = mapped_column(Float)
    mode: Mapped[Optional[str]] = mapped_column(String(16))
    error: Mapped[Optional[str]] = mapped_column(Text)


class InstanceCommand(Base):
    """IPC: jadro → inštancia. Príkazy (start/stop/setpoint/mode); inštancia ich
    konzumuje (consumed_at). FIFO per batéria."""
    __tablename__ = "instance_command"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    battery_id: Mapped[int] = mapped_column(ForeignKey("battery.id", ondelete="CASCADE"),
                                            nullable=False, index=True)
    ts: Mapped[str] = mapped_column(String(32), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)              # start|stop|restart|setpoint|mode
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    consumed_at: Mapped[Optional[str]] = mapped_column(String(32), index=True) # NULL = čaká na spracovanie

    __table_args__ = (
        Index("idx_command_pending", "battery_id", "consumed_at"),
    )


# ─── VPP trading contract-rows (perzistencia kontraktov core/schemas/vpp.py) ──
class TradeOrder(Base):
    """Perzistovaný Order (trading → trh/split). Audit + IPC: trading proces ho
    zapíše, split/dispatch ho rozdelí na Allocation. order_id = contract id."""
    __tablename__ = "trade_order"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    block_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    country: Mapped[str] = mapped_column(String(4), nullable=False)
    day: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    slot_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)            # buy|sell
    volume_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    price_eur_mwh: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)          # dt|rt|vdt
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="planned")
    submitted_at: Mapped[Optional[str]] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        CheckConstraint("country IN ('sk','cz')", name="ck_order_country"),
        CheckConstraint("side IN ('buy','sell')", name="ck_order_side"),
        CheckConstraint("source IN ('dt','rt','vdt')", name="ck_order_source"),
        Index("idx_order_day_block", "day", "block_id"),
    )


class AllocationRow(Base):
    """Perzistovaná Allocation (split → batéria). +vybíja/−nabíja. applied_at=NULL
    = ešte nevykonaná (control loop ju môže čítať ako cieľ)."""
    __tablename__ = "allocation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    battery_id: Mapped[int] = mapped_column(ForeignKey("battery.id", ondelete="CASCADE"),
                                            nullable=False, index=True)
    block_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    order_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    day: Mapped[str] = mapped_column(String(10), nullable=False)
    slot_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    share_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    setpoint_kw: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    applied_at: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("idx_alloc_battery_slot", "battery_id", "day", "slot_idx"),
    )


class AvailabilityReportRow(Base):
    """Perzistovaný AvailabilityReport (batéria → trading). Audit dostupnosti."""
    __tablename__ = "availability_report"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    battery_id: Mapped[int] = mapped_column(ForeignKey("battery.id", ondelete="CASCADE"),
                                            nullable=False, index=True)
    day: Mapped[str] = mapped_column(String(10), nullable=False)
    slot_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    soc_pct: Mapped[float] = mapped_column(Float, nullable=False)
    free_charge_kw: Mapped[float] = mapped_column(Float, nullable=False)
    free_discharge_kw: Mapped[float] = mapped_column(Float, nullable=False)
    free_kwh: Mapped[float] = mapped_column(Float, nullable=False)
    eff: Mapped[float] = mapped_column(Float, nullable=False, default=0.95)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


__all__ = [
    # auth
    "User", "UserProfileAccess", "AuthSession", "AuditLog",
    # profile
    "Profile", "ActiveProfile",
    # plan
    "Plan", "PlanSlot", "PlanOverride",
    # data
    "LoadProfile", "FtvScenario", "VdtPaperTrade", "AutoControlEvent",
    # system
    "UiSettings", "Case", "RealioConfig", "ActiveMarket",
    # livesim storage (CSV→DB)
    "LivesimMeta", "LivesimTraceDay",
    # VPP fleet (multi-batéria / reálne riadenie) — dormantné kým fleet mód off
    "Battery", "Block", "Account", "Assignment", "InstanceStatus", "InstanceCommand",
    # VPP trading contract-rows
    "TradeOrder", "AllocationRow", "AvailabilityReportRow",
]
