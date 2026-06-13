"""Livesim storage — DB backend (migrácia CSV→DB, krok 2).

Nahrádza per-profil livesim CSV + meta.json DB tabuľkami (LivesimMeta,
LivesimTraceDay). Flag-gated: aktívne len keď LIVESIM_STORE=db.

Dôvody (user 2026-06-13 "malo by to byť všetko v DB"):
  - žiadny súborový race (KeyError 'time' z rozpísaného CSV),
  - čistá perzistencia cez reštart + atomický UPSERT meta,
  - per-profil izolácia bez súborov.

Trace sa ukladá per DEŇ ako gzip+base64 JSON blob (cols + rows). load_series(day)
číta jeden deň; full read spojí dni. Tento modul je čisto DATA layer — wiring do
livesim.advance/_read_csv je samostatný krok (za rovnakým flagom).
"""
from __future__ import annotations

import base64
import gzip
import json
import os
from datetime import datetime
from typing import Optional, List

import pandas as pd

__all__ = ["use_db", "read_meta", "write_meta", "read_trace_day", "read_trace_all",
           "write_trace_day", "available_days", "delete_profile", "import_csv_dir"]


def use_db() -> bool:
    """True ak je DB backend zapnutý (flag LIVESIM_STORE=db) a DB dostupná."""
    if str(os.environ.get("LIVESIM_STORE", "csv")).lower() != "db":
        return False
    try:
        from db.session import get_session  # noqa
        return True
    except Exception:
        return False


# ── payload encode/decode ────────────────────────────────────────────────
def encode_day(df: pd.DataFrame) -> tuple[str, int]:
    """DataFrame dňa → (gzip+base64 JSON, n_rows). time stĺpce ako ISO string."""
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_datetime64_any_dtype(d[c]):
            d[c] = d[c].dt.strftime("%Y-%m-%d %H:%M:%S")
    obj = {"cols": list(d.columns), "rows": d.where(pd.notnull(d), None).values.tolist()}
    raw = json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, 6)).decode("ascii"), int(len(d))


def decode_day(payload: str) -> pd.DataFrame:
    """gzip+base64 JSON → DataFrame; time/ts15 späť na datetime."""
    raw = gzip.decompress(base64.b64decode(payload.encode("ascii")))
    obj = json.loads(raw.decode("utf-8"))
    df = pd.DataFrame(obj["rows"], columns=obj["cols"])
    for c in ("time", "ts15"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


# ── helpers ──────────────────────────────────────────────────────────────
def _safe(name) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "default"))[:48]


def _profile_id(s, profile: str):
    from db.models import Profile as _P
    p = s.query(_P).filter_by(name=_safe(profile)).one_or_none()
    return p.id if p else None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── META ───────────────────────────────────────────────────────────────────
def read_meta(profile: str, market: str, case: str) -> Optional[dict]:
    from db.session import get_session
    from db.models import LivesimMeta
    with get_session() as s:
        pid = _profile_id(s, profile)
        if pid is None:
            return None
        m = s.query(LivesimMeta).filter_by(profile_id=pid, market=market, case=case).one_or_none()
        if m is None:
            return None
        out = dict(case=m.case, start_date=m.start_date, done_through=m.done_through,
                   last_min=m.last_min, soc_after_done=m.soc_after_done,
                   cum_dt_done=m.cum_dt_done or 0.0, cum_rt_done=m.cum_rt_done or 0.0,
                   settings_sig=m.settings_sig, params=m.params or {})
        sk = m.skipped or {}
        out["skipped_no_data"] = sk.get("no_data", [])
        out["skipped_no_sys_mw"] = sk.get("no_sys_mw", [])
        return out


def write_meta(profile: str, market: str, case: str, meta: dict) -> None:
    from db.session import get_session
    from db.models import LivesimMeta
    with get_session() as s:
        pid = _profile_id(s, profile)
        if pid is None:
            return
        row = s.query(LivesimMeta).filter_by(profile_id=pid, market=market, case=case).one_or_none()
        skipped = {"no_data": meta.get("skipped_no_data", []),
                   "no_sys_mw": meta.get("skipped_no_sys_mw", [])}
        vals = dict(start_date=meta.get("start_date"), done_through=meta.get("done_through"),
                    last_min=meta.get("last_min"), soc_after_done=meta.get("soc_after_done"),
                    cum_dt_done=float(meta.get("cum_dt_done") or 0.0),
                    cum_rt_done=float(meta.get("cum_rt_done") or 0.0),
                    settings_sig=meta.get("settings_sig"), params=meta.get("params") or {},
                    skipped=skipped, updated_at=_now())
        if row is None:
            s.add(LivesimMeta(profile_id=pid, market=market, case=case, **vals))
        else:
            for k, v in vals.items():
                setattr(row, k, v)


# ── TRACE ──────────────────────────────────────────────────────────────────
def write_trace_day(profile: str, market: str, case: str, day: str,
                    df_day: pd.DataFrame, soc_end: Optional[float] = None) -> None:
    from db.session import get_session
    from db.models import LivesimTraceDay
    payload, n = encode_day(df_day)
    with get_session() as s:
        pid = _profile_id(s, profile)
        if pid is None:
            return
        row = s.query(LivesimTraceDay).filter_by(profile_id=pid, market=market,
                                                 case=case, day=day).one_or_none()
        if row is None:
            s.add(LivesimTraceDay(profile_id=pid, market=market, case=case, day=day,
                                  payload=payload, n_rows=n, soc_end=soc_end, updated_at=_now()))
        else:
            row.payload = payload; row.n_rows = n; row.soc_end = soc_end; row.updated_at = _now()


def read_trace_day(profile: str, market: str, case: str, day: str) -> Optional[pd.DataFrame]:
    from db.session import get_session
    from db.models import LivesimTraceDay
    with get_session() as s:
        pid = _profile_id(s, profile)
        if pid is None:
            return None
        row = s.query(LivesimTraceDay).filter_by(profile_id=pid, market=market,
                                                 case=case, day=day).one_or_none()
        return decode_day(row.payload) if row else None


def read_trace_all(profile: str, market: str, case: str) -> Optional[pd.DataFrame]:
    """Spojí všetky dni do jedného DataFrame (zoradené podľa dňa)."""
    from db.session import get_session
    from db.models import LivesimTraceDay
    with get_session() as s:
        pid = _profile_id(s, profile)
        if pid is None:
            return None
        rows = (s.query(LivesimTraceDay).filter_by(profile_id=pid, market=market, case=case)
                .order_by(LivesimTraceDay.day).all())
        if not rows:
            return None
        frames = [decode_day(r.payload) for r in rows]
    return pd.concat(frames, ignore_index=True) if frames else None


def available_days(profile: str, market: str, case: str) -> List[str]:
    from db.session import get_session
    from db.models import LivesimTraceDay
    with get_session() as s:
        pid = _profile_id(s, profile)
        if pid is None:
            return []
        return [r.day for r in (s.query(LivesimTraceDay.day)
                .filter_by(profile_id=pid, market=market, case=case)
                .order_by(LivesimTraceDay.day).all())]


def delete_profile(profile: str, market: str = None, case: str = None) -> dict:
    """Zmaže livesim DB dáta profilu (per market+case alebo všetko). Pre full reset."""
    from db.session import get_session
    from db.models import LivesimMeta, LivesimTraceDay
    out = {"meta": 0, "days": 0}
    with get_session() as s:
        pid = _profile_id(s, profile)
        if pid is None:
            return out
        for model, kkey in ((LivesimMeta, "meta"), (LivesimTraceDay, "days")):
            q = s.query(model).filter_by(profile_id=pid)
            if market:
                q = q.filter_by(market=market)
            if case:
                q = q.filter_by(case=case)
            out[kkey] = q.count()
            q.delete(synchronize_session=False)
    return out


# ── CSV → DB import (migrácia existujúcich dát) ────────────────────────────
def import_csv_dir(out_dir: str, market: str, profile: str, case: str = "plan_d1",
                   csv_path: str = None, meta_path: str = None) -> dict:
    """Načíta existujúci livesim CSV + meta.json a uloží do DB (per-deň blob).
    Idempotentné (UPSERT). Vracia počty."""
    res = {"days": 0, "rows": 0, "meta": False}
    if csv_path is None:
        csv_path = os.path.join(out_dir, f"livesim_{case}__{_safe(profile)}.csv")
    if meta_path is None:
        meta_path = csv_path[:-4] + ".meta.json"
    # meta
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                _m = json.load(f)
            write_meta(profile, market, case, _m)
            res["meta"] = True
        except Exception as e:
            print(f"[import_csv_dir] meta {meta_path}: {e}")
    # trace per deň
    if os.path.exists(csv_path):
        try:
            df = pd.read_csv(csv_path)
            if "time" in df.columns and not df.empty:
                df["time"] = pd.to_datetime(df["time"], errors="coerce")
                df = df.dropna(subset=["time"])
                for day, g in df.groupby(df["time"].dt.date):
                    soc_end = float(g["soc_kwh"].iloc[-1]) if "soc_kwh" in g.columns and len(g) else None
                    write_trace_day(profile, market, case, str(day), g.reset_index(drop=True), soc_end)
                    res["days"] += 1; res["rows"] += len(g)
        except Exception as e:
            print(f"[import_csv_dir] trace {csv_path}: {e}")
    return res
