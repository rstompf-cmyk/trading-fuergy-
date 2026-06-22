# CDC — korene (suffixy) tagov — JEDNO miesto na krajinu/systém

Toto je prehľad „koreňov" tagov. **Reálny tag = `<prefix>` + `<suffix>`**, kde `<prefix>`
je per batéria/profil (napr. `VW-BA`, `Muller-SE`). Suffixy sú rovnaké pre všetky
batérie daného systému (SK CDC, neskôr CZ CDC).

Skutočný zdroj pre appku je `out/<market>/cdc_system.json` (`tags_read` / `tags_write`).
Tu je to len v čitateľnej forme + miesta na doplnenie. Keď doplníš SOC alebo nový
koreň, prepíšem to aj do `cdc_system.json`.

## SK CDC — server `http://192.168.31.30:8088`, auth `admin/admin`

### READ tagy (príklad pre prefix `VW-BA`)

| logical kľúč | suffix (koreň) | príklad reálneho tagu (VW-BA) | scale | pozn. |
|---|---|---|--:|---|
| `load_power_kw` | `_I_EL1_Power_1h` | `VW-BA_I_EL1_Power_1h` | ×0.001 | spotreba/elektromer (W→kW) |
| `ftv_power_kw` | `_I_SOL_Power_1h` | `VW-BA_I_SOL_Power_1h` | ×0.001 | FTV/solár (nie každá batéria má) |
| `batt_power_kw` | `_C_BAT_StoragePower_15m` | `VW-BA_C_BAT_StoragePower_15m` | ×0.001 | výkon batérie |
| `threshold_kw` | `_C_POW_ThresholdPowerWithoutInv_Actual_1h` | `VW-BA_C_POW_ThresholdPowerWithoutInv_Actual_1h` | ×0.001 | prah |
| `batt_soc_pct` | `_I_BMS_SOC_1m` | `VW-BA_I_BMS_SOC_1m` | ×1.0 | SOC instant/real (1-min) |
| `batt_soc_pct_15m` | `_I_BMS_SOC_15m` | `VW-BA_I_BMS_SOC_15m` | ×1.0 | SOC 15-min |
| `reg_output_kw` | `_C_REG_FinalRegulation_output` | `VW-BA_C_REG_FinalRegulation_output` | ×0.001 | feedback regulačného výkonu |

### WRITE tagy (riadenie)

| logical kľúč | suffix (koreň) | príklad (VW-BA) | scale | pozn. |
|---|---|---|--:|---|
| `cons_plan_kw` | `_U_REG_ConsumptionPlan_Manual_1h` | `VW-BA_U_REG_ConsumptionPlan_Manual_1h` | ×1000 | setpoint (manuálny plán spotreby) |
| `limit_plan_kw` | `_U_Regulation_LimitPlan` | `VW-BA_U_Regulation_LimitPlan` | ×1000 | limit regulácie |

### Nové korene tagov (sem dopíš, ja ich pridám do configu)

| logical kľúč | suffix (koreň) | smer (read/write) | scale | pozn. |
|---|---|---|--:|---|
|  |  |  |  |  |
|  |  |  |  |  |

> Pilotná batéria: **VW-BA** (prefix). Má `load`, `batt`, `threshold`, `cons_plan`;
> nemá v exceli `ftv` ani `limit_plan` — doplníme podľa reality.
