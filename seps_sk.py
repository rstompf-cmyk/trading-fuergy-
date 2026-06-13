# -*- coding: utf-8 -*-
"""
seps_sk.py — fetcher pre SEPS DAE (Slovak TSO real-time grid data).

Endpoint: https://dae.sepsas.sk/SK_PROD/DAEF-GUI/DAE_MVC/DfeDamas/TsPivotTableComponent/LoadData
View:    \\SS_PUB\\PUB\\DATAFLOW\\SYSTEM_STATE_MAP_VIEW

Vracia okamžité hodnoty SK elektrizačnej sústavy (update ~3 min):
  • Frekvencia ES SR [Hz]
  • Zaťaženie ES SR (load) [MW]
  • Výroba ES SR (production) [MW]
  • Regulačný výkon pre potreby ES SR [MW]  ⚠ INVERTED SIGN vs ČEPS sys_MW
  • Cezhraničné saldo obchodný diagram [MW]
  • Cezhraničné saldo merané [MW]
  • timestamp poslednej aktualizácie

⚠ Sign convention pre Regulačný výkon (RE_WITH_GCC):
  RE > 0 → systém v deficite (regulátory tlačia produkciu hore) → batéria má VYBÍJAŤ
  RE < 0 → systém v prebytku → batéria má NABÍJAŤ
  Pri integrácii do RT controlleru: rt_dir = sign(-RE) (opačné znamienko vs ČEPS).

Session management:
  DAE endpoint vyžaduje session cookies + XSRF token. Modul vie:
    1) Auto-bootstrap — GET landing + TS_VIEW → cookies/XSRF (najlepšie pre server)
    2) Manuálne cookies cez env vars SEPS_COOKIES + SEPS_XSRF_TOKEN (debug / fallback)

  Pri 401/403 (session expired) sa automaticky pokúsi o re-bootstrap raz.

Použitie:
    import seps_sk
    state = seps_sk.fetch_seps_realtime()
    print(state["frequency_hz"], state["regulation_power_mw"])

    # Alebo s rozsahom dátumov (historický view):
    state = seps_sk.fetch_seps_realtime(
        interval_from="2026-05-26T22:00:00.000Z",
        interval_till="2026-05-27T22:00:00.000Z",
    )
"""
from __future__ import annotations
import os
import re
import time
import datetime as dt
import json
from typing import Optional, Dict, Any
from urllib.parse import quote

import requests


# ─── Konštanty ────────────────────────────────────────────────────────────────
BASE = "https://dae.sepsas.sk"
LANDING_PATH = "/SK_PROD/"
VIEW_SCREEN_PATH = "/SK_PROD/DAEF-GUI/DAE_MVC/UC/TS_VIEW_SCREEN"
LOAD_DATA_PATH = "/SK_PROD/DAEF-GUI/DAE_MVC/DfeDamas/TsPivotTableComponent/LoadData"
SYSTEM_STATE_VIEW_CODE = "\\SS_PUB\\PUB\\DATAFLOW\\SYSTEM_STATE_MAP_VIEW"

_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_TIMEOUT_S = 25

# Mapping DAMAS code → kľúč v našom output dict (primárna cesta keď je timeSerieConfigurations)
_FIELD_MAP = {
    "REAL_SYSTEM_FREQUENCY":     "frequency_hz",
    "REAL_SYSTEM_LOAD":          "load_mw",
    "REAL_SYSTEM_PRODUCTION":    "production_mw",
    "REAL_BALANCE":              "real_balance_mw",
    "ACKNOWLEDGED_REAL_BALANCE": "scheduled_balance_mw",
    "RE_WITH_GCC":               "regulation_power_mw",       # ⚠ inverted sign vs ČEPS
    "DATA_UPDATED":              "data_updated_sec",
}

# Fallback mapping podľa label textu — DAE často posiela response BEZ timeSerieConfigurations
# (delta update / session-cached metadata), takže `s` index nie je mapovaný. Label match je
# robustnejší — pri zmene UI v SEPS DAE nám stačí upraviť tieto regex-y.
_LABEL_PATTERNS = [
    (r"frekvencia\s+es\s+sr",                                  "frequency_hz"),
    (r"za[tť]a[zž]enie\s+es\s+sr",                              "load_mw"),
    (r"v[yý]roba\s+es\s+sr",                                    "production_mw"),
    (r"meran[eé]\s+[uú]daje|skuto[cč]n[eé]\s+saldo",            "real_balance_mw"),
    (r"obchodn[yý]\s+diagram",                                  "scheduled_balance_mw"),
    (r"regula[cč]n[yý]\s+v[yý]kon",                             "regulation_power_mw"),
]
_LABEL_REGEX = [(re.compile(p, re.IGNORECASE), key) for p, key in _LABEL_PATTERNS]


def _key_from_label(label: str) -> Optional[str]:
    """Vráti náš výsledný kľúč na základe textu label-u, alebo None."""
    if not label:
        return None
    for rx, key in _LABEL_REGEX:
        if rx.search(label):
            return key
    return None


# ─── Parser ──────────────────────────────────────────────────────────────────

def _parse_number(v) -> Optional[float]:
    """Bezpečný parser '49.997' / '3058' / '-14,6' / '' / None → float | None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s == "—":
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def parse_system_state(json_obj: dict) -> Dict[str, Any]:
    """Parsuje response z SystemStateMap view → dict s aktuálnymi hodnotami.

    Návratová štruktúra:
      {
        "updated_at":           "2026-05-27T18:27:14.548Z",  # ISO timestamp
        "frequency_hz":         49.997,
        "load_mw":              3058.0,
        "production_mw":        3767.0,
        "real_balance_mw":      707.0,
        "scheduled_balance_mw": 654.0,
        "regulation_power_mw": -14.6,
      }
    Polia ktoré sa nepodarilo načítať budú chýbať (volajúci nech používa .get()).
    """
    out: Dict[str, Any] = {}
    try:
        sheets = json_obj.get("gridConfig", {}).get("sheets") or []
        if not sheets:
            return out
        sheet = sheets[0]
        ts_cfg = sheet.get("timeSerieConfigurations") or {}
        rows = (sheet.get("dataModel") or {}).get("data") or []
    except (KeyError, IndexError, TypeError, AttributeError):
        return out

    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        label_cell = row[0] or {}
        value_cell = row[1] or {}
        v_raw = value_cell.get("v")
        label_v = (label_cell.get("v") or "")

        # "Čas poslednej aktualizácie" → ISO timestamp
        if "aktualiz" in label_v.lower():
            out["updated_at"] = v_raw
            continue

        # Mapping kľúča — primárne cez label text (najrobustnejšie, funguje aj keď
        # response nemá timeSerieConfigurations — server občas posiela delta bez metadát).
        # Fallback: ak label nezmatchuje, skús ts_cfg[str(s)].code → _FIELD_MAP.
        key = _key_from_label(label_v)
        if key is None:
            s_idx = value_cell.get("s")
            if s_idx is not None:
                cfg = ts_cfg.get(str(s_idx))
                if cfg:
                    key = _FIELD_MAP.get(cfg.get("code"))
        if key is None:
            continue
        # data_updated_sec je len visualType=Second, nepotrebujeme ho ako number
        if key == "data_updated_sec":
            out[key] = v_raw
            continue
        out[key] = _parse_number(v_raw)
    return out


# ─── Session bootstrap + fetch ───────────────────────────────────────────────

class SepsSession:
    """HTTP session s automatickým bootstrap-om cookies + XSRF tokenu.

    Drží jeden requests.Session() ktorý prežíva celý beh appky. Pri 401/403
    sa pokúsi raz o re-bootstrap (možno session vypršala).
    """

    def __init__(self, manual_cookies: Optional[str] = None,
                 manual_xsrf: Optional[str] = None):
        self._sess = requests.Session()
        self._sess.headers.update({
            "User-Agent": _USER_AGENT,
            "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.6",
        })
        self._xsrf: Optional[str] = manual_xsrf or os.environ.get("SEPS_XSRF_TOKEN")
        self._ready = False

        # Priorita zdrojov cookies (od najvyššej):
        # 1) Manuálne argumenty (programatic)
        # 2) Env vars SEPS_COOKIES + SEPS_XSRF_TOKEN (debug/local dev)
        # 3) Disk cache (out/sk/seps_cookies.json) — updatovaná Playwright refresherom
        manual = manual_cookies or os.environ.get("SEPS_COOKIES")
        if manual:
            self._apply_cookie_string(manual)
            self._ready = True
        else:
            disk = self._load_from_disk()
            if disk:
                self._apply_cookie_string(disk.get("cookies", ""))
                self._xsrf = disk.get("xsrf_token") or self._xsrf
                self._ready = True

    @staticmethod
    def _load_from_disk() -> Optional[dict]:
        """Načíta naposledy uložené cookies (z Playwright refresher-a)."""
        try:
            import seps_cookies_refresh
            return seps_cookies_refresh.load_cached_cookies()
        except ImportError:
            return None
        except Exception:
            return None

    def _try_disk_refresh(self) -> bool:
        """Pokus o load fresh cookies z disku (po background refresh)."""
        data = self._load_from_disk()
        if not data:
            return False
        # vyčisti staré cookies
        self._sess.cookies.clear()
        self._apply_cookie_string(data.get("cookies", ""))
        new_xsrf = data.get("xsrf_token")
        if new_xsrf:
            self._xsrf = new_xsrf
        return True

    def _trigger_refresh_subprocess(self) -> bool:
        """Spustí seps_cookies_refresh.py ako subprocess (synchronne, ~10s)."""
        try:
            import seps_cookies_refresh
            ok, _ = seps_cookies_refresh.refresh_once(verbose=False)
            if ok:
                return self._try_disk_refresh()
        except Exception as e:
            print(f"[seps_sk] auto-refresh zlyhal: {e}")
        return False

    def _apply_cookie_string(self, cookie_str: str):
        """Parsuje 'name=val; name2=val2' a pridá do session.cookies."""
        for chunk in cookie_str.split(";"):
            chunk = chunk.strip()
            if "=" in chunk:
                name, val = chunk.split("=", 1)
                self._sess.cookies.set(name.strip(), val.strip(), domain="dae.sepsas.sk")

    def _bootstrap(self):
        """Získa session cookies + XSRF token cez 2 GET requesty."""
        # Step 1: GET landing — IIS vytvorí session cookies
        self._sess.get(BASE + LANDING_PATH, timeout=_TIMEOUT_S, allow_redirects=True)
        # Step 2: GET TS_VIEW_SCREEN — nastaví __RequestVerificationToken cookie a XSRF
        r = self._sess.get(BASE + VIEW_SCREEN_PATH,
                           params={"VIEW_CODE": SYSTEM_STATE_VIEW_CODE},
                           timeout=_TIMEOUT_S)
        # Hľadaj XSRF v cookies (typicky XSRF-TOKEN)
        for c in self._sess.cookies:
            if "XSRF" in c.name.upper():
                self._xsrf = c.value
                break
        # Ak nie je v cookies, hľadaj v HTML body (často v JS variable)
        if not self._xsrf and r.text:
            for pat in (
                r'XSRF[_-]TOKEN["\']?\s*[:=]\s*["\']([^"\']+)',
                r'name=["\']__RequestVerificationToken["\'][^>]*value=["\']([^"\']+)',
                r'antiForgeryToken["\']?\s*[:=]\s*["\']([^"\']+)',
            ):
                m = re.search(pat, r.text, re.IGNORECASE)
                if m:
                    self._xsrf = m.group(1)
                    break
        self._ready = True

    def _headers_for_load_data(self, view_code: str) -> Dict[str, str]:
        h = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": BASE,
            "Referer": BASE + VIEW_SCREEN_PATH + f"?VIEW_CODE={view_code}",
            "X-Requested-With": "XMLHttpRequest",
            "DFE-ClientVersion": "51.0.1.18637",
            "DFE-Component-ID": "PIVOT_TABLE",
            "DFE-Screen-ID": "TS_VIEW_SCREEN",
            "DFE-TimeZone-Id": "CET",
        }
        if self._xsrf:
            h["X-XSRF-TOKEN"] = self._xsrf
        return h

    def fetch_load_data(self, view_code: str,
                        interval_from: str, interval_till: str) -> dict:
        """POST na LoadData endpoint a vráti parsed JSON."""
        if not self._ready:
            self._bootstrap()

        payload = {
            "parameters": [
                {"code": "VIEW_CODE",         "value": view_code},
                {"code": "TIME_FILTER_UNIT",  "value": "DAY"},
                {"code": "INTERVAL_TILL",     "value": interval_till},
                {"code": "INTERVAL_FROM",     "value": interval_from},
                {"code": "USER_COND_FORMULA", "value": "1"},
                {"code": "SKIP_VIEW_CONTEXT", "value": True},
                {"code": "CHART_TYPE",        "value": None},
                {"code": "VIEW_TYPE",         "value": "TimeseriesView"},
                {"code": "QUERY",             "value": "#?VIEW_CODE=" + quote(view_code, safe="")},
            ]
        }

        url = BASE + LOAD_DATA_PATH
        r = self._sess.post(url, json=payload,
                            headers=self._headers_for_load_data(view_code),
                            timeout=_TIMEOUT_S)

        # 401/403/500 → session vypršala alebo server vracia HTML chybu.
        # SEPS DAE niekedy vracia 500 + HTML keď session token expired (namiesto
        # čistého 401). Spustíme refresh cookies aj pri 500.
        _content_is_html = "text/html" in r.headers.get("content-type", "").lower()
        if r.status_code in (401, 403) or (r.status_code == 500 and _content_is_html):
            print(f"[seps_sk] HTTP {r.status_code} — pokus o refresh cookies")
            refreshed = self._try_disk_refresh() or self._trigger_refresh_subprocess()
            if refreshed:
                r = self._sess.post(url, json=payload,
                                    headers=self._headers_for_load_data(view_code),
                                    timeout=_TIMEOUT_S)
            else:
                # posledný pokus — auto-bootstrap (pravdepodobne stále zlyhá, ale logujeme)
                self._ready = False
                self._xsrf = None
                self._bootstrap()
                r = self._sess.post(url, json=payload,
                                    headers=self._headers_for_load_data(view_code),
                                    timeout=_TIMEOUT_S)

        if r.status_code != 200:
            raise RuntimeError(
                f"SEPS DAE LoadData HTTP {r.status_code}: {r.text[:300]}"
            )
        try:
            return r.json()
        except json.JSONDecodeError as e:
            raise RuntimeError(f"SEPS DAE LoadData: non-JSON response: {e}\nBody: {r.text[:300]}")


# ─── Default singleton (lazy) ────────────────────────────────────────────────
_DEFAULT_SESSION: Optional[SepsSession] = None


def _get_default_session() -> SepsSession:
    global _DEFAULT_SESSION
    if _DEFAULT_SESSION is None:
        _DEFAULT_SESSION = SepsSession()
    return _DEFAULT_SESSION


def reset_session():
    """Force re-bootstrap na ďalšom volaní (užitočné ak server preferuje fresh cookies)."""
    global _DEFAULT_SESSION
    _DEFAULT_SESSION = None


# ─── Public API ──────────────────────────────────────────────────────────────

def fetch_seps_realtime(interval_from: Optional[str] = None,
                        interval_till: Optional[str] = None) -> Dict[str, Any]:
    """High-level: vráti okamžité hodnoty SK sústavy.

    Defaults: interval = [včerajšok 22:00 UTC, dnes 22:00 UTC] — endpoint
    si aj tak vyberá najnovšiu hodnotu, ale parameter je povinný.

    Pri zlyhaní vráti prázdny dict + logne chybu (volajúci by mal používať .get()).
    """
    if interval_till is None:
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).replace(microsecond=0)
        # zaokrúhli na celú hodinu nahor
        interval_till = now.replace(minute=0, second=0).strftime("%Y-%m-%dT%H:00:00.000Z")
    if interval_from is None:
        end = dt.datetime.strptime(interval_till, "%Y-%m-%dT%H:%M:%S.%fZ")
        interval_from = (end - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:00:00.000Z")
    try:
        sess = _get_default_session()
        json_obj = sess.fetch_load_data(SYSTEM_STATE_VIEW_CODE,
                                        interval_from, interval_till)
        return parse_system_state(json_obj)
    except Exception as e:
        print(f"[seps_sk] fetch_seps_realtime zlyhalo: {e}")
        return {}


# ─── CSV logger (1-min cadence, dedup podľa updated_at) ─────────────────────

# CSV schema — fixne, aby sa neflákal append (kazdy řádek má rovnaké stĺpce)
_CSV_COLUMNS = [
    "ts_utc",                # čas fetch-u (UTC ISO)
    "updated_at",            # vnútorný timestamp SEPS (UTC ISO)
    "frequency_hz",
    "load_mw",
    "production_mw",
    "scheduled_balance_mw",  # obchodný diagram
    "real_balance_mw",       # merané údaje
    "regulation_power_mw",   # ⚠ INVERTED sign vs ČEPS sys_MW
]


def _csv_path() -> str:
    """Adresár pre SEPS data — vždy out/sk/ (paralela so seps_cookies_refresh)."""
    try:
        import market as mk
        root = os.path.dirname(mk.data_dir().rstrip("/").rstrip(os.sep)) or "out"
    except Exception:
        root = "out"
    return os.path.join(root, "sk", "seps_realtime_minute.csv")


def log_realtime(state: Optional[Dict[str, Any]] = None,
                  csv_path: Optional[str] = None) -> Optional[str]:
    """Pripojí jeden riadok do seps_realtime_minute.csv.

    - Ak `state` je None, sám fetchne aktuálne hodnoty cez fetch_seps_realtime().
    - De-dup podľa `updated_at` — ak posledný riadok v CSV má rovnaký updated_at
      ako nový, nepríde duplicate (SEPS updateuje len každé ~3 min).
    - Vracia path k CSV ak sa zapísalo, alebo None ak skip.
    """
    if state is None:
        state = fetch_seps_realtime()
    if not state or "updated_at" not in state:
        return None

    path = csv_path or _csv_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    # de-dup: ak posledný riadok má rovnaký updated_at, nepiš znovu
    last_updated_at = None
    file_exists = os.path.exists(path)
    if file_exists:
        try:
            with open(path, "rb") as f:
                # rýchle čítanie poslednej línie (bez načítania celého súboru)
                try:
                    f.seek(-2, os.SEEK_END)
                    while f.read(1) != b"\n":
                        f.seek(-2, os.SEEK_CUR)
                except OSError:
                    f.seek(0)
                last_line = f.readline().decode("utf-8", errors="ignore").strip()
            if last_line and not last_line.startswith("ts_utc"):
                # rozparsuj druhé pole (updated_at) z CSV linky
                parts = last_line.split(",")
                if len(parts) >= 2:
                    last_updated_at = parts[1].strip()
        except Exception:
            pass

    if last_updated_at and last_updated_at == state.get("updated_at"):
        return None                                              # skip — žiadny nový update

    ts_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
    row = {
        "ts_utc":               ts_utc,
        "updated_at":           state.get("updated_at", ""),
        "frequency_hz":         state.get("frequency_hz", ""),
        "load_mw":              state.get("load_mw", ""),
        "production_mw":        state.get("production_mw", ""),
        "scheduled_balance_mw": state.get("scheduled_balance_mw", ""),
        "real_balance_mw":      state.get("real_balance_mw", ""),
        "regulation_power_mw":  state.get("regulation_power_mw", ""),
    }
    line = ",".join(str(row[c]) if row[c] is not None else "" for c in _CSV_COLUMNS)
    with open(path, "a") as f:
        if not file_exists:
            f.write(",".join(_CSV_COLUMNS) + "\n")
        f.write(line + "\n")
    return path


# ─── Day-load: SEPS reg.výkon pre /livesim chart ─────────────────────────────

def _historian_re_csv() -> str:
    """Cesta k historickému CSV (z historian_backfill)."""
    try:
        import market as mk
        root = os.path.dirname(mk.data_dir().rstrip("/").rstrip(os.sep)) or "out"
    except Exception:
        root = "out"
    return os.path.join(root, "sk", "historian_I_WEB_DAMAS_ReWithGCC_3m.csv")


def _historian_okte_dam_csv() -> str:
    """Cesta k historickému DAM CSV — tag C_WEB_OKTE_ISOT_15m (kompletný 96 slotov/deň)."""
    try:
        import market as mk
        root = os.path.dirname(mk.data_dir().rstrip("/").rstrip(os.sep)) or "out"
    except Exception:
        root = "out"
    return os.path.join(root, "sk", "historian_C_WEB_OKTE_ISOT_15m.csv")


def load_seps_mw_for_day(date_iso: str) -> "pd.DataFrame":
    """Vráti minútovú radu SEPS reg.výkonu pre daný deň (lokálny SK dátum).

    Zlúči 2 zdroje:
      • Historian CSV (3-min, presné, retrospektívne) — pre dni v minulosti
      • seps_realtime_minute.csv (1-min, čerstvé, len pre dnes) — pre dnešok

    **SIGN FLIP NA VSTUPE**: hodnoty `mw` sú multiplied ×−1 pred návratom,
    aby ČEPS-compat konvencia (+MW=surplus, −MW=deficit) platila pre všetkých
    downstream konzumentov (rt_controller, /rt grafy, build_sk_live_minutes).
    SEPS RE_WITH_GCC native: +MW=deficit, −MW=surplus → po flipe je to ČEPS-style.

    Output: DataFrame s columns [ts_local, mw, source], 1-min mriežka 00:00 → 23:59
    Pre intervaly bez dát → NaN (Chart.js bude robiť gap).
    """
    import pandas as pd
    import numpy as np

    # 1-min grid pre celý deň v lokálnom SK čase
    day = pd.Timestamp(date_iso, tz="Europe/Bratislava")
    grid = pd.date_range(day, day + pd.Timedelta(days=1) - pd.Timedelta(minutes=1),
                         freq="1min", tz="Europe/Bratislava")
    out = pd.DataFrame({"ts_local": grid, "mw": np.nan, "source": ""})

    # ─── Zdroj 1: historian CSV (3-min, retrospektívne) ────────────────────
    hist_path = _historian_re_csv()
    if os.path.exists(hist_path):
        try:
            df = pd.read_csv(hist_path, usecols=["time_utc", "value"])
            df["ts_local"] = pd.to_datetime(df["time_utc"], utc=True).dt.tz_convert("Europe/Bratislava")
            day_start = day
            day_end = day + pd.Timedelta(days=1)
            df = df[(df["ts_local"] >= day_start) & (df["ts_local"] < day_end)]
            if not df.empty:
                # Round na najbližšiu minútu + sort + dedup (musí byť monotonic pre reindex+ffill)
                df["ts_min"] = df["ts_local"].dt.floor("1min")
                ser = df.groupby("ts_min")["value"].last().sort_index()
                ser = ser.reindex(grid, method="ffill", limit=3)        # ffill max 3 min
                mask = ser.notna()
                out.loc[mask.values, "mw"] = ser[mask].values
                out.loc[mask.values, "source"] = "historian"
        except Exception as e:
            print(f"[seps_sk.load_seps_mw_for_day] historian read zlyhal: {e}")

    # ─── Zdroj 2: seps_realtime_minute.csv (1-min, čerstvé) — prepíše ──────
    rt_path = _csv_path()                                              # out/sk/seps_realtime_minute.csv
    if os.path.exists(rt_path):
        try:
            df = pd.read_csv(rt_path, usecols=["ts_utc", "regulation_power_mw"])
            df["ts_local"] = pd.to_datetime(df["ts_utc"], utc=True).dt.tz_convert("Europe/Bratislava")
            day_start = day
            day_end = day + pd.Timedelta(days=1)
            df = df[(df["ts_local"] >= day_start) & (df["ts_local"] < day_end)]
            if not df.empty:
                df["ts_min"] = df["ts_local"].dt.floor("1min")
                ser = df.groupby("ts_min")["regulation_power_mw"].last().sort_index()
                ser = ser.reindex(grid)
                mask = ser.notna()
                # Realtime má prioritu: prepíše historian ak je k dispozícii
                out.loc[mask.values, "mw"] = ser[mask].values
                out.loc[mask.values, "source"] = "realtime"
        except Exception as e:
            print(f"[seps_sk.load_seps_mw_for_day] realtime read zlyhal: {e}")

    # ── SIGN FLIP NA VSTUPE (single point of truth) ────────────────────────
    # SEPS native: +MW = deficit, −MW = surplus
    # ČEPS-compat: +MW = surplus, −MW = deficit
    # Multiplikujeme ×−1 aby downstream konzumenti (rt_controller, /rt grafy,
    # build_sk_live_minutes) dostali ČEPS-compat sign.
    out["mw"] = -out["mw"]
    return out


def load_sys_arrays_for_rt(date_iso: str):
    """Pre /rt graf c2 ("Systémová odchýlka [MW] — 15-min priemer (stĺpce)") pre SK trh.

    Vracia (sys5, sys15rep, sys15rep_col) — 288 prvkov (5-min mriežka 00:00–23:55).

    SIGN: hodnoty `mw` z load_seps_mw_for_day sú už **ČEPS-compat** (flipnuté
    na vstupe v load_seps_mw_for_day). +MW = surplus, −MW = deficit.

    Zdroj: out/sk/historian_I_WEB_DAMAS_ReWithGCC_3m.csv (3-min, retrospektívne)
           + out/sk/seps_realtime_minute.csv (1-min, čerstvé, len pre dnes).
    """
    import pandas as pd
    import numpy as np

    # 1-min mriežka pre celý deň → resamplujeme na 5-min + 15-min
    df1 = load_seps_mw_for_day(date_iso)
    s1 = df1.set_index("ts_local")["mw"]   # mw je už flipnuté na vstupe

    # 5-min mriežka (288 bodov, 00:00..23:55)
    day = pd.Timestamp(date_iso, tz="Europe/Bratislava")
    m5_idx = pd.date_range(day, periods=288, freq="5min", tz="Europe/Bratislava")
    s5 = s1.resample("5min").mean().reindex(m5_idx)

    # 15-min mriežka — 96 bodov, replikované 3× pre 5-min slots
    m15_idx = pd.date_range(day, periods=96, freq="15min", tz="Europe/Bratislava")
    s15 = s1.resample("15min").mean().reindex(m15_idx)
    # Pre každý 5-min bod nájdi 15-min slot (floor na 15min)
    s15_for_5min = []
    for t5 in m5_idx:
        t15 = t5.floor("15min")
        v = s15.get(t15, np.nan)
        s15_for_5min.append(float(v) if pd.notna(v) else None)

    sys5 = [round(float(v), 0) if pd.notna(v) else None for v in s5.values]
    sys15rep = [round(v, 0) if v is not None else None for v in s15_for_5min]
    # Color logic: žltá (#E0A800) keď ≥0 (surplus), modrá (#5DADE2) keď <0 (deficit)
    sys15rep_col = ["#E0A800" if (v or 0) >= 0 else "#5DADE2" for v in sys15rep]
    return sys5, sys15rep, sys15rep_col


def _historian_okte_zco_csv() -> str:
    try:
        import market as mk
        root = os.path.dirname(mk.data_dir().rstrip("/").rstrip(os.sep)) or "out"
    except Exception:
        root = "out"
    return os.path.join(root, "sk", "historian_I_WEB_OKTE_ZCO_15m.csv")


def _historian_okte_vdt_csv() -> str:
    try:
        import market as mk
        root = os.path.dirname(mk.data_dir().rstrip("/").rstrip(os.sep)) or "out"
    except Exception:
        root = "out"
    return os.path.join(root, "sk", "historian_I_WEB_OKTE_VDT_final_15m.csv")


def _historian_okte_vdt_prelim_csv() -> str:
    """Predbežné VDT — tag I_OKTE_ISOT_VDT_15m (continuous, ide do konca dňa)."""
    try:
        import market as mk
        root = os.path.dirname(mk.data_dir().rstrip("/").rstrip(os.sep)) or "out"
    except Exception:
        root = "out"
    return os.path.join(root, "sk", "historian_I_OKTE_ISOT_VDT_15m.csv")


def _load_okte_15min_from_csv(csv_path: str, date_iso: str) -> Dict[str, float]:
    """Spoločný 15-min loader z historian CSV — vracia dict {ts_naive_iso: value}.

    Kľúče sú 15-min slot timestamps v lokálnom SK čase (naive, bez TZ),
    formát "YYYY-MM-DD HH:MM:SS" pre kompatibilitu s /rt full_ts mapou.
    """
    import pandas as pd
    out: Dict[str, float] = {}
    if not os.path.exists(csv_path):
        return out
    try:
        df = pd.read_csv(csv_path, usecols=["time_utc", "value"])
        df["ts_local"] = pd.to_datetime(df["time_utc"], utc=True).dt.tz_convert("Europe/Bratislava")
        day = pd.Timestamp(date_iso, tz="Europe/Bratislava")
        df = df[(df["ts_local"] >= day) & (df["ts_local"] < day + pd.Timedelta(days=1))]
        if df.empty:
            return out
        df["ts15"] = df["ts_local"].dt.floor("15min").dt.tz_localize(None)
        # Deduplikuj — niekedy v CSV sú viaceré samples v rovnakom 15-min slote
        ser = df.groupby("ts15")["value"].last()
        for ts, v in ser.items():
            out[pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S")] = float(v)
    except Exception as e:
        print(f"[seps_sk._load_okte_15min_from_csv] {csv_path}: {e}")
    return out


def load_okte_zco_for_day(date_iso: str) -> Dict[str, float]:
    """ZCO (zúčtovacia cena odchýlky) v €/MWh per 15-min slot. Len D-1 (dnes prázdne)."""
    return _load_okte_15min_from_csv(_historian_okte_zco_csv(), date_iso)


def load_okte_vdt_for_day(date_iso: str) -> Dict[str, float]:
    """VDT (vnútrodenný trh, finálny) v €/MWh per 15-min slot. D-1 only."""
    return _load_okte_15min_from_csv(_historian_okte_vdt_csv(), date_iso)


def load_okte_vdt_preliminary_for_day(date_iso: str) -> Dict[str, float]:
    """VDT predbežné (continuous, ide aj cez dnešok) v €/MWh per 15-min slot."""
    return _load_okte_15min_from_csv(_historian_okte_vdt_prelim_csv(), date_iso)


def load_okte_dt_for_day(date_iso: str) -> Dict[str, float]:
    """DT (denný trh, OKTE clearing) v €/MWh per 15-min slot — alternative formát k load_okte_dam_for_day."""
    return _load_okte_15min_from_csv(_historian_okte_dam_csv(), date_iso)


def load_okte_dam_for_day(date_iso: str) -> "pd.DataFrame":
    """Vráti 15-min DAM ceny pre deň z historian CSV. Pre Chart.js stepped line.

    Output: DataFrame s columns [ts_local, eur_mwh], rozšírený na 1-min grid (stepped).
    """
    import pandas as pd
    import numpy as np

    day = pd.Timestamp(date_iso, tz="Europe/Bratislava")
    grid = pd.date_range(day, day + pd.Timedelta(days=1) - pd.Timedelta(minutes=1),
                         freq="1min", tz="Europe/Bratislava")
    out = pd.DataFrame({"ts_local": grid, "eur_mwh": np.nan})

    dam_path = _historian_okte_dam_csv()
    if not os.path.exists(dam_path):
        return out
    try:
        df = pd.read_csv(dam_path, usecols=["time_utc", "value"])
        df["ts_local"] = pd.to_datetime(df["time_utc"], utc=True).dt.tz_convert("Europe/Bratislava")
        day_start = day
        day_end = day + pd.Timedelta(days=1)
        df = df[(df["ts_local"] >= day_start) & (df["ts_local"] < day_end)]
        if not df.empty:
            df["ts_min"] = df["ts_local"].dt.floor("1min")
            ser = df.groupby("ts_min")["value"].last().sort_index()
            ser = ser.reindex(grid, method="ffill", limit=15)           # 15-min ffill
            mask = ser.notna()
            out.loc[mask.values, "eur_mwh"] = ser[mask].values
    except Exception as e:
        print(f"[seps_sk.load_okte_dam_for_day] DAM read zlyhal: {e}")
    return out


def build_sk_minute_history(from_date: "pd.Timestamp",
                              to_date: "pd.Timestamp",
                              progress_cb=None) -> "pd.DataFrame":
    """Postaviť SK ekvivalent imbalance_minute.csv z historian dát.

    Range from_date → to_date (vrátane). Pre každý deň: 1-min mriežka 00:00–23:59
    naplnená z:
      • SEPS reg.výkon (3-min historian + 1-min realtime, sys_MW = native sign)
      • OKTE DT (15-min, stepped)
      • OKTE ZCO (15-min, len pre D-1 dni; dnes prázdne)
      • OKTE VDT predbežné (15-min)

    FRR aktivácie (aFRR/mFRR) → NaN (SK ich nepublikuje, používa sa CZ proxy z `cz_lf`
    v build_sk_live_minutes pre dnes).

    Vracia DataFrame so schemou matchujúcou `out/imbalance_minute.csv` použitý
    livesim._minute_all().
    """
    import pandas as pd
    import numpy as np

    f = pd.Timestamp(from_date).normalize()
    if isinstance(f, pd.Timestamp) and f.tzinfo:
        f = f.tz_localize(None)
    t = pd.Timestamp(to_date).normalize()
    if isinstance(t, pd.Timestamp) and t.tzinfo:
        t = t.tz_localize(None)

    frames = []
    cur = f
    _total = max(1, int((t - f).days) + 1)
    _i = 0
    while cur <= t:
        d_iso = cur.strftime("%Y-%m-%d")
        if progress_cb is not None:
            try:
                progress_cb(_i, _total, d_iso)        # fáza načítavania dát (pred RT slučkou)
            except Exception:
                pass
        _i += 1
        try:
            day_df = build_sk_live_minutes(today=cur)                 # bez CZ proxy
            if day_df is not None and not day_df.empty:
                frames.append(day_df)
        except Exception as e:
            print(f"[seps_sk.build_sk_minute_history] {d_iso}: {e}")
        cur += pd.Timedelta(days=1)

    if not frames:
        return pd.DataFrame(columns=["time", "ts15", "sys_MW", "aFRR_plus", "aFRR_minus",
                                       "mFRR_plus", "mFRR_minus", "mFRR5", "isot_eur",
                                       "zco_eur", "dt_real_eur", "vdt_eur"])
    out = pd.concat(frames, ignore_index=True).sort_values("time").reset_index(drop=True)
    return out


def build_sk_live_minutes(today: "pd.Timestamp" = None,
                            cz_lf: "pd.DataFrame" = None,
                            cz_zco_map: dict = None) -> "pd.DataFrame":
    """Vráti minútovú mriežku 00:00–23:59 pre dnešok s SK dátami.

    Schéma matchuje _livesim_live_minutes (app.py): [time, ts15, sys_MW,
    aFRR_plus, aFRR_minus, mFRR_plus, mFRR_minus, mFRR5, isot_eur, zco_eur,
    dt_real_eur, vdt_eur].

    SK zdroje:
      • sys_MW           = SEPS reg.výkon (1-min realtime + 3-min historian, BEZ flip)
      • isot_eur         = OKTE DT (C_WEB_OKTE_ISOT_15m, full 24h)
      • dt_real_eur      = OKTE DT (rovnaké, real clearing)
      • zco_eur          = OKTE ZCO (15m, len D-1 publikované → dnes NaN)
      • vdt_eur          = OKTE VDT predbežné (15m, continuous)

    CZ proxy (FRR aktivácie):
      • aFRR_plus/minus, mFRR_plus/minus, mFRR5 — z cz_lf ak je dodaný (CEPS proxy)
    """
    import pandas as pd
    import numpy as np

    if today is None:
        today = pd.Timestamp.now(tz="Europe/Bratislava").normalize().tz_localize(None)
    else:
        today = pd.Timestamp(today)
        if today.tzinfo:
            today = today.tz_localize(None)
    date_iso = today.strftime("%Y-%m-%d")

    # 1-min grid 00:00–23:59 (naive, local-time-like)
    grid = pd.date_range(today, today + pd.Timedelta(hours=24) - pd.Timedelta(minutes=1), freq="1min")
    df = pd.DataFrame({"time": grid})
    df["ts15"] = df["time"].dt.floor("15min")

    # ─── sys_MW: SEPS reg.výkon (už ČEPS-compat flipnuté na vstupe) ────────
    # load_seps_mw_for_day robí ×−1 na vstupe, takže tu len read.
    try:
        mw_df = load_seps_mw_for_day(date_iso)
        if mw_df is not None and not mw_df.empty:
            mw_ser = mw_df.set_index("ts_local")["mw"]
            mw_ser.index = mw_ser.index.tz_localize(None)              # naive
            mw_ser = mw_ser[~mw_ser.index.duplicated(keep="last")].sort_index()
            df = df.merge(mw_ser.rename("sys_MW").reset_index().rename(columns={"ts_local": "time"}),
                            on="time", how="left")
        else:
            df["sys_MW"] = np.nan
    except Exception as _e:
        print(f"[seps_sk.build_sk_live_minutes] sys_MW: {_e}")
        df["sys_MW"] = np.nan

    # ─── DT / VDT / ZCO z OKTE historian (15-min, mapované na ts15) ────────
    dt_map = load_okte_dt_for_day(date_iso)
    vdt_map = load_okte_vdt_preliminary_for_day(date_iso)              # predbežné — continuous
    if not vdt_map:
        vdt_map = load_okte_vdt_for_day(date_iso)                       # fallback na finálne
    zco_map = load_okte_zco_for_day(date_iso)                           # len D-1 (dnes prázdne)

    # ts15 ako string-key pre dictlookup
    df["_ts15_key"] = df["ts15"].dt.strftime("%Y-%m-%d %H:%M:%S")
    df["isot_eur"]    = df["_ts15_key"].map(dt_map)
    df["dt_real_eur"] = df["_ts15_key"].map(dt_map)
    df["vdt_eur"]     = df["_ts15_key"].map(vdt_map)
    df["zco_eur"]     = df["_ts15_key"].map(zco_map)
    df = df.drop(columns="_ts15_key")

    # ─── FRR aktivácie z CZ (proxy) — ak je k dispozícii cz_lf ────────────
    if cz_lf is not None and not cz_lf.empty and "time" in cz_lf.columns:
        try:
            lf = cz_lf.copy()
            lf["time"] = pd.to_datetime(lf["time"]).dt.floor("min")
            if lf["time"].dt.tz is not None:
                lf["time"] = lf["time"].dt.tz_localize(None)
            sig_cols = [c for c in ["aFRR_plus", "aFRR_minus", "mFRR_plus", "mFRR_minus", "mFRR5"]
                        if c in lf.columns]
            df = df.merge(lf[["time"] + sig_cols].drop_duplicates("time"), on="time", how="left")
        except Exception as _e:
            print(f"[seps_sk.build_sk_live_minutes] CZ FRR merge: {_e}")

    # Doplň chýbajúce stĺpce
    for c in ["aFRR_plus", "aFRR_minus", "mFRR_plus", "mFRR_minus", "mFRR5"]:
        if c not in df.columns:
            df[c] = np.nan

    # DT je povinný — bez neho nemá plán zmysel
    df = df.dropna(subset=["isot_eur"])
    if df.empty:
        return None

    cols = ["time", "ts15", "sys_MW", "aFRR_plus", "aFRR_minus", "mFRR_plus",
            "mFRR_minus", "mFRR5", "isot_eur", "zco_eur", "dt_real_eur", "vdt_eur"]
    return df[[c for c in cols if c in df.columns]].reset_index(drop=True)


def probe() -> Dict[str, Any]:
    """Diagnostický helper — spustí fetch + vráti aj surovú odpoveď.

    `python -c "import seps_sk; print(seps_sk.probe())"`
    """
    out = {"timestamp_utc": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat() + "Z"}
    try:
        result = fetch_seps_realtime()
        out["ok"] = bool(result)
        out["values"] = result
        if result:
            out["pretty"] = "  ".join(f"{k}={v}" for k, v in result.items()
                                       if not k.startswith("_"))
    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)
    return out


if __name__ == "__main__":
    import sys
    import pprint

    if len(sys.argv) > 1 and sys.argv[1] == "parse":
        # Test parsera na JSON súbore: python seps_sk.py parse /tmp/seps.json
        with open(sys.argv[2]) as f:
            obj = json.load(f)
        print("Parsed:")
        pprint.pprint(parse_system_state(obj))
    else:
        # Live fetch
        print("Probe SEPS DAE...")
        pprint.pprint(probe())
