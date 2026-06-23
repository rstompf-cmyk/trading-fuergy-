# -*- coding: utf-8 -*-
"""
ui.html — pure HTML helpery (nav, formové polia, badges, farby).

Extrahované z app.py (Fáza 1 refactoringu). Žiadne side-effecty.
`mk` (market) a `ps` (plan_store) sa importujú lazy aby modul fungoval aj keď tieto
nie sú nainštalované.
"""
from __future__ import annotations


# Farby pre reco labelу (RT poradca, livesim Odporúčanie teraz).
_RECO_COL = {"VYBI": "#2E7D32", "NABI": "#1F4E78", "DRŽ": "#888",
             "DRŽ (limit)": "#C49000", "DRŽ (plno)": "#C49000", "—": "#bbb",
             "čaká sa": "#bbb"}


def _field(label: str, name: str, val, step: str = "any") -> str:
    """Štandardný horizontálny <label> + <input> pre formuláre /plan a /dentrh."""
    return (f'<label style="display:flex;justify-content:space-between;gap:8px;margin:4px 0">'
            f'<span>{label}</span>'
            f'<input name="{name}" value="{val}" type="number" step="{step}" '
            f'style="width:120px;padding:4px;border:1px solid #ccc;border-radius:6px"></label>')


def _profile_mode_chip(mode: str = "simulation", small: bool = True) -> str:
    """Vráti farebný chip podľa typu profilu.
    mode='simulation' → 🎮 zelený, mode='real' → 🔴 červený.
    """
    mode = str(mode or "simulation").lower()
    if mode == "real":
        bg = "#C62828"; icon = "🔴"; lbl = "Real"
    else:
        bg = "#2E7D32"; icon = "🎮"; lbl = "Sim"
    pad = "1px 6px" if small else "3px 10px"
    fs = "11px" if small else "13px"
    return (f'<span style="background:{bg};color:#fff;padding:{pad};border-radius:4px;'
            f'font-size:{fs};font-weight:600" title="Typ profilu: {mode}">{icon} {lbl}</span>')


def _profile_mode_bg(mode: str) -> str:
    """Pozaďová farba pre badge active profile podľa mode (jemnejšia variácia chip-u)."""
    mode = str(mode or "simulation").lower()
    if mode == "real":
        return "#C62828"  # červená — varovanie že ide o reálny chod
    if mode == "simulation":
        return "#2E7D32"  # zelená — bezpečná simulácia
    return "#5E35B1"      # neutrál


def _active_profile_badge() -> str:
    """Vráti malý badge s aktívnym profilom pre nav (alebo prázdny ak modul neexistuje).

    Farba badge odzrkadľuje **typ profilu**: zelená pre Simulácia, červená pre
    Reálny chod. Default profile (žiadny) ostáva sivý.
    """
    try:
        import plan_store as _ps
        prof = _ps.resolve_profile()
        if prof == "default":
            color = "#666"
            lbl = "default"
            icon = "🏷"
        else:
            # Zisti mode profilu pre farbu
            try:
                import profiles as _pr
                m = _pr.get_mode(prof) if hasattr(_pr, "get_mode") else "simulation"
            except Exception:
                m = "simulation"
            color = _profile_mode_bg(m)
            lbl = prof
            icon = "🔴" if m == "real" else "🎮"
        return (f'<a href="/profiles" target="_top" title="Aktívny profil — klikni pre prepnutie/správu" '
                f'style="padding:6px 12px;border-radius:8px;text-decoration:none;'
                f'font-size:13px;background:{color};color:#fff;font-weight:600">{icon} {lbl}</a>')
    except Exception:
        return ""


def _market_badge() -> str:
    """Dropdown/menu na prepnutie trhu (Česko / Slovensko)."""
    try:
        import market as _mk
    except ImportError:
        return ""
    try:
        cur = _mk.get_active_market()
        items = _mk.list_markets()
        opts = "".join(
            f'<option value="{m["code"]}"{" selected" if m["code"]==cur else ""}>{m["flag"]} {m["label"]}</option>'
            for m in items)
        cur_lbl = _mk.label_for(cur)
        return (
            f'<form method="post" action="/market/set" style="display:inline-flex;align-items:center;gap:4px;margin-left:auto" '
            f'title="Aktívny trh — všetky dáta (plány, profily, livesim) sa čítajú z out/{cur}/. Prepnutie ovplyvní VŠETKO.">'
            f'<span style="background:#1F4E78;color:#fff;padding:6px 10px;border-radius:8px;font-size:13px;font-weight:600">{cur_lbl}</span>'
            f'<select name="market" onchange="this.form.submit()" '
            f'style="padding:4px 8px;border:1px solid #ccc;border-radius:6px;font-size:13px;background:#fff">'
            f'{opts}</select></form>')
    except Exception:
        return ""


def _profile_tabs(current_active: str = "") -> str:
    """Bug R1: Profile chip tabs — každý profil ako vlastná karta.

    Vizuál:
        🟢🔵 VW_simulacia   (zelená karta, modrý indicator = bg ON, výrazne ak active)
        🔴🔵 Trakany_real   (červená karta — real mode)
        🟢⚪ Bat_D-1_bat_2   (sivý indicator = bg OFF — wake-on-click)

    Klik na neaktívny chip → POST /profiles/apply (set_active + redirect na pôvodu stránku).
    Klik na aktívny chip → bez akcie (alebo /dashboard ?profile=X).
    """
    try:
        import profiles as _pr
        all_profs = _pr.list_profiles()
    except Exception:
        all_profs = []
    # bg-enabled set
    bg_enabled = set()
    try:
        import auto_control as _ac
        bg_enabled = _ac.get_enabled_profiles()
    except Exception:
        pass
    # Order: real first, sim second, alphabetic in each group
    def _mode(name):
        try:
            return _pr.get_mode(name) if all_profs else "unknown"
        except Exception:
            return "unknown"
    profs_sorted = sorted(all_profs, key=lambda n: (0 if _mode(n) == "real" else 1, n.lower()))
    if not profs_sorted:
        return ""
    chips = []
    for name in profs_sorted:
        m = _mode(name)
        is_active = (name == current_active)
        bg_on = (name in bg_enabled)
        # Color: real = červená, sim = zelená; saturácia: active=full, inactive=light
        if m == "real":
            bg_col = "#C62828" if is_active else "#fff"
            fg_col = "#fff" if is_active else "#C62828"
            border = "#C62828"
            mode_icon = "🔴"
        else:
            bg_col = "#2E7D32" if is_active else "#fff"
            fg_col = "#fff" if is_active else "#2E7D32"
            border = "#2E7D32"
            mode_icon = "🟢"
        bg_icon = "🔵" if bg_on else "⚪"
        # Active = priamy link na dashboard tohto profilu (R2 pridáva /dashboard?profile=X)
        # Inactive = POST /profiles/apply form so set_active
        if is_active:
            chips.append(
                f'<a href="/dashboard?profile={name}" target="_top" '
                f'style="display:inline-flex;align-items:center;gap:4px;'
                f'padding:7px 12px;border:2px solid {border};border-radius:9px;'
                f'background:{bg_col};color:{fg_col};font-weight:700;font-size:13px;'
                f'text-decoration:none;box-shadow:0 2px 4px rgba(0,0,0,0.1)" '
                f'title="Aktívny profil — klikni pre dashboard {name}">'
                f'{mode_icon}{bg_icon} {name}</a>'
            )
        else:
            chips.append(
                f'<form method="post" action="/profiles/apply" target="_top" '
                f'style="display:inline-block;margin:0">'
                f'<input type="hidden" name="name" value="{name}">'
                f'<input type="hidden" name="redirect_to" value="/dashboard?profile={name}">'
                f'<button type="submit" '
                f'style="display:inline-flex;align-items:center;gap:4px;'
                f'padding:6px 11px;border:1px solid {border};border-radius:9px;'
                f'background:{bg_col};color:{fg_col};font-weight:600;font-size:13px;'
                f'cursor:pointer;font-family:inherit" '
                f'title="Klikni pre prepnutie na profil {name}">'
                f'{mode_icon}{bg_icon} {name}</button></form>'
            )
    return (f'<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;'
              f'margin:8px 0;padding:8px;background:#f0f4f8;border-radius:10px;'
              f'border-left:3px solid #1F4E78">'
              f'<span style="font-size:12px;color:#666;font-weight:600;margin-right:4px">PROFIL:</span>'
              f'{"".join(chips)}'
              f'</div>')


def _nav(active: str = "") -> str:
    """Hlavná navigácia — zoskupené rozbaľovacie menu (5 skupín), mode-aware.

    Menu sa filtruje podľa módu aktívneho profilu (both/simulation/real); pod menu
    ostáva riadok prepínača profilov (_profile_tabs) + market/user chip vpravo.
    Linky target="_top". Redizajn 2026-06-22 — zhodné s templates/components/nav.html.
    """
    try:
        import profiles as _pr
        from core.profile_resolver import get_active as _ga
        cur_prof = _ga()
        cur_mode = _pr.get_mode(cur_prof) if cur_prof else "unknown"
    except Exception:
        cur_prof = ""
        cur_mode = "unknown"
    nav_groups = [
        ("🗓 Plánovanie", [
            ("/", "Plán D-1", "both"),
            ("/dentrh", "Denný trh 15-min", "both"),
            ("/plan_batch", "Batch plán", "both"),
            ("/plans", "Uložené plány", "both"),
        ]),
        ("🟢 Simulácia & monitoring", [
            ("/livesim", "Živá simulácia", "both"),
            ("/manager", "Manager dashboard", "both"),
            ("/fleet", "Flotila", "both"),
            ("/dashboard", "Profil dashboard", "both"),
            ("/simulacia", "Výsledok simulácie", "simulation"),
        ]),
        ("💹 Obchodovanie", [
            ("/rt", "RT poradca", "both"),
            ("/vdt", "OKTE VDT prehľad", "both"),
            ("/vdt/live_advisor", "VDT Live advisor", "both"),
            ("/vdt/d1", "VDT D-1", "both"),
            ("/vdt/board", "VDT Board", "both"),
            ("/vdt/simulator", "VDT Simulátor", "simulation"),
            ("/vdt/backtest", "VDT Backtest", "simulation"),
            ("/vdt/zco_backtest", "ZCO Backtest", "simulation"),
            ("/auto_control", "Paper trading", "both"),
        ]),
        ("🔌 Reálne riadenie", [
            ("/realio", "Reálne meranie", "real"),
            ("/customers", "Zákazníci", "real"),
            ("/cdc", "CDC konfigurácia", "real"),
            ("/customers/battery/regulation", "Okno regulácie", "real"),
        ]),
        ("💾 Dáta & nastavenia", [
            ("/profiles", "Profily", "both"),
            ("/load_import", "Spotreba", "both"),
            ("/ftv_scenario", "FTV scenár", "both"),
            ("/kalibracia", "Kalibrácia", "both"),
            ("/data", "Dáta", "both"),
        ]),
    ]

    def _vis(m):
        # 'both' vždy; inak len ak sedí mód; pri neznámom móde ukáž všetko (nefiltruj)
        return m == "both" or m == cur_mode or cur_mode not in ("simulation", "real")

    _groups_html = []
    for _glabel, _items in nav_groups:
        _vises = [(h, l) for (h, l, m) in _items if _vis(m)]
        if not _vises:
            continue
        _has_active = any(h == active for h, l in _vises)
        _links = ""
        for h, l in _vises:
            _acls = ' class="active"' if h == active else ''
            _links += f'<a href="{h}" target="_top"{_acls}>{l}</a>'
        _gcls = ' has-active' if _has_active else ''
        _groups_html.append(
            f'<div class="nav-group{_gcls}">'
            f'<button type="button" class="nav-trig" onclick="navTog(this,event)">{_glabel} '
            f'<span style="font-size:11px" aria-hidden="true">▾</span></button>'
            f'<div class="nav-menu"><div class="nav-sec">{_glabel}</div>{_links}</div></div>')
    # User chip + Odhlásiť — JS naplní z /me. Bez auth ostane skrytý (display:none).
    user_chip = (
        '<span id="navUserChip" style="display:none;align-items:center;gap:8px;'
        'padding:4px 4px 4px 12px;background:#fff;border:1px solid #d8e0eb;border-radius:18px;'
        'font-size:13px;color:#333">'
        '<span id="navUserLabel">…</span>'
        '<span id="navUserRole" style="font-size:10px;font-weight:700;padding:2px 7px;'
        'border-radius:10px;background:#eef3f9;color:#1F4E78">role</span>'
        '<a href="/logout" target="_top" title="Odhlásiť" '
        'style="padding:4px 10px;border-radius:14px;background:#C62828;'
        'color:#fff;text-decoration:none;font-size:11px;font-weight:600">↪ Odhlásiť</a>'
        '</span>'
        '<script>(function(){'
          'try{'
            'fetch("/me",{credentials:"same-origin"}).then(function(r){return r.json();})'
            '.then(function(d){'
              'if(!d||!d.authenticated)return;'
              'var u=d.user||{};'
              'var c=document.getElementById("navUserChip");'
              'if(!c)return;'
              'document.getElementById("navUserLabel").textContent=u.username||"?";'
              'var rEl=document.getElementById("navUserRole");'
              'var role=(u.role||"").toLowerCase();'
              'rEl.textContent=role||"?";'
              'var palette={'
                  '"admin":"#fbeaea;color:#7a1810",'
                  '"trading":"#fff3cd;color:#7a5d00",'
                  '"obchodnik":"#e6f4ea;color:#1B5E20",'
                  '"zakaznik":"#e8eaf6;color:#283593"};'
              'var col=palette[role];'
              'if(col)rEl.style.cssText="font-size:10px;font-weight:700;padding:2px 7px;'
              'border-radius:10px;background:"+col;'
              'c.style.display="inline-flex";'
            '}).catch(function(){});'
          '}catch(e){}'
        '})();</script>'
    )
    # Inline CSS menu — legacy stránky (form_page, /dentrh, /livesim...) majú vlastný <head>
    # bez app.css + generické button{} pravidlo. Scoped pod .app-nav (literál farby, vyššia
    # špecificita prebije button{}). Bez tohto sa dropdowny zobrazia rozbalené a neštýlované.
    _navcss = ('<style>'
        '.app-nav{display:flex;flex-wrap:wrap;gap:4px;align-items:center;margin:0 0 12px;'
        'padding:8px;background:#eef3f9;border-radius:10px}'
        '.app-nav .nav-group{position:relative}'
        '.app-nav .nav-trig{border:0;background:transparent;color:#1F4E78;font:500 14px inherit;'
        'padding:8px 12px;border-radius:8px;cursor:pointer;display:inline-flex;align-items:center;gap:6px}'
        '.app-nav .nav-trig:hover{background:#e2ebf6}'
        '.app-nav .nav-group.open>.nav-trig,.app-nav .nav-group.has-active>.nav-trig'
        '{background:#1F4E78;color:#fff}'
        '.app-nav .nav-menu{position:absolute;top:calc(100% + 6px);left:0;z-index:50;background:#fff;'
        'border:1px solid #e3e8ef;border-radius:10px;box-shadow:0 4px 20px rgba(0,0,0,.12);'
        'padding:6px;min-width:230px;display:none}'
        '.app-nav .nav-group.open>.nav-menu{display:block}'
        '.app-nav .nav-menu .nav-sec{font:600 11px inherit;color:#888;text-transform:uppercase;'
        'letter-spacing:.04em;padding:4px 10px 6px}'
        '.app-nav .nav-menu a{display:block;padding:7px 10px;border-radius:7px;color:#222;'
        'font-size:14px;text-decoration:none}'
        '.app-nav .nav-menu a:hover{background:#eef3f9}'
        '.app-nav .nav-menu a.active{background:#1F4E78;color:#fff;font-weight:600}'
        '.app-nav-right{margin-left:auto;display:inline-flex;gap:6px;align-items:center}'
        '@media (max-width:640px){.app-nav .nav-menu{left:0;right:0;min-width:0}}'
        '</style>')
    _navjs = ('<script>function navTog(b,e){if(e)e.stopPropagation();'
              'var g=b.parentNode,w=g.classList.contains("open"),a=document.querySelectorAll(".nav-group");'
              'for(var i=0;i<a.length;i++)a[i].classList.remove("open");'
              'if(!w)g.classList.add("open");}'
              'document.addEventListener("click",function(){'
              'var a=document.querySelectorAll(".nav-group.open");'
              'for(var i=0;i<a.length;i++)a[i].classList.remove("open");});</script>')
    menu_row = ('<nav class="app-nav">' + "".join(_groups_html) +
                '<span class="app-nav-right">' + _market_badge() + user_chip + '</span></nav>')
    # Pod menu: prepínač profilov (chip tabs) — zachované z pôvodného layoutu
    return _navcss + menu_row + _profile_tabs(cur_prof) + _navjs
