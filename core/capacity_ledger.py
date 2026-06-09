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
                      batt_kw_max: float, trade_id: str) -> Dict[str, Any]:
    """Audit gate pre VDT order pred zápisom do paper_trades.

    Args:
        profile, day, slot_idx, direction: identifikátory slotu
        kwh: požadované množstvo (kWh za 15-min slot)
        batt_kw_max: fyzický limit batt (kW)
        trade_id: link na budúci paper trade

    Returns: dict:
        {
            "decision": "accept" | "downscale" | "reject",
            "requested_kwh": float,
            "allowed_kwh": float,
            "free_kw": float,
            "note": str (vysvetlenie)
        }

    Konverzia: 15-min slot, kwh → kw pre porovnanie s kapacitou (kw = kwh * 4).
    Ak vystačí kapacita → accept + automaticky reserve(kwh*4).
    Ak nestačí ale je nejaká voľná → downscale na voľnú + reserve.
    Ak nestačí žiadna → reject (BEZ rezervácie).
    """
    if kwh <= 0:
        return {"decision": "reject", "requested_kwh": kwh, "allowed_kwh": 0.0,
                "free_kw": 0.0, "note": "kwh <= 0"}
    requested_kw = abs(float(kwh) * 4.0)   # 15-min slot → kW
    free_kw = available(profile, day, slot_idx, direction, batt_kw_max)

    if free_kw <= 0:
        return {
            "decision": "reject",
            "requested_kwh": kwh,
            "allowed_kwh": 0.0,
            "free_kw": free_kw,
            "note": f"slot {slot_time(slot_idx)} {direction}: žiadna voľná kapacita (existujúce rezervácie obsadili plnú batt_kw={batt_kw_max:.0f})",
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
            "note": f"slot {slot_time(slot_idx)} {direction}: prijaté plne",
        }

    # downscale na voľnú kapacitu
    allowed_kw = free_kw
    allowed_kwh = allowed_kw / 4.0
    reserve(profile, day, slot_idx, source="vdt", direction=direction,
             kw=allowed_kw, trade_id=trade_id,
             note=f"VDT trade {trade_id} downscale {kwh:.2f}→{allowed_kwh:.2f} kWh "
                  f"(voľná kap {free_kw:.0f} kW pre {direction})")
    return {
        "decision": "downscale",
        "requested_kwh": kwh,
        "allowed_kwh": allowed_kwh,
        "free_kw": free_kw,
        "note": f"slot {slot_time(slot_idx)} {direction}: požiadané {kwh:.2f} kWh ({requested_kw:.0f} kW), voľné {free_kw:.0f} kW",
    }


# ── D-1 SOC trajektória (Bug #614) ────────────────────────────────────────

def save_d1_trajectory(profile: str, day: str,
                         soc_pct_per_slot: list,
                         tolerance_pct: float = 10.0) -> int:
    """Uloží D-1 SOC trajektóriu pre deň. UPSERT podľa (profile, day, slot_idx).

    `soc_pct_per_slot` = pole 96 hodnôt (SOC % pri začiatku každého 15-min slotu).
    Pri novom D-1 uložení sa staré záznamy pre deň najprv zmažú.

    Returns: počet uložených slotov (typicky 96).
    """
    if not soc_pct_per_slot or len(soc_pct_per_slot) == 0:
        return 0
    try:
        from db import get_session
        from db.models import D1SocTrajectory, Profile
        with get_session() as s:
            p = s.query(Profile).filter_by(name=profile).one_or_none()
            if p is None:
                print(f"[capacity_ledger.save_d1_trajectory] profile {profile!r} neexistuje")
                return 0
            # Zmaž staré pre tento deň
            s.query(D1SocTrajectory).filter_by(profile_id=p.id, day=day).delete(
                synchronize_session=False)
            # Insert nové
            _now = _now_iso()
            n_saved = 0
            for idx, soc_val in enumerate(soc_pct_per_slot[:96]):
                if soc_val is None:
                    continue
                try:
                    _soc_clipped = max(0.0, min(100.0, float(soc_val)))
                except Exception:
                    continue
                row = D1SocTrajectory(
                    profile_id=p.id, day=day, slot_idx=idx,
                    expected_soc_pct=_soc_clipped,
                    tolerance_pct=float(tolerance_pct),
                    created_at=_now,
                )
                s.add(row)
                n_saved += 1
            s.commit()
            return n_saved
    except Exception as e:
        print(f"[capacity_ledger.save_d1_trajectory] chyba: {e}")
        return 0


def expected_soc(profile: str, day: str, slot_idx: int) -> Optional[Dict[str, float]]:
    """Vráti očakávaný SOC + tolerance pre slot.

    Returns: {"expected_pct": float, "tolerance_pct": float, "lo": float, "hi": float}
             alebo None ak trajektória neexistuje.
    """
    try:
        from db import get_session
        from db.models import D1SocTrajectory, Profile
        with get_session() as s:
            p = s.query(Profile).filter_by(name=profile).one_or_none()
            if p is None:
                return None
            row = s.query(D1SocTrajectory).filter_by(
                profile_id=p.id, day=day, slot_idx=int(slot_idx),
            ).one_or_none()
            if row is None:
                return None
            return {
                "expected_pct": row.expected_soc_pct,
                "tolerance_pct": row.tolerance_pct,
                "lo": max(0.0, row.expected_soc_pct - row.tolerance_pct),
                "hi": min(100.0, row.expected_soc_pct + row.tolerance_pct),
            }
    except Exception as e:
        print(f"[capacity_ledger.expected_soc] chyba: {e}")
        return None


def clear_day_trajectory(profile: str, day: str) -> int:
    """Zmaže D-1 SOC trajektóriu pre deň. Vráti počet zmazaných slotov."""
    try:
        from db import get_session
        from db.models import D1SocTrajectory, Profile
        with get_session() as s:
            p = s.query(Profile).filter_by(name=profile).one_or_none()
            if p is None:
                return 0
            q = s.query(D1SocTrajectory).filter_by(profile_id=p.id, day=day)
            n = q.count()
            q.delete(synchronize_session=False)
            s.commit()
            return n
    except Exception as e:
        print(f"[capacity_ledger.clear_day_trajectory] chyba: {e}")
        return 0


def compute_soc_trajectory(batt_kw_per_slot: list, soc_init_pct: float,
                              batt_kwh: float, step_min: int = 15) -> list:
    """Helper: vyráta SOC trajektóriu zo `batt_kw` poľa D-1 plánu.

    Vstup:
        batt_kw_per_slot: pole kW per slot (+ = vybíja, − = nabíja)
        soc_init_pct: počiatočný SOC %
        batt_kwh: kapacita batt v kWh
        step_min: dĺžka slotu v minútach (15 alebo 60)

    Výstup: pole 96 hodnôt SOC % pri začiatku každého 15-min slotu.

    Pre step=60 sa každý hodinový slot rozhadže na 4 × 15-min sloty s
    rovnakým kW.
    """
    if not batt_kw_per_slot or batt_kwh <= 0:
        return []
    dt_h = step_min / 60.0
    soc = float(soc_init_pct or 0.0)
    out = []
    # Iba 15-min granularita support v output
    if step_min == 15:
        for idx in range(96):
            out.append(round(soc, 2))
            if idx >= len(batt_kw_per_slot):
                continue
            _kw = float(batt_kw_per_slot[idx] or 0.0)
            _delta_kwh = _kw * dt_h          # + = vybíja (SOC klesá), − = nabíja
            soc = soc - (_delta_kwh / batt_kwh) * 100.0
            soc = max(0.0, min(100.0, soc))
    elif step_min == 60:
        for hour in range(24):
            _kw = float(batt_kw_per_slot[hour] or 0.0) if hour < len(batt_kw_per_slot) else 0.0
            # Distribuuj cez 4 × 15-min sloty rovnakým kW
            _delta_per_15min = _kw * 0.25 / batt_kwh * 100.0   # %
            for q in range(4):
                out.append(round(soc, 2))
                soc = soc - _delta_per_15min
                soc = max(0.0, min(100.0, soc))
    else:
        # Iné kroky — fallback: scale na 15-min
        n_per_15 = max(1, 15 // step_min)
        for i in range(96):
            src_idx = i // n_per_15
            if src_idx >= len(batt_kw_per_slot):
                break
            _kw = float(batt_kw_per_slot[src_idx] or 0.0)
            out.append(round(soc, 2))
            _delta_per_15min = _kw * 0.25 / batt_kwh * 100.0
            soc = soc - _delta_per_15min
            soc = max(0.0, min(100.0, soc))
    # Zarovnaj na 96 prvkov
    while len(out) < 96:
        out.append(round(soc, 2))
    return out[:96]


def constrain_rt_to_soc_band(profile: str, day: str, slot_idx: int,
                                current_soc_pct: float,
                                proposed_kw: float, batt_kwh: float,
                                dt_h: float = 0.25) -> Dict[str, Any]:
    """Pre RT zásah: skontroluje že výsledný SOC zostane v ±tolerance band
    D-1 trajektórie. Ak nie, obmedzí `proposed_kw` na limit.

    Args:
        profile, day, slot_idx: identifikátor slotu
        current_soc_pct: aktuálne meraný/projektovaný SOC %
        proposed_kw: RT-navrhovaný batt výkon (+ vybíja, − nabíja)
        batt_kwh: kapacita batt
        dt_h: dĺžka zásahu v hodinách (default 0.25 = 15 min)

    Returns: {"allowed_kw": float, "decision": str, "note": str,
              "expected_soc_after": float, "band": Optional[Dict]}
    """
    if batt_kwh <= 0:
        return {"allowed_kw": proposed_kw, "decision": "no_constraint",
                "note": "batt_kwh <= 0", "expected_soc_after": current_soc_pct,
                "band": None}
    band = expected_soc(profile, day, slot_idx)
    if band is None:
        return {"allowed_kw": proposed_kw, "decision": "no_trajectory",
                "note": f"D-1 trajektória pre {day} slot {slot_idx} neexistuje",
                "expected_soc_after": current_soc_pct, "band": None}
    # Predikuj výsledný SOC ak by sa proposed_kw uplatnil za dt_h
    delta_kwh = proposed_kw * dt_h
    soc_after = current_soc_pct - (delta_kwh / batt_kwh) * 100.0
    soc_after = max(0.0, min(100.0, soc_after))
    # Skontroluj či je v bande
    if band["lo"] <= soc_after <= band["hi"]:
        return {"allowed_kw": proposed_kw, "decision": "accept",
                "note": f"SOC po zásahu {soc_after:.1f}% v bande [{band['lo']:.1f},{band['hi']:.1f}]",
                "expected_soc_after": soc_after, "band": band}
    # Obmedz proposed_kw aby výsledný SOC bol na okraji bandu
    if soc_after > band["hi"]:
        # Zásah by zvýšil SOC (= nabíja) nad band → obmedz na hranicu
        target_soc = band["hi"]
        required_delta_pct = current_soc_pct - target_soc
        required_kwh = required_delta_pct * batt_kwh / 100.0
        allowed_kw = required_kwh / dt_h
        return {"allowed_kw": allowed_kw, "decision": "constrain_charge",
                "note": f"RT chcel SOC {soc_after:.1f}%, hi limit {band['hi']:.1f}% → obmedz na {allowed_kw:.0f} kW",
                "expected_soc_after": target_soc, "band": band}
    else:   # soc_after < lo
        target_soc = band["lo"]
        required_delta_pct = current_soc_pct - target_soc
        required_kwh = required_delta_pct * batt_kwh / 100.0
        allowed_kw = required_kwh / dt_h
        return {"allowed_kw": allowed_kw, "decision": "constrain_discharge",
                "note": f"RT chcel SOC {soc_after:.1f}%, lo limit {band['lo']:.1f}% → obmedz na {allowed_kw:.0f} kW",
                "expected_soc_after": target_soc, "band": band}


__all__ = [
    "reserve", "available", "audit", "release", "clear_day",
    "audit_vdt_order",
    "slot_idx_from_time", "slot_time",
    # Bug #614 — D-1 SOC trajektória
    "save_d1_trajectory", "expected_soc", "clear_day_trajectory",
    "compute_soc_trajectory", "constrain_rt_to_soc_band",
]
