# Fáza 1 — DB migrácia kompletná (2026-06-04)

## Stav

Všetkých 8 perzistentných modulov implementuje dual storage cez `USE_DB` env var:

| Modul | Fáza | Commit | Verejné API |
|---|---|---|---|
| profiles.py | 1.9 | e7c7184 | list_profiles, save_profile, load_profile, delete_profile, get_active, set_active, get_mode, is_real |
| plan_store.py | 1.10 | b5f9e30 | has_plan, save_plan, load_plan, load_plan_safe, list_plans, delete_plan, missing_plans, resolve_profile |
| load_profile.py | 1.11 | 586f205 | has_data, load_for_date, get_meta, rescale, clear, import_csv |
| ftv_scenarios.py | 1.11 | 586f205 | has_scenario, save_scenario, load_scenario, delete_scenario, list_scenarios |
| vdt_live_advisor.py | 1.12 | fba6b82 | append_paper_trade, append_extra_paper_trade (dual write s DB UPSERT) |
| auto_control.py | 1.12 | fba6b82 | _append_log (dual write) |
| case_config.py | 1.13 | cd1d877 | list_cases, load_case, save_case |
| core/state.py | 1.13 | cd1d877 | _ui_load, _ui_save |

## Pattern

Konzistentný šablónový postup pre každý modul:
1. `_USE_DB = os.environ.get("USE_DB", "0") in ("1", "true", ...)` flag
2. `_db_available()` helper — vie sa fallback ak DB chýba
3. Read funkcie: pri USE_DB najskôr z DB, JSON ako fallback
4. Write funkcie: vždy JSON (back-compat) + ak USE_DB tiež DB

## Paralelný beh main vs refactor-v2

| Branch | Port | USE_DB | Storage |
|---|---|---|---|
| main | 8000 | nie | JSON-only (žiadna zmena) |
| refactor-v2 | 8001 | =1 | DB read primárny, dual write |

E2E smoke test prešiel na refactor-v2:
- 8 profilov, 36 plánov (cz/Simulacia_Coop), 477 VDT trades, ui_settings round-trip OK
- Backward compat: JSON-only mode → 8 profilov, 160 plánov, ftv 1 scenár, cases 3

## Ďalšie kroky (Fáza 2)

Auth — User/Session/UserProfileAccess tabuľky sú už pripravené v db/models.py.
Treba pridať login endpoint, session middleware, role decorator.
