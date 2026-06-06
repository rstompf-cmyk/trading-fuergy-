# -*- coding: utf-8 -*-
"""
okte_vdt_backfill.py — univerzálny od-do backfill VDT a OKTE public dát.

Stiahne pre zvolený rozsah dátumov tieto kindy:
  • trades         — vlastné spárované obchody (REST /idm/trades, cert auth)
  • eval_daily     — denné vyhodnotenie per 15-min (REST /idm/evaluations/daily-detail)
  • dam            — DAM clearing ceny (OKTE public /weboard/dayahead)
  • vdt_15min      — VDT 15-min aukcie (OKTE public /weboard/intraday)

CSV výstupy idú do out/sk/vdt_history/<kind>.csv (UPSERT podľa kľúča).

Volá sa zo /vdt/backfill_range endpointu cez POST formulár.
"""
from __future__ import annotations
import os
import datetime as dt
from typing import Optional, List, Dict, Any, Callable
import json
import pandas as pd


def _data_dir() -> str:
    """VDT history je SK-specific (OKTE = slovenský operátor)."""
    try:
        import market as _mk
        root = os.path.dirname(_mk.data_dir().rstrip("/").rstrip(os.sep)) or "out"
        return os.path.join(root, "sk", "vdt_history")
    except Exception:
        return os.path.join("out", "sk", "vdt_history")


def _csv_path(kind: str) -> str:
    """Mapuje kind na CSV cestu. kind ∈ {trades, eval_daily, dam, vdt_15min}."""
    return os.path.join(_data_dir(), f"{kind}.csv")


def _ensure_dir() -> None:
    os.makedirs(_data_dir(), exist_ok=True)


def _normalize_response(resp: Dict[str, Any], default_keys: List[str]) -> List[Dict[str, Any]]:
    """OKTE REST vracia rôzne shape-y (list, {'data': list}, {'items': list}).
    Normalizuje na list of dicts; pri prázdnom alebo error response vráti []."""
    if not resp or not resp.get("ok"):
        return []
    data = resp.get("data")
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # skús známe wrapping kľúče
        for k in default_keys + ["data", "items", "result", "trades", "evaluations"]:
            if k in data and isinstance(data[k], list):
                return data[k]
        # ak je dict scalar, wrap do list
        return [data]
    return []


def _upsert_csv(csv_path: str, new_rows: List[Dict[str, Any]],
                  key_cols: List[str]) -> Dict[str, int]:
    """Vloží nové riadky do CSV s deduplikáciou podľa key_cols (UPSERT — nové prepíšu staré).
    Vracia {'before': N, 'after': N, 'added': N}."""
    if not new_rows:
        return {"before": 0, "after": 0, "added": 0}
    _ensure_dir()
    new_df = pd.DataFrame(new_rows)
    # Zabezpeč že key_cols existujú v new_df (skip ak nie)
    have_keys = [k for k in key_cols if k in new_df.columns]
    if not have_keys:
        # bez kľúča — len append
        if os.path.exists(csv_path):
            old = pd.read_csv(csv_path)
            combined = pd.concat([old, new_df], ignore_index=True)
        else:
            combined = new_df
        before = len(combined) - len(new_df)
        combined.to_csv(csv_path, index=False)
        return {"before": before, "after": len(combined), "added": len(new_df)}
    # S kľúčom — UPSERT
    if os.path.exists(csv_path):
        try:
            old = pd.read_csv(csv_path)
            before = len(old)
            combined = pd.concat([old, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=have_keys, keep="last")
        except Exception:
            combined = new_df
            before = 0
    else:
        combined = new_df
        before = 0
    combined.to_csv(csv_path, index=False)
    return {"before": before, "after": len(combined), "added": len(combined) - before}


# ─── Per-kind fetchery ──────────────────────────────────────────────────────

def fetch_trades_day(d: dt.date) -> Dict[str, Any]:
    """Vlastné trades pre 1 deň. Vracia {'ok': bool, 'rows': int, 'msg': str}."""
    try:
        import okte_vdt as _vdt
    except Exception as e:
        return {"ok": False, "rows": 0, "msg": f"okte_vdt import fail: {e}"}
    next_d = d + dt.timedelta(days=1)
    resp = _vdt.get_trades(
        delivery_from=f"{d.isoformat()}T00:00:00Z",
        delivery_to=f"{next_d.isoformat()}T00:00:00Z",
    )
    if not resp.get("ok"):
        return {"ok": False, "rows": 0, "msg": resp.get("error", f"HTTP {resp.get('status')}")}
    rows = _normalize_response(resp, ["trades"])
    # Pridaj date column pre filter neskôr
    for r in rows:
        r["_fetch_date"] = d.isoformat()
    res = _upsert_csv(_csv_path("trades"), rows, key_cols=["id", "tradeId", "trade_id"])
    return {"ok": True, "rows": len(rows), "msg": f"+{res['added']} new (total {res['after']})"}


def fetch_eval_daily_day(d: dt.date) -> Dict[str, Any]:
    """Detailné denné vyhodnotenie per 15-min pre 1 deň."""
    try:
        import okte_vdt as _vdt
    except Exception as e:
        return {"ok": False, "rows": 0, "msg": f"okte_vdt import fail: {e}"}
    next_d = d + dt.timedelta(days=1)
    resp = _vdt.get_evaluations_daily_detail(
        delivery_from=f"{d.isoformat()}T00:00:00Z",
        delivery_to=f"{next_d.isoformat()}T00:00:00Z",
    )
    if not resp.get("ok"):
        return {"ok": False, "rows": 0, "msg": resp.get("error", f"HTTP {resp.get('status')}")}
    rows = _normalize_response(resp, ["evaluations", "details"])
    for r in rows:
        r["_fetch_date"] = d.isoformat()
    res = _upsert_csv(_csv_path("eval_daily"), rows,
                       key_cols=["deliveryFrom", "deliveryStart", "interval"])
    return {"ok": True, "rows": len(rows), "msg": f"+{res['added']} new"}


def fetch_dam_day(d: dt.date) -> Dict[str, Any]:
    """DAM clearing ceny — OKTE public, žiadny cert nepotrebuje."""
    try:
        import okte_sk as _osk
    except Exception as e:
        return {"ok": False, "rows": 0, "msg": f"okte_sk import fail: {e}"}
    try:
        df = _osk.fetch_okte_dayahead(d)
    except Exception as e:
        return {"ok": False, "rows": 0, "msg": f"fetch fail: {str(e)[:120]}"}
    if df is None or df.empty:
        return {"ok": False, "rows": 0, "msg": "OKTE nemá dáta (ešte nie publikované?)"}
    rows = df.assign(date=d.isoformat()).to_dict("records")
    res = _upsert_csv(_csv_path("dam"), rows, key_cols=["date", "interval", "hour"])
    return {"ok": True, "rows": len(rows), "msg": f"+{res['added']} new"}


def fetch_vdt_15min_day(d: dt.date) -> Dict[str, Any]:
    """VDT 15-min aukčné ceny — OKTE public intraday."""
    try:
        import okte_sk as _osk
    except Exception as e:
        return {"ok": False, "rows": 0, "msg": f"okte_sk import fail: {e}"}
    try:
        df = _osk.fetch_okte_intraday(d)
    except Exception as e:
        return {"ok": False, "rows": 0, "msg": f"fetch fail: {str(e)[:120]}"}
    if df is None or df.empty:
        return {"ok": False, "rows": 0, "msg": "OKTE nemá dáta"}
    rows = df.assign(date=d.isoformat()).to_dict("records")
    res = _upsert_csv(_csv_path("vdt_15min"), rows, key_cols=["date", "interval"])
    return {"ok": True, "rows": len(rows), "msg": f"+{res['added']} new"}


# ─── Hlavný range backfill ──────────────────────────────────────────────────

KIND_FETCHERS: Dict[str, Callable[[dt.date], Dict[str, Any]]] = {
    "trades": fetch_trades_day,
    "eval_daily": fetch_eval_daily_day,
    "dam": fetch_dam_day,
    "vdt_15min": fetch_vdt_15min_day,
}


def backfill_range(date_from: str, date_to: str, kinds: List[str],
                    log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Stiahne dáta zvolených kindov pre rozsah [date_from, date_to] (inkluzívne).

    Args:
        date_from, date_to: ISO date strings (YYYY-MM-DD)
        kinds: zoznam z {trades, eval_daily, dam, vdt_15min}
        log: optional callback pre progress (jeden riadok per kind+deň)

    Returns dict s per-deň + per-kind výsledkami.
    """
    if log is None:
        log = print
    try:
        d_from = dt.date.fromisoformat(date_from)
        d_to = dt.date.fromisoformat(date_to)
    except ValueError as e:
        return {"ok": False, "error": f"Zlý dátum: {e}"}
    if d_from > d_to:
        return {"ok": False, "error": "date_from > date_to"}
    valid = [k for k in kinds if k in KIND_FETCHERS]
    if not valid:
        return {"ok": False, "error": f"Žiadny platný kind. Povolené: {list(KIND_FETCHERS)}"}
    days = []
    cur = d_from
    while cur <= d_to:
        days.append(cur)
        cur += dt.timedelta(days=1)
    log(f"▸ Backfill {len(days)} dní × {len(valid)} kindov = {len(days)*len(valid)} fetchov")
    results = {"days": [], "summary": {k: {"ok": 0, "fail": 0, "rows": 0} for k in valid}}
    for d in days:
        day_res = {"date": d.isoformat(), "kinds": {}}
        for k in valid:
            try:
                r = KIND_FETCHERS[k](d)
            except Exception as e:
                r = {"ok": False, "rows": 0, "msg": f"EXC: {str(e)[:120]}"}
            day_res["kinds"][k] = r
            mark = "✓" if r.get("ok") else "✗"
            log(f"  {mark} {d} {k}: {r.get('msg', '?')}")
            if r.get("ok"):
                results["summary"][k]["ok"] += 1
                results["summary"][k]["rows"] += r.get("rows", 0)
            else:
                results["summary"][k]["fail"] += 1
        results["days"].append(day_res)
    return {"ok": True, "days_total": len(days), "kinds_total": len(valid), **results}


def coverage_summary() -> Dict[str, Any]:
    """Pokrytie každého CSV — dni, prvý/posledný, počet riadkov."""
    out = {}
    for k in KIND_FETCHERS:
        p = _csv_path(k)
        if not os.path.exists(p):
            out[k] = {"exists": False, "rows": 0}
            continue
        try:
            df = pd.read_csv(p)
            date_col = None
            for c in ("date", "_fetch_date", "deliveryFrom", "deliveryStart"):
                if c in df.columns:
                    date_col = c; break
            if date_col is None:
                out[k] = {"exists": True, "rows": len(df), "date_col": None}
                continue
            dates = pd.to_datetime(df[date_col], errors="coerce").dt.date.dropna().unique()
            out[k] = {
                "exists": True,
                "rows": len(df),
                "date_col": date_col,
                "days": len(dates),
                "first": str(min(dates)) if len(dates) else "—",
                "last": str(max(dates)) if len(dates) else "—",
            }
        except Exception as e:
            out[k] = {"exists": True, "rows": 0, "error": str(e)[:80]}
    return out


if __name__ == "__main__":
    print("=== okte_vdt_backfill smoke test ===")
    print("CSV dir:", _data_dir())
    print("Coverage:", json.dumps(coverage_summary(), ensure_ascii=False, indent=2))
