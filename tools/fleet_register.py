#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/fleet_register.py — registrácia / výpis batérií VPP flotily (CLI).

Aditívne, dormantné: zapisuje len do fleet tabuliek (battery/...), NEdotýka sa
existujúcej appky. Vyžaduje aplikovanú migráciu d1e2f3a4b5c6 (fleet tabuľky).

Príklady:
    # výpis flotily
    python tools/fleet_register.py list

    # registrácia simulačnej batérie (enabled = pôjde ako inštancia v SIM ticku)
    python tools/fleet_register.py add --name SIM1 --country sk --mode simulation \\
        --batt-kw 1000 --batt-kwh 2000 --eff 0.95 --enabled

    # registrácia reálnej batérie (realio wiring je ďalší krok — zatiaľ sa NEspustí)
    python tools/fleet_register.py add --name REAL1 --country sk --mode real \\
        --batt-kw 990 --batt-kwh 2150 --realio-host 10.0.0.9 --enabled

    # zapnúť/vypnúť batériu ako inštanciu
    python tools/fleet_register.py enable 3
    python tools/fleet_register.py disable 3
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import fleet  # noqa: E402


def _print_fleet():
    rows = fleet.list_batteries()
    if not rows:
        print("(flotila prázdna)")
        return
    print(f"{'id':>3}  {'name':<16} {'kraj':<4} {'mód':<11} {'kW':>7} {'kWh':>8} {'en':<3} realio_host")
    for b in rows:
        print(f"{b['id']:>3}  {b['name']:<16} {b['country']:<4} {b['mode']:<11} "
              f"{b['batt_kw']:>7.0f} {b['batt_kwh']:>8.0f} "
              f"{'A' if b['enabled'] else '-':<3} {b['realio_host'] or ''}")


def main():
    ap = argparse.ArgumentParser(description="VPP fleet register")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="vypíš flotilu")

    a = sub.add_parser("add", help="registruj/uprav batériu (UPSERT podľa name)")
    a.add_argument("--name", required=True)
    a.add_argument("--country", required=True, choices=["sk", "cz"])
    a.add_argument("--mode", default="simulation", choices=["simulation", "real"])
    a.add_argument("--batt-kw", type=float, default=0.0)
    a.add_argument("--batt-kwh", type=float, default=0.0)
    a.add_argument("--eff", type=float, default=0.95)
    a.add_argument("--profile-id", type=int, default=None)
    a.add_argument("--enabled", action="store_true")
    a.add_argument("--realio-host", default=None)
    a.add_argument("--realio-username", default=None)
    a.add_argument("--realio-password", default=None)
    a.add_argument("--realio-poll-sec", type=int, default=60)

    en = sub.add_parser("enable", help="zapni batériu ako inštanciu")
    en.add_argument("battery_id", type=int)
    di = sub.add_parser("disable", help="vypni batériu")
    di.add_argument("battery_id", type=int)

    args = ap.parse_args()

    if args.cmd == "list":
        _print_fleet()
    elif args.cmd == "add":
        bid = fleet.register_battery(
            args.name, args.country, mode=args.mode, batt_kw=args.batt_kw,
            batt_kwh=args.batt_kwh, eff=args.eff, profile_id=args.profile_id,
            enabled=args.enabled, realio_host=args.realio_host,
            realio_username=args.realio_username, realio_password=args.realio_password,
            realio_poll_sec=args.realio_poll_sec)
        print(f"OK batéria id={bid} ({args.name}, {args.mode})")
        _print_fleet()
    elif args.cmd == "enable":
        fleet.set_enabled(args.battery_id, True)
        print(f"OK enabled id={args.battery_id}")
    elif args.cmd == "disable":
        fleet.set_enabled(args.battery_id, False)
        print(f"OK disabled id={args.battery_id}")


if __name__ == "__main__":
    main()
