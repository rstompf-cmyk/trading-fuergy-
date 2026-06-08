# Architecture Refactor Plan

**Stav (2026-06-08):** Fáza A KOMPLET · Fáza B 3/4 KOMPLET · Fáza D 1/2 KOMPLET · Fáza C TODO
**Branch:** refactor-v2 (rovnaký ako live)
**Ciele:** eliminovať opakované chyby cez schema-driven kontrakty, single source of truth, per-profile sandbox a automatickú verifikáciu.

## ━━━ Implementation Status ━━━

| Phase | Status | Tests | Notes |
|---|---|---|---|
| **A.1 ProfileConfig** | ✓ DONE (3c82b6a) | 7/7 | 8 profilov validovaných |
| **A.2 StoredPlan + PlanSlot** | ✓ DONE (7114b17) | 14/14 | 712 reálnych plánov |
| **A.3 VDTTrade + Bug UU gate** | ✓ DONE (388f601) | 12/12 | gate odhalil 63 residual trades |
| **A.4 Integration smoke** | ✓ DONE | — | app.py imports OK |
| **B.1 FS migration** | 🟡 PLANNED (tools/migrate_to_sandbox.py) | dry-run | execute=False default, needs downtime |
| **B.2 Pure functions** | ❌ TODO | — | risky, needs golden tests first |
| **B.3 Audit + sanity tools** | ✓ DONE (f177e7b) | manual | objavil 63 Bug UU residual entries |
| **B.4 Audit log** | ✓ DONE (84549a2) | manual | wired do VDT gate + profile_save + set_active |
| **C.1-C.3 SQLite consolidation** | ❌ TODO | — | livesim CSV → SQLite (big change) |
| **D.1 Golden tests** | ✓ DONE | 5/5 | optimize_day snapshot |
| **D.2 GitHub Actions CI** | ✓ DONE | — | runs on push/PR refactor-v2/dev/main |

**Total passing: 38/38 testov** (33 schema + 5 golden)

---

## Prečo

Posledné ~30 bugov (G, I, J, K, L, M, N, O, P, Q, R, S, T, U, V, W, X, Z, AA, BB, CC, DD–VV)
ukázalo 6 systematických vzorcov:

1. **Implicit contracts cez súbory** — modul A zapíše CSV, modul B číta s vlastnou interpretáciou (Bug 441 — VDT trades bez `profile` column)
2. **Toggle sémantika nie je formálne definovaná** — `trade_ftv:false` znamenalo "neuvádzať v obchode" alebo "fyzicky zakázať"? (Bug TT)
3. **State accumulation bez idempotencie** — livesim CSV append, drift z chýb sa hromadí (Bug 525 reset loop)
4. **Shared mutable state bez locks** — `_active_<port>.json`, race conditions (Bug 230)
5. **Per-profile isolation post-hoc** — paper_trades CSV s `profile` column, ale stále shared (Bug 441)
6. **Žiadny automatický verifikačný layer** — všetko sa overuje vizuálne na dashboarde

## Princípy

Pravidlá ktorými sa budeme riadiť pri každej zmene:

1. **Single source of truth** pre každú entitu. Jedna pravda žije v jednom module.
2. **Validate at boundary** — pri každom čítaní z disku aj zápise. Žiadne raw CSV manipulácie.
3. **Toggle = input filter** — toggle ovplyvňuje vstup do funkcie, nie post-hoc query výsledku.
4. **Pure functions** kde sa dá — optimizer, settlement, baseline nemajú side-effects.
5. **Per-profile FS sandbox** — všetky súbory profilu pod `out/profiles/<name>/`.
6. **Test as documentation** — golden tests pre reprezentatívne profily.

---

## Fáza A — Schema layer (1-2 dni, **low risk**)

Cieľ: každá kritická dátová štruktúra má Pydantic model. Validate pri každom read/write.

### A.1 — `ProfileConfig` Pydantic model

**Súbor:** `core/schemas/profile.py` (nový)

```python
class JointLPConfig(BaseModel):
    enabled: bool = False
    trade_batt: bool = True
    trade_ftv: bool = True
    trade_load: bool = True
    use_vdt: bool = True
    optimize_distribution: bool = False

class PlanConfig(BaseModel):
    lat: float
    lon: float
    kwp: float = Field(ge=0)
    batt_kw: float = Field(ge=0)
    batt_kwh: float = Field(ge=0)
    eff_c: float = Field(ge=0.5, le=1.0)
    eff_d: float = Field(ge=0.5, le=1.0)
    soc_min: float = Field(ge=0, le=100)
    soc_max: float = Field(ge=0, le=100)
    joint_lp: JointLPConfig = JointLPConfig()
    # ... všetky polia z plan/

class DistributionConfig(BaseModel):
    enabled: bool = False
    tou_mode: Literal["flat", "tou"] = "tou"
    # ... atď.

class ProfileConfig(BaseModel):
    name: str
    mode: Literal["simulation", "real"] = "simulation"
    plan: PlanConfig
    distribution: DistributionConfig = DistributionConfig()
    # ...
```

**Deliverable:**
- `core/schemas/profile.py` s úplnou definíciou
- `profiles.load_profile()` validuje pri čítaní, vracia `ProfileConfig` objekt aj dict (pre backward-compat)
- `profiles.save_profile()` prijme ProfileConfig, validuje pred zápisom
- Migration: profil ktorý prejde `model_validate(json, mode='lax')` aj keď chýbajú nové fields (Pydantic v2 defaults)
- Smoke test: load všetkých 10 profilov + dump-load round-trip

**Riziko:** žiadne — backward-compat zachovaná, len pridáva validáciu.

### A.2 — `PlanConfig` schema

**Súbor:** `core/schemas/plan.py`

```python
class PlanSlot(BaseModel):
    slot: int = Field(ge=0, le=95)
    pv_kwh: float
    load_kwh: float
    batt_kw: float
    grid_kwh: float

class StoredPlan(BaseModel):
    date: str  # YYYY-MM-DD
    profile: str
    kind: Literal["plan", "dentrh"]
    step_min: Literal[15, 60]
    schedule: list[PlanSlot]
    metadata: dict
```

**Deliverable:**
- `plan_store.load_plan_safe()` vracia validated `StoredPlan`
- Conflict detection: ak plan má `kind=plan` ale `step_min=15` → raise
- Smoke test: load všetkých plánov v `plan_store/`

### A.3 — `VDTTrade` schema + gate

**Súbor:** `core/schemas/vdt.py`

```python
class VDTTrade(BaseModel):
    ts: datetime
    profile: str
    slot: str  # "HH:MM-HH:MM"
    action: Literal["charge", "discharge", "idle", "curtail_ftv", "load_cover"]
    kw: float
    kwh: float
    price_predicted_eur: float
    soc_before_pct: float
    soc_after_pct: float
```

**Deliverable:**
- `TradeStore.append(trade: VDTTrade)` — ak `profile.joint_lp.use_vdt=False`, raise (= Bug UU vstavaný)
- `TradeStore.read(profile, day)` vracia iba pre daný profil
- Migration: existujúce CSV preparsované cez Pydantic, broken rows do quarantine

### A.4 — Smoke test directory

**Súbor:** `tests/smoke/test_profiles.py`, `tests/smoke/test_simulacia_coop.py`, atď.

```python
def test_simulacia_coop_day():
    """Run lsim.advance for 2026-06-07 and check expected ranges."""
    r = lsim.advance("plan_d1", "2026-06-07", profile="Simulacia_Coop")
    assert -500 < r["cum_total"] < 500  # rozumné rozpätie
    assert all_zero(get_vdt_trades("Simulacia_Coop", "2026-06-07"))  # use_vdt:false
```

**Deliverable:**
- `pytest tests/smoke/ -v` beží lokálne
- Per-profil 1 test ktorý overí: žiadne VDT ak use_vdt:false, profit v rozsahu, žiadne nan v CSV

**Trvanie celej Fázy A:** 1-2 dni práce, žiadny visible UI change, ale **eliminuje 40-60% budúcich bugov** typu UU, TT, 441.

---

## Fáza B — Per-profile sandbox + pure functions (3-5 dní, medium risk)

Cieľ: každý profil žije vo vlastnom adresári. Žiadne shared CSV files. Optimizer funkcie sú pure.

### B.1 — FS layout migration

**Pred:**
```
out/profiles/Simulacia_Coop.json
out/sk/plans/Simulacia_Coop/2026-06-08_60min_plan.json
out/sk/livesim_plan_d1_8000.csv          # shared port
out/sk/vdt_paper_trades.csv              # shared, filter na profile column
out/sk/auto_control_log.csv              # shared
```

**Po:**
```
out/profiles/Simulacia_Coop/
    config.json                          # ProfileConfig validated
    plans/2026-06-08_60min.json
    livesim/plan_d1.csv                  # iba pre tento profil
    livesim/plan_d1.meta.json
    vdt_paper_trades.csv                 # iba pre tento profil
    auto_control_log.csv
    vdt_advisor_cache.json
    mpc_cache.json
out/_shared/                             # market-wide
    cz/price_train_2026.csv
    sk/historian_*.csv
    sk/imbalance_minute.csv
```

**Deliverable:**
- `tools/migrate_profiles_to_sandbox.py` — one-shot migration script (s backupom)
- Update všetkých `_csv_path()` helperov na nové cesty
- Per-profile lock files (žiadne race conditions)

**Riziko:** stredné — vyžaduje migration. Backup nutný.

### B.2 — Optimizer funkcie ako pure

**Cieľ:** `optimize_joint_day()`, `optimize_day()`, `_run_physical_day()` neberú nič z disku ani globals. Vstup = config dict, output = result dict.

**Deliverable:**
- `joint_lp.optimize(plan_cfg, day_data) -> JointLPResult` — pure function
- Žiadny `import xyz; xyz.read_csv()` vnútri
- Wrapper `joint_lp_integration.optimize_for_profile(profile, date)` ktorý orchestrá­ ZDROJOV → volá pure optimizer → vracia výsledok

### B.3 — Audit & sanity tooly

**Súbor:** `tools/audit_profile.py`, `tools/sanity_check.py`

```bash
$ python3 -m tools.audit_profile Simulacia_Coop
=== Simulacia_Coop ===
Mode: simulation · Market: SK · BG enabled: True
Config:
  Joint LP: ✓ (trade_batt:T, trade_ftv:F, use_vdt:F, optimize_dist:T)
  kWp: 1000 · Batt: 200 kW / 400 kWh · SOC: 5-100%
Plans: 30 (2026-05-10 → 2026-06-09)
Livesim:
  CSV: 14283 rows · last_min: 2026-06-08 10:23
  Settings sig: ABC123 (stable)
VDT trades: 0 (correctly empty — use_vdt:false ✓)
Cache:
  VDT advisor: None ✓
  MPC tick: 2026-06-08 10:23 ✓
Audit log (last 24h): 8 events
```

```bash
$ python3 -m tools.sanity_check
✓ Bat_D-1_bat_2: clean
✓ Elpremont_PLan: clean
⚠ Simulacia_Coop: use_vdt:false ALE 24 VDT trades v CSV (pre-Bug UU)
✗ Trakany_real: joint_mpc_enabled:false ALE mpc_cache existuje (stale?)
```

**Deliverable:** dva nové CLI nástroje + nightly schedule cez `mcp__scheduled-tasks__create_scheduled_task` (sanity report do emailu).

### B.4 — Audit log / event sourcing

**Súbor:** `out/audit/<date>.jsonl`

Každý write event ide do JSONL:
```json
{"ts":"2026-06-08T10:23:00","actor":"bg_scheduler","action":"livesim_advance","profile":"Simulacia_Coop","minutes_appended":1,"settings_sig":"ABC123"}
{"ts":"2026-06-08T10:23:01","actor":"bg_scheduler","action":"vdt_trade_added","profile":"Trakany_real","trade":{...}}
{"ts":"2026-06-08T10:24:00","actor":"http_user","action":"profile_apply","from":"Trakany_real","to":"Simulacia_Coop"}
```

**Použitie:** replay debugger, post-mortem analýza, audit pre real-money trades.

**Trvanie celej Fázy B:** 3-5 dní. **Eliminuje 30% bugov** typu race conditions, profile leaks, stale cache.

---

## Fáza C — SQLite konsolidácia (1-2 týždne, higher risk)

Cieľ: nahradiť všetky CSV/JSON ktoré sú accumulačné (livesim minutes, VDT trades, audit) za SQLite tabuľky s indexami a constraints. CSV ostávajú ako exporty.

### C.1 — Livesim minutes do SQLite

**Pred:** `out/profiles/<name>/livesim/plan_d1.csv` (append-only)
**Po:** `out/_shared/livesim.db` s tabuľkou `livesim_minute(profile, time, case, ...)` + index na `(profile, time)`

**Výhody:**
- UPSERT idempotentný (no drift z duplikátov)
- Concurrent reads/writes safe (SQLite WAL)
- Query "last 24h" je rýchle bez load celého CSV
- Schema migration verzionovaný cez Alembic (už máme z Fázy 1)

### C.2 — VDT trades + auto_control log do SQLite

Rovnaký pattern, jedna tabuľka per typ, indexes per (profile, ts).

### C.3 — Realio už v SQLite ✓

Z Bug 336-349 už máme. Šablóna pre ostatné.

**Trvanie:** 1-2 týždne s testovaním na živých dátach. **High risk** — vyžaduje migration starých CSV.

---

## Fáza D — Testing + CI (1 týždeň)

Cieľ: žiadny commit bez automatickej verifikácie.

### D.1 — Golden tests

`tests/golden/` — fixed input data + expected output ranges pre 5 reprezentatívnych profilov × 7 dní.

### D.2 — Pre-commit hook

`.git/hooks/pre-commit` — spustí `pytest tests/smoke/ -q` pred každým commit. Žiadny push s broken testom.

### D.3 — GitHub Actions

`.github/workflows/ci.yml` — pri každom push na refactor-v2 spustí full test suite + sanity check.

**Trvanie:** 1 týždeň. **Posledná línia obrany** — všetky vyššie fázy už znížili bugy ale toto zachytí kraje.

---

## Plán nasadenia

| Fáza | Trvanie | Risk | Visible UI change |
|---|---|---|---|
| **A** — Schema layer | 1-2 dni | **Low** | Žiadny |
| **B** — Sandbox + pure | 3-5 dní | Medium | Žiadny |
| **C** — SQLite | 1-2 týždne | **High** | Žiadny |
| **D** — Testing + CI | 1 týždeň | Low | Žiadny |
| **Spolu** | ~3-4 týždne | | |

**Žiadny user-visible feature change.** Iba "behind the scenes" kvalita.

---

## Rollback stratégia

- Každá fáza je **na samostatnom branch** (`refactor-v2-faza-A`, atď.)
- Pre-merge na `refactor-v2`: musia prejsť všetky existujúce smoke testy
- Backup `out/_pre_refactor_<phase>_<ts>/` automaticky pri každej migration

---

## Začneme s Fázou A.1 (ProfileConfig Pydantic)?

To je **najmenší risk** + **najväčšia investícia** (každý budúci bug ako UU/TT/441 by sa nedal vyrobiť). Po dohode začnem implementáciu.
