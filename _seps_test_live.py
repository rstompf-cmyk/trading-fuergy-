# -*- coding: utf-8 -*-
"""Jednorázový test — vlož reálne cookies + XSRF token, otestuj fetch.

Spustenie:  python3 _seps_test_live.py
"""
import os, pprint, sys

# ─── REÁLNE HODNOTY (z DevTools, expirujú za hodiny — refreshni ak treba) ─────
COOKIES = "ASP.NET_SessionId=xa5jxux3yifyxixiuqhicjtc5ALTR+RfDRkd+dUg4bG+vJOUR3Y=; DesktopSetupTestResults=testVersion=1.0&expiration=25.8.2026; DFE_HTTPContextSessionIdKey=4d93d2c7-038c-4d7d-920f-45534945f850; __Host-SK_PROD=680DA22BDCAE938B5ADE1242B86918ABA5F0EF99B4E31982C8209A30ADD7DAA7ECDB9F71313997A616328A298CA71D6E611FE86E22AEB1AD403DF48900AC60A567886E59B226D2C8F8C94ECA0A744D2E91E9391F7746914BDC612CB8C0914D3C23DD6BC4FC4EFA1256858D13D10D8B6EF72D4605592DFF66D6B23F5F3B4EFC3F7E0CBA964E828DC950DFF2E0E3989D2506619ED84221CE8219B08054877E5FAE1FE83716311B9189A4096E4390BB55B758F9B648249466A62267C2C1BEBF8425930C9F3251716C0EAEBA98E73DBACB18FA32006D46DA1E1BB73BF52AC1AE7E91E231AF9D68527CB87D6646E21427EB5EB3FA2B5FDDBF52FB6D9C2247229B004BE97F9433F4CE3C56EBFEA7538BDDA83EF0197F6706AC32A496BDD06960FF9CD69B23E13589366F27F0E982BCB2DEDA224CFE98530BE35A7B8B1CB0E8DDFEE9E77D67CB4EE3E089EFD087E5E4627AD26A03770229C87D4BBE5D23373A18523BA36A10AA845E8E21428027CFE2F9BE644F871CE569B39A7881EC616DDF0636D4D1D87A7A2CC11C734566CC3567415A9E4089299BA120E0C6B57644693B030D72A204BC728FB7B8B59807213DE3E6FE63E7DFDAC1354E26CD5BBA20ACDFE81CCB0D4F0DF944C28D0E186132FDE8176ACCE59628592FFA54C0BC10FA35BFE5477ACD49A68A6C8F1FE5CFACB7F18C67193B6D623EDA4C3494015D05CFAF06B0E79EA8D7B18C91C1B2B2F533424F7A9AED6B72D8F7DFF5EAC245577FC79802F34A623CFD3A2A582D5C2BD153349DFA711D4003E2CF195B1DDC08780778DDF662BB55DF7C0D635BDD250BF03D53A5D678D67E6629D4F3620811D2C7E160DE9D2BB1A8903478AC1F; UserInfo=timezone=Central European Standard Time&cultureReadOnly=sk-SK; __RequestVerificationToken_L1NLX1BST0QvREFFRi1HVUkvREFFX01WQw2=oHtBHma3120OQ76Xb95FSl2UCETwxRo7GPDBbsDFm6_Le034Sfzetm1VlWsUT1SUmmWS-BGQiY6j-Zv-lWRE84KBNvM1; __Host-SK_PROD_DFE=ioJRfAOb6s69CoIa4gbN57sUi1WXzzKp3MMYx6V8qkusxXeSX2kYY194Y5fyhsv5yhSBKj-ssiLfzZ6DIHSdWx0xtFNedXzu9-WPihAxTlgyk4UtIsCbJ3SjQyKLkpY7ktc1DvT-fIN6gNzgLPwtUfIVDSwsf07onyaUZwRQ12HU28Lo8HFxhEB-c_Xjjhuz61Bbznx7iAbhtsEefV12CNnh7h-uI5SmrydGlX1tTNjWbeaP83OC_GX9lIUBlU-dA_vPD4f6KtsjnOqtz_62rtJroZvHcA6cjvbRK5oI7W4_7DlXhlaiUQJzDEP52LHFBxcGf9jZQPolIAB-Ov9NbMDkhYO6bnL4l7SO1pQgZSVPNdfYj8gnd_zvZfIgEp3XNr87YBsDpLEZdtKAJpUOttQZQqiTFcvXyD9DEr4FWcmXU8Uc0Agy4N_LvxUoOZDnt88t2zddZO1cR8h_zOncuskGuTDzn42cM7nhCt6gvkqvK_SHRxN4qhlzQcS8UJmbFHo19Y67NvwESitaJqRQLDtu8hCM7XW064X8obmdX-lqW5oH12r-x-0DAsjq8Z5dkYG67y_h5ZowJoTDWrr2ljmnbcjZy5ad1lgDcnL42pOq8r8tBnedn4ZZ2iaMd1Hp40Ha0x-2utAMF1O_AWtYQ5qbu9yx_6TSo1KMod5nZrIYDuCZF_Zgd9sCDQmUnWabhY1dxXlY0Uu3nYz6j7Bnnf_1bhZ5rw3nySrwqJpG6jXxm9TbonqQ7Su23aJVS8mqSpBoZWyqZQMqzNLo-Bpsj4NP_JCFBxq6H1an7ggQbQIY3TYRDVMI-mP2H9JmJhdii-tVe028E3ZljqBHphUKGfVVNo-vVSvTSiFk9Hkyb_nFlkiD"

XSRF_TOKEN = "WQy7kspQN-udx9VEWPLbltNJr6YGlXFmElAzvHrRZW6gfg7gUUZ0thBOebYszstEAbWHEv2b6d_r-LiPC5JgwqPfhXv4dIDkQAGaosGB1jRLJXIsLZTy2_CB3lGNO_ZOs9TaZg2"

# Vlož do env vars PRED importom seps_sk
os.environ["SEPS_COOKIES"] = COOKIES
os.environ["SEPS_XSRF_TOKEN"] = XSRF_TOKEN
os.environ.pop("SEPS_DEBUG_NOCOOKIES", None)

import seps_sk
import json
import uuid

print(f"cookies length: {len(COOKIES)} chars")
print(f"xsrf length:    {len(XSRF_TOKEN)} chars")
print(f"# cookies parsed: {COOKIES.count(';') + 1}")
print()
print("Probe SEPS DAE...")

# RAW dump — chceme vidieť celý response, nielen parsed dict
sess = seps_sk._get_default_session()
import datetime as dt
now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
interval_till = now.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00:00.000Z")
interval_from = (now - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:00:00.000Z")
print(f"interval: {interval_from} → {interval_till}")

# pokus #1 — bez DFE-ParentRequest-Id (rovnaké ako produkčný kód)
raw = sess.fetch_load_data(seps_sk.SYSTEM_STATE_VIEW_CODE, interval_from, interval_till)
with open("/tmp/seps_live_response.json", "w") as f:
    json.dump(raw, f, indent=2, ensure_ascii=False)
print(f"raw response dumped: /tmp/seps_live_response.json")
print(f"top-level keys: {list(raw.keys())}")

if "gridConfig" in raw:
    sheets = raw["gridConfig"].get("sheets") or []
    print(f"# sheets: {len(sheets)}")
    if sheets:
        sheet = sheets[0]
        data = (sheet.get("dataModel") or {}).get("data") or []
        ts_cfg = sheet.get("timeSerieConfigurations") or {}
        print(f"# data rows: {len(data)}")
        print(f"# timeSerieConfigurations: {len(ts_cfg)}")
        for i, row in enumerate(data[:10]):
            if isinstance(row, list) and len(row) >= 2:
                lab = (row[0] or {}).get("v", "?")
                val = (row[1] or {}).get("v", "?")
                s = (row[1] or {}).get("s", "?")
                print(f"  row[{i}]: label='{lab}'  s={s}  v={val}")

print()
print("--- Parsing ---")
result = seps_sk.parse_system_state(raw)
print()
print("--- Parsed values ---")
pprint.pprint(result)

if result:
    print()
    print("--- Pretty ---")
    print(f"  Frekvencia:          {result.get('frequency_hz')} Hz")
    print(f"  Zaťaženie:           {result.get('load_mw')} MW")
    print(f"  Výroba:              {result.get('production_mw')} MW")
    print(f"  Saldo merané:        {result.get('real_balance_mw')} MW")
    print(f"  Saldo obchodný:      {result.get('scheduled_balance_mw')} MW")
    print(f"  Regulačný výkon:     {result.get('regulation_power_mw')} MW   (>0=deficit, <0=prebytok)")
    print(f"  Updated at:          {result.get('updated_at')}")
    sys.exit(0)
else:
    print()
    print("ZLYHALO. Skontroluj prípadne fresh cookies (refresh stránky + nové COPY).")
    sys.exit(1)
