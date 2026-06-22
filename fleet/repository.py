# -*- coding: utf-8 -*-
"""fleet/repository.py — DB prístup k VPP fleet tabuľkám (battery/block/account/
assignment + instance_status/instance_command). Vracia plain dicts (oddelenie od ORM,
žiadne detached-instance pasce — dict sa stavia vnútri session bloku).

IPC model: jadro píše príkazy (enqueue_command) → inštancia ich konzumuje
(pending_commands / mark_command_consumed) a hlási stav (write_status). Fleet
monitor číta get_status / fleet_status.
"""
from __future__ import annotations
from typing import Optional, List, Dict, Any
import datetime as dt

from db import get_session
from db import models as m


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _batt_dict(b) -> Dict[str, Any]:
    return {
        "id": b.id, "name": b.name, "country": b.country, "profile_id": b.profile_id,
        "customer_id": b.customer_id, "mode": b.mode,
        "backend": b.backend, "cdc_prefix": b.cdc_prefix,
        "batt_kw": b.batt_kw, "batt_kwh": b.batt_kwh, "eff": b.eff,
        "enabled": b.enabled, "realio_host": b.realio_host,
        "realio_username": b.realio_username, "realio_password": b.realio_password,
        "realio_tags_read": b.realio_tags_read, "realio_tags_write": b.realio_tags_write,
        "realio_fve_control": b.realio_fve_control, "realio_poll_sec": b.realio_poll_sec,
    }


def _cust_dict(c) -> Dict[str, Any]:
    return {"id": c.id, "name": c.name, "country": c.country, "note": c.note,
            "created_at": c.created_at, "updated_at": c.updated_at}


# ── batérie ──────────────────────────────────────────────────────────────
def register_battery(name: str, country: str, *, mode: str = "simulation",
                     backend: str = "realio", cdc_prefix: Optional[str] = None,
                     customer_id: Optional[int] = None,
                     batt_kw: float = 0.0, batt_kwh: float = 0.0, eff: float = 0.95,
                     profile_id: Optional[int] = None, enabled: bool = False,
                     realio_host: Optional[str] = None, realio_username: Optional[str] = None,
                     realio_password: Optional[str] = None,
                     realio_tags_read: Optional[dict] = None,
                     realio_tags_write: Optional[dict] = None,
                     realio_fve_control: Optional[dict] = None,
                     realio_poll_sec: int = 60) -> int:
    """UPSERT batérie podľa `name` (idempotentné). Vráti id."""
    now = _now()
    with get_session() as s:
        b = s.query(m.Battery).filter_by(name=name).one_or_none()
        if b is None:
            b = m.Battery(name=name, created_at=now)
            s.add(b)
        b.country = country
        b.mode = mode
        b.backend = backend or "realio"
        b.cdc_prefix = cdc_prefix
        b.customer_id = customer_id
        b.batt_kw = float(batt_kw)
        b.batt_kwh = float(batt_kwh)
        b.eff = float(eff)
        b.profile_id = profile_id
        b.enabled = bool(enabled)
        b.realio_host = realio_host
        b.realio_username = realio_username
        b.realio_password = realio_password
        b.realio_tags_read = realio_tags_read or {}
        b.realio_tags_write = realio_tags_write or {}
        b.realio_fve_control = realio_fve_control or {}
        b.realio_poll_sec = int(realio_poll_sec)
        b.updated_at = now
        s.flush()
        return b.id


def list_batteries(enabled_only: bool = False) -> List[Dict]:
    with get_session() as s:
        q = s.query(m.Battery)
        if enabled_only:
            q = q.filter(m.Battery.enabled.is_(True))
        return [_batt_dict(b) for b in q.order_by(m.Battery.id).all()]


def get_battery(battery_id: int) -> Optional[Dict]:
    with get_session() as s:
        b = s.get(m.Battery, battery_id)
        return _batt_dict(b) if b else None


def set_enabled(battery_id: int, enabled: bool) -> None:
    with get_session() as s:
        b = s.get(m.Battery, battery_id)
        if b:
            b.enabled = bool(enabled)
            b.updated_at = _now()


# ── zákazníci (organizačné zoskupenie batérií) ────────────────────────────
def create_customer(name: str, country: str, *, note: Optional[str] = None) -> int:
    """UPSERT zákazníka podľa `name` (idempotentné). Vráti id."""
    now = _now()
    with get_session() as s:
        c = s.query(m.Customer).filter_by(name=name).one_or_none()
        if c is None:
            c = m.Customer(name=name, created_at=now)
            s.add(c)
        c.country = country
        c.note = note
        c.updated_at = now
        s.flush()
        return c.id


def list_customers(country: Optional[str] = None) -> List[Dict]:
    with get_session() as s:
        q = s.query(m.Customer)
        if country:
            q = q.filter(m.Customer.country == country)
        return [_cust_dict(c) for c in q.order_by(m.Customer.name).all()]


def get_customer(customer_id: int) -> Optional[Dict]:
    with get_session() as s:
        c = s.get(m.Customer, customer_id)
        return _cust_dict(c) if c else None


def update_customer(customer_id: int, *, name: Optional[str] = None,
                    country: Optional[str] = None, note: Optional[str] = None) -> None:
    with get_session() as s:
        c = s.get(m.Customer, customer_id)
        if not c:
            return
        if name is not None:
            c.name = name
        if country is not None:
            c.country = country
        if note is not None:
            c.note = note
        c.updated_at = _now()


def set_battery_customer(battery_id: int, customer_id: Optional[int]) -> None:
    """Priradí batériu zákazníkovi (alebo odpojí ak customer_id=None)."""
    with get_session() as s:
        b = s.get(m.Battery, battery_id)
        if b:
            b.customer_id = customer_id
            b.updated_at = _now()


def batteries_for_customer(customer_id: int) -> List[Dict]:
    with get_session() as s:
        q = s.query(m.Battery).filter(m.Battery.customer_id == customer_id)
        return [_batt_dict(b) for b in q.order_by(m.Battery.name).all()]


# ── bloky (agregačné skupiny) ─────────────────────────────────────────────
def create_block(name: str, country: str, *, split_strategy: str = "free_capacity",
                 enabled: bool = True) -> int:
    """UPSERT bloku podľa name. Vráti id."""
    now = _now()
    with get_session() as s:
        b = s.query(m.Block).filter_by(name=name).one_or_none()
        if b is None:
            b = m.Block(name=name, created_at=now)
            s.add(b)
        b.country = country
        b.split_strategy = split_strategy
        b.enabled = bool(enabled)
        b.updated_at = now
        s.flush()
        return b.id


def get_block(block_id: int) -> Optional[Dict]:
    with get_session() as s:
        b = s.get(m.Block, block_id)
        if not b:
            return None
        return {"id": b.id, "name": b.name, "country": b.country,
                "split_strategy": b.split_strategy, "enabled": b.enabled}


def list_blocks() -> List[Dict]:
    with get_session() as s:
        return [{"id": b.id, "name": b.name, "country": b.country,
                 "split_strategy": b.split_strategy, "enabled": b.enabled}
                for b in s.query(m.Block).order_by(m.Block.id).all()]


def assign(battery_id: int, block_id: Optional[int] = None,
           account_id: Optional[int] = None) -> int:
    """Versioned priradenie batérie do bloku/účtu. Uzavrie predchádzajúce aktívne
    (valid_to=now) a otvorí nové (valid_to=NULL). Vráti id nového priradenia."""
    now = _now()
    with get_session() as s:
        prev = (s.query(m.Assignment)
                  .filter(m.Assignment.battery_id == battery_id,
                          m.Assignment.valid_to.is_(None)).all())
        for a in prev:
            a.valid_to = now
        new = m.Assignment(battery_id=battery_id, block_id=block_id,
                           account_id=account_id, valid_from=now, valid_to=None)
        s.add(new)
        s.flush()
        return new.id


# ── assignment (battery → block → account) ────────────────────────────────
def active_assignment(battery_id: int) -> Optional[Dict]:
    with get_session() as s:
        a = (s.query(m.Assignment)
               .filter(m.Assignment.battery_id == battery_id,
                       m.Assignment.valid_to.is_(None))
               .order_by(m.Assignment.id.desc()).first())
        if not a:
            return None
        return {"id": a.id, "battery_id": a.battery_id, "block_id": a.block_id,
                "account_id": a.account_id, "valid_from": a.valid_from}


def batteries_in_block(block_id: int) -> List[Dict]:
    with get_session() as s:
        rows = (s.query(m.Battery)
                  .join(m.Assignment, m.Assignment.battery_id == m.Battery.id)
                  .filter(m.Assignment.block_id == block_id,
                          m.Assignment.valid_to.is_(None))
                  .order_by(m.Battery.id).all())
        return [_batt_dict(b) for b in rows]


# ── status (IPC: inštancia → jadro, UPSERT) ───────────────────────────────
def write_status(battery_id: int, *, alive: bool = True, health: str = "ok",
                 soc_pct: Optional[float] = None, last_setpoint_kw: Optional[float] = None,
                 error: Optional[str] = None, mode: Optional[str] = None,
                 pid: Optional[int] = None) -> None:
    now = _now()
    with get_session() as s:
        st = s.get(m.InstanceStatus, battery_id)
        if st is None:
            st = m.InstanceStatus(battery_id=battery_id, ts=now)
            s.add(st)
        st.ts = now
        st.alive = bool(alive)
        st.health = health
        if soc_pct is not None:
            st.soc_pct = float(soc_pct)
        st.last_setpoint_kw = last_setpoint_kw
        st.error = error
        if mode is not None:
            st.mode = mode
        if pid is not None:
            st.pid = pid


def get_status(battery_id: int) -> Optional[Dict]:
    with get_session() as s:
        st = s.get(m.InstanceStatus, battery_id)
        if not st:
            return None
        return {"battery_id": st.battery_id, "ts": st.ts, "pid": st.pid, "alive": st.alive,
                "health": st.health, "soc_pct": st.soc_pct,
                "last_setpoint_kw": st.last_setpoint_kw, "mode": st.mode, "error": st.error}


def fleet_status() -> List[Dict]:
    with get_session() as s:
        return [{"battery_id": st.battery_id, "ts": st.ts, "alive": st.alive,
                 "health": st.health, "soc_pct": st.soc_pct,
                 "last_setpoint_kw": st.last_setpoint_kw, "mode": st.mode}
                for st in s.query(m.InstanceStatus).all()]


# ── príkazy (IPC: jadro → inštancia, FIFO) ────────────────────────────────
def enqueue_command(battery_id: int, type: str, payload: Optional[dict] = None) -> int:
    now = _now()
    with get_session() as s:
        c = m.InstanceCommand(battery_id=battery_id, ts=now, type=type, payload=payload or {})
        s.add(c)
        s.flush()
        return c.id


def pending_commands(battery_id: int) -> List[Dict]:
    with get_session() as s:
        rows = (s.query(m.InstanceCommand)
                  .filter(m.InstanceCommand.battery_id == battery_id,
                          m.InstanceCommand.consumed_at.is_(None))
                  .order_by(m.InstanceCommand.id).all())
        return [{"id": c.id, "type": c.type, "payload": c.payload, "ts": c.ts} for c in rows]


def mark_command_consumed(command_id: int) -> None:
    with get_session() as s:
        c = s.get(m.InstanceCommand, command_id)
        if c and c.consumed_at is None:
            c.consumed_at = _now()
