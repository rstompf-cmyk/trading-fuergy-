# Redizajn brief — FUERGY FTV + Batéria Trading Platform

> Tento dokument je kompletný podklad pre **redizajn UI/UX**. Obsahuje čo appka robí, pre koho,
> celú informačnú architektúru (všetky stránky), súčasný dizajn systém, dátové entity, technické
> obmedzenia a ciele redizajnu. Je písaný tak, aby ho dizajnér (alebo dizajnový AI asistent) vedel
> použiť bez prístupu ku kódu.

---

## 1. Čo appka je (v jednej vete)

Interná webová platforma na **plánovanie, simuláciu, obchodovanie a reálne riadenie batériových
úložísk spojených s fotovoltaikou (FTV)** na slovenskom (SK) a českom (CZ) trhu s elektrinou.
Optimalizuje, kedy batéria nabíja/vybíja podľa cien na dennom (DAM) a vnútrodennom (VDT) trhu,
sleduje reálny výsledok voči plánu a vie povel poslať na fyzickú batériu.

Je to **hustý, dátovo náročný „trading + monitoring" dashboard** pre interných operátorov —
nie spotrebiteľský produkt. Priorita je **prehľadnosť čísel, grafov a rýchle rozhodovanie**, nie
marketingová estetika.

## 2. Kto to používa

- **Operátor / obchodník** — denne sleduje plán vs realitu, zisk za deň, VDT obchody, posiela
  povely. Hlavný používateľ.
- **Manažér** — flotilový prehľad viacerých batérií/zákazníkov, ekonomika, bez zásahov.
- **Admin** — správa používateľov, rolí, audit log.

Prihlásenie + role (admin/operator/viewer). Appka je viacjazyčne **po slovensky**.

## 3. Doménový slovník (kľúčové pojmy — dizajn ich musí zrozlišovať vizuálne)

| Pojem | Význam |
|---|---|
| **FTV** | Fotovoltaická výroba (kWp, kWh). |
| **Batéria / SOC** | Úložisko; SOC = stav nabitia v % (0–100). |
| **D-1 plán** | Plán na zajtra (day-ahead), pripravený deň vopred. Po uzávierke **immutable** (nemenný). |
| **DAM / Denný trh** | Day-ahead market — ceny €/MWh známe deň vopred. |
| **VDT** | Vnútrodenný trh (intraday) — obchodovanie počas dňa, párové nákup↔predaj cykly. |
| **RT odchýlka** | Real-time odchýlka reality od plánu (kladná/záporná), zúčtovaná trhom. |
| **Nominácia** | Trhový záväzok (D-1 + uzavreté VDT), ktorý batéria MUSÍ fyzicky splniť. |
| **ZCO** | Zúčtovacia cena odchýlky. |
| **Zisk za deň** | Denný ekonomický výsledok, rozložený na zložky: **DT · RT · VDT · Distribúcia**. |
| **Profil** | Konfigurácia jedného úložiska (batéria kW/kWh, FTV, limity, ceny). Má **mód**: `simulation` alebo `real`. |
| **Trh** | `SK` alebo `CZ` — prepínateľný, mení dátové zdroje. |
| **Bender / realio / CDC** | Backend reálnej batérie (fyzické meranie + zápis povelov). |

**Kritické UX pravidlo:** používateľ musí vždy jasne vidieť **ktorý profil** a **ktorý mód
(sim/real)** je aktívny — reálny mód znamená, že povely idú na fyzické zariadenie.

## 4. Informačná architektúra (súčasná navigácia — 5 skupín, mode-aware)

Navigácia je zoskupené rozbaľovacie menu. Položky sú tagované `both` / `sim` / `real` — skupina/
položka sa zobrazí len ak dáva pre aktívny mód zmysel.

### 🗓 Plánovanie
- **Plán D-1** (`/`) — hlavný D-1 plánovací formulár + generovanie.
- **Denný trh 15-min** (`/dentrh`) — 15-min plán na základe známych/reálnych DAM cien; tu je aj REGFILTER (max odber W).
- **Batch plán** (`/plan_batch`) — hromadné generovanie plánov pre rozsah dní.
- **Uložené plány** (`/plans`) — zoznam + mazanie plánov.

### 🟢 Simulácia & monitoring
- **Živá simulácia** (`/livesim`) — ⭐ NAJDÔLEŽITEJŠIA stránka. Beží od dátumu štartu po teraz;
  karty (Zisk spolu / DT / RT / VDT / Distribúcia / Zisk za deň / FTV výroba), 3 veľké grafy
  (nominácia vs realita; energetické zdroje/spotreba; batéria plán+RT vs realita + SOC), tabuľka.
- **Manager dashboard** (`/manager`) — flotila + detail, read-only ekonomika.
- **Flotila** (`/fleet`, `/fleet/api`) — live prehľad viacerých profilov (SOC, výkon).
- **Profil dashboard** (`/dashboard`).
- **Výsledok simulácie** (`/simulacia`) — len sim profily.

### 💹 Obchodovanie
- **RT poradca** (`/rt`) — odporúčanie pre aktuálny 15-min interval.
- **OKTE VDT prehľad** (`/vdt`) — orderbook, obchody.
- **VDT Live advisor** (`/vdt/live_advisor`), **VDT D-1** (`/vdt/d1`), **VDT Board** (`/vdt/board`).
- **VDT Simulátor / Backtest / ZCO Backtest** (sim).
- **Paper trading** (`/auto_control`) — kill-switch, toggle profilov.
- (+ mnoho diagnostických VDT endpointov: orderbook depth/live, watch, discover, probe…)

### 🔌 Reálne riadenie (len `real` profil)
- **Reálne meranie** (`/realio`) — dáta z Bender backendu, zápis povelov, safety toggle.
- **Zákazníci** (`/customers`) — flotila reálnych batérií.
- **CDC konfigurácia** (`/cdc`).
- **Okno regulácie** (`/customers/battery/regulation`).

### 💾 Dáta & nastavenia
- **Profily** (`/profiles`, edit/save/apply/snapshot/delete) — správa konfigurácií.
- **Spotreba** (`/load_import`) — import spotrebných dát.
- **FTV scenár** (`/ftv_scenario`), **Kalibrácia** (`/kalibracia`), **Dáta** (`/data`).

### Admin (oddelený)
- `/admin/users`, `/admin/audit_log` — správa používateľov, audit.

**Poznámka:** appka má ~90 endpointov, veľká časť sú **diagnostické / low-level nástroje**
(VDT discover, probe, scan, cert inšpekcia…). Redizajn by mal oddeliť **denné pracovné stránky**
(plán, živá sim, VDT prehľad, realio) od **expertných/diagnostických** nástrojov.

## 5. Kľúčové obrazovky — detail (na čo sa sústrediť pri redizajne)

### 5.1 Živá simulácia (`/livesim`) — vlajková loď
Horný pás: veľký zoznam **profilových „pill" prepínačov** (červené = real, zelené = sim, sivé =
neaktívne), pod tým prepínač trhu (SK/CZ), dropdown menu, a riadok ovládania (dátum štartu,
Orezať/vypnúť FTV, RT odchýlka áno/nie, tlačidlo „Spustiť/obnoviť").

Potom **F4 diagnostický banner** (žltý, monospace — technický, malo by byť skryté/za „debug"),
potom **mriežka kariet**:
- Zisk SPOLU (od štartu) · z toho DT · z toho RT · z toho distribúcia · z toho VDT
- VDT obchody dnes · VDT od štartu · VDT Upratovanie
- **Zisk za deň** (zelená karta, veľké číslo + rozklad „DT +550 · RT +11 · VDT −35 · Dist −7")
- Baseline · Prínos batérie · Úspora na distribúcii · FTV výroba za deň

Pod kartami **3 veľké 15-minútové grafy** (96 slotov, 00:00–23:45):
1. **Nominácia (D-1) vs realizované vs realita** — čiarový, farby: svetlomodrá plná = nominácia,
   oranžová = VDT realizované, tmavomodrá = aktuálna nominácia.
2. **Energie — zdroje (+) vs spotreba (−)** — stĺpcový: FTV, batéria vybíja/nabíja, sieť
   import/export, spotreba, orezanie.
3. **Batéria — predikcia (plán+RT) vs realita + SOC** — čiary + SOC os vpravo (0–100 %).

**Problém súčasného stavu:** extrémne husté, veľa legiend, technické bannery viditeľné,
karty nekonzistentne štýlované, málo vizuálnej hierarchie (čo je dôležité vs pomocné).

### 5.2 Plán D-1 (`/`) a Denný trh (`/dentrh`)
Dlhé formuláre: mapa (poloha FTV), geometria FTV, batéria/SOC limity, sieť, ceny, VDT engine,
export, **96-slotová šablóna × a RT mask**, REGFILTER 96-slot tabuľka (max odber W), batch rozsah.
Veľa `fieldset` sekcií pod sebou. Kandidát na **prehľadnejšie zoskupenie / krokový sprievodca /
sekcie s možnosťou zbaliť**.

### 5.3 VDT prehľad (`/vdt`, board, live advisor)
Orderbook (ceny/likvidita), zoznam obchodov (nákup/predaj, párovanie), cash saldo. Číselne husté.

### 5.4 Realio / Zákazníci (`/realio`, `/customers`) — reálne riadenie
Merania z fyzickej batérie, tlačidlá na zápis povelov (setpoint, FVE, manual plan) so
**safety poistkami** (enabled / control_enabled). Vysoké riziko — dizajn musí jasne odlíšiť
„čítanie" od „zápis na hardvér" (potvrdzovacie stavy, výrazné varovania).

### 5.5 Profily (`/profiles`)
Zoznam profilov (pill prepínače hore v každej stránke), editácia konfigurácie, snapshot, apply.

## 6. Súčasný dizajn systém (baseline — čo existuje dnes)

Server-rendered HTML + jeden CSS (`/static/css/app.css`, ~340 riadkov). Dizajnové tokeny:

```css
--primary:#1F4E78;  --primary-dark:#16395a;  --primary-light:#2E75B6;
--danger:#C62828;   --warning:#f0b80f;       --success:#2E7D32;
--bg:#f3f6fb;       --bg-card:#fff;          --border:#e3e8ef;
--text:#222;        --text-muted:#666;       --text-faint:#888;
--radius:10px (sm 7 / lg 14);  --shadow:0 1px 3px rgba(0,0,0,.06);
```

Komponenty (CSS triedy, ktoré existujú): `.container`, `.app-nav` + `.nav-group`/`.nav-menu`
(dropdown), `.card` (+ `.card-h` hlavička, `.card-b` telo, `.card-blue/green/yellow` varianty),
`.kpi`/`.kpi-row` (veľké čísla), `.chip` (profil/trh/rola štítky), `.btn`, `.banner`, `.page-head`,
`.tbl-compact` (husté tabuľky), `.cols`/`.cols3` (grid rozloženie).

Grafy: klientske JS grafy (Chart.js-štýl) na `<canvas>`, mapy cez Leaflet.

**Stav konzistencie:** časť stránok je prevedená na `.card`/`.kpi` systém, **veľa stránok má
ešte inline-styled HTML** (nekonzistentné odsadenia, farby, tlačidlá). Nový mode-aware nav +
CSS dizajn systém sú hotové, ale **konverzia všetkých stránok na jednotný štýl nie je dokončená**
(page guard podľa módu, navigácia na každej stránke vrátane chybových, jednotné karty).

## 7. Dátové entity (čo sa zobrazuje)

- **Profil**: name, mode (sim/real), market (sk/cz), batéria (kW/kWh, eff, SOC min/max/init),
  FTV (kWp, poloha, sklon), sieť (grid limity, poplatok), ceny (cycle_cost, min_spread),
  VDT engine, plan_source (predicted/dentrh).
- **Plán** (per deň): 96 (alebo 24) slotov: cena €/MWh, batéria kW, sieť kWh, SOC %, order_mwh,
  nabíjanie/vybíjanie, export/import. + summary (zisk, nabité/vybité kWh).
- **Trace / efekt** (per minúta → deň): dt/rt/vdt zisk, SOC, výkon, FTV, spotreba. Uložené v DB
  (effect_minute / effect_daily), agregované do kariet a grafov.
- **VDT obchody**: ts, action (buy/sell), kWh, cena, párovanie, cash saldo.
- **Reálne meranie** (real profil): SOC, výkon, FVE, časové rady z Bender backendu.

## 8. Technické obmedzenia (musí redizajn rešpektovať)

1. **Server-rendered FastAPI + Jinja2** (nie SPA/React). HTML sa generuje na serveri; interaktivita
   je vanilla JS + Chart.js + Leaflet. Redizajn musí byť realizovateľný ako HTML/CSS + ľahké JS.
2. **Jeden globálny CSS** (`app.css`) + `base.html` layout + `components/nav.html`. Ideálny výstup
   redizajnu = **nový/rozšírený dizajn systém (CSS) + prepracované šablóny stránok**, nie prepis
   na iný framework.
3. **Endpointy/routy sa nesmú meniť** (URL zostávajú) — mení sa prezentácia, nie API.
4. **Jazyk = slovenčina.**
5. **Desktop-first** (operátori pri monitore, husté dáta, viac grafov naraz). Mobil je sekundárny.
6. **Dátová hustota je feature, nie bug** — nezjednodušovať na úkor informácií; skôr lepšia
   hierarchia, zoskupenie, progresívne odhalenie (základ vs expert/debug).

## 9. Ciele redizajnu (čo chceme dosiahnuť)

1. **Jednotný, konzistentný vizuál** naprieč všetkými stránkami (dokončiť konverziu na dizajn systém).
2. **Jasná hierarchia na Živej simulácii** — dôležité KPI navrch, pomocné/diagnostické skryté za
   „debug/expert" prepínač (F4 banner, low-level VDT nástroje).
3. **Bezpečnostne čitateľné reálne riadenie** — vizuálne oddeliť čítanie od zápisu na hardvér,
   silné potvrdenia a stavy poistiek (enabled/control_enabled).
4. **Zrozumiteľné grafy** — konzistentná farebná paleta zložiek (DT / RT / VDT / Distribúcia /
   FTV / batéria / SOC), čitateľné legendy, menej šumu.
5. **Prehľadnejšie formuláre** (Plán / Denný trh) — zoskupenie, zbaliteľné sekcie, prípadne kroky.
6. **Stále jasný kontext** (aktívny profil + mód + trh) na každej stránke.
7. **Oddeliť denné pracovné stránky od expertných/diagnostických** nástrojov.

## 10. Čo od dizajnového Clauda chcem (deliverables)

1. **Návrh dizajn systému**: farebná paleta (vrátane sémantických farieb pre DT/RT/VDT/Dist a
   sim/real/trh stavy), typografia, spacing, komponenty (karta, KPI, chip, tabuľka, banner,
   graf-legenda, tlačidlá, formulárové sekcie, nav).
2. **Kľúčové obrazovky ako mockupy** (HTML/CSS, aby sa dali priamo použiť v Jinja šablónach):
   - Živá simulácia (karty + 3 grafy + ovládanie + profil prepínače)
   - Plán D-1 / Denný trh (formulár)
   - VDT prehľad
   - Realio (reálne riadenie so safety stavmi)
   - Profily
3. **Vzor konzistentnej stránky** (layout `base.html` + nav + page-head + obsah), ktorý sa dá
   aplikovať na všetkých ~30 stránok.
4. **Paleta pre grafy** + odporúčania na legendy/hustotu.
5. Responzívne správanie (desktop-first, graceful na tablete).

## 11. Odkazy na existujúce súbory (pre kontext)

- Layout: `templates/base.html`
- Navigácia: `templates/components/nav.html` (5 skupín, mode-aware — viď §4)
- Dizajn systém: `static/css/app.css` (tokeny + komponenty — viď §6)
- Stránky: `templates/pages/*.html` (časť), zvyšok generovaný inline v `app.py`
- Hlavná stránka na zdokonalenie: `/livesim` (Živá simulácia)

---

*Pozn.: appka je interný nástroj FUERGY na obchodovanie s energiou z batérií+FTV (SK/CZ). Redizajn
je čisto prezentačný — logika, výpočty, routy a dáta zostávajú.*
