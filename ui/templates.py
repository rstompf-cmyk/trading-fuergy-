# -*- coding: utf-8 -*-
"""ui.templates — Jinja2 environment + render helper (Fáza 3).

Použitie:
    from ui.templates import render

    @app.get("/foo")
    def foo(request):
        return render(request, "pages/foo.html", title="Foo")

`render(request, template, **ctx)` automaticky pridá globálny kontext:
    - app_name      — APP_NAME konstanta
    - user          — request.state.user alebo None
    - active_profile  — {name, mode} dict alebo None (z profiles.get_active)
    - active_market   — 'cz' alebo 'sk'
    - nav_active    — request.url.path (pre highlight v navigácii)
    - auth_required — boolean (či je AUTH_REQUIRED=1)

Jinja2 env je read-only štandardný (žiadne custom filters zatial — pridáme keď bude potreba).
"""
from __future__ import annotations
import os
from typing import Any, Dict, Optional
from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape


# Cesta k templates priečinku (relatívna od projekt root)
_TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates"
)

env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _global_context(request: Optional[Request]) -> Dict[str, Any]:
    """Globálne premenné pre všetky templates."""
    from core.state import APP_NAME
    ctx = {
        "app_name": APP_NAME,
        "user": None,
        "active_profile": None,
        "active_market": "cz",
        "nav_active": "",
        "auth_required": (os.environ.get("AUTH_REQUIRED", "0").strip()
                              in ("1", "true", "True", "yes")),
    }
    if request is not None:
        ctx["user"] = getattr(request.state, "user", None)
        ctx["nav_active"] = request.url.path
    # Aktívny profil + market (best-effort, nezhodíme render pri chybe)
    try:
        import profiles as _pr
        name = _pr.get_active()
        if name:
            data = _pr.load_profile(name) or {}
            ctx["active_profile"] = {"name": name, "mode": data.get("mode", "simulation")}
    except Exception:
        pass
    try:
        import market as _mk
        ctx["active_market"] = str(_mk.active_market() or "cz")
    except Exception:
        pass
    return ctx


def render(request: Optional[Request], template_name: str,
            status_code: int = 200, **context: Any) -> HTMLResponse:
    """Renderuje šablónu so spojeným contextom (globálny + per-call).

    Args:
        request: FastAPI Request (pre user + path lookup), môže byť None pre statické error stránky
        template_name: cesta od templates/ (napr. 'pages/login.html')
        status_code: HTTP status (default 200)
        **context: ďalšie premenné pre šablónu

    Returns:
        HTMLResponse s renderovaným HTML
    """
    tmpl = env.get_template(template_name)
    ctx = _global_context(request)
    ctx.update(context)
    body = tmpl.render(**ctx)
    return HTMLResponse(body, status_code=status_code)


def render_legacy_body(request: Optional[Request], title: str,
                        body_html: str, head_extra: str = "",
                        scripts: str = "") -> HTMLResponse:
    """Wrapper pre legacy f-string stránky — vloží raw HTML body do base.html.

    Použitie v legacy handleroch:
        return render_legacy_body(request, "OKTE VDT", body_html)

    Body sa vloží cez |safe, takže môže obsahovať <style>, <script>, Chart.js.
    Stránka získa: navigáciu, /static/css/app.css link, Trading Fuergy v <title>,
    user chip s logoutom (auth-conditional).

    `head_extra` sa vloží do <head> bloku (napr. dodatočné CDN linky).
    `scripts` sa vloží na koniec body (napr. inicializačné JS).

    NAV-EVERYWHERE (2026-06-22): _legacy_body.html vypína base.html nav, lebo legacy body
    obvykle nesie vlastný _nav(). Ak ho NEnesie (napr. /manager, /fleet, /plan_batch,
    chybové stránky), automaticky predložíme _nav(), aby hlavné menu NIKDY nezmizlo.
    Guard `class="app-nav"` zabráni dvojitému menu pre stránky čo už _nav() volajú.
    """
    try:
        if 'class="app-nav"' not in (body_html or ""):
            from ui.html import _nav
            _active = ""
            try:
                if request is not None:
                    _active = request.url.path
            except Exception:
                _active = ""
            body_html = _nav(_active) + (body_html or "")
    except Exception:
        pass
    return render(request, "pages/_legacy_body.html",
                   legacy_title=title,
                   legacy_body=body_html,
                   legacy_head_extra=head_extra,
                   legacy_scripts=scripts)


__all__ = ["render", "render_legacy_body", "env"]
