# -*- coding: utf-8 -*-
"""
vdt_arbitrage.py — Arbitrage decision engine pre VDT batériu.

Identifikuje **profitable buy/sell páry** v 15-min slotoch dnešného dňa.
Zdroje cien:
  1. OKTE IDM results (verejný endpoint) — vol-weighted avg + min/max + objem
  2. OKTE DAM results (D-1 aukcia) — clearing baseline pre celý deň
  3. Vlastné obchody (z okte_vdt.get_trades) — overlay tvojich pozícií

Výpočet profitu per cyklus:
  profit/MWh = sell_price × eff_roundtrip − buy_price − grid_fee − cycle_cost
  eff_roundtrip = eff_c × eff_d (typicky 0.9025)

READ-ONLY — žiadne objednávky sa neposielajú, len analýza dát.
"""
from __future__ import annotations
import datetime as dt
from typing import Dict, Any, List, Optional
import pandas as pd


def build_backtest_snapshot(date: dt.date) -> pd.DataFrame:
    """Postaví snapshot pre historický deň (backtest).

    Primárny zdroj: internal historian CSV (historian_I_WEB_OKTE_VDT_final_15m.csv
    a historian_I_WEB_OKTE_DAM_15m.csv) — tieto sa nabíjajú scheduled jobs cez
    SEPS/historian login a obsahujú reálne OKTE clearing dáta za minulé dni.
    Fallback: live OKTE API fetch_okte_dayahead/intraday (môže zlyhať pre staré dni).

    `price_eur` = VDT final ak existuje (IDM clearing pre daný slot),
                  inak DAM clearing (D-1 aukcia).
    """
    rows = []
    date_iso = date.isoformat()

    # Najprv skús historian CSV (rýchle, offline)
    vdt_map: Dict[str, float] = {}
    dam_map: Dict[str, float] = {}
    try:
        import seps_sk as _seps
        vdt_map = _seps.load_okte_vdt_for_day(date_iso) or {}
        dam_map = _seps.load_okte_dt_for_day(date_iso) or {}
    except Exception:
        pass

    for i in range(96):
        h, m = divmod(i * 15, 60)
        start = dt.datetime.combine(date, dt.time(h, m))
        end = start + dt.timedelta(minutes=15)
        ts_key = start.strftime("%Y-%m-%d %H:%M:%S")
        vdt_val = vdt_map.get(ts_key)
        dam_val = dam_map.get(ts_key)
        price = vdt_val if vdt_val is not None else dam_val
        rows.append({
            "date": date_iso,
            "slot_idx": i,
            "period": f"{h:02d}:{m:02d}-{end.hour:02d}:{end.minute:02d}",
            "start_local": start,
            "end_local": end,
            "is_past": True, "is_live": False,
            "dam_price": dam_val,
            "idm_avg": vdt_val,
            "idm_min": None, "idm_max": None, "idm_volume": 0.0,
            "price_eur": price,
            "price_source": ("VDT" if vdt_val is not None else
                              ("DAM" if dam_val is not None else "?")),
        })
    df = pd.DataFrame(rows)

    # Ak historian nemal nič, fallback na live OKTE API
    if df["price_eur"].notna().sum() == 0:
        try:
            df_live = _build_day_snapshot(date)
            df_live["is_past"] = True
            df_live["is_live"] = False
            return df_live
        except Exception:
            pass
    return df


_SNAP_CACHE = {}   # {date_iso: (mono_ts, df)} — TTL cache drahých OKTE DAM+IDM fetchov


def _build_day_snapshot(date: dt.date) -> pd.DataFrame:
    """Postaví 96-slot DataFrame pre 1 deň s DAM + IDM cenami.

    SNAP-CACHE (2026-07-06): OKTE DAM+IDM fetch je drahý (network + zlyhania pri nepublikovaných
    budúcich dňoch) a volal sa per-profil per-tick (VDT worker: 12 profilov → 19 s/tick, log
    zaplavený „DAM 2026-07-07 prázdna"). Cache per deň: dnešok 60 s (+refresh is_past/is_live),
    ostatné dni 15 min. Live orderbook (bid/ask) sa fetchuje inde → cache clearingu ho neovplyvní."""
    import okte_sk, time as _t
    _k = date.isoformat()
    _now_mono = _t.monotonic()
    _is_today = (date == dt.date.today())
    _c = _SNAP_CACHE.get(_k)
    if _c and (_now_mono - _c[0]) < (60.0 if _is_today else 900.0):
        _cdf = _c[1].copy()
        _n = pd.Timestamp.now()
        _cdf["is_past"] = _cdf["start_local"].apply(lambda s: pd.Timestamp(s) < _n)
        _cdf["is_live"] = _cdf.apply(
            lambda r: pd.Timestamp(r["start_local"]) <= _n < pd.Timestamp(r["end_local"]), axis=1)
        return _cdf
    now = pd.Timestamp.now()

    rows = []
    for i in range(96):
        h, m = divmod(i * 15, 60)
        start = dt.datetime.combine(date, dt.time(h, m))
        end = start + dt.timedelta(minutes=15)
        end_h, end_m = end.hour, end.minute
        rows.append({
            "date": date.isoformat(),
            "slot_idx": i,
            "period": f"{h:02d}:{m:02d}-{end_h:02d}:{end_m:02d}",
            "start_local": start,
            "end_local": end,
            "is_past": (pd.Timestamp(start) < now),
            "is_live": (pd.Timestamp(start) <= now < pd.Timestamp(end)),
            "dam_price": None,
            "idm_avg": None,
            "idm_min": None,
            "idm_max": None,
            "idm_volume": 0.0,
        })
    df = pd.DataFrame(rows)

    # DAM (D-1 clearing per 15-min slot)
    try:
        dam = okte_sk.fetch_okte_dayahead(date)
        if dam is not None and not dam.empty:
            period_col = price_col = None
            if "period" in dam.columns and "price" in dam.columns:
                period_col, price_col = "period", "price"
            elif "interval" in dam.columns and "cena_EUR" in dam.columns:
                period_col, price_col = "interval", "cena_EUR"
            if period_col:
                for _, dr in dam.iterrows():
                    try:
                        per = int(dr[period_col]) - 1
                        if 0 <= per < 96:
                            df.at[per, "dam_price"] = float(dr[price_col])
                    except (ValueError, TypeError):
                        pass
    except Exception as e:
        print(f"[arbitrage._build_day_snapshot {date}] DAM fetch zlyhal: {e}")

    # IDM (vol-weighted clearing)
    try:
        intra = okte_sk.fetch_okte_intraday(date)
        if intra is not None and not intra.empty:
            for _, ir in intra.iterrows():
                try:
                    per = int(ir.get("period") or 0) - 1
                    if 0 <= per < 96:
                        df.at[per, "idm_avg"] = float(ir.get("cena_EUR") or 0)
                        df.at[per, "idm_min"] = float(ir.get("cena_min") or 0)
                        df.at[per, "idm_max"] = float(ir.get("cena_max") or 0)
                        df.at[per, "idm_volume"] = float(ir.get("objem_MWh") or 0)
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        print(f"[arbitrage._build_day_snapshot {date}] IDM fetch zlyhal: {e}")

    df["price_eur"] = df["idm_avg"].fillna(df["dam_price"])
    df["price_source"] = df.apply(
        lambda r: "IDM" if pd.notna(r["idm_avg"]) else ("DAM" if pd.notna(r["dam_price"]) else "?"),
        axis=1
    )
    _SNAP_CACHE[_k] = (_now_mono, df.copy())    # SNAP-CACHE: ulož pre ďalšie ticky/profily
    return df


def get_market_snapshot(date: dt.date, days_ahead: int = 1,
                          from_current_slot: bool = True) -> pd.DataFrame:
    """Multi-day market snapshot — dnes + ďalších N dní.

    Args:
        date: štartovací deň (typicky dnes)
        days_ahead: koľko ďalších dní zahrnúť (1 = dnes + zajtra; 0 = iba dnes)
        from_current_slot: ak True → filter na sloty >= floor(now, 15min)

    Vracia DataFrame s 96 × (1 + days_ahead) riadkami (pred filtrom).
    Po filtri zostane iba aktuálna 15-min a budúce sloty.
    """
    frames = []
    for offset in range(days_ahead + 1):
        d = date + dt.timedelta(days=offset)
        frames.append(_build_day_snapshot(d))
    df = pd.concat(frames, ignore_index=True)

    if from_current_slot:
        now = pd.Timestamp.now()
        # Floor na aktuálnu 15-min (napr. 14:32 → 14:30)
        current_quarter = now.floor("15min").to_pydatetime()
        df = df[df["start_local"] >= current_quarter].copy().reset_index(drop=True)

    return df


def add_own_trades(df: pd.DataFrame, trades_response: Dict[str, Any]) -> pd.DataFrame:
    """Pridá stĺpce own_buy_qty, own_buy_avg, own_sell_qty, own_sell_avg z trades response.

    Mapuje obchody na riadky DataFrame cez (date, slot_idx). Funguje aj pre
    multi-day snapshot (dnes + zajtra), nielen jeden deň.
    """
    df = df.copy()
    df["own_buy_qty"] = 0.0
    df["own_buy_avg"] = None
    df["own_sell_qty"] = 0.0
    df["own_sell_avg"] = None

    if not trades_response or not trades_response.get("ok"):
        return df
    trades = trades_response.get("data") or []
    if not isinstance(trades, list):
        return df

    # Index pre rýchle lookup (date, slot_idx) → row label
    # Použijeme start_local ako primárny match — bezpečný pre rôzne date
    idx_by_start = {}
    for ridx, row in df.iterrows():
        sl = row["start_local"]
        if isinstance(sl, pd.Timestamp):
            sl = sl.to_pydatetime()
        idx_by_start[sl] = ridx

    # Agregácia obchodov per slot
    agg = {}   # row_idx → {buy_qty, buy_val, sell_qty, sell_val}
    for t in trades:
        ds = t.get("deliveryStart")
        if not ds:
            continue
        try:
            t_utc = pd.to_datetime(ds, utc=True)
            t_local = t_utc.tz_convert("Europe/Bratislava").tz_localize(None).to_pydatetime()
            row_idx = idx_by_start.get(t_local)
            if row_idx is None:
                continue   # obchod mimo zobrazeného rozsahu
            direction = (t.get("direction") or "").lower()
            qty = float(t.get("quantity") or 0)
            price = float(t.get("price") or 0)
            a = agg.setdefault(row_idx, {"buy_qty": 0.0, "buy_val": 0.0,
                                           "sell_qty": 0.0, "sell_val": 0.0})
            if direction == "buy":
                a["buy_qty"] += qty
                a["buy_val"] += qty * price
            elif direction == "sell":
                a["sell_qty"] += qty
                a["sell_val"] += qty * price
        except Exception:
            continue

    for ridx, a in agg.items():
        if a["buy_qty"] > 0:
            df.at[ridx, "own_buy_qty"] = a["buy_qty"]
            df.at[ridx, "own_buy_avg"] = a["buy_val"] / a["buy_qty"]
        if a["sell_qty"] > 0:
            df.at[ridx, "own_sell_qty"] = a["sell_qty"]
            df.at[ridx, "own_sell_avg"] = a["sell_val"] / a["sell_qty"]
    return df


def compute_arbitrage_pairs(df: pd.DataFrame, *,
                              batt_kw: float = 500.0,
                              batt_kwh: float = 800.0,
                              eff_c: float = 0.95,
                              eff_d: float = 0.95,
                              grid_fee: float = 22.0,
                              cycle_cost: float = 2.0,
                              min_spread: float = 5.0,
                              future_only: bool = True,
                              top_n: int = 20,
                              max_hold_hours: float = 24.0,
                              include_unprofitable: bool = False) -> List[Dict[str, Any]]:
    """Vyhľadá top N buy→sell párov + diag info.

    include_unprofitable=True → vráti aj páry pod min_spread (užitočné keď chceš
    vidieť best dostupné aj keď žiadne nepasujú prahu).
    """
    eff_rt = eff_c * eff_d
    slot_kwh_cap = batt_kw * 0.25
    soc_total_kwh = batt_kwh * 0.90

    now = pd.Timestamp.now()
    pairs = []
    for _, buy in df.iterrows():
        if future_only and buy["start_local"] < now:
            continue
        bp = buy.get("price_eur")
        if bp is None or pd.isna(bp):
            continue
        for _, sell in df.iterrows():
            if sell["start_local"] <= buy["start_local"]:
                continue
            hold_h = (sell["start_local"] - buy["start_local"]).total_seconds() / 3600.0
            if hold_h > max_hold_hours:
                continue
            sp = sell.get("price_eur")
            if sp is None or pd.isna(sp):
                continue
            profit = sp * eff_rt - bp - grid_fee - cycle_cost
            if not include_unprofitable and profit < min_spread:
                continue
            max_kwh = min(slot_kwh_cap, soc_total_kwh)
            profit_eur = profit * max_kwh / 1000.0
            kwh_into_batt = max_kwh * eff_c
            kwh_from_batt_to_grid = max_kwh * eff_d
            soc_delta_pct = (kwh_into_batt / batt_kwh * 100.0) if batt_kwh > 0 else 0.0
            pairs.append({
                "buy_period":  buy["period"],
                "sell_period": sell["period"],
                "buy_date":    str(buy.get("date") or "")[5:],
                "sell_date":   str(sell.get("date") or "")[5:],
                "buy_start":   buy["start_local"],
                "sell_start":  sell["start_local"],
                "buy_price":   float(bp),
                "sell_price":  float(sp),
                "buy_source":  buy.get("price_source", "?"),
                "sell_source": sell.get("price_source", "?"),
                "profit_per_mwh": float(profit),
                "max_kwh": float(max_kwh),
                "kwh_into_batt": float(kwh_into_batt),
                "kwh_from_batt": float(kwh_from_batt_to_grid),
                "soc_delta_pct": float(soc_delta_pct),
                "profit_eur": float(profit_eur),
                "hold_hours": float(hold_h),
                "is_profitable": bool(profit >= min_spread),
            })
    pairs.sort(key=lambda p: -p["profit_per_mwh"])
    return pairs[:top_n]


def add_orderbook(df: pd.DataFrame, orderbook_response: Dict[str, Any]) -> pd.DataFrame:
    """Pridá stĺpce z live orderbook do snapshot DataFrame.

    Args:
        df: snapshot DataFrame z get_market_snapshot()
        orderbook_response: výstup z okte_vdt.get_orderbook() (dict s 'top_of_book')

    Pridá stĺpce:
        ob_best_bid_eur (€/MWh), ob_best_bid_mw (MW)
        ob_best_ask_eur (€/MWh), ob_best_ask_mw (MW)
        ob_spread_eur, ob_n_bids, ob_n_asks

    Mapping: orderbook period "T00:00-01:00" alebo "00:00-00:15" → start_local hour/min.
    """
    df = df.copy()
    df["ob_best_bid_eur"] = None
    df["ob_best_bid_mw"] = None
    df["ob_best_ask_eur"] = None
    df["ob_best_ask_mw"] = None
    df["ob_spread_eur"] = None
    df["ob_n_bids"] = 0
    df["ob_n_asks"] = 0

    if not orderbook_response or not orderbook_response.get("ok"):
        return df
    tob = orderbook_response.get("top_of_book") or {}

    # Lookup table: (duration, hour, minute) → row index
    idx_by_key = {}
    for ridx, row in df.iterrows():
        start = row["start_local"]
        if hasattr(start, "to_pydatetime"):
            start = start.to_pydatetime()
        # Determine duration from period string ("00:00-00:15" = 15, "00:00-01:00" = 60)
        period = str(row.get("period", ""))
        try:
            parts = period.split("-")
            h1, m1 = parts[0].split(":")
            h2, m2 = parts[1].split(":")
            dur_min = (int(h2) * 60 + int(m2)) - (int(h1) * 60 + int(m1))
        except Exception:
            dur_min = 15
        # Always store 15-min key; for 60-min lookups also store hourly key
        idx_by_key.setdefault(("quarterly", start.hour, start.minute, dur_min), ridx)
        if dur_min == 60:
            idx_by_key.setdefault(("hourly", start.hour, 0, 60), ridx)

    # Iterate orderbook entries
    for bucket in ("hourly", "quarterly"):
        for ob_period, data in (tob.get(bucket) or {}).items():
            # ob_period format: "HH:MM-HH:MM"
            try:
                p_start, p_end = ob_period.split("-")
                h1, m1 = int(p_start.split(":")[0]), int(p_start.split(":")[1])
                h2, m2 = int(p_end.split(":")[0]), int(p_end.split(":")[1])
                dur_min = (h2 * 60 + m2) - (h1 * 60 + m1)
            except Exception:
                continue
            key = (bucket, h1, m1, dur_min)
            ridx = idx_by_key.get(key)
            if ridx is None:
                continue
            bid = data.get("best_bid") or {}
            ask = data.get("best_ask") or {}
            if bid:
                df.at[ridx, "ob_best_bid_eur"] = float(bid.get("eur") or 0)
                df.at[ridx, "ob_best_bid_mw"] = float(bid.get("mw") or 0)
            if ask:
                df.at[ridx, "ob_best_ask_eur"] = float(ask.get("eur") or 0)
                df.at[ridx, "ob_best_ask_mw"] = float(ask.get("mw") or 0)
            df.at[ridx, "ob_spread_eur"] = data.get("spread_eur")
            df.at[ridx, "ob_n_bids"] = int(data.get("n_bids") or 0)
            df.at[ridx, "ob_n_asks"] = int(data.get("n_asks") or 0)
    return df


def compute_arbitrage_pairs_orderbook(df: pd.DataFrame, *,
                                        batt_kw: float = 500.0,
                                        batt_kwh: float = 800.0,
                                        eff_c: float = 0.95,
                                        eff_d: float = 0.95,
                                        grid_fee: float = 22.0,
                                        cycle_cost: float = 2.0,
                                        min_spread: float = 5.0,
                                        top_n: int = 20,
                                        max_hold_hours: float = 24.0,
                                        include_unprofitable: bool = False) -> List[Dict[str, Any]]:
    """Verzia compute_arbitrage_pairs ktorá používa **live orderbook**:
       - Buy @ best_ask (najnižšia ask cena = čo MUSÍM zaplatiť)
       - Sell @ best_bid (najvyššia bid cena = čo DOSTANEM)

    Vyžaduje že df má stĺpce ob_best_bid_eur a ob_best_ask_eur (cez add_orderbook).
    Sloty bez orderbook ponúk sú preskočené.
    """
    eff_rt = eff_c * eff_d
    slot_kwh_cap = batt_kw * 0.25
    soc_total_kwh = batt_kwh * 0.90

    now = pd.Timestamp.now()
    pairs = []
    for _, buy in df.iterrows():
        if buy["start_local"] < now:
            continue
        ask = buy.get("ob_best_ask_eur")
        ask_mw = buy.get("ob_best_ask_mw")
        if ask is None or pd.isna(ask) or not ask_mw or ask_mw <= 0:
            continue   # nemôžem kúpiť ak nikto nepredáva
        for _, sell in df.iterrows():
            if sell["start_local"] <= buy["start_local"]:
                continue
            hold_h = (sell["start_local"] - buy["start_local"]).total_seconds() / 3600.0
            if hold_h > max_hold_hours:
                continue
            bid = sell.get("ob_best_bid_eur")
            bid_mw = sell.get("ob_best_bid_mw")
            if bid is None or pd.isna(bid) or not bid_mw or bid_mw <= 0:
                continue   # nemôžem predať ak nikto neberie
            profit = float(bid) * eff_rt - float(ask) - grid_fee - cycle_cost
            if not include_unprofitable and profit < min_spread:
                continue
            # Limit MW na min(buy_offer, sell_offer, batt_rate)
            batt_mw = batt_kw / 1000.0
            tradable_mw = min(float(ask_mw), float(bid_mw), batt_mw)
            tradable_kwh = tradable_mw * 1000.0 * 0.25   # 15-min slot
            tradable_kwh = min(tradable_kwh, soc_total_kwh)
            profit_eur = profit * tradable_kwh / 1000.0
            # Batéria — koľko energie ide do/von, ako sa zmení SOC
            # Pri nabíjaní: do batérie ide tradable_kwh × eff_c (časť sa stratí)
            kwh_into_batt = tradable_kwh * eff_c
            # Pri vybíjaní: zo siete dostaneme tradable_kwh × eff_d
            kwh_from_batt_to_grid = tradable_kwh * eff_d
            soc_delta_pct = (kwh_into_batt / batt_kwh * 100.0) if batt_kwh > 0 else 0.0
            pairs.append({
                "buy_period":  buy["period"],
                "sell_period": sell["period"],
                "buy_date":    str(buy.get("date") or "")[5:],
                "sell_date":   str(sell.get("date") or "")[5:],
                "buy_start":   buy["start_local"],
                "sell_start":  sell["start_local"],
                "buy_price":   float(ask),
                "sell_price":  float(bid),
                "buy_source":  "orderbook_ask",
                "sell_source": "orderbook_bid",
                "available_mw": float(tradable_mw),
                "profit_per_mwh": float(profit),
                "max_kwh": float(tradable_kwh),
                "kwh_into_batt": float(kwh_into_batt),
                "kwh_from_batt": float(kwh_from_batt_to_grid),
                "soc_delta_pct": float(soc_delta_pct),
                "profit_eur": float(profit_eur),
                "hold_hours": float(hold_h),
                "is_profitable": bool(profit >= min_spread),
            })
    pairs.sort(key=lambda p: -p["profit_per_mwh"])
    return pairs[:top_n]


def snapshot_diag(df: pd.DataFrame) -> Dict[str, Any]:
    """Diagnostika čo máme v snapshote — koľko slotov, koľko cien, zdroje."""
    if df is None or df.empty:
        return {"total": 0, "msg": "Prázdny snapshot — žiadne dni načítané"}
    total = len(df)
    with_price = df["price_eur"].notna().sum()
    idm_count = df["idm_avg"].notna().sum()
    dam_count = df["dam_price"].notna().sum()
    days = sorted(df["date"].unique()) if "date" in df.columns else []
    return {
        "total_slots": int(total),
        "with_price": int(with_price),
        "missing_price": int(total - with_price),
        "idm_slots": int(idm_count),
        "dam_slots": int(dam_count),
        "days": list(days),
    }


def slot_color(price: Optional[float], all_prices: pd.Series) -> str:
    """Vráti CSS color podľa kvartilu ceny."""
    if price is None or pd.isna(price) or all_prices.empty:
        return "#f5f5f5"
    valid = all_prices.dropna()
    if len(valid) < 4:
        return "#ffffff"
    q25 = valid.quantile(0.25)
    q75 = valid.quantile(0.75)
    if price <= q25:
        return "#d4edda"   # zelená — lacné, kúp
    if price >= q75:
        return "#f8d7da"   # červená — drahé, predaj
    return "#fff8e1"       # žltá — neutrál


def get_default_params_from_profile(profile: Optional[str] = None) -> Dict[str, float]:
    """Vráti default batt parametre z aktívneho profilu (alebo všeobecné defaulty).

    Args:
        profile: ak je zadané, použije sa explicitne (override aktívneho).
                 None → resolve cez plan_store.resolve_profile().

    Profile JSON má parametre nested v `p['plan']` sekcii (kľúče ako batt_kw,
    batt_kwh, eff_c, eff_d, grid_fee, cycle_cost, min_spread). Načítame ich
    odtiaľ + fallback na top-level a defaulty.
    """
    defaults = {
        "batt_kw": 500.0, "batt_kwh": 800.0,
        "eff_c": 0.95, "eff_d": 0.95,
        "grid_fee": 22.0, "cycle_cost": 2.0,
        "min_spread": 5.0,
    }
    try:
        import plan_store as _ps
        import profiles as _pr
        prof = _ps.resolve_profile(profile) if profile else _ps.resolve_profile()
        if prof and prof != "default":
            p = _pr.load_profile(prof)
            if p and isinstance(p, dict):
                # Profile JSON: parametre sú v p['plan']
                plan_section = p.get("plan") or {}
                for k in defaults:
                    if k in plan_section:
                        try:
                            defaults[k] = float(plan_section[k])
                        except (ValueError, TypeError):
                            pass
                    elif k in p:
                        # Legacy fallback — niektoré profily môžu mať params hore
                        try:
                            defaults[k] = float(p[k])
                        except (ValueError, TypeError):
                            pass
    except Exception:
        pass
    return defaults
