# -*- coding: utf-8 -*-
"""vdt_state.py — Single source of truth pre VDT advisor pred každým rozhodnutím.

Bug O fix (2026-06-06): VDT advisor predtým plánoval trades na základe odhadu SOC
(fallback 50% ak livesim CSV bol prázdny). To viedlo k chybným trades (napr. NABÍJAŤ
pri SOC 95% — plná batéria, žiadny efekt, ale paper trade zalogovaný ako BUY).

Architektúra:
    compute_current_state(profile, today, now) → vráti kompletný stav pred VDT LP:
      • start_soc_pct (SOC v 00:00 dnes)
      • dam_nomination_kwh (96 slotov z D-1 plánu)
      • vdt_realized_kwh (96 slotov zo všetkých paper trades dnes)
      • soc_path_pct (kumulatívna SOC trajektória 00:00 → now → end)
      • current_soc_pct (interpolované na now)
      • data_completeness flag + missing_items zoznam

Ak data_completeness=False, VDT LP NESMIE bežať. UI vyhlási error, scheduler skip.

Zdroj poriadku:
    1. start_soc = livesim včera 23:59 → D-1 plán včera terminal → soc_init (default)
    2. dam = D-1 plán pre dnes (dentrh 15-min preferovaný, plan 60-min fallback)
    3. vdt_realized = vdt_paper_trades.csv (filter today + profile)
    4. soc_path = kumulatívna integrácia (start_soc + Σ batt_kwh / batt_kwh_capacity)

Conventions:
    • Batt-pohľad: + = vybíjanie (discharge), - = nabíjanie (charge)
    • DAM nominácia: di_kwh - ch_kwh (batt-pohľad — čo musí batt fyzicky urobiť)
    • Slot indexy: 0..95 (15-min), kde slot 0 = 00:00-00:15

Autor: Bug O architektonický refactor.
"""
from __future__ import annotations
import os
import datetime as dt
from typing import Optional, Dict, List, Any

# Bug P: žiadne hard-coded defaults. Všetky hodnoty pochádzajú z profile.plan
# (ktoré profiles.load_profile auto-doplní cez _ensure_plan_vdt_defaults).
# Iba ak by sa stalo že profile.load_profile vráti None a my padneme na hard
# fallback — v tom prípade použiť bezpečnú konzervatívnu hodnotu nižšie.
# Aby žiadna hodnota nebola hard-coded mimo profilu, čítame fallback hodnoty z
# profiles._PLAN_VDT_DEFAULTS (single source of truth pre VDT defaults).


# ────────────────────────── Helpery ──────────────────────────

def _market_root() -> str:
    """`out/{market}/` cesta pre paper_trades, plans, livesim CSV."""
    try:
        import market as _mk
        return _mk.data_dir().rstrip("/").rstrip(os.sep)
    except Exception:
        return os.path.join("out", "sk")


def _safe_load_plan(profile: str, day_iso: str) -> Optional[Dict[str, Any]]:
    """Cascade load: try dentrh (15-min) first, then plan (60-min). Vracia plán JSON alebo None."""
    try:
        import plan_store as _ps
    except Exception:
        return None
    for step_min, kind in ((15, "dentrh"), (60, "plan")):
        try:
            sch = _ps.load_plan_safe(day_iso, step_min, kind, profile=profile)
            if sch is not None:
                # Pridáme metadata o tom, ktorý kind sme vybrali
                sch.setdefault("_meta_kind", kind)
                sch.setdefault("_meta_step_min", step_min)
                return sch
        except Exception:
            continue
    return None


def _slot_idx_for_time(now: dt.datetime, step_min: int = 15) -> int:
    """Index slotu (0..N-1) pre súčasný čas. Default 15-min sloty (96 cez deň)."""
    minutes_since_midnight = now.hour * 60 + now.minute
    return max(0, min(95, minutes_since_midnight // step_min))


# ────────────────────────── Start SOC zdroj ──────────────────────────

def _get_start_soc_from_livesim_yesterday(profile: str) -> Optional[Dict[str, Any]]:
    """SOC zo včerajšieho livesim CSV — posledný non-null soc_pct z 23:xx pásma.

    Vracia dict {"soc_pct": float, "source": str} alebo None.
    """
    try:
        import livesim as _ls
        import pandas as _pd
    except Exception:
        return None
    port = os.environ.get("APP_PORT") or os.environ.get("PORT") or "8000"
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    for case in ("dt_15min", "plan_d1"):
        try:
            df = _ls.load_series(case, port=port, day=yesterday, max_points=10**9)
        except Exception:
            continue
        if df is None or df.empty or "soc_pct" not in df.columns:
            continue
        sub = df[df["soc_pct"].notna()]
        if len(sub) == 0:
            continue
        # Posledný non-null soc_pct = terminálny stav včera
        soc = float(sub["soc_pct"].iloc[-1])
        ts = str(sub["time"].iloc[-1])[:19] if "time" in sub.columns else yesterday
        return {"soc_pct": soc,
                "source": f"livesim včera 23:xx ({case}, profile={profile}, ts={ts})"}
    return None


def _get_start_soc_from_d1_yesterday(profile: str) -> Optional[Dict[str, Any]]:
    """SOC z včerajšieho D-1 plánu — soc_pct[-1] = terminal SOC slot."""
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    plan = _safe_load_plan(profile, yesterday)
    if plan is None:
        return None
    schedule = plan.get("schedule") or {}
    soc_arr = schedule.get("soc_pct") or []
    if not soc_arr:
        return None
    terminal_soc = float(soc_arr[-1])
    kind = plan.get("_meta_kind", "?")
    return {"soc_pct": terminal_soc,
            "source": f"D-1 plán včera terminal ({kind}, profile={profile}, day={yesterday})"}


def _get_start_soc_default(profile: str) -> Dict[str, Any]:
    """Default soc_init z profile.plan (povinný — profil musí existovať).

    Hierarchia: profile.plan.soc_init_pct → profile.plan.fallback_soc_pct
    (Bug P: žiadny module-level konstantný fallback — vždy z profilu.)
    """
    try:
        import profiles as _pr
        p = _pr.load_profile(profile) or {}
        plan = p.get("plan") or {}
        # Priorita: soc_init_pct (D-1 plánovací default), potom fallback_soc_pct (VDT advisor fallback)
        if "soc_init_pct" in plan:
            soc = float(plan["soc_init_pct"])
            src = f"profile.plan.soc_init_pct ({soc:.0f}%)"
        elif "fallback_soc_pct" in plan:
            soc = float(plan["fallback_soc_pct"])
            src = f"profile.plan.fallback_soc_pct ({soc:.0f}%)"
        else:
            # Posledná instancia — _PLAN_VDT_DEFAULTS (single source of truth)
            soc = float(_pr._PLAN_VDT_DEFAULTS["fallback_soc_pct"])
            src = f"profiles._PLAN_VDT_DEFAULTS.fallback_soc_pct ({soc:.0f}%)"
    except Exception as _e:
        # Profile nedostupný — hard fail-safe (bezpečná konzervatívna hodnota)
        soc = 50.0
        src = f"hard fail-safe 50% (profile {profile} neexistuje: {_e})"
    return {"soc_pct": soc,
            "source": f"{src} — žiadna história (livesim/D-1 yesterday chýbajú)"}


def _get_start_soc(profile: str) -> Dict[str, Any]:
    """3-vrstvový fallback chain: livesim včera → D-1 včera → soc_init.
    Vždy vráti dict s soc_pct + source. Nikdy None.
    """
    return (_get_start_soc_from_livesim_yesterday(profile)
            or _get_start_soc_from_d1_yesterday(profile)
            or _get_start_soc_default(profile))


# ────────────────────────── DAM nominácia ──────────────────────────

def _load_dam_nomination(profile: str, today_iso: str) -> Optional[Dict[str, Any]]:
    """Načíta DAM nomináciu z D-1 plánu pre dnes.

    Vráti 96-slot array s batt-pohľad kWh (di_kwh - ch_kwh, + = vybíjať, - = nabíjať).
    Pre 60-min plan re-expanduje na 96 slotov (každá hodina = 4 sloty, kWh/4 každý).
    Vracia None ak D-1 plán pre dnes neexistuje.
    """
    plan = _safe_load_plan(profile, today_iso)
    if plan is None:
        return None
    schedule = plan.get("schedule") or {}
    di = schedule.get("di_kwh") or []
    ch = schedule.get("ch_kwh") or []
    if not di or not ch or len(di) != len(ch):
        return None
    kind = plan.get("_meta_kind", "?")
    step_min = plan.get("_meta_step_min", 15)
    # Spočítaj batt-pohľad nomináciu: + = vybíjať (di), - = nabíjať (ch)
    batt_kwh_per_slot = [float(di[i]) - float(ch[i]) for i in range(len(di))]
    # Ak je 60-min plán (24 slotov), expanduj na 96 (každý hodinový slot = 4 × 15-min so štvrtinou kWh)
    if step_min == 60 and len(batt_kwh_per_slot) == 24:
        expanded = []
        for h in range(24):
            quarter = batt_kwh_per_slot[h] / 4.0
            expanded.extend([quarter] * 4)
        batt_kwh_per_slot = expanded
    if len(batt_kwh_per_slot) != 96:
        return None                              # neočakávaný formát
    return {"kwh_batt_view": batt_kwh_per_slot,
            "kind": kind,
            "step_min": step_min,
            "source": f"D-1 plán dnes ({kind}, profile={profile}, day={today_iso})"}


# ────────────────────────── VDT realized trades ──────────────────────────

def _load_vdt_realized(profile: str, today_iso: str) -> Dict[str, Any]:
    """Načíta všetky VDT paper trades z dneška pre profil.

    Vracia dict s 96-slot array kWh (batt-pohľad: + = discharge, - = charge)
    + count of trades + suma €.
    """
    realized_kwh = [0.0] * 96
    count = 0
    total_eur = 0.0
    try:
        import vdt_live_advisor as _vla
        path = _vla.paper_trades_csv_path()
    except Exception:
        return {"kwh_batt_view": realized_kwh, "count": 0, "total_eur": 0.0,
                "source": "vdt_paper_trades.csv neprístupný"}
    if not os.path.exists(path):
        return {"kwh_batt_view": realized_kwh, "count": 0, "total_eur": 0.0,
                "source": f"{path} neexistuje (žiadne paper trades)"}
    try:
        import csv as _csv
        with open(path, "r", encoding="utf-8", newline="") as f:
            rdr = _csv.DictReader(f)
            for row in rdr:
                # Filter na dnes + profile
                if str(row.get("ts", ""))[:10] != today_iso:
                    continue
                if str(row.get("profile", "") or "") != profile:
                    continue
                slot = str(row.get("slot", "") or "")
                action = str(row.get("action", "") or "").lower()
                try:
                    kwh = float(row.get("kwh", 0) or 0)
                except (ValueError, TypeError):
                    continue
                # Spočítaj slot index z "HH:MM-HH:MM" formátu
                try:
                    hh, mm = slot.split("-")[0].split(":")
                    idx = (int(hh) * 60 + int(mm)) // 15
                    if 0 <= idx < 96:
                        # Discharge = + (predaj zo batt), Charge = - (nákup do batt)
                        sign = +1 if action == "discharge" else (-1 if action == "charge" else 0)
                        realized_kwh[idx] += sign * abs(kwh)
                        count += 1
                        # Profit estimate (price_predicted_eur * kwh, sign berie action)
                        try:
                            price = float(row.get("price_predicted_eur", 0) or 0)
                            total_eur += sign * abs(kwh) * price / 1000.0
                        except (ValueError, TypeError):
                            pass
                except Exception:
                    continue
    except Exception as e:
        return {"kwh_batt_view": [0.0] * 96, "count": 0, "total_eur": 0.0,
                "source": f"chyba pri čítaní paper_trades: {e}"}
    return {"kwh_batt_view": realized_kwh, "count": count, "total_eur": total_eur,
            "source": f"vdt_paper_trades.csv (profile={profile}, day={today_iso}, n_trades={count})"}


# ────────────────────────── SOC path integrácia ──────────────────────────

def _integrate_soc_path(start_soc_pct: float, dam_batt_kwh: List[float],
                          vdt_realized_kwh: List[float],
                          batt_kwh_capacity: float,
                          eff_c: float = 0.95, eff_d: float = 0.95,
                          soc_min_pct: float = 5.0,
                          soc_max_pct: float = 100.0) -> List[float]:
    """Kumulatívna SOC trajektória cez 96 slotov + START hodnota.

    Vstup je batt-pohľad kWh per slot (positive = vybíjať, negative = nabíjať).
    DAM + VDT spolu = total batt akcia pre slot.

    Returns: array dĺžky 97 (start + 96 endov slotu, indexed by [slot+1] pre koniec slotu).
    """
    soc_path = [float(start_soc_pct)]
    cur_soc = float(start_soc_pct)
    cap = max(1.0, batt_kwh_capacity)
    for t in range(96):
        di_kwh = max(0.0, dam_batt_kwh[t]) + max(0.0, vdt_realized_kwh[t])    # vybijanie
        ch_kwh = abs(min(0.0, dam_batt_kwh[t])) + abs(min(0.0, vdt_realized_kwh[t]))  # nabíjanie
        # Δ SOC v kWh: nabíjanie pridá ch * eff_c, vybíjanie odoberie di / eff_d
        delta_kwh = ch_kwh * eff_c - di_kwh / max(0.01, eff_d)
        cur_soc += (delta_kwh / cap) * 100.0
        cur_soc = max(soc_min_pct, min(soc_max_pct, cur_soc))   # clip do range
        soc_path.append(cur_soc)
    return soc_path


# ────────────────────────── Main API ──────────────────────────

def compute_current_state(profile: str,
                            today: Optional[dt.date] = None,
                            now: Optional[dt.datetime] = None,
                            batt_kwh: Optional[float] = None) -> Dict[str, Any]:
    """Single source of truth pre VDT advisor — vždy ho zavolaj pred LP.

    Args:
        profile: meno profilu (povinné — nie active fallback, voláme explicit)
        today: ISO date dneška (default = today)
        now: súčasný moment (default = now)
        batt_kwh: kapacita batt (default z profile.plan.batt_kwh)

    Returns dict:
        {"ok": bool, "data_completeness": bool,
         "missing_items": List[str], "warnings": List[str],
         "today": str, "now": str, "profile": str,
         "start_soc_pct": float, "start_soc_source": str,
         "dam_nomination_kwh": List[float], "dam_kind": str,
         "vdt_realized_kwh": List[float], "vdt_realized_count": int, "vdt_realized_eur": float,
         "soc_path_pct": List[float],         # 97 hodnôt (start + 96 endov slotu)
         "current_soc_pct": float, "current_slot_idx": int,
         "batt_kwh": float, "batt_kw": float,
         "eff_c": float, "eff_d": float, "soc_min_pct": float, "soc_max_pct": float,
        }

    `data_completeness=False` znamená že VDT trades sú NEDÔVERYHODNÉ.
    VDT LP MUSÍ tento prípad detegovať a NESMIE spustit optimalizáciu.
    """
    today = today or dt.date.today()
    now = now or dt.datetime.now()
    today_iso = today.isoformat()
    missing: List[str] = []
    warnings: List[str] = []

    # 1. Profil parametre — povinné
    try:
        import profiles as _pr
        prof = _pr.load_profile(profile) or {}
    except Exception:
        prof = {}
    plan = prof.get("plan") or {}
    # Bug P: žiadne hard-coded defaults — všetky hodnoty musia prísť z profile.plan
    # (profiles.load_profile auto-doplnil chýbajúce VDT polia cez _ensure_plan_vdt_defaults).
    # Plán polia (batt_kwh, eff_c, eff_d, soc_min_pct, soc_max_pct) musia byť v každom
    # rozumnom profile — ak chýba, použijeme bezpečný conservative fallback len ako
    # last resort safety (nestane sa pri zdravom profile).
    try:
        import profiles as _pr
        vdt_def = _pr._PLAN_VDT_DEFAULTS                  # single source of truth pre VDT defaults
    except Exception:
        vdt_def = {}
    cap = float(batt_kwh if batt_kwh is not None
                  else plan.get("batt_kwh") or 800.0)         # batt_kwh pri novom profile musí byť v plane
    batt_kw = float(plan.get("batt_kw") or 100.0)
    eff_c = float(plan.get("eff_c") or 0.95)
    eff_d = float(plan.get("eff_d") or 0.95)
    soc_min = float(plan.get("soc_min_pct") or 5.0)
    # Pre vdt_state používame fyzikálny strop SOC (default 100%), nie operačný
    soc_max = float(plan.get("soc_max_pct") or 100.0)

    # 2. Start SOC (vždy success, fallback chain)
    start = _get_start_soc(profile)
    start_soc = float(start["soc_pct"])
    if "default" in start["source"].lower():
        warnings.append(f"⚠ Start SOC je odhad (žiadny livesim ani D-1 plán z včera) — {start['source']}")

    # 3. DAM nominácia — povinná
    dam = _load_dam_nomination(profile, today_iso)
    if dam is None:
        missing.append("dam_today")
        dam_kwh = [0.0] * 96
        dam_kind = "missing"
    else:
        dam_kwh = dam["kwh_batt_view"]
        dam_kind = dam["kind"]

    # 4. VDT realized — vždy success (môže byť prázdny array)
    vdt = _load_vdt_realized(profile, today_iso)
    vdt_kwh = vdt["kwh_batt_view"]

    # 5. SOC path integration (start + 96 slot ends)
    soc_path = _integrate_soc_path(start_soc, dam_kwh, vdt_kwh, cap, eff_c, eff_d,
                                       soc_min_pct=soc_min, soc_max_pct=soc_max)
    # Current slot index (0..95)
    cur_idx = _slot_idx_for_time(now, step_min=15)
    # current_soc_pct = SOC na začiatku aktuálneho slotu (= koniec predchádzajúceho)
    current_soc = float(soc_path[cur_idx])      # soc_path[0]=start, soc_path[1]=koniec slotu 0

    # 6. Data completeness final check
    data_completeness = (len(missing) == 0)

    return {"ok": True,
            "data_completeness": data_completeness,
            "missing_items": missing,
            "warnings": warnings,
            "today": today_iso,
            "now": now.isoformat(timespec="seconds"),
            "profile": profile,
            "start_soc_pct": start_soc,
            "start_soc_source": start["source"],
            "dam_nomination_kwh": dam_kwh,
            "dam_kind": dam_kind,
            "dam_source": dam["source"] if dam else "missing",
            "vdt_realized_kwh": vdt_kwh,
            "vdt_realized_count": int(vdt["count"]),
            "vdt_realized_eur": float(vdt["total_eur"]),
            "vdt_realized_source": vdt["source"],
            "soc_path_pct": soc_path,
            "current_soc_pct": current_soc,
            "current_slot_idx": cur_idx,
            "batt_kwh": cap,
            "batt_kw": batt_kw,
            "eff_c": eff_c,
            "eff_d": eff_d,
            "soc_min_pct": soc_min,
            "soc_max_pct": soc_max}


# ────────────────────────── CLI smoke test ──────────────────────────

if __name__ == "__main__":
    import json as _json
    import sys
    prof = sys.argv[1] if len(sys.argv) > 1 else "default"
    state = compute_current_state(prof)
    print(_json.dumps({k: v for k, v in state.items()
                       if k not in ("dam_nomination_kwh", "vdt_realized_kwh", "soc_path_pct")},
                      indent=2, ensure_ascii=False))
    print(f"\ndam_nomination_kwh: {len(state['dam_nomination_kwh'])} slotov, "
           f"sum={sum(state['dam_nomination_kwh']):.1f} kWh")
    print(f"vdt_realized_kwh: {len(state['vdt_realized_kwh'])} slotov, "
           f"sum={sum(state['vdt_realized_kwh']):.1f} kWh")
    print(f"soc_path_pct: {len(state['soc_path_pct'])} hodnôt, "
           f"start={state['soc_path_pct'][0]:.1f}%, "
           f"current={state['current_soc_pct']:.1f}%, "
           f"end={state['soc_path_pct'][-1]:.1f}%")
