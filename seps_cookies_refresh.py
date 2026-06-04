# -*- coding: utf-8 -*-
"""
seps_cookies_refresh.py — udržiavanie autorizovanej session na SEPS DAE.

SEPS DAE (https://dae.sepsas.sk) vyžaduje login. Používame Playwright "storage
state" pattern:

  1) JEDNORÁZOVO: `python3 seps_cookies_refresh.py --login`
     → otvorí headed Chromium → ty sa prihlásiš → script uloží browser state
       (cookies + localStorage) do `out/sk/seps_state.json`.

  2) BEŽNE: `python3 seps_cookies_refresh.py` (alebo cez scheduler každých 25 min)
     → spustí headless Chromium s NAhraným state-om → stránka už je prihlásená
     → JS spraví LoadData call → my zachytíme cookies + X-XSRF-TOKEN → uložíme
       do `out/sk/seps_cookies.json`.
     → tiež uloží AKTUALIZOVANÝ state nazad (server občas rotuje session token).

  3) AŽ SESSION VYPRŠÍ: headless refresh začne dostávať LoginTest fail →
     script vráti chybu → ty znova spustíš `--login` (raz za pár dní/týždňov).

Použitie:
  # Prvý raz (interactive, vyžaduje GUI):
  python3 seps_cookies_refresh.py --login

  # Bežný refresh (headless, beží zo scheduleru):
  python3 seps_cookies_refresh.py

  # Daemon (refreshuje každých 25 min):
  python3 seps_cookies_refresh.py --daemon

  # Headed debug (pozri čo browser robí):
  SEPS_HEADED=1 python3 seps_cookies_refresh.py
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
import datetime as dt
from typing import Optional, Tuple


# ─── Konštanty ───────────────────────────────────────────────────────────────
LOGIN_URL = "https://dae.sepsas.sk/SK_PROD/default.aspx"
DAE_URL = ("https://dae.sepsas.sk/SK_PROD/DAEF-GUI/DAE_MVC/UC/TS_VIEW_SCREEN"
           "?VIEW_CODE=\\SS_PUB\\PUB\\DATAFLOW\\SYSTEM_STATE_MAP_VIEW")
LOAD_DATA_PATTERN = "LoadData"
PAGE_TIMEOUT_MS = 60_000
WAIT_AFTER_LOAD_MS = 20_000
DEFAULT_INTERVAL_SEC = 25 * 60
COOKIE_LIFETIME_SEC = 25 * 60
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _cookies_dir() -> str:
    """Adresár pre SEPS cookies + state. Vždy out/sk/ — SEPS je SK-specific
    bez ohľadu na aktívny market v UI (CZ user môže mať SK monitoring otvorený).
    """
    # market.py vracia adresár aktívneho marketu (CZ/SK), ale SEPS dáta sú
    # vždy SK-specific. Použijeme fixnú cestu out/sk/ relative na out/ root.
    try:
        import market as mk
        # zoberi root out/ (mk.data_dir() pre CZ vracia "out/cz", pre SK "out/sk")
        # → root je parent
        return os.path.join(os.path.dirname(mk.data_dir().rstrip("/").rstrip(os.sep)) or "out", "sk")
    except Exception:
        return os.path.join("out", "sk")


def _output_path() -> str:
    return os.path.join(_cookies_dir(), "seps_cookies.json")


def _state_path() -> str:
    return os.path.join(_cookies_dir(), "seps_state.json")


# ─── Interactive login (jednorázovo) ─────────────────────────────────────────

def do_login() -> Tuple[bool, str]:
    """Otvorí headed Chromium → ty sa prihlásiš → uložíme state."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, ("Playwright nie je nainštalovaný. "
                       "Spusti: pip install playwright && playwright install chromium")

    print("\n━━━ SEPS DAE — INTERACTIVE LOGIN ━━━")
    print("Otvorí sa okno Chromium. Prihlás sa do SEPS DAE.")
    print("Po úspešnom prihlásení a načítaní hodnôt KEEP okno otvorené —")
    print("script automaticky uloží session keď uvidí LoadData request.")
    print("V prípade núdze stlač Enter v terminále pre manuálne uloženie.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        # Načítaj predošlý state ak existuje (aby si nemusel zadávať heslo znova)
        state_arg = {}
        if os.path.exists(_state_path()):
            state_arg["storage_state"] = _state_path()
            print(f"[seps-login] načítavam existujúci state z {_state_path()}")

        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="sk-SK",
            timezone_id="Europe/Bratislava",
            viewport={"width": 1280, "height": 800},
            **state_arg,
        )
        page = context.new_page()

        # Sleduj LoadData — keď príde, sme prihlásení
        load_data_seen = {"yes": False}
        def on_request(req):
            if LOAD_DATA_PATTERN.lower() in req.url.lower() and req.method == "POST":
                load_data_seen["yes"] = True
                print(f"[seps-login] ✓ LoadData zachytený, sme prihlásení!")
        page.on("request", on_request)

        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        print("[seps-login] stránka otvorená — prihlás sa v okne Chromium.")
        print("[seps-login] čakám max 5 min na LoadData request (po úspešnom logine)...")

        deadline = time.time() + 300                   # 5 min na manuálny login
        while time.time() < deadline:
            if load_data_seen["yes"]:
                break
            try:
                page.wait_for_timeout(1000)
            except Exception:
                break

        if not load_data_seen["yes"]:
            print("[seps-login] ⚠ 5 min vypršalo, ale LoadData som nevidel.")
            print("[seps-login] Ak si sa prihlásil ale data sa nezobrazili, "
                  "skús naviguj na System State Map view (pravý horný roh menu).")
            response = input("Mám pokračovať a uložiť state aj tak? [y/N]: ")
            if response.strip().lower() != "y":
                browser.close()
                return False, "User aborted login"

        # Ulož state (cookies + localStorage)
        os.makedirs(_cookies_dir(), exist_ok=True)
        context.storage_state(path=_state_path())
        print(f"[seps-login] ✓ state uložený: {_state_path()}")

        # Tiež ihneď vyextrahuj cookies pre seps_sk
        ok, msg = _capture_and_save(context, verbose=True)
        browser.close()
        return ok, msg


# ─── Headless refresh (bežný flow) ───────────────────────────────────────────

def _capture_and_save(context, verbose: bool) -> Tuple[bool, str]:
    """Plné flow: 1) klik Verejný prístup ak treba 2) navig DAE view 3) zachyt LoadData."""
    page = context.new_page()
    captured_headers = {}
    captured_url = None
    all_posts = []

    def on_request(request):
        nonlocal captured_url
        if request.method == "POST":
            all_posts.append(request.url)
            if LOAD_DATA_PATTERN.lower() in request.url.lower():
                captured_url = request.url
                for k, v in request.headers.items():
                    captured_headers[k.lower()] = v

    page.on("request", on_request)

    def log(msg):
        if verbose:
            print(f"[seps-refresh] {msg}", flush=True)

    # Krok 1: GET landing (default.aspx) — možno máme login form, možno už dashboard
    try:
        log("krok 1: GET default.aspx")
        page.goto(LOGIN_URL, timeout=30_000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
    except Exception as e:
        log(f"landing timeout: {str(e)[:120]}")

    # Krok 2: ak vidíme login form / link "Verejný prístup", klikni ho
    try:
        # Selektor: link s textom "Verejný prístup" (alebo bez diakritiky)
        verejny_link = page.locator("a:has-text('Verejný prístup'), a:has-text('Verejny pristup')")
        if verejny_link.count() > 0:
            log(f"krok 2: našiel som link 'Verejný prístup' — klikám")
            verejny_link.first.click(timeout=10_000)
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
            page.wait_for_timeout(3000)
            log(f"krok 2: po kliknutí na 'Verejný prístup', URL: {page.url}")
        else:
            log(f"krok 2: link 'Verejný prístup' nenájdený (možno sme už v public session). URL: {page.url}")
    except Exception as e:
        log(f"krok 2 chyba: {str(e)[:120]} (skúsim pokračovať)")

    # Krok 3: navigácia na System State Map view → tu sa spustí LoadData
    try:
        log(f"krok 3: GET DAE view (System State Map)")
        page.goto(DAE_URL, timeout=30_000, wait_until="load")
    except Exception as e:
        log(f"krok 3 goto timeout: {str(e)[:120]}")

    # Krok 4: čakaj na LoadData
    log(f"krok 4: čakám {WAIT_AFTER_LOAD_MS}ms na LoadData POST")
    deadline = time.time() + WAIT_AFTER_LOAD_MS / 1000.0
    while time.time() < deadline:
        if captured_headers:
            log("✓ LoadData zachytený")
            break
        page.wait_for_timeout(500)

    page.wait_for_timeout(2000)

    log(f"# POST requestov celkom: {len(all_posts)}")
    if verbose and len(all_posts) <= 12:
        for u in all_posts:
            log(f"  POST: {u[:140]}")

    cookies_list = context.cookies()
    log(f"# cookies v contexte: {len(cookies_list)}")

    xsrf = ""
    if captured_headers:
        xsrf = captured_headers.get("x-xsrf-token") or captured_headers.get("xsrf-token") or ""

    # Fallback — XSRF v cookies
    if not xsrf:
        for c in cookies_list:
            if "XSRF" in c.get("name", "").upper():
                xsrf = c["value"]
                break

    if not xsrf:
        # Vypíš všetky cookies + POSTy do error msg pre debugging
        cookie_names = [c.get("name") for c in cookies_list]
        return False, (f"XSRF token sa nezískal. cookies={cookie_names}, "
                       f"# POSTov={len(all_posts)}, last URL: {page.url}")

    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies_list
                            if c.get("domain", "").endswith("sepsas.sk"))

    now_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    expires_at = now_utc + dt.timedelta(seconds=COOKIE_LIFETIME_SEC)
    payload = {
        "cookies":      cookie_str,
        "xsrf_token":   xsrf,
        "refreshed_at": now_utc.isoformat() + "Z",
        "expires_at":   expires_at.isoformat() + "Z",
        "n_cookies":    len([c for c in cookies_list if c.get("domain", "").endswith("sepsas.sk")]),
    }

    os.makedirs(_cookies_dir(), exist_ok=True)
    out_path = _output_path()
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)

    # Ulož aj fresh state (server občas rotuje)
    context.storage_state(path=_state_path())

    log(f"✓ uložené {out_path}  ({len(cookie_str)} chars cookies, "
        f"{len(xsrf)} chars XSRF, expires {expires_at:%H:%M} UTC)")
    return True, out_path


def refresh_once(verbose: bool = True) -> Tuple[bool, str]:
    """Headless refresh — používa 'Verejný prístup' (žiadny login netreba).

    Workflow:
      1) GET default.aspx (login obrazovka)
      2) Klik 'Verejný prístup' → session cookies pre public mode
      3) GET TS_VIEW_SCREEN (System State Map) → LoadData sa spustí
      4) Zachytí cookies + X-XSRF-TOKEN → uloží do JSON-u

    Ak existuje seps_state.json zo `--login`, použije ho (rýchlejšie).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, ("Playwright nie je nainštalovaný. "
                       "Spusti: pip install playwright && playwright install chromium")

    headless_flag = os.environ.get("SEPS_HEADED") != "1"
    use_state = os.path.exists(_state_path())
    if verbose:
        print(f"[seps-refresh] štart — {'HEADLESS' if headless_flag else 'HEADED'}, "
              f"{'state z disk' if use_state else 'fresh session (Verejný prístup)'}",
              flush=True)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=headless_flag)
        except Exception as e:
            return False, f"Chromium launch zlyhal: {e}"

        ctx_kwargs = dict(
            user_agent=USER_AGENT,
            locale="sk-SK",
            timezone_id="Europe/Bratislava",
            viewport={"width": 1280, "height": 800},
        )
        if use_state:
            ctx_kwargs["storage_state"] = _state_path()

        context = browser.new_context(**ctx_kwargs)

        try:
            ok, msg = _capture_and_save(context, verbose=verbose)
        finally:
            browser.close()
        return ok, msg


# ─── Daemon ──────────────────────────────────────────────────────────────────

def run_daemon(interval_sec: int = DEFAULT_INTERVAL_SEC):
    print(f"[seps-refresh] daemon štart (interval {interval_sec}s)")
    failures = 0
    while True:
        ok, msg = refresh_once(verbose=True)
        if ok:
            failures = 0
        else:
            failures += 1
            print(f"[seps-refresh] zlyhanie #{failures}: {msg}")
            if failures >= 5:
                print("[seps-refresh] 5x zlyhanie — končím (scheduler nech reštartuje)")
                sys.exit(1)
        time.sleep(interval_sec)


# ─── Helpers pre seps_sk.py ──────────────────────────────────────────────────

def load_cached_cookies() -> Optional[dict]:
    """Načíta naposledy uložené cookies. Vracia None ak chýbajú / sú expirované."""
    path = _output_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    exp_str = data.get("expires_at")
    if exp_str:
        try:
            exp = dt.datetime.fromisoformat(exp_str.rstrip("Z"))
            now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            if now > exp:
                return None
        except ValueError:
            pass
    return data


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="SEPS DAE session manager — login + headless refresh.")
    ap.add_argument("--login", action="store_true",
                    help="Interactive login (headed Chromium, jednorázovo)")
    ap.add_argument("--daemon", action="store_true",
                    help="Beží nepretržite, refreshuje každých INTERVAL sekúnd")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SEC,
                    help=f"Refresh interval v sekundách (default {DEFAULT_INTERVAL_SEC})")
    ap.add_argument("--quiet", action="store_true", help="Tlmí výstup")
    args = ap.parse_args()

    if args.login:
        ok, msg = do_login()
    elif args.daemon:
        run_daemon(args.interval)
        return
    else:
        ok, msg = refresh_once(verbose=not args.quiet)

    if not ok:
        print(f"ZLYHANIE: {msg}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ {msg}")


if __name__ == "__main__":
    main()
