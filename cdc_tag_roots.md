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

### Plánovacie tagy regulácie v čase (WRITE, per 15-min slot) — „Local regulation limit params"

Časový rozvrh riadenia batérie. **GL** = prahová hodnota (threshold), **RL** = požadovaná
hodnota na batérke, **SL** = rozsah SOC. `*_Act` = povoliť/zakázať danú časť (true/false).
Jednotky/škálovanie zatiaľ `scale = 1.0` (doplniť podľa popisu — RL hodnoty v príklade
−0.700 / −0.167 / 0.000; SL ako celé čísla SOC).

| logical kľúč | suffix (koreň) | pozn. |
|---|---|---|
| `reg_gl_act_plan` | `_U_RegLimit_GL_Act_Plan` | GL aktívne (on/off) |
| `reg_gl_min_plan` | `_U_RegLimit_GL_Min_Plan` | prah min |
| `reg_gl_base_plan` | `_U_RegLimit_GL_Base_Plan` | prah base |
| `reg_gl_max_plan` | `_U_RegLimit_GL_Max_Plan` | prah max |
| `reg_rl_act_plan` | `_U_RegLimit_RL_Act_Plan` | RL aktívne (on/off) |
| `reg_rl_min_plan` | `_U_RegLimit_RL_Min_Plan` | batéria min |
| `reg_rl_base_plan` | `_U_RegLimit_RL_Base_Plan` | batéria base |
| `reg_rl_max_plan` | `_U_RegLimit_RL_Max_Plan` | batéria max |
| `reg_sl_min_plan` | `_U_RegLimit_SL_Min_Plan` | SOC rozsah dolný |
| `reg_sl_max_plan` | `_U_RegLimit_SL_Max_Plan` | SOC rozsah horný |

> Použitie: neskôr napojiť na **plánovanie batérie** (zapísať časový rozvrh GL/RL/SL + Act).
> Presný popis sémantiky a jednotiek dodá Radoslav.

### Live 1-min read tagy (doplnené)
`load_power_kw` = `_I_EL1_Power_1m`, `batt_power_kw` = `_C_BAT_StoragePower_1m`
(pôvodné priemery ostali ako `load_power_kw_1h`, `batt_power_kw_15m`).

### Pridanie ďalších koreňov
Nové korene si vieš pridať priamo na stránke **/cdc** (zvýraznený riadok „pridať nový koreň").

> Pilotná batéria: **VW-BA** (prefix).
