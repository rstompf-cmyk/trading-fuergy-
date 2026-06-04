# -*- coding: utf-8 -*-
"""
historian_login.py — Playwright login flow pre interný historian.

Otvorí stránku, vyplní credentials, počká na úspešnú navigáciu, zachytí cookies
+ uloží do out/sk/historian_cookies.json. internal_historian.py ich potom číta.

Použitie:
  # Manuálny refresh (po zmene hesla / pri prvom setup-e):
  export HISTORIAN_USER=admin
  export HISTORIAN_PASSWORD=admin#2023
  python3 historian_login.py

  # Daemon (refresh každú hodinu — defenzívne pred session timeout):
  python3 historian_login.py --daemon

  # Headed debug (vidíš čo browser robí):
  HISTORIAN_HEADED=1 python3 historian_login.py
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
import datetime as dt
from typing import Optional, Tuple

# Auto-load .env (HISTORIAN_PASSWORD, atď.)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


DEFAULT_HOST = os.environ.get("HISTORIAN_HOST", "http://192.168.34.31:8088")
LOGIN_PATH = "/"                  # login form je na root URL
DASHBOARD_PATH = "/dashboard-overview"
PAGE_TIMEOUT_MS = 60_000
WAIT_AFTER_LOGIN_MS = 10_000
COOKIE_LIFETIME_SEC = 50 * 60     # 50 min defenzívne (session typicky 1h)
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _cookies_dir() -> str:
    """Adresár pre cookies — out/sk/ (firemný historian je SK-centric)."""
    try:
        import market as mk
        root = os.path.dirname(mk.data_dir().rstrip("/").rstrip(os.sep)) or "out"
    except Exception:
        root = "out"
    return os.path.join(root, "sk")


def _output_path() -> str:
    return os.path.join(_cookies_dir(), "historian_cookies.json")


def login_once(verbose: bool = True) -> Tuple[bool, str]:
    """Otvorí historian, prihlási sa, uloží cookies. Vracia (ok, msg/path)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, ("Playwright nie je nainštalovaný. "
                       "pip install playwright && playwright install chromium")

    user = os.environ.get("HISTORIAN_USER", "admin")
    password = os.environ.get("HISTORIAN_PASSWORD")
    host = os.environ.get("HISTORIAN_HOST", DEFAULT_HOST)
    if not password:
        return False, "HISTORIAN_PASSWORD env var nie je nastavený"

    headless = os.environ.get("HISTORIAN_HEADED") != "1"

    def log(msg):
        if verbose:
            print(f"[historian-login] {msg}", flush=True)

    log(f"štart — {'HEADLESS' if headless else 'HEADED'}, host {host}, user {user}")

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
            ignore_https_errors=True,
        )
        page = context.new_page()

        # Sleduj XHR-y aby sme vedeli kedy sa stránka prihlásila
        login_post_seen = {"yes": False}
        def on_response(resp):
            if resp.request.method == "POST" and ("login" in resp.url.lower() or "auth" in resp.url.lower()):
                login_post_seen["yes"] = True
                if verbose:
                    print(f"[historian-login]   POST {resp.url} → HTTP {resp.status}", flush=True)
        page.on("response", on_response)

        try:
            log(f"GET {host}{LOGIN_PATH}")
            page.goto(host + LOGIN_PATH, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
        except Exception as e:
            browser.close()
            return False, f"page.goto zlyhal: {e}"

        # Heuristika: nájdi pole pre username + password. Hľadáme bežné name/placeholder.
        try:
            log("hľadám login form...")
            # username field — typicky name=username|user|login|email
            user_field = page.locator(
                "input[name='username'], input[name='user'], input[name='login'], "
                "input[name='email'], input[type='text']:not([type='password']):visible"
            ).first
            pass_field = page.locator("input[type='password']").first

            if user_field.count() == 0 or pass_field.count() == 0:
                browser.close()
                return False, "Nenašiel som username/password polia na stránke"

            log(f"vyplňujem user={user}")
            user_field.fill(user, timeout=5000)
            pass_field.fill(password, timeout=5000)

            # Submit — najprv skús kliknúť tlačidlo, ináč Enter
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

            # Počkaj kým login POST prebehne + presmerovanie na dashboard
            log(f"čakám až {WAIT_AFTER_LOGIN_MS}ms na úspešné prihlásenie")
            deadline = time.time() + WAIT_AFTER_LOGIN_MS / 1000.0
            while time.time() < deadline:
                cur_url = page.url
                if "dashboard" in cur_url.lower() or DASHBOARD_PATH in cur_url:
                    log(f"✓ presmerovaný na dashboard ({cur_url})")
                    break
                if login_post_seen["yes"]:
                    page.wait_for_timeout(1500)
                    break
                page.wait_for_timeout(500)
            else:
                log("⚠ timeout — možno login zlyhal, kontrolujem cookies...")

            # Extra čakanie pre dokončenie redirect-ov
            page.wait_for_timeout(2000)

        except Exception as e:
            browser.close()
            return False, f"login flow zlyhal: {e}"

        # Extract cookies
        cookies_list = context.cookies()
        log(f"# cookies: {len(cookies_list)}")
        for c in cookies_list:
            log(f"  {c.get('name')} = {str(c.get('value',''))[:40]}...")

        # Hľadáme connect.sid + Bender-Authenticate (alebo iné session cookies)
        has_session = any("connect" in c.get("name", "").lower() or
                          "session" in c.get("name", "").lower() or
                          "bender" in c.get("name", "").lower()
                          for c in cookies_list)
        if not has_session or len(cookies_list) == 0:
            browser.close()
            return False, ("Nenastavili sa session cookies — pravdepodobne login zlyhal "
                           f"(zlé heslo?). Cookies: {[c.get('name') for c in cookies_list]}")

        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies_list)
        now_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        expires_at = now_utc + dt.timedelta(seconds=COOKIE_LIFETIME_SEC)

        payload = {
            "host":         host,
            "cookies":      cookie_str,
            "refreshed_at": now_utc.isoformat() + "Z",
            "expires_at":   expires_at.isoformat() + "Z",
            "n_cookies":    len(cookies_list),
            "user":         user,
        }
        os.makedirs(_cookies_dir(), exist_ok=True)
        out_path = _output_path()
        with open(out_path, "w") as f:
            json.dump(payload, f, indent=2)

        browser.close()
        return True, f"{out_path}  ({len(cookie_str)} chars, expires {expires_at:%H:%M} UTC)"


# ─── Helpers pre internal_historian.py ───────────────────────────────────────

def load_cached_cookies() -> Optional[dict]:
    """Vráti uložené cookies ako dict alebo None ak chýbajú / expirované."""
    path = _output_path()
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


# ─── Daemon ──────────────────────────────────────────────────────────────────

def run_daemon(interval_sec: int = 50 * 60):
    print(f"[historian-login] daemon štart (interval {interval_sec}s)")
    failures = 0
    while True:
        ok, msg = login_once(verbose=True)
        if ok:
            print(f"[historian-login] ✓ {msg}")
            failures = 0
        else:
            failures += 1
            print(f"[historian-login] zlyhanie #{failures}: {msg}")
            if failures >= 5:
                print("[historian-login] 5x zlyhanie — končím")
                sys.exit(1)
        time.sleep(interval_sec)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Login do interného historiana cez Playwright.")
    ap.add_argument("--daemon", action="store_true", help="Nepretržite refreshuje cookies")
    ap.add_argument("--interval", type=int, default=50 * 60, help="Daemon interval sek")
    args = ap.parse_args()

    if args.daemon:
        run_daemon(args.interval)
        return

    ok, msg = login_once(verbose=True)
    if not ok:
        print(f"ZLYHANIE: {msg}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ {msg}")


if __name__ == "__main__":
    main()
