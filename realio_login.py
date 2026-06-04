# -*- coding: utf-8 -*-
"""
realio_login.py — Playwright auto-login pre realio host (Bender Express.js dashboard).

Pattern: rovnaký ako `historian_login.py` (pre 192.168.34.31:8088), len konfigurácia sa berie
z `realio.load_config()` namiesto env vars (HISTORIAN_USER/PASSWORD).

Použitie:
    python -c "import realio_login; ok, msg = realio_login.login_once(); print(ok, msg)"

Po úspechu:
    - Cookies sa uložia do realio_config.json (cfg['cookies'] string)
    - Aj do disk cache `out/<market>/realio_cookies.json` s expires_at timestampom
    - Globálna session v realio sa zruší aby sa pri ďalšom volaní použili nové cookies
"""
from __future__ import annotations
import os
import json
import datetime as dt
from typing import Tuple, Optional

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

PAGE_TIMEOUT_MS = 30000
WAIT_AFTER_LOGIN_MS = 8000
COOKIE_LIFETIME_SEC = 30 * 60        # konzervatívne 30 min — session zvyčajne 1h


def _cookies_path() -> str:
    """Per-market cesta pre cached cookies."""
    try:
        import market as _mk
        return os.path.join(_mk.data_dir(), "realio_cookies.json")
    except Exception:
        return "out/realio_cookies.json"


def login_once(verbose: bool = True) -> Tuple[bool, str]:
    """Otvorí dashboard, prihlási sa, uloží cookies. Vracia (ok, msg)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, ("Playwright nie je nainštalovaný. "
                       "pip install playwright && playwright install chromium")

    try:
        import realio as _rio
        cfg = _rio.load_config()
    except Exception as e:
        return False, f"realio.load_config zlyhalo: {e}"

    host = (cfg.get("host") or "").rstrip("/")
    user = cfg.get("username") or "admin"
    password = cfg.get("password") or "admin"
    if not host:
        return False, "host nie je nakonfigurovaný v realio_config.json"

    headless = os.environ.get("REALIO_LOGIN_HEADED") != "1"

    def log(msg):
        if verbose:
            print(f"[realio-login] {msg}", flush=True)

    log(f"štart — {'HEADLESS' if headless else 'HEADED'}, host {host}, user {user}")

    import time
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=headless)
        except Exception as e:
            return False, f"Chromium launch zlyhal: {e}"

        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="sk-SK",
            timezone_id="Europe/Bratislava",
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,         # self-signed LAN cert
        )
        page = context.new_page()

        login_post_seen = {"yes": False}

        def on_response(resp):
            if resp.request.method == "POST" and "login" in resp.url.lower():
                login_post_seen["yes"] = True
                if verbose:
                    print(f"[realio-login]   POST {resp.url} → HTTP {resp.status}", flush=True)
        page.on("response", on_response)

        # Endpoint: /login/ (zistené z 403 redirectu)
        login_url = host + "/login/"
        try:
            log(f"GET {login_url}")
            page.goto(login_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
        except Exception as e:
            browser.close()
            return False, f"page.goto zlyhal: {e}"

        # Heuristika: nájdi username + password polia
        try:
            user_field = page.locator(
                "input[name='username'], input[name='user'], input[name='login'], "
                "input[name='email'], input[type='text']:not([type='password']):visible"
            ).first
            pass_field = page.locator("input[type='password']").first

            if user_field.count() == 0 or pass_field.count() == 0:
                browser.close()
                return False, "Nenašiel som username/password polia na login stránke"

            log(f"vyplňujem user={user}")
            user_field.fill(user, timeout=5000)
            pass_field.fill(password, timeout=5000)

            submit_btn = page.locator(
                "button[type='submit'], input[type='submit'], "
                "button:has-text('Prihlásiť'), button:has-text('Login'), "
                "button:has-text('Sign'), button:has-text('OK')"
            ).first
            if submit_btn.count() > 0:
                log("klikám submit tlačidlo")
                submit_btn.click(timeout=5000)
            else:
                log("submit tlačidlo nenájdené, posielam Enter")
                pass_field.press("Enter")

            log(f"čakám až {WAIT_AFTER_LOGIN_MS}ms na úspešný redirect")
            deadline = time.time() + WAIT_AFTER_LOGIN_MS / 1000.0
            while time.time() < deadline:
                cur_url = page.url
                if "/login" not in cur_url.lower():
                    log(f"✓ redirect z login na {cur_url}")
                    break
                if login_post_seen["yes"]:
                    page.wait_for_timeout(1500)
                    break
                page.wait_for_timeout(500)

            page.wait_for_timeout(1500)
        except Exception as e:
            browser.close()
            return False, f"login flow zlyhal: {e}"

        cookies_list = context.cookies()
        log(f"# cookies: {len(cookies_list)}")
        for c in cookies_list:
            log(f"  {c.get('name')} = {str(c.get('value',''))[:40]}...")

        has_session = any("connect" in c.get("name", "").lower() or
                          "session" in c.get("name", "").lower() or
                          "bender" in c.get("name", "").lower()
                          for c in cookies_list)
        if not has_session or len(cookies_list) == 0:
            browser.close()
            return False, ("Nenastavili sa session cookies — login pravdepodobne zlyhal. "
                           f"Cookies: {[c.get('name') for c in cookies_list]}")

        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies_list)
        now_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        expires_at = now_utc + dt.timedelta(seconds=COOKIE_LIFETIME_SEC)

        # 1) Ulož do disk cache
        payload = {
            "host":         host,
            "cookies":      cookie_str,
            "refreshed_at": now_utc.isoformat() + "Z",
            "expires_at":   expires_at.isoformat() + "Z",
            "n_cookies":    len(cookies_list),
            "user":         user,
        }
        os.makedirs(os.path.dirname(_cookies_path()), exist_ok=True)
        with open(_cookies_path(), "w") as f:
            json.dump(payload, f, indent=2)

        # 2) Aktualizuj realio_config.json — vlož čerstvé cookies
        try:
            import realio as _rio
            cfg = _rio.load_config()
            cfg["cookies"] = cookie_str
            _rio.save_config(cfg)
            # Resetni session aby sa nové cookies hneď použili
            _rio._SESSION = None
            _rio._SESSION_HOST = None
        except Exception as e:
            log(f"⚠ nepodarilo sa zapísať do realio_config: {e}")

        browser.close()
        return True, (f"OK — {len(cookies_list)} cookies, expires {expires_at:%H:%M} UTC "
                       f"(refresh každých {COOKIE_LIFETIME_SEC//60} min)")


def load_cached_cookies() -> Optional[dict]:
    """Vráti cached cookies dict alebo None ak chýbajú / expirované."""
    path = _cookies_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    exp = data.get("expires_at")
    if exp:
        try:
            ts = dt.datetime.fromisoformat(exp.rstrip("Z"))
            if dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) > ts:
                return None
        except ValueError:
            pass
    return data


if __name__ == "__main__":
    ok, msg = login_once(verbose=True)
    print(f"\n{'✓' if ok else '✗'} {msg}")
