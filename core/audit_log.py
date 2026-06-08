# -*- coding: utf-8 -*-
"""core/audit_log.py — Append-only event sink (JSONL).

Slúži ako structured audit trail pre write-actions v systéme:
  - profile apply / save
  - livesim advance
  - VDT trade added / blocked by gate
  - MPC tick output
  - real Bender write
  - auto_control setpoint

Každý event je 1 riadok JSONL v out/<market>/audit/<date>.jsonl
Per-day rotation, append-only, immutable (rovnako ako accounting ledger).

Použitie ako optional helper — nikdy nepadne na chybu, žiadne side-effecty
okrem disk write. Volajúci kód funguje aj keby audit log neexistoval.

Príklad:
    from core.audit_log import log_event
    log_event(actor="bg_scheduler", action="livesim_advance",
              profile="Simulacia_Coop", minutes_appended=1,
              settings_sig="ABC123")

Query:
    from core.audit_log import read_events
    events = read_events(since="2026-06-08T00:00", action="livesim_advance")
    # → list[dict], filtered by since/until/actor/action/profile
"""
from __future__ import annotations
import os
import json
import datetime as dt
import threading
from typing import Any, Dict, List, Optional


# Lock na thread-safe write (Python GIL chráni dict mutácie ale nie file append)
_WRITE_LOCK = threading.Lock()


def _audit_dir() -> str:
    """Adresár pre audit JSONL súbory. Per-market (cz/sk)."""
    try:
        import market as _mk
        root = _mk.data_dir()
    except Exception:
        root = "out/cz"
    return os.path.join(root, "audit")


def _audit_path(date_iso: Optional[str] = None) -> str:
    """Cesta k JSONL súboru pre daný deň. Default = dnes."""
    if date_iso is None:
        date_iso = dt.date.today().isoformat()
    return os.path.join(_audit_dir(), f"{date_iso}.jsonl")


def log_event(actor: str, action: str, **kwargs: Any) -> bool:
    """Pridá event do dnešného audit logu.

    Args:
        actor: kto event vyvolal — 'bg_scheduler', 'http_user',
               'auto_control', 'mpc_tick', 'cli_tool', ...
        action: čo sa stalo — 'profile_apply', 'livesim_advance',
                'vdt_trade_added', 'vdt_blocked_by_gate', 'mpc_setpoint',
                'bender_write', 'profile_save', ...
        **kwargs: ľubovoľné polia (profile, slot, kwh, eur, ...) — všetko
                  serializovateľné cez json.dumps

    Returns:
        True ak event bol úspešne zapísaný, False inak (žiadny raise).

    Garantie:
        - Append-only — žiadny existujúci riadok nemení
        - Per-day rotation (deň v UTC podľa servera)
        - Thread-safe (cez Lock)
        - Robust — chyby sa proste swallownú (nie je to kritická cesta)
    """
    try:
        ts = dt.datetime.now().isoformat(timespec="seconds")
        event: Dict[str, Any] = {
            "ts": ts, "actor": str(actor), "action": str(action),
        }
        # Serializuj všetky kwargs — non-JSONable hodnoty prevediem na str
        for k, v in kwargs.items():
            try:
                json.dumps(v)
                event[k] = v
            except (TypeError, ValueError):
                event[k] = str(v)
        line = json.dumps(event, ensure_ascii=False, default=str)
        p = _audit_path()
        with _WRITE_LOCK:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except Exception as e:
        # Posledná línia obrany — print do stderru, nikdy raise
        try:
            print(f"[audit_log.log_event] failed: {e}",
                  file=__import__("sys").stderr)
        except Exception:
            pass
        return False


def read_events(date_iso: Optional[str] = None,
                 since: Optional[str] = None,
                 until: Optional[str] = None,
                 actor: Optional[str] = None,
                 action: Optional[str] = None,
                 profile: Optional[str] = None,
                 limit: int = 1000) -> List[Dict[str, Any]]:
    """Načíta eventy z audit logu — s filtrami.

    Args:
        date_iso: ktorý deň (YYYY-MM-DD). Default = dnes.
                  Špeciálne "all" — všetky dni v audit/.
        since: iso timestamp — vrátiť iba eventy > since
        until: iso timestamp — vrátiť iba eventy < until
        actor / action / profile: filtre podľa hodnoty
        limit: max počet vrátených eventov (najnovšie najprv)

    Returns:
        list[dict] — najnovšie najprv (reverse chronological).
    """
    paths: List[str] = []
    if date_iso == "all":
        d = _audit_dir()
        if os.path.isdir(d):
            paths = sorted([os.path.join(d, f) for f in os.listdir(d)
                             if f.endswith(".jsonl")], reverse=True)
    else:
        paths = [_audit_path(date_iso)]

    out: List[Dict[str, Any]] = []
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                lines = f.readlines()
            # Iteruj odzadu (najnovšie najprv)
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                # Filtre
                if actor and e.get("actor") != actor:
                    continue
                if action and e.get("action") != action:
                    continue
                if profile and e.get("profile") != profile:
                    continue
                if since and e.get("ts", "") < since:
                    continue
                if until and e.get("ts", "") > until:
                    continue
                out.append(e)
                if len(out) >= limit:
                    return out
        except Exception:
            continue
    return out


def tail(n: int = 20, **filters: Any) -> List[Dict[str, Any]]:
    """Convenience helper — posledných N eventov (s voliteľnými filtrami)."""
    return read_events(limit=n, **filters)


def stats(date_iso: Optional[str] = None) -> Dict[str, Any]:
    """Súhrn počtu eventov za daný deň podľa action / actor.

    Use case: dashboard banner "X eventov dnes" + kto/čo dominantne robí.
    """
    events = read_events(date_iso=date_iso, limit=100000)
    out = {
        "total": len(events),
        "by_action": {},
        "by_actor": {},
        "by_profile": {},
    }
    for e in events:
        a = e.get("action", "?")
        out["by_action"][a] = out["by_action"].get(a, 0) + 1
        ac = e.get("actor", "?")
        out["by_actor"][ac] = out["by_actor"].get(ac, 0) + 1
        pr = e.get("profile")
        if pr:
            out["by_profile"][pr] = out["by_profile"].get(pr, 0) + 1
    return out


__all__ = ["log_event", "read_events", "tail", "stats"]
