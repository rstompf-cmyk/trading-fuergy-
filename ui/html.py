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


def _nav(active: str = "") -> str:
    """Hlavná navigácia (rovnaká na všetkých stránkach). `active` = href aktuálnej stránky.

    Všetky linky majú `target="_top"` — keď je nejaká stránka embedovaná v iframe
    (napr. /livesim v /realio?tab=riadenie), kliknutie v navigácii vyskočí do
    top window namiesto vnorenia ďalšieho iframu (zabráni nested iframe rekurzii).
    """
    items = [("/", "🗓 Plán D-1"), ("/dentrh", "⚡ Denný trh 15-min"), ("/rt", "🔴 RT poradca"),
             ("/plan_batch", "📦 Batch plán"), ("/plans", "📋 Plány"),
             ("/profiles", "⚙ Profily"),
             ("/livesim", "🟢 Živá simulácia"),
             ("/load_import", "🏠 Spotreba"),
             ("/realio", "🔌 Reálne meranie"),
             ("/auto_control", "🤖 Paper trading"),
             ("/vdt", "💹 OKTE VDT"),
             ("/kalibracia", "📈 Kalibrácia"), ("/data", "💾 Dáta")]
    links = "".join(
        f'<a href="{href}" target="_top" style="padding:8px 12px;border-radius:8px;text-decoration:none;font-size:14px;'
        f'{"background:#1F4E78;color:#fff;font-weight:600" if href==active else "color:#1F4E78"}">{lab}</a>'
        for href, lab in items)
    return (f'<nav style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 18px;'
            f'padding:8px;background:#eef3f9;border-radius:10px">{links}'
            f'<span style="margin-left:auto;display:inline-flex;gap:6px;align-items:center">'
            f'{_active_profile_badge()}{_market_badge()}</span></nav>')
