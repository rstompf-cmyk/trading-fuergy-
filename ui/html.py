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


_CLEANUP_UI_TMPL = r"""
  <div id="cleanup-alert-bar" style="display:none;position:sticky;top:0;z-index:9999;
       background:#7a1f1f;color:#fff;padding:8px 14px;font-size:14px;box-shadow:0 2px 6px rgba(0,0,0,.3)"></div>
  <div id="manual-trade-modal" style="display:none;position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.5)">
    <div style="max-width:420px;margin:8% auto;background:#fff;color:#111;border-radius:10px;padding:18px 20px;box-shadow:0 8px 30px rgba(0,0,0,.4);font-size:14px">
      <div style="font-weight:700;font-size:16px;margin-bottom:10px">&#9998; Ru&#269;n&yacute; VDT obchod</div>
      <div id="mt-info" style="background:#f3f4f6;border-radius:6px;padding:8px 10px;margin-bottom:12px;font-size:13px;color:#374151"></div>
      <input type="hidden" id="mt-profile">
      <label style="display:block;margin:6px 0 2px">Profil</label>
      <input id="mt-profile-show" readonly style="width:100%;padding:6px;border:1px solid #d1d5db;border-radius:5px;background:#f9fafb">
      <div style="display:flex;gap:10px">
        <div style="flex:1"><label style="display:block;margin:6px 0 2px">&#268;as (HH:MM)</label>
          <input id="mt-slot" placeholder="16:45" style="width:100%;padding:6px;border:1px solid #d1d5db;border-radius:5px"></div>
        <div style="flex:1"><label style="display:block;margin:6px 0 2px">Akcia</label>
          <select id="mt-action" style="width:100%;padding:6px;border:1px solid #d1d5db;border-radius:5px">
            <option value="discharge">Vyb&iacute;ja&#357; / predaj</option>
            <option value="charge">Nab&iacute;ja&#357; / n&aacute;kup</option>
          </select></div>
      </div>
      <div style="display:flex;gap:10px">
        <div style="flex:1"><label style="display:block;margin:6px 0 2px">Objem (kW)</label>
          <input id="mt-kw" type="number" step="1" style="width:100%;padding:6px;border:1px solid #d1d5db;border-radius:5px"></div>
        <div style="flex:1"><label style="display:block;margin:6px 0 2px">Cena (&euro;/MWh)</label>
          <input id="mt-price" type="number" step="0.01" style="width:100%;padding:6px;border:1px solid #d1d5db;border-radius:5px"></div>
      </div>
      <div style="margin-top:16px;display:flex;justify-content:flex-end;gap:8px">
        <button onclick="__closeManualTrade()" style="padding:7px 14px;border:1px solid #d1d5db;background:#fff;border-radius:6px;cursor:pointer">Zru&#353;i&#357;</button>
        <button id="mt-submit" style="padding:7px 14px;border:0;background:#166534;color:#fff;border-radius:6px;font-weight:600;cursor:pointer">Zobchodova&#357;</button>
      </div>
    </div>
  </div>
  <button id="manual-trade-fab" style="display:none;position:fixed;right:18px;bottom:18px;z-index:9998;background:#166534;color:#fff;border:0;border-radius:24px;padding:10px 16px;font-size:14px;font-weight:600;box-shadow:0 3px 10px rgba(0,0,0,.3);cursor:pointer">&#9998; Ru&#269;n&yacute; VDT obchod</button>
  <button id="cleanup-sim-fab" style="display:none;position:fixed;right:18px;bottom:64px;z-index:9998;background:#92400e;color:#fff;border:0;border-radius:24px;padding:10px 16px;font-size:14px;font-weight:600;box-shadow:0 3px 10px rgba(0,0,0,.3);cursor:pointer">&#129529; Simuluj upratovanie (de&#328;)</button>
  <button id="cleanup-diag-fab" style="display:none;position:fixed;right:18px;bottom:110px;z-index:9998;background:#1F4E78;color:#fff;border:0;border-radius:24px;padding:10px 16px;font-size:14px;font-weight:600;box-shadow:0 3px 10px rgba(0,0,0,.3);cursor:pointer">&#128270; Diagnostika upratovania</button>
  <script>
  (function(){
    var _activeProfile = "__ACTIVE_PROFILE__";
    var p = window.location.pathname || "";
    var onLive = (p.indexOf("/livesim") === 0 || p.indexOf("/realio") === 0);
    if (onLive){
      var fab = document.getElementById("manual-trade-fab");
      if (fab){ fab.style.display="block"; fab.addEventListener("click", function(){
        window.__openManualTrade({profile:_activeProfile, action:"discharge",
          info:"Ručný VDT obchod pre profil "+_activeProfile+". Zadaj čas 15-min slotu, akciu, objem a cenu."}); }); }
      var cfab = document.getElementById("cleanup-sim-fab");
      if (cfab){ cfab.style.display="block"; cfab.addEventListener("click", async function(){
        var day = new URLSearchParams(location.search).get("day") || new Date().toISOString().slice(0,10);
        if(!confirm("Simulovať upratovanie (Option B, OKTE VDT ceny) pre "+_activeProfile+" deň "+day+"?")) return;
        this.disabled=true; this.textContent="Upratávam…";
        try{ var r=await fetch("/vdt/cleanup_simulate",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:"profile="+encodeURIComponent(_activeProfile)+"&day="+encodeURIComponent(day)});
          var j=await r.json(); alert(j.ok ? (j.reason||"hotovo") : ("Chyba: "+(j.reason||"neznáma"))); if(j.ok && j.count>0) location.reload();
        }catch(e){ alert("Chyba: "+e); } this.disabled=false; this.textContent="🧹 Simuluj upratovanie (deň)"; }); }
      var dfab = document.getElementById("cleanup-diag-fab");
      if (dfab){ dfab.style.display="block"; dfab.addEventListener("click", function(){
        var day = new URLSearchParams(location.search).get("day") || new Date().toISOString().slice(0,10);
        window.open("/vdt/cleanup_diagnostics?profile="+encodeURIComponent(_activeProfile)+"&day="+encodeURIComponent(day),"_blank");
      }); }
    }
    function openManualTrade(pf){ pf=pf||{};
      document.getElementById("mt-profile").value=pf.profile||"";
      document.getElementById("mt-profile-show").value=pf.profile||"";
      document.getElementById("mt-slot").value=(pf.slot||"").slice(0,5);
      document.getElementById("mt-action").value=pf.action||"discharge";
      document.getElementById("mt-kw").value=(pf.kw!=null?Math.round(pf.kw):"");
      document.getElementById("mt-price").value=(pf.price!=null?pf.price:"");
      document.getElementById("mt-info").textContent=pf.info||"Zadaj objem, cenu a čas 15-min slotu.";
      document.getElementById("manual-trade-modal").style.display="block"; }
    function closeManualTrade(){ document.getElementById("manual-trade-modal").style.display="none"; }
    window.__openManualTrade=openManualTrade; window.__closeManualTrade=closeManualTrade;
    var _sb=document.getElementById("mt-submit");
    if(_sb) _sb.addEventListener("click", async function(){
      var b=this; b.disabled=true; b.textContent="Zapisujem…";
      var body="profile="+encodeURIComponent(document.getElementById("mt-profile").value)
        +"&slot="+encodeURIComponent(document.getElementById("mt-slot").value)
        +"&action="+encodeURIComponent(document.getElementById("mt-action").value)
        +"&kw="+encodeURIComponent(document.getElementById("mt-kw").value)
        +"&price_eur_mwh="+encodeURIComponent(document.getElementById("mt-price").value);
      try{ var r=await fetch("/vdt/manual_trade",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:body});
        var j=await r.json(); alert(j.ok ? (j.reason+(j.warn?("\n\n"+j.warn):"")) : ("Chyba: "+(j.reason||"neznáma"))); if(j.ok) closeManualTrade();
      }catch(e){ alert("Chyba: "+e); } b.disabled=false; b.textContent="Zobchodovať"; });
    async function forceCleanup(profile, btn){
      if(!confirm("Upratať profil "+profile+" za najlepšiu dostupnú cenu (aj so stratou)?")) return;
      btn.disabled=true; btn.textContent="Upratávam…";
      try{ const r=await fetch("/cleanup_force",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:"profile="+encodeURIComponent(profile)});
        const j=await r.json(); alert(j.acted?("Upratané: "+(j.reason||"")):("Neupratané: "+(j.reason||"chyba")));
      }catch(e){ alert("Chyba: "+e); } poll(); }
    window.__forceCleanup=forceCleanup;
    async function poll(){ const bar=document.getElementById("cleanup-alert-bar"); if(!bar) return;
      try{ const r=await fetch("/cleanup_alerts",{cache:"no-store"}); const j=await r.json(); const al=(j&&j.alerts)||[];
        if(!al.length){ bar.style.display="none"; bar.innerHTML=""; return; }
        bar.innerHTML=al.map(function(a){
          const dir=a.direction==="sell"?"vybíjať (SOC preplné)":"nabíjať (SOC pod min)";
          const px=(a.best_price_eur_mwh!=null)?a.best_price_eur_mwh+" €/MWh":"cena n/a";
          const loss=(a.would_loss_eur!=null)?(" · strata ~"+a.would_loss_eur+" €"):"";
          return '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:3px 0">'
            +'<span>⚠ <b>'+a.profile+'</b>: o '+(a.tau_h!=null?a.tau_h:"?")+' h nedodateľný slot <b>'+(a.problem_slot||"")+'</b> — treba '+dir
            +', '+(a.kw!=null?a.kw:"?")+' kW. Najlepšia cena <b>'+px+'</b>'+loss+'.</span>'
            +'<button onclick="__forceCleanup(\''+a.profile+'\',this)" style="background:#fff;color:#7a1f1f;border:0;border-radius:5px;padding:4px 10px;font-weight:600;cursor:pointer">Upratať za túto cenu</button>'
            +'<button onclick=\'__openManualTrade('+JSON.stringify({profile:a.profile,slot:a.problem_slot,action:a.direction,kw:a.kw,price:a.best_price_eur_mwh})+')\' style="background:#fde68a;color:#7a1f1f;border:0;border-radius:5px;padding:4px 10px;font-weight:600;cursor:pointer">✎ Zadať ručne</button>'
            +'</div>'; }).join("");
        bar.style.display="block";
      }catch(e){} }
    poll(); setInterval(poll, 30000);
  })();
  </script>
"""


def render_cleanup_ui(active_profile: str = "") -> str:
    """Zdieľané VDT UI (alert banner + ručný obchod modal + FAB tlačidlá + JS).
    Vkladá sa do base.html (cez context) AJ do legacy raw stránok (/livesim, /realio),
    lebo tie nejdú cez base.html. active_profile = meno aktívneho profilu pre predvyplnenie."""
    _p = str(active_profile or "").replace("\\", "").replace('"', "")
    return _CLEANUP_UI_TMPL.replace("__ACTIVE_PROFILE__", _p)


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
    # REDIZAJN 2026-07: inline (legacy) stránky majú vlastný <head> bez app.css. Vložíme <link>
    # naň priamo do outputu _nav() (link v <body> je platný) → dizajn systém (tokeny, sticky
    # header, karty, chip, tlačidlá, tabuľky) sa aplikuje aj na inline stránky. app.css definuje
    # .app-header/.app-nav/.nav-group/... takže starý inline _navcss už netreba.
    _assets = '<link rel="stylesheet" href="/static/css/app.css?v=20260721redesign">'
    _navjs = ('<script>function navTog(b,e){if(e)e.stopPropagation();'
              'var g=b.parentNode,w=g.classList.contains("open"),a=document.querySelectorAll(".nav-group");'
              'for(var i=0;i<a.length;i++)a[i].classList.remove("open");'
              'if(!w)g.classList.add("open");}'
              'document.addEventListener("click",function(){'
              'var a=document.querySelectorAll(".nav-group.open");'
              'for(var i=0;i<a.length;i++)a[i].classList.remove("open");});</script>')
    _brand = ('<span class="app-brand"><span class="logo">⚡</span>FUERGY '
              '<small>· FTV · Batéria Trading</small></span>')
    _prof_admin = ('<a href="/profiles" target="_top" title="Spravovať a editovať profily" '
                   'class="btn sm" style="white-space:nowrap">⚙ Profily</a>')
    _topbar = ('<nav class="app-nav">' + _brand + "".join(_groups_html)
               + '<span class="app-nav-right">' + user_chip + '</span></nav>')
    _context = ('<div class="app-context">' + _prof_admin + _profile_tabs(cur_prof)
                + '<span class="app-nav-right" style="margin-left:auto">' + _market_badge()
                + '</span></div>')
    _warn = ('<div class="real-warning"><span class="dot"></span> REÁLNY MÓD — '
             'povely idú na fyzické zariadenie (Bender)</div>') if cur_mode == "real" else ''
    header = '<header class="app-header">' + _topbar + _context + _warn + '</header>'
    return _assets + header + _navjs
