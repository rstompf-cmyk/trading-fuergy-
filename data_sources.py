# -*- coding: utf-8 -*-
"""
data_sources.py – kompletná dátová vrstva pre model FTV + batéria (CZ trh).

Funkcie:
  fetch_pv_forecast(...)           Open-Meteo: hodinová predikcia výroby [kW]
  fetch_ote_dayahead(date)         OTE denný trh: 15-min cena ISOT [EUR/MWh]
  fetch_ote_imbalance(date,ver)    OTE odchýlky: ZCO [Kč/MWh] + sys. odchýlka [MWh]
  fetch_ceps_imbalance(f,t)        CEPS: minútová systémová odchýlka [MW]
  fetch_ceps_activation(f,t)       CEPS: minútové aFRR/mFRR aktivácie [MW]
  fetch_ceps_re_price(f,t)         CEPS: minútová cena RE (aFRR/mFRR) [EUR/MWh]
  fetch_ceps_est_price(f,t)        CEPS: 15-min odhadovaná cena odchýlky [Kč/MWh]

Závislosti: requests, pandas, lxml.   Overené 2026-05-22.
"""
from __future__ import annotations
import io, re, html, datetime as dt
import xml.etree.ElementTree as ET
import requests
import pandas as pd

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) FTV-app/1.0"}
TIMEOUT = 60
CEPS_ASMX = "https://www.ceps.cz/_layouts/CepsData.asmx"


# ============================================================ Open-Meteo PV
def fetch_pv_forecast(lat: float, lon: float, kwp: float = 99.0, tilt: float = 30.0,
                      azimuth: float = 0.0, eff: float = 0.85,
                      start: dt.date = None, end: dt.date = None,
                      timezone: str = "Europe/Prague") -> pd.DataFrame:
    """Hodinová predikcia FTV. kW = max(0, GTI/1000*kWp*eff*(1-0.004*(temp-25))).
    azimuth 0 = juh (Open-Meteo). Vracia: time, gti, temp, kw, kwh."""
    today = dt.date.today()
    start = start or today
    end = end or (today + dt.timedelta(days=1))
    frames, cutoff = [], today - dt.timedelta(days=5)
    segs = []
    if start < cutoff:
        segs.append(("https://archive-api.open-meteo.com/v1/archive", start, min(end, cutoff - dt.timedelta(days=1))))
    if end >= cutoff:
        segs.append(("https://api.open-meteo.com/v1/forecast", max(start, cutoff), end))
    for base, s, e in segs:
        r = requests.get(base, params={
            "latitude": lat, "longitude": lon,
            "hourly": "global_tilted_irradiance,temperature_2m,cloud_cover",
            "tilt": tilt, "azimuth": azimuth, "timezone": timezone,
            "start_date": s.isoformat(), "end_date": e.isoformat()}, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        h = r.json().get("hourly", {})
        if h.get("time"):
            frames.append(pd.DataFrame({"time": pd.to_datetime(h["time"]),
                                        "gti": h.get("global_tilted_irradiance"),
                                        "temp": h.get("temperature_2m"),
                                        "cloud": h.get("cloud_cover")}))
    if not frames:
        return pd.DataFrame(columns=["time", "gti", "temp", "cloud", "kw", "kwh"])
    df = pd.concat(frames).drop_duplicates("time").sort_values("time").reset_index(drop=True)
    # FTV-TZ-ALIGN (2026-06-27): open-meteo hodinová radiácia (GTI) je priemer za PREDCHÁDZAJÚCU
    # hodinu — hodnota pri čase T ≈ priemer [T−1h, T]. Použitie ako produkcia hodiny T ju posúva
    # o +1 h DOPREDU oproti realite (merač). Overené: predikcia vrchol 13:00 vs realita 12:00.
    # Realign −1 h: hodnota patrí hodine, ktorú reálne reprezentuje. Globálne pre všetky FTV.
    df["time"] = pd.to_datetime(df["time"]) - pd.Timedelta(hours=1)
    df["gti"] = df["gti"].fillna(0).clip(lower=0)
    df["temp"] = df["temp"].fillna(25)
    df["kw"] = ((df["gti"]/1000.0)*kwp*eff*(1-0.004*(df["temp"]-25))).clip(lower=0)
    df["kwh"] = df["kw"]
    return df


# ============================================================ MET Norway PV (backup)
def _solar_position(lat_deg: float, lon_deg: float, ts: pd.Timestamp, tz_offset_h: float) -> tuple:
    """Vráti (elevation_deg, azimuth_deg) pre danú lokáciu a lokálny čas.
    Konvencia: azimut 0 = juh, kladný = západ, záporný = východ (matches panel_az v fetch_pv_forecast).
    Presnosť ±1° (jednoduchá astronomická aproximácia)."""
    import math
    n = ts.dayofyear
    h_local = ts.hour + ts.minute / 60.0
    # equation of time (minúty)
    B = math.radians(360.0 * (n - 81) / 365.0)
    eot_min = 9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)
    # solar time = lokálny čas posunutý o longitúdu a EOT
    h_solar = h_local + (lon_deg / 15.0) - tz_offset_h + eot_min / 60.0
    omega = math.radians(15.0 * (h_solar - 12.0))                       # hour angle (+ popoludní)
    delta = math.radians(23.45 * math.sin(math.radians(360.0 * (284 + n) / 365.0)))  # declination
    phi = math.radians(lat_deg)
    sin_a = math.sin(delta) * math.sin(phi) + math.cos(delta) * math.cos(phi) * math.cos(omega)
    alpha = math.asin(max(-1.0, min(1.0, sin_a)))
    # azimut cez atan2 — zachová znamenku (juh = 0, západ = +)
    # γs = atan2(sin(ω), cos(ω)·sin(φ) − tan(δ)·cos(φ))
    gamma = math.atan2(math.sin(omega), math.cos(omega) * math.sin(phi) - math.tan(delta) * math.cos(phi))
    return math.degrees(alpha), math.degrees(gamma)


def _gti_from_cloud(elevation_deg: float, sun_az_deg: float, cloud_pct: float,
                     tilt_deg: float, panel_az_deg: float) -> float:
    """W/m² odhad GTI z slnečnej polohy + cloud cover. Jednoduchý model, ±15-20% vs pvlib.
    Konvencia azimutu: 0 = juh, kladné = západ (zhodné so sun_az z _solar_position aj s panel_az_deg
    v fetch_pv_forecast volaní)."""
    import math
    if elevation_deg <= 0:
        return 0.0
    alpha = math.radians(elevation_deg)                                 # elevácia nad horizontom
    beta = math.radians(tilt_deg)                                       # sklon panela
    az_diff = math.radians(sun_az_deg - panel_az_deg)                   # rozdiel azimutu slnka a panela
    # clear-sky GHI (W/m²) — empirická krivka
    ghi_clear = 1100.0 * (math.sin(alpha) ** 1.15)
    # útlm mračnami: GHI_actual = GHI_clear × (1 − 0.75 × CC^3.4)
    cc = max(0.0, min(1.0, cloud_pct / 100.0))
    ghi = ghi_clear * (1.0 - 0.75 * (cc ** 3.4))
    # uhol dopadu na naklonenú plochu (panel):
    # cos(θ) = sin(α)·cos(β) + cos(α)·sin(β)·cos(γs − γp)
    cos_theta = math.sin(alpha) * math.cos(beta) + math.cos(alpha) * math.sin(beta) * math.cos(az_diff)
    cos_theta = max(0.0, cos_theta)
    # rozdelenie: ~80% direct + ~20% diffuse (typický slnečný deň)
    # direct GTI: GHI × cos(θ) / sin(α)  (klipne sa zhora cez cos_theta a ghi_clear obmedzenie)
    gti_dir = 0.80 * ghi * cos_theta / max(math.sin(alpha), 1e-9)
    # diffuse GTI: 20% z GHI, isotropic sky on tilted surface
    gti_dif = 0.20 * ghi * (1.0 + math.cos(beta)) / 2.0
    return max(0.0, gti_dir + gti_dif)


def fetch_pv_forecast_metno(lat: float, lon: float, kwp: float = 99.0, tilt: float = 30.0,
                             azimuth: float = 0.0, eff: float = 0.85,
                             start: dt.date = None, end: dt.date = None,
                             timezone: str = "Europe/Prague") -> pd.DataFrame:
    """BACKUP PVF source: MET Norway LocationForecast 2.0 + custom solar geometry.
    Schéma rovnaká ako fetch_pv_forecast (time, gti, temp, cloud, kw, kwh).
    Pokrytie: ~10 dní dopredu. Žiadny API kľúč, vyžaduje User-Agent.
    URL: https://api.met.no/weatherapi/locationforecast/2.0/complete?lat=..&lon=.."""
    today = dt.date.today()
    start = start or today
    end = end or (today + dt.timedelta(days=1))
    url = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
    # MET Norway TOS vyžaduje identifikujúci User-Agent (kontakt v ňom)
    headers = {"User-Agent": "fuergy-ftv-planner/1.0 (radoslav.stompf@fuergy.com)",
               "Accept": "application/json"}
    r = requests.get(url, params={"lat": round(lat, 4), "lon": round(lon, 4)},
                     headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    series = data.get("properties", {}).get("timeseries", [])
    if not series:
        return pd.DataFrame(columns=["time", "gti", "temp", "cloud", "kw", "kwh"])
    # filter podľa rozsahu (v lokálnom čase)
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone)
    except ImportError:
        import pytz
        tz = pytz.timezone(timezone)
    start_local = pd.Timestamp(start).tz_localize(tz)
    end_local = (pd.Timestamp(end) + pd.Timedelta(days=1)).tz_localize(tz)
    rows = []
    for entry in series:
        ts_utc = pd.Timestamp(entry["time"])
        ts_local = ts_utc.tz_convert(tz)
        if not (start_local <= ts_local < end_local):
            continue
        details = entry.get("data", {}).get("instant", {}).get("details", {})
        cloud_pct = details.get("cloud_area_fraction", None)
        temp = details.get("air_temperature", None)
        if cloud_pct is None or temp is None:
            continue
        # tz offset pre solar position (handle DST)
        tz_offset_h = ts_local.utcoffset().total_seconds() / 3600.0
        ts_naive = ts_local.tz_localize(None)
        elev, sun_az = _solar_position(lat, lon, ts_naive, tz_offset_h)
        gti = _gti_from_cloud(elev, sun_az, float(cloud_pct), tilt, azimuth)
        # rovnaký vzorec ako fetch_pv_forecast: kw = max(0, GTI/1000 × kWp × eff × (1 − 0.004 × (T − 25)))
        kw = max(0.0, (gti / 1000.0) * kwp * eff * (1.0 - 0.004 * (float(temp) - 25.0)))
        rows.append((ts_naive, gti, float(temp), float(cloud_pct), kw, kw))
    df = pd.DataFrame(rows, columns=["time", "gti", "temp", "cloud", "kw", "kwh"])
    df = df.sort_values("time").reset_index(drop=True)
    # MET Norway dáva 1-hodinové intervaly len pre ~48 h, potom 6-hodinové. Pre náš use case (D-1)
    # je 48-h hodinovka dostatočná. Pre dlhší rozsah vyplníme dieru NaN-mi a interpolujeme.
    if not df.empty and "time" in df.columns:
        idx = pd.date_range(df["time"].iloc[0], df["time"].iloc[-1], freq="1h")
        df = df.set_index("time").reindex(idx).interpolate(method="linear").reset_index().rename(columns={"index": "time"})
    return df


# ============================================================ PVGIS TMY (3. fallback)
_PVGIS_TMY_CACHE = {}   # key=(lat,lon,kwp,tilt,azimuth,eff) → DataFrame 8760 hodín × kw


def fetch_pv_forecast_pvgis_tmy(lat: float, lon: float, kwp: float = 99.0,
                                  tilt: float = 30.0, azimuth: float = 0.0,
                                  eff: float = 0.85,
                                  start: dt.date = None, end: dt.date = None,
                                  timezone: str = "Europe/Prague") -> pd.DataFrame:
    """3. fallback PVF source: PVGIS TMY (Typical Meteorological Year, JRC EU).

    Žiadny rate limit, žiadny API kľúč, oficiálny zdroj solar dát (European Commission JRC).
    Vracia 8760 hodín priemerného roka pre danú lokáciu — pre konkrétny dátum extrahujeme
    príslušné hodiny.

    Pozor: TMY je *priemerný* rok, nie predpoveď konkrétneho dňa.
    Použité ako last-resort keď Open-Meteo aj MET Norway zlyhajú.

    Endpoint: https://re.jrc.ec.europa.eu/api/v5_2/seriescalc

    Schéma rovnaká ako fetch_pv_forecast (time, gti, temp, cloud, kw, kwh).
    """
    today = dt.date.today()
    start = start or today
    end = end or (today + dt.timedelta(days=1))
    key = (round(lat, 4), round(lon, 4), round(kwp, 2),
           round(tilt, 1), round(azimuth, 1), round(eff, 3))

    # Stiahnuť TMY raz pre lokáciu (cache na process-level)
    if key not in _PVGIS_TMY_CACHE:
        url = "https://re.jrc.ec.europa.eu/api/v5_2/seriescalc"
        params = {
            "lat": round(lat, 4), "lon": round(lon, 4),
            "raddatabase": "PVGIS-SARAH2",      # backup: PVGIS-ERA5
            "startyear": 2020, "endyear": 2020,   # 1 rok stačí
            "pvcalculation": 1,
            "peakpower": kwp,
            "loss": (1 - eff) * 100,             # straty v %
            "mountingplace": "building",
            "angle": tilt,
            # PVGIS azimut: 0 = juh, kladné = západ (rovnaké ako u nás)
            "aspect": azimuth,
            "components": 1,
            "outputformat": "json",
            "browser": 0,
        }
        try:
            r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            hourly = data.get("outputs", {}).get("hourly", [])
            if not hourly:
                return pd.DataFrame(columns=["time", "gti", "temp", "cloud", "kw", "kwh"])
            # PVGIS čas formát: "20200101:0010" → 1.1.2020 00:10 (UTC!)
            # Treba konvertovať na lokálny čas (Europe/Bratislava: UTC+1 zima, UTC+2 leto).
            try:
                from zoneinfo import ZoneInfo
                tz_local = ZoneInfo(timezone)
            except ImportError:
                import pytz
                tz_local = pytz.timezone(timezone)
            rows = []
            for h in hourly:
                t_str = h.get("time", "")
                if len(t_str) < 12:
                    continue
                try:
                    y, m, d = int(t_str[0:4]), int(t_str[4:6]), int(t_str[6:8])
                    hh = int(t_str[9:11])
                    # UTC → lokálny čas (zachová DST per dátum)
                    ts_utc = pd.Timestamp(year=y, month=m, day=d, hour=hh,
                                            tz="UTC")
                    ts = ts_utc.tz_convert(tz_local).tz_localize(None)
                except Exception:
                    continue
                # PVGIS vracia: P (power [W]), G(i) (GTI [W/m²]), T2m (temp °C)
                p_w = float(h.get("P") or 0)
                gti = float(h.get("G(i)") or 0)
                temp = float(h.get("T2m") or 25)
                kw_h = p_w / 1000.0
                rows.append((ts, gti, temp, float("nan"), kw_h, kw_h))
            df_tmy = pd.DataFrame(rows, columns=["time", "gti", "temp", "cloud", "kw", "kwh"])
            df_tmy = df_tmy.sort_values("time").reset_index(drop=True)
            _PVGIS_TMY_CACHE[key] = df_tmy
        except Exception as e:
            raise RuntimeError(f"PVGIS TMY fetch zlyhal: {e}")

    tmy = _PVGIS_TMY_CACHE[key]
    if tmy.empty:
        return pd.DataFrame(columns=["time", "gti", "temp", "cloud", "kw", "kwh"])

    # Pre požadovaný rozsah extrahuj hodiny — TMY je za rok 2020,
    # tak posunieme každý slot na rovnaký mesiac/deň/hodinu v cieľovom roku.
    target_dates = pd.date_range(start, end, freq="D")
    rows_out = []
    for d in target_dates:
        # Nájdi rovnaký dátum v TMY (mesiac, deň)
        mask = (tmy["time"].dt.month == d.month) & (tmy["time"].dt.day == d.day)
        day_tmy = tmy[mask].copy()
        if day_tmy.empty:
            continue
        # Premapuj rok 2020 → d.year
        day_tmy["time"] = day_tmy["time"].apply(
            lambda t: t.replace(year=d.year))
        rows_out.append(day_tmy)
    if not rows_out:
        return pd.DataFrame(columns=["time", "gti", "temp", "cloud", "kw", "kwh"])
    out = pd.concat(rows_out, ignore_index=True)
    out = out.sort_values("time").reset_index(drop=True)
    return out


# ============================================================ OTE (HTML tabuľky)
def _ote_tables(url, params):
    r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    txt = r.text.replace("\xa0", " ").replace("\u202f", " ")
    return pd.read_html(io.StringIO(txt), thousands=" ", decimal=",")

def _interval_col(df):
    for c in df.columns:
        s = df[c].astype(str).str.replace(" ", "", regex=False)
        if s.str.match(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$").mean() > 0.5:
            return c
    return None

def _flat_cols(df):
    df = df.copy()
    df.columns = [" ".join(map(str, c)) if isinstance(c, tuple) else str(c) for c in df.columns]
    return df

def fetch_ote_dayahead(date: dt.date) -> pd.DataFrame:
    """15-min ceny denného trhu ČR [EUR/MWh]. Vracia: date, interval, cena_EUR."""
    url = "https://www.ote-cr.cz/cs/kratkodobe-trhy/elektrina/denni-trh"
    for tb in _ote_tables(url, {"date": date.isoformat()}):
        tb = _flat_cols(tb)
        ic = _interval_col(tb)
        if ic is None:
            continue
        pc = next((c for c in tb.columns if "cena" in c.lower() and "eur" in c.lower()), None)
        if not pc:
            continue
        out = pd.DataFrame({"interval": tb[ic].astype(str).str.replace(" ", "", regex=False),
                            "cena_EUR": pd.to_numeric(tb[pc], errors="coerce")}).dropna()
        out = out[out["interval"].str.match(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$")]
        if len(out) >= 20:
            out.insert(0, "date", date.isoformat())
            return out.reset_index(drop=True)
    raise RuntimeError("OTE denný trh: tabuľka s cenou sa nenašla.")

def fetch_ote_intraday(date: dt.date) -> pd.DataFrame:
    """15-min ceny VNÚTRODENNÉHO trhu (VDT) ČR [EUR/MWh]. Vracia: date, interval, cena_EUR
    (vážený priemer), príp. cena_min/cena_max/objem_MWh. Publikované len pre obchodované periódy,
    takže môže vrátiť menej než 96 riadkov (typicky po aktuálnu/nasledujúcu)."""
    url = "https://www.ote-cr.cz/cs/kratkodobe-trhy/elektrina/vnitrodenni-trh"
    best = None
    for tb in _ote_tables(url, {"date": date.isoformat()}):
        tb = _flat_cols(tb)
        ic = _interval_col(tb)
        if ic is None:
            continue
        cols = {c.lower(): c for c in tb.columns}
        def find(*subs, need_eur=True):
            for lc, orig in cols.items():
                if (not need_eur or "eur" in lc) and all(s in lc for s in subs):
                    return orig
            return None
        pc = (find("vážen", "průměr") or find("vážen") or find("průměr")
              or find("cena") or find("price"))                  # vážený priemer EUR (hlavný signál)
        if not pc:
            continue
        out = pd.DataFrame({"interval": tb[ic].astype(str).str.replace(" ", "", regex=False),
                            "cena_EUR": pd.to_numeric(tb[pc], errors="coerce")})
        mn = find("min"); mx = find("max")
        vol = find("množ", need_eur=False) or find("objem", need_eur=False)
        if mn:  out["cena_min"] = pd.to_numeric(tb[mn], errors="coerce")
        if mx:  out["cena_max"] = pd.to_numeric(tb[mx], errors="coerce")
        if vol: out["objem_MWh"] = pd.to_numeric(tb[vol], errors="coerce")
        out = out[out["interval"].str.match(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$")]
        out = out.dropna(subset=["cena_EUR"])                    # len periódy, kde už je cena
        if len(out) >= 1 and (best is None or len(out) > len(best)):
            out.insert(0, "date", date.isoformat())
            best = out.reset_index(drop=True)
    if best is not None:
        return best
    raise RuntimeError("OTE vnútrodenný trh: tabuľka s cenou sa nenašla (skús ds._ote_debug()).")

def _ote_debug(url_key: str = "vnitrodenni-trh", date: dt.date = None):
    """Diagnostika: vypíše stĺpce a pár riadkov zo všetkých tabuliek na OTE stránke.
    url_key napr. 'vnitrodenni-trh' alebo 'denni-trh'. Pomáha doladiť parser."""
    date = date or dt.date.today()
    url = f"https://www.ote-cr.cz/cs/kratkodobe-trhy/elektrina/{url_key}"
    tabs = _ote_tables(url, {"date": date.isoformat()})
    print(f"URL: {url}?date={date.isoformat()}  →  {len(tabs)} tabuliek")
    for i, tb in enumerate(tabs):
        tb = _flat_cols(tb)
        print(f"\n── tabuľka {i}  ({tb.shape[0]}x{tb.shape[1]}) ──")
        print("stĺpce:", list(tb.columns))
        print(tb.head(3).to_string())
    return tabs

def fetch_ote_imbalance(date: dt.date, version: int = 0) -> pd.DataFrame:
    """15-min ZCO + systémová odchýlka. Vracia: date, interval, sys_MWh, zco_CZK."""
    url = "https://www.ote-cr.cz/cs/statistika/odchylky-elektrina"
    for tb in _ote_tables(url, {"date": date.isoformat(), "version": version}):
        tb = _flat_cols(tb)
        ic = _interval_col(tb)
        if ic is None:
            continue
        zc = next((c for c in tb.columns if "zúčtovací cena odchylky" in c.lower()), None)
        sc = next((c for c in tb.columns if "systémová odchylka" in c.lower()), None)
        if not zc:
            continue
        out = pd.DataFrame({"interval": tb[ic].astype(str).str.replace(" ", "", regex=False),
                            "sys_MWh": pd.to_numeric(tb[sc], errors="coerce") if sc else None,
                            "zco_CZK": pd.to_numeric(tb[zc], errors="coerce")}).dropna(subset=["zco_CZK"])
        out = out[out["interval"].str.match(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$")]
        if len(out) >= 20:
            out.insert(0, "date", date.isoformat())
            return out.reset_index(drop=True)
    raise RuntimeError("OTE odchýlky: tabuľka so ZCO sa nenašla.")


# ============================================================ CEPS (SOAP)
def _fmt(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S")

def _ceps_soap(op: str, params: dict) -> str:
    """Zavolá CEPS operáciu cez SOAP, vráti odkódovaný vnútorný <root>…</root> XML."""
    body = "".join(f"<{k}>{v}</{k}>" for k, v in params.items())
    env = ('<?xml version="1.0" encoding="utf-8"?>'
           '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
           f'<soap:Body><{op} xmlns="https://www.ceps.cz/CepsData/">{body}</{op}></soap:Body>'
           '</soap:Envelope>')
    h = {**UA, "Content-Type": "text/xml; charset=utf-8",
         "SOAPAction": f"https://www.ceps.cz/CepsData/{op}"}
    r = requests.post(CEPS_ASMX, data=env.encode("utf-8"), headers=h, timeout=TIMEOUT)
    r.raise_for_status()
    m = re.search(rf"<{op}Result>(.*?)</{op}Result>", r.text, re.S)
    if not m:
        raise RuntimeError(f"CEPS {op}: prázdna/neočakávaná odpoveď.")
    return html.unescape(m.group(1))

def _parse_ceps(xml_str: str) -> pd.DataFrame:
    s = re.sub(r'\sxmlns="[^"]+"', "", xml_str, count=1)
    root = ET.fromstring(s)
    df = pd.DataFrame([dict(it.attrib) for it in root.findall(".//item")])
    return df

def fetch_ceps_imbalance(dfrom: dt.datetime, dto: dt.datetime, agregation: str = "MI") -> pd.DataFrame:
    """Minútová systémová odchýlka [MW]. KLADNÁ = prebytok, ZÁPORNÁ = deficit (CEPS konvencia)."""
    xml = _ceps_soap("AktualniSystemovaOdchylkaCR",
                     {"dateFrom": _fmt(dfrom), "dateTo": _fmt(dto), "agregation": agregation, "function": "AVG"})
    df = _parse_ceps(xml).rename(columns={"date": "time", "value1": "sys_MW"})
    df["time"] = pd.to_datetime(df["time"]); df["sys_MW"] = pd.to_numeric(df["sys_MW"], errors="coerce")
    return df[["time", "sys_MW"]]

def fetch_ceps_activation(dfrom: dt.datetime, dto: dt.datetime, agregation: str = "MI") -> pd.DataFrame:
    """Minútové aktivácie [MW]: aFRR+/-, mFRR+/-, mFRR5."""
    xml = _ceps_soap("AktivaceSVRvCR",
                     {"dateFrom": _fmt(dfrom), "dateTo": _fmt(dto), "agregation": agregation,
                      "function": "AVG", "param1": "ALL"})
    df = _parse_ceps(xml).rename(columns={"date": "time", "value1": "aFRR_plus", "value2": "aFRR_minus",
                                          "value3": "mFRR_plus", "value4": "mFRR_minus", "value7": "mFRR5"})
    df["time"] = pd.to_datetime(df["time"])
    for c in ["aFRR_plus", "aFRR_minus", "mFRR_plus", "mFRR_minus", "mFRR5"]:
        if c in df: df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

def fetch_ceps_re_price(dfrom: dt.datetime, dto: dt.datetime) -> pd.DataFrame:
    """Minútová cena regulačnej energie [EUR/MWh]: aFRR (kľúčový prediktor), mFRR+/-/5."""
    xml = _ceps_soap("AktualniCenaRE", {"dateFrom": _fmt(dfrom), "dateTo": _fmt(dto), "param1": "ALL"})
    df = _parse_ceps(xml).rename(columns={"date": "time", "value1": "aFRR_EUR", "value2": "mFRRp_EUR",
                                          "value3": "mFRRm_EUR", "value4": "mFRR5_EUR"})
    df["time"] = pd.to_datetime(df["time"])
    for c in ["aFRR_EUR", "mFRRp_EUR", "mFRRm_EUR", "mFRR5_EUR"]:
        if c in df: df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

def fetch_ceps_est_price(dfrom: dt.datetime, dto: dt.datetime) -> pd.DataFrame:
    """15-min odhadovaná cena odchýlky [Kč/MWh] (real-time odhad ZCO). Vracia: interval, zco_est_CZK."""
    xml = _ceps_soap("OdhadovanaCenaOdchylky", {"dateFrom": _fmt(dfrom), "dateTo": _fmt(dto)})
    df = _parse_ceps(xml).rename(columns={"value2": "zco_est_CZK", "value15": "interval"})
    df["zco_est_CZK"] = pd.to_numeric(df["zco_est_CZK"], errors="coerce")
    cols = [c for c in ["interval", "zco_est_CZK"] if c in df]
    return df[cols]
