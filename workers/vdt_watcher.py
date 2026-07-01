#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workers/vdt_watcher.py — ŽIVÝ VDT watcher ako SAMOSTATNÝ PROCES (10 s cadence).

VDT (SIDC 15-min) je veľmi živé — 15-min scheduler advisor je pomalý. Tento proces
každých VDT_WATCH_SEC (default 10 s):
  1. stiahne best bid/ask na VŠETKÝCH 96 slotoch z OKTE orderbooku (okte_vdt.get_orderbook(15)),
  2. prežene ich cez vdt_pair_matcher.match_pairs → NAJLEPŠIE páry (nákup-slot → predaj-slot)
     na celom obchodovanom rozsahu, voči fyzike batérie profilu (SOC, výkon, účinnosť, poplatky),
  3. odsimuluje ich nákup/predaj (paper) → očakávaný zisk,
  4. zapíše snapshot (out/_status/vdt_watch.json) — stránka /vdt/watch ho číta (read-only, auto 10 s).

READ-ONLY: nezasiela žiadne obchody. Fail-safe: chyba tiku nezhodí proces. Graceful SIGTERM.

Spustenie:
    VDT_WATCH_CONTROL=1 python -m workers.vdt_watcher
    VDT_WATCH_SEC=10 VDT_WATCH_PROFILE="Trakany_real" ...
"""
from __future__ import annotations
import os
import sys
import time
import json
import signal
import datetime as dt
from typing import Optional, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_RUNNING = True
_HI = 1e9    # ask pre slot bez predajcu (nekúpim tam)
_LO = -1e9   # bid pre slot bez kupca (nepredám tam)


def _stop(signum, _frame):
    global _RUNNING
    _RUNNING = False
    print(f"[vdt-watcher] signal {signum} → graceful stop", flush=True)


def _ts() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _snapshot_path() -> str:
    d = os.path.join("out", "_status")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "vdt_watch.json")


def _write_snapshot(rec: dict) -> None:
    try:
        p = _snapshot_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception as e:
        print(f"[vdt-watcher] snapshot zápis zlyhal: {e}", flush=True)


def _slot_period(i: int) -> str:
    h, m = (i // 4) % 24, (i % 4) * 15
    eh, em = ((i + 1) // 4) % 24, ((i + 1) % 4) * 15
    return f"{h:02d}:{m:02d}-{eh:02d}:{em:02d}"


def _best_bid_ask_per_slot(ob: dict):
    """Z get_orderbook(15) → 96× (ask, ask_mw, bid, bid_mw). period 'HH:MM-HH:MM' → slot idx."""
    q = (ob.get("orders") or {}).get("quarterly") or {}
    bids_all, asks_all = q.get("bids") or {}, q.get("asks") or {}
    ask = [None] * 96
    ask_mw = [0.0] * 96
    bid = [None] * 96
    bid_mw = [0.0] * 96
    for i in range(96):
        per = _slot_period(i)
        bl = sorted(bids_all.get(per, []), key=lambda x: -x["eur"])   # bids: najvyššie prvé
        al = sorted(asks_all.get(per, []), key=lambda x: x["eur"])    # asks: najnižšie prvé
        if bl:
            bid[i] = float(bl[0]["eur"]); bid_mw[i] = float(bl[0]["mw"])
        if al:
            ask[i] = float(al[0]["eur"]); ask_mw[i] = float(al[0]["mw"])
    return ask, ask_mw, bid, bid_mw


def _profile_params(profile: str):
    import profiles as _pr
    pd = _pr.load_profile(profile) or {}
    pl = pd.get("plan") or pd or {}
    def g(k, d):
        try:
            return float(pl.get(k, d) or d)
        except Exception:
            return d
    return {
        "mode": pd.get("mode"),
        "batt_kw": g("batt_kw", 1000.0), "batt_kwh": g("batt_kwh", 2000.0),
        "eff_c": g("eff_c", 0.95), "eff_d": g("eff_d", 0.95),
        "grid_fee": g("grid_fee_vdt", g("grid_fee", 22.0)),
        "cycle_cost": g("cycle_cost_vdt", g("cycle_cost", 2.0)),
        "min_spread": g("min_spread", 5.0),
        "soc_min": g("soc_min", 0.05), "soc_max": g("soc_max", 1.0),
        "priority": (pl.get("vdt_pair_priority") or "profit"),
    }


def _tick(profile: str) -> dict:
    import okte_vdt as _ov
    from vdt_pair_matcher import match_pairs

    ob = _ov.get_orderbook(delivery_duration=15)
    rec = {"ts": _ts(), "profile": profile}
    if not ob or not ob.get("ok"):
        rec.update({"ok": False, "error": (ob or {}).get("error", "orderbook nedostupný"),
                    "slots": [], "cycles": [], "profit_eur": 0.0})
        return rec

    ask, ask_mw, bid, bid_mw = _best_bid_ask_per_slot(ob)
    p = _profile_params(profile)
    bkwh = p["batt_kwh"]
    cap_slot = p["batt_kw"] * 0.25                       # kWh/15-min slot
    # ceny pre matcher: chýbajúci ask=HI (nekúpim), chýbajúci bid=LO (nepredám)
    buy_price = [a if a is not None else _HI for a in ask]
    sell_price = [b if b is not None else _LO for b in bid]
    # likvidita orderbooku ako per-slot strop (kWh): MW*0.25*1000
    cap_chg = [min(cap_slot, (m or 0.0) * 0.25 * 1000.0) for m in ask_mw]   # nákup limituje ask MW
    cap_dis = [min(cap_slot, (m or 0.0) * 0.25 * 1000.0) for m in bid_mw]   # predaj limituje bid MW

    try:
        res = match_pairs(
            buy_price, sell_price,
            soc0_kwh=bkwh * 0.5,
            soc_lo_kwh=bkwh * p["soc_min"], soc_hi_kwh=bkwh * p["soc_max"],
            batt_kwh_per_slot=cap_slot,
            eff_c=p["eff_c"], eff_d=p["eff_d"],
            cycle_cost=p["cycle_cost"], grid_fee=p["grid_fee"],
            min_spread=p["min_spread"], priority=str(p["priority"]),
            cap_charge_soc=cap_chg, cap_discharge_soc=cap_dis,
            allow_buyback=False,
        )
    except Exception as e:
        rec.update({"ok": False, "error": f"match_pairs: {e}", "slots": [], "cycles": [], "profit_eur": 0.0})
        return rec

    cycles = []
    for c in (res.get("cycles") or []):
        cs, ds = int(c.get("charge_slot", -1)), int(c.get("discharge_slot", -1))
        cycles.append({
            "buy_slot": cs, "buy_period": _slot_period(cs) if cs >= 0 else "",
            "sell_slot": ds, "sell_period": _slot_period(ds) if ds >= 0 else "",
            "buy_eur": round(ask[cs], 2) if (0 <= cs < 96 and ask[cs] is not None) else None,
            "sell_eur": round(bid[ds], 2) if (0 <= ds < 96 and bid[ds] is not None) else None,
            "soc_kwh": round(float(c.get("soc_kwh", 0.0)), 0),
            "margin_eur_mwh": round(float(c.get("margin_eur_mwh", 0.0)), 1),
            "profit_eur": round(float(c.get("profit_eur", 0.0)), 2),
        })
    slots = [{"slot": i, "period": _slot_period(i),
              "ask": (round(ask[i], 2) if ask[i] is not None else None), "ask_mw": round(ask_mw[i], 1),
              "bid": (round(bid[i], 2) if bid[i] is not None else None), "bid_mw": round(bid_mw[i], 1),
              "spread": (round(bid[i] - ask[i], 2) if (ask[i] is not None and bid[i] is not None) else None)}
             for i in range(96)]
    rec.update({"ok": True, "mode": p["mode"], "min_spread": p["min_spread"],
                "slots": slots, "cycles": cycles,
                "n_cycles": len(cycles), "profit_eur": round(float(res.get("profit_eur", 0.0)), 2)})
    return rec


def run(profile: Optional[str] = None, watch_sec: Optional[float] = None,
        max_ticks: Optional[int] = None) -> None:
    if watch_sec is None:
        watch_sec = float(os.environ.get("VDT_WATCH_SEC", "10"))
    if not profile:
        profile = os.environ.get("VDT_WATCH_PROFILE") or ""
        if not profile:
            try:
                from core.profile_resolver import get_active as _ga
                profile = _ga(None) or ""
            except Exception:
                profile = ""
    if not profile:
        print("[vdt-watcher] chýba profil (VDT_WATCH_PROFILE / aktívny). Končím.", flush=True)
        return

    print(f"[vdt-watcher] štart — profil='{profile}', každých {watch_sec}s", flush=True)
    n = 0
    while _RUNNING:
        t0 = time.time()
        try:
            rec = _tick(profile)
            rec["watch_sec"] = watch_sec
            rec["tick"] = n
            rec["took_s"] = round(time.time() - t0, 2)
            _write_snapshot(rec)
            if rec.get("ok"):
                print(f"[vdt-watcher] {_ts()} tick {n}: {rec.get('n_cycles',0)} párov, "
                      f"zisk {rec.get('profit_eur',0)} € ({rec['took_s']}s)", flush=True)
            else:
                print(f"[vdt-watcher] {_ts()} tick {n}: {rec.get('error')}", flush=True)
        except Exception as e:
            print(f"[vdt-watcher] {_ts()} tick {n} zlyhal (pokračujem): {e}", flush=True)
        n += 1
        if max_ticks is not None and n >= max_ticks:
            break
        sleep_s = max(0.0, watch_sec - (time.time() - t0))
        slept = 0.0
        while _RUNNING and slept < sleep_s:
            time.sleep(min(0.5, sleep_s - slept))
            slept += 0.5
    print(f"[vdt-watcher] zastavený po {n} tickoch", flush=True)


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if os.environ.get("VDT_WATCH_CONTROL", "0") != "1":
        print("[vdt-watcher] VDT_WATCH_CONTROL != 1 → idle. Končím.", flush=True)
        return
    run()


if __name__ == "__main__":
    main()
