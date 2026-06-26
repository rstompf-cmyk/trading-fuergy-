# Zoznam tagov SK batérií + komunikačný protokol

Zdroj: `Planovac_Vzor_V51.xlsm` (VBA modul `cdc.bas`, hárky `Read`/`Read2`).

## Komunikačný protokol (CDC API)

Rovnaký princíp ako `trakany_real` (HTTP + tag mapovanie), ale iný backend — centrálny CDC server, nie Bender dashboard jednotlivej inštalácie.

**Server:** `http://192.168.31.30:8088` (API z VBA) — dashboard supervízia beží na `http://192.168.31.33:8088`. *(IP rozdiel overiť.)*

**Auth:** HTTP Basic, `admin` / `admin` (named ranges `user`/`pwd` v exceli).

**READ** — `GET /api/excel/data/read`
```
GET /api/excel/data/read?tag=<TAG>&bt=<DD.MM.YYYY_HH:mm:ss>&et=<DD.MM.YYYY_HH:mm:ss>&step=<sekundy>
Odpoveď JSON: {"values":[{"time":"DD.MM.YYYY HH:mm:ss","value":"123.4"}, ...]}
```
- `step`: 900 = 15-min, 3600 = 1h.
- `value` chodí ako string s desatinnou bodkou.
- Pozor na škálovanie: VBA `read_RE` delí `*_C_BAT_StoragePower_15m` hodnotu **/4 000 000** (overiť pre ostatné tagy; Power_1h/Threshold idú do buniek bez delenia).

**WRITE** — `POST /api/excel/data/write`
```
Content-Type: application/json
Body: {"<TAG>": [ {<dátové body>} ]}
```
- Riadiace tagy: `*_U_REG_ConsumptionPlan_Manual_1h` (manuálny plán spotreby = setpoint) a `*_U_Regulation_LimitPlan` (limit regulácie).
- Analógia s Trakany: `ConsumptionPlan_Manual` ~ `REG_Regulator_Manual_Plan`, `LimitPlan` ~ `REG_Regulator_Param3`.

## Stránky/batérie — 35 subjektov

Y = tag existuje v exceli, . = chýba. Tag = `<PREFIX>` + prípona.

| Subjekt (prefix) | LOAD EL1 | FTV SOL | BAT | PRAH | W:ConsPlan | W:LimitPlan |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| `Amico` | Y | . | Y | Y | Y | Y |
| `Amico2` | . | . | Y | Y | Y | Y |
| `AudiaPlastics` | Y | . | Y | Y | Y | Y |
| `AudiaPlastics2` | . | . | Y | Y | Y | Y |
| `BILLA-KOMARNO` | Y | . | Y | Y | Y | . |
| `Delika-SE` | . | . | Y | Y | Y | Y |
| `EPG` | . | . | . | Y | Y | Y |
| `Eissmann` | . | . | Y | Y | Y | Y |
| `Eissmann2` | . | . | Y | . | Y | Y |
| `Embraco` | Y | . | Y | Y | Y | Y |
| `Embraco2` | . | . | Y | Y | Y | Y |
| `Gevorkyan` | Y | . | Y | Y | Y | Y |
| `HU-BillaBajkalska` | . | . | Y | Y | Y | Y |
| `HU-LindMobler` | Y | . | Y | Y | Y | Y |
| `HU-Nimnica` | Y | . | Y | Y | Y | Y |
| `HU-Stavokov` | Y | . | Y | Y | Y | . |
| `HU_Hrbaty` | Y | Y | Y | Y | Y | Y |
| `HU_Jakab` | Y | Y | Y | Y | Y | Y |
| `HU_Klucar` | Y | Y | Y | Y | Y | Y |
| `HU_Stavokov` | . | . | . | . | . | Y |
| `HU_VDZVSFinancie` | Y | . | Y | Y | Y | Y |
| `Hanon` | Y | Y | Y | Y | Y | Y |
| `Hanon2` | Y | Y | Y | Y | Y | Y |
| `Laugaricio` | Y | . | Y | Y | Y | Y |
| `Medeko` | . | . | Y | Y | Y | Y |
| `Mincovna` | . | . | Y | Y | Y | Y |
| `Muller-SE` | Y | Y | Y | Y | Y | Y |
| `Muller2-SE` | . | . | Y | Y | Y | Y |
| `NFS` | Y | . | Y | Y | Y | Y |
| `OC-GaleriaMartin` | . | . | Y | Y | Y | Y |
| `Osram` | Y | Y | Y | Y | Y | Y |
| `RD2-Piestany` | Y | Y | Y | Y | Y | Y |
| `Tower115` | Y | . | Y | Y | Y | Y |
| `VW-BA` | Y | . | Y | Y | Y | . |
| `Vertiv` | Y | . | Y | Y | Y | Y |

## Plné názvy tagov podľa subjektu

### Amico
- READ load — elektromer EL1 (1h): `Amico_I_EL1_Power_1h`
- READ batéria — StoragePower (15m): `Amico_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Amico_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Amico_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Amico_U_Regulation_LimitPlan`

### Amico2
- READ batéria — StoragePower (15m): `Amico2_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Amico2_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Amico2_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Amico2_U_Regulation_LimitPlan`

### AudiaPlastics
- READ load — elektromer EL1 (1h): `AudiaPlastics_I_EL1_Power_1h`
- READ batéria — StoragePower (15m): `AudiaPlastics_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `AudiaPlastics_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `AudiaPlastics_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `AudiaPlastics_U_Regulation_LimitPlan`

### AudiaPlastics2
- READ batéria — StoragePower (15m): `AudiaPlastics2_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `AudiaPlastics2_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `AudiaPlastics2_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `AudiaPlastics2_U_Regulation_LimitPlan`

### BILLA-KOMARNO
- READ load — elektromer EL1 (1h): `BILLA-KOMARNO_I_EL1_Power_1h`
- READ batéria — StoragePower (15m): `BILLA-KOMARNO_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `BILLA-KOMARNO_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `BILLA-KOMARNO_U_REG_ConsumptionPlan_Manual_1h`

### Delika-SE
- READ batéria — StoragePower (15m): `Delika-SE_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Delika-SE_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Delika-SE_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Delika-SE_U_Regulation_LimitPlan`

### EPG
- READ prah — ThresholdPowerWithoutInv (1h): `EPG_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `EPG_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `EPG_U_Regulation_LimitPlan`

### Eissmann
- READ batéria — StoragePower (15m): `Eissmann_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Eissmann_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Eissmann_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Eissmann_U_Regulation_LimitPlan`

### Eissmann2
- READ batéria — StoragePower (15m): `Eissmann2_C_BAT_StoragePower_15m`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Eissmann2_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Eissmann2_U_Regulation_LimitPlan`

### Embraco
- READ load — elektromer EL1 (1h): `Embraco_I_EL1_Power_1h`
- READ batéria — StoragePower (15m): `Embraco_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Embraco_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Embraco_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Embraco_U_Regulation_LimitPlan`

### Embraco2
- READ batéria — StoragePower (15m): `Embraco2_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Embraco2_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Embraco2_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Embraco2_U_Regulation_LimitPlan`

### Gevorkyan
- READ load — elektromer EL1 (1h): `Gevorkyan_I_EL1_Power_1h`
- READ batéria — StoragePower (15m): `Gevorkyan_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Gevorkyan_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Gevorkyan_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Gevorkyan_U_Regulation_LimitPlan`

### HU-BillaBajkalska
- READ batéria — StoragePower (15m): `HU-BillaBajkalska_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `HU-BillaBajkalska_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `HU-BillaBajkalska_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `HU-BillaBajkalska_U_Regulation_LimitPlan`

### HU-LindMobler
- READ load — elektromer EL1 (1h): `HU-LindMobler_I_EL1_Power_1h`
- READ batéria — StoragePower (15m): `HU-LindMobler_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `HU-LindMobler_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `HU-LindMobler_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `HU-LindMobler_U_Regulation_LimitPlan`

### HU-Nimnica
- READ load — elektromer EL1 (1h): `HU-Nimnica_I_EL1_Power_1h`
- READ batéria — StoragePower (15m): `HU-Nimnica_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `HU-Nimnica_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `HU-Nimnica_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `HU-Nimnica_U_Regulation_LimitPlan`

### HU-Stavokov
- READ load — elektromer EL1 (1h): `HU-Stavokov_I_EL1_Power_1h`
- READ batéria — StoragePower (15m): `HU-Stavokov_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `HU-Stavokov_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `HU-Stavokov_U_REG_ConsumptionPlan_Manual_1h`

### HU_Hrbaty
- READ load — elektromer EL1 (1h): `HU_Hrbaty_I_EL1_Power_1h`
- READ FTV — solár SOL (1h): `HU_Hrbaty_I_SOL_Power_1h`
- READ batéria — StoragePower (15m): `HU_Hrbaty_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `HU_Hrbaty_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `HU_Hrbaty_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `HU_Hrbaty_U_Regulation_LimitPlan`

### HU_Jakab
- READ load — elektromer EL1 (1h): `HU_Jakab_I_EL1_Power_1h`
- READ FTV — solár SOL (1h): `HU_Jakab_I_SOL_Power_1h`
- READ batéria — StoragePower (15m): `HU_Jakab_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `HU_Jakab_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `HU_Jakab_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `HU_Jakab_U_Regulation_LimitPlan`

### HU_Klucar
- READ load — elektromer EL1 (1h): `HU_Klucar_I_EL1_Power_1h`
- READ FTV — solár SOL (1h): `HU_Klucar_I_SOL_Power_1h`
- READ batéria — StoragePower (15m): `HU_Klucar_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `HU_Klucar_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `HU_Klucar_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `HU_Klucar_U_Regulation_LimitPlan`

### HU_Stavokov
- WRITE limit — Regulation_LimitPlan: `HU_Stavokov_U_Regulation_LimitPlan`

### HU_VDZVSFinancie
- READ load — elektromer EL1 (1h): `HU_VDZVSFinancie_I_EL1_Power_1h`
- READ batéria — StoragePower (15m): `HU_VDZVSFinancie_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `HU_VDZVSFinancie_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `HU_VDZVSFinancie_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `HU_VDZVSFinancie_U_Regulation_LimitPlan`

### Hanon
- READ load — elektromer EL1 (1h): `Hanon_I_EL1_Power_1h`
- READ FTV — solár SOL (1h): `Hanon_I_SOL_Power_1h`
- READ batéria — StoragePower (15m): `Hanon_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Hanon_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Hanon_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Hanon_U_Regulation_LimitPlan`

### Hanon2
- READ load — elektromer EL1 (1h): `Hanon2_I_EL1_Power_1h`
- READ FTV — solár SOL (1h): `Hanon2_I_SOL_Power_1h`
- READ batéria — StoragePower (15m): `Hanon2_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Hanon2_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Hanon2_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Hanon2_U_Regulation_LimitPlan`

### Laugaricio
- READ load — elektromer EL1 (1h): `Laugaricio_I_EL1_Power_1h`
- READ batéria — StoragePower (15m): `Laugaricio_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Laugaricio_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Laugaricio_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Laugaricio_U_Regulation_LimitPlan`

### Medeko
- READ batéria — StoragePower (15m): `Medeko_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Medeko_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Medeko_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Medeko_U_Regulation_LimitPlan`

### Mincovna
- READ batéria — StoragePower (15m): `Mincovna_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Mincovna_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Mincovna_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Mincovna_U_Regulation_LimitPlan`

### Muller-SE
- READ load — elektromer EL1 (1h): `Muller-SE_I_EL1_Power_1h`
- READ FTV — solár SOL (1h): `Muller-SE_I_SOL_Power_1h`
- READ batéria — StoragePower (15m): `Muller-SE_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Muller-SE_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Muller-SE_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Muller-SE_U_Regulation_LimitPlan`

### Muller2-SE
- READ batéria — StoragePower (15m): `Muller2-SE_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Muller2-SE_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Muller2-SE_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Muller2-SE_U_Regulation_LimitPlan`

### NFS
- READ load — elektromer EL1 (1h): `NFS_I_EL1_Power_1h`
- READ batéria — StoragePower (15m): `NFS_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `NFS_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `NFS_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `NFS_U_Regulation_LimitPlan`

### OC-GaleriaMartin
- READ batéria — StoragePower (15m): `OC-GaleriaMartin_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `OC-GaleriaMartin_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `OC-GaleriaMartin_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `OC-GaleriaMartin_U_Regulation_LimitPlan`

### Osram
- READ load — elektromer EL1 (1h): `Osram_I_EL1_Power_1h`
- READ FTV — solár SOL (1h): `Osram_I_SOL_Power_1h`
- READ batéria — StoragePower (15m): `Osram_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Osram_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Osram_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Osram_U_Regulation_LimitPlan`

### RD2-Piestany
- READ load — elektromer EL1 (1h): `RD2-Piestany_I_EL1_Power_1h`
- READ FTV — solár SOL (1h): `RD2-Piestany_I_SOL_Power_1h`
- READ batéria — StoragePower (15m): `RD2-Piestany_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `RD2-Piestany_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `RD2-Piestany_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `RD2-Piestany_U_Regulation_LimitPlan`

### Tower115
- READ load — elektromer EL1 (1h): `Tower115_I_EL1_Power_1h`
- READ batéria — StoragePower (15m): `Tower115_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Tower115_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Tower115_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Tower115_U_Regulation_LimitPlan`

### VW-BA
- READ load — elektromer EL1 (1h): `VW-BA_I_EL1_Power_1h`
- READ batéria — StoragePower (15m): `VW-BA_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `VW-BA_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `VW-BA_U_REG_ConsumptionPlan_Manual_1h`

### Vertiv
- READ load — elektromer EL1 (1h): `Vertiv_I_EL1_Power_1h`
- READ batéria — StoragePower (15m): `Vertiv_C_BAT_StoragePower_15m`
- READ prah — ThresholdPowerWithoutInv (1h): `Vertiv_C_POW_ThresholdPowerWithoutInv_Actual_1h`
- WRITE setpoint — ConsumptionPlan_Manual (1h): `Vertiv_U_REG_ConsumptionPlan_Manual_1h`
- WRITE limit — Regulation_LimitPlan: `Vertiv_U_Regulation_LimitPlan`
