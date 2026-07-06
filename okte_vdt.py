# -*- coding: utf-8 -*-
"""
okte_vdt.py — OKTE ISOT VDT (intraday) participant API klient.

**READ-ONLY** — modul obsahuje výhradne GET funkcie. Žiadne POST/PUT/DELETE.
Nepodáva, nemení, ani neruší príkazy. Slúži iba na zobrazenie dát ktoré
participant vidí na svojom účte.

Auth: mTLS (client certificate) + voliteľne username/password (basic auth).
Cert sa typicky exportuje z macOS Keychain ako .p12 (s heslom), potom
sa konvertuje na .crt + .key cez `install_okte_cert.sh`.

Endpointy (configurable):
  Base: https://isot.okte.sk/api/v1
  - GET /participant/idm/orders          — vlastné aktívne príkazy
  - GET /idm/orderbook?product=YYYYMMDDHHMM  — verejný orderbook pre 15-min produkt
  - GET /participant/idm/trades?dateFrom=...&dateTo=...  — vlastné obchody
  - GET /participant/account             — pozícia + balance

POZNÁMKA: OKTE participant API URL štruktúra je do času prvého testu odhadnutá
podľa štandardných konvencií. Skutočné endpointy uvidíme z 401/404 a doladíme
v config-u (okte_vdt_config.json) bez zmeny kódu.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import time
from typing import Optional, Dict, Any, List
import requests
import pandas as pd


_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _data_dir() -> str:
    """OKTE VDT je SK-specific (slovenský operátor) — bez ohľadu na aktívny market.
    Vždy vracia out/sk/. Aktívny CZ market by inak hľadal v out/cz/ a vrátil prázdny config.
    """
    try:
        import market as _mk
        # Použi market.data_dir(market='sk') keď existuje, inak ručná konštrukcia
        root = os.path.dirname(_mk.data_dir().rstrip("/").rstrip(os.sep)) or "out"
        return os.path.join(root, "sk")
    except Exception:
        return os.path.join("out", "sk")


def config_path() -> str:
    return os.path.join(_data_dir(), "okte_vdt_config.json")


# ─── Config ──────────────────────────────────────────────────────────────────
# URL paths podľa OKTE technickej špecifikácie XMtrade/ISOT v1.23 (jún 2026)
# Tabuľka 43 — Prehľad WEB API IDM. Všetko READ-ONLY (GET) tu uvedené.
DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    # PDF v1.23 kap. 3.3: port 443 = striktná mTLS (vyžaduje špecifický cert profile),
    # port 8443 = menej striktná mTLS — odporúčaný štart, mení sa cez UI ak treba.
    "base_url": "https://isot.okte.sk:8443/api/v1",
    "cert_path": "",            # .crt v PEM formáte (z install_okte_cert.sh)
    "key_path": "",             # .key v PEM formáte (z install_okte_cert.sh)
    "verify_ssl": True,
    "username": "",             # voliteľné basic auth (OKTE typicky iba mTLS)
    "password": "",
    "timeout_s": 20,
    # EIC kódy pre SOAP IdmOrderBook (kniha objednávok = aktuálne bids/asks).
    # FORMÁT: "24X-XXXXXXXXXXX-X" (16 znakov). Účastníka nájdeš v OKTE portáli
    # (Účastník trhu → Detail). OKTE receiver je vždy "24X-OT-SK------V".
    "sender_eic":   "",
    "receiver_eic": "24X-OT-SK------V",
    "soap_url":     "https://isot.okte.sk/interfaces/IdmOrderBook/Service.svc",
    "endpoints": {
        # GET zoznam vlastných objednávok (?{query} filter — voliteľný)
        "orders":             "/idm/orders/",
        # GET detail objednávky podľa ID
        "order_detail":       "/idm/orders/{orderid}",
        # GET zoznam obchodov k objednávke
        "order_trades":       "/idm/orders/{orderid}/trades",
        # GET zoznam vlastných obchodov
        "trades":             "/idm/trades",
        # GET súhrnné/denné/mesačné vyhodnotenia VDT
        "eval_daily_summary": "/idm/evaluations/daily-summary",
        "eval_daily_detail":  "/idm/evaluations/daily-detail",
        "eval_monthly_summary":"/idm/evaluations/monthly-summary",
        # GET hub-to-hub matica (cezhraničné kapacity)
        "hub_to_hub":         "/idm/hub-to-hub",
        # GET aktuálny stav trhu (najjednoduchší endpoint na test)
        "market_status":      "/idm/market-status",
    },
    "last_check_ts": None,
    "last_check_status": None,
}


def load_config() -> Dict[str, Any]:
    p = config_path()
    if not os.path.exists(p):
        return dict(DEFAULT_CONFIG)
    try:
        with open(p) as f:
            cfg = json.load(f)
        # Merge defaults pre chýbajúce kľúče
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v
        # AUTO-MIGRÁCIA endpoints: ak chýba 'market_status', config je z prv-deploymentu
        # (predchádzajúce URL paths boli odhady — všetky 404). Prepíšeme cely endpoints
        # dict default-mi z PDF špec v1.23.
        eps = cfg.get("endpoints") or {}
        needs_resave = False
        if "market_status" not in eps or "/participant/" in (eps.get("orders") or ""):
            cfg["endpoints"] = dict(DEFAULT_CONFIG["endpoints"])
            print("[okte_vdt] auto-migrácia endpoints na PDF v1.23 paths")
            needs_resave = True
        else:
            for ek, ev in DEFAULT_CONFIG["endpoints"].items():
                if ek not in cfg["endpoints"]:
                    cfg["endpoints"][ek] = ev
                    needs_resave = True
        # AUTO-MIGRÁCIA base_url: ak je na porte 443 (default) a hlási SSL handshake,
        # prepneme na 8443 (menej striktná mTLS — PDF v1.23 odporúča).
        url = cfg.get("base_url") or ""
        if "isot.okte.sk/" in url and ":8443" not in url and ":443" not in url:
            # URL bez explicitného portu (default 443) → migruj na 8443
            cfg["base_url"] = url.replace("isot.okte.sk/", "isot.okte.sk:8443/")
            print(f"[okte_vdt] auto-migrácia base_url na port 8443 (menej striktná mTLS)")
            needs_resave = True
        if needs_resave:
            try:
                save_config(cfg)
            except Exception:
                pass
        return cfg
    except Exception as e:
        print(f"[okte_vdt.load_config] zlyhalo: {e}")
        return dict(DEFAULT_CONFIG)


def save_config(cfg: Dict[str, Any]) -> None:
    p = config_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ─── Session s mTLS ──────────────────────────────────────────────────────────
_SESSION: Optional[requests.Session] = None
_SESSION_CFG_SIG: str = ""


def _cfg_sig(cfg: Dict[str, Any]) -> str:
    """Hash relevantných config polí — ak sa zmenia, session sa znovu otvorí."""
    return f"{cfg.get('cert_path')}::{cfg.get('key_path')}::{cfg.get('username')}::{cfg.get('base_url')}"


def _get_session(cfg: Dict[str, Any]) -> requests.Session:
    """Vráti perzistentnú Session s nakonfigurovaným client cert + headers."""
    global _SESSION, _SESSION_CFG_SIG
    sig = _cfg_sig(cfg)
    if _SESSION is not None and sig == _SESSION_CFG_SIG:
        return _SESSION
    s = requests.Session()
    cert_p = cfg.get("cert_path") or ""
    key_p = cfg.get("key_path") or ""
    if cert_p and key_p and os.path.exists(cert_p) and os.path.exists(key_p):
        s.cert = (cert_p, key_p)
    s.headers.update({
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
    })
    if cfg.get("username") and cfg.get("password"):
        s.auth = (cfg["username"], cfg["password"])
    s.verify = bool(cfg.get("verify_ssl", True))
    _SESSION = s
    _SESSION_CFG_SIG = sig
    return s


def _request(method: str, path: str, params: Optional[dict] = None,
              cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Vykoná HTTP request. Vracia {ok, status, data, error, url}."""
    if method.upper() != "GET":
        return {"ok": False, "error": f"Iba GET je povolený (READ-ONLY); volané {method}",
                "status": 0, "data": None, "url": ""}
    if cfg is None:
        cfg = load_config()
    if not cfg.get("enabled"):
        return {"ok": False, "error": "VDT modul je vypnutý (enabled=false)",
                "status": 0, "data": None, "url": ""}
    base = (cfg.get("base_url") or "").rstrip("/")
    url = f"{base}{path}"
    s = _get_session(cfg)
    timeout = float(cfg.get("timeout_s") or 20)
    try:
        r = s.get(url, params=params, timeout=timeout)
        status = r.status_code
        # JSON alebo text
        try:
            data = r.json() if r.text else None
        except Exception:
            data = {"_raw_text": r.text[:1000]}
        out = {"ok": (200 <= status < 300), "status": status,
               "url": r.url, "data": data,
               "error": "" if (200 <= status < 300) else f"HTTP {status}"}
        if not out["ok"]:
            # Skrátený diag
            body = (r.text or "")[:300]
            out["error"] = f"HTTP {status} · {body}"
        return out
    except requests.exceptions.SSLError as e:
        return {"ok": False, "error": f"SSL/mTLS error: {e}", "status": 0,
                "url": url, "data": None}
    except requests.exceptions.ConnectionError as e:
        return {"ok": False, "error": f"Connection error: {e}", "status": 0,
                "url": url, "data": None}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "status": 0,
                "url": url, "data": None}


# ─── READ-ONLY public funkcie ────────────────────────────────────────────────
def get_market_status() -> Dict[str, Any]:
    """Stav trhu (najjednoduchší endpoint — bez query params, ideálne na test).
    Response: {systemTime, tradeDay, tradingStatus: xbidOk|xbidNok|xbidHalt|...}."""
    cfg = load_config()
    path = cfg["endpoints"].get("market_status", "/idm/market-status")
    return _request("GET", path, cfg=cfg)


def get_orders(**query) -> Dict[str, Any]:
    """Vlastné objednávky (zoznam) — PDF Tabuľka 55.

    Povinné: aspoň 1 zo skupiny createdFrom, updatedFrom, deliveryFrom, groupId, id.
    Voliteľné: deliveryTo, state, ...

    Default: deliveryFrom=dnes 00:00 UTC, deliveryTo=zajtra 00:00 UTC.
    """
    cfg = load_config()
    path = cfg["endpoints"].get("orders", "/idm/orders")
    if not query:
        today = dt.date.today()
        tomorrow = today + dt.timedelta(days=1)
        query = {
            "deliveryFrom": f"{today.isoformat()}T00:00:00Z",
            "deliveryTo":   f"{tomorrow.isoformat()}T00:00:00Z",
        }
    return _request("GET", path, params=query, cfg=cfg)


def get_order_detail(orderid: str) -> Dict[str, Any]:
    """Detail jednej objednávky podľa ID."""
    cfg = load_config()
    path_tmpl = cfg["endpoints"].get("order_detail", "/idm/orders/{orderid}")
    return _request("GET", path_tmpl.replace("{orderid}", str(orderid)), cfg=cfg)


def get_order_trades(orderid: str) -> Dict[str, Any]:
    """Zoznam obchodov k jednej objednávke (čiastočné výplne)."""
    cfg = load_config()
    path_tmpl = cfg["endpoints"].get("order_trades", "/idm/orders/{orderid}/trades")
    return _request("GET", path_tmpl.replace("{orderid}", str(orderid)), cfg=cfg)


def get_trades(delivery_from: Optional[str] = None,
                delivery_to: Optional[str] = None,
                **extra) -> Dict[str, Any]:
    """Vlastné spárované obchody — PDF Tabuľka 61.

    Povinné: aspoň 1 zo skupiny timeFrom, deliveryFrom, orderId, id.
    Default: deliveryFrom=dnes 00:00 UTC, deliveryTo=zajtra 00:00 UTC.
    """
    cfg = load_config()
    path = cfg["endpoints"].get("trades", "/idm/trades")
    today = dt.date.today()
    tomorrow = today + dt.timedelta(days=1)
    params = {
        "deliveryFrom": delivery_from or f"{today.isoformat()}T00:00:00Z",
        "deliveryTo":   delivery_to   or f"{tomorrow.isoformat()}T00:00:00Z",
    }
    params.update(extra)
    return _request("GET", path, params=params, cfg=cfg)


def get_evaluations_daily_summary(delivery_day_from: Optional[str] = None,
                                    delivery_day_to: Optional[str] = None) -> Dict[str, Any]:
    """Súhrnné denné vyhodnotenie VDT — PDF Tabuľka 64.
    Povinné: deliveryDayFrom, deliveryDayTo (formát YYYY-MM-DD)."""
    cfg = load_config()
    path = cfg["endpoints"].get("eval_daily_summary", "/idm/evaluations/daily-summary")
    today = dt.date.today().isoformat()
    params = {
        "deliveryDayFrom": delivery_day_from or today,
        "deliveryDayTo":   delivery_day_to   or today,
    }
    return _request("GET", path, params=params, cfg=cfg)


def get_evaluations_daily_detail(delivery_from: Optional[str] = None,
                                    delivery_to: Optional[str] = None) -> Dict[str, Any]:
    """Podrobné denné vyhodnotenie VDT (per 15-min) — PDF Tabuľka 70.
    Povinné: deliveryFrom, deliveryTo (formát YYYY-MM-DDTHH:mm:SSZ)."""
    cfg = load_config()
    path = cfg["endpoints"].get("eval_daily_detail", "/idm/evaluations/daily-detail")
    today = dt.date.today()
    tomorrow = today + dt.timedelta(days=1)
    params = {
        "deliveryFrom": delivery_from or f"{today.isoformat()}T00:00:00Z",
        "deliveryTo":   delivery_to   or f"{tomorrow.isoformat()}T00:00:00Z",
    }
    return _request("GET", path, params=params, cfg=cfg)


def get_evaluations_monthly_summary(delivery_day_from: Optional[str] = None,
                                       delivery_day_to: Optional[str] = None) -> Dict[str, Any]:
    """Mesačné súhrnné vyhodnotenie VDT — PDF Tabuľka 67.
    Povinné: deliveryDayFrom, deliveryDayTo (formát YYYY-MM-DD).
    Default: 1. deň aktuálneho mesiaca → posledný deň aktuálneho mesiaca."""
    cfg = load_config()
    path = cfg["endpoints"].get("eval_monthly_summary", "/idm/evaluations/monthly-summary")
    today = dt.date.today()
    first = today.replace(day=1)
    # posledný deň mesiaca = nasledujúci mesiac 1. - 1 deň
    if today.month == 12:
        last = today.replace(year=today.year + 1, month=1, day=1) - dt.timedelta(days=1)
    else:
        last = today.replace(month=today.month + 1, day=1) - dt.timedelta(days=1)
    params = {
        "deliveryDayFrom": delivery_day_from or first.isoformat(),
        "deliveryDayTo":   delivery_day_to   or last.isoformat(),
    }
    return _request("GET", path, params=params, cfg=cfg)


def get_hub_to_hub(**query) -> Dict[str, Any]:
    """Cezhraničné kapacity vo forme Hub-to-Hub matice."""
    cfg = load_config()
    path = cfg["endpoints"].get("hub_to_hub", "/idm/hub-to-hub")
    return _request("GET", path, params=(query or None), cfg=cfg)


# Spätná kompatibilita — staré meno
def get_account() -> Dict[str, Any]:
    """DEPRECATED — OKTE API nemá samostatný /account endpoint.
    Pozícia + balance sú v evaluations. Zachované pre kompatibilitu so starým UI."""
    return get_evaluations_daily_summary()


def fetch_wsdl() -> Dict[str, Any]:
    """GET WSDL zo SOAP endpointu cez náš mTLS klient session.

    WSDL obsahuje všetky podporované operácie + ich presné Action URIs.
    READ-ONLY metadata fetch — nemení nič na strane servera.
    """
    cfg = load_config()
    if not cfg.get("enabled"):
        return {"ok": False, "error": "Modul disabled"}
    soap_url = cfg.get("soap_url") or "https://isot.okte.sk/interfaces/IdmOrderBook/Service.svc"
    wsdl_url = soap_url + "?wsdl"
    s = _get_session(cfg)
    try:
        r = s.get(wsdl_url, timeout=float(cfg.get("timeout_s") or 20))
        return {
            "ok": r.status_code == 200,
            "status": r.status_code,
            "url": wsdl_url,
            "content": r.text[:200000],   # WSDL môže byť veľký
            "size_b": len(r.text),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "url": wsdl_url}


def parse_wsdl_operations(wsdl_xml: str) -> List[Dict[str, str]]:
    """Z WSDL XML vytiahne zoznam operations + ich Action URIs.

    Returns list of dicts: [{"name": "...", "action": "...", "input_msg": "..."}]
    """
    import xml.etree.ElementTree as ET
    operations = []
    try:
        root = ET.fromstring(wsdl_xml)
    except Exception:
        return operations

    # WSDL používa rôzne namespaces — strip pri každom tagu
    def _local(tag):
        return tag.split("}", 1)[1] if "}" in tag else tag

    # Hľadaj <wsdl:operation> elementy v binding (kde sú soapAction atribúty)
    for el in root.iter():
        if _local(el.tag) != "operation":
            continue
        # Skip portType operations — chceme binding operations s soapAction
        op_name = el.get("name") or ""
        action = ""
        for child in el:
            if _local(child.tag) == "operation":
                action = child.get("soapAction") or child.get("Action") or ""
                break
        if op_name:
            operations.append({"name": op_name, "action": action})

    # Dedup podľa name
    seen = set()
    uniq = []
    for op in operations:
        if op["name"] in seen:
            continue
        seen.add(op["name"])
        uniq.append(op)
    return uniq


_OB_CACHE: Dict[Any, Any] = {}   # {delivery_duration: (mono_ts, result)} — OB-CACHE
_OB_TTL_S = 4.0


def get_orderbook(delivery_duration: Optional[int] = None) -> Dict[str, Any]:
    """OB-CACHE (2026-07-06): tenký TTL wrapper (4 s) nad `_get_orderbook_uncached`. SOAP orderbook
    fetch je drahý (network) a volal sa PER PROFIL per tick (VDT worker 12 profilov = 12× ten istý
    trhový orderbook → hlavný podiel na 18 s/tick). Cache = jeden fetch za tick, zdieľaný cez profily;
    ďalší tick (>4 s) fetchne fresh → VDT stále reaguje na aktuálne ceny. Cachujeme len úspech."""
    import time as _t
    _k = delivery_duration
    _c = _OB_CACHE.get(_k)
    if _c and (_t.monotonic() - _c[0]) < _OB_TTL_S:
        return _c[1]
    _res = _get_orderbook_uncached(delivery_duration)
    if isinstance(_res, dict) and _res.get("ok"):
        _OB_CACHE[_k] = (_t.monotonic(), _res)
    return _res


def _get_orderbook_uncached(delivery_duration: Optional[int] = None) -> Dict[str, Any]:
    """**Trhový orderbook** (všetky bids/asks na trhu, anonymizované) cez SOAP IdmOrderBook.

    PDF kap. 3.1.5 + 4.4.2 — message-code=810 (CDSREQ-VDT.810).
    Vracia ISOTEDATA-VDT.812 XML so VŠETKÝMI aktívnymi ponukami nákupu/predaja
    od všetkých účastníkov SIDC trhu (anonymizované cez seq-num).

    Args:
        delivery_duration: None = všetky produkty (15m + 60m).
                           15 = iba 15-min sloty.
                           60 = iba hodinové produkty.

    SOAP envelope obsahuje:
      - WS-Addressing: Action, ReplyTo, MessageID, To
      - WS-Security: UsernameToken (username/password z config), Timestamp
      - Body: DownloadRequest s CDSREQ-VDT.810
    """
    import xml.etree.ElementTree as ET
    import uuid
    cfg = load_config()
    if not cfg.get("enabled"):
        return {"ok": False, "error": "Modul disabled", "status": 0, "data": None}
    cert_p = cfg.get("cert_path", "")
    key_p = cfg.get("key_path", "")
    if not (cert_p and key_p and os.path.exists(cert_p) and os.path.exists(key_p)):
        return {"ok": False, "error": "cert/key chýba", "status": 0, "data": None}

    sender_eic = cfg.get("sender_eic") or ""
    receiver_eic = cfg.get("receiver_eic") or "24X-OT-SK------V"
    if not sender_eic:
        return {"ok": False, "error": (
                    "sender_eic nie je nakonfigurovaný. Otvor out/sk/okte_vdt_config.json "
                    "a doplň svoj EIC kód účastníka (format 24X-XXXXXXXXXX-X)."),
                "status": 0, "data": None}

    username = cfg.get("username") or ""
    password = cfg.get("password") or ""
    if not username or not password:
        return {"ok": False, "error": (
                    "WS-Security vyžaduje username + password v config-u. "
                    "Doplň ich do okte_vdt_config.json."),
                "status": 0, "data": None}

    soap_url = cfg.get("soap_url") or "https://isot.okte.sk/interfaces/IdmOrderBook/Service.svc"
    now_utc = dt.datetime.utcnow()
    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%S")
    ts_created = now_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    ts_expires = (now_utc + dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    req_id = f"vdt-{int(now_utc.timestamp() * 1000)}"
    msg_id = str(uuid.uuid4())

    # SOAP Action URI — podľa WSDL (Download operation input action):
    #   http://sfera.sk/xmtrade/isot/services/IDMOrderBook/2016/04/IDMOrderBookContract/Download
    # POZOR: contract je "IDMOrderBookContract" (BEZ prefix "I") a service path
    # je "services/IDMOrderBook/2016/04" (NIE "ws/.../interfaces/.../services/...")
    action_uri = ("http://sfera.sk/xmtrade/isot/services/IDMOrderBook/2016/04/"
                   "IDMOrderBookContract/Download")

    # CDSREQ-VDT.810 — vyžaduje aspoň jeden Trade element s trade-day,
    # aby server vedel za ktorý deň fetchnúť orderbook.
    trade_day = now_utc.strftime("%Y-%m-%d")
    if delivery_duration in (15, 60):
        trade_elem = f'<Trade trade-day="{trade_day}" delivery-duration="{int(delivery_duration)}"/>'
    else:
        trade_elem = f'<Trade trade-day="{trade_day}"/>'

    # XML escape pre password (znak # je validný, ale & < > " ' treba escape-ovať)
    import html as _h
    pwd_escaped = _h.escape(password, quote=True)
    user_escaped = _h.escape(username, quote=True)

    # WS-Security + WS-Addressing v hlavičke + Body s CDSREQ
    soap_body = f'''<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:u="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
  <s:Header>
    <a:Action s:mustUnderstand="1">{action_uri}</a:Action>
    <a:MessageID>urn:uuid:{msg_id}</a:MessageID>
    <a:ReplyTo>
      <a:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address>
    </a:ReplyTo>
    <a:To s:mustUnderstand="1">{soap_url}</a:To>
    <o:Security xmlns:o="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <u:Timestamp u:Id="TS-1">
        <u:Created>{ts_created}</u:Created>
        <u:Expires>{ts_expires}</u:Expires>
      </u:Timestamp>
      <o:UsernameToken u:Id="UT-1">
        <o:Username>{user_escaped}</o:Username>
        <o:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">{pwd_escaped}</o:Password>
      </o:UsernameToken>
    </o:Security>
  </s:Header>
  <s:Body>
    <DownloadRequestMessage xmlns="http://sfera.sk/xmtrade/isot/services/idmorderbook/2016/04">
      <DownloadRequest>
        <CDSREQ date-time="{now_iso}" dtd-release="1" dtd-version="1" id="{req_id}" message-code="810" xmlns="http://sfera.sk/ws/xmtrade/isot/interfaces/ut/types/2009/04/01">
          <SenderIdentification coding-scheme="15" id="{sender_eic}"/>
          <ReceiverIdentification coding-scheme="15" id="{receiver_eic}"/>
          {trade_elem}
        </CDSREQ>
      </DownloadRequest>
    </DownloadRequestMessage>
  </s:Body>
</s:Envelope>'''

    # WS-Security X.509 digital signature (xmldsig) — povinné pre OKTE IdmOrderBook.
    # WSDL hovorí AlgorithmSuite=Basic256 → SHA-1 + RSA-SHA1 (default).
    # Pre debug možno vypnúť cez "wsse_signed": false, alebo prepnúť na SHA-256
    # cez "wsse_algorithm_suite": "Basic256Sha256".
    wsse_signed = cfg.get("wsse_signed", True)
    algorithm_suite = cfg.get("wsse_algorithm_suite", "Basic256")
    soap_body_final = soap_body
    if wsse_signed:
        try:
            import okte_wsse as _wsse
            soap_body_final = _wsse.sign_soap_envelope(
                soap_body, cert_p, key_p,
                body_id="Body-1", ts_id="TS-1",
                algorithm_suite=algorithm_suite,
            )
        except Exception as e:
            return {"ok": False, "error": f"WSSE podpis zlyhal: {e}",
                    "status": 0, "data": None}

    s = _get_session(cfg)
    body_bytes = (soap_body_final.encode("utf-8")
                  if isinstance(soap_body_final, str) else soap_body_final)
    # Debug payload — vrátime pri akomkoľvek výsledku, na ladenie
    debug = {
        "url": soap_url,
        "action_uri": action_uri,
        "wsse_signed": wsse_signed,
        "request_size_b": len(body_bytes),
        "request_body": soap_body_final if isinstance(soap_body_final, str) else soap_body_final.decode("utf-8", "replace"),
    }
    try:
        r = s.post(
            soap_url,
            data=body_bytes,
            headers={
                # SOAPAction v Content-Type (SOAP 1.2 syntax)
                "Content-Type": f'application/soap+xml; charset=utf-8; action="{action_uri}"',
                "Accept": "application/soap+xml, application/xml, text/xml, */*",
            },
            timeout=float(cfg.get("timeout_s") or 20),
        )
    except Exception as e:
        debug["response_body"] = ""
        debug["status"] = 0
        return {"ok": False, "error": f"SOAP request zlyhal: {e}",
                "status": 0, "data": None, "debug": debug}

    status = r.status_code
    debug["status"] = status
    debug["response_size_b"] = len(r.text or "")
    # Plný response body — UI render ho zoreže pre zobrazenie, ale parser
    # a /vdt/raw_orderbook ho potrebujú celý
    debug["response_body"] = (r.text or "")
    debug["response_headers"] = dict(r.headers)
    if status != 200:
        return {"ok": False, "status": status,
                "error": f"HTTP {status} · {(r.text or '')[:300]}",
                "url": soap_url, "data": None, "debug": debug}

    # Parse XML response
    try:
        root = ET.fromstring(r.text)
    except Exception as e:
        return {"ok": False, "status": status,
                "error": f"XML parse fail: {e}",
                "raw_xml_head": r.text[:1000], "debug": debug}

    # Hľadaj ISOTEDATA-VDT.812 v body (namespace-agnostic)
    isotedata = None
    for el in root.iter():
        if el.tag.endswith("}ISOTEDATA") or el.tag == "ISOTEDATA":
            if el.get("message-code") == "812":
                isotedata = el; break
    if isotedata is None:
        # Skús nájsť RESPONSE element s Reason (server vrátil business-level error)
        reasons = []
        for el in root.iter():
            local = el.tag.split("}", 1)[1] if "}" in el.tag else el.tag
            if local == "Reason":
                rtype = el.get("type", "")
                rtext = (el.text or "").strip()
                rcode = el.get("code", "")
                reasons.append(f"[{rtype}{'·'+rcode if rcode else ''}] {rtext}")
        err_msg = "ISOTEDATA.812 nenájdená"
        if reasons:
            err_msg += " · Server Reason: " + " | ".join(reasons)
        return {"ok": False, "status": status,
                "error": err_msg,
                "raw_xml_head": r.text[:1500], "debug": debug,
                "reasons": reasons}

    # Parse Trade bloky — kategorizuj na bids (trade-type=N) a asks (trade-type=P)
    orders = {"hourly": {"bids": {}, "asks": {}},
              "quarterly": {"bids": {}, "asks": {}}}
    stats = {"trades_parsed": 0}

    def _strip_ns(tag):
        return tag.split("}", 1)[1] if "}" in tag else tag

    def _period_to_str(pfrom, pto, period_minutes):
        """Konvertuje OKTE period indexes (1-based) na HH:MM-HH:MM string.

        period 1 (15-min) = 00:00-00:15
        period 48 (15-min) = 11:45-12:00
        period 1 (60-min) = 00:00-01:00
        """
        try:
            start_min = (int(pfrom) - 1) * period_minutes
            end_min = (int(pto) - 1) * period_minutes
            sh, sm = divmod(start_min, 60)
            eh, em = divmod(end_min, 60)
            return f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
        except Exception:
            return f"{pfrom}-{pto}"

    for trade in isotedata:
        if _strip_ns(trade.tag) != "Trade":
            continue
        dur = trade.get("delivery-duration", "")
        ttype = trade.get("trade-type", "")  # N=nákup (bid), P=predaj (ask)
        bucket = "hourly" if dur == "60" else "quarterly" if dur == "15" else None
        if bucket is None:
            continue
        side = "bids" if ttype == "N" else ("asks" if ttype == "P" else None)
        if side is None:
            continue   # info-only Trade (TC01/LC01/LP01 stats)

        period_min = 60 if bucket == "hourly" else 15

        # Zber ProfileData per role. Každý ProfileData má 1 Data so seq-num
        # indikujúcim depth level (1 = top of book) v rámci jednej periódy.
        bc01 = {}  # period_str → {seq → MW}
        bp01 = {}  # period_str → {seq → EUR}
        for pd in trade:
            if _strip_ns(pd.tag) != "ProfileData":
                continue
            role = pd.get("profile-role", "")
            for d in pd:
                if _strip_ns(d.tag) != "Data":
                    continue
                pfrom = d.get("period-from", "?")
                pto = d.get("period-to", "?")
                period = _period_to_str(pfrom, pto, period_min)
                seq = d.get("seq-num", "1")
                try:
                    val = float(d.get("value", 0))
                except (ValueError, TypeError):
                    continue
                if role == "BC01":
                    bc01.setdefault(period, {})[seq] = val
                elif role == "BP01":
                    bp01.setdefault(period, {})[seq] = val
        # Spojiť MW + EUR per (period, seq)
        for period in sorted(set(list(bc01.keys()) + list(bp01.keys()))):
            for seq in sorted(set(list(bc01.get(period, {}).keys()) + list(bp01.get(period, {}).keys()))):
                mw = bc01.get(period, {}).get(seq)
                eur = bp01.get(period, {}).get(seq)
                if mw is None or eur is None:
                    continue
                orders[bucket][side].setdefault(period, []).append({
                    "seq": int(seq), "mw": mw, "eur": eur,
                })
                stats["trades_parsed"] += 1

    # Stats — top of book per produkt
    top_of_book = {}
    for bucket, sides in orders.items():
        for period in set(list(sides["bids"].keys()) + list(sides["asks"].keys())):
            bids = sorted(sides["bids"].get(period, []), key=lambda x: -x["eur"])  # bids: highest first
            asks = sorted(sides["asks"].get(period, []), key=lambda x: x["eur"])   # asks: lowest first
            top_of_book.setdefault(bucket, {})[period] = {
                "best_bid": bids[0] if bids else None,
                "best_ask": asks[0] if asks else None,
                "n_bids": len(bids), "n_asks": len(asks),
                "spread_eur": (asks[0]["eur"] - bids[0]["eur"])
                                 if (bids and asks) else None,
            }

    return {
        "ok": True, "status": status, "url": soap_url,
        "trade_day_extract_ts": now_iso,
        "stats": stats,
        "top_of_book": top_of_book,
        "orders": orders,
        "debug": debug,
    }


def get_products(date: Optional[str] = None) -> Dict[str, Any]:
    """DEPRECATED — OKTE API nemá /idm/products endpoint. Použi market-status alebo orders."""
    return {"ok": False, "error": "/idm/products neexistuje v OKTE API. Použi market-status pre stav trhu.",
            "status": 0, "data": None, "url": "(not implemented)"}


# ─── Probe + status ──────────────────────────────────────────────────────────
def probe() -> Dict[str, Any]:
    """Otestuje pripojenie — overí cert, dosažiteľnosť všetkých 5 endpointov.
    Vracia diag dict s výsledkom per endpoint. Žiadny write request — bezpečné."""
    cfg = load_config()
    out: Dict[str, Any] = {
        "ok": False,
        "config_ok": True,
        "checks": [],
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
    }
    # Validácia configu
    if not cfg.get("enabled"):
        out["error"] = "enabled=false v config-u — najprv enable v UI"
        out["config_ok"] = False
        return out
    if not cfg.get("cert_path") or not os.path.exists(cfg.get("cert_path", "")):
        out["error"] = f"cert_path neexistuje: {cfg.get('cert_path')}"
        out["config_ok"] = False
        return out
    if not cfg.get("key_path") or not os.path.exists(cfg.get("key_path", "")):
        out["error"] = f"key_path neexistuje: {cfg.get('key_path')}"
        out["config_ok"] = False
        return out

    # Test endpointov v poradí najmenej-rušivý → najviac (GET only).
    # market-status je bez query params → najlepší canary.
    tests = [
        ("market_status",         get_market_status,              {}),
        ("orders",                get_orders,                     {}),
        ("trades",                get_trades,                     {}),
        ("eval_daily_summary",   get_evaluations_daily_summary,  {}),
        ("eval_daily_detail",    get_evaluations_daily_detail,   {}),
        ("eval_monthly_summary", get_evaluations_monthly_summary,{}),
        ("hub_to_hub",            get_hub_to_hub,                 {}),
    ]
    n_ok = 0
    for name, fn, kwargs in tests:
        try:
            res = fn(**kwargs)
            ok = bool(res.get("ok"))
            out["checks"].append({
                "endpoint": name,
                "ok": ok,
                "status": res.get("status"),
                "url": res.get("url"),
                "error": res.get("error", ""),
                "data_type": type(res.get("data")).__name__,
                "data_sample": str(res.get("data"))[:200] if res.get("data") else None,
            })
            if ok:
                n_ok += 1
        except Exception as e:
            out["checks"].append({"endpoint": name, "ok": False, "error": str(e)})

    out["n_ok"] = n_ok
    out["n_total"] = len(tests)
    out["ok"] = n_ok > 0

    # Audit posledný check
    cfg["last_check_ts"] = out["ts"]
    cfg["last_check_status"] = f"{n_ok}/{len(tests)} OK"
    save_config(cfg)
    return out


def discover_endpoints() -> Dict[str, Any]:
    """Discovery: skúsi všetky bežné URL prefixy + path varianty pre OKTE participant API.

    Cieľ: nájsť URL ktorá vracia **niečo iné než IIS 404** (typicky 200, 401, 403, 405).
    Po zistení sa správne paths zapíšu do okte_vdt_config.json automaticky.

    Iba GET requesty — žiadny write.
    """
    cfg = load_config()
    if not cfg.get("enabled"):
        return {"ok": False, "msg": "Modul nie je enabled"}
    base = (cfg.get("base_url") or "https://isot.okte.sk").rstrip("/")
    # Stripni /api/v1 z base — budeme skúšať rôzne prefixy
    server_root = base.split("/api/")[0]

    # Známe OKTE patterns (od najpravdepodobnejšieho)
    prefix_candidates = [
        "/api/v1",                  # current default — nefunguje
        "/api/v2",
        "/api",
        "/dm/api/v1",               # ISOT DM (data management)
        "/dm/api",
        "/trader/api/v1",
        "/trader/api",
        "/portal/api/v1",
        "/portal/api",
        "/participant/api/v1",
        "/participant/api",
        "/idm/api/v1",
        "/idm/api",
        "",                         # root-level
    ]

    # Endpointy ktoré skúšame pre každý prefix
    endpoint_paths = {
        "orders":    ["/orders", "/idm/orders", "/trader/orders", "/order/list",
                      "/Orders", "/idm/Orders", "/orders/list"],
        "trades":    ["/trades", "/idm/trades", "/trader/trades", "/trades/list",
                      "/Trades", "/idm/Trades"],
        "account":   ["/account", "/participant/account", "/balance", "/trader/account",
                      "/Account", "/me"],
        "products":  ["/products", "/idm/products", "/idm/contracts", "/contracts"],
        "orderbook": ["/orderbook", "/idm/orderbook", "/orderBook"],
    }
    # Známy public endpoint na overenie že cert je rozpoznaný (kontrolný)
    control = "/api/v1/dam/results"

    s = _get_session(cfg)
    timeout = float(cfg.get("timeout_s") or 20)

    out: Dict[str, Any] = {
        "ok": True,
        "server_root": server_root,
        "control_check": None,
        "winners": {},   # endpoint → URL ktorá nevrátila 404
        "tried": [],
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
    }

    # 1) Kontrolný probe — overí že cert funguje na známom public endpoint
    try:
        r = s.get(server_root + control, timeout=timeout)
        out["control_check"] = {
            "url": server_root + control,
            "status": r.status_code,
            "ok": (r.status_code == 200),
            "size_kb": len(r.text or "") / 1024.0,
        }
    except Exception as e:
        out["control_check"] = {"url": server_root + control, "error": str(e)}

    # 2) Discovery — pre každý endpoint skúš všetky prefix × path kombinácie
    for ep_name, paths in endpoint_paths.items():
        out["winners"][ep_name] = []
        for prefix in prefix_candidates:
            for path in paths:
                full = server_root + prefix + path
                try:
                    r = s.get(full, timeout=timeout)
                    status = r.status_code
                    # IIS 404 = HTML, application 404 = JSON
                    is_iis_404 = (status == 404 and ("text/html" in (r.headers.get("Content-Type") or "").lower()))
                    is_interesting = (status != 404 or not is_iis_404)
                    out["tried"].append({
                        "endpoint": ep_name, "url": full, "status": status,
                        "ct": (r.headers.get("Content-Type") or "")[:60],
                        "interesting": is_interesting,
                    })
                    if is_interesting:
                        out["winners"][ep_name].append({
                            "url": full, "status": status,
                            "ct": r.headers.get("Content-Type"),
                            "snippet": (r.text or "")[:200],
                        })
                except requests.exceptions.SSLError:
                    out["tried"].append({"endpoint": ep_name, "url": full, "error": "ssl_handshake_fail"})
                except Exception as e:
                    out["tried"].append({"endpoint": ep_name, "url": full, "error": str(e)[:80]})

    # 3) Sumarizuj
    total = len(out["tried"])
    interesting = sum(1 for t in out["tried"] if t.get("interesting"))
    out["summary"] = (f"Skúšaných {total} URL, {interesting} neobyčajných odpovedí (mimo IIS 404)")
    return out


def inspect_cert() -> Dict[str, Any]:
    """Diagnostika klientskeho certifikátu — Subject, Issuer, Validity, Extended Key Usage.

    Postupne skúsi 2 backendy:
      1. Python `cryptography` lib (ak je nainštalovaná)
      2. `openssl x509` CLI (vždy dostupné na macOS) — fallback

    Pomocné info: či cert má v EKU clientAuth, kedy expiruje, kto ho vydal.
    OKTE 443 striktná mTLS môže odmietnuť cert bez clientAuth EKU alebo s nesprávnym CA.
    """
    cfg = load_config()
    cert_p = cfg.get("cert_path", "")
    if not cert_p or not os.path.exists(cert_p):
        return {"ok": False, "error": f"cert_path neexistuje: {cert_p}"}

    # Backend 1: cryptography lib
    try:
        from cryptography import x509
        with open(cert_p, "rb") as f:
            cert_data = f.read()
        cert = x509.load_pem_x509_certificate(cert_data)
        info = {
            "ok": True,
            "backend": "cryptography",
            "subject": cert.subject.rfc4514_string(),
            "issuer": cert.issuer.rfc4514_string(),
            "valid_from": cert.not_valid_before.isoformat(),
            "valid_to": cert.not_valid_after.isoformat(),
            "serial": format(cert.serial_number, 'x'),
            "signature_algorithm": cert.signature_algorithm_oid._name,
        }
        now = dt.datetime.utcnow()
        if cert.not_valid_after < now:
            info["expired"] = True
            info["expired_days_ago"] = (now - cert.not_valid_after).days
        else:
            info["expired"] = False
            info["expires_in_days"] = (cert.not_valid_after - now).days
        try:
            eku_ext = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
            eku_list = [usage._name for usage in eku_ext.value]
            info["extended_key_usage"] = eku_list
            info["has_client_auth"] = ("clientAuth" in eku_list)
        except Exception:
            info["extended_key_usage"] = "(none)"
            info["has_client_auth"] = False
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            info["san"] = [str(n) for n in san.value]
        except Exception:
            info["san"] = None
        return info
    except ImportError:
        pass   # Fallback na openssl CLI
    except Exception as e:
        return {"ok": False, "error": f"cryptography: {type(e).__name__}: {e}"}

    # Backend 2: openssl CLI
    import subprocess
    try:
        # Subject + Issuer + Validity
        r = subprocess.run(
            ["openssl", "x509", "-in", cert_p, "-noout",
             "-subject", "-issuer", "-startdate", "-enddate",
             "-ext", "extendedKeyUsage,subjectAltName", "-serial"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            return {"ok": False, "error": f"openssl error: {r.stderr.strip()[:300]}"}
        out = r.stdout
        info: Dict[str, Any] = {"ok": True, "backend": "openssl-cli", "raw": out}
        # Parse riadky
        for line in out.splitlines():
            ln = line.strip()
            if ln.startswith("subject="):
                info["subject"] = ln.split("=", 1)[1].strip()
            elif ln.startswith("issuer="):
                info["issuer"] = ln.split("=", 1)[1].strip()
            elif ln.startswith("notBefore="):
                info["valid_from"] = ln.split("=", 1)[1].strip()
            elif ln.startswith("notAfter="):
                info["valid_to"] = ln.split("=", 1)[1].strip()
            elif ln.startswith("serial="):
                info["serial"] = ln.split("=", 1)[1].strip()
        # Extended Key Usage v raw outputu
        has_eku = "X509v3 Extended Key Usage" in out
        if has_eku:
            # Vytiahni riadok pod EKU header
            idx = out.find("X509v3 Extended Key Usage")
            if idx >= 0:
                # Nasledujúci ne-prázdny riadok obsahuje EKU
                tail = out[idx:].split("\n")
                eku_value = tail[1].strip() if len(tail) > 1 else ""
                info["extended_key_usage"] = eku_value
                info["has_client_auth"] = ("TLS Web Client Authentication" in eku_value
                                            or "clientAuth" in eku_value)
        else:
            info["extended_key_usage"] = "(EKU extension chýba — môže byť problém pre strict mTLS)"
            info["has_client_auth"] = False
        # Expiry check — parsuj notAfter
        try:
            va = info.get("valid_to", "")
            # OpenSSL format: "Jun  1 12:00:00 2027 GMT"
            from datetime import datetime
            exp = datetime.strptime(va, "%b %d %H:%M:%S %Y %Z")
            now = datetime.utcnow()
            if exp < now:
                info["expired"] = True
                info["expired_days_ago"] = (now - exp).days
            else:
                info["expired"] = False
                info["expires_in_days"] = (exp - now).days
        except Exception as e:
            info["expiry_parse_error"] = str(e)
        return info
    except FileNotFoundError:
        return {"ok": False,
                 "error": "openssl CLI nenájdené ani cryptography lib (pip install cryptography). "
                          "Na macOS by malo openssl byť v /usr/bin/openssl alebo /opt/homebrew/bin/openssl"}
    except Exception as e:
        return {"ok": False, "error": f"openssl: {type(e).__name__}: {e}"}


def tls_handshake_test(port: Optional[int] = None) -> Dict[str, Any]:
    """Detailný TLS handshake test cez `openssl s_client`. Ukáže ktorá fáza
    handshake-u zlyhala (cert chain, cipher, certificate verification).

    Pomáha rozlíšiť:
    - Server odmieta klientský cert (Alert: certificate_required / bad_certificate)
    - Cipher suite mismatch
    - Server cert verification fail (vlastný side, sandbox)
    """
    cfg = load_config()
    cert_p = cfg.get("cert_path", "")
    key_p = cfg.get("key_path", "")
    if not cert_p or not os.path.exists(cert_p):
        return {"ok": False, "error": f"cert_path neexistuje: {cert_p}"}
    if not key_p or not os.path.exists(key_p):
        return {"ok": False, "error": f"key_path neexistuje: {key_p}"}

    # Vytiahni host:port z base_url
    base = cfg.get("base_url") or ""
    import re
    m = re.match(r"https?://([^:/]+)(?::(\d+))?", base)
    if not m:
        return {"ok": False, "error": f"base_url nie je validná URL: {base}"}
    host = m.group(1)
    server_port = port if port is not None else int(m.group(2) or 443)

    import subprocess
    cmd = [
        "openssl", "s_client",
        "-connect", f"{host}:{server_port}",
        "-cert", cert_p,
        "-key", key_p,
        "-servername", host,        # SNI
        "-tls1_2",                   # OKTE typicky podporuje TLS 1.2
        "-verify_return_error",
        "-quiet",
    ]
    try:
        # Pošli "Q" na stdin aby openssl uzavrel pripojenie po handshake
        result = subprocess.run(
            cmd, input="Q\n", capture_output=True, text=True, timeout=15
        )
        return {
            "ok": (result.returncode == 0),
            "host": host,
            "port": server_port,
            "command": " ".join(cmd),
            "returncode": result.returncode,
            "stdout": result.stdout[:3000],
            "stderr": result.stderr[:3000],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timeout (15s)", "host": host, "port": server_port}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def status_summary() -> Dict[str, Any]:
    """Krátky stav — config OK, posledný probe, počet endpointov."""
    cfg = load_config()
    return {
        "enabled": bool(cfg.get("enabled")),
        "cert_exists": bool(cfg.get("cert_path") and os.path.exists(cfg.get("cert_path", ""))),
        "key_exists": bool(cfg.get("key_path") and os.path.exists(cfg.get("key_path", ""))),
        "base_url": cfg.get("base_url"),
        "username": cfg.get("username") or "(none)",
        "last_check_ts": cfg.get("last_check_ts"),
        "last_check_status": cfg.get("last_check_status"),
        "endpoints": cfg.get("endpoints", {}),
    }
