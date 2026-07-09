# VDT — kompletný prehľad vrstiev a definitívne riešenie (2026-07-08)

## Problém (tvoj, správny)
VDT sa má správať JEDNODUCHO: **párovať obchody (nákup↔predaj), rešpektovať SOC [min,max]
a DAM plán, obchodovať len na reálnych cenách.** Namiesto toho VDT nakupuje do 100 % SOC,
robí nespárované obchody a „prejde cez audit". Príčina: **priveľa vrstiev, čo si protirečia.**

## Všetky VDT vrstvy, čo existujú (7 vrstiev nad sebou!)

| # | Vrstva | Kde | Čo robí | Problém |
|---|--------|-----|---------|---------|
| 1 | **Párový matcher** | vdt_pair_matcher.match_pairs | greedy páry nákup↔predaj, SOC check voči DAM base | JE správny, SOC+DAM+pair aware |
| 2 | **audit_action** (#612/#637) | core/soc_use_audit | per-obchod SOC/kapacita audit pri zápise | per-trade, kumulatívne prepúšťa |
| 3 | **no-worsen / delta audit** | core/soc_use_audit | VDT prejde ak nezvýši excess | ráta unclipped SOC, nie prácu |
| 4 | **VDT-DELIVERABLE** | core/soc_use_audit | per-slot buffer 3% pod max / nad min | per-slot, nie forward |
| 5 | **VDT-IMMUTABLE** | append_extra_paper_trade | uzavretý obchod sa neprepíše | zamkýna aj budúce plány → patchwork |
| 6 | **VDT-HEADROOM** | optimizer plán | DAM plán ≤ max−15% (pásmo pre VDT) | OK, pomáha |
| 7 | **VDT-NOMINATION-CLIP** | vdt_state + livesim | greedy oreže committed VDT na feasible | **ROZBÍJA PÁRY** (oreže nákup, nechá predaj) |
| 8 | **#81 clear-future** | scheduler | prepíše budúce VDT koherentne | nové, správny smer |
| + | **cleanup** | vdt_live_advisor | buyback na nedodateľné | ďalšia vrstva navrch |

**Toto je koreň chaosu:** 8 vrstiev, ktoré sa navzájom „opravujú". Matcher (1) spraví
správne páry, ale (5) ich zamkne po tickoch → patchwork, (7) ich oreže → rozbité páry,
(2-4) prepúšťajú kumulatívne. Ty vidíš výsledok: nákup do 100 %, nespárované.

## Definitívne riešenie — JEDEN zdroj pravdy

**Matcher (vrstva 1) JE správny** — páruje, rešpektuje SOC aj DAM. Stačí mu VERIŤ a
odstrániť vrstvy, čo ho kazia:

1. **#81 (clear-future + koherentný zápis)** = committed VDT je PRESNE matcher výstup
   (koherentný párový plán), nie patchwork. → rieši vrstvu 5 (patchwork).
2. **Vypnúť VDT-NOMINATION-CLIP** (`VDT_NOMINATION_CLIP=0`) — matcher výstup je už
   feasibilný, clip ho len rozbíja. → rieši vrstvu 7 (rozbité páry).
3. **Matcher dostane REÁLNY SOC + REÁLNY committed base** (over drift soc0) — potom je
   jeho výstop feasibilný BEZ clipu. → rieši vrstvy 2-4 (netreba per-trade clip).
4. **Cleanup ostáva len ako poistka** (floor-side buyback na krytie DAM), nič viac.

Výsledok: **1 zdroj (matcher) → 1 koherentný zápis (#81) → žiadne clipy nad ním.**
VDT páry ostanú celé, SOC feasibilný z konštrukcie matchera, žiadny nákup do 100 %
bez páru (matcher to nedovolí — allow_buyback=False + SOC check).

## Overiť (kľúčová otázka „prejde cez audit?")
Ak matcher pri reálnom SOC a DAM base sám nedáva feasibilné páry → chyba je v jeho
VSTUPOCH (soc0/base drift), nie v audite. To treba zmerať a opraviť pri zdroji, nie
ďalším clipom.

## Kroky (dev-first)
1. Over: matcher single-tick výstup (full_plan) pri reálnom SOC → je feasibilný a párový?
2. Ak áno → `VDT_NOMINATION_CLIP=0` + #81 → hotovo (žiadne clipy nad matcherom).
3. Ak nie → oprav soc0/base matcheru (VSTUP), nie výstup.
4. Golden 5/5 + repro + dev → prod.
