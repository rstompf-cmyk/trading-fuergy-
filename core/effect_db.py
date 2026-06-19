# -*- coding: utf-8 -*-
"""core/effect_db — jediný zdroj pravdy pre €-hodnoty livesim.

Bug #650 / DB unify F1: namiesto 3 paralelných výpočtov (core/effect.py
compute_effect_totals + r['cum_*'] z CSV + dfull['cum_rt'] sum)
všetko ide cez SQL agregát nad `effect_minute` / `effect_daily` tabuľkami.

Verejné API:
    upsert_minute_batch(profile, market, df) — zapíše/aktualizuje N minútových riadkov
    upsert_daily(profile, day, market, df_or_dict) — denný agregát
    get_period_effect(profile, date_from, date_to, joint_flags) → Dict[str, float]
    get_daily_series(profile, date_from, date_to, joint_flags) → pd.DataFrame
    get_minute_series(profile, day, joint_flags) → pd.DataFrame
    get_period_cum_series(profile, date_from, date_to, joint_flags) → pd.DataFrame
        s cum_dt/cum_rt/cum_total/cum_vdt pre chC graf

Filter joint_flags (trade_batt/ftv/load): aplikovaný v SUM() priamo v SQL.
Pre rt_eur = trade_batt*rt_batt + trade_ftv*rt_ftv + trade_load*rt_load.

Žiadne CSV agregácie pre €-hodnoty. CSV `livesim_today.csv` zostáva pre
interný incremental state (cum_dt_done, cum_rt_done, last_min).
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional, Iterable

import pandas as pd
from sqlalchemy import select, func, and_, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.session import SessionFactory as SessionLocal
from db.models import EffectMinute, EffectDaily, Profile


# ── Pomocné ───────────────────────────────────────────────────────────────

def _profile_id(session, profile_name: str) -> Optional[int]:
    row = session.execute(
        select(Profile.id).where(Profile.name == profile_name)
    ).first()
    return int(row[0]) if row else None


def _flags_factors(joint_flags: Optional[Dict]) -> Dict[str, float]:
    """Vráti faktory (1.0/0.0) pre rt_batt/ftv/load podľa joint LP toggles.

    Defaults: trade_batt=True (vždy obchodované), trade_ftv/load=False.
    Ak joint_flags je None → všetky 1.0 (= total bez filtra, backward compat).
    """
    if joint_flags is None:
        return {"batt": 1.0, "ftv": 1.0, "load": 1.0, "curtail": 1.0}
    return {
        "batt": 1.0 if bool(joint_flags.get("trade_batt", True)) else 0.0,
        "ftv": 1.0 if bool(joint_flags.get("trade_ftv", False)) else 0.0,
        "load": 1.0 if bool(joint_flags.get("trade_load", False)) else 0.0,
        "curtail": 1.0 if bool(joint_flags.get("trade_batt", True)) else 0.0,  # curtail patrí batt cestou
    }


def _iso_to_ms(time_iso: str) -> int:
    """ISO 'YYYY-MM-DD HH:MM:SS' (lokálny TZ) → epoch ms UTC."""
    t = pd.Timestamp(time_iso)
    if t.tzinfo is None:
        # assume Europe/Bratislava local → UTC
        t = t.tz_localize("Europe/Bratislava", nonexistent="shift_forward",
                            ambiguous="NaT").tz_convert("UTC")
    return int(t.value // 1_000_000)


# ── UPSERT zápis ───────────────────────────────────────────────────────────

def purge_profile(profile_name: str) -> Dict[str, int]:
    """Bug FULL-RESET-GHOSTS (2026-06-11): zmaže VŠETKY effect riadky profilu
    (effect_minute + effect_daily). Volá ho plan_store.purge_full_profile pri
    úplnom resete profilu — bez toho karty/chC/Excel ukazujú "duchov" zo starej
    histórie, ktorá už nemá zodpovedajúci livesim trace ani plány.
    """
    out = {"effect_minute": 0, "effect_daily": 0}
    try:
        from sqlalchemy import delete as _sa_delete
        with SessionLocal() as session:
            pid = _profile_id(session, profile_name)
            if pid is None:
                return out
            r1 = session.execute(_sa_delete(EffectMinute)
                                  .where(EffectMinute.profile_id == pid))
            out["effect_minute"] = int(r1.rowcount or 0)
            r2 = session.execute(_sa_delete(EffectDaily)
                                  .where(EffectDaily.profile_id == pid))
            out["effect_daily"] = int(r2.rowcount or 0)
            session.commit()
    except Exception as e:
        print(f"[effect_db.purge_profile] {profile_name}: {e}")
    return out


def upsert_minute_batch(profile_name: str, market: str, df: pd.DataFrame) -> int:
    """Zapíše/aktualizuje minútové riadky pre celý deň.

    Volá sa z livesim.advance po dokončení dňa. df musí mať stĺpec `time`
    (Timestamp) a stĺpce €-hodnoty + kW pomocníky.

    Returns: počet zapísaných riadkov.
    """
    if df is None or df.empty:
        return 0
    market = (market or "cz").lower()
    if market not in ("cz", "sk"):
        market = "cz"

    with SessionLocal() as session:
        pid = _profile_id(session, profile_name)
        if pid is None:
            return 0

        rows = []
        # PERF (2026-06-19): iterrows() + per-riadok _iso_to_ms(tz_localize)+strftime nad ~244k
        # riadkami (170 dní × 1440 min) tvorili veľký kus effectdb času. Vektorizujeme:
        #  • time_iso + time_ms RAZ na celý df (nie tz_localize per riadok),
        #  • iterujeme cez to_dict("records") (plain dicty, nie Series per riadok).
        # t_ms = rovnaká tz konverzia (Europe/Bratislava→UTC) ako _iso_to_ms → identické hodnoty
        # (overené testom). DST-ambiguózne minúty → NaT → preskočíme (pôvodný kód tam dával
        # nezmysel tiež). .get() sémantika + _maybe_float nezmenené. Bulk INSERT nižšie ostáva.
        _dd = df.copy()
        _ts = pd.to_datetime(_dd.get("time"), errors="coerce")
        _utc = _ts.dt.tz_localize("Europe/Bratislava", nonexistent="shift_forward",
                                   ambiguous="NaT").dt.tz_convert("UTC")
        _dd["__t_iso"] = _ts.dt.strftime("%Y-%m-%d %H:%M:%S")
        _dd["__t_ms"] = _utc.values.astype("int64") // 1_000_000   # NaT→min int, skip cez __ok
        _dd["__ok"] = _ts.notna().values & _utc.notna().values
        for r in _dd.to_dict("records"):
            if not r.get("__ok"):
                continue
            t_iso = r["__t_iso"]
            t_ms = int(r["__t_ms"])
            rows.append({
                "profile_id": pid,
                "time_iso": t_iso,
                "time_ms": t_ms,
                "market": market,
                "dt_rev_eur": float(r.get("dt_rev_min", 0) or 0),
                "rt_batt_eur": float(r.get("rt_rev_batt_min", 0) or 0),
                "rt_ftv_eur": float(r.get("rt_rev_ftv_min", 0) or 0),
                "rt_load_eur": float(r.get("rt_rev_load_min", 0) or 0),
                "rt_curtail_eur": float(r.get("rt_rev_curtail_min", 0) or 0),
                "vdt_arb_eur": float(r.get("vdt_arb_min", 0) or 0),
                "baseline_eur": float(r.get("baseline_per_min_eur", 0) or 0),
                "batt_kw_real": _maybe_float(r.get("batt_kw_realistic") or r.get("batt_kw")),
                "plan_batt_kw": _maybe_float(r.get("plan_batt_kw")),
                "ftv_kw_real": _maybe_float(r.get("ftv_min_real_kw") or r.get("ftv_kw")),
                "load_kw_real": _maybe_float(r.get("load_min_real_kw") or r.get("load_kw")),
                "soc_pct": _maybe_float(r.get("soc_pct")),
                "zco_eur": _maybe_float(r.get("zco_eur")),
                "dt_eur_mwh": _maybe_float(r.get("dt_real_eur") or r.get("dt_eur")),
            })

        if not rows:
            return 0

        # SQLite ON CONFLICT UPDATE (UPSERT)
        stmt = sqlite_insert(EffectMinute).values(rows)
        update_cols = {c: stmt.excluded[c] for c in (
            "dt_rev_eur", "rt_batt_eur", "rt_ftv_eur", "rt_load_eur",
            "rt_curtail_eur", "vdt_arb_eur", "baseline_eur",
            "batt_kw_real", "plan_batt_kw", "ftv_kw_real", "load_kw_real",
            "soc_pct", "zco_eur", "dt_eur_mwh",
        )}
        stmt = stmt.on_conflict_do_update(
            index_elements=["profile_id", "time_ms"],
            set_=update_cols,
        )
        session.execute(stmt)
        session.commit()
        return len(rows)


def upsert_daily(profile_name: str, day: str, market: str,
                   totals: Dict[str, float]) -> bool:
    """Denný agregát UPSERT.

    `totals` = {dt_rev_eur, rt_batt_eur, rt_ftv_eur, rt_load_eur,
                  rt_curtail_eur, vdt_arb_eur, baseline_eur,
                  ftv_kwh, load_kwh, soc_end_pct}
    """
    market = (market or "cz").lower()
    if market not in ("cz", "sk"):
        market = "cz"

    with SessionLocal() as session:
        pid = _profile_id(session, profile_name)
        if pid is None:
            return False

        row = {
            "profile_id": pid,
            "day": day,
            "market": market,
            "dt_rev_eur": float(totals.get("dt_rev_eur", 0) or 0),
            "rt_batt_eur": float(totals.get("rt_batt_eur", 0) or 0),
            "rt_ftv_eur": float(totals.get("rt_ftv_eur", 0) or 0),
            "rt_load_eur": float(totals.get("rt_load_eur", 0) or 0),
            "rt_curtail_eur": float(totals.get("rt_curtail_eur", 0) or 0),
            "vdt_arb_eur": float(totals.get("vdt_arb_eur", 0) or 0),
            "baseline_eur": float(totals.get("baseline_eur", 0) or 0),
            "ftv_kwh": _maybe_float(totals.get("ftv_kwh")),
            "load_kwh": _maybe_float(totals.get("load_kwh")),
            "soc_end_pct": _maybe_float(totals.get("soc_end_pct")),
            "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        stmt = sqlite_insert(EffectDaily).values(**row)
        update_cols = {c: stmt.excluded[c] for c in (
            "dt_rev_eur", "rt_batt_eur", "rt_ftv_eur", "rt_load_eur",
            "rt_curtail_eur", "vdt_arb_eur", "baseline_eur",
            "ftv_kwh", "load_kwh", "soc_end_pct", "updated_at", "market",
        )}
        stmt = stmt.on_conflict_do_update(
            index_elements=["profile_id", "day"],
            set_=update_cols,
        )
        session.execute(stmt)
        session.commit()
        return True


# ── Čítanie pre UI ─────────────────────────────────────────────────────────

def get_period_effect(profile_name: str, date_from: str, date_to: str,
                        joint_flags: Optional[Dict] = None) -> Dict[str, float]:
    """Vráti agregát za obdobie [date_from, date_to] s aplikovaným joint LP filtrom.

    Returns: {dt_eur, rt_eur, rt_batt_eur, rt_ftv_eur, rt_load_eur,
                vdt_arb_eur, total_eur, baseline_eur, prinos_eur, days_count}
    """
    f = _flags_factors(joint_flags)
    with SessionLocal() as session:
        pid = _profile_id(session, profile_name)
        if pid is None:
            return _empty_period()

        row = session.execute(
            select(
                func.coalesce(func.sum(EffectDaily.dt_rev_eur), 0).label("dt"),
                func.coalesce(func.sum(EffectDaily.rt_batt_eur), 0).label("rt_batt"),
                func.coalesce(func.sum(EffectDaily.rt_ftv_eur), 0).label("rt_ftv"),
                func.coalesce(func.sum(EffectDaily.rt_load_eur), 0).label("rt_load"),
                func.coalesce(func.sum(EffectDaily.rt_curtail_eur), 0).label("rt_curt"),
                func.coalesce(func.sum(EffectDaily.vdt_arb_eur), 0).label("vdt"),
                func.coalesce(func.sum(EffectDaily.baseline_eur), 0).label("bl"),
                func.count(EffectDaily.id).label("days"),
            ).where(and_(
                EffectDaily.profile_id == pid,
                EffectDaily.day >= date_from,
                EffectDaily.day <= date_to,
            ))
        ).first()

        dt_eur = float(row.dt)
        rt_batt = float(row.rt_batt) * f["batt"]
        rt_ftv = float(row.rt_ftv) * f["ftv"]
        rt_load = float(row.rt_load) * f["load"]
        rt_curt = float(row.rt_curt) * f["curtail"]
        rt_eur = rt_batt + rt_ftv + rt_load + rt_curt
        vdt = float(row.vdt)
        bl = float(row.bl)
        total = dt_eur + rt_eur + vdt
        return {
            "dt_eur": dt_eur,
            "rt_eur": rt_eur,
            "rt_batt_eur": rt_batt,
            "rt_ftv_eur": rt_ftv,
            "rt_load_eur": rt_load,
            "vdt_arb_eur": vdt,
            "total_eur": total,
            "baseline_eur": bl,
            "prinos_eur": total - bl,
            "days_count": int(row.days),
        }


def get_period_dist_fee(profile_name: str, date_from: str, date_to: str,
                         grid_fee_eur_mwh: float) -> float:
    """Distribučná úspora za obdobie (kumulatív, od štartu) = grid_fee × ušetrený import.

    User-pravidlo (2026-06-15): poplatok len na reálnu spotrebu zo siete; nabíjanie
    (z FTV aj grid-arbitráž) je vyňaté → do importu počítame iba VYBÍJANIE.
    Per minútu z effect_minute:
      baseline_import = max(load − ftv, 0)
      skutočný_import = max(load − ftv − max(batt, 0), 0)   (max(batt,0) = len discharge)
    Σ(baseline − skutočný) [kW·min] / 60 = ušetrené kWh; × grid_fee/1000 = €.
    Konzistentné s cum_dt/cum_rt (rovnaký zdroj effect_minute, vrátane dnešku cez Fázu A).
    """
    gf = float(grid_fee_eur_mwh or 0.0)
    if gf <= 0:
        return 0.0
    from sqlalchemy import text
    with SessionLocal() as session:
        pid = _profile_id(session, profile_name)
        if pid is None:
            return 0.0
        # Bug DIST-FEE-DOUBLECOUNT (2026-06-19, profil Simulacia_Coop: záporná dist úspora −337):
        # pri cons_only=False (default) je distribučný poplatok za nabíjanie zo siete UŽ v DT
        # (e_batt = (cena+grid_fee)×im_batt). Ak dist zložka brala signed batt_kw_real, pri
        # NABÍJANÍ (batt<0) zväčšila „skutočný import" → odpočítala ten istý poplatok 2× →
        # záporná úspora (double-count). Dist = LEN self-consumption (vybíjanie do load) →
        # MAX(batt_kw_real, 0.0) = iba discharge. Žiadny profil nepoužíva cons_only=True (overené);
        # ak by sa zapol, charging-fee by bol mimo DT a tu by musel byť signed (NETTO).
        row = session.execute(text(
            "SELECT COALESCE(SUM("
            "  MAX(load_kw_real - ftv_kw_real, 0.0)"
            "  - MAX(load_kw_real - ftv_kw_real - MAX(batt_kw_real, 0.0), 0.0)"
            "), 0.0) "
            "FROM effect_minute "
            "WHERE profile_id = :pid "
            "AND substr(time_iso,1,10) >= :f AND substr(time_iso,1,10) <= :t"
        ), {"pid": pid, "f": date_from, "t": date_to}).first()
        red_kw_min = float(row[0] or 0.0)
        return round(gf * (red_kw_min / 60.0) / 1000.0, 2)


def get_daily_series(profile_name: str, date_from: str, date_to: str,
                       joint_flags: Optional[Dict] = None) -> pd.DataFrame:
    """Denné riadky pre chC graf "Po dňoch" + Excel sheet.

    Vracia: DataFrame(date, dt, rt, vdt_arb, total, baseline)
    """
    f = _flags_factors(joint_flags)
    with SessionLocal() as session:
        pid = _profile_id(session, profile_name)
        if pid is None:
            return pd.DataFrame(columns=["date", "dt", "rt", "vdt_arb", "total", "baseline"])

        rows = session.execute(
            select(
                EffectDaily.day, EffectDaily.dt_rev_eur,
                EffectDaily.rt_batt_eur, EffectDaily.rt_ftv_eur,
                EffectDaily.rt_load_eur, EffectDaily.rt_curtail_eur,
                EffectDaily.vdt_arb_eur, EffectDaily.baseline_eur,
            ).where(and_(
                EffectDaily.profile_id == pid,
                EffectDaily.day >= date_from,
                EffectDaily.day <= date_to,
            )).order_by(EffectDaily.day)
        ).all()

    data = []
    for r in rows:
        rt = (float(r.rt_batt_eur or 0) * f["batt"] +
               float(r.rt_ftv_eur or 0) * f["ftv"] +
               float(r.rt_load_eur or 0) * f["load"] +
               float(r.rt_curtail_eur or 0) * f["curtail"])
        dt_eur = float(r.dt_rev_eur or 0)
        vdt = float(r.vdt_arb_eur or 0)
        data.append({
            "date": r.day,
            "dt": dt_eur,
            "rt": rt,
            "vdt_arb": vdt,
            "total": dt_eur + rt + vdt,
            "baseline": float(r.baseline_eur or 0),
        })
    return pd.DataFrame(data)


def get_minute_series(profile_name: str, day: str,
                        joint_flags: Optional[Dict] = None) -> pd.DataFrame:
    """Per-minute detail pre konkrétny deň (chC 15-min detail toggle, Excel Vsetky_15min).

    Vracia: DataFrame(time, dt, rt, vdt_arb, total, batt_kw_real, plan_batt_kw,
                       ftv_kw_real, load_kw_real, soc_pct, zco_eur, dt_eur_mwh, baseline)
    """
    f = _flags_factors(joint_flags)
    day_start = f"{day} 00:00:00"
    day_end = f"{day} 23:59:59"
    with SessionLocal() as session:
        pid = _profile_id(session, profile_name)
        if pid is None:
            return pd.DataFrame()

        rows = session.execute(
            select(EffectMinute).where(and_(
                EffectMinute.profile_id == pid,
                EffectMinute.time_iso >= day_start,
                EffectMinute.time_iso <= day_end,
            )).order_by(EffectMinute.time_ms)
        ).scalars().all()

    data = []
    for r in rows:
        rt = (float(r.rt_batt_eur or 0) * f["batt"] +
               float(r.rt_ftv_eur or 0) * f["ftv"] +
               float(r.rt_load_eur or 0) * f["load"] +
               float(r.rt_curtail_eur or 0) * f["curtail"])
        dt_eur = float(r.dt_rev_eur or 0)
        vdt = float(r.vdt_arb_eur or 0)
        data.append({
            "time": r.time_iso,
            "dt": dt_eur,
            "rt": rt,
            "vdt_arb": vdt,
            "total": dt_eur + rt + vdt,
            "batt_kw_real": r.batt_kw_real,
            "plan_batt_kw": r.plan_batt_kw,
            "ftv_kw_real": r.ftv_kw_real,
            "load_kw_real": r.load_kw_real,
            "soc_pct": r.soc_pct,
            "zco_eur": r.zco_eur,
            "dt_eur_mwh": r.dt_eur_mwh,
            "baseline": float(r.baseline_eur or 0),
        })
    df = pd.DataFrame(data)
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"])
    return df


def get_period_cum_series(profile_name: str, date_from: str, date_to: str,
                            joint_flags: Optional[Dict] = None) -> pd.DataFrame:
    """Pre chC graf "Kumulatívne" cez celé obdobie.

    Vracia: DataFrame(date, cum_dt, cum_rt, cum_vdt, cum_total, cum_baseline)
    """
    df = get_daily_series(profile_name, date_from, date_to, joint_flags=joint_flags)
    if df.empty:
        return df
    df = df.copy()
    df["cum_dt"] = df["dt"].cumsum()
    df["cum_rt"] = df["rt"].cumsum()
    df["cum_vdt"] = df["vdt_arb"].cumsum()
    df["cum_total"] = df["total"].cumsum()
    df["cum_baseline"] = df["baseline"].cumsum()
    return df


# ── Helpers ────────────────────────────────────────────────────────────────

def _empty_period() -> Dict[str, float]:
    return {"dt_eur": 0.0, "rt_eur": 0.0, "rt_batt_eur": 0.0, "rt_ftv_eur": 0.0,
            "rt_load_eur": 0.0, "vdt_arb_eur": 0.0, "total_eur": 0.0,
            "baseline_eur": 0.0, "prinos_eur": 0.0, "days_count": 0}


def _maybe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        if pd.isna(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def compute_day_totals_from_df(df: pd.DataFrame) -> Dict[str, float]:
    """Pomocný helper: zo živého `tr` df dopočíta dict totals pre upsert_daily.

    Volá ho livesim.advance po dokončení dňa pred upsert_daily(...).
    Zhoduje sa s SUM-amy ktoré sa zapíšu do effect_minute → effect_daily je
    konzistentný snapshot bez nutnosti SELECT zo minutových riadkov.
    """
    if df is None or df.empty:
        return {}

    def _s(col: str) -> float:
        if col not in df.columns:
            return 0.0
        return float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum())

    out = {
        "dt_rev_eur": _s("dt_rev_min"),
        "rt_batt_eur": _s("rt_rev_batt_min"),
        "rt_ftv_eur": _s("rt_rev_ftv_min"),
        "rt_load_eur": _s("rt_rev_load_min"),
        "rt_curtail_eur": _s("rt_rev_curtail_min"),
        "vdt_arb_eur": _s("vdt_arb_min"),
        "baseline_eur": _s("baseline_per_min_eur"),
    }
    # Bug #650-B fallback: ak decomp stĺpce neexistujú v starom CSV, dopočítaj
    # z primárnych stĺpcov (batt_kw_real, plan_batt_kw, ftv_*, load_*, zco_eur).
    # Vzorec: rt_X = (real_X - plan_X)/60 × zco / 1000 (€/min) → sumarizovaný.
    if out["rt_batt_eur"] == 0 and "zco_eur" in df.columns:
        _zco = pd.to_numeric(df["zco_eur"], errors="coerce").fillna(0)
        if ("batt_kw_realistic" in df.columns or "batt_kw" in df.columns) and "plan_batt_kw" in df.columns:
            _br = pd.to_numeric(df.get("batt_kw_realistic", df.get("batt_kw")),
                                 errors="coerce").fillna(0)
            _bp = pd.to_numeric(df["plan_batt_kw"], errors="coerce").fillna(0)
            out["rt_batt_eur"] = float(((_br - _bp) / 60.0 * _zco / 1000.0).sum())
        if "ftv_min_real_kw" in df.columns and ("ftv_hour_plan_kw" in df.columns or "ftv_plan_kw" in df.columns):
            _fr = pd.to_numeric(df["ftv_min_real_kw"], errors="coerce").fillna(0)
            _fp_col = "ftv_hour_plan_kw" if "ftv_hour_plan_kw" in df.columns else "ftv_plan_kw"
            _fp = pd.to_numeric(df[_fp_col], errors="coerce").fillna(0)
            out["rt_ftv_eur"] = float(((_fr - _fp) / 60.0 * _zco / 1000.0).sum())
        if "load_min_real_kw" in df.columns and ("load_plan_kw" in df.columns or "plan_load_kw" in df.columns):
            _lr = pd.to_numeric(df["load_min_real_kw"], errors="coerce").fillna(0)
            _lp_col = "load_plan_kw" if "load_plan_kw" in df.columns else "plan_load_kw"
            _lp = pd.to_numeric(df[_lp_col], errors="coerce").fillna(0)
            # +load = viac spotreby = under-export → záporná odchýlka
            out["rt_load_eur"] = float(((-(_lr - _lp)) / 60.0 * _zco / 1000.0).sum())
    # FTV/Load kWh (× 1/60 lebo per-minute kW → kWh)
    if "ftv_min_real_kw" in df.columns:
        out["ftv_kwh"] = _s("ftv_min_real_kw") / 60.0
    elif "ftv_kw" in df.columns:
        out["ftv_kwh"] = _s("ftv_kw") / 60.0
    if "load_min_real_kw" in df.columns:
        out["load_kwh"] = _s("load_min_real_kw") / 60.0
    elif "load_kw" in df.columns:
        out["load_kwh"] = _s("load_kw") / 60.0
    # SOC na konci dňa
    if "soc_pct" in df.columns:
        last_soc = pd.to_numeric(df["soc_pct"], errors="coerce").dropna()
        if not last_soc.empty:
            out["soc_end_pct"] = float(last_soc.iloc[-1])
    return out
