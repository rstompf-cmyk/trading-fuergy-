# Runbook: validácia + promócia KROK 3 (zjednotená feasibility brána)

*2026-06-28. Flag `VDT_FEASIBILITY_UNIFIED` — DEV (8001) má =1, prod (8000) bez flagu.*

## Čo testujeme

Či `core.feasibility.gate_extras` (JEDNA brána SOC ∧ grid ∧ výkon z 1 reálneho SOC) dáva
**rovnaké alebo lepšie** výsledky než stará reťaz 3 poistiek (clip_extras_to_grid + 2× clip_extras_to_capacity).
Lepšie = menej falošnej odchýlky/pokuty pri zachovaní (alebo zlepšení) zisku.

## Príprava (raz)

1. Deploy KROK 1+2+3 (Mac `bash deploy_dev.sh` → Windows `git pull origin dev` + `.\scripts\upgrade_dev.ps1`).
2. Over, že flag beží: `docker exec trading-fuergy-dev printenv VDT_FEASIBILITY_UNIFIED` → `1`.

## A/B test (pre VW_simulacia_3 aj 4)

Keďže DEV má flag zapnutý natrvalo, A/B sa robí cez 2 behy s prepnutím flagu:

**Beh A — NOVÁ brána (flag ON, default na DEV):**
1. Prepni profil na VW_simulacia_3.
2. Full re-sim (`/plan_batch`, full reset, od ~2026-05-01 do dnes, step 15, kind plan).
3. Zapíš si z kariet: **celková odchýlka / pokuta**, **VDT zisk**, **počet VDT obchodov**.
4. V logoch nájdi `[VDT-FEASIBILITY-UNIFIED] ... gate_extras orezal N slotov`.

**Beh B — STARÁ reťaz (flag OFF):**
1. Doplň do `docker-compose.yml` dev službe dočasne `VDT_FEASIBILITY_UNIFIED: "0"` (alebo
   `docker exec -e` sa nedá pre bežiaci proces → najjednoduchšie: v compose zmeň na "0",
   `docker compose up -d trading-fuergy-dev`, počkaj štart).
2. Rovnaký full re-sim VW_simulacia_3.
3. Zapíš tie isté 3 čísla.
4. V logoch uvidíš staré `[VDT-PENALTY/grid]` / `[VDT-PENALTY/soc-real]` namiesto unified.

(Potom vráť compose na "1".)

## Pass / fail kritériá

| Metrika | PASS (promovať) | FAIL (vrátiť na OFF, analyzovať) |
|---------|-----------------|----------------------------------|
| Odchýlka/pokuta | ≤ stará (ideálne nižšia) | výrazne vyššia |
| VDT zisk | ≈ starý (±malá tolerancia) | výrazne nižší |
| SOC trajektória | vždy v [5,100] %, žiadne „nedržíme" | vyletí z pásma |
| Nominácia vs realita | sedí (graf plán+RT ≈ realita) | veľké rozdiely |

## Ak PASS → promócia na prod (spravím ja, rýchle)

1. V `vdt_live_advisor.py` zmeniť default flagu: `_FEAS_UNIFIED` default `"1"` namiesto `"0"`
   (alebo flag úplne vyhodiť a nechať len novú vetvu).
2. Zmazať starú vetvu (clip_extras_to_grid + 2× clip_extras_to_capacity) + HARD-GUARD (už zbytočný).
3. Voliteľne zmazať `core/vdt_capacity_guard.py` ak ho už nič nevolá (grep pred zmazaním).
4. Pridať `VDT_FEASIBILITY_UNIFIED: "1"` aj prod službe (alebo nový default to pokryje).
5. Golden 5/5 + full suite + deploy prod (`deploy_prod.sh`).

## Ak FAIL → diagnostika

- Najčastejší podozrivý: `net_base_kw` znamienko (shared-meter FTV/load) v gate_extras integrácii
  (`vdt_live_advisor.py`, `_nb = (dam_grid − dam_net)/0.25`).
- Porovnaj per-slot `gate_extras` report vs starý `[VDT-PENALTY/*]` report — kde sa líši clip.
- Flag späť na "0" = okamžitý návrat k overenému správaniu (zero downtime risk).

## Potom (až po PASS KROK 3)

KROK 4 (oddeliť immutable nomináciu od exekúcie, `livesim.py:1505`) + KROK 5 (merge RT auditov,
semantické SOC zjednotenie). Tieto idú až teraz, lebo stavajú na potvrdení, že brána `gate` je
ekonomicky správna — inak by sme stackovali nevalidované riziko.
