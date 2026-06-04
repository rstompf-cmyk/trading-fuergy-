# Audit dátového modelu — FTV+batéria FastAPI appka

> Fáza 0 migrácie pred Windows produkciou. Stav: 2026-06-04.
>
> Cieľ: pripraviť kompletný prehľad všetkých perzistentných úložísk
> pre návrh SQLAlchemy ORM modelov v Fáze 1.

## Súhrn

Aplikácia rozdeľuje perzistentné úložiská podľa:
1. **Market** (CZ vs SK): `out/cz/` a `out/sk/` oddelené dáta
2. **Profile** (per-zákazník): `out/profiles/` — zdieľané medzi trhmi
3. **Typ** (plány, scenáre, load profily, merania): podadresáre v každom market rootu

15 kategórií úložísk. Hlavné formáty: JSON (profily, plány, scenáre, config), CSV (logy, ceny, time-series), SQLite (realio_measurements).

---

## 1. Profiles — konfigurácia zákazníka

**Modul:** `profiles.py`
**Cesta:** `out/profiles/<name>.json` + `out/profiles/_active.json` (port=8000) / `out/profiles/_active_<PORT>.json` (per-port)
**Format:** JSON
**Počet:** ~11 profilov (Trakany, Trakany_real, FTV_Predaj, Bat_D-1_bat_2, Simulacia_Coop, …)
**Vzťahy:** Per-profil, zdieľaný medzi CZ a SK (market-agnostic)

### Schéma `profile.json`

```jsonc
{
  "name": "Trakany",
  "mode": "simulation" | "real",        // FIXNÉ pri vzniku, neprepínateľné
  "created_at": "2026-...",
  "updated_at": "2026-...",
  "plan": {                              // form polia z /plan
    "lat": float, "lon": float,
    "kwp": float, "tilt": float, "azimuth": float, "eff": float,
    "batt_kw": float, "batt_kwh": float,
    "eff_c": float, "eff_d": float,
    "soc_min": float, "soc_max": float, "soc_init": float, "terminal_soc": float,
    "grid_kw": float, "grid_kw_import": float, "grid_kw_export": float,
    "grid_fee": float, "cycle_cost": float,
    "min_spread": float, "min_trade": float,
    "price_scale": float, "pv_scale": float,
    "allow_curtail": bool, "allow_grid_charge": bool, "block_neg_import": bool,
    "no_planned_discharge": bool, "rt_freedom": bool, "aggressive_rt": bool,
    "ftv_balance": bool, "ftv_lookahead_h": float, "ftv_persistence_throttle": bool,
    "rt_no_worsen_dev": bool, "ftv_strict_plan": bool, "ftv_strict_deadband_kw": float,
    "zco_bias_w": float,
    "baseline_im_mode": "dt_x"|"fix", "baseline_im_value": float,
    "baseline_ex_mode": "dt_x"|"fix", "baseline_ex_value": float,
    "max_export_kwh_day": float, "max_import_kwh_day": float,
    "joint_lp": {                        // Joint LP toggle (F2)
      "enabled": bool, "trade_batt": bool, "trade_ftv": bool,
      "trade_load": bool, "use_vdt": bool, "optimize_distribution": bool
    }
  },
  "dentrh": { ... },                     // form polia z /dentrh (15-min)
  "rt": { ... },                         // RT poradca settings (kdis, kchg, dtk, rboost)
  "mult96": [float × 96],                // šablóna multiplierov pre 15-min sloty
  "rt_on96": [0|1 × 96],                 // RT zapnutý/vypnutý per slot
  "distribution": {                      // F3 distribučné tarify (TOU + SK poplatky)
    "enabled": bool,
    "distribution_company": "ZSD"|"SSD"|"VSD",
    "tariff_group": "MO1"|"MO2"|"MO3"|"VO1"|"VO2"|"VO3"|"VO4"|"VO5"|"VTL"|"CUSTOM",
    "voltage_level": "NN"|"VN"|"VVN",
    "tou_mode": "tou"|"flat"|"hourly",
    "tou_high_eur_per_mwh": float, "tou_low_eur_per_mwh": float,
    "tou_high_hours": [int], "tou_weekend_low_only": bool,
    "hourly_custom_eur_per_mwh": [float × 24] | null,
    "tps_eur_per_mwh": float, "ss_eur_per_mwh": float, "oze_eur_per_mwh": float,
    "peak_charge_eur_per_kw_month": float, "monthly_fix_eur": float
  },
  "note": ""
}
```

### `_active.json` (per-port)

```jsonc
{"name": "Trakany", "set_at": "2026-..."}
```

**Volajúci moduli:** `profiles.py` (CRUD), `plan_store._dir_for(profile)`, `joint_lp_integration.get_flags_from_profile`, `distribution_cost.get_config(profile)`, `app.py` na desiatkach miest.

---

## 2. PlanStore — D-1 plány optimalizácie

**Modul:** `plan_store.py`
**Cesta:** `out/<market>/plans/<profile>/<YYYY-MM-DD>_<step>min_<kind>.json`
**Format:** JSON
**Počet:** ~150 súborov za 5 mesiacov (január–máj 2026, jeden per deň per profile)
**Vzťahy:** Per-market, per-profile, per-day, per-kind ("plan" 60min alebo "dentrh" 15min)

### Schéma

```jsonc
{
  "date": "YYYY-MM-DD",
  "step_min": 60 | 15,
  "kind": "plan" | "dentrh",
  "generated_at": "2026-...",
  "params": { /* všetky config parametre z profilu pri generovaní */ },
  "block_planned_discharge": bool,
  "zco_bias_w": float,
  "rt_freedom": bool,
  "mults": [float × 24],                 // hodinová agregácia z 96-slot šablóny
  "rt_mask": [0|1 × 24],                 // RT mask effective
  "schedule": {
    "hour": [int × N],
    "pv_kwh": [float × N],
    "load_kwh": [float × N],
    "price_eur": [float × N],
    "batt_kw": [float × N],
    "grid_kwh": [float × N],
    "order_mwh": [float × N],
    "curtail_kwh": [float × N],
    "soc_pct": [float × N],
    "_charge_kw": [float × N],
    "_discharge_kw": [float × N],
    "_export_kwh": [float × N],
    "_import_kwh": [float × N],
    "soc_kwh": [float × N]
  },
  "summary": {
    "ZISK_EUR": float, "bez_baterie_EUR": float, "prinos_baterie_EUR": float,
    "nabite_kWh": float, "vybite_kWh": float, "import_kWh": float, "orezane_kWh": float,
    "_joint_lp": bool, "_joint_flags": {...}, "_joint_economics": {...}
  },
  "meta": {"source": "/plan", "price_kind": "predicted"}
}
```

**Volajúci moduli:** `app.py /plan POST`, `app.py /dentrh POST`, `app.py /plan_batch`, `livesim` (strict mode read), `combined_backtest`, `vdt_live_advisor`.

---

## 3. PlanOverrides — ručné multiplikátory

**Modul:** `plan_overrides.py`
**Cesta:**
- `out/<market>/plan_overrides/<profile>/_template.json` — globálna šablóna
- `out/<market>/plan_overrides/<profile>/<YYYY-MM-DD>.json` — per-day prepis (legacy, postupne nahradzované šablónou)

**Format:** JSON

### Schéma

```jsonc
{
  "mult96": [float × 96],                // NaN = "neoverride"
  "rt_on96": [0|1 × 96]
}
```

**Priorita:** per-day override > _template > default 1.0

---

## 4. LoadProfile — spotreba zákazníka

**Modul:** `load_profile.py`, `load_minute.py`
**Cesta:** `out/<market>/load_profile/<profile>/profile.json` (default = `out/<market>/load_profile/profile.json`)
**Format:** JSON

### Schéma

```jsonc
{
  "imported_dates": ["YYYY-MM-DD", ...],
  "weekday_kw": [float × 96],            // 15-min priemer pracovných dní
  "weekend_kw": [float × 96],
  "unit": "kW",                          // alebo "kWh" (rescale aware)
  "meta": {
    "n_weekday_days": int,
    "n_weekend_days": int,
    "import_date": "2026-...",
    "note": str
  }
}
```

**Vzťahy:** Per-profile, per-market.
**Input:** CSV upload cez `/load_import` (15-min timestamp + kW), auto-detekcia oddeľovača (`,;\t`) a desatinného znaku (`.,`).

---

## 5. FTV Scenarios — ručný override hodinového FTV

**Modul:** `ftv_scenarios.py`
**Cesta:** `out/<market>/ftv_scenarios/<YYYY-MM-DD>.json`
**Format:** JSON

### Schéma

```jsonc
{
  "date": "YYYY-MM-DD",
  "hourly_kw": [float × 24],
  "smooth_sigma": float,
  "offset_h": float,                     // časový posun
  "saved_at": "2026-...",
  "note": str
}
```

**Vzťahy:** Per-day, per-market, **globálne** (nie per-profile).
**Použitie:** Livesim ho použije namiesto PVF prognózy ak existuje pre daný deň.

---

## 6. Auto Control Log — paper trading rozhodnutia

**Modul:** `auto_control.py`
**Cesta:** `out/<market>/auto_control_log.csv`
**Format:** CSV append-only

### Stĺpce

```
timestamp, datetime, profile, soc_pct, batt_kw_setpoint, mode, dry_run, reason,
margin_check, soc_terminal_ok, grid_capacity_ok, plan_available, setpoint_clipped,
grid_kw_min, grid_kw_max, price_eur_mwh, qty_kwh, notes
```

**Cron:** quarter-hourly (00, 15, 30, 45 minút). Číta aktívny plán, počíta setpoint, loguje.

---

## 7. Realio Measurements — real-time merania

**Modul:** `realio.py`, `realio_db.py`
**Cesta:**
- SQLite primary: `out/<market>/realio_measurements.db`
- CSV legacy: `out/<market>/realio_measurements.csv` (deprecated, ale ešte čítaný pre fallback)

### SQLite schéma

```sql
CREATE TABLE realio_measurements (
  time_ms INTEGER PRIMARY KEY,           -- UTC ms (canonical)
  time_iso TEXT,                          -- ISO8601 lokálny čas (read convenience)
  ftv_power_kw REAL,
  ftv_power_kw_15m REAL,
  load_power_kw REAL,
  load_power_kw_15m REAL,
  batt_power_kw REAL,
  batt_soc_pct REAL,
  grid_power_kw REAL,
  batt_setpoint_kw_cmd REAL,             // posledný command zapísaný do Bender
  ftv_curtail_kw_cmd REAL
);
CREATE INDEX idx_time_iso ON realio_measurements(time_iso);
```

**Zdroj:** HTTP polling z Trakany Bender dashboardu (každú minútu).
**UPSERT:** atomic per tag (žiadne race condition pri concurrent writes).

### Realio Config

**Cesta:** `out/<market>/realio_config.json`

```jsonc
{
  "host": "https://...",
  "user": "...",
  "password": "...",
  "cookies": {...},                      // session cookies (refresh každých 45 min)
  "tags_read": {                         // logical → hardware tag mapping
    "ftv_power_kw": "PHV_Aggregated_C_Active_Power_1m",
    "load_power_kw": "ELM1_Aggregated_C_Power_1m",
    "batt_soc_pct": "BAT_Aggregated_C_SOC",
    ...
  },
  "tags_write": {
    "batt_setpoint_kw": "BAT_Manual_Plan",
    "ftv_curtail_pct": "PHV_Curtail_Setpoint"
  },
  "fve_control": {
    "enabled": bool,
    "min_curtail_pct": 0,
    "max_curtail_pct": 100
  },
  "poll_interval_sec": 60
}
```

---

## 8. Livesim Log — paper trading simulácia

**Modul:** `livesim.py`
**Cesta:** `out/<market>/livesim_log_<case>_<PORT>.csv` (per-port, per-case)
**Format:** CSV append-only
**Veľkosť:** desiatky MB pri 5-mes histórii (1440 minút × 150 dní)

### Stĺpce

```
time, slot_idx, hour, pv_kw, load_kw, batt_power_kw, batt_soc_pct,
plan_grid_kwh, dt_eur, dt_real_eur, zco_eur, vdt_eur,
plan_batt_kw, batt_kw_realistic, soc_pct, mw_sig, band_dis, band_chg,
rt_dir, rt_power_pct, rt_reason,
dt_rev_min, rt_rev_min, cum_total,
ftv_kw, ftv_hour_plan_kw, ftv_min_real_kw, ftv_min_curtailed_kw,
load_plan_kw, load_min_real_kw, plan_curtail_kwh,
realio_ftv_kw, realio_load_kw, realio_load_kw_15m, realio_batt_kw,
realio_soc_pct, realio_grid_kw,
is_live, prov_date, case, step_min, ts15
```

**Per-port isolation:** každá inštancia (port 8000, 8001, …) má vlastný log súbor.
**Restart-friendly:** pokračuje od posledného spracovaného timestampu.

---

## 9. VDT Paper Trades — virtuálne intraday obchody

**Modul:** `vdt_live_advisor.py`, `vdt_extras.py`, `auto_control.py`
**Cesta:** `out/<market>/vdt_paper_trades.csv`
**Format:** CSV append + UPSERT (per profile, slot, action)

### Stĺpce

```
timestamp, profile, slot, action, kwh, price_eur_mwh, dam_clearing_eur_mwh,
soc_before_pct, soc_after_pct, delta_profit_eur, status, source
```

**UPSERT logika:** ak prichádza záznam s `(profile, slot, action)` ktorý už existuje → prepíše. Inak append.

### VDT cache (per profile)

**Cesta:** `out/<market>/vdt_cache_<profile>.json`

```jsonc
{
  "profile": "Trakany",
  "ts": "2026-...",
  "full_plan": [
    {"slot": "12:15", "action": "discharge"|"charge", "kwh": float, "price_eur_mwh": float, ...},
    ...
  ],
  "dam_commits": [float × 96]            // DAM nominácia ako referencia
}
```

---

## 10. OKTE/OTE/PVF Prices a Imbalance

**Modul:** `settlement.py`, `okte_sk.py`, `seps_sk.py`, `internal_historian.py`, `core/caches.py`
**Cesty:**

### Day-ahead clearing
- CZ: `out/cz/price_train_2026.csv` — hodinové DT €/MWh
- SK: `out/sk/historian_C_OKTE_ISOT_15m_final.csv` — 15-min DT €/MWh z historianu

### Imbalance / ZCO real
- CZ: `out/cz/imbalance_minute.csv` — 1-min ZCO real €/MWh (z ČEPS)
- SK: `out/sk/historian_I_WEB_OKTE_ZCO_15m.csv` — 15-min ZCO real z historianu

### VDT (intraday) ceny
- SK: `out/sk/historian_I_OKTE_ISOT_VDT_15m.csv` — 15-min VDT €/MWh

### PVF cache (per-day)
- `out/cache/ote_dt_<YYYY-MM-DD>.csv` — OTE fetch cache (TTL aware)
- PVGIS TMY: in-memory cache (process-level)

### Schémy

```
price_train_2026.csv: time, hour, date, isot_eur, ote_eur, pse_eur, pge_eur
imbalance_minute.csv: timestamp, price_eur_mwh
historian_*.csv:     timestamp, value, quality
```

---

## 11. SEPS DAE Real-time (SK)

**Modul:** `seps_sk.py`
**Endpoint:** `https://dae.sepsas.sk/SK_PROD/...` (LoadData)
**Cache:** `out/cz/seps_cookies.json` (session cookies)

### Response fields

```jsonc
{
  "frequency_hz": float,
  "load_mw": float,
  "production_mw": float,
  "real_balance_mw": float,              // sign flipped on input (vs ČEPS)
  "scheduled_balance_mw": float,
  "regulation_power_mw": float,
  "data_updated_sec": int
}
```

---

## 12. Internal Historian — EMS tagy (Trakany)

**Modul:** `internal_historian.py`
**Endpoint:** `http://<bender>/tag-data?json=...`
**Cookies:** `connect.sid`, `Bender-Authenticate` (refresh každých 45 min cez Playwright)

### Common tags
- `C_OKTE_ISOT_15m_final` — DAM clearing
- `I_WEB_OKTE_ZCO_15m` — ZCO real
- `I_OKTE_ISOT_VDT_15m` — VDT real
- `C_OKTE_LOAD_15m` — load forecast

**Output:** per-tag CSV v `out/sk/historian_<tag>.csv`

---

## 13. UI Settings — per-port konfigurácia

**Modul:** `core/state.py` (`_ui_load`, `_ui_save`)
**Cesta:** `out/ui_settings_<PORT>.json` (PORT=8000 = `ui_settings.json` bez sufixu)
**Format:** JSON

### Schéma

```jsonc
{
  "plan": {...},                         // form params z /plan (rovnaké ako profile.plan)
  "dentrh": {...},
  "rt": {...},                           // RT poradca
  "case": "default" | "denny_trh_15m",
  "last_date": "YYYY-MM-DD",
  ...
}
```

**Vzťahy:** Per-port singleton. Auto-save pri každej zmene v UI.

---

## 14. Backtest results

**Modul:** `combined_backtest.py`
**Cesta:** `out/<market>/backtest.csv`
**Format:** CSV append

### Stĺpce

```
date, profile, market, pv_kwh, load_kwh,
batt_charge_kwh, batt_discharge_kwh,
dam_revenue_eur, zco_revenue_eur,
grid_fee_eur, cycle_cost_eur,
net_profit_eur, rt_mode, case
```

---

## 15. Cases — RT poradca konfigurácia

**Modul:** `core/state.py` (load_case, save_case)
**Cesta:** `out/cases/<case>.json`
**Format:** JSON

### Schéma

```jsonc
{
  "name": "default" | "denny_trh_15m" | "realistic",
  "allow_curtail": bool,
  "rt_kdis": float,
  "rt_kchg": float,
  "dtk": float,
  "rboost": float,
  "battery": {...},
  ...
}
```

**Vzťahy:** Per-instance config (žiadny profile-specific dnes, ale Trading rola môže potrebovať custom cases per profile).

---

## Special concerns

### Lock files & race conditions
- `auto_control_unlock.json` — safety token (`I_UNDERSTAND_THIS_WRITES_TO_BENDER`) pre REAL mode write
- SQLite atomic writes — realio_measurements.db
- CSV append-only — lock-free (OS handles)
- Market-aware separation: žiadny cross-talk medzi `cz/` a `sk/`

### Backward compat
- `plan_store.DIR`, `profiles.DIR` sú resolved dynamicky cez `_dir_root()` (market-aware)
- `plan_overrides._template.json` (legacy bez profilu) vs `<profile>/_template.json` (nové)
- Realio CSV (legacy) + SQLite (primary) bežia paralelne

### Externé schémy
- Bender tagy z internal_historiana sú externalne definované (Trakany dashboard `/tag-data` API)
- SEPS DAE má fixed JSON štruktúru ale label texty môžu byť regulárne výrazy
- OKTE VDT cez SOAP/WSDL s WSSE signing (Cert authentication)

---

## Mapping na ORM tabuľky (návrh pre Fázu 1)

### Hlavné tabuľky

| Úložisko | DB tabuľka | Primary key | Vzťahy |
|---|---|---|---|
| Profiles (.json) | `profile` | `id` | 1:N → plans, distribution, load_profile |
| Profile.plan / .dentrh / .rt | `profile_settings` (JSON column) | (profile_id, kind) | |
| PlanStore | `plan` | `id` | profile_id, date, kind, step_min, market |
| Plan schedule | `plan_slot` | `(plan_id, slot_idx)` | hodinový/15-min časový rad |
| PlanOverrides | `plan_override` | `id` | profile_id, market, mult96, rt_on96 |
| LoadProfile | `load_profile` | `id` | profile_id, market |
| FTV Scenarios | `ftv_scenario` | `(market, date)` | globálne |
| Auto Control Log | `auto_control_event` | `id` | timestamp, profile_id |
| Realio Measurements | `realio_measurement` | `time_ms` | (existujúce SQLite, integrate) |
| Realio Config | `realio_config` | `id` | per market |
| Livesim Log | `livesim_event` | `id` | port, case, time, market, profile_id |
| VDT Paper Trades | `vdt_paper_trade` | `id` | UNIQUE(profile_id, slot, action) |
| VDT Cache | `vdt_cache` | `(profile_id, market)` | rolling JSON |
| Prices (DT) | `dt_price` | `(market, time)` | hodinové |
| Prices (VDT) | `vdt_price` | `(market, time_15m)` | 15-min |
| Imbalance | `zco_price` | `(market, time)` | 1-min CZ / 15-min SK |
| Cases | `case` | `id` | name, JSON config |
| UI Settings | `ui_settings` | `(port, key)` | per-port |
| Backtest results | `backtest_result` | `id` | date, profile_id, market |

### Nové tabuľky pre auth

| Tabuľka | Účel |
|---|---|
| `user` | id, username, password_hash, email, role_id, created_at |
| `role` | id, name (Admin/Obchodník/Trading/Zákazník) |
| `permission` | id, role_id, resource, action |
| `user_profile_access` | (user_id, profile_id, can_read, can_write) |
| `session` | id, user_id, token, expires_at |

---

## Migrácia — pristup

1. **Phase 1.1:** Create empty SQLAlchemy models + alembic init
2. **Phase 1.2:** Import existing JSON profiles → `profile` table (jednorazový script)
3. **Phase 1.3:** Import existing plan_store JSON → `plan` + `plan_slot` tables
4. **Phase 1.4:** Refactor `profiles.py` na DB read/write (zachovať API, ale internal storage = SQL)
5. **Phase 1.5:** Refactor `plan_store.py`
6. **Phase 1.6:** Refactor `load_profile.py`, `ftv_scenarios.py`
7. **Phase 1.7:** Refactor `vdt_paper_trades`, `auto_control_log` (CSV → DB)
8. **Phase 1.8:** Integrate `realio_measurements.db` (rename existing SQLite, link via SQLAlchemy)
9. **Phase 1.9:** Livesim logs — ostať ako CSV (objem > 100 MB, time-series workload, append-only) ale možno indexovať cez `livesim_index` tabuľku
10. **Phase 1.10:** Cases, ui_settings, vdt_cache → DB

**Princíp:** zachovať verejné API každého modulu rovnaké. Iba internal storage sa zmení z disk → DB. Refactor po module + smoke test.
