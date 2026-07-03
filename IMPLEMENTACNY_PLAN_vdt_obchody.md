# Implementačný plán: VDT obchodovanie — feasibilná nominácia + cez-polnočná koordinácia

*2026-06-30. Cieľ: VDT nominuje LEN feasibilné obchody (SOC+grid+zvyšok po DAM), koordinované cez
polnoc → realita ich dodá → odchýlka/pokuta klesne REÁLNE (nie schovaním). Predikcia LEN pre D-1 plán;
všade inde reálny DAM. Sim aj reál rovnaká logika. Golden nedotknuté, DEV-first, validácia na WV_simulacia_4.*

---

## 0. Pravidlá (cieľové správanie — odsúhlasené)

- VDT je intraday vrstva NAD DAM; DAM je posvätný, VDT len v zvyšnej kapacite (SOC, výkon, grid).
- Každý VDT obchod feasibilný **už pri nominácii** (nie pri zobrazení). Žiadny nákup pri ~plnej, predaj pri ~prázdnej.
- Rešpektovať **REÁLNY** SOC (nie projekciu). Párovať: nákup má neskorší predaj; každý pár ziskový (spread ≥ min_spread + grid_fee + cycle_cost).
- **Reálne ceny** (OKTE/OTE bid/ask, reálny DAM). Predikcia LEN pre generovanie D-1 plánu.
- **Cez polnoc:** koordinovať večer dňa N ↔ ráno dňa N+1 (nezastaviť sa o 00:00).
- Dôsledok: feasibilná nominácia → realita dodá → pokuta ≈ 0.

---

## 1. Overené korene (z investigácie kódu, nie domnienky)

**K1 — VDT audit je delta + day-start SOC, nie absolútny + reálny.**
`core/soc_use_audit.py:audit_action` (r.317): simuluje trajektóriu z `start_soc_pct` = SOC o 00:00
(r.449), nie z reálneho aktuálneho SOC (writery neposielajú `current_soc_pct_at_si`). A používa
**DELTA** logiku (`_base_viols_set`, r.482, 491-495, 510-514): zamietne len violáciu ktorú obchod
NOVO spôsobí; baseline/kumulatívne violácie ignoruje → nákup do „už plnej" trajektórie prejde.
Volajú ho `vdt_live_advisor.append_paper_trade` (r.1441) aj `append_extra_paper_trade` (r.1229).

**K2 — per-deň horizont + stale carryover → cez-polnočné dvojité nabíjanie.**
`optimize_day` je per-deň (00:00–24:00). `_resolve_soc_init_carryover` (app.py:600) berie
`carried_soc_for_date` (livesim CSV koniec N-1), ALE plán je immutable → `soc_init_used` zamrzne v čase
generovania, livesim medzitým N-1 prepočíta → divergencia (plán 26 % vs livesim 63 %). Večer N nakúpi
(drží na zajtra), deň N+1 štartuje so zlým nízkym SOC → ráno znova nakúpi (DT) → dvojité nabíjanie → pokuta.
`_resolve_terminal_soc` (app.py:656) má `base_term=0` (terminál sa nevynucuje), mode default „fixed".

**K3 — viacero nezhodných SOC zdrojov** (compute_current_state projekcia vs realizovaný trace vs meta
vs carryover). Audit/VDT dostávajú raz jeden, raz druhý → flaky a nad-nominácia. (Čiastočne riešené dnes:
core/soc_source.current_engine_soc, ale carryover/re-sim cesta ostáva.)

**K4 — zobrazenie mieša predikciu a realitu.** Napr. dentrh cena brala český OTE namiesto SK OKTE
(opravené v `/dentrh` aj `_gen_one_plan`), tabuľka „Posledných 20 slotov" berie SOC z projekcie, atď.

---

## 2. Implementačné kroky (poradie, golden-safe, DEV-first)

### KROK A — Najprv ISOLÁCIA dnešných zmien (rozhodnúť regresia vs realita)
Pred ďalšími zmenami: na DEV over, či −9201 spôsobili moje dnešné SOC zmeny (B2 proj-seed, REAL-STATE
fallback, VDT-absolute) alebo je to reálna pokuta. Postup: dočasne vrátiť tieto 3 na stav po KROK 1/2/3
(kde bolo +504), re-sim WV_4.
- ak +504 sa vráti → moje zmeny zhoršili → ponechať vrátené, ďalej stavať na čistom základe;
- ak −9201 ostane → je to reálna pokuta z nad-nominácie → ideme na KROK B.
**Akcept:** vieme jednoznačne, ktorá hodnota je pravda. (Bez tohto sa fix stavia naslepo.)

### KROK B — VDT audit: REÁLNY SOC + ABSOLÚTNA kontrola (jadro feasibility)
`core/soc_use_audit.audit_action`, len pre `source in (vdt, vdt_extra)`:
1. **Absolútna kontrola** (`_base_viols_set = set()`) — VDT nesmie pretlačiť SOC mimo [min,max] ani keď
   baseline pokazený. (RT/auto_control ostáva delta.) — *čiastočne hotové dnes, over po izolácii.*
2. **Kotviť na REÁLNOM SOC**: writery (append_paper_trade r.1441, append_extra r.1229) musia poslať
   `current_soc_pct_at_si` = realizovaný SOC v danom slote (z core/soc_source), nie nechať day-start.
**Akcept:** unit test (nabíjanie do plnej = orezané, feasibilné = accept) + WV_4 re-sim: VDT prestane
nad-nominovať večerné nákupy. Golden 5/5.

### KROK C — Cross-midnight carryover (koniec dvojitého nabíjania cez polnoc)
Cieľ: deň N+1 štartuje so SPRÁVNYM realizovaným SOC (vrátane večerného VDT dňa N) → ráno neprekúpi.
1. `_resolve_soc_init_carryover` (app.py:600) + livesim `advance` soc_after_done (r.1081) → **jeden
   zdroj** realizovaného konca N (vrátane VDT). Vyriešiť stale-immutable: keď livesim N-1 prepočíta,
   plán N pre EXEKÚCIU použije realizovaný koniec (nominácia ostáva immutable — oddelenie nominácia/exekúcia).
2. Voliteľne: `_resolve_terminal_soc` mode „next_day_price" ako default pre koordináciu večera N s cenami N+1.
**Akcept:** WV_4: večerný nákup N + ranný nákup N+1 sa nezdvojujú; SOC plynulý cez polnoc; pokuta ~0.

### KROK D — Jeden SOC zdroj (dokončiť, ak izolácia ukáže nekonzistenciu)
Dokončiť core/soc_source ako JEDINÝ current+carryover SOC pre plán, livesim, VDT, audit, zobrazenie.
(Nadväzuje na project-soc-current-vs-projection.)

---

## 3. KONZISTENČNÝ CHECKLIST — musí sedieť VŠADE (po každom kroku overiť)

Po B/C prejsť každú plochu a overiť, že ukazuje rovnaké, správne dáta:

| Plocha | Súbor / miesto | Musí ukazovať |
|---|---|---|
| **Graf „Denný trh 15-min" — DT cena** | /dentrh render | REÁLNY DAM (SK→OKTE 91/755, CZ→OTE), nie forecast |
| **Graf „Riadenie batérie" — SOC PLÁN/PREDIKCIA** | livesim render (SOC PLÁN full-day, SOC PREDIKCIA realita post-cap) | realizovaný SOC (oranžová), plán referenčný; bez skoku pri TERAZ |
| **Graf „Riadenie batérie" — Batéria kW** | livesim | plán+RT post-cap = čo realita spraví (feasibilné) |
| **Graf „Nominácia voči trhu" — odchýlka** | livesim _dev_kw (r.1743) | realita − nominácia; po fixe ≈ 0 (žiadna nad-nominácia) |
| **Tabuľka „Posledných 20 slotov" — SOC %** | app.py:9466 (`soc_pct:"last"` z dview) | REÁLNY SOC (nie projekcia 100 %) |
| **Tabuľka — VDT obchody (soc_before)** | paper_trades (core/schemas/vdt) | reálny SOC pri obchode; nákup nie pri 100 % |
| **Karty „Zisk: DT / odchýlka(RT) / VDT"** | effect_db (EffectDaily) | RT odchýlka ≈ 0 po fixe; DT/VDT reálne |
| **Plán export (Excel/PDF)** | app.py plan_export_matrix/long | rovnaké hodnoty ako plán/graf (batt_kw, ceny, SOC); jednotky kW/kWh |
| **Plán (/plans, load_plan)** | plan_store | dentrh = real_ote; soc_init_used = realizovaný carryover |
| **Manager/fleet karty** | manager dashboard | rovnaký SOC + efekt ako detail profilu |

**Pravidlo overenia:** pre vybraný deň (napr. WV_4) musia DT cena, SOC, batt a odchýlka byť **identické**
naprieč grafom, tabuľkou, kartou aj exportom. Ak sa líšia → zdroj nie je jeden → opraviť.

---

## 4. Validácia (po KAŽDOM kroku)
1. `pytest tests/test_golden_optimizer.py` (+ matcher, soc_feasible, capacity) → 5/5.
2. WV_simulacia_4 full re-sim → RT odchýlka (effect_db) musí klesnúť k ~0 (nie schovaná).
3. Prejsť konzistenčný checklist (§3) — jeden deň, všetky plochy rovnaké.
4. Skontrolovať, že VDT zisk je rozumný (orezané len nefeasibilné, nie všetko).
5. Až po zelenom na DEV → prod.

## 5. Čo NEROBIŤ
- Nepatchovať golden jadro unavene/naslepo.
- Neriešiť pokutu schovaním odchýlky (nepočítaním) — len feasibilnou nomináciou.
- Nemeniť ekonomiku (realistic, rt_kdis/kchg, breakeven) — len feasibility/konzistenciu okolo nej.
