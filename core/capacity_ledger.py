# -*- coding: utf-8 -*-
"""core/capacity_ledger.py — Single source of truth pre rezerváciu kapacity batérie.

Bug #611-#613 (2026-06-09):
    Pred refactorom mohol RT engine vytvoriť `_batt_p = -15000 kW` na batérii
    s `batt_kw_max = 6000 kW` lebo D-1 plán, VDT obchody a RT engine bežali
    PARALELNE bez koordinácie. Bezpečnostný clip (#610) ohraničil výsledok
    post-process, ale konflikty boli nereálne (RT engine sa "snažil" zobchodovať
    už zarezervovanú kapacitu).

    Tento modul implementuje **capacity ledger** s **explicitným poradím**:

        D-1 plán (primárny)  →  VDT obchod (s auditom)  →  RT engine (zvyšok)

    Každý zdroj rezervuje svoju kapacitu pred použitím. RT engine vidí len
    **voľnú kapacitu** (= batt_kw_max − Σ rezervácie). VDT order pred zápisom
    do paper_trades prejde **auditom** ktorý ho buď príjme, zmenší (downscale)
    alebo odmietne (reject).

API
---
    reserve(profile, day, slot_idx, source, direction, kw, trade_id=None, note=None)
        Zaberie kapacitu. UPSERT podľa (profile, day, slot_idx, source, direction, trade_id).

    available(profile, day, slot_idx, direction, batt_kw_max)
        Vráti voľnú kapacitu (kW, vždy ≥ 0) v danom smere.

    audit(profile, day, slot_idx)
        Vráti zoznam všetkých rezervácií pre slot (rozpis pre transparenciu).

    release(profile, day, slot_idx=None, source=None, trade_id=None)
        Zmaže rezervácie podľa zadaných filtrov (napr. cancel VDT order).

    clear_day(profile, day)
        Zmaže všetky rezervácie pre deň (napr. reset pri novom D-1 pláne).

Konvencie
---------
    - `slot_idx` 0..95 reprezentuje 15-min sloty dňa (slot 0 = 00:00, slot 95 = 23:45)
    - `kw` je vždy POSITIVE, znamienko určuje `direction` ('charge' = nabíja, 'discharge' = vybíja)
    - V rovnakom slote MÔŽU existovať rezervácie v oboch smeroch zároveň
      (D-1 chce vybíjať 1000 kW, VDT chce kúpiť 500 kW = nabíjať)
      Ale to je často kontraproduktívne — VDT audit logger sa pokúsi takéto
      konflikty zachytiť cez `note` field pre auditovateľnosť.
"""
from __future__ import annotations
from typing import List, Optional, Dict, Any
import datetime as dt


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def slot_idx_from_time(hh: int, mm: int) -> int:
    """Helper: 'HH:MM' → 0..95."""
    return (hh * 60 + mm) // 15


def slot_time(slot_idx: int) -> str:
    """Helper: 0..95 → 'HH:MM' (začiatok slotu)."""
    mins = slot_idx * 15
    return f"{mins // 60:02d}:{mins % 60:02d}"


# ── core API (DB-backed) ──────────────────────────────────────────────────

def reserve(profile: str, day: str, slot_idx: int,
             source: str, direction: str, kw: float,
             trade_id: Optional[str] = None,
             note: Optional[str] = None) -> Optional[int]:
    """Zaberie kapacitu batt v slote (UPSERT podľa unique key).

    Args:
        profile: meno profilu
        day: YYYY-MM-DD
        slot_idx: 0..95
        source: 'd1'|'vdt'|'rt'|'auto_control'
        direction: 'charge'|'discharge'
        kw: POSITIVE kW (znamienko z direction)
        trade_id: optional FK na vdt_paper_trade (pre VDT zdroj) alebo iný id
        note: voľný text (audit reason — downscale, conflict, ...)

    Returns: ID rezervácie alebo None pri chybe.

    Idempotentné: druhé volanie s rovnakým (profile, day, slot_idx, source,
    direction, trade_id) UPDATE-uje existujúci záznam.
    """
    if kw <= 0:
        return None    # nič na rezerváciu
    try:
        from db import get_session
        from db.models import BattCapacityReservation, Profile
        with get_session() as s:
            p = s.query(Profile).filter_by(name=profile).one_or_none()
            if p is None:
                print(f"[capacity_ledger.reserve] profile {profile!r} neexistuje")
                return None
            # UPSERT podľa unique key (profile, day, slot_idx, source, direction, trade_id)
            existing = s.query(BattCapacityReservation).filter_by(
                profile_id=p.id, day=day, slot_idx=slot_idx,
                source=source, direction=direction, trade_id=trade_id,
            ).one_or_none()
            if existing is not None:
                existing.kw = float(kw)
                existing.note = note
                s.commit()
                return existing.id
            r = BattCapacityReservation(
                profile_id=p.id, day=day, slot_idx=slot_idx,
                source=source, direction=direction, kw=float(kw),
                trade_id=trade_id, note=note, created_at=_now_iso(),
            )
            s.add(r)
            s.commit()
            return r.id
    except Exception as e:
        print(f"[capacity_ledger.reserve] chyba: {e}")
        return None


def available(profile: str, day: str, slot_idx: int,
                direction: str, batt_kw_max: float) -> float:
    """Vráti voľnú kapacitu (kW, ≥ 0) v danom smere.

    Formula:
        available = batt_kw_max − Σ kw kde direction == požadovaný smer

    Pre opačný smer rezervácie sa NEPRIČÍTA do "zaberajú" (sú nezávislé).
    Príklad: slot má rezervácie:
        d1: charge 2000
        vdt: charge 1500
        d1: discharge 500  ← v opačnom smere, neovplyvňuje voľnú charge
    Pre direction='charge', batt_kw_max=6000:
        used = 2000 + 1500 = 3500
        available = 6000 − 3500 = 2500
    Pre direction='discharge':
        used = 500
        available = 6000 − 500 = 5500
    """
    if batt_kw_max <= 0:
        return 0.0
    try:
        from db import get_session
        from db.models import BattCapacityReservation, Profile
        from sqlalchemy import func
        with get_session() as s:
            p = s.query(Profile).filter_by(name=profile).one_or_none()
            if p is None:
                return float(batt_kw_max)   # neexistujúci profil → ako keby žiadne rezervácie
            used = s.query(func.coalesce(func.sum(BattCapacityReservation.kw), 0.0)).filter_by(
                profile_id=p.id, day=day, slot_idx=slot_idx, direction=direction,
            ).scalar() or 0.0
            return max(0.0, float(batt_kw_max) - float(used))
    except Exception as e:
        print(f"[capacity_ledger.available] chyba: {e}")
        return float(batt_kw_max)   # safe fallback — povoliť plný rozsah


def audit(profile: str, day: str, slot_idx: int) -> List[Dict[str, Any]]:
    """Vráti zoznam všetkých rezervácií pre slot (audit trail).

    Returns: [{source, direction, kw, trade_id, created_at, note}, ...]
    """
    try:
        from db import get_session
        from db.models import BattCapacityReservation, Profile
        with get_session() as s:
            p = s.query(Profile).filter_by(name=profile).one_or_none()
            if p is None:
                return []
            rows = s.query(BattCapacityReservation).filter_by(
                profile_id=p.id, day=day, slot_idx=slot_idx,
            ).order_by(BattCapacityReservation.created_at).all()
            return [{
                "source": r.source, "direction": r.direction, "kw": r.kw,
                "trade_id": r.trade_id, "created_at": r.created_at, "note": r.note,
            } for r in rows]
    except Exception as e:
        print(f"[capacity_ledger.audit] chyba: {e}")
        return []


def release(profile: str, day: str, slot_idx: Optional[int] = None,
             source: Optional[str] = None,
             trade_id: Optional[str] = None) -> int:
    """Zmaže rezervácie podľa filtrov. Vráti počet zmazaných záznamov.

    Použitie:
        release(profile, day, trade_id="vdt_123")     # cancel konkrétneho VDT
        release(profile, day, source="rt")             # release všetkých RT na deň
        release(profile, day, slot_idx=42, source="vdt")  # konkrétny slot+source
    """
    try:
        from db import get_session
        from db.models import BattCapacityReservation, Profile
        with get_session() as s:
            p = s.query(Profile).filter_by(name=profile).one_or_none()
            if p is None:
                return 0
            q = s.query(BattCapacityReservation).filter_by(profile_id=p.id, day=day)
            if slot_idx is not None:
                q = q.filter_by(slot_idx=slot_idx)
            if source is not None:
                q = q.filter_by(source=source)
            if trade_id is not None:
                q = q.filter_by(trade_id=trade_id)
            n = q.count()
            q.delete(synchronize_session=False)
            s.commit()
            return n
    except Exception as e:
        print(f"[capacity_ledger.release] chyba: {e}")
        return 0


def clear_day(profile: str, day: str) -> int:
    """Zmaže všetky rezervácie pre deň (reset pri novom D-1 pláne)."""
    return release(profile, day)


# ── high-level audit gate pre VDT (#612) ──────────────────────────────────

def audit_vdt_order(profile: str, day: str, slot_idx: int,
                      direction: str, kwh: float,
                      batt_kw_max: float, trade_id: str,
                      grid_kw_import: float = None,
                      grid_kw_export: float = None) -> Dict[str, Any]:
    """Audit gate pre VDT order pred zápisom do paper_trades.

    Args:
        profile, day, slot_idx, direction: identifikátory slotu
        kwh: požadované množstvo (kWh za 15-min slot)
        batt_kw_max: fyzický limit batt (kW)
        trade_id: link na budúci paper trade
        grid_kw_import: limit nákupu zo siete kW (None = neaplikovať, back-compat)
        grid_kw_export: limit predaja do siete kW (None = neaplikovať)

    Returns: dict:
        {
            "decision": "accept" | "downscale" | "reject",
            "requested_kwh": float,
            "allowed_kwh": float,
            "free_kw": float,
            "grid_cap_kw": float (= grid limit pre tento direction, alebo inf),
            "note": str (vysvetlenie)
        }

    Konverzia: 15-min slot, kwh → kw pre porovnanie s kapacitou (kw = kwh * 4).
    Effective free_kw = min(batt_available_kw, grid_cap_kw).
        - direction="charge" (batt nabíja zo siete) → grid_cap = grid_kw_import
        - direction="discharge" (batt vybíja do siete) → grid_cap = grid_kw_export
    Ak vystačí kapacita → accept + automaticky reserve(kwh*4).
    Ak nestačí ale je nejaká voľná → downscale na voľnú + reserve.
    Ak nestačí žiadna → reject (BEZ rezervácie).

    Bug GRID-AUDIT (2026-06-10): predtým sa kontroloval len batt kapacita.
    VDT advisor mohol uzavrieť 5980 kW nákup pri grid_kw_import=200 kW →
    fyzicky neprejde sieťou → odchýlka + ZCO pokuta. Teraz aj grid clip.
    """
    if kwh <= 0:
        return {"decision": "reject", "requested_kwh": kwh, "allowed_kwh": 0.0,
                "free_kw": 0.0, "grid_cap_kw": float("inf"), "note": "kwh <= 0"}
    requested_kw = abs(float(kwh) * 4.0)   # 15-min slot → kW
    batt_free_kw = available(profile, day, slot_idx, direction, batt_kw_max)

    # Bug GRID-AUDIT: grid kapacita podľa smeru
    _dir_low = (direction or "").lower().strip()
    if _dir_low in ("charge",) and grid_kw_import is not None:
        grid_cap_kw = float(grid_kw_import or 0.0)
    elif _dir_low in ("discharge",) and grid_kw_export is not None:
        grid_cap_kw = float(grid_kw_export or 0.0)
    else:
        grid_cap_kw = float("inf")

    # Effective free = min(batt available, grid cap)
    free_kw = min(batt_free_kw, grid_cap_kw)

    if free_kw <= 0:
        _which = ("batt" if batt_free_kw <= 0 else "grid")
        return {
            "decision": "reject",
            "requested_kwh": kwh,
            "allowed_kwh": 0.0,
            "free_kw": free_kw,
            "grid_cap_kw": grid_cap_kw,
            "note": f"slot {slot_time(slot_idx)} {direction}: žiadna voľná kapacita "
                    f"({_which}=0; batt_free={batt_free_kw:.0f} kW, grid_cap={grid_cap_kw:.0f} kW)",
        }

    if requested_kw <= free_kw:
        # plne akceptované
        reserve(profile, day, slot_idx, source="vdt", direction=direction,
                 kw=requested_kw, trade_id=trade_id,
                 note=f"VDT trade {trade_id} accepted ({kwh:.2f} kWh)")
        return {
            "decision": "accept",
            "requested_kwh": kwh,
            "allowed_kwh": kwh,
            "free_kw": free_kw,
            "grid_cap_kw": grid_cap_kw,
            "note": f"slot {slot_time(slot_idx)} {direction}: prijaté plne "
                    f"(batt_free={batt_free_kw:.0f} kW, grid_cap={grid_cap_kw:.0f} kW)",
        }

    # downscale na voľnú kapacitu
    allowed_kw = free_kw
    allowed_kwh = allowed_kw / 4.0
    reserve(profile, day, slot_idx, source="vdt", direction=direction,
             kw=allowed_kw, trade_id=trade_id,
             note=f"VDT trade {trade_id} downscale {kwh:.2f}→{allowed_kwh:.2f} kWh "
                  f"(voľná kap {free_kw:.0f} kW pre {direction})")
    # Identifikovať či downscale spôsobil batt limit alebo grid limit (alebo oba)
    _bind = []
    if batt_free_kw <= grid_cap_kw + 0.5:
        _bind.append(f"batt {batt_free_kw:.0f} kW")
    if grid_cap_kw <= batt_free_kw + 0.5 and grid_cap_kw != float("inf"):
        _bind.append(f"grid {grid_cap_kw:.0f} kW")
    _bind_str = " a ".join(_bind) if _bind else f"{free_kw:.0f} kW"
    return {
        "decision": "downscale",
        "requested_kwh": kwh,
        "allowed_kwh": allowed_kwh,
        "free_kw": free_kw,
        "grid_cap_kw": grid_cap_kw,
        "note": f"slot {slot_time(slot_idx)} {direction}: požiadané {kwh:.2f} kWh "
                f"({requested_kw:.0f} kW), limit {_bind_str}",
    }


__all__ = [
    "reserve", "available", "audit", "release", "clear_day",
    "audit_vdt_order",
    "slot_idx_from_time", "slot_time",
]
