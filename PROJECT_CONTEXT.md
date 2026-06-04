# PROJECT_CONTEXT.md — FTV + batéria: plán D-1 a RT riadenie odchýlky

> Odovzdávací dokument. Slúži na to, aby ktorýkoľvek nástroj (Cowork, Claude Code, nový chat)
> okamžite pochopil architektúru, rozhodnutia a pravidlá projektu a nepokazil to, čo funguje.
> **Komunikácia: po slovensky.**

---

## 1. Čo to je

Lokálna Python WEB appka (Mac) na **plánovanie obchodnej pozície D-1** a **real-time riadenie
odchýlky** batériového úložiska pri fotovoltickej elektrárni:

- **FTV:** 99 kWp
- **Batéria:** 100 kW / 200 kWh
- **Trh:** český (OTE denný trh, ČEPS zúčtovacia cena odchýlky)
- **Stack:** FastAPI + HTML/Chart.js (server-rendered), Excel exporty, scikit-learn cenový model

Cieľ appky: **realistický simulátor** „ako sa to reálne správa" + nástroj na ladenie riadiaceho
signálu. Nie je to finančný poradca; všetky čísla sú s triezvymi predpokladmi (viď §5).

---

## 2. Pracovný adresár a workflow

**Adresár (Mac):**
`/Users/radoslavstompf/Documents/_FUERGY/01_Zakaznici/CZ/_Spolocne/Analyzy/Predikcia FTV/Aplikacia`

- venv: `.venv`, Python 3.13
- Spustenie: `python app.py` → http://127.0.0.1:8000
- Port sa dá zmeniť cez `PORT` env (`_PORT = os.environ.get("PORT","8000")`).

**Doterajší (chatový) workflow — toto chceme nahradiť priamym prístupom k disku:**
prezentácia `.py` → stiahnuť z `~/Downloads` → `mv ~/Downloads/X.py .` → Ctrl+C → `python app.py`.

> ⚠️ **OPAKOVANÝ PROBLÉM:** `app.py` a `livesim.py` sa menia **takmer vždy spolu**. Keď sa presunie
> len jeden, beží stará verzia a vznikajú „mátožné" chyby. Pri zmene jedného vždy over kompatibilitu druhého.

**Prechod na Cowork / Claude Code:** povoliť prístup len k tomuto jednému priečinku
(princíp najmenších oprávnení). API kľúče / citlivé dáta držať mimo. Appka ťahá dáta z webu
(OTE/ČEPS) → pozor na prompt-injection cez stiahnuté súbory; používať „plán najprv / potvrď akciu".

---

## 3. Súbory

**Jadro (mení sa najčastejšie):**

| Súbor | Úloha |
|---|---|
| `app.py` | FastAPI server, všetky routy, HTML render, live fetch, real-time dnešok, beh na pozadí |
| `livesim.py` | Engine simulátora `/livesim` (denné prehrávanie plánu + RT, log, trace) |
| `rt_controller.py` | **Fyzikálny model riadenia — JEDEN ZDROJ PRAVDY** (`run_day_physical`) |
| `combined_backtest.py` | Backtest plán+RT cez históriu; `DPARAMS`, `_dt15_for_day` |
| `optimizer.py` | `optimize_day` — LP/heuristika rozvrhu batérie na DT |
| `case_config.py` | Prípady (`load_case`/`save_case`), nastavenia ekonomiky a RT |
| `deviation_stats.py` | Štatistika odchýlky / profil |

**Podporné (KEEP, nemazať):**
`backfill.py`, `data_sources.py` (`ds`), `price_model.py`, `report.py`, `predict_tomorrow.py`,
`fetch_imbalance_history.py`, `selfcheck.py`.

---

## 4. Dáta (`out/`)

| Súbor | Obsah | Pozn. |
|---|---|---|
| `out/imbalance_minute.csv` | Minútové dáta: `time, sys_MW, aFRR_*, mFRR_*, mFRR5, ts15, isot_eur (reálny DT), zco_eur (reálna settled ZCO), date` | **Končí D−1** (backfill `end=today−1`, ZCO settled spätne). Pre dnešok/zajtra v ňom dáta NIE SÚ. |
| `out/price_train_2026.csv` | Tréningové dáta cenového modelu | **Len na Macu** (sandbox to nemá) |
| `out/price_model.joblib` | Natrénovaný cenový model | **Len na Macu** |
| `out/pv_calibration.json` | Mesačná kalibrácia FTV | |
| `out/deviation_profile.json` | Profil odchýlky | |
| `out/livesim_<case>[_<port>].csv` + `.meta.json` | Živé logy simulátora | režimy: `plan_d1`, `dt_15min` |

> **Dôsledok:** DT / signál / ZCO pre **dnešok** idú výhradne zo živého fetchu (OTE/ČEPS), nie z CSV.

---

## 5. Ekonomické nastavenie (OOS-overené — NEMENIŤ bez dôvodu)

Prípad **`realistic`:**
```
rt_kdis=1.5, rt_kchg=2.5   (mierka pásma; optimum ~5 ekvivalent)
plan_cycles=None, zco_bias_w=0, max_cycles=3, haircut=0.65, latencia=1
```
Výsledok: ~25,6 €/deň (train), ~23,5 €/deň (OOS).

**Dôležité chápanie:**
- `rt_kdis` / `rt_kchg` = **ŠÍRKA pásma**, NIE on/off. `0` = maximálne agresívne = stráca.
- RT vypnúť = `use_rt=False` (vtedy sa neoplatí).
- PpS (podporné služby) zatiaľ NEideme.

Zamknutie prípadu:
```bash
python -c "import case_config as cc; c=cc.load_case('realistic'); \
c.rt_kdis=1.5; c.rt_kchg=2.5; c.plan_cycles=None; cc.save_case(c)"
```

---

## 6. Fyzikálny model — `rt_controller.run_day_physical` (GOLDEN)

**JEDNA fyzická batéria.** D-1 plán = nominácia (na DT). RT odchýlka sa pridáva NA TEJ ISTEJ
batérii (zdieľané SOC, limit ±100 kW). Rozhodnutia sú **kauzálne** (žiadny look-ahead):

- `decide_reason`: `sys_MW` + aktivácie (aFRR/mFRR) + bežiaci priemer periódy + DT známa D-1.
- **ZCO sa NIKDY nepoužíva na rozhodnutie**, len na zúčtovanie.

Signatúra (zjednodušene):
```python
run_day_physical(g, plan_kw_arr, day_start, step_min,
                 band_dis, band_chg, w_sys, sys_orient, soc0,
                 dev_budget_kwh, dt_bias_k, strong_mw,
                 grid_kw_arr, grid_cap, return_trace)
```
- `w_sys` / `sys_orient` z prípadu cez `rtc.apply_case` (default `PROD_W_SYS=3.0`, `PROD_SYS_ORIENT=−1.0`).
- **`/livesim`, `/rt` aj backtest delegujú SEM** → riadiaci signál je zdieľaný a konzistentný.

> 🔒 **PRAVIDLO:** `run_day_physical` je „golden". Nepridávať doň stĺpce ani logiku narýchlo.
> Extra dáta (napr. reálna DT, VDT) sa mergujú až v `app.py`/`livesim.py`, nie do tejto funkcie.
> Pred zmenou jadra spustiť golden test (porovnať výstup pred/po na pár dňoch).

---

## 7. `/livesim` — štruktúra

Dva pomenované režimy (`MODES`):
- `plan_d1` → „Plán D-1 (hodinový)", base `realistic`, krok 60 min
- `dt_15min` → „Denný trh 15-min", base `realistic`, krok 15 min

Stránka: full-width, navigácia dní (◀ / dropdown / ▶ + „⏭ Najnovší deň"),
auto-obnova `<meta http-equiv="refresh" content="60">`.

**Grafy (v `_livesim_body`), poradie:**
1. **chMW** — MW signál + pásma (+ DT) → vysvetľuje rozhodnutia odchýlky
2. **chDT** — DT ceny: predikcia / realita (clearing) / VDT *(viď §11)*
3. **chPlan** — plán batérie + výkon na prahu (FTV+batéria) 1-min/15-min + odchýlka 1-min/15-min *(viď §11)*
4. **chRiadenie** — batéria SPOLU (fyzická) + RT odchýlka + SOC
5. **chF** — FTV výkon + orezanie
6. **chC** — kumulatívny zisk

**Polia v trace / dview:** `time, ts15, plan_batt_kw, rt_dir, rt_power_pct, soc_pct, soc_kwh,
ftv_kw, plan_curtail_kwh (UŽ v kW, max ~99 — žiadne ×60!), mw_sig, band_dis, band_chg,
dt_eur, dt_real_eur, vdt_eur, zco_eur, rt_reason, dt_rev_min, rt_rev_min,
cum_dt/cum_rt/cum_total, is_live`.

**Pomocníci na render:** `_js(x)` (NaN/inf → `'null'`), `_jsm(v, live)` (null ak nie je živá minúta),
`_lv` = maska `is_live`. Grafy reality (výkon na prahu, odchýlka, batéria SPOLU, SOC) idú cez `_jsm`
→ **končia pri „teraz"** (budúcnosť = null). Plán a DT ceny idú **celý deň**.

---

## 8. Real-time dnešok (kauzálne, provizórne, NEUKLADÁ sa)

- `app.py _rt_fetch()` → `today, now, isot (OTE day-ahead), est (odhad ZCO), sysd, afrr, act, vdt (VDT)`.
- `_livesim_live_minutes()` stavia **celodennú minútovú mriežku 00:00–23:59**: DT pre všetky periódy
  (day-ahead známy D-1), signál (sys/aktivácie) + `zco_eur` = odhad (ffill) len po „teraz".
  Celé v `try/except → None` (fallback historický).
- `_minute_all(live_minutes)` primerge dnešné minúty (prednosť, dedup `time`).
- `livesim.advance(live_minutes=...)`: dnešok **len zobrazenie, NEUKLADÁ** do CSV, NEfolduje do `done`.
  Plán z **celého dňa** (`mn_day_full`), fyzika beží len po „teraz" (`mn_day = filter ≤ now`).
- `today_trace` rozšírený na celý deň: živé minúty (RT) po posledný signál; gap posledný-signál → reálny-now
  = plán beží, RT=0, `mw_sig=NaN`, `is_live=1` (zdedí SOC, projektuje SOC); po reálnom now = projekcia `is_live=0`.
- „Hodnoty teraz" + SOC čítajú **poslednú `is_live=1` minútu** (reálny čas).
- Badge: „⚠ Dnešok PROVIZÓRNY (odhad ZCO)". Info riadok hlási lag voči reálnemu času
  (ČEPS publikuje s oneskorením).

---

## 9. Beh na pozadí (`app.py __main__`)

- Vlákno `_livesim_bg_loop` (gate `LIVESIM_BG!=0`, interval `LIVESIM_BG_SEC`, default 60 s):
  každých 60 s `_livesim_bg_tick()` (advance naposledy zvoleného režimu z UI stavu),
  raz/hod `backfill.backfill_all`.
- `_LIVESIM_LOCK = threading.Lock()` serializuje zápis (vlákno aj prehliadač obaľujú `advance`).
- Beží **aj keď je prehliadač zatvorený**, kým beží `python app.py`.

---

## 10. CESTA B — plán = predikované ceny + nastavenia FORMULÁRA (kľúčové rozhodnutie)

**Rozhodnutie užívateľa:** simulátor má ukázať „ako sa to reálne správa" pri nominácii —
clearing ešte nevieš, ideš podľa **predikcie**. Preto livesim plán pre **dnešok**:

1. **Predikované ceny** (rovnaký model ako generátor `/plan`):
   `app.py _livesim_pred_dt(today)` replikuje predikciu (`ds.fetch_pv_forecast` + `_isot_history`
   + ctx + `_model().predict` → `pred_isot` hodinové → mapa `ts15→cena`, repeat 4×).
   `_livesim_live_minutes` použije pred ceny pre `isot_eur`; **fallback** na reálny day-ahead, ak predikcia zlyhá.
2. **Nastavenia z FORMULÁRA plánu** (nie z prípadu!):
   `livesim._plan_override(pp)` mapuje formulár → `optimize_day` (`soc_min/max_pct`, `terminal_soc_pct`,
   `min_spread_eur`, `allow_curtail/allow_grid_charge/block_neg_import`, `batt/grid/eff/cycle_cost`).
   `_day_plan(..., plan_params=...)` to aplikuje. **`soc_init` ostáva CARRIED SOC** (spojitá batéria),
   nie formulárová hodnota — viď §12.
   - `app.py` posiela `plan_params = _ui_load("plan", DEF)` do `advance` (v `livesim_get` aj v bg ticku).
3. **RT signál podľa `/rt` poradcu:** `app.py _livesim_rt_params(cfg)` číta posuvníky z UI stavu „rt"
   (`kdis/kchg/dtk/rboost`), fallback prípad. Posunie do `advance` → `_run_physical_day` (override
   pásiem ×kdis/kchg, `dt_bias_k`).

> **Historické dni** ostávajú na **reálnych** cenách (validácia — vieš, čo sa naozaj stalo).
> Iba **dnešok** ide na predikovaných (čo by si nominoval). Toto je principiálne správne rozdelenie.
> Backtest (`combined_backtest`) je samostatný (reálne ceny, nastavenia prípadu) — slúži na ladenie RT.

---

## 11. Grafy chDT + chPlan (rozdelený pôvodný „Obchod")

Pôvodný graf „Obchod (denný trh)" rozdelený na DVA:

**chDT — DT ceny:**
- DT **predikcia** (čo sa nominovalo, cesta B) — len pre dnešok
- DT **realita** (clearing, `dt_real_eur`)
- **VDT** (`vdt_eur`, vnútrodenný)
- Historický deň: len „DT realita" + VDT (predikcia nedáva zmysel, deň je uzavretý).

**chPlan — Plán a realita výkonu (kW):**
- **Plán batérie** (nominácia, `plan_batt_kw`)
- **Výkon na prahu zákazníka = FTV + batéria SPOLU** (`ftv_kw + act_batt_kw`), **1-min aj 15-min**
- **Výsledná odchýlka** (`act_batt − plan_batt` = RT) v kW, **1-min aj 15-min**
- Realita výkonu + odchýlka idú **len po „teraz"** (`is_live`), 15-min = `groupby ts15 mean` broadcastnutý na 1-min os.

**Dátové cesty (app.py):**
- `_livesim_live_minutes`: pridáva `dt_real_eur` (= reálny day-ahead `dtmap`) a `vdt_eur` (z VDT fetchu).
- `livesim.advance`: mapuje `dt_real_eur` + `vdt_eur` z `mn_day_full` do `tr` aj do budúcej projekcie (`fut`).
- `_livesim_body`: stavia série `THR1/THR15` (výkon na prahu), `DEV1/DEV15` (odchýlka), `DTREAL`, `VDT`;
  `_is_today = (view_day == prov_date)` rozhoduje o popiskoch DT.

> **Pozn.:** Reálne meranie FTV zatiaľ NIE je napojené → FTV realita = FTV plán, takže výsledná
> odchýlka na prahu = odchýlka batérie. Po napojení reálneho FTV pribudne FTV člen automaticky.

---

## 12. Auto-reset logu pri zmene nastavení (`settings_sig`)

**Problém, ktorý to rieši:** starý log počítaný so starými nastaveniami (napr. terminal SOC 50 %)
→ dni končili na 50 % → dnešok zdedil 50 % (carried SOC) → ráno zbytočne vybíjal.

**Riešenie:** `advance` počíta `sig` (JSON z `_plan_override(plan_params)` + `rt_params` zaokrúhlené
+ `d1_step` + `batt` + `curtail_case`), uloží do `meta["settings_sig"]`. Pri načítaní, ak sa líši
→ `meta=None` → **log sa prepočíta odznova**. Užívateľ už nemusí mazať log ručne.

Vďaka tomu pri `terminal_soc=5 %` carried SOC konverguje k ~5 % a dnešok prirodzene štartuje nízko
→ plán livesim sedí s tabuľkou generátora.

**Transparentnosť:** pod info riadkom je modrý pásik
„Nastavenia plánu (z formulára): SOC koniec … · min. rozdiel … · orezanie … · batéria … |
RT signál (poradca): kdis … kchg … dtk …" — z `r["plan_used"]` (`_po`) a `r["rt_used"]` (`rt_params`).
Slúži na overenie, či livesim berie užívateľove hodnoty.

> Ak pásik ukazuje „—" alebo defaulty: formulár plánu si neuložil hodnoty →
> na stránke **Plán D-1** treba kliknúť **Generovať** (to uloží do `_ui_load("plan")`).

---

## 13. Čo je HOTOVÉ

1. Real-time dnešok na celý deň (DT plán celý deň, fyzika po teraz, RT gap dopočet).
2. Beh na pozadí (vlákno + zámok + hodinový backfill).
3. Cesta B: plán na predikovaných cenách + nastaveniach formulára; RT z `/rt` poradcu.
4. `settings_sig` auto-reset logu.
5. Rozdelenie grafu na **chDT** (predikcia/realita/VDT) a **chPlan** (plán + výkon na prahu 1/15-min
   + odchýlka 1/15-min).
6. Rôzne opravy: „Hodnoty teraz" z reálneho času, full-day DT pre plán, MW graf NaN→null,
   Riadenie graf končí pri teraz, jednotky orezania (kW), curtail toggle persistencia,
   SOC bunka = graf, lag indikátor.

---

## 14. Čo je NAPLÁNOVANÉ (ďalšie kroky)

- **Režim „Iba odchýlka (bez plánu)"** = `plan_kw_arr=0`, plná batéria pre RT (malá zmena, fyzika to vie).
- **Reálne meranie FTV** → FTV člen odchýlky (teraz 0).
- **Reálne SOC z BMS**.
- **Validácia `/livesim` vs backtest** po nazbieraní živých dní.
- **Multi-krajina:** samostatné aplikácie per krajina (jeden priečinok = jedna appka = jeden projekt).
  Držať **zdieľané jadro** (`optimizer`, `rt_controller`, livesim engine), meniť len špecifiká krajiny:
  trhové API (OTE/ČEPS vs. iný operátor), cenový model, sviatky, poplatky.

---

## 15. Spôsob práce / pravidlá (TÓN A PROCES)

- **Triezve, poctivé očakávania:** haircut 0.65, žiadny look-ahead v rozhodnutí, OOS-overené čísla.
  Zdôrazňovať kauzalitu. Nie je to finančný poradca.
- **Pred zmenou jadra** (`run_day_physical`, `optimize_day`) → golden test (pred/po na pár dňoch).
- **Každá zmena kódu:** `str_replace` → `ast.parse` (syntax) → import/mock test → overiť na reálnych
  CSV (`out/imbalance_minute.csv`) alebo mocku → až potom doručiť.
- **Live fetch** (predikcia, VDT, reálna DT dneška) sa v sandboxe NEDÁ otestovať (no network/model);
  preto reuse overených funkcií `/plan` a `/rt`, defenzívne s fallbackom.
- **`app.py` a `livesim.py` meniť spolu** (viď §2).
- Užívateľ posiela výpisy/screenshoty z Macu; Claude kód na Macu nespúšťa.

---

## 16. Rýchly „smoke test" po nasadení

```bash
# 1) syntaxe
python -c "import ast; [ast.parse(open(f).read()) for f in ['app.py','livesim.py']]; print('OK')"

# 2) čo livesim reálne použije ako nastavenia plánu
python -c "import app; print(app._ui_load('plan', {}))"
#   → očakávané: soc_init/terminal_soc podľa formulára (napr. 5/5), nie default 50/50

# 3) štart
python app.py   # → http://127.0.0.1:8000/livesim
```
Na stránke `/livesim` skontroluj modrý pásik s nastaveniami (§12) a dva nové grafy chDT + chPlan (§11).
