# FUERGY FTV — Redizajn · Handoff pre Claude projekt

Podklad na **implementáciu redizajnu** do existujúcej FastAPI + Jinja2 appky.
Referenčný mockup: `FUERGY Redizajn.dc.html` (otvor v prehliadači — má stránky
Simulácia / Plánovanie / Profily / Dizajn systém, prepínač v hornom nave).

> Redizajn je **čisto prezentačný**. Routy, výpočty, dáta a API sa NEMENIA.
> Mení sa `static/css/app.css`, `templates/base.html`, `templates/components/nav.html`
> a jednotlivé `templates/pages/*.html`.

---

## 1. Ciele (z briefu)

1. Jednotný light-theme vizuál naprieč všetkými ~30 stránkami.
2. Jasná hierarchia na Živej simulácii — dôležité KPI navrch, F4/VDT/debug za „Expert" prepínač.
3. Bezpečnostne čitateľné reálne riadenie — real mód = výrazné červené varovanie.
4. Zrozumiteľné grafy s konzistentnou farebnou sémantikou zložiek.
5. Prehľadnejšie formuláre (Plán/Denný trh) — zbaliteľné sekcie.
6. Stály kontext (profil + mód + trh) na každej stránke — sticky hlavička.
7. Responzívne (desktop-first, graceful na tablete/mobile).

---

## 2. Dizajn tokeny → nahradiť `:root` v `static/css/app.css`

```css
:root {
  /* povrchy */
  --bg-0:#eef4fb; --bg-1:#ffffff; --bg-2:#f7faff; --bg-3:#eaf1fb;
  --line:#d9e3f1; --line-strong:#c4d3e6;
  /* text */
  --text-0:#0b1e3f; --text-1:#38516e; --text-2:#7f93ac;
  /* brand + semantické */
  --accent:#1e6bd6;            /* primárny modrý brand akcent */
  --green:#16a34a;             /* zisk / úspech / sim */
  --danger:#dc2626;            /* nebezpečné / real HW */
  --warning:#f59e0b;
  /* sémantika grafov / zložiek (jedna farba = jedna metrika) */
  --dt:#2563eb; --rt:#0891b2; --vdt:#db2777; --dist:#7c3aed;
  --ftv:#f59e0b; --batt:#059669; --grid:#0ea5e9; --consume:#e11d48;
  /* geometria */
  --radius:10px; --radius-md:14px; --radius-pill:999px;
  --shadow:0 1px 3px rgba(15,53,110,.06);
  --shadow-lift:0 4px 12px rgba(30,107,214,.22);
}
```

**Pravidlá farieb**
- `--accent` (modrá) = brand + primárne akcie. Zisk vždy `--green`.
- Jedna metrika = jedna farba (DT/RT/VDT/Dist/FTV/Batéria/Sieť/Spotreba vyššie). Nemiešať.
- Real mód = `--danger` všade (chip, varovný pás, potvrdenia).

## 3. Typografia

```html
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
```
- Telo/nadpisy: **Sora**. Všetky čísla / časy / tagy / raw hodnoty: **JetBrains Mono**.
- Čísla: `font-variant-numeric: tabular-nums`. Veľké KPI: weight 600, letter-spacing -.02em.
- Malé labely: 11px, UPPERCASE, letter-spacing .08em, `--text-2`.
- SK konvencie: desatinná čiarka (`43,85`), jednotky malé (kW, kWh, MWh, %), čas `HH:MM:SS`,
  dátum `DD.MM.YYYY`. Použi `toLocaleString('sk-SK', {minimumFractionDigits:2})`.

## 4. Layout / responzivita
- `base.html`: sticky hlavička (topbar + nav + kontextový pás), pod ňou `<main>` max-width 1640px,
  padding `clamp(16px,2.6vw,28px)`.
- Responzivita **bez media queries** — gridy `repeat(auto-fit, minmax(Xpx, 1fr))` + `flex-wrap` +
  `clamp()`. Kolabuje na mobile automaticky. (Ak treba, doplň media query < 980px na 1 stĺpec.)

---

## 5. Komponenty (mapovanie na CSS triedy v `app.css`)

| Komponent | Trieda | Poznámka |
|---|---|---|
| Karta | `.card` + `.card-head` / `.card-body` | biela, `--line`, `--radius-md`, `--shadow` |
| KPI dlaždica | `.kpi-tile` | `border-left:3px solid <metrika>`, hodnota vo farbe metriky |
| Chip / stavy | `.chip.simulation` / `.chip.real` / `.chip` (trh, rola) | sim=zelená, real=červená |
| Tlačidlá | `.btn` / `.btn-primary` / `.btn-danger` | primary=modrá, danger=červená |
| Segmented (SK/CZ, D-1/denný trh) | `.range-bar` + `.range-btn.active` | |
| Banner | `.banner.info/.success/.warn/.error` | ľavý 4px border v sémantickej farbe |
| Tabuľka | `table.data` | mono bunky, sticky thead, hover tint, tfoot súčet |
| Formulárová sekcia | `<details>` karta so `<summary>` hlavičkou | zbaliteľná, chevron rotuje |

Presné inline hodnoty (padding, farby, radiusy) vyčítaj z mockupu `FUERGY Redizajn.dc.html`
— v ňom je každý komponent postavený na týchto tokenoch.

---

## 6. Kľúčové obrazovky

### base.html + nav.html (najprv — najväčší zisk)
Sticky hlavička v 3 pásoch:
1. **Topbar**: brand (logo + „FTV · Batéria Trading") · mode-aware nav skupiny · user chip + logout.
2. **Kontextový pás**: profil-pill prepínače (● zelená=sim, ● červená=real, sivá=neaktívny) · SK/CZ segmented.
3. **Varovný pás** (len `active_profile.mode == "real"`): červený, pulzujúca bodka,
   text „REÁLNY MÓD — povely idú na fyzické zariadenie (Bender)". Podmienené v Jinja.

Nav ostáva mode-aware (skupiny both/sim/real ako dnes v `nav.html`).

### /livesim — Živá simulácia (vlajková loď)
- Page-head + LIVE badge + „Spustiť/obnoviť".
- Control strip: dátum štartu, Orezať FTV, RT odchýlka, **Expert/debug toggle**.
- **F4 banner + VDT karty + low-level nástroje → len keď je Expert zapnutý** (progresívne odhalenie).
- KPI v 3 sekciách: *Ekonomika od štartu* (Zisk spolu + DT/RT/VDT/Dist) · *Dnešný deň*
  (Zisk za deň = hero zelená karta s rozkladom „DT +550 · RT +11 · VDT −35 · Dist −7" + baseline/prínos/úspora/FTV) · *VDT* (expert).
- 3 grafy (Chart.js, paleta zložiek zhora): nominácia vs realizované vs realita · energie zdroje(+)/spotreba(−) 96 stĺpcov · batéria plán+RT vs realita + SOC (pravá os).
- Tabuľka 15-min slotov (mono, pos/neg farby, XLSX export).

### / a /dentrh — Plán D-1 / Denný trh
- Segmented „Plán D-1 · 60 min" / „Denný trh · 15 min".
- Info banner o immutable nominácii.
- Dlhý formulár → **zbaliteľné `<details>` sekcie** v auto-fit gride: Poloha & FTV · Batéria & SOC ·
  Sieť & ceny · VDT engine · **Šablóna priebehu (96 bodov, editovateľná ťahaním)** · REGFILTER.
- **Šablóna × už nie mriežka, ale interaktívny graf** (96 bodov = 15-min sloty; baseline ×1.0;
  ťahaním bodu sa škáluje plánovaný výkon batérie v danom slote). V produkcii napojiť na
  `mult_arr` (96×) a `rt_arr` — pozri implementáciu v logike mockupu (`curveChart`, `onCurve*`).
- Sticky spodný akčný pás: Generovať plán / Uložiť profil / Batch rozsah.

### /profiles — Profily
- Header: Nový profil / Snímka z UI.
- Mriežka **profilových kariet**: mode-chip (real/sim), ★ AKTÍVNY prstenec, „beží na pozadí" indikátor,
  štatistiky (kdis/kchg · × akt · RT off), poznámka, akcie Aplikovať/Upraviť/Zmazať, čas update.
- Editor: identita + **zamknutý typ** (mód + plan_source ako read-only chips) + „beží na pozadí" toggle,
  JSON parametre (/plan, /dentrh, /rt), distribučné tarify (preset + polia). Sticky Uložiť/Späť.

---

## 7. Poradie implementácie (odporúčané)
1. `app.css` tokeny + komponenty (`.card`, `.kpi-tile`, `.chip`, `.btn`, `.range-bar`, `table.data`, `.banner`, `<details>` sekcia).
2. `base.html` + `nav.html` (sticky hlavička + kontext + real varovný pás).
3. `home.html` (/plan formulár) — vzor zbaliteľných sekcií + editovateľná krivka.
4. `profiles_list.html` + `profiles_edit.html`.
5. `livesim.html` — na koniec (najkomplexnejší; grafy cez Chart.js s paletou zložiek).
6. Zvyšné stránky prekonvertovať na rovnaké komponenty.

## 8. Riziká (nemeniť logiku)
- Realio HW write endpointy — meniť len GET render, nie POST handlery.
- Livesim Chart.js — zachovať dátové zdroje, meniť len prezentáciu/paletu.
- Per-profile `resolve_profile()` middleware — nechať tak.
- URL routy zostávajú.
