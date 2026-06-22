# CDC fleet + Zákazník — implementačný plán

Cieľ: reálne riadenie SK (neskôr CZ) batérií cez centrálny **CDC server**, integrované do
existujúcej appky (žiadna nová appka). Beh **proces-per-batéria** na pozadí, nový koncept
**Zákazník** (1 zákazník = N batérií, napr. Muller = `Muller-SE` + `Muller2-SE`), agregovaný
pohľad po zákazníkoch v **Manager dashboarde**. Profily (plány/RT) ostávajú samostatné — nemiešať.

## Rozhodnutia (potvrdené 2026-06-22)
- Server SK: `http://192.168.31.30:8088` (z VBA excelu). Hodnoty vo W → ×0.001 kW.
- Beh: **proces per batéria** (úplná izolácia) + supervisor.
- **Zákazník** = nová samostatná entita (nezávislá od `Block`, ktorý ostáva na trhovú agregáciu).
- Agregovaný pohľad: **rozšíriť Manager dashboard** + samostatná karta **Zákazník** na správu.
- Pilot: **VW-BA**.

## Model (existujúce fleet tabuľky — Battery oddelená od Profile = presne čo treba)
- `battery` (inštancia: hardvér/komunikácia, `country`, `enabled`, `profile_id` → plány) — **rozšíriť** o:
  - `customer_id` (FK → nový `customer`)
  - `backend` (`realio` | `cdc`, default `realio`)
  - `cdc_prefix` (napr. `VW-BA`)
- nová tabuľka `customer`: `id, name, country, note, created_at, updated_at`
- `profile` (plány/RT/dentrh) — **bez zmeny**, batéria naň odkazuje cez `profile_id`.
- Konfigurácia CDC **per krajina** = súbor `out/<market>/cdc_system.json` (host, auth, tag KORENE/suffixy, scale) — **už hotové**.

## Komunikačná vrstva (HOTOVÉ ✅)
- `cdc.py` — systémový config per krajina, poskladanie tagov z prefixu, read (GET) / write (POST), scale. Zápis gated `enabled+control_enabled+FLEET_REAL_WRITE=1` → inak DRY-RUN.
- `out/sk/cdc_system.json` — SK config (7 read + 2 write tag korene).
- `cdc_tag_roots.md` — editovateľný zoznam koreňov.
- `tools/cdc_test.py` — lokálny read test.

## Executor + beh na pozadí (HOTOVÉ ✅, aditívne, golden netknuté)
- `control/executor.py` — nový **`CdcExecutor`** (read_soc/apply_setpoint cez `cdc.py`, prefix) + `build_executor` dispatch podľa `battery.backend`. `RealExecutor`/`SimExecutor` bez zmeny.
- `workers/control_loop.py` — nový **`run_single(battery_id)`** režim (tick len jednej batérie) + `--battery <id>` / `FLEET_BATTERY_ID`. Legacy „jeden proces celá flotila" ostáva.
- `workers/fleet_supervisor.py` — **supervisor**: 1 proces na enabled batériu, monitor, reštart (backoff), graceful stop. Gated `FLEET_CONTROL=1`.

## Zostáva spraviť

### Fáza A — DB (sensitívne: migrácia, DEV-first)
1. `db/models.py`: tabuľka `Customer` + stĺpce `battery.customer_id/backend/cdc_prefix` (nullable, aditívne).
2. Alembic migrácia (pozn. pravidlá: ASCII, po builde upgrade; `tools/fix_alembic_version.py` ak stuck).
3. `fleet/repository.py`: `register_battery(... backend, cdc_prefix, customer_id)`, `_batt_dict` doplniť polia; CRUD `customer` (create/list/get/update/assign battery↔customer).

### Fáza B — setpoint source pre fleet (riadiace jadro)
4. Alokátor/plánovač: per batéria načítať jej profil (D-1 plán + RT slot) a `fleet.enqueue_command(bid,'setpoint',{kw})` na aktuálny slot. (Single-profile `auto_control` to robí dnes priamo pre Trakany_real; pre fleet to enqueuje príkaz, ktorý si inštancia v `control.loop.tick` prevezme.) Golden funkcie (`optimize_day`, `run_day_physical`) sa **nevolajú nanovo**, len sa číta hotový plán.
5. Integrovať štart supervisora do `scheduler.py` / deploy (dev servis, oddelený od webu).

### Fáza C — UI
6. Nav: pridať položky **„Konfigurácia CDC"** (`/cdc`) a **„Zákazník"** (`/customers`) do `templates/components/nav.html` (+ legacy `ui/html.py`).
7. `/cdc` — editor systémového configu per krajina (host/auth/tag korene/scale/enable) + Test read (prefix).
8. `/customers` — CRUD zákazníka + priradenie batérií (a ich profilov), prehľad batérií zákazníka.
9. **Manager dashboard** — agregácia po zákazníkoch (zákazník → batérie: SOC, výkon, plnenie plánu, €), využiť existujúci read-only nad effect_db/ledger/status.

### Fáza D — overenie a nasadenie
10. Live read test (`tools/cdc_test.py VW-BA sk`) → potvrdiť odpoveď + škálovanie + znamienko zápisu.
11. Golden testy 5/5 nedotknuté; nové unit/e2e SIM testy (CdcExecutor mock, run_single, supervisor reconcile).
12. DEV (8001) najprv, potom promote na PROD; `FLEET_REAL_WRITE=1` až po commissioningu pilota.

## Bezpečnostné poistky (zachované)
- Zápis na HW len `FLEET_REAL_WRITE=1` (default DRY-RUN). Beh procesov len `FLEET_CONTROL=1`.
- `read_soc` read-only; nedosiahnuteľný server → fail-safe degraded (nezhodí ostatné).
- CZ neskôr = len `out/cz/cdc_system.json` (iný host), rovnaké korene; tá istá `/cdc` stránka podľa aktívnej krajiny.
