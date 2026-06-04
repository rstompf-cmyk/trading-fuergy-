# Návrh refaktoringu — FTV+batéria appka

## Stav teraz (problém)

```
app.py          5586 riadkov   ← monolit, ~30 endpointov, fetch helpery, HTML render, state
livesim.py       848
seps_sk.py       893           ← fetcher + parser + historian + auth + logger spolu
rt_controller.py 722
combined_backtest 506
+17 menších modulov
─────────────────
total           ~15 500 riadkov
```

Hlavná bolesť: **`app.py` je strašne veľký** a každá zmena ho musí dotknúť, čo:
- generuje merge conflicts (aj keď pracuješ sám)
- ťažšie sa hľadá kód
- testovanie je takmer nemožné (FastAPI handler + business logic + HTML render v jednom)
- onboard nového vývojára trvá dni

## Cieľová štruktúra

```
app/
  main.py                    ~60 riadkov — FastAPI app, lifespan, middleware, mount routes
  routes/
    plan.py                  /, /plan, /dentrh, /plan_batch        ~700 r.
    profile.py               /profiles/*                            ~250 r.
    livesim.py               /livesim, /livesim/chC_export          ~1000 r.
    rt.py                    /rt                                    ~700 r.
    simulacia.py             /simulacia                             ~250 r.
    market.py                /market/set, /ftv_scenario             ~200 r.
    data.py                  /data, /load_import, /download, /kalibracia  ~300 r.
  core/
    state.py                 _ui_load, _ui_save, _PORT, DEF, settings_sig
    caches.py                _OTE_CACHE, _PVF_CACHE, _MODEL_CACHE, _LIVE_FETCH_CACHE
    fetch.py                 _rt_fetch, _rt_live_frame, _fetch_ote_cached, _get_live_dataframe_cached
  ui/
    html.py                  _nav, _field, head/style, _RECO_COL, _js, _nz
    charts.py                Chart.js render helpery (chPlan, chRiadenie, chF, c0, c4...)
    tables.py                _detail_table, period rows render

engine/                      (premenované z root)
  rt_controller.py           run_day_physical, decide_reason, mw_signal — bez zmien
  optimizer.py               LP optimizer
  livesim.py                 advance(), today_trace, fut block

markets/                     ← NOVÝ: market adapter pattern
  base.py                    abstract Market: get_minute_data(d), get_live_data(d), get_dt(d), get_vdt(d), get_zco(d)
  cz.py                      konkrétna CZ implementácia (ČEPS imbalance, OTE-CR DT, ČEPS ZCO odhad)
  sk.py                      konkrétna SK implementácia (SEPS, OKTE historian)
  registry.py                get_market(name) → factory

data/
  seps/
    realtime.py              fetch_seps_realtime, parse_system_state    ~250 r.
    cookies.py               seps_cookies_refresh (Playwright)          ~100 r.
    historian.py             build_sk_live_minutes, build_sk_minute_history, load_seps_mw_for_day  ~400 r.
    okte.py                  load_okte_dt/vdt/zco_for_day                ~150 r.
    log.py                   log_realtime, CSV utility                    ~50 r.
  ceps/                      (premenované z koreňa)
    data_sources.py          fetch_ote_dayahead, fetch_ceps_*
  historian/                 (firemný)
    client.py                Historian class
    login.py                 Playwright auth
    backfill.py              CLI script
  fetchers/
    pvgis.py                 PV forecast (MET Norway + PVGIS)
    nominatim.py             location search

storage/                     (perzistencia)
  plan_store.py              plán per dátum/profil
  plan_overrides.py          mults + rt_mask
  ftv_scenarios.py           FTV minute scenár
  profiles.py                profile JSON
  load_profile.py            load CSV import
  case_config.py             cases (default, realistic, dt_15min...)

bg/
  scheduler.py               APScheduler jobs
  market_migrate.py          one-time DB layout migration

scripts/                     (CLI tools, nie importované)
  backfill.py
  fetch_imbalance_history.py
  fetch_okte_imbalance.py
  historian_backfill.py
  deviation_stats.py

tests/                       ← NOVÝ
  golden/
    test_run_day_physical.py        snapshot test
    test_optimize_day.py
  markets/
    test_cz_adapter.py
    test_sk_adapter.py
  unit/
    test_seps_parser.py
    test_okte_loader.py
```

## Migrácia — po fázach (každá fáza je nezávisle deployable)

### Fáza 1 — Extrakcia "lacných" pomocníkov (najmenšie riziko)

**Cieľ:** Vyrezať z `app.py` veci čo nemajú dependencies na FastAPI app objekt.

```
ui/html.py        ← _nav, _field, _RECO_COL, _js, _nz, head štýly
core/state.py     ← _ui_load, _ui_save, _PORT, DEF, settings_sig
core/caches.py    ← _OTE_CACHE, _OTE_TTL_FUTURE, _MODEL_CACHE, _LIVE_FETCH_CACHE
core/fetch.py     ← _rt_fetch, _rt_live_frame, _fetch_ote_cached
```

`app.py` ostáva s endpointami ale len ich importuje. Po Fáze 1: ~4000 r.

**Risk:** veľmi nízky — pure Python presun, žiadna logika.
**Čas:** 2-3 hodiny.
**Test:** smoke test že appka štartuje a `/livesim` načítava CZ aj SK.

### Fáza 2 — Routes per endpoint group

**Cieľ:** Každú skupinu endpointov dostať do vlastného modulu.

```
routes/plan.py        ← všetky /plan*, /, /dentrh, /plan_batch, /plans, /plan_view
routes/livesim.py     ← /livesim, /livesim/chC_export
routes/rt.py          ← /rt
routes/profile.py     ← /profiles*
routes/simulacia.py   ← /simulacia
routes/market.py      ← /market/set, /ftv_scenario
routes/data.py        ← /data, /load_import*, /download, /kalibracia
```

`app/main.py` len mount-uje:
```python
from app.routes import plan, livesim, rt, profile, ...
app.include_router(plan.router)
app.include_router(livesim.router)
# ...
```

Po Fáze 2: `app/main.py` ~60 r., každý route file 200-1000 r.

**Risk:** stredný — treba pretiahnuť referencie na shared state. Použiť `core/state.py` ako single source of truth.
**Čas:** 1-2 dni.
**Test:** Manuálny click-through cez všetky stránky + golden test pre run_day_physical (už máš).

### Fáza 3 — Market adapter pattern

**Cieľ:** Eliminovať `if is_sk:` rozvetvenia v engine kóde. Trh = abstrakcia.

```python
# markets/base.py
class Market(ABC):
    @abstractmethod
    def get_minute_history(self, from_d, to_d) -> pd.DataFrame: ...
    @abstractmethod
    def get_live_minutes(self, day) -> pd.DataFrame: ...
    @abstractmethod
    def get_dt(self, day) -> pd.DataFrame: ...
    @abstractmethod
    def get_vdt(self, day) -> pd.DataFrame: ...
    @abstractmethod
    def get_zco(self, day) -> pd.DataFrame: ...
    @abstractmethod
    def get_frr_activations(self, day) -> pd.DataFrame: ...

# markets/sk.py
class SKMarket(Market):
    def get_dt(self, day):
        return data.seps.okte.load_okte_dt_for_day(day)
    def get_zco(self, day):
        return data.seps.okte.load_okte_zco_for_day(day)  # vráti None pre dnes
    ...

# engine/livesim.py
def advance(case, port, market: Market = None):
    market = market or get_active_market()
    mn = market.get_minute_history(...)
    ...
```

Toto je **najvyššia hodnota** — nový trh (napr. AT, HU) je len nový `markets/at.py` so 200 riadkami. Žiadne dotyky `livesim`, `app.py`, `rt_controller`.

**Risk:** vysoký, ale len pri prvom resolúciu. Po nasadení sa znižuje.
**Čas:** 2-3 dni.
**Test:** Existujúce CZ správanie musí byť bit-exact (golden tests). SK fungovať ako teraz.

### Fáza 4 — Rozdelenie `seps_sk.py`

Aktuálne `seps_sk.py` (893 r.) miesa fetcher, parser, historian, auth a CSV logger. Po rozdelení každý súbor je <300 r. a má jednu zodpovednosť.

```
data/seps/realtime.py    ← SepsSession class, fetch_seps_realtime, parse_system_state
data/seps/cookies.py     ← seps_cookies_refresh integration
data/seps/historian.py   ← build_sk_live_minutes, build_sk_minute_history, load_seps_mw_for_day
data/seps/okte.py        ← load_okte_dt/vdt/zco_for_day
data/seps/log.py         ← log_realtime, _csv_path
```

**Risk:** nízky.
**Čas:** pol dňa.

### Fáza 5 — Testy

Existujú golden testy na `run_day_physical`? Treba ich explicitne pridať pre:
- `engine/rt_controller.run_day_physical` — snapshot 1 typický deň, hash výsledku
- `engine/optimizer.optimize_day` — to isté
- `engine/livesim.advance` — happy path + edge cases (žiadny ZCO, žiadne FTV, žiadny plán)
- `markets/cz.get_minute_history` — známy deň, kontrolný hash
- `markets/sk.get_minute_history` — to isté

**Čas:** 1-2 dni.

## Priority odporúčania

Ak chceš urobiť **jednu vec ktorá má najväčší impact**:

1. **Fáza 1 + Fáza 2** — rozdeliť `app.py` na route files. Tým sa zníži kognitívna záťaž najviac. Aj keby si nikdy nedošiel k market adapteru, už toto ti šetrí čas pri každej zmene.

Ak chceš urobiť **dve veci**:

1. Fáza 1+2 (rozdelenie app.py)
2. Fáza 5 (golden testy) — bez nich sa nedá robiť agresívna refaktorizácia ďalej

Až keď máš testy, môžeš sa pustiť do Fázy 3 (market adapter) s istotou že nič nerozbiješ.

## Čo NErobiť

- **Nepremenovávať** kompletne, ak nemáš testy. Refactor bez testov = lottery.
- **Nemiešať** refactor s feature work v jednom commit-e. Vždy separate PR/commit.
- **Nezačínať od `rt_controller.py`** — to je jadro fyziky, najmenej sa mení, najťažšie sa testuje. Nech ostane ako je, kým nemáš golden tests.
- **Neignorovať** `app.v1.py` (995 r.) — to vyzerá ako stará verzia. Ak je live používaná, treba mergnúť alebo zmazať.

## Stručná zhrnutie

```
TERAZ:           app.py 5586r monolit, 17 ďalších modulov, 0 testov
PO Fáze 1+2:     app.py 60r + 7 route files po 200-1000r, ľahké navigovať
PO Fáze 1-3:     CZ aj SK ide cez `Market` interface, pridať nový trh = 1 súbor
PO Fáze 1-5:     plus golden testy → bezpečné meniť hocičo
```

Ak chceš začať, doporučujem začať Fázou 1 (extrakcia helperov) — je to malý, izolovaný PR ktorý sa dá spraviť za jeden večer a okamžite cítiť benefit.
