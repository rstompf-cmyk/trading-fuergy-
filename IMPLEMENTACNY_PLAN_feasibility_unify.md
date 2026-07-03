# Implementačný plán: zjednotenie feasibility vrstvy

*2026-06-28. Nadväzuje na `ARCHITEKTURA_planovanie_obchodovanie_analyza.md`.*

## Princíp celej migrácie

Inkrementálne, **DEV first**, každý krok je samostatne nasaditeľný a overiteľný. `optimizer.optimize_day` a `rt_controller.run_day_physical` (golden) sa **nedotýkajú** — golden testy ostávajú platné po celý čas. Ekonomika (`realistic`, `rt_kdis/rt_kchg`, breakeven, soc-neutral, min_spread) sa **nemení**.

**Zlaté pravidlo každého kroku:** najprv napísať *characterization test* (zafixovať súčasné správanie na vzorke dní/vstupov), až potom refaktor. Refaktor je hotový len keď charakterizačný test = identický výstup. Žiadny krok nemení správanie, kým to nie je výslovne cieľ kroku (až KROK 5).

---

## KROK 0 — Bezpečnostná sieť (0,5 dňa)

Pred akoukoľvek zmenou zafixovať dnešné správanie.

1. **Snapshot fixtures.** Pre 3 profily (VW_simulacia_2/3/4) a 5 dní vyexportovať vstupy + výstupy oboch funkcií:
   - `soc_feasible_vdt(dam96, vdt96, soc_init, ...)` → `(vdt_allowed, soc_path, clipped)`
   - `clip_extras_to_capacity(...)` a `clip_extras_to_grid(...)` → `(extras, report)`
   Uložiť do `tests/fixtures/feasibility_golden/*.json`.
2. **Parity test** `tests/test_feasibility_parity.py`: načíta fixtures, zavolá funkcie, porovná bit-exact (tolerancia 1e-6).
3. Spustiť existujúce `tests/test_vdt_soc_feasible.py` + `tests/test_vdt_capacity_guard.py` + golden — zelené.

**Akcept:** parity test + golden zelené na DEV.

---

## KROK 1 — `core/feasibility.py` ako čistá zlúčenina (1–2 dni) ⭐

Vstupná brána. Nezmení správanie, len centralizuje matematiku.

### 1.1 Nový modul `core/feasibility.py`

Jedna fyzika batérie definovaná **raz**:

```python
def battery_step(soc_kwh, batt_kw, dt_h, eff_c, eff_d):
    """+batt_kw = vybíjanie (SOC↓ /eff_d), −batt_kw = nabíjanie (SOC↑ ·eff_c)."""
    if batt_kw > 0:   return soc_kwh - (batt_kw*dt_h)/eff_d
    if batt_kw < 0:   return soc_kwh + (-batt_kw*dt_h)*eff_c
    return soc_kwh
```

Jedna feasibility brána (zjednocuje `soc_feasible_vdt` + `clip_extras_to_capacity` + `clip_extras_to_grid`):

```python
def gate(soc_start_kwh, dam_batt_kw, vdt_batt_kw, *,
         batt_kwh, soc_min_frac, soc_max_frac, eff_c, eff_d, dt_h=0.25,
         grid_export_kw=None, grid_import_kw=None, net_base_kw=None,
         reserve_frac=0.0):
    """Vráti (vdt_allowed_kw[N], soc_path_kwh[N+1], report).
    DAM ostáva nedotknutý; orezáva sa LEN VDT na SOC ∧ grid ∧ výkon naraz."""
```

`gate` = presná logika zo `soc_feasible_vdt` (forward target clip s grid net_base) — **tá je správnejšia** než `clip_extras_to_capacity`, lebo rieši grid aj SOC v jednom prechode z jedného baseline. `clip_extras_to_*` (dict-based, dva baseline) sa stáva tenkým adaptérom nad `gate`.

Headroom pre RT (zjednocuje `audit_capacity` + `audit_action`):

```python
def headroom(soc_path_kwh, slot_idx, *, batt_kwh, soc_min_frac, soc_max_frac,
            eff_c, eff_d, dt_h, horizon_slots, reserve_frac=0.0):
    """Max povolený vybíjací/nabíjací kW v slote, aby budúca trajektória ostala v pásme."""
```

### 1.2 Charakterizačná zhoda

- `gate` musí na fixtures z KROKU 0 dať identický výstup ako `soc_feasible_vdt`.
- Adaptér `clip_extras_to_capacity_v2` (dict → pole → `gate` → dict) musí dať identický výstup ako starý `clip_extras_to_capacity` na fixtures, ALEBO ak sa líši, rozdiel musí byť dokázateľne „lepší" (menšia/rovnaká odchýlka) a vedome odsúhlasený — toto je jediné miesto, kde sa správanie môže zmeniť, a musí byť izolované a zdokumentované.

### 1.3 Staré moduly = tenké wrappery (dočasne)

`vdt_soc_feasible.soc_feasible_vdt` a `core/vdt_capacity_guard.*` ostanú ako funkcie, ale vnútri zavolajú `core.feasibility`. Žiadny caller sa zatiaľ nemení → nulové riziko.

**Akcept:** `test_feasibility_parity` + `test_vdt_soc_feasible` + `test_vdt_capacity_guard` + golden 5/5 zelené. Nasadiť na DEV, žiadna viditeľná zmena správania.

---

## KROK 2 — Zjednotiť SOC zdroj (1 deň)

Odstrániť 5 zdrojov „aktuálneho SOC".

### 2.1 `core/state.py`

```python
def current_soc_pct(profile, *, now=None, mode=None) -> dict:
    """JEDINÝ zdroj aktuálneho SOC. real→realio_db, simulation→livesim trace.
    Vráti {soc_pct, source, ts, age_min}."""
```

Presunúť sem logiku z `vdt_live_advisor.get_current_soc_pct` + `vdt_state._get_current_soc_from_livesim_today`.

### 2.2 Prepojiť callerov

`vdt_state`, `vdt_live_advisor`, `livesim` (audit state), `rt_controller` (seed) → všetky volajú `core.state.current_soc_pct`. Odstrániť „SOC-UNIFY" override hacky (`vdt_state.py:617`).

**Akcept:** každý caller dostáva identický SOC ako predtým (charakterizačný test na 5 dní × 3 profily). Golden 5/5. DEV.

---

## KROK 3 — Prepojiť VDT cesty na `core.feasibility.gate` (1 deň)

Teraz, keď `gate` existuje a SOC je jeden, prepojiť reálnych callerov a vyhodiť duplicitné clipy.

### 3.1 `vdt_live_advisor.py` (r. 658–838)

Nahradiť blok 4 prekrývajúcich poistiek (`clip_extras_to_capacity` ×2 + `clip_extras_to_grid` + HARD-GUARD) **jedným** volaním:

```python
vdt_allowed, soc_path, rep = feasibility.gate(
    soc_start_kwh=current_soc_kwh,   # z core.state, JEDEN baseline (reálny)
    dam_batt_kw=dam96, vdt_batt_kw=vdt96,
    grid_export_kw=gexp, grid_import_kw=gimp, net_base_kw=net_base, ...)
```

Zmazať `#5a` (idealizovaný `soc_init+DAM` baseline) aj HARD-GUARD — `gate` z reálneho SOC ich nahrádza.

### 3.2 `livesim.py:1338`

`soc_feasible_vdt` → `core.feasibility.gate` (rovnaký vstup). Wrapper z KROKU 1 to už robí; tu len priame volanie + zmazať fallback raw+power-clip vetvu (r. 1360–1365), ktorá je nekonzistentná.

**Akcept:** VW_simulacia_2/3/4 na DEV majú odchýlku ≤ dnešok (ideálne lepšiu — žiadny baseline-mismatch). Golden 5/5. Porovnať `dev_kwh` súčet pred/po na 5 dňoch.

---

## KROK 4 — Oddeliť nomináciu od exekúcie (2–3 dni) ⭐ koreň falošných pokút

### 4.1 Immutable `Nomination`

V `plan_store` (alebo `core/schemas`) zaviesť per-deň/profil objekt:
```
Nomination = { grid_kwh[96], batt_kwh[96], source: dam|dam+vdt, frozen_at }
```
Vzniká raz (po VDT gate), uloží sa, **nikdy** sa neprepíše simuláciou.

### 4.2 `livesim.py:1505`

Dnes `tr["plan_batt_kw"] = sch.batt_kw` (= exekúcia). Rozdeliť:
- `tr["nomination_batt_kw"]` = z immutable `Nomination` (záväzok voči trhu),
- `tr["exec_batt_kw"]` = `sch.batt_kw` (čo batéria reálne spravila).

### 4.3 Odchýlka

`dev = realita − Nomination`, NIE `realita − feasibilný_plán`. Upraviť dekompozíciu odchýlky (`livesim.py:~1750–1790`) a ZCO pokutu, aby čítali `Nomination`.

**Akcept:** odchýlka prestane vznikať z toho, že „nominácia sa redefinovala podľa toho, čo batéria zvládla". Charakterizovať na dňoch, kde dnes vzniká falošná pokuta — musí klesnúť. Golden 5/5.

---

## KROK 5 — Zlúčiť RT audity + upratať (1–2 dni)

1. `audit_capacity` (#7) + `audit_action` (#8) → jedno volanie `core.feasibility.headroom`. `core/rt_audit.audit_rt_slot` a per-minútový audit v `livesim.py:1594` volajú ju. Callers: `rt_controller.py:670`, `core/rt_audit.py:119/159`, `vdt_live_advisor.py:1187/1399`, `auto_control.py:444`.
2. **Zmazať mŕtvu 3. VDT cestu** `vdt_extras.propose_greedy/propose_lp` + `_compute_soc_path` — najprv `grep` overiť, že ju nič živé nevolá.
3. Zmazať staré wrappery z KROKU 1.3, keď už nemajú callerov.

**Akcept:** RT správanie identické (charakterizačný test RT na 5 dní). Golden 5/5. Žiadny import mŕtveho kódu.

---

## Súhrn rizika a poradia

| Krok | Dopad | Riziko | Mení správanie? |
|------|-------|--------|-----------------|
| 0 Bezpečnostná sieť | — | žiadne | nie |
| 1 `core/feasibility.py` | vysoký | nízke | nie (parity) |
| 2 `core/state.py` SOC | vysoký | nízke | nie (parity) |
| 3 VDT cesty → gate | vysoký | stredné | áno, cielene (menej falošných clipov) |
| 4 Nominácia vs exekúcia | vysoký | stredné | áno, cielene (koreň pokút) |
| 5 RT audit merge + upratanie | stredný | nízke | nie (parity) |

Odhad: ~8–11 dní práce, každý krok samostatne nasaditeľný na DEV s vlastným akcept testom. `optimize_day`/`run_day_physical` + golden 5/5 nedotknuté celý čas.

## Prvý krok na zajtra

KROK 0 + KROK 1.1 (`battery_step` + `gate` ako extrakcia `soc_feasible_vdt`) + parity test. To je samostatne hodnotná, nulovo-riziková zmena, ktorá hneď ukáže, či zjednotená matematika reprodukuje dnešok 1:1.
