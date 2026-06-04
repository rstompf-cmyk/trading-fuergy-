# -*- coding: utf-8 -*-
"""
okte_sk.py — fetcher OKTE/ISOT API pre slovenský trh (DAM, IDM, IDA + imbalance).

Endpoints (verifikované z https://www.okte.sk/en/api-documentation/)
--------------------------------------------------------------------
  Day-ahead market (DAM) — 15-min od MTU projektu (po 2024):
    GET https://isot.okte.sk/api/v1/dam/results
      ?deliveryDayFrom=YYYY-MM-DD&deliveryDayTo=YYYY-MM-DD
    Vracia: TOP-LEVEL ARRAY objektov s period, deliveryStart/End (UTC), price (EUR/MWh),
    purchaseSuccessfulVolume, saleSuccessfulVolume, priceCz/Hu/Ro, ATCs, flows, publicationStatus.

  Intraday continuous (IDM):
    GET https://isot.okte.sk/api/v1/idm/results
      ?deliveryDayFrom=...&deliveryDayTo=...
    Vol-weighted avg cena + min/max za 15-min slot (len obchodované periódy).

  Intraday auctions (IDA1/IDA2/IDA3):
    GET https://isot.okte.sk/api/v1/ida/results

  Imbalance settlement (zúčtovanie odchýlok) — host iszo.okte.sk:
    GET https://iszo.okte.sk/api/v1/SystemImbalance
      ?dateFrom=YYYY-MM-DD&dateTo=YYYY-MM-DD&evaluationType=preliminarydaily
    evaluationType enum: 'preliminarydaily' (D+1 ~11:30), 'regulardaily', 'decadal', 'monthly', 'final'.
    Response = TOP-LEVEL ARRAY denných objektov, každý má pole `periods` (96 × 15-min).

Auth: žiadny (verejné public REST endpointy pre published-information).
Bez paginácie — celý rozsah dní vráti v jednom array.
"""
from __future__ import annotations
import datetime as dt
import os
import time
import requests
import pandas as pd
import numpy as np
from typing import Optional, List


ISOT_BASE = "https://isot.okte.sk/api/v1"
ISZO_BASE = "https://iszo.okte.sk/api/v1"
# Browser-like UA — OKTE WAF zhadzuje requesty bez bežného UA (vracia 503 + HTML).
_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.okte.sk/",
    "Origin": "https://www.okte.sk",
    "Connection": "keep-alive",
}
_TIMEOUT_S = 30


def _get_json(url: str, params: dict = None, retries: int = 3, backoff: float = 1.5):
    """GET JSON s retry. Hodí RuntimeError pri opakovanom zlyhaní."""
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params or {},
                              headers=_DEFAULT_HEADERS,
                              timeout=_TIMEOUT_S)
            if r.status_code == 200:
                return r.json()
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            # 503/504/429 → retry s dlhším čakaním
            if r.status_code in (429, 502, 503, 504) and i < retries - 1:
                time.sleep(backoff * (2 ** i))
                continue
            # 4xx (okrem 429) → zbytočné skúšať znova
            if 400 <= r.status_code < 500 and r.status_code != 429:
                break
        except Exception as e:
            last_err = str(e)
        if i < retries - 1:
            time.sleep(backoff * (i + 1))
    raise RuntimeError(f"OKTE API zlyhalo po {retries} pokusoch: {last_err}")


def _interval_from_utc(start_iso: str, end_iso: str) -> str:
    """'2025-04-23T22:00:00Z' + '2025-04-23T22:15:00Z' → '00:00-00:15' (v SK lokálnom čase)."""
    # UTC → Europe/Bratislava
    s = pd.Timestamp(start_iso).tz_convert("Europe/Bratislava")
    e = pd.Timestamp(end_iso).tz_convert("Europe/Bratislava")
    return f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}"


def _utc_to_sk_date(start_iso: str) -> str:
    """Lokálny SK dátum pre začiatok periódy."""
    return pd.Timestamp(start_iso).tz_convert("Europe/Bratislava").strftime("%Y-%m-%d")


# ─── DAM — day-ahead market ─────────────────────────────────────────────

def fetch_okte_dayahead(date: dt.date) -> pd.DataFrame:
    """15-min (alebo hodinové pre legacy obdobie) ceny SK day-ahead trhu [EUR/MWh].
    Vracia: date, interval, cena_EUR, period, deliveryStart, deliveryEnd, publicationStatus,
    objem_MWh_buy, objem_MWh_sell, priceCz, priceHu (pre cross-market porovnanie).
    """
    url = f"{ISOT_BASE}/dam/results"
    params = {"deliveryDayFrom": date.isoformat(), "deliveryDayTo": date.isoformat()}
    data = _get_json(url, params)
    if not isinstance(data, list) or len(data) == 0:
        raise RuntimeError(f"OKTE DAM {date}: prázdna alebo neplatná odpoveď")
    rows = []
    for it in data:
        ds = it.get("deliveryStart"); de = it.get("deliveryEnd")
        if not ds or not de:
            continue
        rows.append({
            "date": _utc_to_sk_date(ds),                                     # SK kalendárny deň
            "interval": _interval_from_utc(ds, de),
            "cena_EUR": float(it.get("price") or 0.0),
            "period": int(it.get("period") or 0),
            "deliveryStart": ds, "deliveryEnd": de,
            "publicationStatus": it.get("publicationStatus", ""),
            "objem_MWh_buy": float(it.get("purchaseSuccessfulVolume") or 0.0),
            "objem_MWh_sell": float(it.get("saleSuccessfulVolume") or 0.0),
            "priceCz": it.get("priceCz"),                                     # null ak coupling neaktívny
            "priceHu": it.get("priceHu"),
        })
    if not rows:
        raise RuntimeError(f"OKTE DAM {date}: žiadne periódy v odpovedi")
    df = pd.DataFrame(rows).sort_values("period").reset_index(drop=True)
    return df


# ─── IDM — intraday continuous market ──────────────────────────────────

def fetch_okte_intraday(date: dt.date) -> pd.DataFrame:
    """15-min agregované IDM ceny pre SK [EUR/MWh]. Vol-weighted avg + min/max + objem.
    Vracia: date, interval, cena_EUR, cena_min, cena_max, objem_MWh, deliveryStart/End.
    Iba pre obchodované periódy (môže byť menej než 96 riadkov).
    """
    url = f"{ISOT_BASE}/idm/results"
    params = {"deliveryDayFrom": date.isoformat(), "deliveryDayTo": date.isoformat()}
    try:
        data = _get_json(url, params)
    except RuntimeError:
        return pd.DataFrame(columns=["date", "interval", "cena_EUR", "cena_min", "cena_max", "objem_MWh"])
    if not isinstance(data, list):
        return pd.DataFrame(columns=["date", "interval", "cena_EUR", "cena_min", "cena_max", "objem_MWh"])
    rows = []
    for it in data:
        ds = it.get("deliveryStart"); de = it.get("deliveryEnd")
        if not ds or not de:
            continue
        # OKTE IDM má rôzne názvy podľa endpoint variantu — skúsime všetky
        price = (it.get("weightedAveragePrice")
                 or it.get("averagePrice")
                 or it.get("price"))
        if price is None:
            continue
        rows.append({
            "date": _utc_to_sk_date(ds),
            "interval": _interval_from_utc(ds, de),
            "cena_EUR": float(price),
            "cena_min": float(it.get("minPrice") or price),
            "cena_max": float(it.get("maxPrice") or price),
            "objem_MWh": float(it.get("totalVolume") or it.get("volume") or 0.0),
            "deliveryStart": ds, "deliveryEnd": de,
            "period": int(it.get("period") or 0),
        })
    if not rows:
        return pd.DataFrame(columns=["date", "interval", "cena_EUR", "cena_min", "cena_max", "objem_MWh"])
    df = pd.DataFrame(rows).sort_values("period").reset_index(drop=True)
    return df


# ─── Imbalance settlement (zúčtovanie odchýlok) ─────────────────────────

def fetch_okte_imbalance(date_from: dt.date, date_to: Optional[dt.date] = None,
                          evaluation_type: str = "preliminarydaily") -> pd.DataFrame:
    """SK imbalance settlement (ISZO) — 15-min ZCO + systémová odchýlka.

    Parametre
    ---------
    date_from, date_to : dátum od/do (ak date_to=None → 1 deň)
    evaluation_type : 'preliminarydaily' (default, D+1 ~11:30), 'regulardaily',
                       'decadal', 'monthly', 'final' (final ~2-3 mesiace po settlemente)

    Vracia DataFrame s riadkami per 15-min slot:
      date, period, interval (HH:MM-HH:MM, SK čas),
      isp (cena ZCO €/MWh — system imbalance price),
      srec (cena/koeficient regulačnej elektriny €/MWh),
      mppre (max cena ZRE+ €/MWh), mpnre (max cena ZRE− €/MWh),
      si (systémová odchýlka MWh, + = nadbytok, − = nedostatok),
      pi (kladná zložka MWh), ni (záporná zložka MWh, negatívna),
      pre, nre (kladná/záporná regulačná elektrina MWh),
      pspre, psnre (peniaze za pozitívnu/negatívnu RE €),
      emergency (bool), tacre (total activated control RE),
      evaluationType, evaluationDate (z dennej hlavičky)

    Daň príklad: imbalance pre včerajšok ráno po 11:30 cez preliminarydaily.
    """
    if date_to is None:
        date_to = date_from
    url = f"{ISZO_BASE}/SystemImbalance"
    # Použijeme lowercased query keys (docs ukazujú že server je case-insensitive)
    params = {"dateFrom": date_from.isoformat(),
              "dateTo": date_to.isoformat(),
              "evaluationType": evaluation_type}
    try:
        data = _get_json(url, params)
    except RuntimeError:
        return pd.DataFrame(columns=["date", "period", "interval", "isp", "srec", "si",
                                     "pi", "ni", "pre", "nre", "pspre", "psnre"])
    if not isinstance(data, list) or not data:
        return pd.DataFrame(columns=["date", "period", "interval", "isp", "srec", "si",
                                     "pi", "ni", "pre", "nre", "pspre", "psnre"])
    rows = []
    for day in data:
        d_iso = day.get("date")
        eval_type = day.get("evaluationType")
        eval_date = day.get("evaluationDate")
        last_eval = day.get("lastEvaluationType")
        srec_day = day.get("srec")
        for p in (day.get("periods") or []):
            per = int(p.get("period") or 0)
            if per < 1:
                continue
            # interval HH:MM-HH:MM v SK čase: per 15-min, period 1 = 00:00-00:15
            # POZN. v DST dňoch je premostenie, ale väčšina dní 96 slotov
            start_min = (per - 1) * 15
            sh, sm = divmod(start_min, 60)
            eh, em = divmod(start_min + 15, 60)
            # cap pre DST: eh môže byť 24
            if eh >= 24:
                interval = f"{sh:02d}:{sm:02d}-{(eh%24):02d}:{em:02d}"
            else:
                interval = f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
            rows.append({
                "date": d_iso,
                "period": per,
                "interval": interval,
                "evaluationType": eval_type,
                "evaluationDate": eval_date,
                "lastEvaluationType": last_eval,
                "isp": p.get("isp"),                                          # ISP = system imbalance price EUR/MWh
                "srec": p.get("srec"),                                        # SREC EUR/MWh (per-period)
                "srec_day": srec_day,
                "mppre": p.get("mppre"),                                      # max cena ZRE+ €/MWh
                "mpnre": p.get("mpnre"),                                      # max cena ZRE- €/MWh
                "si": p.get("si"),                                            # systémová odchýlka MWh
                "pi": p.get("pi"),                                            # kladná zložka MWh
                "ni": p.get("ni"),                                            # záporná zložka MWh (negatívna)
                "pre": p.get("pre"),                                          # kladná regulačná elektrina MWh
                "nre": p.get("nre"),                                          # záporná RE MWh (negatívna)
                "pspre": p.get("pspre"),                                      # zúčtovanie pre kladnú RE EUR
                "psnre": p.get("psnre"),                                      # zúčtovanie pre zápornú RE EUR
                "psi": p.get("psi"),                                          # cena × SI = settlement value EUR
                "psrec": p.get("psrec"),                                      # period SREC value EUR
                "tcre": p.get("tcre"),                                        # total cost of regulation electricity
                "balance": p.get("balance"),
                "emergency": bool(p.get("emergency", False)),
                "tacre": p.get("tacre"),
            })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values(["date", "period"]).reset_index(drop=True)
    return df


# ─── DAM indices (denný/mesačný/ročný priemer) ──────────────────────────

def fetch_okte_dam_indices(date_from: dt.date, date_to: dt.date) -> pd.DataFrame:
    """Denné DAM indexy (base load, peak load priemery) pre SK trh.
    Vracia: date, baseLoad_EUR, peakLoad_EUR (alebo čokoľvek vráti API)."""
    url = f"{ISOT_BASE}/dam/indices"
    params = {"deliveryDayFrom": date_from.isoformat(), "deliveryDayTo": date_to.isoformat()}
    try:
        data = _get_json(url, params)
    except RuntimeError:
        return pd.DataFrame()
    if not isinstance(data, list):
        return pd.DataFrame()
    return pd.DataFrame(data)


# ─── Batch helper: fetch range → CSV ────────────────────────────────────

def fetch_range_to_csv(date_from: dt.date, date_to: dt.date, kind: str = "dayahead",
                        out_csv: Optional[str] = None) -> str:
    """Stiahne `kind` ('dayahead' / 'intraday' / 'imbalance') za rozsah dní,
    spojí do CSV. Default cesta out/sk/okte_<kind>_<from>_<to>.csv."""
    if out_csv is None:
        out_csv = f"out/sk/okte_{kind}_{date_from}_{date_to}.csv"
    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    fetcher = {
        "dayahead": fetch_okte_dayahead,
        "intraday": fetch_okte_intraday,
        "imbalance": fetch_okte_imbalance,
    }.get(kind)
    if fetcher is None:
        raise ValueError(f"Neznámy kind={kind!r} (povolené: dayahead/intraday/imbalance)")
    rng = pd.date_range(date_from, date_to, freq="D")
    all_dfs = []
    # Pre 'imbalance' je signature iný (date_from + date_to + evaluation_type) — handluj zvlášť:
    if kind == "imbalance":
        try:
            df = fetcher(date_from, date_to)
            if not df.empty:
                all_dfs.append(df)
                print(f"  ✓ {date_from}→{date_to}: {len(df)} riadkov")
            else:
                print(f"  ⊘ {date_from}→{date_to}: prázdne (evaluation ešte nepublikovaný?)")
        except Exception as e:
            print(f"  ✗ {date_from}→{date_to}: {e}")
    else:
        for d in rng:
            try:
                df = fetcher(d.date())
                if not df.empty:
                    all_dfs.append(df)
                    print(f"  ✓ {d.date()}: {len(df)} riadkov")
                else:
                    print(f"  ⊘ {d.date()}: prázdne")
            except Exception as e:
                print(f"  ✗ {d.date()}: {e}")
            time.sleep(0.3)                                                   # neútočiť na API rate-limit
    if not all_dfs:
        raise RuntimeError("Žiadny deň sa nepodarilo stiahnuť.")
    full = pd.concat(all_dfs, ignore_index=True)
    full.to_csv(out_csv, index=False)
    print(f"\n✓ Uložené: {out_csv} ({len(full)} riadkov)")
    return out_csv


def probe() -> dict:
    """Otestuje konektivitu k OKTE API. Vracia dict so stavom všetkých endpointov."""
    yesterday = dt.date.today() - dt.timedelta(days=1)
    older = yesterday - dt.timedelta(days=7)                                  # pre imbalance final treba 2-3 mesiace
    res = {}
    # DAM + IDM majú signature fn(date)
    for name, fn, arg in [
        ("dayahead_DAM (ISOT)", fetch_okte_dayahead, yesterday),
        ("intraday_IDM (ISOT)", fetch_okte_intraday, yesterday),
    ]:
        try:
            df = fn(arg)
            res[name] = {"ok": True, "rows": len(df),
                          "sample": df.head(2).to_dict(orient="records") if len(df) else []}
        except Exception as e:
            res[name] = {"ok": False, "error": str(e)[:200]}
    # imbalance: fn(date_from, date_to, evaluation_type)
    try:
        df = fetch_okte_imbalance(yesterday, yesterday, "preliminarydaily")
        res["imbalance_preliminarydaily (ISZO)"] = {"ok": True, "rows": len(df),
                                                       "sample": df.head(2).to_dict(orient="records") if len(df) else []}
    except Exception as e:
        res["imbalance_preliminarydaily (ISZO)"] = {"ok": False, "error": str(e)[:200]}
    try:
        df = fetch_okte_imbalance(older, older, "final")
        res["imbalance_final (ISZO)"] = {"ok": True, "rows": len(df),
                                          "sample": df.head(2).to_dict(orient="records") if len(df) else []}
    except Exception as e:
        res["imbalance_final (ISZO)"] = {"ok": False, "error": str(e)[:200]}
    return res
