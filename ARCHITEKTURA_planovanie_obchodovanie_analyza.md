# Architektonická analýza: plánovanie → VDT → RT → realita

*Vypracované 2026-06-28. Read-only analýza, žiadne zmeny v kóde.*

## Zhrnutie pre netrpezlivých

Tvoj pocit, že posledné týždne sú „len plátanie", je technicky presný. **Nie je to séria nesúvisiacich bugov — je to jeden architektonický defekt, ktorý sa opakovane prejavuje.**

Konkrétne: feasibilita batérie (SOC / grid prípojka / výkon) je rozsypaná do **9 nezávislých implementácií**, ktoré čítajú **5 rôznych zdrojov „aktuálneho SOC"** a počítajú tú istú fyziku **3 rôznymi zápismi**. Vždy keď vznikla falošná odchýlka (pokuta), riešilo sa to pridaním ďalšieho clipu na ďalšom mieste namiesto opravy zdroja. Preto „VDT dokupuje a nedržíme to" — VDT prejde jednou poistkou (z jedného SOC baseline), ale iná vrstva (z iného SOC baseline) ho už neorezala alebo orezala inak.

Riešenie nie je ďalší clip. Riešenie je **jedna feasibility vrstva (single source of truth)** a **oddelenie nominácie od exekúcie**.

---

## 1. Mapa toku a zdroje pravdy

**Tok:** D-1 plán (`optimizer.optimize_day`) → `plan_store` (immutable, kind `dentrh`/`plan`) → VDT advisor (`get_live_recommendation`) → paper trades CSV → `livesim.advance` (aplikuje VDT do `sch.batt_kw`) → `rt_controller.run_day_physical` (RT + audity + grid clip) → `effect_db` trace → odchýlka voči nominácii → ZCO pokuta. Trace SOC sa číta späť ako seed ďalšieho dňa.

**Zdroje pravdy — a v čom je problém:**

- **Nominácia (záväzok voči trhu):** DAM nominácia žije v `plan_store`, ALE efektívna nominácia, voči ktorej sa počíta pokuta, vzniká až v `livesim.py:1505` ako `tr["plan_batt_kw"] = sch.batt_kw` — **po** VDT clipe. Nominácia teda **nie je dáta, je to vedľajší produkt simulácie.** To je hlavný seam.
- **Aktuálny SOC: päť zdrojov.** (a) `vdt_state` z trace, (b) `vdt_state` meta `today_soc_pct`, (c) `vdt_live_advisor.get_current_soc_pct` z realio_db/trace, (d) plán projekcia `_integrate_soc_path`, (e) running `soc` v `rt_controller.run_day_physical`. Komentáre „SOC-UNIFY" sú pokusy ich dodatočne zladiť — dôkaz, že zladené nie sú.
- **Plán batérie:** `sch.batt_kw` sa za behu prepisuje **trikrát** v jednej funkcii — čistý DAM (`livesim.py:1322`), DAM+VDT feasible (`:1357`), finál (`:1505`).

---

## 2. Inventár feasibility poistiek (jadro „plátania")

Deväť nezávislých SOC/grid/výkon implementácií:

| # | Miesto | Čo rieši | SOC štart |
|---|--------|----------|-----------|
| 1 | `optimizer.optimize_day` | SOC + grid ako LP constraint + post-LP forward clip | soc_init (00:00) |
| 2 | `vdt_optimizer.optimize_vdt_day` (LP) | SOC band + soc-neutral + hard-guard | current SOC |
| 3 | `vdt_pair_matcher.match_pairs` | SOC headroom medzi pármi + výkon | soc0 |
| 4 | `vdt_extras._compute_soc_path` | vlastná SOC trajektória (3. generačná cesta) | start_soc |
| 5a | `clip_extras_to_capacity` (advisor) | oreže VDT na SOC band | **soc_init + plný DAM (00:00)** |
| 5b | tá istá funkcia, 2. volanie | to isté | **reálny SOC (now)** |
| — | `clip_extras_to_grid` (advisor) | oreže VDT na grid prípojku | DAM grid pozícia |
| — | HARD-GUARD (advisor) | výkon + SOC na 1. trade | reálny SOC (now) |
| 6 | `soc_feasible_vdt` (livesim) | SOC + grid forward clip DAM+VDT | soc (00:00 sim) |
| 7 | `audit_capacity` (RT) | headroom z trajektórie | running SOC |
| 8 | `audit_action` (RT, 96-slot binárne hľadanie) | max RT zásah | current/start SOC |
| 9 | GRID-LIMIT clip v RT (**2×**) | grid prípojka pred aj po RT | per-minúta |

**Najhoršie duplicity:**

- **#5a vs #5b — dva clipy z dvoch rôznych SOC baseline-ov za sebou v jednej funkcii.** Najprv z idealizovaného `soc_init + DAM`, potom z reálneho SOC. Ak sa rozídu (RT drift, realizované VDT), výsledok závisí od poradia a oba sú „správne" voči svojmu baseline, ale navzájom nekonzistentné. **Toto je presne mechanizmus „nedržíme to".**
- **#5 vs #6 — redundancia.** Advisor (`clip_extras_to_capacity`) a livesim (`soc_feasible_vdt`) robia to isté (orežú DAM+VDT na SOC band), ale **rozdielnou matematikou**. Dve odpovede na tú istú otázku.
- **#7 vs #8 — dva RT audity s rôznou matematikou** (O(1) headroom vs O(96) binárne hľadanie). Kód si ich sám stavia proti sebe.
- **Tri zápisy tej istej fyziky:** `soc += c·eff_c − d/eff_d` vs `soc −= target·dt/eff_d` vs `delta = −v/eff_d`. Každá úprava sa musí synchronizovať v 6 súboroch — čo sa nedeje.

---

## 3. Seamy / zdroje „nedržíme to"

- **Nominácia sa redefinuje podľa toho, čo simulácia dokáže dodať** (`livesim.py:1493` VDT-CONSISTENT). To je logicky obrátené: v reálnom nasadení trh drží pôvodnú nomináciu, nie tú, ktorú batéria nakoniec zvládla.
- **VDT je v stored pláne (paper trades CSV) aj v sim trace (closed-price LP), cez rôzne zdroje** — môžu sa rozísť.
- **Falošná odchýlka vzniká vždy, keď baseline pre clip ≠ baseline pre nomináciu.** VDT „povolené" advisorom (z `soc_init+DAM`) môže byť v livesime znova orezané (z reálneho sim SOC) → realita ≠ nominácia.

---

## 4. Vstup reality

- **SOC drift (RT/obchody):** ad-hoc, **3 vrstvy** na ten istý problém (SOC-UNIFY override + clip #5b + HARD-GUARD).
- **FTV ≠ predikcia:** relatívne **čisté** — fyzikálna rovnováha v engine (`livesim.py:1554`), dekompozícia odchýlky. OK, patrí to tam.
- **VDT ceny:** **čisté** — rolling MPC re-beh každých 15 min s fresh orderbookom. Správny design.
- **Grid < batt výkon:** ad-hoc, **4–5 miest** clipuje na grid (advisor, soc_feasible, plan_grid, 2× RT).

---

## 5. Štrukturálne problémy (koreň)

1. **Päť zdrojov pravdy pre SOC.** Žiadny owner. „SOC-UNIFY" je symptóm, nie liek.
2. **Feasibilita v 9 miestach, 5 SOC enginov, 3 zápisy fyziky.** Doslova „vrstva na vrstve".
3. **Plán / nominácia / realita nie sú oddelené.** Nominácia odvodená z `sch.batt_kw` počas simulácie → „čo sme sľúbili" a „čo sme zvládli" zdieľajú tú istú premennú → odchýlku nemožno čisto definovať.
4. **Tri paradigmy generovania VDT** (LP, greedy páry, starý `vdt_extras`), každá s vlastnou feasibilitou.
5. **Baseline-mismatch ako trieda bugov.** Takmer každý datovaný komentár (06-11 až 06-26) opravuje „clip štartoval z iného SOC než graf/realita". Jeden defekt, opakovane.
6. **Konvencie znamienka/účinnosti duplikované a nesynchronizované** v 6 súboroch.

---

## 6. Návrh čistej architektúry

Cieľ: **jedna feasibility vrstva + jasné oddelenie 4 vrstiev.** Ekonomika (`realistic`, `rt_kdis/rt_kchg`, breakeven, soc-neutral, min_spread) sa **nemení** — len sa okolo nej reorganizuje štruktúra. `optimize_day` a `run_day_physical` (golden) ostávajú nedotknuté.

**Cieľové vrstvy:**

- **(i) Plán / Nominácia — immutable dáta.** Explicitný `Nomination` objekt: per-slot grid+batt kWh, raz zafixovaný, **nikdy** prepísaný simuláciou. Realita ho číta, nemodifikuje.
- **(ii) Feasibility / Audit — JEDEN modul `core/feasibility.py`.** Jedna funkcia: vstup `soc_start, dam[96], vdt[96], eff, SOC band, grid, net_base` → výstup `(feasible_vdt[96], soc_path[97], report)`. Nahradí #2, #3, #4, #5a, #5b, #6 a dá headroom pre #7/#8. **Pravidlo „VDT musí prejsť auditom SOC trajektórie" sa stane jediným vstupným bodom** — engine navrhne, táto vrstva schváli. Fyzika definovaná **raz** tu.
- **(iii) Exekúcia / Engine.** `optimize_day` + `run_day_physical` (golden) ostávajú. RT audity #7/#8 sa zredukujú na jedno volanie `feasibility.headroom()`.
- **(iv) Rekonciliácia odchýlky.** `dev = realita − Nomination(i)`, kde Nomination je immutable, nie `sch.batt_kw`. Pokuta sa počíta len tu.

**Ako by VDT správne fungoval:** advisor (LP/páry) navrhne kandidátov → `feasibility.gate(soc_start_real, dam, candidates)` ich orežе na SOC+grid+výkon **naraz, z jedného reálneho SOC** → výstup je jediná pravda pre nomináciu aj sim. Žiadny druhý clip, žiadny HARD-GUARD, žiadny #5b.

**Migrácia (inkrementálne, DEV first, golden-protected):**

1. Napísať `core/feasibility.py` ako **čistú extrakciu** existujúcej matematiky (`vdt_soc_feasible` + `vdt_capacity_guard`) — bez zmeny správania, kryté golden testom (starý vstup = starý výstup).
2. Prepojiť `vdt_live_advisor` naň (namiesto #5a+#5b+grid); overiť na DEV (VW_simulacia_2/3/4 = identická/lepšia odchýlka).
3. Prepojiť `livesim` (#6) na ten istý modul; zmazať duplicitnú matematiku.
4. Zaviesť immutable `Nomination`; oddeliť `plan_batt_kw` (nominácia) od `sch.batt_kw` (exekúcia).
5. Zlúčiť RT audity #7/#8. Nakoniec zmazať mŕtvy `vdt_extras`.

---

## 7. Prioritizované odporúčania (dopad / riziko)

1. **[Vysoký / nízke] Extrahovať `core/feasibility.py`** ako čistú zlúčeninu `soc_feasible_vdt` + `clip_extras_to_capacity` + `clip_extras_to_grid`. Bez zmeny správania, golden-krytý. Vstupná brána ku všetkému.
2. **[Vysoký / nízke] Zjednotiť SOC zdroj** — jedna `core/state.current_soc(profile, now)`, volaná všade. Odstrániť SOC-UNIFY override hacky.
3. **[Vysoký / stredné] Oddeliť nomináciu od exekúcie** (`livesim.py:1505`). Immutable nominácia; odchýlka = realita − nominácia. Rieši koreň falošných pokút.
4. **[Stredný / nízke] Zlúčiť `audit_capacity` + `audit_action`** do jednej headroom funkcie.
5. **[Stredný / nízke] Zmazať mŕtvu 3. VDT cestu** `vdt_extras.propose_greedy/propose_lp` (po overení, že ju nič živé nevolá).
6. **[Nízky / nízke] Centralizovať konvenciu fyziky batérie** (znamienko + eff) do jednej helper funkcie.

---

## Kľúčové súbory

- `vdt_live_advisor.py` (get_live_recommendation, r. 658–838 — 4 prekrývajúce poistky)
- `core/vdt_capacity_guard.py` (clip_extras_to_capacity / clip_extras_to_grid)
- `vdt_soc_feasible.py` (soc_feasible_vdt — 2. implementácia tej istej feasibility)
- `core/soc_use_audit.py` (audit_capacity #7 + audit_action #8 — 2 RT audity)
- `livesim.py` (advance: VDT-FEASIBLE ~1322–1385, nominácia ~1499–1521, dev/pokuta ~1750–1790)
