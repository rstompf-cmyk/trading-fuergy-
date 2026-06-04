# -*- coding: utf-8 -*-
"""
load_profile.py — spotreba zákazníka (load) per-profil.

Funguje analogicky k FTV scenárom + plan_overrides:
- Import 15-min CSV (timestamp + kW) z meraku/distribútora pre nejaké obdobie.
- Z importovaných dní sa vyrobí PRIEMERNÝ DENNÝ PROFIL zvlášť pre weekday a víkend (96 × kW).
- Pre ľubovoľný budúci/historický dátum vieme vrátiť 96 hodnôt podľa typu dňa.
- Per-profil úložisko: `out/load_profile/<profil>/profile.json` (legacy default profil = priamo out/load_profile/profile.json).

CSV formát:
- Auto-detekcia: oddeľovač (,;\t), desatinný (./,), timestamp formát.
- Stĺpce: predpokladáme TIMESTAMP + kW.
- Timestamp môže byť: '2026-05-01 12:30:00', '2026-05-01 12:30', '01.05.2026 12:30', '2026-05-01T12:30',
  alebo OTE-štýl '2026-05-01' + 'interval' v ďalšom stĺpci. Detekujeme.
- kW = priemerný výkon v 15-min intervale.

Funkcie:
  import_csv(path, profile=None) → dict so štatistikami importu
  load_for_date(date_iso, profile=None) → np.ndarray dĺžky 96 (kW pre 15-min sloty)
  has_data(profile=None) → bool
  get_meta(profile=None) → dict s metadata (počet dní, dátumy, profily WD/WE)
  clear(profile=None) → zmaže profil
"""
from __future__ import annotations
import os, json
from datetime import datetime
from typing import Optional, Dict, Any, List
import numpy as np
import pandas as pd


DEFAULT_PROFILE = "default"
N96 = 96


def _root() -> str:
    env = os.environ.get("LOAD_PROFILE_DIR")
    if env:
        return env
    try:
        import market as _mk
        return os.path.join(_mk.data_dir(), "load_profile")
    except Exception:
        return "out/cz/load_profile"


DIR = _root()                                                        # back-compat const


def _resolve_profile(profile: Optional[str] = None) -> str:
    """Aktívny profil (rovnaká logika ako plan_store/plan_overrides)."""
    if profile:
        return profile
    try:
        import plan_store as _ps
        return _ps.resolve_profile()
    except Exception:
        return DEFAULT_PROFILE


def _dir_for(profile: Optional[str] = None) -> str:
    p = _resolve_profile(profile)
    DIR_LOCAL = _root()
    return DIR_LOCAL if p == DEFAULT_PROFILE else os.path.join(DIR_LOCAL, p)


def _profile_path(profile: Optional[str] = None) -> str:
    return os.path.join(_dir_for(profile), "profile.json")


def _ensure_dir(profile: Optional[str] = None) -> None:
    os.makedirs(_dir_for(profile), exist_ok=True)


# ─── CSV parser ─────────────────────────────────────────────────────────────

def _read_csv_robust(path: str) -> pd.DataFrame:
    """Načíta CSV s auto-detekciou oddeľovača (',', ';', '\\t') a desatinného znaku ('.' alebo ',').
    Skúsi všetky kombinácie a vyberie tú, ktorá produkuje NAJVIAC numerických stĺpcov
    (= väčšina dát úspešne sparsovaná ako čísla, nie ako string s desatinnou čiarkou).
    Vracia DataFrame s minimálne 2 stĺpcami."""
    best = None
    best_score = -1
    last_err = None
    for sep in [";", ",", "\t", "|"]:
        for dec in [".", ","]:
            if sep == "," and dec == ",":
                continue                              # konflikt: oddeľovač = desatinné
            try:
                df = pd.read_csv(path, sep=sep, decimal=dec, header=0, engine="python")
                if df.shape[1] < 2 or len(df) < 4:
                    continue
                # score = počet numerických stĺpcov (kde >=50 % riadkov je číslo)
                n_numeric = 0
                for c in df.columns:
                    vals = pd.to_numeric(df[c], errors="coerce").dropna()
                    if len(vals) >= len(df) * 0.5:
                        n_numeric += 1
                if n_numeric > best_score:
                    best_score = n_numeric
                    best = df
            except Exception as e:
                last_err = e
                continue
    if best is None:
        raise ValueError(f"Nepodarilo sa parsovať CSV: {last_err}")
    return best


def _parse_timestamp(series: pd.Series) -> pd.Series:
    """Konverzia stĺpca na datetime — preferuje ISO, potom európsky DD.MM.YYYY (CZ/SK)."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    s = series.astype(str).str.strip()
    # skús ISO 8601 (2026-05-01 12:30) — má prednosť ak hodnoty začínajú rokom
    sample = s.iloc[0] if len(s) else ""
    iso_like = bool(sample) and sample[:4].isdigit() and (len(sample) >= 4 and sample[4] in "-/")
    if iso_like:
        try:
            return pd.to_datetime(s, format="ISO8601", errors="raise")
        except Exception:
            pass
        try:
            return pd.to_datetime(s, errors="raise")
        except Exception:
            pass
    # európsky formát DD.MM.YYYY HH:MM — má prednosť pred US ak začína dňom
    try:
        return pd.to_datetime(s, dayfirst=True, errors="raise")
    except Exception:
        pass
    # fallback auto
    try:
        return pd.to_datetime(s, errors="raise")
    except Exception:
        pass
    raise ValueError(f"Nepodarilo sa rozpoznať timestamp v stĺpci. Príklad hodnoty: {sample!r}")


def _detect_columns(df: pd.DataFrame) -> tuple[str, str]:
    """Vráti (timestamp_col, kw_col). Heuristika:
    - ts_col: prvý stĺpec ktorý sa dá rozparsovať ako datetime
    - kw_col: numerický stĺpec ktorý NIE JE monotónny (= index/meter), s preferenciou pre stĺpec
      ktorého meno obsahuje 'kw', 'value', 'power', 'load', 'spotreba'."""
    cols = list(df.columns)
    # ── timestamp column ──
    ts_col = None
    for c in cols:
        s = df[c].astype(str).head(20)
        if s.str.contains(r"\d{4}|\d{1,2}:\d{2}", regex=True).any():
            try:
                _parse_timestamp(df[c].head(5))
                ts_col = c
                break
            except Exception:
                continue
    if ts_col is None:
        ts_col = cols[0]
    # ── kW column ──
    # zber všetkých numerických stĺpcov + zaznač monotónne (= index/meter) — tie odstavíme
    candidates = []
    for c in cols:
        if c == ts_col:
            continue
        try:
            vals = pd.to_numeric(df[c], errors="coerce").dropna()
            if len(vals) < len(df) * 0.5:
                continue
            arr = vals.values
            is_monotonic = False
            if len(arr) >= 10:
                diffs = np.diff(arr)
                pos_frac = float((diffs > 0).mean())
                # ≥95 % rastúcich diferenciácií = striktne stúpajúci stĺpec → index/meter reading
                is_monotonic = (pos_frac >= 0.95)
            candidates.append({"col": c, "std": float(vals.std()), "monotonic": is_monotonic,
                                "name_score": _name_match_kw(c)})
        except Exception:
            continue
    if not candidates:
        raise ValueError("Nenašiel som numerický stĺpec s kW hodnotami.")
    # 1) preferuj non-monotonic + meno match (kw/value/load/...)
    non_mono = [c for c in candidates if not c["monotonic"]]
    if non_mono:
        # vyber ten s najvyšším name_score; tie-break: najvyššia std
        non_mono.sort(key=lambda x: (-x["name_score"], -x["std"]))
        return ts_col, non_mono[0]["col"]
    # 2) ak všetko monotonic, fallback: vyber stĺpec s najvyššou std (= asi meter reading,
    #    aspoň upozorníme používateľa)
    candidates.sort(key=lambda x: -x["std"])
    return ts_col, candidates[0]["col"]


def _name_match_kw(name: str) -> int:
    """Skóre podľa toho ako meno stĺpca pripomína 'spotreba/kW': 0 = nič, 3 = silná zhoda."""
    n = str(name).lower().strip()
    if n in ("kw", "kwh", "value", "spotreba", "load", "power", "consumption"):
        return 3
    for needle in ("kw", "value", "load", "power", "spotreba", "consumption"):
        if needle in n:
            return 2
    if n in ("p", "p1", "p_avg"):
        return 1
    return 0


def import_csv(path: str, profile: Optional[str] = None,
                replace: bool = True, unit: str = "kW") -> Dict[str, Any]:
    """Naimportuje CSV spotreby do profile-priečinka.
    `replace=True` → nahradí celé úložisko; False → pridá k existujúcim dňom (override pri kolízii).
    `unit`: jednotka v ktorej sú hodnoty v CSV:
        - 'kW' (default): okamžitý výkon v kW, žiadna konverzia
        - 'W': okamžitý výkon vo wattoch → vydelíme 1000 na kW
        - 'kWh_15min': energia v kWh za 15-min interval → vynásobíme 4 na priemer kW (4 × 15 min = 1 h)
        - 'Wh_15min': energia vo wattoch-hodinách za 15-min interval → /1000 × 4 = /250

    Vracia dict so štatistikami: počet dní WD/WE, range dátumov, priemer kW.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"CSV súbor neexistuje: {path}")
    df = _read_csv_robust(path)
    ts_col, kw_col = _detect_columns(df)
    df = df[[ts_col, kw_col]].dropna()
    df[ts_col] = _parse_timestamp(df[ts_col])
    df[kw_col] = pd.to_numeric(df[kw_col], errors="coerce")
    df = df.dropna()
    if df.empty:
        raise ValueError("Po parsovaní CSV nezostali žiadne platné riadky.")
    # ── konverzia jednotiek na kW ──
    unit_factor = {
        "kW": 1.0,
        "W": 1.0 / 1000.0,
        "kWh_15min": 4.0,                # kWh za 15min × 4 = priemerný výkon kW (15min × 4 = 1h)
        "Wh_15min": 4.0 / 1000.0,        # Wh za 15min → W → kW
    }
    if unit not in unit_factor:
        raise ValueError(f"Neznáma jednotka '{unit}'. Povolené: {list(unit_factor)}")
    if unit_factor[unit] != 1.0:
        df[kw_col] = df[kw_col] * unit_factor[unit]
    # ── sanity check rozsahu hodnôt ──
    _vals = df[kw_col].values
    _vmax, _vmin, _vmean = float(np.nanmax(_vals)), float(np.nanmin(_vals)), float(np.nanmean(_vals))
    _warning = None
    if _vmax > 100000 or _vmin < -1000:
        _warning = (f"Hodnoty vyzerajú nereálne (min={_vmin:.1f}, max={_vmax:.1f}, mean={_vmean:.1f} kW). "
                    f"Možno CSV obsahuje kumulatívny meter reading namiesto okamžitého výkonu, "
                    f"alebo som detegoval zlý stĺpec (detegovaný: '{kw_col}').")
    elif _vmax < 0.001 and _vmin >= 0:
        _warning = "Všetky hodnoty sú 0 — žiadna spotreba?"
    # zoradíme + zaokrúhlime na 15-min sloty
    df = df.sort_values(ts_col).reset_index(drop=True)
    df["ts15"] = df[ts_col].dt.floor("15min")
    # agreguj na 15-min priemer (pre prípad jemnejších dát)
    g = df.groupby("ts15")[kw_col].mean().reset_index()
    g.columns = ["ts15", "kw"]
    g["date"] = g["ts15"].dt.date
    g["slot"] = (g["ts15"].dt.hour * 4 + g["ts15"].dt.minute // 15).astype(int)
    # vyrobíme per-day 96-bodový profile
    daily: Dict[str, np.ndarray] = {}
    for d, grp in g.groupby("date"):
        arr = np.full(N96, np.nan, dtype=float)
        for _, r in grp.iterrows():
            i = int(r["slot"])
            if 0 <= i < N96:
                arr[i] = float(r["kw"])
        # vyplníme krátke medzery linear, dlhšie ostávajú NaN
        s = pd.Series(arr)
        arr = s.interpolate(limit=4).bfill().ffill().to_numpy()
        if np.all(np.isfinite(arr)):
            daily[str(d)] = arr
    if not daily:
        raise ValueError("Žiadny celý deň po agregácii. Skontroluj CSV (asi má príliš veľké medzery).")

    # ── výpočet weekday a weekend profilov (priemer cez dni rovnakého typu) ──
    wd_arrs, we_arrs = [], []
    for date_iso, arr in daily.items():
        wd = datetime.fromisoformat(date_iso).weekday()        # 0..6, 5+6 = víkend
        (we_arrs if wd >= 5 else wd_arrs).append(arr)
    wd_profile = np.nanmean(np.stack(wd_arrs, axis=0), axis=0).tolist() if wd_arrs else None
    we_profile = np.nanmean(np.stack(we_arrs, axis=0), axis=0).tolist() if we_arrs else None

    _ensure_dir(profile)
    path_out = _profile_path(profile)
    existing = {}
    if os.path.exists(path_out) and not replace:
        try:
            with open(path_out) as fh:
                existing = json.load(fh)
        except Exception:
            existing = {}
    # merge dní (nový override existujúce)
    merged_daily: Dict[str, list] = dict(existing.get("daily_kw", {}))
    for d, arr in daily.items():
        merged_daily[d] = [float(x) for x in arr]
    # re-spočítaj WD/WE z merged
    wd_arrs2, we_arrs2 = [], []
    for date_iso, arr in merged_daily.items():
        wd = datetime.fromisoformat(date_iso).weekday()
        (we_arrs2 if wd >= 5 else wd_arrs2).append(np.asarray(arr, float))
    wd_final = np.nanmean(np.stack(wd_arrs2, axis=0), axis=0).tolist() if wd_arrs2 else None
    we_final = np.nanmean(np.stack(we_arrs2, axis=0), axis=0).tolist() if we_arrs2 else None

    body = {
        "profile": _resolve_profile(profile),
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "source_file": os.path.basename(path),
        "source_unit": str(unit),
        "detected_ts_col": str(ts_col),
        "detected_kw_col": str(kw_col),
        "daily_kw": merged_daily,                                  # per-day 96-bodové dáta
        "weekday_profile_kw": wd_final,                            # 96 × kW (priemer Po-Pia)
        "weekend_profile_kw": we_final,                            # 96 × kW (priemer So+Ne)
        "n_days_wd": len(wd_arrs2),
        "n_days_we": len(we_arrs2),
        "n_days_total": len(merged_daily),
        "date_min": min(merged_daily.keys()) if merged_daily else None,
        "date_max": max(merged_daily.keys()) if merged_daily else None,
        "kwh_per_day_avg": float(np.mean([np.sum(arr) * 0.25 for arr in merged_daily.values()])) if merged_daily else None,
        "value_min_kw": _vmin,
        "value_max_kw": _vmax,
        "value_mean_kw": _vmean,
        "warning": _warning,
    }
    with open(path_out, "w") as fh:
        json.dump(body, fh, ensure_ascii=False, indent=1)
    return {"path": path_out, **{k: v for k, v in body.items() if k not in ("daily_kw",)}}


# ─── retrieve ────────────────────────────────────────────────────────────────

def _load_raw(profile: Optional[str] = None) -> Optional[Dict[str, Any]]:
    p = _profile_path(profile)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as fh:
            return json.load(fh)
    except Exception:
        return None


def has_data(profile: Optional[str] = None) -> bool:
    """True ak profil má aspoň jeden weekday alebo weekend profile."""
    d = _load_raw(profile)
    if not d:
        return False
    return bool(d.get("weekday_profile_kw") or d.get("weekend_profile_kw"))


def load_for_date(date_iso: str, profile: Optional[str] = None) -> np.ndarray:
    """Vráti 96 × kW pre daný dátum (podľa weekday/weekend pattern).
    Ak existuje exact-match v daily_kw, vráti ten. Inak fallback na typ dňa.
    Ak žiadne dáta → vráti pole núl (no load)."""
    d = _load_raw(profile)
    if not d:
        return np.zeros(N96, dtype=float)
    # 1) exact match
    daily = d.get("daily_kw", {}) or {}
    if date_iso in daily and len(daily[date_iso]) == N96:
        return np.asarray(daily[date_iso], dtype=float)
    # 2) weekday vs weekend
    try:
        wd = datetime.fromisoformat(date_iso).weekday()
    except Exception:
        wd = 0
    if wd >= 5 and d.get("weekend_profile_kw"):
        return np.asarray(d["weekend_profile_kw"], dtype=float)
    if d.get("weekday_profile_kw"):
        return np.asarray(d["weekday_profile_kw"], dtype=float)
    # 3) fallback na akýkoľvek profile čo existuje
    if d.get("weekend_profile_kw"):
        return np.asarray(d["weekend_profile_kw"], dtype=float)
    return np.zeros(N96, dtype=float)


def get_meta(profile: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Vráti dict s metadata (bez daily_kw kvôli veľkosti)."""
    d = _load_raw(profile)
    if not d:
        return None
    return {k: v for k, v in d.items() if k != "daily_kw"}


def rescale(factor: float, profile: Optional[str] = None) -> bool:
    """Prenásobí všetky uložené hodnoty (daily_kw + weekday_profile_kw + weekend_profile_kw)
    konštantou. Použité na konverziu jednotiek dodatočne (napr. W → kW: factor=0.001).
    Vracia True ak úspešne, False ak žiadny profil neexistuje."""
    p = _profile_path(profile)
    if not os.path.exists(p):
        return False
    try:
        with open(p) as fh:
            d = json.load(fh)
    except Exception:
        return False
    f = float(factor)
    # daily_kw: dict[date] → list of 96 floats
    if d.get("daily_kw"):
        d["daily_kw"] = {k: [x * f for x in v] for k, v in d["daily_kw"].items()}
    for key in ("weekday_profile_kw", "weekend_profile_kw"):
        if d.get(key):
            d[key] = [x * f for x in d[key]]
    if d.get("kwh_per_day_avg") is not None:
        d["kwh_per_day_avg"] = float(d["kwh_per_day_avg"]) * f
    for key in ("value_min_kw", "value_max_kw", "value_mean_kw"):
        if d.get(key) is not None:
            d[key] = float(d[key]) * f
    d["rescaled_at"] = datetime.now().isoformat(timespec="seconds")
    d["rescale_factor"] = float(d.get("rescale_factor", 1.0)) * f
    with open(p, "w") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
    return True


def clear(profile: Optional[str] = None) -> bool:
    """Zmaže profile.json. True ak existoval."""
    p = _profile_path(profile)
    if not os.path.exists(p):
        return False
    try:
        os.remove(p)
        return True
    except OSError:
        return False
