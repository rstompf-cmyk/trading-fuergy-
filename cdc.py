# -*- coding: utf-8 -*-
"""
cdc.py — Komunikácia s CDC serverom (SK/CZ) pre flotilu batérií.

Analógia k `realio.py` (Trakany Bender dashboard), ale iný backend:
centrálny CDC server s "excel" REST API, ktorý obsluhuje VŠETKY batérie daného
systému/krajiny. Jednotlivé batérie sa líšia len PREFIXOM tagu.

Princíp (požiadavka 2026-06-22):
    • Jedno miesto na krajinu/systém = `out/<market>/cdc_system.json`
      (host, auth, endpointy, tag SUFFIXY rovnaké pre všetky batérie, scale).
    • Každý profil (= batéria) má len svoj PREFIX (napr. "VW-BA", "Muller-SE")
      + vlastné plány/RT/dentrh ako dnes.
    • Reálny tag = <prefix> + <suffix>.

Protokol (zistený z VBA modulu `cdc.bas` v Planovac_Vzor_V51.xlsm):
    READ:  GET  {host}/api/excel/data/read?tag=<TAG>&bt=<DD.MM.YYYY_HH:mm:ss>
                 &et=<DD.MM.YYYY_HH:mm:ss>&step=<sekundy>
           Basic auth (user/pwd).
           Odpoveď JSON: {"values":[{"time":"DD.MM.YYYY HH:mm:ss","value":"123.4"}, ...]}
           (value je string s desatinnou bodkou)
    WRITE: POST {host}/api/excel/data/write
           Content-Type: application/json
           Body: {"<TAG>": [ {"time": "...", "value": <num>} , ... ]}
           Basic auth.

Tagy pre SK CDC (z excelu, 4 read + 2 write, rovnaké pre všetky batérie):
    READ:
      <prefix>_I_EL1_Power_1h                          — spotreba / elektromer (W)
      <prefix>_I_SOL_Power_1h                          — FTV / solár (W)  [nie všetky batérie]
      <prefix>_C_BAT_StoragePower_15m                  — výkon batérie (W)
      <prefix>_C_POW_ThresholdPowerWithoutInv_Actual_1h — prah (W)
    WRITE:
      <prefix>_U_REG_ConsumptionPlan_Manual_1h         — manuálny plán spotreby = setpoint
      <prefix>_U_Regulation_LimitPlan                  — limit regulácie

POZNÁMKA k SOC: excel pre tieto batérie SOC tag neobsahuje. `soc_pct` suffix je
v configu prázdny — doplní sa keď zistíme názov tagu na serveri. Dovtedy SOC vráti None.

POZNÁMKA k škálovaniu: hodnoty zo servera sú vo Wattoch → scale 0.001 (W→kW).
SOC by malo byť % → scale 1.0. (VBA delenie /4 000 000 bola len normalizácia na
zlomok kapacity pre graf, nie prevod na kW.)

Bezpečnosť zápisu: zápis je povolený len ak cfg['control_enabled'] aj cfg['enabled']
a zároveň env FLEET_REAL_WRITE='1' (rovnaký princíp ako control/executor.py). Inak DRY-RUN.
"""
from __future__ import annotations

import os
import json
import datetime as dt
from typing import Dict, List, Optional, Any

import requests

try:
    import pandas as pd
except Exception:  # pandas je v projekte vždy, ale neblokujeme import modulu
    pd = None


# ─── Cesty (per-market, zhodné s ostatnými modulmi) ──────────────────────────
def _data_dir(market: Optional[str] = None) -> str:
    try:
        import market as _mk
        return _mk.data_dir(market)
    except Exception:
        return os.path.join("out", market or "sk")


def _config_path(market: Optional[str] = None) -> str:
    return os.path.join(_data_dir(market), "cdc_system.json")


# ─── Default systémový config (jeden na krajinu/systém) ──────────────────────
# Logické meno → SUFFIX tagu (prefix sa dolepí per batéria).
DEFAULT_TAGS_READ = {
    "load_power_kw":     "_I_EL1_Power_1m",   # spotreba/elektromer 1-min (live)
    "load_power_kw_1h":  "_I_EL1_Power_1h",   # spotreba 1h priemer
    "ftv_power_kw":      "_I_SOL_Power_1h",
    "batt_power_kw":     "_C_BAT_StoragePower_1m",   # výkon batérie 1-min (live)
    "batt_power_kw_15m": "_C_BAT_StoragePower_15m",  # výkon batérie 15-min
    "threshold_kw":      "_C_POW_ThresholdPowerWithoutInv_Actual_1h",
    "batt_soc_pct":      "_I_BMS_SOC_1m",    # SOC instant/real (1-min, %)
    "batt_soc_pct_15m":  "_I_BMS_SOC_15m",   # SOC 15-min (%)
    "reg_output_kw":     "_C_REG_FinalRegulation_output",  # feedback regulačného výkonu (W)
}

DEFAULT_TAGS_WRITE = {
    "cons_plan_kw":    "_U_REG_ConsumptionPlan_Manual_1h",   # setpoint (manuálny plán spotreby)
    "limit_plan_kw":   "_U_Regulation_LimitPlan",            # limit regulácie
    # ── Plánovacie tagy regulácie v čase (per 15-min slot) ──
    # GL = prahová hodnota (threshold), RL = požadovaná hodnota na batérke,
    # SL = rozsah SOC. *_Act = povoliť/zakázať danú časť (true/false).
    # POZN.: presné jednotky/škálovanie doplniť podľa popisu (zatiaľ scale 1.0).
    "reg_gl_act_plan":  "_U_RegLimit_GL_Act_Plan",    # GL aktívne (on/off)
    "reg_gl_min_plan":  "_U_RegLimit_GL_Min_Plan",
    "reg_gl_base_plan": "_U_RegLimit_GL_Base_Plan",
    "reg_gl_max_plan":  "_U_RegLimit_GL_Max_Plan",
    "reg_rl_act_plan":  "_U_RegLimit_RL_Act_Plan",    # RL aktívne (on/off)
    "reg_rl_min_plan":  "_U_RegLimit_RL_Min_Plan",
    "reg_rl_base_plan": "_U_RegLimit_RL_Base_Plan",
    "reg_rl_max_plan":  "_U_RegLimit_RL_Max_Plan",
    "reg_sl_min_plan":  "_U_RegLimit_SL_Min_Plan",    # SOC rozsah dolný
    "reg_sl_max_plan":  "_U_RegLimit_SL_Max_Plan",    # SOC rozsah horný
}

# Logické meno → multiplier (hodnota_servera × scale = kW alebo %).
DEFAULT_SCALE_READ = {
    "load_power_kw":     0.001,   # W → kW
    "load_power_kw_1h":  0.001,
    "ftv_power_kw":      0.001,
    "batt_power_kw":     0.001,
    "batt_power_kw_15m": 0.001,
    "threshold_kw":      0.001,
    "batt_soc_pct":      1.0,     # %
    "batt_soc_pct_15m":  1.0,     # %
    "reg_output_kw":     0.001,   # W → kW
}

# Zápis: kW → hodnota servera. Default ×1000 (kW → W).
DEFAULT_SCALE_WRITE = {
    "cons_plan_kw":    1000.0,
    "limit_plan_kw":   1000.0,
    # plánovacie tagy: zatiaľ bez prepočtu (scale 1.0), kým nepoznáme jednotky
    "reg_gl_act_plan":  1.0, "reg_gl_min_plan": 1.0,
    "reg_gl_base_plan": 1.0, "reg_gl_max_plan": 1.0,
    "reg_rl_act_plan":  1.0, "reg_rl_min_plan": 1.0,
    "reg_rl_base_plan": 1.0, "reg_rl_max_plan": 1.0,
    "reg_sl_min_plan":  1.0, "reg_sl_max_plan": 1.0,
}

DEFAULT_SYSTEM: Dict[str, Any] = {
    "name": "CDC SK",
    "host": "http://192.168.31.30:8088",
    "endpoint_read":  "/api/excel/data/read",
    "endpoint_write": "/api/excel/data/write",
    "username": "admin",
    "password": "admin",
    "verify_ssl": False,
    "timeout_s": 15,
    "step_read_s": 900,            # 900 = 15-min, 3600 = 1h
    "enabled": False,              # master kill switch (read)
    "control_enabled": False,      # write povolené (+ env FLEET_REAL_WRITE=1)
    "tags_read":   dict(DEFAULT_TAGS_READ),
    "tags_write":  dict(DEFAULT_TAGS_WRITE),
    "scale_read":  dict(DEFAULT_SCALE_READ),
    "scale_write": dict(DEFAULT_SCALE_WRITE),
}


def _fresh_default() -> Dict[str, Any]:
    import copy
    return copy.deepcopy(DEFAULT_SYSTEM)


def load_system_config(market: Optional[str] = None) -> Dict[str, Any]:
    """Načíta systémový config pre danú krajinu (alebo aktívny trh).
    Pri chýbajúcom/poškodenom súbore vráti default."""
    p = _config_path(market)
    if not os.path.exists(p):
        return _fresh_default()
    try:
        with open(p) as fh:
            d = json.load(fh)
        out = _fresh_default()
        out.update({k: v for k, v in d.items() if k not in
                    ("tags_read", "tags_write", "scale_read", "scale_write")})
        for key, default in (("tags_read", DEFAULT_TAGS_READ),
                             ("tags_write", DEFAULT_TAGS_WRITE),
                             ("scale_read", DEFAULT_SCALE_READ),
                             ("scale_write", DEFAULT_SCALE_WRITE)):
            if isinstance(d.get(key), dict):
                out[key] = {**default, **d[key]}
        return out
    except (OSError, json.JSONDecodeError):
        return _fresh_default()


def save_system_config(cfg: Dict[str, Any], market: Optional[str] = None) -> None:
    """Atomické uloženie systémového configu."""
    base = _data_dir(market)
    os.makedirs(base, exist_ok=True)
    p = _config_path(market)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


# ─── Poskladanie tagov z prefixu ─────────────────────────────────────────────
def resolve_read_tags(prefix: str, cfg: Optional[Dict[str, Any]] = None,
                      market: Optional[str] = None) -> Dict[str, str]:
    """Vráti {logical: '<prefix><suffix>'} pre read tagy (prázdne suffixy preskočí)."""
    cfg = cfg or load_system_config(market)
    return {k: f"{prefix}{suf}" for k, suf in (cfg.get("tags_read") or {}).items() if suf}


def resolve_write_tags(prefix: str, cfg: Optional[Dict[str, Any]] = None,
                       market: Optional[str] = None) -> Dict[str, str]:
    """Vráti {logical: '<prefix><suffix>'} pre write tagy."""
    cfg = cfg or load_system_config(market)
    return {k: f"{prefix}{suf}" for k, suf in (cfg.get("tags_write") or {}).items() if suf}


# ─── HTTP klient ─────────────────────────────────────────────────────────────
def _session(cfg: Dict[str, Any]) -> requests.Session:
    s = requests.Session()
    s.auth = (cfg.get("username") or "admin", cfg.get("password") or "admin")
    s.headers.update({
        "User-Agent": "FTV-Aplikacia-CDC/1.0",
        "Content-type": "application/json",
    })
    s.verify = bool(cfg.get("verify_ssl", False))
    return s


def _fmt_dt(d: dt.datetime) -> str:
    """DD.MM.YYYY_HH:mm:ss — formát ktorý očakáva CDC read endpoint (z VBA)."""
    return d.strftime("%d.%m.%Y_%H:%M:%S")


def _parse_time(s: str) -> Optional[dt.datetime]:
    """'DD.MM.YYYY HH:mm:ss' (alebo s '_') → datetime."""
    if not s:
        return None
    s = s.strip().replace("_", " ")
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _read_tag_raw(cfg: Dict[str, Any], s: requests.Session, tag: str,
                  bt: dt.datetime, et: dt.datetime, step: int) -> List[Dict[str, Any]]:
    """Jeden GET read pre jeden tag. Vráti list {'time': datetime, 'value': float}."""
    host = (cfg.get("host") or "").rstrip("/")
    path = cfg.get("endpoint_read") or "/api/excel/data/read"
    url = (f"{host}{path}?tag={tag}"
           f"&bt={_fmt_dt(bt)}&et={_fmt_dt(et)}&step={int(step)}")
    r = s.get(url, timeout=float(cfg.get("timeout_s", 15)))
    r.raise_for_status()
    data = r.json()
    values = data.get("values", []) if isinstance(data, dict) else []
    out: List[Dict[str, Any]] = []
    for item in values:
        v = item.get("value")
        t = item.get("time")
        fv = None
        if v is not None and str(v).strip() != "":
            try:
                fv = float(str(v).replace(",", "."))
            except ValueError:
                fv = None
        out.append({"time": _parse_time(str(t)) if t else None, "value": fv})
    return out


# ─── Verejné READ API ────────────────────────────────────────────────────────
def fetch_latest(prefix: str, market: Optional[str] = None,
                 cfg: Optional[Dict[str, Any]] = None,
                 lookback_min: int = 120) -> Optional[Dict[str, Optional[float]]]:
    """Stiahne POSLEDNÚ hodnotu pre všetky read tagy danej batérie (prefix).
    Aplikuje scale. Vracia {logical: kW/%} + '_ts', alebo None ak modul disabled.
    """
    cfg = cfg or load_system_config(market)
    if not cfg.get("enabled"):
        return None
    tags = resolve_read_tags(prefix, cfg)
    if not tags:
        return {}
    scale = cfg.get("scale_read") or {}
    step = int(cfg.get("step_read_s", 900))
    et = dt.datetime.now()
    bt = et - dt.timedelta(minutes=max(lookback_min, step // 60 + 1))
    s = _session(cfg)
    out: Dict[str, Optional[float]] = {}
    for logical, tag in tags.items():
        try:
            rows = _read_tag_raw(cfg, s, tag, bt, et, step)
        except Exception as e:
            out[logical] = None
            out.setdefault("_errors", {})[logical] = str(e)  # type: ignore
            continue
        # posledná nenulová hodnota
        last = None
        for row in rows:
            if row["value"] is not None:
                last = row["value"]
        out[logical] = (last * float(scale.get(logical, 1.0))) if last is not None else None
    out["_ts"] = dt.datetime.now().isoformat(timespec="seconds")
    return out


def fetch_history_range(prefix: str, from_dt: dt.datetime, to_dt: dt.datetime,
                        step: Optional[int] = None, market: Optional[str] = None,
                        cfg: Optional[Dict[str, Any]] = None):
    """História pre jednu batériu (prefix) v rozsahu. Vracia pandas.DataFrame
    indexovaný časom so stĺpcami = logické názvy (kW/%). Per-tag fetch (odolné)."""
    if pd is None:
        raise RuntimeError("pandas nie je dostupné")
    cfg = cfg or load_system_config(market)
    tags = resolve_read_tags(prefix, cfg)
    scale = cfg.get("scale_read") or {}
    step = int(step or cfg.get("step_read_s", 900))
    s = _session(cfg)
    frames = []
    for logical, tag in tags.items():
        try:
            rows = _read_tag_raw(cfg, s, tag, from_dt, to_dt, step)
        except Exception as e:
            print(f"[cdc.fetch_history_range] {tag} zlyhal: {e}")
            continue
        sc = float(scale.get(logical, 1.0))
        recs = {r["time"]: (r["value"] * sc if r["value"] is not None else None)
                for r in rows if r["time"] is not None}
        if recs:
            frames.append(pd.Series(recs, name=logical))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, axis=1).sort_index()
    df.index.name = "time"
    return df


# ─── Verejné WRITE API ───────────────────────────────────────────────────────
def _minute_aligned(now: Optional[dt.datetime] = None) -> dt.datetime:
    n = now or dt.datetime.now()
    return n.replace(second=0, microsecond=0)


def _real_write_enabled(cfg: Dict[str, Any]) -> bool:
    """Skutočný zápis len ak enabled + control_enabled + env FLEET_REAL_WRITE=1."""
    return (bool(cfg.get("enabled")) and bool(cfg.get("control_enabled"))
            and os.environ.get("FLEET_REAL_WRITE") == "1")


def write_value(prefix: str, logical: str, value_kw: float,
                market: Optional[str] = None, cfg: Optional[Dict[str, Any]] = None,
                source: str = "manual",
                ts: Optional[dt.datetime] = None) -> Dict[str, Any]:
    """Zapíše jednu hodnotu (kW) do write tagu danej batérie.
    logical ∈ {'cons_plan_kw','limit_plan_kw'}. DRY-RUN ak nie je povolený reálny zápis.
    """
    cfg = cfg or load_system_config(market)
    wtags = resolve_write_tags(prefix, cfg)
    tag = wtags.get(logical)
    out: Dict[str, Any] = {"ok": False, "dry_run": True, "prefix": prefix,
                           "logical": logical, "tag": tag, "value_kw": value_kw,
                           "source": source}
    if not tag:
        out["error"] = f"write tag '{logical}' nie je nakonfigurovaný"
        return out
    scale = float((cfg.get("scale_write") or {}).get(logical, 1000.0))
    server_val = value_kw * scale
    when = _minute_aligned(ts)
    payload = {tag: [{"time": _fmt_dt(when).replace("_", " "), "value": server_val}]}
    out["payload"] = payload

    if not _real_write_enabled(cfg):
        # DRY-RUN: nič nepošleme (chýba enabled/control_enabled alebo FLEET_REAL_WRITE!=1)
        out["ok"] = True
        out["note"] = "DRY-RUN (zapnúť enabled+control_enabled a FLEET_REAL_WRITE=1)"
        return out

    host = (cfg.get("host") or "").rstrip("/")
    path = cfg.get("endpoint_write") or "/api/excel/data/write"
    s = _session(cfg)
    try:
        r = s.post(f"{host}{path}", data=json.dumps(payload),
                   timeout=float(cfg.get("timeout_s", 15)))
        out["status"] = r.status_code
        out["response"] = r.text[:500]
        r.raise_for_status()
        out["ok"] = True
        out["dry_run"] = False
    except Exception as e:
        out["ok"] = False
        out["dry_run"] = False
        out["error"] = str(e)
    return out


def write_setpoint(prefix: str, value_kw: float, market: Optional[str] = None,
                   source: str = "manual") -> Dict[str, Any]:
    """Setpoint batérie = zápis do cons_plan tagu (manuálny plán spotreby).
    Konvencia znamienka sa doladí pri commissioningu (predbežne +kW = nabíjanie/odber)."""
    return write_value(prefix, "cons_plan_kw", value_kw, market=market, source=source)


def write_series(prefix: str, logical: str, series: List, market: Optional[str] = None,
                 cfg: Optional[Dict[str, Any]] = None, source: str = "plan",
                 apply_scale: bool = False) -> Dict[str, Any]:
    """Zapíše ČASOVÚ SÉRIU do jedného write tagu (jeden POST). `series` = list
    (datetime, value). Pre plánovacie tagy (GL/RL/SL) apply_scale=False (hodnoty
    sa posielajú tak ako sú). DRY-RUN ak nie je povolený reálny zápis."""
    cfg = cfg or load_system_config(market)
    wtags = resolve_write_tags(prefix, cfg)
    tag = wtags.get(logical)
    out: Dict[str, Any] = {"ok": False, "dry_run": True, "prefix": prefix,
                           "logical": logical, "tag": tag, "n": len(series), "source": source}
    if not tag:
        out["error"] = f"write tag '{logical}' nie je nakonfigurovaný"
        return out
    scale = float((cfg.get("scale_write") or {}).get(logical, 1.0)) if apply_scale else 1.0
    payload = {tag: [{"time": _fmt_dt(t).replace("_", " "), "value": v * scale}
                     for (t, v) in series]}
    out["payload_sample"] = payload[tag][:3]
    if not _real_write_enabled(cfg):
        out["ok"] = True
        out["note"] = "DRY-RUN (zapnúť enabled+control_enabled a FLEET_REAL_WRITE=1)"
        return out
    host = (cfg.get("host") or "").rstrip("/")
    path = cfg.get("endpoint_write") or "/api/excel/data/write"
    s = _session(cfg)
    try:
        r = s.post(f"{host}{path}", data=json.dumps(payload),
                   timeout=float(cfg.get("timeout_s", 15)))
        out["status"] = r.status_code
        r.raise_for_status()
        out["ok"] = True
        out["dry_run"] = False
    except Exception as e:
        out["ok"] = False
        out["dry_run"] = False
        out["error"] = str(e)
    return out


def write_band_table(prefix: str, rows: List[Dict[str, Any]], day_iso: str,
                     market: Optional[str] = None, cfg: Optional[Dict[str, Any]] = None,
                     source: str = "reg_plan") -> Dict[str, Any]:
    """Zapíše celú 15-min tabuľku regulačných pásiem (z cdc_reg_plan.build_band_table)
    — každý band tag ako časová séria (jeden POST/tag). DRY-RUN ak nie je povolené.
    `rows` musia mať kľúče 'time' (HH:MM) + band hodnoty; mapovanie cez BAND_TO_TAG."""
    from cdc_reg_plan import BAND_TO_TAG
    cfg = cfg or load_system_config(market)
    try:
        base_day = dt.datetime.strptime(day_iso, "%Y-%m-%d")
    except ValueError:
        base_day = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    results: Dict[str, Any] = {"dry_run": True, "tags": {}}
    any_real = False
    for band_key, logical in BAND_TO_TAG.items():
        series = []
        for row in rows:
            if band_key not in row:
                continue
            hh, mm = (row.get("time") or "00:00").split(":")
            ts = base_day.replace(hour=int(hh), minute=int(mm))
            series.append((ts, float(row[band_key])))
        if not series:
            continue
        res = write_series(prefix, logical, series, cfg=cfg, source=source)
        results["tags"][logical] = {"ok": res.get("ok"), "n": res.get("n"),
                                    "dry_run": res.get("dry_run"), "tag": res.get("tag"),
                                    "error": res.get("error")}
        if not res.get("dry_run"):
            any_real = True
    results["dry_run"] = not any_real
    return results


# ─── Diagnostika ─────────────────────────────────────────────────────────────
def diagnose(prefix: str, market: Optional[str] = None) -> Dict[str, Any]:
    """Rýchla diagnostika: config + poskladané tagy + skúšobný read."""
    cfg = load_system_config(market)
    info: Dict[str, Any] = {
        "market": market or _active_market(),
        "host": cfg.get("host"),
        "enabled": cfg.get("enabled"),
        "control_enabled": cfg.get("control_enabled"),
        "read_tags": resolve_read_tags(prefix, cfg),
        "write_tags": resolve_write_tags(prefix, cfg),
    }
    if cfg.get("enabled"):
        info["latest"] = fetch_latest(prefix, cfg=cfg)
    else:
        info["latest"] = "modul disabled (cfg.enabled=False)"
    return info


def _active_market() -> str:
    try:
        import market as _mk
        return _mk.get_active_market()
    except Exception:
        return "sk"
