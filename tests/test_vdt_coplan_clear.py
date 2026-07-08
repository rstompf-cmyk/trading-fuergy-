# -*- coding: utf-8 -*-
"""#81 VDT-COPLAN: clear_future_vdt_paper_trades — zmaže len BUDÚCE (>= from_slot) VDT obchody
dnešného dňa, uzavreté (< from_slot) a iné dni NECHÁVA. Rešpektuje VDT-IMMUTABLE (uzavretý
obchod = nemenný).
"""
import os
import csv
import datetime as dt
import vdt_live_advisor as adv


def _write_csv(path, rows):
    header = ["ts", "profile", "slot", "action", "kw", "kwh",
              "price_predicted_eur", "soc_before_pct", "soc_after_pct",
              "soc_source", "profit_eur_rest_of_day"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def _row(ts, prof, slot, action):
    return [ts, prof, slot, action, "100", "25", "120", "50", "51", "vdt", "0"]


def test_clear_future_keeps_closed_and_other_days(tmp_path, monkeypatch):
    prof = "TEST_VW"
    today = dt.date.today().isoformat()
    yday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    p = tmp_path / "vdt_paper_trades.csv"
    rows = [
        _row(f"{today}T08:00:00", prof, "08:00-08:15", "BUY"),    # dnes CLOSED (past) — nechať
        _row(f"{today}T13:00:00", prof, "13:00-13:15", "SELL"),   # dnes FUTURE (>=48) — zmazať
        _row(f"{today}T20:00:00", prof, "20:00-20:15", "BUY"),    # dnes FUTURE — zmazať
        _row(f"{yday}T20:00:00", prof, "20:00-20:15", "SELL"),    # iný deň — nechať
    ]
    _write_csv(p, rows)
    monkeypatch.setattr(adv, "paper_trades_csv_path", lambda profile=None: str(p))
    monkeypatch.setattr(adv, "_is_sk_market", lambda: True)
    monkeypatch.setenv("VDT_COPLAN_CLEAR", "1")

    removed = adv.clear_future_vdt_paper_trades(prof, today, from_slot_idx=48)  # 12:00
    assert removed == 2, f"mali sa zmazať 2 budúce sloty, zmazaných {removed}"

    with open(p, encoding="utf-8") as f:
        left = list(csv.DictReader(f))
    slots_today = sorted(r["slot"] for r in left if r["ts"][:10] == today)
    assert slots_today == ["08:00-08:15"], f"dnes má ostať len CLOSED 08:00, zostalo {slots_today}"
    assert any(r["ts"][:10] == yday for r in left), "iný deň sa nemá dotknúť"


def test_killswitch_off_noop(tmp_path, monkeypatch):
    prof = "TEST_VW"
    today = dt.date.today().isoformat()
    p = tmp_path / "vdt_paper_trades.csv"
    _write_csv(p, [_row(f"{today}T20:00:00", prof, "20:00-20:15", "BUY")])
    monkeypatch.setattr(adv, "paper_trades_csv_path", lambda profile=None: str(p))
    monkeypatch.setattr(adv, "_is_sk_market", lambda: True)
    monkeypatch.setenv("VDT_COPLAN_CLEAR", "0")
    assert adv.clear_future_vdt_paper_trades(prof, today, 0) == 0


if __name__ == "__main__":
    import tempfile, types
    # jednoduchý manuálny beh bez pytest
    d = tempfile.mkdtemp()
    p = os.path.join(d, "vdt_paper_trades.csv")
    today = dt.date.today().isoformat()
    yday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    _write_csv(p, [
        _row(f"{today}T08:00:00", "TEST_VW", "08:00-08:15", "BUY"),
        _row(f"{today}T13:00:00", "TEST_VW", "13:00-13:15", "SELL"),
        _row(f"{today}T20:00:00", "TEST_VW", "20:00-20:15", "BUY"),
        _row(f"{yday}T20:00:00", "TEST_VW", "20:00-20:15", "SELL"),
    ])
    adv.paper_trades_csv_path = lambda profile=None: p
    adv._is_sk_market = lambda: True
    os.environ["VDT_COPLAN_CLEAR"] = "1"
    n = adv.clear_future_vdt_paper_trades("TEST_VW", today, 48)
    print("removed:", n, "(očakávané 2)")
    with open(p, encoding="utf-8") as f:
        for line in f: print("  ", line.strip())
