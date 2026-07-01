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


def _engine_plan_order() -> tuple:
    """Bug SOC-UNIFY-PLAN (2026-06-14): poradie (step_min, kind) na načítanie D-1 plánu
    ZHODNÉ s tým, čo používa engine livesim. Engine zapisuje svoj d1_step_min do meta
    (60→'plan', 15→'dentrh'). Bez tohto advisor VŽDY preferoval 'dentrh' → ak engine
    bežal na 'plan' (hodinový), advisor čítal INÝ plán než graf → iná SOC trajektória
    → VDT navrhoval nákupy do batérie, ktorá je v engine už plná ("nemá sa kam uložiť").
    Fallback (meta chýba): poradie (15 dentrh, 15 plan, 60 plan).

    Bug VDT-DAM-TODAY (2026-06-26): po prechode na IMMUTABLE 15-min plány auto-plán ukladá
    kind=(15,"plan") (PREDIKOVANÝ); reálny DAM (15,"dentrh") existuje len keď príde DAM.
    Stará kaskáda ((15,"dentrh"),(60,"plan")) nikdy neskúšala (15,"plan") → DAM nominácia na
    dnes sa nenašla → compute_current_state vrátil data_completeness=False ("dam_today") →
    VDT advisor ok=False → NULA obchodov (VW_simulacia_2/3/4). Fix: do kaskády pridať
    (15,"plan"). Poradie: reálny DAM (dentrh, ak je) → predikovaný 15-min plán → legacy 60-min."""
    default = ((15, "dentrh"), (15, "plan"), (60, "plan"))
    try:
        import livesim as _ls
        port = os.environ.get("PORT") or os.environ.get("APP_PORT") or "8000"
        best_step = None
        best_mt = -1.0
        for case in ("dt_15min", "plan_d1"):
            try:
                _, mp = _ls.paths(case, port)
                mt = os.path.getmtime(mp)
                meta = _ls._load_meta(mp) or {}
                step = meta.get("d1_step_min")
                if step is not None and mt > best_mt:
                    best_mt = mt
                    best_step = int(step)
            except Exception:
                continue
        if best_step == 60:
            return ((60, "plan"), (15, "dentrh"), (15, "plan"))
        if best_step == 15:
            return ((15, "dentrh"), (15, "plan"), (60, "plan"))
    except Exception:
        pass
    return default


def _safe_load_plan(profile: str, day_iso: str) -> Optional[Dict[str, Any]]:
    """Cascade load D-1 plánu — poradie kind podľa enginu (SOC-UNIFY-PLAN), aby advisor
    čítal TEN ISTÝ plán ako graf/engine. Vracia plán JSON alebo None."""
    try:
        import plan_store as _ps
    except Exception:
        return None
    for step_min, kind in _engine_plan_order():
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
    port = os.environ.get("PORT") or os.environ.get("APP_PORT") or "8000"
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    # Bug START-SOC-CASE (2026-06-11): poradie case-ov podľa čerstvosti meta.json
    # (naposledy advancovaný = ten ktorý sa reálne používa). Pevné poradie
    # (dt_15min najprv) bralo štart SOC zo zastaraného CSV nepoužívaného case-u
    # → dnešný deň štartoval z náhodnej historickej hodnoty namiesto konca
    # včerajška v aktívnom case (plan_d1).
    _cases = ["dt_15min", "plan_d1"]
    try:
        def _meta_mtime(_c):
            try:
                _, _mp = _ls.paths(_c, port)
                return os.path.getmtime(_mp)
            except OSError:
                return 0.0
        _cases.sort(key=_meta_mtime, reverse=True)
    except Exception:
        pass
    for case in _cases:
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


def _get_current_soc_from_livesim_today(profile: str, today: dt.date,
                                          now: dt.datetime) -> Optional[Dict[str, Any]]:
    """Bug SOC-UNIFY (2026-06-13): kanonický REÁLNY „aktuálny SOC" — posledný
    non-null soc_pct z DNEŠNÉHO livesim traceu v čase <= now.

    Toto je engine pravda (plán + VDT + RT po clipe). Slúži na to, aby audit,
    VDT advisor, auto_control aj zobrazenie mali identický SOC — všetci ho
    dostanú cez compute_current_state. Vracia {"soc_pct", "source"} alebo None
    ak dnešný trace ešte neexistuje (vtedy fallback na plán projekciu).

    KROK 2 (2026-06-28): TENKÝ WRAPPER nad core.soc_source.current_engine_soc
    (single source of truth pre aktuálny SOC). Logika nezmenená (čistý presun).
    """
    from core.soc_source import current_engine_soc as _ces
    return _ces(profile, today, now)


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
        # Priorita: soc_init_pct → soc_init → fallback_soc_pct → _PLAN_VDT_DEFAULTS
        # Bug SOC-NULL-KEY (2026-06-10): "in plan" je True aj pre null hodnoty
        # (napr. config.json má {"soc_init_pct": null}). float(None) → TypeError
        # → exception → hard fail-safe 50%. Riešenie: kontrolovať is not None.
        _v_init_pct = plan.get("soc_init_pct")
        _v_init = plan.get("soc_init")
        _v_fallback = plan.get("fallback_soc_pct")
        if _v_init_pct is not None:
            soc = float(_v_init_pct)
            src = f"profile.plan.soc_init_pct ({soc:.0f}%)"
        elif _v_init is not None:
            soc = float(_v_init)
            src = f"profile.plan.soc_init ({soc:.0f}%)"
        elif _v_fallback is not None:
            soc = float(_v_fallback)
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

    Vráti 96-slot array s batt-pohľad kWh (+ = vybíjať, - = nabíjať).
    Schedule v plan_store používa tieto kľúče (priorita):
      1. batt_kw (signed kW): + = discharge, - = charge → kWh = batt_kw × dt
      2. _discharge_kw / _charge_kw (separated, vždy non-neg): kWh = (di - ch) × dt
    Pre 60-min plan re-expanduje na 96 slotov.
    Vracia None ak D-1 plán pre dnes neexistuje alebo má prázdne polia.
    """
    plan = _safe_load_plan(profile, today_iso)
    if plan is None:
        return None
    schedule = plan.get("schedule") or {}
    kind = plan.get("_meta_kind", "?")
    step_min = plan.get("_meta_step_min", 15)
    dt_h = step_min / 60.0                            # dĺžka slotu v hodinách

    # Priorita 1: batt_kw (signed kW) — najčastejšie pole z optimizer.optimize_day
    batt_kw_arr = schedule.get("batt_kw") or []
    if batt_kw_arr:
        batt_kwh_per_slot = [float(v) * dt_h for v in batt_kw_arr]
    else:
        # Priorita 2: _discharge_kw - _charge_kw (separated streams)
        di = schedule.get("_discharge_kw") or []
        ch = schedule.get("_charge_kw") or []
        if not di and not ch:
            return None                                 # žiadne použiteľné pole
        n = max(len(di), len(ch))
        di = list(di) + [0.0] * (n - len(di))
        ch = list(ch) + [0.0] * (n - len(ch))
        batt_kwh_per_slot = [(float(di[i]) - float(ch[i])) * dt_h for i in range(n)]
    if not batt_kwh_per_slot:
        return None
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
    # Bug VDT-DATE-ISO (2026-06-11): volajúci občas pošle Timestamp.isoformat()
    # ("2026-06-10T00:00:00") — porovnanie ts[:10] != today_iso potom NIKDY nesedí
    # a funkcia ticho vráti nuly (= VDT trades sa neaplikujú). Normalizuj na YYYY-MM-DD.
    today_iso = str(today_iso)[:10]
    # POZN (2026-06-19): closed-only filter v REALITE (batéria) sa robí cez #27 closed-price
    # cestu (get_closedprice_batt_kw / _compute_closedprice_day — oceňuje VDT REÁLNYMI OKTE
    # cenami), NIE ad-hoc filtrom na predikčných cenách (ten by zaniesol chybu). Tu reality
    # nemeníme — VDT engine ostáva ako bol; closed-price sa zapína cez vdt_closed_from/to.
    try:
        import vdt_live_advisor as _vla
        # Bug #620: musíme explicitne odovzdať profile, inak sandbox-aware path
        # resolver vráti zlú cestu (default/legacy) a CSV nenájdeme.
        path = _vla.paper_trades_csv_path(profile)
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


def get_realized_prices_per_slot(profile: str, today_iso: str) -> list:
    """Bug QQ (2026-06-08): vrati 96-slot array s realnymi cenami pri paper trades.

    Cena = vazeny priemer price_predicted_eur cez vsetky trades v slote
    (weight = abs(kwh) — väčšie obchody dominuju).

    Slot bez trade = NaN.

    Pouzitie: livesim 15-min tabulka stlpec VDT €/MWh — uzivatel vidi za aku
    cenu sa obchody UZAVRELI (paper trade execution price), nie OKTE clearing.
    """
    import math
    prices = [float("nan")] * 96
    sum_pw = [0.0] * 96      # sum (price * weight)
    sum_w = [0.0] * 96       # sum (weight)
    today_iso = str(today_iso)[:10]    # Bug VDT-DATE-ISO: normalizácia (viď _load_vdt_realized)
    try:
        import vdt_live_advisor as _vla
        # Bug #620: musíme explicitne odovzdať profile, inak sandbox-aware path
        # resolver vráti zlú cestu (default/legacy) a CSV nenájdeme.
        path = _vla.paper_trades_csv_path(profile)
    except Exception:
        return prices
    if not os.path.exists(path):
        return prices
    try:
        import csv as _csv
        with open(path, "r", encoding="utf-8", newline="") as f:
            rdr = _csv.DictReader(f)
            for row in rdr:
                if str(row.get("ts", ""))[:10] != today_iso:
                    continue
                if str(row.get("profile", "") or "") != profile:
                    continue
                slot = str(row.get("slot", "") or "")
                action = str(row.get("action", "") or "").lower()
                if action not in ("charge", "discharge"):
                    continue
                try:
                    kwh = float(row.get("kwh", 0) or 0)
                    price = float(row.get("price_predicted_eur", 0) or 0)
                except (ValueError, TypeError):
                    continue
                w = abs(kwh)
                if w <= 0 or not math.isfinite(price) or abs(price) < 0.01:
                    continue
                try:
                    hh, mm = slot.split("-")[0].split(":")
                    idx = (int(hh) * 60 + int(mm)) // 15
                except Exception:
                    continue
                if not (0 <= idx < 96):
                    continue
                sum_pw[idx] += price * w
                sum_w[idx] += w
        for i in range(96):
            if sum_w[i] > 0:
                prices[i] = sum_pw[i] / sum_w[i]
    except Exception:
        pass
    return prices


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
    # Bug VDT-SOC-RANGE (2026-06-11): profil ukladá rozsah batérie pod kľúčmi
    # soc_min/soc_max — pôvodné soc_min_pct/soc_max_pct tu NIKDY neexistovali,
    # takže profily s iným rozsahom (napr. 15-90) dostávali defaulty 5-100.
    soc_min = float((plan.get("soc_min") if plan.get("soc_min") is not None
                     else plan.get("soc_min_pct")) or 5.0)
    soc_max = float((plan.get("soc_max") if plan.get("soc_max") is not None
                     else plan.get("soc_max_pct")) or 100.0)

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
    current_soc_source = "plán projekcia (DAM+VDT)"

    # Bug SOC-UNIFY (2026-06-13): kanonický „aktuálny SOC" = engine livesim trace
    # (plán + VDT + RT po clipe), NIE plán DAM+VDT projekcia. Tým majú audit, VDT
    # advisor, auto_control aj zobrazenie identický SOC (všetci volajú toto). Plán
    # soc_path ostáva pre forecast budúcich slotov + fallback keď dnešný trace ešte
    # nie je. Override len pre REÁLNY dnešok — historický backfill audit ostáva
    # nezmenený (číta plán projekciu ako doteraz, nulové riziko regresie).
    if today == dt.date.today():
        try:
            _realized = _get_current_soc_from_livesim_today(profile, today, now)
        except Exception:
            _realized = None
        if _realized is not None:
            current_soc = float(_realized["soc_pct"])
            current_soc_source = _realized["source"]
        else:
            # REAL-STATE fallback (user 2026-06-29: „obchodník berie SKUTOČNÝ stav"): keď
            # realizovaný SOC ešte nie je k dispozícii (skoro ráno, livesim dnes nebežal),
            # NEobchoduj na PLÁNOVEJ PROJEKCII (soc_path[cur_idx] predpokladá, že sa plán už
            # odohral — napr. nabíjanie → optimizer vidí fiktívny SOC → nepárové nákupy).
            # Použi POSLEDNÝ ZNÁMY REÁLNY = ŠTART dňa (carryover z reálneho konca N-1).
            current_soc = float(soc_path[0])
            current_soc_source = (f"štart dňa carryover {float(soc_path[0]):.1f}% "
                                  f"(realiz. trace dnes ešte nedostupný)")

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
            "current_soc_source": current_soc_source,
            "current_slot_idx": cur_idx,
            "batt_kwh": cap,
            "batt_kw": batt_kw,
            "eff_c": eff_c,
            "eff_d": eff_d,
            "soc_min_pct": soc_min,
            "soc_max_pct": soc_max}


# ────────────────────────── Bug V: efektívny batt setpoint ──────────────────────────

def get_realized_batt_kw(profile: str, today_iso: Optional[str] = None,
                          dt_h: float = 0.25) -> List[float]:
    """Vráti 96-slot list signed kW pre VDT realized trades.

    Konvencia (rovnaká ako plan_batt_kw v plan_store):
      • + kW = discharge (predaj zo batt)
      • − kW = charge (nákup do batt)

    Vstup: dt_h (default 0.25 = 15-min slot). Pre 60-min plán treba 1.0.

    Použitie:
      • auto_control._extract_batt_kw_for_slot pripočíta toto k D-1 batt_kw
      • livesim.advance píše plan_batt_kw = D-1 + VDT pre konzistenciu chartu
      • dashboard zobrazí efektívny batt setpoint (D-1 + VDT) namiesto čistého D-1

    Žiadny LP recalc — čisté čítanie z vdt_paper_trades.csv (persistované,
    nezáleží na otvorení dashboardu).
    """
    today_iso = today_iso or dt.date.today().isoformat()
    try:
        vdt = _load_vdt_realized(profile, today_iso)
        kwh_arr = vdt.get("kwh_batt_view") or [0.0] * 96
    except Exception:
        return [0.0] * 96
    dt_h = max(0.001, float(dt_h))
    return [float(k) / dt_h for k in kwh_arr]


# ────── #27: VDT podľa reálnych UZAVRETÝCH cien (LEN HISTÓRIA, vybraný rozsah) ──────
# Náhradné oceňovanie VDT pre HISTORICKÉ dni: namiesto živých paper trades sa VDT
# obchody nasimulujú LP optimizerom na REÁLNYCH OKTE VDT uzavretých cenách
# (vdt_arbitrage.build_backtest_snapshot → value per slot; use_orderbook=False =
# value pre nákup AJ predaj, bez spreadu = len cross-slot arbitráž). Vracia
# VDT-EXTRA (nad DAM nomináciu — zabráni dvojitému započítaniu DAM), rovnaký
# kontrakt ako get_realized_batt_kw / get_realized_prices_per_slot, aby livesim
# len prehodil zdroj. Dnešok/budúcnosť sa SEM nikdy nedostane (gate je v livesime:
# iba deň < dnešok a vo zvolenom rozsahu). Cache per (profil, deň, kľúčové parametre).
_CLOSEDPRICE_CACHE: Dict[str, Dict[str, Any]] = {}


def _compute_closedprice_day(profile: str, day_iso: str) -> Dict[str, Any]:
    import math as _m
    day_iso = str(day_iso)[:10]
    zeros = {"kwh_batt_view": [0.0] * 96, "prices_per_slot": [float("nan")] * 96,
             "total_eur": 0.0, "source": "closed: no-op"}
    try:
        # Profil parametre (rovnaké kľúče ako compute_current_state, market-agnostic)
        import profiles as _pr
        prof = _pr.load_profile(profile) or {}
        plan = prof.get("plan") or {}
        batt_kw = float(plan.get("batt_kw") or 100.0)
        batt_kwh = float(plan.get("batt_kwh") or 800.0)
        eff_c = float(plan.get("eff_c") or 0.95)
        eff_d = float(plan.get("eff_d") or 0.95)
        grid_fee = float(plan.get("grid_fee") or 0.0)
        cycle_cost = float(plan.get("cycle_cost") or 0.0)
        min_spread = float(plan.get("min_spread") if plan.get("min_spread") is not None
                           else (plan.get("min_spread_eur") or 5.0))
        soc_min = float((plan.get("soc_min") if plan.get("soc_min") is not None
                         else plan.get("soc_min_pct")) or 5.0)
        soc_max = float((plan.get("soc_max") if plan.get("soc_max") is not None
                         else plan.get("soc_max_pct")) or 100.0)
        max_cycles = plan.get("max_cycles_per_day")
        # CLOSED-PAIRS (2026-07-01, user: „párový matcher aj v simulácii"): historická VDT
        # simulácia použije PROFILOVÝ engine (rovnako ako živý advisor) — pairs = spárované
        # nákup↔predaj cykly, žiadne nepárové nákupy. Predtým sa tu volalo optimize_vdt_day
        # bez engine → default LP (nespárované). Zrkadlo vdt_live_advisor.py:581-586.
        _ck_engine = str(plan.get("vdt_engine", "lp") or "lp").lower()
        _ck_priority = str(plan.get("vdt_pair_priority", "closest") or "closest").lower()
        _ck_buyback = bool(plan.get("vdt_allow_buyback", False))
        _soc_init_ck = float(plan.get("soc_init_pct",
                             plan.get("soc_init", (soc_min + soc_max) / 2.0))
                             or (soc_min + soc_max) / 2.0)
        ck = (f"{profile}|{day_iso}|{batt_kwh:.0f}|{batt_kw:.0f}|{min_spread:.1f}"
              f"|{soc_min:.0f}|{soc_max:.0f}|{max_cycles}|si{_soc_init_ck:.1f}"
              f"|e{_ck_engine}|p{_ck_priority}|b{int(_ck_buyback)}")
        _c = _CLOSEDPRICE_CACHE.get(ck)
        if _c is not None:
            return _c

        import datetime as _dt2
        date = _dt2.date.fromisoformat(day_iso)
        import vdt_arbitrage as _arb
        import vdt_optimizer as _opt
        snap = _arb.build_backtest_snapshot(date)
        if snap is None or snap.empty or snap["price_eur"].notna().sum() == 0:
            _CLOSEDPRICE_CACHE[ck] = zeros
            return zeros

        # DAM nominácia pre daný deň (ak plán existuje) — batt view (+dis −chg) = lower bounds
        dam = _load_dam_nomination(profile, day_iso)
        dam_view = (dam.get("kwh_batt_view") if dam else None) or [0.0] * 96
        # Bug #27-SOC-BASE (2026-06-18, user VW_simulacia_3): štart SOC MUSÍ byť reálny
        # soc_init plánu, NIE stred rozsahu (~52 %). Pri 52 % báze optimizer našiel rannú
        # arbitráž (SOC až 100 % ráno), ktorá v DT pláne (soc_init 5 % → DAM ramp) NIE je →
        # closed-price VDT bol „feasible" len v 52 %-bubline; engine ho na reálnej
        # 5 %-trajektórii buď zahodil (zeros) alebo orezal → simulácia sa rozišla s plánom
        # (overené simuláciou: starý SOC −42 %, nový v pásme). Zo soc_init + DAM commitments
        # optimizer drží tú istú trajektóriu ako realita → VDT-extra dodateľný, simulácia ho sleduje.
        soc_start = float(plan.get("soc_init_pct",
                          plan.get("soc_init", (soc_min + soc_max) / 2.0))
                          or (soc_min + soc_max) / 2.0)
        res = _opt.optimize_vdt_day(
            snap, batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
            grid_fee=grid_fee, cycle_cost=cycle_cost, min_spread=min_spread,
            soc_min_pct=soc_min, soc_max_pct=soc_max, soc_start_pct=soc_start,
            soc_end_min_pct=soc_start,
            max_cycles_per_day=(float(max_cycles) if max_cycles else None),
            dam_commitments=dam_view, slot_minutes=15,
            use_orderbook=False, future_only=False,
            engine=_ck_engine, pair_priority=_ck_priority,
            allow_buyback=_ck_buyback,
        )
        if not res.get("ok"):
            _CLOSEDPRICE_CACHE[ck] = zeros
            return zeros

        # closed ceny per slot_idx
        price_by_idx: Dict[int, float] = {}
        try:
            for _i in range(len(snap)):
                _r = snap.iloc[_i]
                _pv = _r.get("price_eur")
                price_by_idx[int(_r.get("slot_idx"))] = (float(_pv) if _pv is not None
                                                          else float("nan"))
        except Exception:
            pass

        batt_view = [0.0] * 96
        for tr in res.get("trades", []):
            i = int(tr.get("slot_idx", -1))
            if 0 <= i < 96:
                batt_view[i] = (float(tr.get("discharge_kwh", 0) or 0)
                                - float(tr.get("charge_kwh", 0) or 0))
        # VDT-EXTRA = trade nad DAM nomináciu (KRITICKÉ: zabráni dvojitému započítaniu DAM)
        vdt_only = [batt_view[i] - float(dam_view[i] if i < len(dam_view) else 0.0)
                    for i in range(96)]
        prices = [float("nan")] * 96
        total_eur = 0.0
        for i in range(96):
            if abs(vdt_only[i]) > 0.01:
                _p = price_by_idx.get(i, float("nan"))
                prices[i] = _p
                if _m.isfinite(_p):
                    total_eur += vdt_only[i] * _p / 1000.0
        out = {"kwh_batt_view": vdt_only, "prices_per_slot": prices,
               "total_eur": total_eur,
               "source": f"closed OKTE VDT (profile={profile}, day={day_iso})"}
        _CLOSEDPRICE_CACHE[ck] = out
        return out
    except Exception:
        return zeros


def get_closedprice_batt_kw(profile: str, today_iso: Optional[str] = None,
                            dt_h: float = 0.25) -> List[float]:
    """#27: 96-slot signed kW pre VDT ocenené reálnymi UZAVRETÝMI cenami (len história).
    Rovnaký kontrakt ako get_realized_batt_kw (+ discharge / − charge)."""
    today_iso = today_iso or dt.date.today().isoformat()
    try:
        c = _compute_closedprice_day(profile, today_iso)
        kwh_arr = c.get("kwh_batt_view") or [0.0] * 96
    except Exception:
        return [0.0] * 96
    dt_h = max(0.001, float(dt_h))
    return [float(k) / dt_h for k in kwh_arr]


def get_closedprice_prices_per_slot(profile: str, today_iso: str) -> list:
    """#27: 96-slot ceny (€/MWh) VDT-extra obchodov za reálne uzavreté ceny, inak NaN."""
    try:
        c = _compute_closedprice_day(profile, str(today_iso)[:10])
        return c.get("prices_per_slot") or [float("nan")] * 96
    except Exception:
        return [float("nan")] * 96


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
