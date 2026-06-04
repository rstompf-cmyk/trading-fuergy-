# -*- coding: utf-8 -*-
"""
internal_historian.py — klient pre interný firemný historian (Bender / Express.js).

Endpoint:  GET http://<host>:8088/tag-data?json={...JSON v URL...}

Payload formát (URL-encoded JSON):
    {
      "requests": [
        {
          "type": 1,                       # 1 = latest hodnota
          "params": {"tags": ["TAG_NAME"], "inclusive": true, "time": null}
        },
        {
          "type": 2,                       # 2 = history range
          "params": {
            "tags": ["C_OKTE_ISOT_15m_final", "C_OKTE_ZCO_15m_final"],
            "from": 1779919200000,         # Unix ms
            "to":   1780005600000,
            "count": 998
          }
        }
      ]
    }

Response formát:
    [                                       # outer = per-request
      [                                     # inner = per-tag (= request.params.tags)
        {
          "values": [
            {
              "begin": <ms>, "end": <ms>,   # interval-okno servera (zber)
              "first": {"value": 133.48, "time": <ms>},   # ← samotný data point
              "last":  {"value": 133.48, "time": <ms>},
              "maxValue": 133.48,
              "minValue": 133.48
            },
            ...
          ]
        },
        {"values": [...]}                   # ďalší tag
      ]
    ]

Auth:
  Sessions cookies: `connect.sid` + `Bender-Authenticate` (UUID).
  Get cookies cez: login form (POST) ALEBO manuálne z prehliadača (env var).
  Pre development:   export BENDER_COOKIES='connect.sid=...; Bender-Authenticate=...'
  Pre prod / Docker: Playwright login flow (TBD — pošle user login curl).

Použitie:
    import internal_historian as ih
    h = ih.Historian("http://192.168.34.31:8088")
    df = h.fetch_history(["C_OKTE_ISOT_15m_final"],
                         from_dt=dt.datetime(2026, 5, 27, 0, 0),
                         to_dt=dt.datetime(2026, 5, 28, 0, 0))
    print(df.head())  # columns: time, tag, value

    latest = h.fetch_latest(["I_WEB_DAMAS_ReWithGCC_3m"])
    print(latest)     # {"I_WEB_DAMAS_ReWithGCC_3m": {"value": 26.3, "time": <Timestamp>}}
"""
from __future__ import annotations
import os
import json
import datetime as dt
from typing import List, Dict, Any, Optional, Union
from urllib.parse import quote

import requests
import pandas as pd


DEFAULT_HOST = os.environ.get("HISTORIAN_HOST", "http://192.168.34.31:8088")
DEFAULT_TIMEOUT_S = 30
_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ─── Parser ──────────────────────────────────────────────────────────────────

def _parse_value_block(values: List[dict], tag_name: str) -> List[dict]:
    """Z jedného values[] arrayu pre 1 tag urob list rows {time, tag, value}."""
    rows = []
    for entry in values or []:
        if not isinstance(entry, dict):
            continue
        # primárny data point je vo `first` (alebo `last`, sú rovnaké pre single sample)
        point = entry.get("first") or entry.get("last")
        if not isinstance(point, dict):
            continue
        t_ms = point.get("time")
        v = point.get("value")
        if t_ms is None or v is None:
            continue
        rows.append({
            "time":  pd.to_datetime(int(t_ms), unit="ms", utc=True),
            "tag":   tag_name,
            "value": float(v),
            "min":   float(entry.get("minValue", v)),
            "max":   float(entry.get("maxValue", v)),
        })
    return rows


def parse_response(response_json: Any, requested_tags_per_request: List[List[str]]) -> pd.DataFrame:
    """Parsuje response array → long DataFrame s columns [time, tag, value, min, max].

    `requested_tags_per_request` = list-of-lists v poradí v akom boli posielané
    (response je v tom istom poradí, lebo neobsahuje tag mená).

    Príklad volania:
        df = parse_response(resp, [["C_OKTE_ISOT_15m_final", "C_OKTE_ZCO_15m_final"]])
    """
    rows = []
    if not isinstance(response_json, list):
        return pd.DataFrame(columns=["time", "tag", "value", "min", "max"])

    for req_idx, per_request in enumerate(response_json):
        if not isinstance(per_request, list):
            continue
        tags_for_req = requested_tags_per_request[req_idx] if req_idx < len(requested_tags_per_request) else []
        for tag_idx, per_tag in enumerate(per_request):
            if not isinstance(per_tag, dict):
                continue
            tag_name = tags_for_req[tag_idx] if tag_idx < len(tags_for_req) else f"tag_{tag_idx}"
            rows.extend(_parse_value_block(per_tag.get("values") or [], tag_name))

    if not rows:
        return pd.DataFrame(columns=["time", "tag", "value", "min", "max"])
    df = pd.DataFrame(rows).sort_values(["tag", "time"]).reset_index(drop=True)
    return df


def pivot_wide(df_long: pd.DataFrame) -> pd.DataFrame:
    """Z long formy (time, tag, value) sprav wide (time + 1 stĺpec na tag)."""
    if df_long.empty:
        return df_long
    return df_long.pivot_table(index="time", columns="tag", values="value", aggfunc="last")


# ─── HTTP klient ─────────────────────────────────────────────────────────────

class Historian:
    """Persistent session na firemný historian. Cookies cez env var alebo manuálne."""

    def __init__(self, host: str = None, cookies: str = None,
                 user: str = None, password: str = None):
        self.host = (host or DEFAULT_HOST).rstrip("/")
        self._sess = requests.Session()
        self._sess.headers.update({
            "User-Agent": _USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.6",
            "Referer": self.host + "/",
        })
        # Auth credentials (pre budúci login flow — momentálne sa nepoužívajú)
        self._user = user or os.environ.get("HISTORIAN_USER")
        self._password = password or os.environ.get("HISTORIAN_PASSWORD")

        # Priorita zdrojov cookies:
        # 1) explicit cookies argument
        # 2) env vars BENDER_COOKIES / HISTORIAN_COOKIES (dev)
        # 3) disk cache (out/sk/historian_cookies.json) — updatovaná Playwright login flow
        cookies = cookies or os.environ.get("BENDER_COOKIES") or os.environ.get("HISTORIAN_COOKIES")
        if cookies:
            self._apply_cookie_string(cookies)
        else:
            disk = self._load_from_disk()
            if disk:
                self._apply_cookie_string(disk.get("cookies", ""))

    @staticmethod
    def _load_from_disk() -> Optional[dict]:
        try:
            import historian_login
            return historian_login.load_cached_cookies()
        except ImportError:
            return None
        except Exception:
            return None

    def _try_disk_refresh(self) -> bool:
        """Naloaduj fresh cookies z disku (po background login refresh)."""
        data = self._load_from_disk()
        if not data:
            return False
        self._sess.cookies.clear()
        self._apply_cookie_string(data.get("cookies", ""))
        return True

    def _trigger_login_subprocess(self) -> bool:
        """Spustí Playwright login synchronne (~5s)."""
        try:
            import historian_login
            ok, _ = historian_login.login_once(verbose=False)
            if ok:
                return self._try_disk_refresh()
        except Exception as e:
            print(f"[internal_historian] auto-login zlyhal: {e}")
        return False

    def _apply_cookie_string(self, cookie_str: str):
        """Parsuje 'a=1; b=2' a vloží do session.cookies."""
        host_no_proto = self.host.replace("https://", "").replace("http://", "").split(":")[0]
        for chunk in cookie_str.split(";"):
            chunk = chunk.strip()
            if "=" in chunk:
                name, val = chunk.split("=", 1)
                self._sess.cookies.set(name.strip(), val.strip(), domain=host_no_proto)

    # ─── Core API ──────────────────────────────────────────────────────────

    def _request(self, payload: dict, timeout: int = DEFAULT_TIMEOUT_S) -> Any:
        """GET /tag-data?json=<URL-encoded JSON>. Auto-relogin pri 401/403."""
        url = self.host + "/tag-data?json=" + quote(json.dumps(payload, separators=(",", ":")), safe="")
        r = self._sess.get(url, timeout=timeout)

        # Auth zlyhanie → skús disk refresh + login subprocess
        if r.status_code in (401, 403):
            print(f"[historian] HTTP {r.status_code} — pokus o cookie refresh")
            renewed = self._try_disk_refresh() or self._trigger_login_subprocess()
            if renewed:
                r = self._sess.get(url, timeout=timeout)
            else:
                raise RuntimeError(f"Historian HTTP {r.status_code}: auto-login zlyhal. "
                                    f"Skontroluj HISTORIAN_PASSWORD env var.")

        # Niektoré Express.js setupy vracajú 302 redirect na login → text/html response
        if r.status_code == 302 or "text/html" in r.headers.get("content-type", "").lower():
            print(f"[historian] dostal som HTML/redirect (session vypršala?) — login retry")
            if self._try_disk_refresh() or self._trigger_login_subprocess():
                r = self._sess.get(url, timeout=timeout)

        if r.status_code != 200:
            raise RuntimeError(f"Historian HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Historian non-JSON response: {e}\nBody: {r.text[:300]}")

    def fetch_latest(self, tags: List[str]) -> Dict[str, Dict[str, Any]]:
        """Aktuálna hodnota pre každý tag.

        Vracia: {tag_name: {"value": float, "time": pd.Timestamp}}
        """
        payload = {"requests": [{
            "type": 1,
            "params": {"tags": list(tags), "inclusive": True, "time": None}
        }]}
        resp = self._request(payload)
        df = parse_response(resp, [list(tags)])
        # latest = posledný riadok per tag
        out = {}
        if not df.empty:
            for tag, sub in df.groupby("tag"):
                last = sub.iloc[-1]
                out[tag] = {"value": float(last["value"]), "time": last["time"]}
        return out

    def fetch_history(self, tags: List[str],
                       from_dt: Union[dt.datetime, pd.Timestamp, int],
                       to_dt: Union[dt.datetime, pd.Timestamp, int],
                       count: int = 998) -> pd.DataFrame:
        """Historický range. from/to môže byť datetime alebo Unix ms (int).

        Vracia LONG DataFrame s columns [time, tag, value, min, max].
        """
        def _to_ms(x):
            if isinstance(x, (int,)) and x > 1_000_000_000_000:
                return int(x)                                  # už je v ms
            if isinstance(x, (int,)) and x > 1_000_000_000:
                return int(x) * 1000                            # sekundy → ms
            if isinstance(x, dt.datetime):
                if x.tzinfo is None:
                    x = x.replace(tzinfo=dt.timezone.utc)
                return int(x.timestamp() * 1000)
            if isinstance(x, pd.Timestamp):
                return int(x.tz_localize("UTC").timestamp() * 1000) if x.tz is None \
                    else int(x.timestamp() * 1000)
            raise TypeError(f"Neviem konvertovať {type(x)} na ms")

        payload = {"requests": [{
            "type": 2,
            "params": {
                "tags":  list(tags),
                "from":  _to_ms(from_dt),
                "to":    _to_ms(to_dt),
                "count": int(count),
            }
        }]}
        resp = self._request(payload)
        return parse_response(resp, [list(tags)])

    # ─── Convenience ──────────────────────────────────────────────────────

    def probe(self, tag: str = "I_WEB_DAMAS_ReWithGCC_3m") -> dict:
        """Diagnostika: skús fetchnúť 1h histórie pre daný tag."""
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        try:
            df = self.fetch_history([tag], now - dt.timedelta(hours=1), now, count=100)
            return {
                "ok": True, "host": self.host, "tag": tag,
                "n_rows": len(df),
                "head": df.head(3).to_dict("records") if not df.empty else [],
                "last_value": float(df["value"].iloc[-1]) if not df.empty else None,
                "last_time": str(df["time"].iloc[-1]) if not df.empty else None,
            }
        except Exception as e:
            return {"ok": False, "host": self.host, "tag": tag, "error": str(e)}


# ─── Singleton + CLI ─────────────────────────────────────────────────────────
_DEFAULT_INSTANCE: Optional[Historian] = None


def default_historian() -> Historian:
    global _DEFAULT_INSTANCE
    if _DEFAULT_INSTANCE is None:
        _DEFAULT_INSTANCE = Historian()
    return _DEFAULT_INSTANCE


if __name__ == "__main__":
    import sys
    import pprint

    if len(sys.argv) > 1 and sys.argv[1] == "parse":
        # Test parser na uloženom JSON: python3 internal_historian.py parse /tmp/hist.json TAG1 TAG2
        with open(sys.argv[2]) as f:
            resp = json.load(f)
        tags = sys.argv[3:] if len(sys.argv) > 3 else []
        df = parse_response(resp, [tags])
        print(f"Parsed {len(df)} rows")
        print(df.head(10))
    else:
        h = Historian()
        tag = sys.argv[1] if len(sys.argv) > 1 else "I_WEB_DAMAS_ReWithGCC_3m"
        print(f"Probe historian @ {h.host}, tag = {tag}")
        pprint.pprint(h.probe(tag))
