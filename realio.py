# -*- coding: utf-8 -*-
"""
realio.py — Real-time I/O pre FTV elektráreň + bateriové úložisko.

Komunikácia s firemným EMS/dashboardom (dashboard-overview) cez ten istý tag-data
pattern ako `internal_historian.py` (Bender / Express.js).

Príklad reálnej inštalácie (Trakany):
    Host:  https://10.200.136.21
    Login: admin / admin
    Endpoint: /tag-data (predpoklad — overí sa pri prvom Test čítania)

Tagy (z reálnej inštalácie, hodnoty vo Watts okrem SOC ktoré je %):
    READ:
      PHV_Aggregated_I_Active_Power      — FTV instant (W)
      PHV_Aggregated_C_Active_Power_1m   — FTV priemer 1m (W)
      PHV_Aggregated_C_Active_Power_15m  — FTV priemer 15m (W)
      ELM1_Aggregated_I_Power            — Elektromer instant (W)
      ELM1_Aggregated_C_Power_1m         — Elektromer priemer 1m (W)
      ELM1_Aggregated_C_Power_f1m        — Elektromer rýchly 1m (W)
      BAT_Aggregated_C_Power             — Batéria instant (W)
      BAT_Aggregated_C_Power_1m          — Batéria priemer 1m (W)
      BAT_Aggregated_C_Power_15m         — Batéria priemer 15m (W)
      BAT_Aggregated_C_SOC               — Batéria SOC (%)
      BAT_Aggregated_C_Capacity          — Batéria kapacita (Wh)
      REG_C_Regulation_Power             — Regulačný výkon absolútna (W)
      REG_Regulation_Filter_Param1       — Filter param 1
      REG_Regulation_Filter_Param3       — Filter param 3
      REG_Regulation_Filter_Offseted_RK  — Filter offset RK
      REG_Regulation_Filter_Offseted_MF  — Filter offset MF

Konfigurácia tagov sa ukladá do `out/<market>/realio_config.json`.

Setpoint write (TBD — keď dostaneme príklad write requestu):
    Pravdepodobne POST /tag-data alebo /api/setpoint s payloadom obsahujúcim
    `{"tag": "...", "value": <kW>, "time": <ms>}` — schéma sa upraví keď user pošle curl.
"""
from __future__ import annotations
import os
import json
import datetime as dt
from typing import Dict, List, Optional, Any
from urllib.parse import quote

import requests
import pandas as pd


# ─── Cesty ───────────────────────────────────────────────────────────────────
def _data_dir() -> str:
    """Per-market data dir (zhodný s ostatnými modulmi)."""
    try:
        import market as _mk
        return _mk.data_dir()
    except Exception:
        return "out"


def _config_path() -> str:
    return os.path.join(_data_dir(), "realio_config.json")


def _csv_path() -> str:
    return os.path.join(_data_dir(), "realio_measurements.csv")


# ─── Konfigurácia ────────────────────────────────────────────────────────────
# Defaultné mapovania logických tagov → reálne tagy v dashboarde.
# Hodnoty z dashboardu sú vo W (treba ÷1000 pre kW). SOC je %.
DEFAULT_TAGS_READ = {
    "ftv_power_kw":  "PHV_Aggregated_C_Active_Power_1m",   # FTV 1-min priemer
    "load_power_kw": "ELM1_Aggregated_C_Power_1m",         # ELM1 elektromer 1m = SIEŤ
    "batt_power_kw": "BAT_Aggregated_C_Power",             # batéria instant (bez _1m sufixu)
    "batt_soc_pct":  "BAT_Aggregated_C_SOC",               # SOC %
    "grid_power_kw": "REG_C_Regulation_Power",             # regulačný výkon (info-only)
}

# Scale faktor: tag → multiplier pre prevod na kW.
# Default 0.001 (W → kW); SOC ostáva 1.0 (%); kapacita 0.001 (Wh → kWh).
DEFAULT_SCALE = {
    "ftv_power_kw":  0.001,
    "load_power_kw": 0.001,
    "batt_power_kw": 0.001,
    "batt_soc_pct":  1.0,
    "grid_power_kw": 0.001,
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "host": "https://10.200.136.21",
    "endpoint_path": "/tag-data",        # cesta na server pre tag query (Bender konvencia)
    "cookies": "",                        # Cookie header string z prehliadača (connect.sid=...; Bender-Authenticate=...)
    "username": "admin",                  # voliteľné (ak skúšame auto-login form)
    "password": "admin",                  # voliteľné; manuálne cookies majú prioritu
    "verify_ssl": False,                  # LAN servery často majú self-signed cert
    "poll_interval_s": 60,
    "enabled": False,
    "control_enabled": False,
    "tags_read":  dict(DEFAULT_TAGS_READ),
    "scale_read": dict(DEFAULT_SCALE),
    # Trakany batt control protokol: dva tagy s rovnakou minútovo zarovnanou časovou značkou
    #   REG_Regulator_Manual_Plan = 2     (enable manual plan mode)
    #   REG_Regulator_Param3      = value_W (setpoint v Wattoch)
    # Aby sa batéria riadila externe, OBA musia byť zapísané v tej istej minúte.
    "tags_write": {
        "batt_setpoint_kw": "REG_Regulator_Param3",         # setpoint vo W (×1000 z kW)
        "batt_control_mode": "REG_Regulator_Manual_Plan",   # mode flag
        "ftv_curtail_kw":   "",                              # voliteľný
    },
    # Manual_Plan enable: user povedal 2, F12 capture (2026-05-31) ukázal 1.
    # Necháme 2 ako default (user vie čo má hodnota znamenať v jeho inštalácii),
    # ale je editovateľné cez UI / config.
    "control_mode_enable_value":  2,    # hodnota Manual_Plan pri AKTIVÁCII externej kontroly
    "control_mode_disable_value": 0,    # hodnota Manual_Plan pri UVOĽNENÍ kontroly (späť na auto)
    "history_tags": [],                  # extra tagy len pre history backfill
    # FVE riadenie cez SSH+Modbus (Huawei SmartLogger).
    # Defaulty z fve_setpoint.py — zápis register 40428 (Active power adjustment by percentage),
    # gain 10: pct=50% → register=500, pct=100% → register=1000, pct=0 → vypnuté FTV.
    "fve_control": {
        "enabled":      False,                  # master switch — pri False sú FVE writes zamietnuté
        "ssh_host":     "10.200.136.21",
        "ssh_user":     "support",
        "ssh_port":     8222,
        "ssh_key":      "~/.ssh/support.rsa",
        "device_ip":    "192.168.1.250",        # IP SmartLoggera z pohľadu jump hosta
        "modpoll":      "modpoll",              # cesta k modpoll na jump hoste
        "slave_id":     0,                       # modpoll -a<slave_id>
        "ctrl_register": 40428,                  # riadiaci register Huawei SmartLogger
        "gain":         10,                      # zápis = pct × gain
    },
    "last_write": {
        "batt_setpoint_kw": None, "batt_setpoint_kw_ts": None, "batt_setpoint_kw_source": None,
        "ftv_curtail_kw":   None, "ftv_curtail_kw_ts":   None, "ftv_curtail_kw_source":   None,
        "fve_pct":          None, "fve_pct_ts":          None, "fve_pct_source":          None,
    },
}


def _fresh_default() -> Dict[str, Any]:
    """Vráti hlbokú kópiu defaultného configu."""
    return {
        **DEFAULT_CONFIG,
        "tags_read":   dict(DEFAULT_TAGS_READ),
        "scale_read":  dict(DEFAULT_SCALE),
        "tags_write":  dict(DEFAULT_CONFIG["tags_write"]),
        "fve_control": dict(DEFAULT_CONFIG["fve_control"]),
        "last_write":  dict(DEFAULT_CONFIG["last_write"]),
    }


def load_config() -> Dict[str, Any]:
    """Načíta config alebo vráti default. Pri korupcii vráti default."""
    p = _config_path()
    if not os.path.exists(p):
        return _fresh_default()
    try:
        with open(p) as fh:
            d = json.load(fh)
        out = _fresh_default()
        out.update({k: v for k, v in d.items() if k not in
                    ("tags_read", "scale_read", "tags_write", "fve_control", "last_write")})
        if isinstance(d.get("tags_read"), dict):
            out["tags_read"] = {**DEFAULT_TAGS_READ, **d["tags_read"]}
        if isinstance(d.get("scale_read"), dict):
            out["scale_read"] = {**DEFAULT_SCALE, **d["scale_read"]}
        if isinstance(d.get("tags_write"), dict):
            out["tags_write"] = {**DEFAULT_CONFIG["tags_write"], **d["tags_write"]}
        if isinstance(d.get("fve_control"), dict):
            out["fve_control"] = {**DEFAULT_CONFIG["fve_control"], **d["fve_control"]}
        if isinstance(d.get("last_write"), dict):
            out["last_write"] = {**DEFAULT_CONFIG["last_write"], **d["last_write"]}
        return out
    except (OSError, json.JSONDecodeError):
        return _fresh_default()


def save_config(cfg: Dict[str, Any]) -> None:
    """Atomické uloženie configu."""
    os.makedirs(_data_dir(), exist_ok=True)
    p = _config_path()
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


# ─── HTTP klient (session-based, podporuje login + self-signed SSL) ──────────
_SESSION: Optional[requests.Session] = None
_SESSION_HOST: Optional[str] = None


def _parse_csrf_token(html: str) -> Optional[str]:
    """Vytiahne hodnotu csrfmiddlewaretoken / _csrf hidden inputu z HTML form-y."""
    import re
    # Django: <input type="hidden" name="csrfmiddlewaretoken" value="abc123">
    for pat in (r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)["\']',
                r'name=["\']_csrf["\']\s+value=["\']([^"\']+)["\']',
                r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']'):
        m = re.search(pat, html, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _try_login_django(s: requests.Session, host: str, user: str, pwd: str,
                       verify_ssl: bool) -> Dict[str, Any]:
    """Django/Flask form-based login flow:
      1. GET /login/ → CSRF cookie + token z HTML
      2. POST /login/ (form-encoded) s username+password+csrfmiddlewaretoken
    Vracia diag dict: {ok, msg, status_get, status_post, csrf_token, final_url}.
    """
    result = {"ok": False, "msg": "", "status_get": None, "status_post": None,
              "csrf_token": None, "final_url": ""}
    login_url = host.rstrip("/") + "/login/"
    headers_login = {"Referer": login_url, "Origin": host.rstrip("/")}
    try:
        r = s.get(login_url, timeout=15, verify=verify_ssl,
                  headers={"User-Agent": "Mozilla/5.0 RealIO/1.0"})
        result["status_get"] = r.status_code
        token = _parse_csrf_token(r.text)
        # niektoré frameworky majú CSRF len v cookie (Django: csrftoken)
        if not token:
            token = (s.cookies.get("csrftoken") or s.cookies.get("XSRF-TOKEN")
                     or s.cookies.get("_csrf"))
        result["csrf_token"] = token
    except Exception as e:
        result["msg"] = f"GET /login/ zlyhal: {e}"
        return result
    # POST login form
    payload = {"username": user, "password": pwd, "next": "/tag-data", "continue": "tag-data"}
    if token:
        payload["csrfmiddlewaretoken"] = token
    try:
        r = s.post(login_url, data=payload, timeout=15, verify=verify_ssl,
                   headers=headers_login, allow_redirects=True)
        result["status_post"] = r.status_code
        result["final_url"] = r.url
        # Úspech: server redirect na inú stránku ako /login/ alebo 200 s rozumným payloadom
        if r.status_code < 400 and "/login" not in r.url.lower():
            result["ok"] = True
            result["msg"] = f"login OK (HTTP {r.status_code}, redirect {r.url})"
        elif r.status_code < 400:
            # 200 ale ostal na login — credentials sú zle alebo iný flow
            result["msg"] = f"login form ostal na /login/ (HTTP {r.status_code}) — pravdepodobne zlé creds"
        else:
            result["msg"] = f"POST /login/ HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        result["msg"] = f"POST /login/ zlyhal: {e}"
    return result


def _apply_cookie_string(s: requests.Session, host: str, cookie_str: str) -> int:
    """Parsuje 'a=1; b=2' a vloží do session.cookies pre daný host.
    Vracia počet úspešne pridaných cookies."""
    host_no_proto = host.replace("https://", "").replace("http://", "").split(":")[0].split("/")[0]
    n = 0
    for chunk in (cookie_str or "").split(";"):
        chunk = chunk.strip()
        if "=" in chunk:
            name, val = chunk.split("=", 1)
            s.cookies.set(name.strip(), val.strip(), domain=host_no_proto)
            n += 1
    return n


def _get_session(cfg: Dict[str, Any]) -> requests.Session:
    """Vráti requests.Session pre tento host. Auth poradie:
      1) Manuálne cookies (cfg['cookies'])  — RECOMMENDED pre Bender Express.js
      2) Django/Flask form login (cfg['username']+'password')  — fallback (často nefunguje
         lebo každý Bender setup má iný login form)
    Re-použije session ak je rovnaký host."""
    global _SESSION, _SESSION_HOST
    host = cfg.get("host", "").rstrip("/")
    if _SESSION is not None and _SESSION_HOST == host:
        return _SESSION
    s = requests.Session()
    s.verify = bool(cfg.get("verify_ssl", False))
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.6",
        "Referer": host + "/",
    })
    if not s.verify:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
    # 1) Manuálne cookies majú prioritu (najspoľahlivejšia metóda)
    cookie_str = cfg.get("cookies") or ""
    if cookie_str.strip():
        n = _apply_cookie_string(s, host, cookie_str)
        print(f"[realio] použité manuálne cookies ({n} hodnôt)")
    else:
        # 2) Fallback: Django/Flask form login (často nefunguje — len pokus)
        user = cfg.get("username") or ""
        pwd = cfg.get("password") or ""
        if user and pwd:
            diag = _try_login_django(s, host, user, pwd, s.verify)
            if diag["ok"]:
                print(f"[realio] {diag['msg']}")
            else:
                print(f"[realio] auto-login zlyhal: {diag['msg']}")
                s.__dict__["_realio_login_diag"] = diag
    _SESSION = s
    _SESSION_HOST = host
    return s


def login_diagnose() -> Dict[str, Any]:
    """Force-test login flow. Vyčistí session a skúsi sa znova prihlásiť. Pre /realio Test čítania."""
    global _SESSION, _SESSION_HOST
    _SESSION = None
    _SESSION_HOST = None
    cfg = load_config()
    s = requests.Session()
    s.verify = bool(cfg.get("verify_ssl", False))
    s.headers.update({"User-Agent": "Mozilla/5.0 RealIO/1.0"})
    if not s.verify:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
    host = cfg.get("host", "").rstrip("/")
    diag = _try_login_django(s, host, cfg.get("username", ""), cfg.get("password", ""), s.verify)
    return diag


# ─── Čítanie (latest + history) ──────────────────────────────────────────────
def _is_in_asyncio_loop() -> bool:
    """Detekuje či sme vnútri bežiaceho asyncio event loopu (FastAPI handler)."""
    try:
        import asyncio
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _try_auto_relogin() -> bool:
    """Skús auto-refresh cookies cez realio_login (Playwright). Vracia True ak prešlo.

    POZOR: Playwright sync API NEFUNGUJE v async kontexte (FastAPI handler).
    Ak sme v event loope, preskočíme auto-relogin a vrátime False — user musí
    kliknúť „Refresh cookies" v UI (ten ide cez asyncio.to_thread).
    Scheduler job beží v BackgroundScheduler vlákne (nie asyncio) → tam funguje OK.
    """
    if _is_in_asyncio_loop():
        print("[realio] auto-relogin preskočený (v async kontexte — použi /realio/relogin tlačidlo)")
        return False
    global _SESSION, _SESSION_HOST
    try:
        import realio_login
        ok, msg = realio_login.login_once(verbose=False)
        if ok:
            print(f"[realio] auto-relogin OK — {msg}")
            _SESSION = None
            _SESSION_HOST = None
            return True
        print(f"[realio] auto-relogin zlyhal — {msg}")
    except Exception as e:
        print(f"[realio] auto-relogin exception: {e}")
    return False


def _fetch_latest_via_bender(cfg: Dict[str, Any], tags: List[str],
                                return_raw: bool = False) -> Dict[str, Any]:
    """Čítanie cez Bender pattern: GET ?json={requests:[{type:1, params:{tags,...}}]}
    Vracia dict tag → value (raw, bez scale).
    Ak return_raw=True, vráti dict {"_raw": body, "_url": url, "_values": {tag:...}}.
    Pri 401/403 sa pokúsi auto-relogin cez Playwright."""
    host = cfg.get("host", "").rstrip("/")
    path = cfg.get("endpoint_path", "/tag-data") or "/tag-data"
    s = _get_session(cfg)
    req = {"requests": [{"type": 1, "params": {"tags": tags, "inclusive": True, "time": None}}]}
    url = f"{host}{path}?json={quote(json.dumps(req))}"
    r = s.get(url, timeout=15, verify=s.verify)
    # Auth zlyhanie → auto-relogin a 1× retry
    if r.status_code in (401, 403) or (r.status_code == 200 and "/login" in r.url.lower()):
        if _try_auto_relogin():
            # Re-load cfg (cookies sa zmenili) + nová session
            cfg = load_config()
            s = _get_session(cfg)
            url = f"{host}{path}?json={quote(json.dumps(req))}"
            r = s.get(url, timeout=15, verify=s.verify)
    r.raise_for_status()
    body = r.json()
    out: Dict[str, Any] = {}
    # ── Skús viacero schém parsovania (Bender server môže používať varianty) ──
    # Schéma A (klasická Bender): [ [ {values:[{last:{value,time},...}]} ] ]
    # Schéma B: [ [ {value, time} ] ]  alebo  [{tag: ..., value: ..., time: ...}]
    # Schéma C: {results: [...]} alebo {data: [...]}
    def _extract_value_from_slot(slot):
        """Skús extrahovať skalárnu hodnotu zo slot objektu — robustne."""
        if slot is None:
            return None
        if not isinstance(slot, dict):
            return None
        # Schéma 10.200.136.21 (Trakany Bender): {result:{value:{name,value,time,metadata}},error}
        res = slot.get("result")
        if isinstance(res, dict):
            rv = res.get("value")
            if isinstance(rv, dict) and "value" in rv:
                try: return float(rv["value"])
                except (TypeError, ValueError): pass
            if not isinstance(rv, (dict, list)) and rv is not None:
                try: return float(rv)
                except (TypeError, ValueError): pass
        # priame skalárne {value: X} alebo {v: X}
        for k in ("value", "v", "val"):
            if k in slot and not isinstance(slot[k], (dict, list)):
                try: return float(slot[k])
                except (TypeError, ValueError): pass
        # Klasická Bender (192.168.34.31): {values:[{first:{value},last:{value}}, ...]}
        vals = slot.get("values")
        if isinstance(vals, list) and vals:
            last = vals[-1]
            if isinstance(last, dict):
                for k in ("last", "first"):
                    p = last.get(k)
                    if isinstance(p, dict) and "value" in p:
                        try: return float(p["value"])
                        except (TypeError, ValueError): pass
                for k in ("value", "v"):
                    if k in last and not isinstance(last[k], (dict, list)):
                        try: return float(last[k])
                        except (TypeError, ValueError): pass
        # nested: {first:{value}} priamo na slot úrovni
        for k in ("last", "first", "latest"):
            p = slot.get(k)
            if isinstance(p, dict) and "value" in p:
                try: return float(p["value"])
                except (TypeError, ValueError): pass
        return None

    # body = [ [...] ] alebo body = [...] alebo body = {results:[...]}
    request_results = None
    if isinstance(body, list):
        request_results = body
    elif isinstance(body, dict):
        for k in ("results", "data", "responses", "response"):
            if k in body and isinstance(body[k], list):
                request_results = body[k]
                break

    if request_results and isinstance(request_results[0], list):
        # outer list = per-request; my máme len 1 request → vezmi prvý
        per_tag = request_results[0]
        for tag, slot in zip(tags, per_tag):
            out[tag] = _extract_value_from_slot(slot)
    elif request_results and isinstance(request_results[0], dict):
        # plochá list of slots
        for tag, slot in zip(tags, request_results):
            out[tag] = _extract_value_from_slot(slot)
    # ak nič nesedí, out zostane prázdny — diagnostika to ukáže

    if return_raw:
        return {"_raw": body, "_url": url, "_values": out}
    return out


def fetch_latest_all() -> Optional[Dict[str, Optional[float]]]:
    """Stiahne posledné hodnoty pre VŠETKY nakonfigurované read tagy naraz.
    Aplikuje scale (W→kW). Vracia dict s logickými kľúčmi alebo None ak modul disabled."""
    cfg = load_config()
    if not cfg.get("enabled"):
        return None
    tag_map = {k: v for k, v in (cfg.get("tags_read") or {}).items() if v}
    if not tag_map:
        return {}
    scale_map = cfg.get("scale_read") or {}
    try:
        raw = _fetch_latest_via_bender(cfg, list(tag_map.values()))
    except Exception as e:
        print(f"[realio.fetch_latest_all] HTTP zlyhalo: {e}")
        return {"_error": str(e)}
    out: Dict[str, Optional[float]] = {}
    for logical, tag_name in tag_map.items():
        v = raw.get(tag_name)
        if v is None:
            out[logical] = None
        else:
            scale = float(scale_map.get(logical, 1.0))
            out[logical] = float(v) * scale
    out["_ts"] = dt.datetime.now().isoformat(timespec="seconds")
    return out


def _fetch_history_single_tag(s, host: str, path: str, tag_name: str,
                                from_dt: dt.datetime, to_dt: dt.datetime,
                                count: int, _diag: dict = None) -> list:
    """Fetch history pre jeden tag. Vracia list of {time, value} dictov.
    Retry s session reset pri ConnectionResetError."""
    req = {"requests": [{"type": 2, "params": {
        "tags": [tag_name],
        "from": int(from_dt.timestamp() * 1000),
        "to":   int(to_dt.timestamp() * 1000),
        "count": int(count),
    }}]}
    url = f"{host}{path}?json={quote(json.dumps(req))}"
    last_err = None
    for attempt in range(2):
        try:
            r = s.get(url, timeout=60, verify=s.verify)
            # Ulož raw response sample do diag (len prvý tag — aby sme nemali príliš veľký diag)
            if _diag is not None and "raw_sample" not in _diag:
                _diag["raw_sample"] = (r.text or "")[:2000]
                _diag["raw_sample_tag"] = tag_name
                _diag["raw_sample_http"] = r.status_code
            r.raise_for_status()
            body = r.json()
            # Parse response
            if isinstance(body, list):
                request_results = body
            elif isinstance(body, dict):
                request_results = None
                for k in ("results", "data", "responses", "response"):
                    if k in body and isinstance(body[k], list):
                        request_results = body[k]
                        break
            else:
                request_results = None
            if not request_results or not isinstance(request_results[0], list):
                if _diag is not None:
                    _diag.setdefault("per_tag_errors", {})[tag_name] = (
                        f"no request_results (body_type={type(body).__name__})")
                return []
            per_request = request_results[0]
            if not per_request:
                if _diag is not None:
                    _diag.setdefault("per_tag_errors", {})[tag_name] = "per_request empty"
                return []
            slot = per_request[0]
            # Diag: prvý slot sample (aby sme videli strukturu)
            if _diag is not None and "first_slot_full" not in _diag:
                _diag["first_slot_full"] = str(slot)[:1500]
                _diag["first_slot_keys"] = list(slot.keys()) if isinstance(slot, dict) else []
            pts = []
            # Najprv extrahuj `result` ak existuje, inak slot priamo (klasický Bender)
            res = slot.get("result") if isinstance(slot, dict) else None
            container = res if isinstance(res, dict) else (slot if isinstance(slot, dict) else {})
            if _diag is not None and "res_keys" not in _diag:
                _diag["res_keys"] = list(container.keys()) if isinstance(container, dict) else []
            # Schéma C (Trakany type:2): result.values = [{begin, end, first:{value,time}, last, maxValue, minValue}, ...]
            #   → vezmi 'first' (alebo 'last') ako bod
            res_values = container.get("values") if isinstance(container, dict) else None
            if isinstance(res_values, list):
                if _diag is not None and "rv_type_first" not in _diag:
                    _diag["rv_type_first"] = "result.values (Trakany history bucket)"
                for entry in res_values:
                    if not isinstance(entry, dict):
                        continue
                    pt = entry.get("first") or entry.get("last") or {}
                    t = pt.get("time") if isinstance(pt, dict) else None
                    v = pt.get("value") if isinstance(pt, dict) else None
                    if t is not None and v is not None:
                        pts.append({"time": int(t), "value": float(v)})
                return pts
            # Schéma A (Trakany type:1 list): result.value = [{name, value, time}, ...]
            res_value = container.get("value") if isinstance(container, dict) else None
            if isinstance(res_value, list):
                if _diag is not None and "rv_type_first" not in _diag:
                    _diag["rv_type_first"] = "result.value (list of {name,value,time})"
                for pt in res_value:
                    if isinstance(pt, dict) and pt.get("time") is not None and pt.get("value") is not None:
                        pts.append({"time": int(pt["time"]), "value": float(pt["value"])})
                return pts
            # Ani jedna známa schéma — ulož aspoň diag
            if _diag is not None:
                _diag.setdefault("per_tag_errors", {})[tag_name] = (
                    f"unknown schema; container keys: "
                    f"{list(container.keys()) if isinstance(container, dict) else type(container).__name__}")
            return []
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError) as e:
            last_err = e
            # Reset session a skús znova (možno expirované cookies)
            if attempt == 0:
                global _SESSION, _SESSION_HOST
                _SESSION = None
                _SESSION_HOST = None
                cfg = load_config()
                s = _get_session(cfg)
                print(f"[realio.fetch_history] {tag_name}: connection reset, session reset + retry")
                continue
            break
        except Exception as e:
            last_err = e
            break
    if _diag is not None:
        _diag.setdefault("per_tag_errors", {})[tag_name] = f"{type(last_err).__name__}: {last_err}"
    return []


def fetch_history_range(from_dt: dt.datetime, to_dt: dt.datetime,
                          count: int = 0,
                          include_extra_tags: bool = True,
                          _diag: dict = None) -> pd.DataFrame:
    """Stiahne históriu pre nakonfigurované tagy. Vracia wide DataFrame (jeden stĺpec na logický tag,
    hodnoty už v kW podľa scale).

    **Per-tag fetch:** jeden HTTP request per tag (5 menších namiesto 1 veľkého) — vyhne
    sa Connection reset by peer pri veľkých response bodies. Pri reset tries 1× znova
    s fresh session.

    count: 0 = server vyberie defaultný binning; inak max počet bodov per tag.
    """
    cfg = load_config()
    tag_map = {k: v for k, v in (cfg.get("tags_read") or {}).items() if v}
    if include_extra_tags:
        for t in (cfg.get("history_tags") or []):
            if t:
                tag_map[t] = t
    if not tag_map:
        if _diag is not None:
            _diag["err"] = "tag_map je prázdne (nie sú nakonfigurované tags_read)"
        return pd.DataFrame(columns=["time"])
    host = cfg.get("host", "").rstrip("/")
    path = cfg.get("endpoint_path", "/tag-data") or "/tag-data"
    s = _get_session(cfg)
    if _diag is not None:
        _diag["tags_count"] = len(tag_map)
        _diag["from_ms"] = int(from_dt.timestamp() * 1000)
        _diag["to_ms"] = int(to_dt.timestamp() * 1000)
        _diag["per_tag_rows"] = {}

    scale_map = cfg.get("scale_read") or {}
    rows = []
    # Per-tag fetch (5 menších requestov)
    for logical, tag_name in tag_map.items():
        scale = float(scale_map.get(logical, 1.0))
        pts = _fetch_history_single_tag(s, host, path, tag_name,
                                          from_dt, to_dt, count, _diag=_diag)
        if _diag is not None:
            _diag["per_tag_rows"][tag_name] = len(pts)
        for pt in pts:
            # Bender vracia unix ms (UTC). Explicitne tz-aware UTC, aby realio_db._to_ms
            # vedel rozpoznať pôvod (UTC) a nepokúsil sa parsovať ako local CEST.
            rows.append({"time": pd.to_datetime(pt["time"], unit="ms", utc=True),
                         "logical": logical, "value": pt["value"] * scale})

    if _diag is not None:
        _diag["rows_parsed"] = len(rows)
    if not rows:
        if _diag is not None:
            errs = _diag.get("per_tag_errors", {})
            _diag["err"] = (f"rows je prázdne — žiadne valid data body pre všetkých {len(tag_map)} tagov. "
                             f"Per-tag chyby: {errs}" if errs else
                             "rows je prázdne — žiadne valid data body sa nenašli v response")
        return pd.DataFrame(columns=["time"])
    df_long = pd.DataFrame(rows)
    wide = df_long.pivot_table(index="time", columns="logical", values="value", aggfunc="mean")
    return wide.reset_index().sort_values("time")


def backfill_range_to_csv(from_dt: dt.datetime, to_dt: dt.datetime,
                           count_per_tag: int = 10000,
                           max_days: float = 2.0,
                           overwrite: bool = False) -> Dict[str, Any]:
    """Stiahne históriu pre interval [from_dt, to_dt] a append-ne do realio_measurements.csv.

    Bezpečnostná poistka: rozsah > max_days dní vráti error (zabraňuje preťaženiu
    Bender servera pri pokuse stiahnuť mesiace dát). Default 2 dni.

    Vracia: {ok, rows_added, period_from, period_to, msg}.
    """
    cfg = load_config()
    if not cfg.get("enabled"):
        return {"ok": False, "msg": "modul nie je enabled", "rows_added": 0}
    if from_dt >= to_dt:
        return {"ok": False, "msg": "from_dt musí byť pred to_dt", "rows_added": 0}
    span = (to_dt - from_dt).total_seconds() / 86400.0
    if span > max_days:
        return {"ok": False, "rows_added": 0,
                 "msg": (f"Rozsah {span:.2f} dní presahuje limit {max_days} dní. "
                         f"Skús menší interval (Bender server by mohol odmietnuť alebo crashnúť).")}
    _diag = {}
    df = fetch_history_range(from_dt, to_dt, count=int(count_per_tag),
                              include_extra_tags=False, _diag=_diag)
    result = _merge_and_write_history(df, from_dt, to_dt, overwrite_range=overwrite)
    # Pri zlyhaní zachyť diag info do msg pre debugging
    if not result.get("ok") and _diag:
        diag_parts = []
        if "http_status" in _diag:
            diag_parts.append(f"HTTP {_diag['http_status']}")
        if "tags_count" in _diag:
            diag_parts.append(f"tagov={_diag['tags_count']}")
        if "body_type" in _diag:
            diag_parts.append(f"body_type={_diag['body_type']}")
        if "per_tag_rows" in _diag:
            diag_parts.append(f"per_tag_rows={_diag['per_tag_rows']}")
        if "rows_parsed" in _diag:
            diag_parts.append(f"rows_parsed={_diag['rows_parsed']}")
        if "raw_sample_http" in _diag:
            diag_parts.append(f"HTTP {_diag['raw_sample_http']}")
        if "first_slot_keys" in _diag:
            diag_parts.append(f"slot_keys={_diag['first_slot_keys']}")
        if "rv_type_first" in _diag:
            diag_parts.append(f"rv_type={_diag['rv_type_first']}")
        if "res_keys" in _diag:
            diag_parts.append(f"res_keys={_diag['res_keys']}")
        if "first_slot_full" in _diag:
            diag_parts.append(f"slot_sample={_diag['first_slot_full'][:500]}")
        if "raw_sample" in _diag and "first_slot_full" not in _diag:
            diag_parts.append(f"raw={_diag['raw_sample'][:500]}")
        if "per_tag_errors" in _diag:
            diag_parts.append(f"per_tag_errors={_diag['per_tag_errors']}")
        if "err" in _diag:
            diag_parts.append(f"err={_diag['err']}")
        result["diag"] = " | ".join(diag_parts)
        result["msg"] = (result.get("msg", "")
                          + " | Diag: " + result["diag"])
    return result


def _merge_and_write_history(df, from_dt: dt.datetime, to_dt: dt.datetime,
                               overwrite_range: bool = False) -> Dict[str, Any]:
    """Helper — vezmie DataFrame z fetch_history_range a UPSERT-ne do SQLite.

    Per-stĺpec UPSERT s COALESCE — pri partial dátach (1 tag) zachová existing
    hodnoty ostatných stĺpcov. Tým sa rieši pôvodný 90% NaN problém z per-tag
    backfill v CSV verzii.

    `overwrite_range=True` → najprv DELETE riadky v rozsahu, potom UPSERT.
    """
    if df is None or df.empty:
        return {"ok": False, "msg": "history fetch vrátil prázdne dáta", "rows_added": 0,
                "period_from": from_dt.isoformat(timespec="seconds"),
                "period_to":   to_dt.isoformat(timespec="seconds")}
    try:
        import realio_db as _db
    except ImportError:
        return {"ok": False, "msg": "realio_db modul nedostupný", "rows_added": 0}
    _ensure_db_migrated()
    rows_removed = 0
    if overwrite_range:
        # DELETE v rozsahu pred UPSERT — vyplní diery, nahradí staré neúplné body
        try:
            c = _db._conn()
            try:
                f_ms = int(pd.Timestamp(from_dt).timestamp() * 1000)
                t_ms = int(pd.Timestamp(to_dt).timestamp() * 1000)
                cur = c.execute(
                    "DELETE FROM realio_measurements WHERE time_ms BETWEEN ? AND ?",
                    (f_ms, t_ms)
                )
                rows_removed = cur.rowcount or 0
            finally:
                c.close()
        except Exception as e:
            print(f"[realio._merge] DELETE zlyhalo: {e}")
    # Per-row UPSERT — pre každý riadok DataFrame
    rows_added = 0
    for _, row in df.iterrows():
        t = row.get("time")
        if t is None or pd.isna(t):
            continue
        vals = {}
        for col in ("ftv_power_kw", "load_power_kw", "load_power_kw_15m",
                     "batt_power_kw", "batt_soc_pct", "grid_power_kw"):
            v = row.get(col)
            if v is not None and pd.notna(v):
                try:
                    vals[col] = float(v)
                except (TypeError, ValueError):
                    pass
        try:
            _db.insert_row(t, vals)
            rows_added += 1
        except Exception as e:
            print(f"[realio._merge] UPSERT zlyhalo pre {t}: {e}")
    total_after = _db.count_rows()
    span_days = (to_dt - from_dt).total_seconds() / 86400.0
    msg_extra = f", prepísaných {rows_removed} starých" if overwrite_range and rows_removed > 0 else ""
    return {"ok": True, "rows_added": int(rows_added), "rows_removed": int(rows_removed),
            "period_from": from_dt.isoformat(timespec="seconds"),
            "period_to":   to_dt.isoformat(timespec="seconds"),
            "msg": f"Backfill {span_days:.2f} dní ({from_dt:%Y-%m-%d %H:%M} → "
                    f"{to_dt:%Y-%m-%d %H:%M}): UPSERT {rows_added} riadkov{msg_extra} "
                    f"(DB celkom {total_after})"}


def backfill_to_csv(days: int = 7, count_per_tag: int = 10000) -> Dict[str, Any]:
    """Stiahne posledných N dní histórie a append-ne do realio_measurements.csv (dedup podľa time).
    Vracia dict: {ok, rows_added, period_from, period_to, msg}."""
    cfg = load_config()
    if not cfg.get("enabled"):
        return {"ok": False, "msg": "modul nie je enabled", "rows_added": 0}
    to_dt = dt.datetime.now()
    from_dt = to_dt - dt.timedelta(days=int(days))
    df = fetch_history_range(from_dt, to_dt, count=int(count_per_tag), include_extra_tags=False)
    return _merge_and_write_history(df, from_dt, to_dt)


def poll_status() -> Dict[str, Any]:
    """Vráti info o stave periodického pollingu — či CSV existuje, posledný timestamp, počet riadkov."""
    p = _csv_path()
    cfg = load_config()
    out = {"enabled": bool(cfg.get("enabled")),
            "control_enabled": bool(cfg.get("control_enabled")),
            "poll_interval_s": int(cfg.get("poll_interval_s", 60)),
            "csv_path": p, "csv_exists": os.path.exists(p),
            "last_ts": None, "rows": 0, "minutes_since_last": None}
    if not os.path.exists(p):
        return out
    try:
        df = pd.read_csv(p, usecols=["time"])
        out["rows"] = len(df)
        if not df.empty:
            last = pd.to_datetime(df["time"].iloc[-1], errors="coerce")
            if pd.notna(last):
                out["last_ts"] = last.isoformat(timespec="seconds")
                delta = dt.datetime.now() - last.to_pydatetime()
                out["minutes_since_last"] = round(delta.total_seconds() / 60.0, 1)
    except Exception as e:
        out["error"] = str(e)
    return out


# ─── Zápis (setpoint) ────────────────────────────────────────────────────────
def _minute_aligned_ms() -> int:
    """Vráti unix-ms zarovnaný na začiatok aktuálnej minúty (sekundy=0, microsekundy=0)."""
    now = dt.datetime.now()
    aligned = now.replace(second=0, microsecond=0)
    return int(aligned.timestamp() * 1000)


def _send_tag_writes(cfg: Dict[str, Any], writes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pošle write request na Bender dashboard.

    Protokol (zistený z F12 capture dashboardu, 2026-05-31):
      POST <host>/tag-set-new-values
      Content-Type: application/json
      Headers: Origin, Referer (rovnaký host)
      Body: {"tags": [
                {"tag": "<NAME>", "values": [{"time": <unix_ms>, "value": <num>}]},
                ...
              ]}

    Server odpovedá HTTP 200 s krátkym JSON ({"errors":[]} alebo podobne) pri úspechu.

    Vstup `writes`: List[{"tag": str, "value": float, "time": int(ms)}]
    Výstup: {"ok": bool, "msg": str, "writes_done": [tag,...], "errors": [...], "tried": [...]}.
    """
    host = cfg.get("host", "").rstrip("/")
    s = _get_session(cfg)
    out = {"ok": False, "msg": "", "writes_done": [], "errors": [], "tried": []}
    if not writes:
        out["msg"] = "nič na zápis"
        return out

    # Zoskupiť writes podľa tag-u → každý tag dostane svoj 'values' array.
    # Trakany dual-write má 2 rôzne tagy s rovnakým timestampom — server prijíma
    # všetky tagy v jednom POST.
    from collections import defaultdict as _dd
    by_tag: Dict[str, List[Dict[str, Any]]] = _dd(list)
    for w in writes:
        by_tag[w["tag"]].append({"time": int(w["time"]), "value": w["value"]})

    body = {"tags": [{"tag": tg, "values": vs} for tg, vs in by_tag.items()]}
    url = f"{host}/tag-set-new-values"
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin":  host,
        "Referer": host + "/dashboard-overview",
    }

    try:
        r = s.post(url, json=body, headers=headers, timeout=15, verify=s.verify)
        resp_body = (r.text or "")[:300]
        out["tried"].append(f"POST {url} → HTTP {r.status_code} · {resp_body}")
        r.raise_for_status()
        # Skontroluj že response neobsahuje errors: [...nejaké...]
        try:
            j = r.json() if r.text else {}
            errs = j.get("errors") if isinstance(j, dict) else None
            if isinstance(errs, list) and errs:
                out["msg"] = f"Server vrátil errors: {errs[:3]}"
                out["errors"].extend(str(e) for e in errs[:5])
                return out
        except Exception:
            pass
        out["ok"] = True
        out["writes_done"] = list(by_tag.keys())
        out["msg"] = f"OK — HTTP {r.status_code} ({len(by_tag)} tag(ov), body {len(resp_body)} B)"
        return out
    except Exception as e:
        out["errors"].append(f"POST {url}: {e}")
        out["msg"] = f"POST /tag-set-new-values zlyhal: {e}"
        return out


def write_setpoint(logical_name: str, value: float, source: str = "manual") -> Dict[str, Any]:
    """Pošle setpoint command na server.

    Pre `logical_name='batt_setpoint_kw'` vykonáva DUAL-WRITE protokol (Trakany):
        1. REG_Regulator_Manual_Plan = 2   (enable manual mode)
        2. REG_Regulator_Param3      = value × 1000   (setpoint W)
       Oba s ROVNAKOU minútovo zarovnanou časovou značkou.

    Pre ostatné logické tagy single write.
    """
    cfg = load_config()
    out: Dict[str, Any] = {"ok": False, "tag": "", "value": float(value), "msg": ""}
    if not cfg.get("enabled"):
        out["msg"] = "realio modul nie je enabled v configu"
        return out
    if not cfg.get("control_enabled"):
        out["msg"] = "control_enabled je FALSE — write je zakázaný (bezpečnostná poistka)"
        return out
    tags_w = cfg.get("tags_write") or {}
    setpoint_tag = tags_w.get(logical_name)
    if not setpoint_tag:
        out["msg"] = f"logical tag '{logical_name}' nemá nakonfigurovaný server tag (tags_write)"
        return out
    out["tag"] = setpoint_tag
    # Minútovo zarovnaný timestamp — OBA writes ho zdieľajú
    ts_ms = _minute_aligned_ms()
    # Setpoint hodnota: kW → W (×1000)
    raw_value = float(value) * 1000.0 if logical_name.endswith("_kw") else float(value)
    # Postaviť write list
    writes = []
    # Dual-write: ak je to batt setpoint, pridaj aj Manual_Plan mode
    if logical_name == "batt_setpoint_kw":
        mode_tag = tags_w.get("batt_control_mode")
        mode_val = float(cfg.get("control_mode_enable_value", 2))
        if mode_tag:
            writes.append({"tag": mode_tag, "value": mode_val, "time": ts_ms})
    writes.append({"tag": setpoint_tag, "value": raw_value, "time": ts_ms})
    # Odoslať
    res = _send_tag_writes(cfg, writes)
    out["ok"] = res["ok"]
    if res["ok"]:
        cfg.setdefault("last_write", {})
        cfg["last_write"][logical_name] = float(value)
        cfg["last_write"][f"{logical_name}_ts"] = dt.datetime.fromtimestamp(ts_ms/1000).isoformat(timespec="seconds")
        cfg["last_write"][f"{logical_name}_source"] = str(source)
        if logical_name == "batt_setpoint_kw":
            cfg["last_write"]["batt_control_mode"] = mode_val if 'mode_val' in dir() else 2
            cfg["last_write"]["batt_control_mode_ts"] = cfg["last_write"][f"{logical_name}_ts"]
        save_config(cfg)
        ts_str = dt.datetime.fromtimestamp(ts_ms/1000).strftime("%Y-%m-%d %H:%M:00")
        n_tags = len(res["writes_done"])
        out["msg"] = (f"✓ {res['msg']} — {n_tags} tag(ov) @ {ts_str}, "
                       f"raw={raw_value:.0f} W ({value:.2f} kW)")
    else:
        # Vráť clean msg + tried/errors v dictu — UI vrstva formátuje diag
        out["msg"] = res.get("msg") or "write zlyhal — všetky schémy"
        out["tried"] = res.get("tried", [])
        out["errors"] = res.get("errors", [])
    return out


def write_regfilter_param1(value_w: float, source: str = "regfilter_plan",
                             force: bool = False) -> Dict[str, Any]:
    """REGFILTER (2026-07-08): zapíše REG_Regulation_Filter_Param1 (max odber zo siete, vo W)
    na Bender. Hodnota je PRIAMO vo W (bez ×1000, na rozdiel od batt_setpoint_kw).

    CHANGE-ONLY: ak je hodnota rovnaká ako naposledy ÚSPEŠNE zapísaná → NEpíše a NEloguje
    (force=True to obíde, napr. prvý zápis po povolení). Pri zmene → write + audit log
    (core.audit_log). Gated enabled+control_enabled (bezpečnostné poistky). Vráti dict s
    'changed'/'skipped'.
    """
    cfg = load_config()
    out: Dict[str, Any] = {"ok": False, "tag": "", "value": float(value_w),
                           "changed": False, "skipped": False, "msg": ""}
    tags_w = cfg.get("tags_write") or {}
    tag = tags_w.get("regfilter_param1") or "REG_Regulation_Filter_Param1"
    out["tag"] = tag
    _last = (cfg.get("last_write") or {}).get("regfilter_param1")
    # CHANGE-ONLY: rovnaká hodnota ako naposledy zapísaná → skip (bez write, bez log)
    if (not force) and _last is not None and abs(float(_last) - float(value_w)) < 1e-6:
        out["ok"] = True; out["skipped"] = True
        out["msg"] = f"bez zmeny ({float(value_w):.0f} W) — neloguje sa"
        return out
    if not cfg.get("enabled"):
        out["msg"] = "realio modul nie je enabled v configu"
        return out
    if not cfg.get("control_enabled"):
        out["msg"] = "control_enabled=FALSE — write zakázaný (bezpečnostná poistka)"
        return out
    ts_ms = _minute_aligned_ms()
    res = _send_tag_writes(cfg, [{"tag": tag, "value": float(value_w), "time": ts_ms}])
    out["ok"] = res["ok"]
    if res["ok"]:
        cfg.setdefault("last_write", {})
        cfg["last_write"]["regfilter_param1"] = float(value_w)
        cfg["last_write"]["regfilter_param1_ts"] = dt.datetime.fromtimestamp(ts_ms / 1000).isoformat(timespec="seconds")
        cfg["last_write"]["regfilter_param1_source"] = str(source)
        save_config(cfg)
        out["changed"] = True
        # CHANGE-ONLY LOG — len pri reálnej zmene hodnoty
        try:
            from core.audit_log import log_event
            log_event(actor="regfilter_writer", action="param1_change", tag=tag,
                      old_w=(float(_last) if _last is not None else None),
                      new_w=float(value_w), source=str(source),
                      ts=cfg["last_write"]["regfilter_param1_ts"])
        except Exception as _e_log:
            print(f"[regfilter] audit log zlyhal: {_e_log}")
        ts_str = dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:00")
        out["msg"] = (f"✓ REG_Regulation_Filter_Param1 = {float(value_w):.0f} W @ {ts_str} "
                      f"(zmena z {_last})")
    else:
        out["msg"] = res.get("msg") or "write zlyhal — všetky schémy"
        out["tried"] = res.get("tried", [])
        out["errors"] = res.get("errors", [])
    return out


def get_regfilter_plan(profile_name: str):
    """REGFILTER: vráti (values_96_w: list[float] | None, enabled: bool) z profilu (plan sekcia).

    `regfilter_param1_w` = 96×15-min hodnôt max odberu vo W. Je to ŠABLÓNA — platí KAŽDÝ deň,
    kým ju nezmeníš (= kopíruje sa deň na deň). Ak chýba 96-pole, skúsi jednu default hodnotu
    `regfilter_param1_default_w` (rovnaká pre celý deň). `regfilter_write_enabled` = povoliť zápis.
    """
    try:
        import profiles as _pr
        pl = (_pr.load_profile(profile_name) or {}).get("plan") or {}
        enabled = bool(pl.get("regfilter_write_enabled", False))
        vals = pl.get("regfilter_param1_w")
        if isinstance(vals, list) and len(vals) >= 96:
            return [float(x or 0.0) for x in vals[:96]], enabled
        dflt = pl.get("regfilter_param1_default_w")
        if dflt is not None:
            return [float(dflt)] * 96, enabled
        return None, enabled
    except Exception as _e:
        print(f"[regfilter] get_regfilter_plan zlyhal ({profile_name}): {_e}")
        return None, False


def regfilter_target_now(profile_name: str, now=None):
    """REGFILTER: aktuálna cieľová hodnota Param1 (W) pre profil podľa 15-min rozvrhu (šablóna
    platí každý deň). Vráti (value_w: float | None, enabled: bool)."""
    vals, enabled = get_regfilter_plan(profile_name)
    if not vals:
        return None, enabled
    n = now or dt.datetime.now()
    si = min(95, max(0, (n.hour * 60 + n.minute) // 15))
    return float(vals[si]), enabled


def write_battery_plan_15min(plan_15min_kw: List[float],
                                 day_iso: str,
                                 source: str = "plan_export") -> Dict[str, Any]:
    """Zapíše 15-min plán batt setpointov na Bender pre daný deň.

    Vstup:
      plan_15min_kw — List[float] presne 96 hodnôt v kW (96 × 15-min slotov)
                      **Konvencia: kladné = vybíjanie (do siete), záporné = nabíjanie (zo siete)**
                      Bender Param3 očakáva watty: kW × 1000.
      day_iso       — "YYYY-MM-DD" lokálny CEST deň
      source        — string pre audit log

    Postup:
      • Pre každý 15-min slot vypočíta unix-ms timestamp začiatku slotu (lokálny CEST)
      • Pošle naraz cez `/tag-set-new-values` s `values` arrayom:
          {tag: REG_Regulator_Param3, values: [{time, value(W)} × 96]}
          {tag: REG_Regulator_Manual_Plan, values: [{time, 2} × 96]}
        Server si ich aplikuje v správny čas (overené pri history backfill — Bender
        akceptuje pre-scheduled hodnoty s budúcim time).

    Vracia: {ok, msg, written_slots, day, total_kwh_charge, total_kwh_discharge, errors, tried}
    """
    cfg = load_config()
    out: Dict[str, Any] = {"ok": False, "msg": "", "day": day_iso,
                            "written_slots": 0,
                            "total_kwh_charge": 0.0, "total_kwh_discharge": 0.0}
    if not cfg.get("enabled"):
        out["msg"] = "realio modul nie je enabled"
        return out
    if not cfg.get("control_enabled"):
        out["msg"] = "control_enabled je FALSE — write zakázaný (bezpečnostná poistka)"
        return out
    if not isinstance(plan_15min_kw, list) or len(plan_15min_kw) != 96:
        out["msg"] = f"Očakáva presne 96 hodnôt (15-min slotov dňa), dostal {len(plan_15min_kw) if isinstance(plan_15min_kw, list) else type(plan_15min_kw).__name__}"
        return out
    tags_w = cfg.get("tags_write") or {}
    setpoint_tag = tags_w.get("batt_setpoint_kw")
    mode_tag = tags_w.get("batt_control_mode")
    if not setpoint_tag:
        out["msg"] = "tags_write.batt_setpoint_kw nie je nakonfigurovaný"
        return out

    # Parse day → vytvor slot timestampy (lokálny CEST → UTC unix ms)
    try:
        from zoneinfo import ZoneInfo
        TZ = ZoneInfo("Europe/Bratislava")
    except Exception:
        TZ = None
    try:
        d = dt.datetime.strptime(day_iso, "%Y-%m-%d").date()
    except Exception as e:
        out["msg"] = f"day_iso parse error: {e}"
        return out

    slot_ms = []
    for i in range(96):
        h, m = divmod(i * 15, 60)
        slot_local = dt.datetime(d.year, d.month, d.day, h, m, 0)
        if TZ is not None:
            slot_local = slot_local.replace(tzinfo=TZ)
        slot_ms.append(int(slot_local.timestamp() * 1000))

    # Build write requests
    # **Manual_Plan handshake** (Trakany Bender behavior, 2026-06-02 zistené):
    #   Pri zápise plánu treba do REG_Regulator_Manual_Plan poslať hodnotu **1**
    #   = "nový plán pripravený, čaká na spracovanie". Bender po načítaní setpointov
    #   sám zmení na 2 ("plán aktívny"). Ak by sme rovno poslali 2, server ho
    #   môže ignorovať (myslí si že už beží predošlý plán).
    # Konfigurovateľné cez `control_mode_pending_value` (default 1).
    # Pôvodný `control_mode_enable_value` (default 2) je čo Bender NÁM ukáže po spracovaní.
    mode_val_pending = float(cfg.get("control_mode_pending_value", 1))
    writes = []
    sum_charge_kwh = 0.0
    sum_discharge_kwh = 0.0
    for i, kw in enumerate(plan_15min_kw):
        try:
            kw_val = float(kw)
        except (TypeError, ValueError):
            kw_val = 0.0
        watts = kw_val * 1000.0   # kW → W
        # Bender konvencia: kladné = vybíjanie, záporné = nabíjanie (per user, 2026-06-01)
        writes.append({"tag": setpoint_tag, "value": watts, "time": slot_ms[i]})
        if mode_tag:
            writes.append({"tag": mode_tag, "value": mode_val_pending, "time": slot_ms[i]})
        # Energia per slot: kW × 0.25 h = kWh
        kwh = kw_val * 0.25
        if kwh > 0:
            sum_discharge_kwh += kwh
        else:
            sum_charge_kwh += abs(kwh)

    out["total_kwh_charge"] = round(sum_charge_kwh, 2)
    out["total_kwh_discharge"] = round(sum_discharge_kwh, 2)

    # Odoslať jedným POST-om — server akceptuje multi-time array
    res = _send_tag_writes(cfg, writes)
    out["tried"] = res.get("tried", [])
    out["errors"] = res.get("errors", [])
    if res.get("ok"):
        out["ok"] = True
        out["written_slots"] = 96
        out["msg"] = (f"✓ Zapísaných 96 slotov pre {day_iso} · "
                       f"nabíjanie {sum_charge_kwh:.1f} kWh, vybíjanie {sum_discharge_kwh:.1f} kWh")
        # Audit do last_write
        cfg.setdefault("last_write", {})
        cfg["last_write"]["batt_plan_15min_day"] = day_iso
        cfg["last_write"]["batt_plan_15min_slots"] = 96
        cfg["last_write"]["batt_plan_15min_ts"] = dt.datetime.now().isoformat(timespec="seconds")
        cfg["last_write"]["batt_plan_15min_source"] = str(source)
        save_config(cfg)
    else:
        out["msg"] = res.get("msg") or "Bender write zlyhal"
    return out


def discover_write_endpoint() -> Dict[str, Any]:
    """Probe-discover správny write endpoint na Bender / Express.js serveri.

    Postup:
      1. Skús niekoľko common write paths (HEAD + krátky GET) — anything != 404 je signál
      2. Stiahni `/dashboard-overview` HTML a vyhľadaj v ňom 'write', 'set', 'control',
         'param3', 'manual_plan' výrazy + URL hints v JS bundle linkoch
      3. Ak je dashboard HTML clean Vue/React SPA, JS bundle môže byť linknutý a obsahovať
         názvy endpointov — vrátime aspoň zoznam JS súborov pre manuálne stiahnutie

    Vracia: {"ok": bool, "probes": [...], "html_hints": [...], "js_bundles": [...]}.
    """
    cfg = load_config()
    s = _get_session(cfg)
    host = cfg.get("host", "").rstrip("/")
    out: Dict[str, Any] = {"ok": False, "probes": [], "html_hints": [], "js_bundles": []}
    if not host:
        out["msg"] = "host nie je nakonfigurovaný"
        return out

    # 1) Probe common write paths — GET s krátkym timeoutom; 404 = nie, inak je niečo tam
    candidates = [
        "/tag-write", "/tag-set", "/tag-data/write", "/tag-data/set",
        "/api/tag-write", "/api/tag-set", "/api/write", "/api/set", "/api/setpoint",
        "/api/control", "/api/regulator", "/api/regulator/write",
        "/control", "/setpoint", "/set-tag", "/write-tag", "/manual-plan",
        "/api/v1/write", "/api/v1/tag-write", "/v1/tag-write",
        "/regulator/write", "/regulator/set", "/regulator/manual-plan",
    ]
    for p in candidates:
        url = host + p
        try:
            r = s.get(url, timeout=4, verify=s.verify, allow_redirects=False)
            body = (r.text or "")[:120].replace("\n", " ")
            note = ""
            if r.status_code == 404:
                note = " (not found — preskočiť)"
            elif r.status_code in (200, 400, 405, 415, 422):
                note = "  ⬅ existuje (zaujímavé!)"
            elif r.status_code in (301, 302, 303, 307, 308):
                note = f" → redirect {r.headers.get('Location','?')}"
            elif r.status_code in (401, 403):
                note = " (auth required — pravdepodobne existuje)"
            out["probes"].append(f"GET {p} → HTTP {r.status_code}{note} · body: {body}")
        except Exception as e:
            out["probes"].append(f"GET {p} → EXC {e}")

    # 2) Stiahni dashboard HTML a hľadaj write hinty
    for dash_path in ("/dashboard-overview", "/dashboard", "/", "/control"):
        try:
            r = s.get(host + dash_path, timeout=8, verify=s.verify)
            if r.status_code >= 400:
                continue
            text = r.text or ""
            if len(text) < 100:
                continue
            # Greep výrazy
            import re as _re
            hints = set()
            for pat in (
                r'(?:fetch|axios|\.post|\.put|\.patch)\s*\(\s*["\']([^"\']{4,80})["\']',
                r'url\s*:\s*["\']([^"\']{4,80}(?:write|set|setpoint|control|regulator|tag-data)[^"\']*)["\']',
                r'["\'](/[a-zA-Z][a-zA-Z0-9/_-]{2,60}(?:write|set|setpoint|control|regulator|tag-data)[a-zA-Z0-9/_-]*)["\']',
                r'["\'](?:Manual_Plan|Param3|Regulator)["\']',
            ):
                for m in _re.findall(pat, text, flags=_re.IGNORECASE):
                    if isinstance(m, tuple):
                        m = m[0]
                    if m and len(m) < 200:
                        hints.add(m.strip())
            # JS bundle URLs
            js_bundles = set(_re.findall(r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']', text))
            if hints:
                out["html_hints"].append(f"{dash_path}: " + ", ".join(sorted(hints)[:30]))
            if js_bundles:
                out["js_bundles"].extend([f"{dash_path}: {b}" for b in sorted(js_bundles)[:10]])
            # Stačí jeden dashboard
            if hints or js_bundles:
                break
        except Exception as e:
            out["probes"].append(f"GET {dash_path} HTML → EXC {e}")

    # 3) Ak máme JS bundles, skús stiahnuť prvý a vyhľadať write endpointy v ňom
    if out["js_bundles"]:
        first_js_line = out["js_bundles"][0]
        # extrahuj URL z "path: url"
        js_url = first_js_line.split(": ", 1)[1] if ": " in first_js_line else first_js_line
        if not js_url.startswith("http"):
            js_url = host + ("/" + js_url if not js_url.startswith("/") else js_url)
        try:
            r = s.get(js_url, timeout=10, verify=s.verify)
            if r.status_code < 400 and r.text:
                text = r.text
                import re as _re
                # Find URL patterns in bundled JS (after minification it's usually inline strings)
                js_hints = set()
                for pat in (
                    r'["\'](/(?:api/)?[a-z][a-z0-9/_-]*(?:write|set|setpoint|control|regulator|tag-data|manual)[a-z0-9/_-]*)["\']',
                    r'(?:method|type)\s*:\s*["\'](POST|PUT|PATCH)["\']',
                ):
                    for m in _re.findall(pat, text, flags=_re.IGNORECASE):
                        if isinstance(m, tuple): m = m[0]
                        if m:
                            js_hints.add(m.strip())
                if js_hints:
                    out["html_hints"].append(f"JS bundle hints ({len(js_hints)}): " +
                                              ", ".join(sorted(js_hints)[:40]))
        except Exception as e:
            out["probes"].append(f"JS bundle fetch → EXC {e}")

    out["ok"] = True
    return out


def scan_js_bundle() -> Dict[str, Any]:
    """Stiahne dashboard JS bundle a vyhľadá v ňom write/Socket.IO indikátory.

    Skenuje:
      • Konkrétne tag mená (REG_Regulator, Manual_Plan, Param3) → context 100 chars
      • Socket.IO patterns: socket.emit, io(, .emit(, .on(
      • URL stringy: všetky string literály začínajúce '/'
      • Write keywords: write, setValue, setTag, manualPlan

    Vracia: {"ok": bool, "bundles": [...], "hits": {kategoria: [úryvky]}}.
    """
    cfg = load_config()
    s = _get_session(cfg)
    host = cfg.get("host", "").rstrip("/")
    out: Dict[str, Any] = {"ok": False, "bundles": [], "hits": {}, "errors": []}
    if not host:
        out["msg"] = "host nie je nakonfigurovaný"
        return out

    # Najprv stiahnuť dashboard HTML aby sme našli aktuálne JS hashe
    import re as _re
    js_urls = []
    try:
        r = s.get(host + "/dashboard-overview", timeout=10, verify=s.verify)
        if r.status_code < 400 and r.text:
            for m in _re.findall(r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']', r.text):
                if not m.startswith("http"):
                    m = host + ("/" + m if not m.startswith("/") else m)
                js_urls.append(m)
    except Exception as e:
        out["errors"].append(f"dashboard fetch: {e}")

    if not js_urls:
        # fallback: pevne dané bežné cesty
        js_urls = [host + "/js/app.js", host + "/js/main.js", host + "/js/bundle.js"]

    # Sken patterns — kľúčové stringy
    patterns = {
        "tag_names": [
            r'REG_Regulator[A-Za-z_]*',
            r'Manual_Plan',
            r'Param3',
            r'Manual[A-Z][a-zA-Z]*',
        ],
        "socketio": [
            r'socket\.emit\s*\(\s*["\']([^"\']+)["\']',
            r'io\s*\(\s*["\']([^"\']*)["\']',
            r'\.emit\s*\(\s*["\']([a-zA-Z][a-zA-Z0-9_-]+)["\']',
            r'\.on\s*\(\s*["\']([a-zA-Z][a-zA-Z0-9_:-]+)["\']',
        ],
        "urls": [
            r'["\'](/[a-zA-Z][a-zA-Z0-9/_.?=&-]{4,80})["\']',
        ],
        "write_kw": [
            r'(setValue|setTag|writeTag|writeValue|manualPlan|setSetpoint|controlBattery)',
        ],
    }

    hits: Dict[str, List[str]] = {k: [] for k in patterns}
    bundles_summary = []

    for url in js_urls[:5]:  # max 5 bundles
        try:
            r = s.get(url, timeout=15, verify=s.verify)
            if r.status_code >= 400 or not r.text:
                bundles_summary.append(f"{url} → HTTP {r.status_code} (skip)")
                continue
            text = r.text
            bundles_summary.append(f"{url} → HTTP {r.status_code} ({len(text):,} bytes)")

            # Pre každú kategóriu nájdi matches s kontextom (40 chars pred + 60 po)
            for cat, pats in patterns.items():
                cat_hits = set()
                for pat in pats:
                    for m in _re.finditer(pat, text, flags=_re.IGNORECASE):
                        start = max(0, m.start() - 40)
                        end = min(len(text), m.end() + 60)
                        ctx = text[start:end].replace("\n", " ").replace("\r", "")
                        # Skrátiť whitespace
                        ctx = _re.sub(r'\s+', ' ', ctx)
                        # Označiť match v kontexte
                        if len(ctx) < 250:
                            cat_hits.add(ctx)
                        if len(cat_hits) >= 30:
                            break
                hits[cat].extend(sorted(cat_hits)[:30])
        except Exception as e:
            out["errors"].append(f"{url}: {e}")
            bundles_summary.append(f"{url} → EXC {e}")

    # Dedup per kategória (po concat)
    for cat in hits:
        hits[cat] = sorted(set(hits[cat]))[:40]

    out["bundles"] = bundles_summary
    out["hits"] = hits
    out["ok"] = True
    return out


def probe_ws_endpoint() -> Dict[str, Any]:
    """Stiahne /ws-address a vyhľadá _onSetValueButtonClick body v app.js.

    Dashboard používa WebSocket pre write — toto je discovery toho protokolu.
      1. GET /ws-address → vráti URL (alebo objekt s URL) WS servera
      2. GET /js/app.js → vyhľadáme _onSetValueButtonClick s 600 chars kontextu
         (tam je presný formát write message-u)
      3. Bonus: vyhľadáme aj 'send', 'WebSocket', 'new WebSocket' patterns
    """
    cfg = load_config()
    s = _get_session(cfg)
    host = cfg.get("host", "").rstrip("/")
    out: Dict[str, Any] = {"ok": False, "ws_address": "", "ws_raw": "",
                            "set_value_snippets": [], "ws_send_snippets": [], "errors": []}
    if not host:
        out["msg"] = "host nie je nakonfigurovaný"
        return out

    # 1) GET /ws-address
    try:
        r = s.get(host + "/ws-address", timeout=10, verify=s.verify,
                   headers={"Cache-Control": "no-store"})
        out["ws_raw"] = f"HTTP {r.status_code} · {(r.text or '')[:500]}"
        if r.status_code < 400 and r.text:
            try:
                j = r.json()
                # rôzne formáty: string, {url:"..."}, {address:"..."}, {ws:"..."}
                if isinstance(j, str):
                    out["ws_address"] = j
                elif isinstance(j, dict):
                    for k in ("url", "address", "ws", "websocket", "endpoint", "host"):
                        if k in j and isinstance(j[k], str):
                            out["ws_address"] = j[k]; break
                    if not out["ws_address"]:
                        # vezmi prvý string hodnota v dict
                        for v in j.values():
                            if isinstance(v, str) and ("ws" in v.lower() or v.startswith("/")):
                                out["ws_address"] = v; break
            except Exception:
                # text response, možno priamo URL
                txt = (r.text or "").strip()
                if txt and len(txt) < 300:
                    out["ws_address"] = txt
    except Exception as e:
        out["errors"].append(f"/ws-address: {e}")

    # 2) Stiahnuť app.js a vyhľadať _onSetValueButtonClick + ws.send patterns
    import re as _re
    # Najprv získaj aktuálne JS URL z dashboard HTML
    js_urls = []
    try:
        r = s.get(host + "/dashboard-overview", timeout=10, verify=s.verify)
        if r.status_code < 400 and r.text:
            for m in _re.findall(r'<script[^>]+src=["\']([^"\']+app[^"\']*\.js[^"\']*)["\']', r.text):
                if not m.startswith("http"):
                    m = host + ("/" + m if not m.startswith("/") else m)
                js_urls.append(m)
    except Exception as e:
        out["errors"].append(f"dashboard fetch: {e}")
    if not js_urls:
        js_urls = [host + "/js/app.js"]

    for url in js_urls[:2]:
        try:
            r = s.get(url, timeout=20, verify=s.verify)
            if r.status_code >= 400 or not r.text:
                continue
            text = r.text

            # 2a) Veľký kontext okolo _onSetValueButtonClick a _onSetValuesButtonClick
            for pat in (r'_onSetValueButtonClick', r'_onSetValuesButtonClick'):
                for m in _re.finditer(pat, text):
                    start = max(0, m.start() - 100)
                    end = min(len(text), m.end() + 600)
                    snippet = text[start:end]
                    out["set_value_snippets"].append(snippet)
                    if len(out["set_value_snippets"]) >= 6:
                        break

            # 2b) WebSocket send patterns: ws.send(, this.send(, socket.send(
            for pat in (
                r'\.send\s*\(\s*[^)]{0,200}\)',
                r'new\s+WebSocket\s*\([^)]+\)',
                r'WebSocket\.prototype\.send',
                r'"set-value"|\'set-value\'|"setValue"',
                r'"writeTag"|\'writeTag\'|"setTagValue"|\'setTagValue\'',
            ):
                for m in _re.finditer(pat, text, flags=_re.IGNORECASE):
                    start = max(0, m.start() - 80)
                    end = min(len(text), m.end() + 250)
                    snippet = text[start:end].replace("\n", " ").replace("\r", "")
                    snippet = _re.sub(r'\s+', ' ', snippet)
                    if len(snippet) < 400:
                        out["ws_send_snippets"].append(snippet)
                    if len(out["ws_send_snippets"]) >= 25:
                        break
        except Exception as e:
            out["errors"].append(f"{url}: {e}")

    # Dedup
    out["set_value_snippets"] = list(dict.fromkeys(out["set_value_snippets"]))[:8]
    out["ws_send_snippets"] = list(dict.fromkeys(out["ws_send_snippets"]))[:30]
    out["ok"] = True
    return out


def scan_message_types() -> Dict[str, Any]:
    """Vyhľadá všetky `messageType: N` v app.js + ich kontexty.

    Vieme: write ide cez `this._ws.send(JSON.stringify(req))` kde `req` má
    `messageType: N`. Vidíme N=11 = ping. Potrebujeme zistiť ktorý N = write tag.

    Tiež hľadáme `_sendRequestsToServer` callers — odtiaľ uvidíme typové stavby.
    """
    cfg = load_config()
    s = _get_session(cfg)
    host = cfg.get("host", "").rstrip("/")
    out: Dict[str, Any] = {"ok": False, "message_types": {}, "send_callers": [],
                            "tag_data_builder": [], "errors": []}
    if not host:
        out["msg"] = "host nie je nakonfigurovaný"
        return out

    import re as _re
    re_escape = _re.escape
    # Najprv získaj aktuálnu app.js URL
    js_url = host + "/js/app.js"
    try:
        r = s.get(host + "/dashboard-overview", timeout=10, verify=s.verify)
        if r.status_code < 400 and r.text:
            m = _re.search(r'<script[^>]+src=["\']([^"\']+app[^"\']*\.js[^"\']*)["\']', r.text)
            if m:
                p = m.group(1)
                if not p.startswith("http"):
                    p = host + ("/" + p if not p.startswith("/") else p)
                js_url = p
    except Exception as e:
        out["errors"].append(f"dashboard fetch: {e}")

    try:
        r = s.get(js_url, timeout=20, verify=s.verify)
        if r.status_code >= 400 or not r.text:
            out["errors"].append(f"{js_url} → HTTP {r.status_code}")
            return out
        text = r.text
        out["js_size"] = len(text)
        out["js_url"] = js_url

        # 1) Zhromaždi všetky messageType: N (N v rozmedzí 0..99) s kontextom 80+200
        mt_map: Dict[str, List[str]] = {}
        for m in _re.finditer(r'messageType\s*:\s*(\d{1,3})', text):
            n = m.group(1)
            start = max(0, m.start() - 100)
            end = min(len(text), m.end() + 250)
            ctx = text[start:end].replace("\n", " ").replace("\r", "")
            ctx = _re.sub(r'\s+', ' ', ctx)
            if len(ctx) < 400:
                mt_map.setdefault(n, []).append(ctx)
        # Dedup per N a obmedz na 3 vzorky
        for n in mt_map:
            mt_map[n] = list(dict.fromkeys(mt_map[n]))[:3]
        out["message_types"] = {n: mt_map[n] for n in sorted(mt_map.keys(), key=lambda x: int(x))}

        # 1b) Celá switch case tabuľka pri _onMessageWS — vytiahneme 2500 chars
        on_msg_blocks = []
        for m in _re.finditer(r'_onMessageWS\s*[:\(]', text):
            start = max(0, m.start() - 50)
            end = min(len(text), m.end() + 2500)
            block = text[start:end].replace("\n", " ").replace("\r", "")
            block = _re.sub(r'\s+', ' ', block)
            on_msg_blocks.append(block)
        out["on_message_ws"] = list(dict.fromkeys(on_msg_blocks))[:3]

        # 1c) MessageType enum-like definícia v minified kóde
        # Patterns: var X = {Read: 1, Write: 2, ...} alebo Object.freeze({...})
        # Plus konkrétne stringy: "Read", "Write", "Subscribe", "Heartbeat", "ConnectionOpen"
        enum_blocks = []
        for pat in (
            r'Object\.freeze\s*\(\s*\{[^}]{20,300}\}',
            r'\{[^{}]*(?:Heartbeat|ConnectionOpen|ServiceResponse|WriteValue|ReadValue|Read|Write)[^{}]*\}',
        ):
            for m in _re.finditer(pat, text):
                snippet = m.group(0).replace("\n", " ").replace("\r", "")
                snippet = _re.sub(r'\s+', ' ', snippet)
                if 30 < len(snippet) < 500:
                    enum_blocks.append(snippet)
                if len(enum_blocks) >= 15:
                    break
        out["enum_blocks"] = list(dict.fromkeys(enum_blocks))[:15]

        # 1d) Body metód súvisiacich s tag write (edit mode v dashboard tag-table)
        # _applyBufferedChangesData je najpravdepodobnejšie miesto kde sa robí write call.
        write_method_blocks = []
        for method_name in ("_applyBufferedChangesData", "_getListOfSavedTags",
                             "confirmChanges", "_processCreateOrUpdateLogs",
                             "_writeTagsValues", "_saveChanges", "applyChanges",
                             "_onSaveChangesClick", "_sendTagWrite"):
            for m in _re.finditer(re_escape(method_name) + r'\s*[:\(]', text):
                start = max(0, m.start() - 50)
                end = min(len(text), m.end() + 1500)
                snippet = text[start:end].replace("\n", " ").replace("\r", "")
                snippet = _re.sub(r'\s+', ' ', snippet)
                write_method_blocks.append(f"[{method_name}] {snippet}")
                if len([b for b in write_method_blocks if method_name in b]) >= 2:
                    break
        out["write_method_blocks"] = list(dict.fromkeys(write_method_blocks))[:12]

        # 1e) Hľadáme priamo createOrUpdateValues a setValue / writeValue / setTagValue
        # (an.createOrUpdateValues(t) bol nájdený v method 1 — toto je presný write call)
        create_or_update_blocks = []
        for fn_name in ("createOrUpdateValues", "setTagValue", "writeTagValue",
                         "setValues", "writeValue", "writeTags", "saveTagValue"):
            # Definícia funkcie: key:"fnName",value:function(...) {...}
            for m in _re.finditer(r'(?:key\s*:\s*)?["\']?' + re_escape(fn_name) +
                                    r'["\']?\s*[:,]\s*function\s*\([^)]*\)\s*\{', text):
                start = max(0, m.start() - 30)
                end = min(len(text), m.end() + 1800)
                snippet = text[start:end].replace("\n", " ").replace("\r", "")
                snippet = _re.sub(r'\s+', ' ', snippet)
                create_or_update_blocks.append(f"[def {fn_name}] {snippet}")
                if len([b for b in create_or_update_blocks if fn_name in b.split("]")[0]]) >= 2:
                    break
            # Aj nájdi všetky USE/calls: .createOrUpdateValues(  alebo  fnName: ...
            for m in _re.finditer(r'\.' + re_escape(fn_name) + r'\s*\(', text):
                start = max(0, m.start() - 200)
                end = min(len(text), m.end() + 400)
                snippet = text[start:end].replace("\n", " ").replace("\r", "")
                snippet = _re.sub(r'\s+', ' ', snippet)
                create_or_update_blocks.append(f"[call .{fn_name}] {snippet}")
                if len([b for b in create_or_update_blocks if f".{fn_name}" in b]) >= 3:
                    break
        out["create_or_update_blocks"] = list(dict.fromkeys(create_or_update_blocks))[:20]

        # 2) Kontext okolo _sendRequestsToServer callers — kto ho volá?
        for m in _re.finditer(r'_sendRequestsToServer\s*\(', text):
            start = max(0, m.start() - 400)
            end = min(len(text), m.end() + 200)
            ctx = text[start:end].replace("\n", " ").replace("\r", "")
            ctx = _re.sub(r'\s+', ' ', ctx)
            if len(ctx) < 700:
                out["send_callers"].append(ctx)
        out["send_callers"] = list(dict.fromkeys(out["send_callers"]))[:6]

        # 3) Vyhľadaj kde sa stavia request s tagmi (požiadavka na write hodnoty)
        # patterns: requests:[{...tag...value...}], tags:[...]+value+messageType
        for pat in (
            r'requests\s*:\s*\[[^\]]{0,400}\]',
            r'\{[^{}]{0,200}messageType\s*:\s*\d+[^{}]{0,200}\}',
            r'tags\s*:\s*\[[^\]]{0,150}\][^{}]{0,150}value',
        ):
            for m in _re.finditer(pat, text):
                snippet = m.group(0).replace("\n", " ").replace("\r", "")
                snippet = _re.sub(r'\s+', ' ', snippet)
                if 50 < len(snippet) < 500:
                    out["tag_data_builder"].append(snippet)
                if len(out["tag_data_builder"]) >= 20:
                    break
        out["tag_data_builder"] = list(dict.fromkeys(out["tag_data_builder"]))[:20]

    except Exception as e:
        out["errors"].append(f"{js_url}: {e}")

    out["ok"] = True
    return out


def ws_listen_probe(duration_s: int = 12) -> Dict[str, Any]:
    """Pripojí sa na wss://10.200.136.21:443 cez WebSocket, počúva príchodzie zprávy
    a loguje ich. Voliteľne pošle test READ request (rovnaký formát ako HTTP GET tag-data).

    Vyžaduje websocket-client knižnicu.
    """
    cfg = load_config()
    host = cfg.get("host", "").rstrip("/")
    out: Dict[str, Any] = {"ok": False, "ws_url": "", "messages": [], "sent": [], "errors": []}
    if not host:
        out["msg"] = "host nie je nakonfigurovaný"
        return out

    # Importuj websocket-client (treba mať nainštalované)
    try:
        import websocket as _ws  # type: ignore
    except ImportError:
        out["errors"].append("Chýba websocket-client. Inštaluj: pip install websocket-client")
        return out

    # Získaj WS URL z /ws-address (re-použiť cookies)
    s = _get_session(cfg)
    ws_url = ""
    try:
        r = s.get(host + "/ws-address", timeout=8, verify=s.verify,
                   headers={"Cache-Control": "no-store"})
        if r.status_code < 400 and r.text:
            try:
                j = r.json()
                if isinstance(j, dict):
                    ws_url = j.get("address", "") or j.get("url", "")
                elif isinstance(j, str):
                    ws_url = j
            except Exception:
                pass
    except Exception as e:
        out["errors"].append(f"/ws-address: {e}")
    if not ws_url:
        ws_url = host.replace("https://", "wss://").replace("http://", "ws://")
    out["ws_url"] = ws_url

    # Pripoj WebSocket s našimi cookies
    cookie_str = cfg.get("cookies") or ""
    if not cookie_str:
        # extrahuj zo session
        cookie_str = "; ".join(f"{c.name}={c.value}" for c in s.cookies)
    headers = [f"Cookie: {cookie_str}"] if cookie_str else []
    headers.append("Origin: " + host)
    headers.append("User-Agent: Mozilla/5.0 RealIO/1.0")

    try:
        import ssl as _ssl
        ws = _ws.create_connection(ws_url, header=headers, timeout=10,
                                     sslopt={"cert_reqs": _ssl.CERT_NONE},
                                     origin=host)
    except Exception as e:
        out["errors"].append(f"WS connect: {e}")
        return out

    import time as _time
    import threading as _th
    stop_at = _time.time() + duration_s
    msg_collected: List[str] = []

    def reader():
        try:
            ws.settimeout(0.5)
            while _time.time() < stop_at:
                try:
                    m = ws.recv()
                    if m:
                        msg_str = m if isinstance(m, str) else m.decode("utf-8", errors="replace")
                        msg_collected.append(msg_str[:600])
                except Exception:
                    pass
        except Exception:
            pass

    t = _th.Thread(target=reader, daemon=True)
    t.start()

    # Po krátkej pauze brute-force pošli messageType:N pre N=2..30 s minimálnym body.
    # Server vráti "Unsupported message type N" pre neplatné, niečo iné pre platné.
    _time.sleep(1.0)
    test_tag = list((cfg.get("tags_read") or {}).values())[0] if cfg.get("tags_read") else "BAT_Aggregated_C_SOC"
    # Cieľ: nájsť všetky platné messageType-y (mimo 1, 10, 11, 12 ktoré poznáme)
    test_ranges = list(range(2, 30)) + [100, 101, 102]
    for n in test_ranges:
        # Body: zahrň aj request-like fields aby server mohol robiť viac validácie
        req = {"messageType": n,
                "requestId": n,  # niektoré protokoly chcú correlation ID
                "requests": [{"type": 1, "params": {"tags": [test_tag]}}]}
        try:
            ws.send(json.dumps(req))
            out["sent"].append(f"mt={n}")
            _time.sleep(0.15)  # krátka pauza medzi requestami
        except Exception as e:
            out["errors"].append(f"send mt={n}: {e}")
            break
    # Extra: skús varianty s pole 'data' / 'payload' miesto 'requests'
    for fld_name in ("data", "payload", "params", "body"):
        try:
            req = {"messageType": 2, fld_name: {"tags": [test_tag], "value": 0, "time": _minute_aligned_ms()}}
            ws.send(json.dumps(req))
            out["sent"].append(f"mt=2 + {fld_name}")
            _time.sleep(0.15)
        except Exception as e:
            out["errors"].append(f"send mt=2+{fld_name}: {e}")
            break

    # Počkaj na zvyšok času
    _time.sleep(max(0.1, stop_at - _time.time()))
    try:
        ws.close()
    except Exception:
        pass

    out["messages"] = msg_collected
    out["ok"] = True
    return out


def disable_battery_control(source: str = "manual") -> Dict[str, Any]:
    """Vypne manuálne riadenie batérie — zápiše REG_Regulator_Manual_Plan = control_mode_disable_value
    (typicky 0). Batéria sa vráti do auto módu (lokálna logika alebo D-1 plán)."""
    cfg = load_config()
    out: Dict[str, Any] = {"ok": False, "msg": ""}
    if not cfg.get("enabled"):
        out["msg"] = "realio modul nie je enabled"
        return out
    if not cfg.get("control_enabled"):
        out["msg"] = "control_enabled=False"
        return out
    mode_tag = (cfg.get("tags_write") or {}).get("batt_control_mode")
    if not mode_tag:
        out["msg"] = "batt_control_mode tag nie je nakonfigurovaný"
        return out
    disable_val = float(cfg.get("control_mode_disable_value", 0))
    ts_ms = _minute_aligned_ms()
    res = _send_tag_writes(cfg, [{"tag": mode_tag, "value": disable_val, "time": ts_ms}])
    out["ok"] = res["ok"]
    if res["ok"]:
        cfg.setdefault("last_write", {})
        cfg["last_write"]["batt_control_mode"] = disable_val
        cfg["last_write"]["batt_control_mode_ts"] = dt.datetime.fromtimestamp(ts_ms/1000).isoformat(timespec="seconds")
        cfg["last_write"]["batt_control_mode_source"] = source
        save_config(cfg)
        out["msg"] = f"✓ Batt control vypnutý ({mode_tag}={disable_val})"
    else:
        out["msg"] = f"write zlyhal: {res.get('msg','?')}"
    return out


# ─── FVE riadenie cez SSH + Modbus (Huawei SmartLogger) ─────────────────────
def write_fve_percent(pct: float, source: str = "manual") -> Dict[str, Any]:
    """Nastaví činný výkon FVE inverterа cez SSH jump host + modpoll.

    Volá `fve_setpoint.FvePowerControl.set_percent(pct)` s parametrami z configu.
    Postupy:
      1. Načíta cfg.fve_control sekciu (ssh_host, user, port, key, device_ip,
         modpoll, slave_id, ctrl_register, gain)
      2. Skontroluje master switche: cfg.enabled + cfg.control_enabled + cfg.fve_control.enabled
      3. Inštantuje FvePowerControl z fve_setpoint.py s parametrami z configu
      4. .connect() → .set_percent(pct) → .close()
      5. Pri úspechu uloží last_write.fve_pct + timestamp + source

    Hodnota `pct` v rozsahu 0..100 (0 = vypnúť FTV, 100 = max výkon).
    Vracia: {"ok": bool, "msg": str, "raw_register": int, "output": str}.
    """
    out: Dict[str, Any] = {"ok": False, "msg": "", "raw_register": None, "output": ""}
    cfg = load_config()
    if not cfg.get("enabled"):
        out["msg"] = "realio modul nie je enabled"
        return out
    if not cfg.get("control_enabled"):
        out["msg"] = "control_enabled=False — write je zakázaný (bezpečnostná poistka)"
        return out
    fve_cfg = cfg.get("fve_control") or {}
    if not fve_cfg.get("enabled"):
        out["msg"] = ("fve_control.enabled=False — povoľ FVE riadenie v configu "
                       "(záložka Nastavenie → FVE setpoint → checkbox 'FVE control povolený')")
        return out

    try:
        pct_int = int(round(float(pct)))
    except (ValueError, TypeError):
        out["msg"] = f"neplatná hodnota pct: {pct!r}"
        return out
    pct_int = max(0, min(100, pct_int))

    try:
        import fve_setpoint as _fve
    except ImportError as e:
        out["msg"] = f"fve_setpoint modul sa nepodarilo importnúť: {e}"
        return out

    try:
        ctrl = _fve.FvePowerControl(
            ssh_host=fve_cfg.get("ssh_host", "10.200.136.21"),
            ssh_user=fve_cfg.get("ssh_user", "support"),
            ssh_port=int(fve_cfg.get("ssh_port", 8222)),
            ssh_key=fve_cfg.get("ssh_key", "~/.ssh/support.rsa"),
            device_ip=fve_cfg.get("device_ip", "192.168.1.250"),
            modpoll=fve_cfg.get("modpoll", "modpoll"),
            slave_id=int(fve_cfg.get("slave_id", 0)),
            ctrl_register=int(fve_cfg.get("ctrl_register", 40428)),
            gain=int(fve_cfg.get("gain", 10)),
        )
    except Exception as e:
        out["msg"] = f"FvePowerControl inicializácia zlyhala: {e}"
        return out

    # Connect (otvor zdielané SSH spojenie)
    ok_conn, msg_conn = ctrl.connect()
    if not ok_conn:
        out["msg"] = f"SSH pripojenie zlyhalo: {msg_conn}"
        try: ctrl.close()
        except Exception: pass
        return out

    try:
        ok_write, output = ctrl.set_percent(pct_int)
        raw = pct_int * int(fve_cfg.get("gain", 10))
        out["output"] = output
        out["raw_register"] = raw
        if ok_write:
            # Audit
            cfg.setdefault("last_write", {})
            now_iso = dt.datetime.now().isoformat(timespec="seconds")
            cfg["last_write"]["fve_pct"] = pct_int
            cfg["last_write"]["fve_pct_ts"] = now_iso
            cfg["last_write"]["fve_pct_source"] = str(source)
            save_config(cfg)
            out["ok"] = True
            out["msg"] = (f"OK — {pct_int}% (register {fve_cfg.get('ctrl_register',40428)}"
                           f" = {raw}, gain {fve_cfg.get('gain',10)})")
        else:
            out["msg"] = f"modpoll zápis zlyhal: {output[:300]}"
    except Exception as e:
        out["msg"] = f"set_percent výnimka: {e}"
    finally:
        try: ctrl.close()
        except Exception: pass

    return out


# ─── Lokálne CSV — append per minute ─────────────────────────────────────────
CSV_COLS = ["time", "ftv_power_kw", "load_power_kw", "load_power_kw_15m",
            "batt_power_kw", "batt_soc_pct", "grid_power_kw",
            "batt_setpoint_kw_cmd", "ftv_curtail_kw_cmd"]


def _ensure_db_migrated() -> None:
    """Auto-migrácia CSV → SQLite pri prvom použití. Idempotentné — ak DB
    už existuje s rovnakým/väčším počtom riadkov ako CSV, preskočí."""
    try:
        import realio_db as _db
    except ImportError:
        return
    csv_path_real = _csv_path()
    db_path_real = _db.db_path()
    # Ak DB neexistuje a CSV áno → migruj
    if not os.path.exists(db_path_real) and os.path.exists(csv_path_real):
        try:
            res = _db.migrate_from_csv(csv_path_real)
            print(f"[realio] auto-migration CSV → SQLite: {res.get('msg')}")
        except Exception as e:
            print(f"[realio] auto-migration zlyhalo: {e}")


def append_measurement(values: Dict[str, Any]) -> None:
    """Append jeden záznam do SQLite (UPSERT na time_ms).

    Backward-compat API: zachované meno a signature, ale namiesto CSV write
    sa robí SQLite UPSERT s COALESCE — per-tag values sa zlúčia s existing.
    """
    try:
        import realio_db as _db
    except ImportError:
        return
    _ensure_db_migrated()
    cfg = load_config()
    ts = values.get("_ts") or dt.datetime.now().isoformat(timespec="seconds")
    row = {}
    for k in ("ftv_power_kw", "load_power_kw", "load_power_kw_15m",
                "batt_power_kw", "batt_soc_pct", "grid_power_kw"):
        v = values.get(k)
        if v is not None:
            row[k] = v
    # Setpoint cmd audit (rovnaké správanie ako CSV verzia)
    lw = cfg.get("last_write") or {}
    if lw.get("batt_setpoint_kw") is not None:
        row["batt_setpoint_kw_cmd"] = float(lw["batt_setpoint_kw"])
    if lw.get("ftv_curtail_kw") is not None:
        row["ftv_curtail_kw_cmd"] = float(lw["ftv_curtail_kw"])
    try:
        _db.insert_row(ts, row)
    except Exception as e:
        print(f"[realio.append_measurement] DB insert zlyhalo: {e}")


def read_recent(n_minutes: int = 240) -> pd.DataFrame:
    """Načíta posledných N minút zo SQLite.

    Vracia DataFrame s rovnakými stĺpcami ako pôvodné CSV (`CSV_COLS`) — backward
    compatible API. Per-row dropna pre stĺpce ktoré sú NULL v DB sa robí v
    SQLite directly (NULL → pandas NaN automaticky).
    """
    try:
        import realio_db as _db
    except ImportError:
        return pd.DataFrame(columns=CSV_COLS)
    _ensure_db_migrated()
    try:
        df = _db.read_recent(n_minutes=n_minutes)
        # Doplň chýbajúce CSV_COLS (kompat s legacy CSV consumers)
        for c in CSV_COLS:
            if c not in df.columns:
                df[c] = pd.NA
        # Filter — drop riadky kde sú VŠETKY power stĺpce NULL (artefakt starého CSV pred migráciou)
        power_cols = [c for c in ("ftv_power_kw", "load_power_kw", "load_power_kw_15m",
                                    "batt_power_kw", "batt_soc_pct", "grid_power_kw") if c in df.columns]
        if power_cols and not df.empty:
            df = df.dropna(subset=power_cols, how="all")
        return df
    except Exception as e:
        print(f"[realio.read_recent] DB read zlyhalo: {e}")
        return pd.DataFrame(columns=CSV_COLS)


def cleanup_csv() -> Dict[str, Any]:
    """Cleanup pre SQLite — VACUUM (reorganizácia DB súboru).

    Pôvodný CSV cleanup riešil duplicitné/prázdne riadky. V SQLite tieto problémy
    nevznikajú (PRIMARY KEY na time_ms zaisťuje unikátnosť, COALESCE per-stĺpec
    UPSERT zachováva non-NULL hodnoty). VACUUM len uvoľní fragmentované miesto.
    """
    try:
        import realio_db as _db
    except ImportError:
        return {"ok": False, "msg": "realio_db modul nedostupný"}
    _ensure_db_migrated()
    try:
        res = _db.vacuum()
        stats_info = _db.stats()
        res["total_rows"] = stats_info.get("total")
        res["msg"] = (f"VACUUM OK · {res.get('size_before',0):,} → {res.get('size_after',0):,} B "
                       f"· DB má {stats_info.get('total',0):,} riadkov "
                       f"({stats_info.get('first','?')[:10]} → {stats_info.get('last','?')[:10]})")
        return res
    except Exception as e:
        return {"ok": False, "msg": f"VACUUM zlyhal: {e}"}


# ─── Scheduler hook ──────────────────────────────────────────────────────────
def poll_and_log() -> Optional[Dict[str, Any]]:
    """Volá sa zo scheduler.py periodicky."""
    cfg = load_config()
    if not cfg.get("enabled"):
        return None
    vals = fetch_latest_all()
    if not vals or vals.get("_error"):
        return vals
    try:
        append_measurement(vals)
    except Exception as e:
        print(f"[realio.poll_and_log] CSV append zlyhalo: {e}")
    return vals


# ─── Diagnostika (užitočná pre /realio Test čítania) ─────────────────────────
def diagnose() -> Dict[str, Any]:
    """Vykoná diagnostický test pripojenia + tag dostupnosti.
    Vracia dict s detailmi pre UI display."""
    cfg = load_config()
    out: Dict[str, Any] = {"host": cfg.get("host"),
                            "endpoint_path": cfg.get("endpoint_path"),
                            "verify_ssl": cfg.get("verify_ssl"),
                            "tags_configured": len([v for v in (cfg.get("tags_read") or {}).values() if v]),
                            "errors": [], "latest": None, "raw": None,
                            "login": None}
    if not cfg.get("enabled"):
        out["errors"].append("enabled=False — modul je vypnutý")
        return out
    tag_map = {k: v for k, v in (cfg.get("tags_read") or {}).items() if v}
    if not tag_map:
        out["errors"].append("žiadne tagy nakonfigurované")
        return out
    # Force fresh session (rešpektuje manuálne cookies alebo skúsi auto-login)
    global _SESSION, _SESSION_HOST
    _SESSION = None
    _SESSION_HOST = None
    cookie_str = (cfg.get("cookies") or "").strip()
    if not cookie_str:
        # Bez cookies skúsime form login a zaznamenáme výsledok
        login = login_diagnose()
        out["login"] = login
        if not login.get("ok"):
            out["errors"].append(
                f"chýbajú manuálne cookies A form login zlyhal: {login.get('msg','?')} "
                f"(GET {login.get('status_get')}, POST {login.get('status_post')}, "
                f"final_url={login.get('final_url','?')}). "
                f"Skopíruj cookies z prehliadača (F12 → Network → Cookie header) do poľa „Manuálne cookies\".")
            return out
    else:
        out["login"] = {"ok": True, "msg": f"použité manuálne cookies ({cookie_str.count('=')} hodnôt)"}
    # Skús tag fetch — return_raw=True aby sme videli aj surový response keď parser zlyhá
    try:
        result = _fetch_latest_via_bender(cfg, list(tag_map.values()), return_raw=True)
        raw_body = result.get("_raw")
        raw = result.get("_values") or {}
        out["raw"] = raw
        out["raw_response"] = raw_body
        out["request_url"] = result.get("_url")
        scale_map = cfg.get("scale_read") or {}
        scaled = {}
        for logical, tag_name in tag_map.items():
            v = raw.get(tag_name)
            scale = float(scale_map.get(logical, 1.0))
            scaled[logical] = (None if v is None else float(v) * scale)
        out["latest"] = scaled
        # Ak parser nedostal žiadnu hodnotu — pravdepodobne iná schéma → ukáž raw response
        non_null = sum(1 for v in scaled.values() if v is not None)
        if non_null == 0:
            try:
                raw_snippet = json.dumps(raw_body, ensure_ascii=False)[:1500]
            except Exception:
                raw_snippet = str(raw_body)[:1500]
            out["errors"].append(
                f"Server vrátil odpoveď (HTTP 200) ale parser nenašiel hodnoty. "
                f"Skús: 1) overiť názvy tagov či sedia s dashboardom, 2) pošli mi tento raw "
                f"response aby som dokázal upraviť parser:<br><pre style='background:#f5f5f5;"
                f"padding:8px;border-radius:6px;overflow:auto;max-height:400px;font-size:11px'>"
                f"URL: {result.get('_url')}<br><br>{raw_snippet}</pre>")
    except Exception as e:
        msg = str(e)
        if "403" in msg or "401" in msg or "/login" in msg.lower():
            out["errors"].append(
                f"HTTP fetch zlyhalo s auth chybou ({msg}). "
                f"Cookies pravdepodobne expirovali alebo nie sú nastavené. "
                f"Choď na https://10.200.136.21/dashboard-overview, F12 → Network → klikni "
                f"na ľubovoľný request → skopíruj celý 'Cookie:' header → vlož ho do poľa "
                f"„Manuálne cookies\" v /realio a Ulož.")
        else:
            out["errors"].append(f"HTTP fetch zlyhalo: {msg}")
    return out
