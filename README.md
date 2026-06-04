# FTV app – štartovací balík (dátová vrstva)

Cieľ: lokálna web appka (Mac) pre **plán D-1** obchodovania FTV + batéria.
Tento prvý krok overuje, že sa **vstupné dáta dajú stiahnuť**.

## Zdroje dát
| Dáta | Zdroj | Stav |
|---|---|---|
| Predikcia FTV (kW) | Open-Meteo API | formát overený (vzorec z tvojho HTML) |
| Cena denného trhu ISOT (€/MWh, 15min) | OTE `denni-trh` | HTML tabuľka, overené |
| Cena odchýlky ZCO (Kč/MWh) + sys. odchýlka | OTE `odchylky-elektrina` | HTML tabuľka, overené |
| Minútové aFRR/mFRR, sys. odchýlka, cena RE | CEPS `CepsData.asmx` | API existuje, treba doladiť názvy operácií |

Záloha: pôvodný Excel (`analyza_regulacie_ELPREMONT.xlsx`) ako manuálny vstup
v rovnakom formáte (pridám čítačku neskôr).

## Inštalácia (Mac)
```bash
cd ftv_app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Spustenie testu importov
1. V `test_imports.py` hore nastav **LAT, LON, KWP, TILT, AZIMUTH** podľa reálnej FTV.
2. ```bash
   python test_imports.py
   ```
3. Pozri súhrn ✓/✗ a CSV v `./out/`.

## Čo mi pošli späť
- súhrn z konzoly (čo prešlo / zlyhalo),
- zoznam CEPS operácií (vypíše sa),
- 1–2 CSV (hlavne `ceps_*.csv`, prípadne `ote_*.csv`),

aby som doladil parsovanie (hlavne CEPS) a postavil nad tým cenový model,
optimalizátor plánu D-1 a HTML rozhranie s exportom do Excelu.
