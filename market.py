# -*- coding: utf-8 -*-
"""
market.py — výber trhu (Česko / Slovensko) pre celú appku.

Princíp
-------
Aplikácia podporuje dva oddelené trhy:
  - **cz** (Česko) — OTE-CZ day-ahead, ČEPS imbalance signál
  - **sk** (Slovensko) — OKTE-SK day-ahead, SEPS imbalance signál

Každý trh má vlastný dátový strom `out/<market>/...` — plány, livesim, ceny, scenáre.
Profile (per-zákazník) sú vnútri trhu (`out/cz/profiles/`, `out/sk/profiles/`).

Aktívny trh je per-port (rovnaký pattern ako profiles), aby každá inštancia mohla
bežať na inom trhu súbežne (port 8000 = CZ, port 8001 = SK napríklad).

Storage
-------
  out/_active_market.json           — pre PORT 8000 (default)
  out/_active_market_<port>.json    — pre iné porty

Funkcie
-------
  get_active_market() -> 'cz' | 'sk'    : aktívny trh pre tento process
  set_active_market(name)               : prepne (perzistuje na disk)
  data_dir(market=None) -> str          : "out/cz" alebo "out/sk"
  list_markets() -> list[dict]          : zoznam podporovaných (s ikonkami)
  ensure_dirs(market=None)              : vytvori out/<market>/ + podpriečinky
"""
from __future__ import annotations
import os, json
from datetime import datetime
from typing import Optional, List, Dict

# trigger legacy → multi-market migráciu (idempotent, no-op po prvom behu)
try:
    import market_migrate as _mm
    _mm.ensure_migrated()
except Exception:
    pass


SUPPORTED = [
    {"code": "cz", "label": "Česko", "flag": "🇨🇿"},
    {"code": "sk", "label": "Slovensko", "flag": "🇸🇰"},
]
DEFAULT = "cz"

# Fallback chain: PORT → APP_PORT → "8000". start_dev.sh nastavuje APP_PORT,
# nie PORT — bez tohto fallbacku by všetky inštancie čítali rovnaký súbor.
_PORT = os.environ.get("PORT") or os.environ.get("APP_PORT") or "8000"
ACTIVE_PATH = ("out/_active_market.json" if _PORT == "8000"
               else f"out/_active_market_{_PORT}.json")


def _ensure_out() -> None:
    os.makedirs("out", exist_ok=True)


def list_markets() -> List[Dict[str, str]]:
    return list(SUPPORTED)


def is_valid(name: str) -> bool:
    return any(m["code"] == name for m in SUPPORTED)


def get_active_market() -> str:
    """Aktívny trh pre tento process (default 'cz')."""
    if not os.path.exists(ACTIVE_PATH):
        return DEFAULT
    try:
        with open(ACTIVE_PATH) as fh:
            d = json.load(fh)
        n = d.get("market")
        if n and is_valid(n):
            return n
    except (OSError, json.JSONDecodeError):
        pass
    return DEFAULT


def set_active_market(name: str) -> None:
    """Prepne aktívny trh. Nehlási chybu — neplatný kod fallbackne na DEFAULT."""
    _ensure_out()
    n = name if is_valid(name) else DEFAULT
    with open(ACTIVE_PATH, "w") as fh:
        json.dump({"market": n, "set_at": datetime.now().isoformat(timespec="seconds")}, fh)


def data_dir(market: Optional[str] = None) -> str:
    """Vráti koreňový dátový priečinok pre daný (alebo aktívny) trh.
    Napr. 'out/cz' alebo 'out/sk'.
    """
    m = market if (market and is_valid(market)) else get_active_market()
    return os.path.join("out", m)


def ensure_dirs(market: Optional[str] = None) -> None:
    """Vytvori out/<market>/ + štandardné podpriečinky ak chýbajú."""
    base = data_dir(market)
    os.makedirs(base, exist_ok=True)
    for sub in ("plans", "plan_overrides", "profiles", "load_profile",
                 "ftv_scenarios", "cases"):
        os.makedirs(os.path.join(base, sub), exist_ok=True)


def label_for(market: Optional[str] = None) -> str:
    """Human label '🇨🇿 Česko' pre badge / header."""
    m = market or get_active_market()
    for item in SUPPORTED:
        if item["code"] == m:
            return f"{item['flag']} {item['label']}"
    return f"❓ {m}"


def fetch_dam(date, market: Optional[str] = None):
    """Univerzálny fetch DAM (day-ahead) clearing cien pre aktívny trh.

    Args:
        date: datetime.date pre ktorý deň fetchnúť ceny.
        market: 'cz' / 'sk' / None (= aktívny trh).

    Returns:
        pandas.DataFrame s 96 (15-min) alebo 24 (hodinovými) riadkami:
            - cena_EUR: clearing cena €/MWh
            - period: 1..N (1-based perióda)
            - interval: "HH:MM-HH:MM" (lokálny SK/CZ čas)
            - date: ISO date string
        Pre SK trh ide cez okte_sk.fetch_okte_dayahead (96 × 15-min).
        Pre CZ trh ide cez data_sources.fetch_ote_dayahead (24 × hod, fallback).
        Vyhodí RuntimeError ak DAM ešte nebol publikovaný (D-1 ~12:00).
    """
    m = market if (market and is_valid(market)) else get_active_market()
    if m == "sk":
        # Najprv skús live API
        try:
            import okte_sk as _ok
            df = _ok.fetch_okte_dayahead(date)
            if df is not None and not df.empty:
                return df
        except Exception as e_live:
            _live_err = str(e_live)
        else:
            _live_err = "prázdny response"
        # Fallback: SK historian CSV (z internal historian — C_WEB_OKTE_ISOT_15m)
        try:
            import seps_sk as _seps
            import pandas as _pd
            csv_path = _seps._historian_okte_dam_csv()
            import os as _os
            if _os.path.exists(csv_path):
                dam_map = _seps._load_okte_15min_from_csv(csv_path, date.isoformat())
                if dam_map:
                    rows = []
                    for ts_str, price in sorted(dam_map.items()):
                        # ts_str = "YYYY-MM-DD HH:MM:SS"
                        ts = _pd.Timestamp(ts_str)
                        period = (ts.hour * 60 + ts.minute) // 15 + 1   # 1-based
                        end = ts + _pd.Timedelta(minutes=15)
                        rows.append({
                            "date": date.isoformat(),
                            "interval": f"{ts.strftime('%H:%M')}-{end.strftime('%H:%M')}",
                            "cena_EUR": float(price),
                            "period": period,
                            "deliveryStart": ts.isoformat(),
                            "deliveryEnd": end.isoformat(),
                            "publicationStatus": "historian",
                            "objem_MWh_buy": 0.0, "objem_MWh_sell": 0.0,
                            "priceCz": None, "priceHu": None,
                        })
                    if rows:
                        return _pd.DataFrame(rows).sort_values("period").reset_index(drop=True)
        except Exception:
            pass
        raise RuntimeError(f"SK DAM nedostupné (live API: {_live_err[:120]}, historian CSV tiež)")
    # CZ — použiť existujúci data_sources/OTE-CZ fetcher
    try:
        import data_sources as _ds
        return _ds.fetch_ote_dayahead(date)
    except (ImportError, AttributeError):
        raise RuntimeError(f"CZ DAM fetcher nedostupný (data_sources.fetch_ote_dayahead chýba)")
