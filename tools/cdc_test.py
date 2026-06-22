# -*- coding: utf-8 -*-
"""
cdc_test.py — lokálny test CDC komunikácie (spúšťať na stroji s prístupom do LAN).

Použitie (z koreňa projektu):
    python tools/cdc_test.py                 # read test pre VW-BA na SK
    python tools/cdc_test.py Muller-SE       # iný prefix
    python tools/cdc_test.py VW-BA sk        # prefix + market

Test je READ-ONLY. Dočasne zapne cfg.enabled len v pamäti (nezapisuje do configu),
write sa NEvykonáva (ostáva DRY-RUN).
"""
import sys
import os
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cdc


def main():
    prefix = sys.argv[1] if len(sys.argv) > 1 else "VW-BA"
    market = sys.argv[2] if len(sys.argv) > 2 else "sk"

    cfg = cdc.load_system_config(market)
    cfg["enabled"] = True  # len v pamäti pre tento test

    print(f"=== CDC test — market={market}, prefix={prefix} ===")
    print(f"host: {cfg['host']}{cfg['endpoint_read']}")
    print(f"auth: {cfg['username']}/***  verify_ssl={cfg['verify_ssl']}  step={cfg['step_read_s']}s")
    print()

    read_tags = cdc.resolve_read_tags(prefix, cfg)
    print("READ tagy (logical -> reálny tag):")
    for k, v in read_tags.items():
        print(f"  {k:18s} {v}")
    print()
    write_tags = cdc.resolve_write_tags(prefix, cfg)
    print("WRITE tagy:")
    for k, v in write_tags.items():
        print(f"  {k:18s} {v}")
    print()

    # 1) raw read jedného tagu (ukáže surovú odpoveď servera)
    s = cdc._session(cfg)
    et = dt.datetime.now()
    bt = et - dt.timedelta(hours=3)
    first_logical = next(iter(read_tags), None)
    if first_logical:
        tag = read_tags[first_logical]
        print(f"--- RAW read '{tag}' (posledné 3h, step={cfg['step_read_s']}s) ---")
        try:
            rows = cdc._read_tag_raw(cfg, s, tag, bt, et, cfg["step_read_s"])
            for r in rows[-5:]:
                print(f"   {r['time']}  raw={r['value']}")
            if not rows:
                print("   (žiadne hodnoty)")
        except Exception as e:
            print(f"   CHYBA: {e}")
        print()

    # 2) fetch_latest — posledné hodnoty so scale (kW/%)
    print("--- fetch_latest (so scale, kW/%) ---")
    try:
        latest = cdc.fetch_latest(prefix, cfg=cfg)
        for k, v in (latest or {}).items():
            print(f"   {k:18s} {v}")
    except Exception as e:
        print(f"   CHYBA: {e}")


if __name__ == "__main__":
    main()
