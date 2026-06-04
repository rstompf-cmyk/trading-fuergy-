# -*- coding: utf-8 -*-
"""
ftv_scenarios.py — per-dátum override hodinového FTV priebehu.

Užívateľ môže ručne upraviť 24 hodinových hodnôt FTV (kW) pre konkrétny deň
v /ftv_scenario UI. Scenár sa uloží do `out/ftv_scenarios/<date>.json` a
livesim ho preberie namiesto auto-fetchovaného PVF predikčného priebehu.

Formát súboru:
{
  "date":        "2026-05-26",
  "hourly_kw":   [0, 0, 0, 0, 0, 0, 2, 8, 22, 40, 55, 65, 70, 68, 62, 50, 35, 20, 8, 2, 0, 0, 0, 0],
  "smooth_sigma": 1.0,       # σ Gauss vyhladenia použitá pri tvorbe
  "saved_at":    "2026-05-26T15:30:00",
  "note":        "free text"
}

Použitie:
    import ftv_scenarios as fs
    if fs.has_scenario("2026-05-26"):
        hours = fs.load_scenario("2026-05-26")["hourly_kw"]
"""
from __future__ import annotations
import os, json, re
from datetime import datetime
from typing import Optional, List, Dict, Any

def _root() -> str:
    env = os.environ.get("FTV_SCENARIOS_DIR")
    if env:
        return env
    try:
        import market as _mk
        return os.path.join(_mk.data_dir(), "ftv_scenarios")
    except Exception:
        return "out/cz/ftv_scenarios"


DIR = _root()                                                        # back-compat const


# ── Dual storage prepínač (Fáza 1.11 migrácie) ─────────────────────────────
_USE_DB = os.environ.get("USE_DB", "0").strip() in ("1", "true", "True", "yes")


def _db_available() -> bool:
    if not _USE_DB:
        return False
    try:
        from db import get_session   # noqa: F401
        return True
    except Exception:
        return False


def _current_market() -> str:
    try:
        import market as _mk
        return str(_mk.active_market() or "cz")
    except Exception:
        return "cz"


def _ensure_dir() -> None:
    os.makedirs(_root(), exist_ok=True)


def _safe_date(date_iso: str) -> str:
    """Bezpečný názov súboru — povolíme len YYYY-MM-DD format."""
    s = re.sub(r"[^0-9\-]", "", str(date_iso).strip())
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        raise ValueError(f"neplatný formát dátumu: {date_iso!r} (očakávam YYYY-MM-DD)")
    return s


def _path(date_iso: str) -> str:
    return os.path.join(_root(), f"{_safe_date(date_iso)}.json")


def has_scenario(date_iso: str) -> bool:
    try:
        if _db_available():
            try:
                from db import get_session
                from db.models import FtvScenario as _DbFS
                with get_session() as s:
                    if s.query(_DbFS).filter_by(
                        market=_current_market(), date=_safe_date(date_iso)
                    ).first():
                        return True
            except Exception:
                pass
        return os.path.exists(_path(date_iso))
    except ValueError:
        return False


def save_scenario(date_iso: str, hourly_kw: List[float],
                  smooth_sigma: float = 1.0, note: str = "",
                  time_shift_min: int = 0) -> str:
    """Uloží 24 hodinových FTV hodnôt pre daný dátum + voliteľný časový posun minútovej
    reality. Vracia cestu k súboru.

    time_shift_min: cyklický posun minútovej reality o ±N min (rozsah ±180). Záporné = skôr
    (napr. mraky prišli skôr než PVF predpoklad), kladné = neskôr. Plánová krivka (hourly_kw)
    sa NEPOSÚVA — posúva sa len minútová realita ktorú livesim vygeneruje.
    """
    arr = [float(x) for x in hourly_kw]
    if len(arr) != 24:
        raise ValueError(f"očakávam 24 hodnôt, dostal {len(arr)}")
    # validácia: nezáporné, rozumný horný strop
    for i, v in enumerate(arr):
        if v < 0:
            raise ValueError(f"hodina {i}: záporná hodnota {v}")
        if v > 10000:
            raise ValueError(f"hodina {i}: hodnota {v} je príliš veľká")
    shift = max(-180, min(180, int(time_shift_min)))
    _ensure_dir()
    p = _path(date_iso)
    body = {
        "date": _safe_date(date_iso),
        "hourly_kw": arr,
        "smooth_sigma": float(smooth_sigma),
        "time_shift_min": shift,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "note": str(note),
    }
    with open(p, "w") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)
    # DB dual write
    if _db_available():
        try:
            from db import get_session
            from db.models import FtvScenario as _DbFS
            mkt = _current_market()
            with get_session() as s:
                existing = s.query(_DbFS).filter_by(
                    market=mkt, date=body["date"]
                ).one_or_none()
                if existing:
                    existing.hourly_kw = arr
                    existing.smooth_sigma = float(smooth_sigma)
                    existing.offset_h = float(shift) / 60.0
                    existing.note = str(note)
                    existing.saved_at = body["saved_at"]
                else:
                    s.add(_DbFS(
                        market=mkt, date=body["date"], hourly_kw=arr,
                        smooth_sigma=float(smooth_sigma),
                        offset_h=float(shift) / 60.0,
                        note=str(note), saved_at=body["saved_at"],
                    ))
        except Exception as e:
            print(f"[ftv_scenarios.save] DB write zlyhal: {e}")
    return p


def load_scenario(date_iso: str) -> Optional[Dict[str, Any]]:
    """Načíta scenár alebo None ak neexistuje / je poškodený."""
    try:
        safe = _safe_date(date_iso)
    except ValueError:
        return None
    # DB read prvé
    if _db_available():
        try:
            from db import get_session
            from db.models import FtvScenario as _DbFS
            with get_session() as s:
                fs = s.query(_DbFS).filter_by(
                    market=_current_market(), date=safe
                ).one_or_none()
                if fs is not None:
                    return {
                        "date": fs.date,
                        "hourly_kw": list(fs.hourly_kw or []),
                        "smooth_sigma": float(fs.smooth_sigma or 0.0),
                        "time_shift_min": int(round(float(fs.offset_h or 0.0) * 60)),
                        "saved_at": fs.saved_at,
                        "note": fs.note or "",
                    }
        except Exception as e:
            print(f"[ftv_scenarios.load DB] zlyhal: {e}")
    # JSON fallback
    p = _path(date_iso)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            d = json.load(f)
        if not isinstance(d.get("hourly_kw"), list) or len(d["hourly_kw"]) != 24:
            return None
        return d
    except (OSError, json.JSONDecodeError):
        return None


def delete_scenario(date_iso: str) -> bool:
    """Zmaže scenár (DB + JSON). True ak existoval aspoň v jednom."""
    found = False
    # DB delete
    if _db_available():
        try:
            from db import get_session
            from db.models import FtvScenario as _DbFS
            with get_session() as s:
                safe = _safe_date(date_iso)
                fs = s.query(_DbFS).filter_by(
                    market=_current_market(), date=safe
                ).one_or_none()
                if fs is not None:
                    s.delete(fs)
                    found = True
        except (ValueError, Exception) as e:
            print(f"[ftv_scenarios.delete DB] zlyhal: {e}")
    # JSON delete
    try:
        p = _path(date_iso)
    except ValueError:
        return found
    if os.path.exists(p):
        try:
            os.remove(p)
            found = True
        except OSError:
            pass
    return found


def list_scenarios() -> List[Dict[str, Any]]:
    """Vráti zoznam dostupných scenárov ako [{date, path, saved_at, note}]."""
    if _db_available():
        try:
            from db import get_session
            from db.models import FtvScenario as _DbFS
            with get_session() as s:
                rows = (s.query(_DbFS)
                          .filter_by(market=_current_market())
                          .order_by(_DbFS.date.desc()).all())
                if rows:
                    return [{
                        "date": fs.date, "path": f"db://ftv_scenario/{fs.id}",
                        "saved_at": fs.saved_at, "note": fs.note or "",
                        "peak_kw": max(fs.hourly_kw or [0]),
                    } for fs in rows]
        except Exception as e:
            print(f"[ftv_scenarios.list DB] zlyhal: {e}")
    _ensure_dir()
    out = []
    _d = _root()
    if not os.path.isdir(_d):
        return out
    for fn in os.listdir(_d):
        if not fn.endswith(".json"):
            continue
        date_iso = fn[:-5]
        try:
            with open(os.path.join(_root(), fn)) as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        out.append(dict(date=d.get("date", date_iso),
                        path=os.path.join(_root(), fn),
                        saved_at=d.get("saved_at"),
                        note=d.get("note", ""),
                        peak_kw=max(d.get("hourly_kw") or [0])))
    return sorted(out, key=lambda x: x["date"], reverse=True)


def apply_gauss_smooth(hourly_kw: List[float], idx: int, new_value: float,
                       sigma: float = 1.0, radius: int = 3) -> List[float]:
    """Server-side helper: aplikuje Gauss smoothing na susedov keď sa zmení 1 bod.
    Klient by mal robiť to isté v JS pre live preview, server toto použije pri save
    ak prišlo `apply_smooth=1` (záruka konzistentnosti).

    idx: index hodiny ktorú užívateľ "potiahol" (0..23)
    new_value: nová hodnota tej hodiny
    sigma: štandardná odchýlka Gauss kernela [hodín]
    radius: dosah smoothingu [hodín] (susedia v rozsahu ±radius sa pridajú)
    """
    import math
    out = list(hourly_kw)
    old_value = out[idx]
    delta = new_value - old_value
    out[idx] = new_value
    if abs(delta) < 1e-9 or sigma <= 0:
        return out
    # Gauss kernel pre susedov (mimo idx)
    for off in range(-radius, radius + 1):
        if off == 0:
            continue
        j = idx + off
        if j < 0 or j >= 24:
            continue
        w = math.exp(-(off * off) / (2.0 * sigma * sigma))
        # pomerná zmena — relatívne k delta s váhou w
        out[j] = max(0.0, out[j] + w * delta * 0.5)   # 0.5 = stredný vplyv (nech sused nie je rovnako veľký)
    return out
