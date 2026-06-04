# Audit UI — FTV+batéria FastAPI appka

> Fáza 0 migrácie. Stav: 2026-06-04.
>
> Cieľ: zmapovať všetkých 64 endpointov ako základ pre refactor inline HTML strings
> na Jinja2 templates + `base.html` v Fáze 3.

## Súhrn

- **app.py:** 13 806 riadkov, 64 endpointov (34 GET, 29 POST, 1 JSON bez response_class)
- **Štýl:** všetky stránky inline f-string HTML s `<style>` blokom; spoločný `_nav()` helper
- **12 funkčných skupín** — od D-1 plánov po realio HW write

---

## 1. Plán D-1 + Denný trh 15-min (6 endpointov)

| Route | Metóda | Účel | Form / Query | Vracia | Write |
|-------|--------|------|--------------|--------|-------|
| `/` | GET | Landing → `home()` → `form_page()` | — | HTMLResponse | čít |
| `/plan` | POST | **Generovať D-1 60-min plán** + uložiť do plan_store + autosave profilu | 50+ params: date, lat/lon, kwp/tilt/azimuth/eff, batt_*, soc_*, grid_kw_im/ex, grid_fee, cycle_cost, min_spread, baseline_*, joint_lp_* (6 flags), mult_arr (24×), rt_arr (24×), save_only | HTMLResponse (tabuľka 24h + cards + Joint LP card + Excel download) | ÁNO — plan_store.save_plan, ui_settings, profile autosave |
| `/dentrh` | GET | Formulár pre denný trh 15-min | reload, multipliers | HTMLResponse | čít |
| `/dentrh` | POST | **Generovať 15-min plán (96 slotov)** — OTE ceny | ~25 params: shared geometrie + mult_arr (96×), rt_arr (96×) | HTMLResponse (tabuľka 96 slotov + súhrny) | ÁNO — plan_store.save_plan(kind="dentrh", step_min=15) |
| `/plan_batch` | GET | Stránka hromadného generovania (od-do) | from_date, to_date, step_min, kind | HTMLResponse | čít |
| `/plan_batch` | POST | Vygenerovať N plánov v cykle + streaming progress | from_date, to_date, step_min, kind | HTMLResponse (progress + výsledky) | ÁNO — plan_store.save_plan × N |

**Kritické moduly:** `joint_lp_integration.optimize_day_or_joint`, `plan_store`, `_fetch_pv_cached`, `_isot_history`, `_model`, `load_profile`, `_cal_for` (mesačná kalibrácia)
**Per-profile:** ÁNO — `resolve_profile()` na začiatku
**Sensitivity:** Čistá simulácia (žiadny external write)

---

## 2. Správa profilov (5 endpointov)

| Route | Metóda | Účel | Form / Query | Vracia | Write |
|-------|--------|------|--------------|--------|-------|
| `/profiles` | GET | Zoznam profilov + actions (apply/edit/snapshot/delete) | — | HTML tabuľka (name, mode chip, ×/RT count, actions) | čít |
| `/profiles/edit` | GET | Editor — všetky ~50 polí (plan + dentrh + RT + distribution) | ?name=<profile> | HTML form | čít |
| `/profiles/save` | POST | Uložiť profil (full overwrite) | 50+ + mult96 + rt_on96 + distribution_json | HTML OK/error | ÁNO — `profiles.save_profile` |
| `/profiles/apply` | POST | Aktivovať profil (`set_active`) | profile_name | Redirect `/` | ÁNO — `profiles.set_active` |
| `/profiles/delete` | POST | Vymazať profil + mult/rt templates | profile_name | HTML OK | ÁNO — `profiles.delete_profile`, glob rm |

**Bonus:**
- `/profiles/snapshot` POST — snapshot aktuálneho UI stavu ako nový profil
- `/market/set` POST — prepnúť market tag (cz/sk)

---

## 3. FTV scenario testing (2 endpointy)

| Route | Metóda | Účel | Form | Vracia | Write |
|-------|--------|------|------|--------|-------|
| `/ftv_scenario` | GET | UI na test FTV logiky (volatility, lookahead, throttle) | — | HTML form | čít |
| `/ftv_scenario` | POST | Run scenario — generovať plán s modifikovanými FTV params | volatility_pct, lookahead_h, min_spread, persist_throttle, ftv_balance, disable_balance, date, hourly_kw[24] | HTML comparison (baseline vs scenario) | ÁNO — `ftv_scenarios.save_scenario` |

---

## 4. Prehliadač uložených plánov (3 endpointy)

| Route | Metóda | Účel | Query | Vracia | Write |
|-------|--------|------|-------|--------|-------|
| `/plans` | GET | Zoznam všetkých uložených plánov (filter podľa profilu) | profile, market, kind | HTML tabuľka s profitmi | čít |
| `/plan_view` | GET | Detail plánu (tabuľka 24/96h + parametre + Excel link) | date, step, kind | HTML tabuľka + params | čít |
| `/plans/delete` | POST | Vymazať plán z plan_store | date, step, kind | HTML OK | ÁNO — `plan_store.delete_plan` |

---

## 5. Živá simulácia (3 endpointy)

| Route | Metóda | Účel | Query | Vracia | Write |
|-------|--------|------|-------|--------|-------|
| `/livesim` | GET | **Veľký dashboard** — DCA/RT obchodovanie po minútach. Realio overlay = real FTV/load/SOC z DB. Sekcie: cards, chMW, chSEPSMW, chDT, chPlan, chFlow (F4 nové), chRiadenie, chF, chC, tail tabuľka | case, start, view, curtail, use_rt, realio_overlay, profile | HTMLResponse (komplexný JS dashboard + Chart.js) | ÁNO (case setting, curtail flag do case.json) |
| `/livesim/chC_export` | GET | Excel export simulácie | case, start, format | FileResponse (.xlsx) | čít |
| `/livesim_pdf` | GET | PDF report (grafy + summary) | case, start | FileResponse (.pdf) | čít |

**Realio overlay:** nahrá sim dáta reálnymi meraniami z `realio_measurements.db` ak sú dostupné (per minute).
**Per-profile:** ÁNO.

---

## 6. Import spotrebiteľa (4 endpointy)

| Route | Metóda | Účel | Form | Vracia | Write |
|-------|--------|------|------|--------|-------|
| `/load_import` | GET | Upload UI | — | HTML form + status | čít |
| `/load_import` | POST | Nahrať CSV (15-min × N dní → priemer weekday/weekend) | file, rescale_factor, unit | HTML OK | ÁNO — `load_profile.update_data` |
| `/load_import/rescale` | POST | Zmeniť scale (×) na existujúcom profile | rescale_factor | HTML OK | ÁNO — `load_profile.rescale` |
| `/load_import/clear` | POST | Vymazať load profil | — | HTML OK | ÁNO — `load_profile.clear` |

---

## 7. OKTE VDT — Intraday platforma (11 endpointov)

Všetko READ-ONLY (žiadny write do OKTE; iba paper trades v lokálnej DB).

| Route | Účel | Sensitivity |
|-------|------|-------------|
| `/vdt` | Status účtu (status, orders, trades, position) | čít |
| `/vdt/live_advisor` | BM ceny + návrhy buy/sell vs plán | čít |
| `/vdt/d1` | D-1 plán vs BM realita (side-by-side) | čít |
| `/vdt/backtest` | Backtest stratégií cez históriu | čít |
| `/vdt/simulator` | Paper trading sandbox | čít |
| `/vdt/board` | Live orderbook (bid/ask, depth) | čít |
| `/vdt/test_orderbook` | Mock orderbook (debug) | čít |
| `/vdt/wsdl` | OKTE SOAP schema (info) | čít |
| `/vdt/zco_backtest` | Backtest ZCO collar stratégie | čít |
| `/vdt/raw_orderbook` | Raw JSON orderbook | JSON |
| `/vdt/inspect_cert`, `/vdt/discover`, `/vdt/probe`, `/vdt/zco_profile_rebuild` (POST) | Diag + cert tools | čít |

---

## 8. Realio batériový modul — **KRITICKÉ** (19 endpointov)

### 8a. Config (4 endpointov)
| Route | Vracia | Sensitivity |
|-------|--------|-------------|
| `/realio` | HTML dashboard (3 taby: viz, nastavenie, riadenie) | čít/zápis config |
| `/realio/save` (POST) | host/user/pwd/tags/poll_interval | ÁNO — realio_config.json |
| `/realio/test_read` (POST) | tag values | čít |
| `/realio/relogin` (POST) | refresh cookies | ÁNO — config cookies update |

### 8b. CSV housekeeping (2 POST)
- `/realio/cleanup_csv` — vyčistiť stale CSV riadky
- `/realio/fix_future_timestamps` — opravit corrupted timestamp values

### 8c. Backfill — import histórie (2 POST)
- `/realio/backfill_range` — import od-do
- `/realio/backfill` — N dní spätne

### 8d. **REÁLNE ZÁPISY DO HW** — najsenzitívnejšie (4 endpointy)
| Route | Forma | **Sensitivity** |
|-------|-------|-----------------|
| `/realio/batt_plan_export` (GET) | — | čít (iba preview) |
| `/realio/batt_plan_export/submit` (POST) | start_time, soc_sp, p_batt, hold_time | **🔴 REÁLNY ZÁPIS HW** (Bender batt setpoint) |
| `/realio/write` (POST) | soc_setpoint, p_batt_w, duration_min, tag_soc, tag_p | **🔴 REÁLNY ZÁPIS HW** |
| `/realio/fve_write` (POST) | curtail_pct, tag_curtail | **🔴 REÁLNY ZÁPIS HW** (FVE curtail) |

### 8e. Write discovery + control (2 POST)
- `/realio/discover_write` — list dostupných write tags
- `/realio/disable_control` — toggle off control flag

### 8f. Diagnostika komunikácie (4 POST)
- `/realio/scan_js` — JS schema
- `/realio/probe_ws` — WebSocket probe
- `/realio/scan_msg_types` — message type discovery
- `/realio/ws_listen` — WS live listen

### 8g. JSON API (2 GET)
- `/realio/api/latest` — JSON: {time, ftv_power_kw, load_power_kw, batt_*, grid_*}
- `/realio/api/soc` — JSON: {soc, ok} (pre iPhone widget)

---

## 9. Automatické riadenie (3 endpointy)

| Route | Vracia | Sensitivity |
|-------|--------|-------------|
| `/auto_control` | HTML — status, scheduler, kill switch, profile toggle | čít |
| `/auto_control/kill_switch` (POST) | OK | ÁNO — config flag |
| `/auto_control/toggle_profile` (POST) | Redirect | ÁNO — `profiles.set_active` |

**Backend:** APScheduler cron job (15-min cadence) — `compute_setpoint(profile)` → log do CSV → ak unlock=True a profile.mode=real, write do Bender.

---

## 10. Simulácia a kalibrácia (4 endpointy)

| Route | Vracia | Sensitivity |
|-------|--------|-------------|
| `/simulacia` (GET) | HTML form + aktuálny status | čít |
| `/simulacia` (POST) | HTML výsledky: equity, Sharpe, max DD | čít |
| `/kalibracia` (GET) | HTML form | čít |
| `/kalibracia` (POST) | HTML fit graph + metrics | čít |

---

## 11. RT poradca (1 endpoint)

| Route | Vracia | Sensitivity |
|-------|--------|-------------|
| `/rt` | HTML — real-time table (čas, BM cena, delta vs plán, návrh, ratio) + 5 grafov (c0 sig, c1 bands, c4 reco, c5 ceny, …) | čít |

**Header:** "Odporúčanie teraz" karta s aktuálnymi metrikami (cur_sig, cur_sys, cur_dt, cur_vdt).
**Pre SK trh:** swap sys_MW na SEPS regulation_power.

---

## 12. Utilita (3 endpointy)

| Route | Vracia | Sensitivity |
|-------|--------|-------------|
| `/data` (GET) | Raw súbor browser | čít |
| `/data` (POST) | Upload custom dáta | ÁNO — files |
| `/download` | ZIP (all plans/profiles/CSVs/logs) | čít |

---

## Sumár skupín

| Skupina | # endpointov | Hlavná kategória |
|---------|--------------|------------------|
| Plan D-1 + Denný trh | 6 | Generátor (write plans) |
| Profily | 5 | Config (write profiles) |
| FTV scenario | 2 | Testing |
| Plans browse | 3 | Prehľad |
| Livesim | 3 | Simulácia (overlay) |
| Load import | 4 | Data (write load_profile) |
| VDT | 11 | Read-only (paper) |
| **Realio** | **19** | **Config + 🔴 HW write** |
| Auto control | 3 | Automation |
| Simulácia/kalibrácia | 4 | Testing |
| RT advisor | 1 | Advisory |
| Utilita | 3 | Debug/export |
| **SPOLU** | **64** | |

---

## Pre refactor → Jinja2 (Fáza 3)

### Štruktúra `templates/`

```
templates/
  base.html              # layout (header s logo + nav + profile chip + login user + logout)
  pages/
    home.html            # form_page() = /plan formulár
    plan_result.html     # /plan POST response (tabuľka + cards + Joint LP card)
    dentrh_form.html
    dentrh_result.html
    plan_batch.html
    plans_list.html
    plan_view.html
    profiles_list.html
    profiles_edit.html
    livesim.html         # veľký dashboard
    load_import.html
    ftv_scenario.html
    vdt_dashboard.html
    vdt_live_advisor.html
    vdt_board.html
    realio_viz.html      # /realio?tab=vizualizacia
    realio_config.html
    realio_riadenie.html
    auto_control.html
    simulacia.html
    rt_advisor.html
  components/
    nav.html             # _nav() helper
    profile_chip.html
    cards.html           # ZISK/Bez batérie/Prínos
    joint_lp_card.html   # Joint LP settlement (F4)
    mult_template_editor.html
    distribution_section.html  # Tarify form
    error_banner.html
    overrides_status.html

static/
  css/
    app.css              # base colors, typography, buttons, tables
    livesim.css          # chart layout specific
    realio.css
  js/
    chart_helpers.js     # spoločné helpers pre Chart.js
    plan_form.js         # mapa + Nominatim + weather widget
    livesim.js           # chart updates
```

### Priority refaktorizácie

1. **`base.html` + `nav.html`** — okamžitý zisk, malé riziko
2. **`home.html` (formulár /plan)** — komplexný formulár, dobrý test patternu
3. **`profiles_edit.html`** — podobný formulár, sub-sekcie (plan/dentrh/RT/distribution)
4. **`plan_result.html`** — tabuľka + cards + Joint LP card (nedávno pridaná)
5. **`livesim.html`** — najväčší, najkomplexnejší, na koniec
6. **Realio** — kvôli sensitivity (HW write) refactor opatrne

### Risk areas
- Realio HW write endpointy — **nemeniť POST handlers**, iba GET render
- Livesim Chart.js inline JS — vytiahnuť do `static/js/livesim.js`
- Per-profile resolve — zachovať middleware ktorý dáva `g.profile` do contextu
- VDT WSDL/SOAP signing — nemeniť, len UI render okolo
