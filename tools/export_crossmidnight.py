# -*- coding: utf-8 -*-
"""export_crossmidnight.py — cez-polnočná VDT diagnostika (2026-06-30).

Vytiahne pre profil 2 po sebe idúce dni (N a N+1):
  - plán: kind/step/soc_init_pct, plán koniec SOC (posledný slot), carryover (trace soc_end)
  - VDT obchody oboch dní (večer N + ráno N+1): slot, akcia, kWh, cena, dam_clearing,
    soc_before/after, status, source
  - odchýlka z realizovaného trace per deň (real_grid − plan_grid), top sloty s |dev|

Spustenie v DEV kontajneri:
    python tools/export_crossmidnight.py --profile WV_simulacia_4
    python tools/export_crossmidnight.py --profile WV_simulacia_4 --day 2026-06-15
    python tools/export_crossmidnight.py --db /app/db/data/app.db --profile WV_simulacia_4

Výstup je čistý text — prilep ho späť do chatu.
"""
import argparse, sqlite3, json, gzip, base64, sys, os

def _find_db(arg):
    if arg and os.path.exists(arg):
        return arg
    for p in ("db/data/app.db", "/app/db/data/app.db",
              os.path.join(os.path.dirname(__file__), "..", "db", "data", "app.db")):
        if os.path.exists(p):
            return os.path.abspath(p)
    sys.exit(f"DB nenájdená (skús --db). Hľadal som db/data/app.db a /app/db/data/app.db")

def _decode_trace(payload):
    """gzip+base64 JSON {cols:[...], rows:[[...]]} -> (cols, rows)."""
    try:
        raw = gzip.decompress(base64.b64decode(payload))
        d = json.loads(raw)
        return d.get("cols", []), d.get("rows", [])
    except Exception as e:
        return None, f"decode zlyhal: {e}"

def _slot_idx(slot):
    """slot môže byť int (0..95) alebo 'HH:MM' string → vráti index 0..95."""
    if slot is None:
        return 0
    if isinstance(slot, (int, float)):
        return int(slot)
    s = str(slot)
    if ":" in s:
        try:
            hh, mm = s.split(":")[:2]
            return int(hh) * 4 + int(mm) // 15
        except Exception:
            return 0
    try:
        return int(float(s))
    except Exception:
        return 0

def _col(cols, *names):
    for n in names:
        if n in cols:
            return cols.index(n)
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--profile", required=True, help="názov profilu (presne ako v DB)")
    ap.add_argument("--day", default=None, help="deň N (YYYY-MM-DD); N+1 sa doplní. Bez neho = auto.")
    args = ap.parse_args()

    dbp = _find_db(args.db)
    c = sqlite3.connect(dbp)
    print(f"# DB: {dbp}")

    row = c.execute("select id,name,mode from profile where name=?", (args.profile,)).fetchone()
    if not row:
        names = [r[0] for r in c.execute("select name from profile order by name")]
        sys.exit(f"Profil '{args.profile}' nenájdený. Dostupné: {names}")
    pid, pname, pmode = row
    print(f"# PROFIL: {pname} (id={pid}, mode={pmode})")

    # market profilu (z plan tabuľky)
    mk = c.execute("select market from plan where profile_id=? order by date desc limit 1", (pid,)).fetchone()
    market = mk[0] if mk else "?"
    print(f"# MARKET: {market}")

    # dni s VDT obchodmi
    trade_days = [r[0] for r in c.execute(
        "select distinct date from vdt_paper_trade where profile_id=? order by date", (pid,))]
    if not trade_days:
        print("# (žiadne VDT obchody pre tento profil)")
    # vyber 2 po sebe idúce dni
    import datetime as dt
    def nextday(s):
        try:
            return (dt.date.fromisoformat(str(s)[:10]) + dt.timedelta(days=1)).isoformat()
        except Exception:
            return None
    trade_days = [s for s in trade_days if nextday(s) is not None]  # odfiltruj pokazené dátumy
    if args.day:
        dN, dN1 = args.day, nextday(args.day)
    else:
        # posledná dvojica kde N aj N+1 majú obchody
        dN = dN1 = None
        for s in reversed(trade_days):
            if nextday(s) in trade_days:
                dN, dN1 = s, nextday(s); break
        if dN is None:
            dN = trade_days[-1] if trade_days else None
            dN1 = nextday(dN) if dN else None
    print(f"# DNI: N={dN}  N+1={dN1}\n")

    def plan_info(day):
        p = c.execute("select id,kind,step_min,params from plan where profile_id=? and date=? "
                      "order by generated_at desc limit 1", (pid, day)).fetchone()
        if not p:
            return None
        plan_id, kind, step, params = p
        try:
            pj = json.loads(params) if params else {}
        except Exception:
            pj = {}
        slots = c.execute("select slot_idx,soc_pct,batt_kw,grid_kwh,price_eur from plan_slot "
                          "where plan_id=? order by slot_idx", (plan_id,)).fetchall()
        end_soc = slots[-1][1] if slots else None
        return {"plan_id": plan_id, "kind": kind, "step": step,
                "soc_init_pct": pj.get("soc_init_pct"), "end_soc_pct": end_soc,
                "slots": slots}

    _has_trace = c.execute("select name from sqlite_master where type='table' "
                           "and name='livesim_trace_day'").fetchone() is not None

    def trace_soc_end(day):
        if not _has_trace:
            return None
        r = c.execute("select payload,soc_end,n_rows from livesim_trace_day "
                      "where profile_id=? and day=? order by updated_at desc limit 1",
                      (pid, day)).fetchone()
        if not r:
            return None
        return {"soc_end": r[1], "n_rows": r[2], "payload": r[0]}

    # ── PLÁN + CARRYOVER ──────────────────────────────────────────────
    print("== PLÁN + CARRYOVER (cez polnoc) ==")
    pN, pN1 = plan_info(dN), plan_info(dN1)
    tN, tN1 = trace_soc_end(dN), trace_soc_end(dN1)
    for tag, day, pl, tr in (("N", dN, pN, tN), ("N+1", dN1, pN1, tN1)):
        if pl:
            print(f"  [{tag} {day}] kind={pl['kind']} step={pl['step']} "
                  f"plán_soc_init={pl['soc_init_pct']}% plán_koniec_soc={pl['end_soc_pct']}%")
        else:
            print(f"  [{tag} {day}] (žiadny plán)")
        if tr:
            print(f"        livesim trace soc_end={tr['soc_end']}% (n_rows={tr['n_rows']})")
    # kľúčový gap: carryover N -> štart N+1
    if tN and pN1 and pN1.get("soc_init_pct") is not None and tN.get("soc_end") is not None:
        gap = float(pN1["soc_init_pct"]) - float(tN["soc_end"])
        print(f"  >>> CARRYOVER GAP: plán N+1 soc_init={pN1['soc_init_pct']}% "
              f"vs realita koniec N={tN['soc_end']}%  → rozdiel {gap:+.1f} bod")

    # ── VDT OBCHODY ───────────────────────────────────────────────────
    def dump_trades(day, slot_from=None, slot_to=None, label=""):
        q = ("select slot,action,kwh,price_eur_mwh,dam_clearing_eur_mwh,"
             "soc_before_pct,soc_after_pct,delta_profit_eur,status,source "
             "from vdt_paper_trade where profile_id=? and date=? order by slot")
        rows = c.execute(q, (pid, day)).fetchall()
        # len reálne obchody (nie idle/0 kWh)
        rows = [r for r in rows if str(r[1]).lower() not in ("idle", "", "none")
                and abs(float(r[2] or 0.0)) > 1e-6]
        if slot_from is not None:
            rows = [r for r in rows if slot_from <= _slot_idx(r[0]) <= (slot_to if slot_to is not None else 95)]
        print(f"\n== VDT obchody {label} {day} ({len(rows)}) ==")
        if not rows:
            print("  (žiadne)")
            return
        print("  čas   akcia    kWh    cena  dam_clr  soc_pred soc_po  Δ€   stav  zdroj")
        for (slot, act, kwh, pr, dam, sb, sa, dp, st, src) in rows:
            si = _slot_idx(slot); hh = si // 4; mm = (si % 4) * 15
            print(f"  {hh:02d}:{mm:02d} {str(act):<8} {kwh or 0:>6.0f} "
                  f"{pr or 0:>6.0f} {dam or 0:>7.0f} {sb if sb is not None else -1:>7.1f} "
                  f"{sa if sa is not None else -1:>6.1f} {dp or 0:>5.0f} {str(st):<5} {src}")

    # večer dňa N (slot 64..95 = 16:00–24:00) + ráno N+1 (slot 0..32 = 00:00–08:00)
    dump_trades(dN, 64, 95, label="VEČER N")
    dump_trades(dN1, 0, 32, label="RÁNO N+1")
    # plus celé dni súhrn
    for day in (dN, dN1):
        rows = c.execute("select action,count(*),sum(kwh) from vdt_paper_trade "
                         "where profile_id=? and date=? group by action", (pid, day)).fetchall()
        print(f"\n  súhrn {day}: " + ", ".join(f"{a}={n}× {s or 0:.0f}kWh" for a, n, s in rows))

    # ── ODCHÝLKA z trace ──────────────────────────────────────────────
    print("\n== ODCHÝLKA (real_grid − plan_grid) z realizovaného trace ==")
    for tag, day, tr in (("N", dN, tN), ("N+1", dN1, tN1)):
        if not tr or not tr.get("payload"):
            print(f"  [{tag} {day}] (žiadny trace)")
            continue
        cols, rows = _decode_trace(tr["payload"])
        if cols is None:
            print(f"  [{tag} {day}] {rows}")
            continue
        i_pg = _col(cols, "plan_grid_kwh")
        i_rt = _col(cols, "rt_rev_realistic_min")
        i_ts = _col(cols, "ts", "time")
        i_soc = _col(cols, "soc_pct")
        i_br = _col(cols, "batt_kw_realistic")
        i_pb = _col(cols, "plan_batt_kw")
        # real grid kWh/min: ak nie je priamo, použijeme rt_rev a plan_grid len ako indikátor
        i_rg = _col(cols, "real_grid_kwh", "grid_kwh_real")
        n = len(rows)
        tot_rt = 0.0
        dev_slots = []
        for r in rows:
            if i_rt is not None and r[i_rt] is not None:
                tot_rt += float(r[i_rt])
            if i_pg is not None and i_rg is not None and r[i_pg] is not None and r[i_rg] is not None:
                d = float(r[i_rg]) - float(r[i_pg])
                dev_slots.append((abs(d), r[i_ts] if i_ts is not None else "?", d))
        soc_first = rows[0][i_soc] if (i_soc is not None and n) else None
        soc_last = rows[-1][i_soc] if (i_soc is not None and n) else None
        print(f"  [{tag} {day}] minút={n} RT_rev_realistic_SUM={tot_rt:.1f} € "
              f"soc_first={soc_first} soc_last={soc_last}")
        dev_slots.sort(reverse=True)
        for ad, ts, d in dev_slots[:5]:
            print(f"        top dev: {ts}  {d:+.2f} kWh/min")
    print("\n# HOTOVO — prilep celý tento výstup do chatu.")

if __name__ == "__main__":
    main()
