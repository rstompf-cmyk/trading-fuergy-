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
]
