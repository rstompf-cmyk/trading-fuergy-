# -*- coding: utf-8 -*-
"""
core.caches — TTL/mtime cache pre pomalé operácie (OTE/PVF fetch, model load).

Extrahované z app.py (Fáza 1 refactoringu).

Module-level dicty fungujú ako process-wide cache. Pri reštarte appky in-memory cache
zanikne, ale niektoré (napr. OTE DT) majú aj disk-perzistenciu.
"""
from __future__ import annotations
import os
import json
import datetime as dt
import pandas as pd

import data_sources as ds
from price_model import PriceModel, FEATURES


# ─── Cache dicty + TTL konštanty ────────────────────────────────────────────
_MODEL_CACHE = {"pm": None}
_OTE_CACHE = {}                    # dict[date_iso] → (mtime, df) pre fetch_ote_dayahead
_PVF_CACHE = {}                    # dict[key] → (timestamp, df) pre fetch_pv_forecast
_ISOT_HIST_CACHE = {}              # dict[(target_iso, days)] → (timestamp, df)
_LIVE_FETCH_CACHE = {"ts": 0, "df": None}   # TTL cache pre _livesim_live_minutes
_OTE_TTL_FUTURE = 300              # dnes/zajtra (môže sa meniť): 5 min TTL
_PVF_TTL_FUTURE = 600              # PV forecast pre dnes/zajtra: 10 min TTL
_ISOT_HIST_TTL = 600               # _isot_history cache TTL
_LIVE_FETCH_TTL = 45               # live fetch (OTE+ČEPS+VDT): 45 s


def _model():
    """Načíta PriceModel z disku, pri chybe trénuje nový z out/price_train_2026.csv. Cachuje."""
    if _MODEL_CACHE["pm"] is not None:
        return _MODEL_CACHE["pm"]
    try:
        pm = PriceModel.load("out/price_model.joblib")
        if pm.reg is not None and getattr(pm.reg, "n_features_in_", None) == len(FEATURES):
            _MODEL_CACHE["pm"] = pm
            return pm
    except Exception:
        pass
    pm = PriceModel().fit(pd.read_csv("out/price_train_2026.csv", parse_dates=["time"]))
    _MODEL_CACHE["pm"] = pm
    return pm


def _ote_cache_csv(d: dt.date) -> str:
    """Disk-cache cesta pre DT (CZ OTE day-ahead) — prežije reštart appky aj OTE výpadky."""
    return os.path.join("out", "cache", f"ote_dt_{d.isoformat()}.csv")


def _fetch_ote_cached(d: dt.date) -> pd.DataFrame:
    """Cache fetch_ote_dayahead per dátum.

    Stratégia (DT sa po publikovaní 14:00 D-1 NEMENÍ):
      • In-memory cache: ak je už načítaný „plný deň" (≥20 intervalov), použij ho **navždy**
        — bez ohľadu na TTL.
      • Disk cache (out/cache/ote_dt_<date>.csv): perzistencia cez reštart.
      • Pri network chybe (OTE 503 atď.): vráť posledný známy cache (in-memory alebo disk)
        namiesto vyhodenia výnimky. Hodit chybu môžem len keď nikdy nemáme dáta.
    """
    import time
    key = d.isoformat()
    today = dt.date.today()
    is_future = d >= today
    now = time.time()

    cached = _OTE_CACHE.get(key)
    if cached is not None:
        ts, df = cached
        # ak máme plný deň, nevracajme sa po sieť (DT je finálne po 14:00 D-1)
        if df is not None and len(df) >= 20:
            return df
        # historický deň: cache je permanentná
        if not is_future:
            return df
        # dnes/zajtra ale neúplné → použij ak ešte nie je expirované
        if (now - ts) < _OTE_TTL_FUTURE:
            return df

    # ── pokus načítať z disku (cez reštart appky) ────────────────────────
    if cached is None:
        try:
            _csv = _ote_cache_csv(d)
            if os.path.exists(_csv):
                df_disk = pd.read_csv(_csv)
                if len(df_disk) >= 20:
                    _OTE_CACHE[key] = (now, df_disk)
                    print(f"[OTE cache] {key}: načítané {len(df_disk)} riadkov z disku ({_csv})")
                    return df_disk
        except Exception as _e:
            print(f"[OTE cache] {key}: disk-load zlyhal: {_e}")

    # ── fetch z OTE ──────────────────────────────────────────────────────
    try:
        df = ds.fetch_ote_dayahead(d)
        _OTE_CACHE[key] = (now, df)
        # ulož na disk pre prežitie reštartu
        try:
            os.makedirs(os.path.dirname(_ote_cache_csv(d)), exist_ok=True)
            df.to_csv(_ote_cache_csv(d), index=False)
        except Exception as _e:
            print(f"[OTE cache] {key}: disk-save zlyhal: {_e}")
        return df
    except Exception as _fetch_e:
        # Fallback: vráť čokoľvek čo už máme (in-memory alebo disk)
        if cached is not None and cached[1] is not None:
            print(f"[OTE cache] {key}: fetch zlyhal ({_fetch_e}), vraciam in-memory cache "
                  f"({len(cached[1])} riadkov)")
            return cached[1]
        try:
            _csv = _ote_cache_csv(d)
            if os.path.exists(_csv):
                df_disk = pd.read_csv(_csv)
                print(f"[OTE cache] {key}: fetch zlyhal ({_fetch_e}), vraciam disk cache "
                      f"({len(df_disk)} riadkov)")
                _OTE_CACHE[key] = (now, df_disk)
                return df_disk
        except Exception:
            pass
        # Nikdy sme nedostali dáta — pošli výnimku ďalej
        raise


def _isot_history(target: dt.date, days: int = 8) -> pd.DataFrame:
    """Posledných `days` dní hodinových cien ISOT z OTE (pre výpočet lag/rolling príznakov).
    Cachované per (target, days) na TTL."""
    import time
    key = (target.isoformat(), int(days))
    cached = _ISOT_HIST_CACHE.get(key)
    if cached is not None and (time.time() - cached[0]) < _ISOT_HIST_TTL:
        return cached[1]
    parts = []
    for k in range(days, 0, -1):
        dd = target - dt.timedelta(days=k)
        try:
            parts.append(_fetch_ote_cached(dd))
        except Exception:
            pass
    if not parts:
        out = pd.DataFrame(columns=["time", "isot_eur"])
        _ISOT_HIST_CACHE[key] = (time.time(), out)
        return out
    isot = pd.concat(parts, ignore_index=True)

    def _ts(r):
        hh, mm = r["interval"].split("-")[0].split(":")
        return pd.Timestamp(r["date"]) + pd.Timedelta(hours=int(hh), minutes=int(mm))
    isot["time"] = isot.apply(_ts, axis=1)
    out = (isot.set_index("time")["cena_EUR"].resample("h").mean()
            .rename("isot_eur").reset_index())
    _ISOT_HIST_CACHE[key] = (time.time(), out)
    return out


def _fetch_pv_cached(lat, lon, kwp, tilt, azimuth, eff, start, end) -> pd.DataFrame:
    """Cache PV forecast per (lokácia, dátum). Historické dni → permanent, dnes/zajtra → 10-min TTL.

    Retry logika: pri prechodných chybách (5xx, timeout, connection reset) skúsi 3× s exponenciálnym
    backoff (0.5 s, 1.5 s, 3 s). Ak stále zlyhá a máme stale cache pre rovnaký kľúč, vráti ten
    s upozornením v exception message-i. Inak prepošle pôvodnú chybu hore.

    Pre **minulé dni** (start < today) Open-Meteo a MET Norway sú zbytočné (sú to FORECAST API,
    nemajú historic data). Preskočíme rovno na: plan_store cache → PVGIS TMY. To šetrí
    sekundy/minúty čakania pri /plan_batch cez minulé obdobie a vyhne sa 429 rate limit-u."""
    import time
    key = (round(lat, 4), round(lon, 4), round(kwp, 2), round(tilt, 1), round(azimuth, 1),
           round(eff, 3), str(start), str(end))
    today = dt.date.today()
    try:
        start_date = pd.Timestamp(start).date()
        is_future = start_date > today           # zajtra+ (D+1 a ďalej)
        is_today = start_date == today           # dnes
        is_past = start_date < today             # minulé dni
    except Exception:
        is_future = True
        is_today = False
        is_past = False
    now = time.time()
    cached = _PVF_CACHE.get(key)
    if cached is not None:
        ts, df = cached
        # in-memory cache hit: future (zajtra) má 10-min TTL; dnes/minulé sú permanent
        if not is_future or (now - ts) < _PVF_TTL_FUTURE:
            # Sanity check: ak je kwp>0 ale všetky kw=0, je to stale cache zo zlého plánu — invalidate
            try:
                if float(kwp or 0) > 0.01 and len(df) > 0 and float(df["kw"].max()) < 0.005 * float(kwp):
                    print(f"[_fetch_pv_cached] in-memory cache má max kw={df['kw'].max():.2f} pri kwp={kwp} → INVALIDATE")
                    _PVF_CACHE.pop(key, None)
                else:
                    return df
            except Exception:
                return df

    # ───────────────── DNES: skús plan_store CACHE prvé ─────────────────
    # D-1 plán pre dnešok bol pravdepodobne vygenerovaný včera v noci (scheduler) — netreba
    # znova fetchovať Open-Meteo. Šetrí 1-5s + vyhne sa 429 rate limit.
    if is_today:
        try:
            import plan_store as _ps_t
            sched = _ps_t.load_plan_safe(today.isoformat(), 60, "plan")
            if sched is not None:
                pv_kwh = sched.get("schedule", {}).get("pv_kwh", [])
                # Sanity check: keď kwp>0 ale uložený plán má všetky pv_kwh=0 (legacy stale plán
                # z čias batt-only profilu), neprijať — pokračovať na live forecast
                if pv_kwh and len(pv_kwh) == 24 and float(kwp or 0) > 0.01 and max(pv_kwh) < 0.005 * float(kwp):
                    print(f"[_fetch_pv_cached] plan_store hit ale max pv_kwh={max(pv_kwh):.2f} pri kwp={kwp} → REJECT (stale plán)")
                    pv_kwh = []  # → fallthrough na Open-Meteo
                if pv_kwh and len(pv_kwh) == 24:
                    base = pd.Timestamp(today.isoformat())
                    times = pd.date_range(base, periods=24, freq="1h")
                    df = pd.DataFrame({
                        "time": times,
                        "gti": [float("nan")] * 24,
                        "temp": [25.0] * 24,
                        "cloud": [float("nan")] * 24,
                        "kw": [float(x) for x in pv_kwh],
                        "kwh": [float(x) for x in pv_kwh],
                    })
                    print(f"[_fetch_pv_cached] dnes {today} → plan_store hit (24 h z D-1 plánu)")
                    _PVF_CACHE[key] = (now, df)
                    return df
        except Exception as _ps_t_e:
            print(f"[_fetch_pv_cached] dnes plan_store chyba: {_ps_t_e}")
        # plan_store nemá → fallthrough na Open-Meteo (forecast vie dať aj zvyšok dnešného dňa)

    # ───────────────── MINULÉ DNI: preskoč live forecast, rovno fallback chain ─────────────────
    # Open-Meteo + MET Norway sú forecast API → pre minulé dni vrátia 429 alebo prázdne dáta.
    # Pre minulé dni je správna cesta: (1) plan_store ak existuje → (2) PVGIS TMY (priemerný rok).
    if is_past:
        # (a) plan_store fallback — využíva už vygenerovaný plán daného dňa
        try:
            import plan_store as _ps
            sd = pd.Timestamp(start).date()
            ed = pd.Timestamp(end).date()
            frames_p = []
            for d in pd.date_range(sd, ed, freq="D"):
                diso = d.date().isoformat()
                sch_disk = _ps.load_plan_safe(diso, 60, "plan")
                if sch_disk is None:
                    continue
                sched = sch_disk.get("schedule", {})
                pv_kwh = sched.get("pv_kwh", [])
                if not pv_kwh or len(pv_kwh) != 24:
                    continue
                # Sanity: stale plán (kwp>0, ale pv_kwh=0) preskočiť → fallback na PVGIS TMY
                if float(kwp or 0) > 0.01 and max(pv_kwh) < 0.005 * float(kwp):
                    print(f"[_fetch_pv_cached] past day {diso} plan_store stale (max pv_kwh={max(pv_kwh):.2f}) → fallback")
                    continue
                base = pd.Timestamp(diso)
                times = pd.date_range(base, periods=24, freq="1h")
                frames_p.append(pd.DataFrame({
                    "time": times,
                    "gti": [float("nan")] * 24,
                    "temp": [25.0] * 24,
                    "cloud": [float("nan")] * 24,
                    "kw": [float(x) for x in pv_kwh],
                    "kwh": [float(x) for x in pv_kwh],
                }))
            if frames_p:
                df = pd.concat(frames_p).reset_index(drop=True)
                print(f"[_fetch_pv_cached] past day {start}..{end} → plan_store hit "
                      f"({len(df)} h z {len(frames_p)} dní uložených plánov)")
                _PVF_CACHE[key] = (now, df)
                return df
        except Exception as _ps_e:
            print(f"[_fetch_pv_cached] past day plan_store chyba: {_ps_e}")
        # (b) PVGIS TMY — Typical Meteorological Year (EC JRC, free, no rate limit)
        try:
            if hasattr(ds, "fetch_pv_forecast_pvgis_tmy"):
                df = ds.fetch_pv_forecast_pvgis_tmy(lat, lon, kwp, tilt, azimuth, eff,
                                                      start=start, end=end)
                if not df.empty:
                    print(f"[_fetch_pv_cached] past day {start}..{end} → PVGIS TMY hit "
                          f"({len(df)} h, priemerný rok)")
                    _PVF_CACHE[key] = (now, df)
                    return df
        except Exception as _pg_e:
            print(f"[_fetch_pv_cached] past day PVGIS TMY chyba: {_pg_e}")
        # (c) cross-date stale cache — najbližší dátum
        loc_key_p = (round(lat, 4), round(lon, 4), round(kwp, 2),
                       round(tilt, 1), round(azimuth, 1), round(eff, 3))
        candidates_p = [(k, v) for k, v in _PVF_CACHE.items() if k[:6] == loc_key_p]
        if candidates_p:
            candidates_p.sort(key=lambda kv: kv[1][0], reverse=True)
            _k_p, (_ts_p, _df_p) = candidates_p[0]
            print(f"[_fetch_pv_cached] past day {start} → cross-date stale cache "
                  f"z {_k_p[6]}..{_k_p[7]}")
            return _df_p
        # (d) totálny fail — detailný error
        raise RuntimeError(
            f"PVF pre minulý deň {start}..{end} nedostupný. "
            f"Skúsil som: plan_store (žiadny uložený plán), PVGIS TMY (chyba alebo prázdne), "
            f"cross-date cache (prázdna). "
            f"Fix: vygeneruj plán pre tento deň manuálne v /plan, alebo over PVGIS endpoint."
        )

    # ───────────────── DNES/BUDÚCNOSŤ: live forecast retry loop ─────────────────
    # retry s exponenciálnym backoffom pri prechodných chybách
    # 429 (Too Many Requests) → kratší extra wait aby celá batch nevisela hodinami
    # 5xx / timeout → kratší backoff (~22 sec total)
    last_exc = None
    last_error_type = None    # diagnostika: 429 / 5xx / timeout / other
    backoffs = [0.0, 0.5, 1.5, 3.0, 6.0, 12.0]
    for attempt, delay in enumerate(backoffs):
        if delay > 0:
            time.sleep(delay)
        try:
            df = ds.fetch_pv_forecast(lat, lon, kwp, tilt, azimuth, eff, start=start, end=end)
            _PVF_CACHE[key] = (now, df)
            return df
        except Exception as e:
            last_exc = e
            msg = str(e).lower()
            is_429 = ("429" in msg or "too many requests" in msg)
            transient = is_429 or any(s in msg for s in (
                "502", "503", "504", "timeout", "timed out",
                "connection reset", "connection refused",
                "bad gateway", "service unavailable", "gateway timeout"))
            if is_429:
                last_error_type = "429 (rate limit)"
            elif "timeout" in msg or "timed out" in msg:
                last_error_type = "timeout"
            elif any(s in msg for s in ("502", "503", "504")):
                last_error_type = "5xx server error"
            else:
                last_error_type = type(e).__name__
            if not transient:
                raise
            if is_429 and attempt < len(backoffs) - 1:
                # 429: kratší extra wait 5/10/15/20/20 sec (max 90 sec spolu)
                extra_wait = min(5.0 + 5.0 * attempt, 20.0)
                print(f"[_fetch_pv_cached] 429 rate limit pre {start} "
                      f"(pokus {attempt+1}/{len(backoffs)}), čakám {extra_wait:.0f} s")
                time.sleep(extra_wait)
    # 1) všetky retry zlyhali → skús stale cache pre presne tento (lokácia+dátum) kľúč
    if cached is not None:
        _ts, df = cached
        age_min = (now - _ts) / 60.0
        print(f"[_fetch_pv_cached] PVF API zlyhalo — používam stale cache ({age_min:.0f} min) pre {start}")
        return df
    # 2) cross-date stale cache: nájdi NAJBLIŽŠÍ dátum v cache s rovnakou lokáciou+parametrami
    loc_key = (round(lat, 4), round(lon, 4), round(kwp, 2), round(tilt, 1), round(azimuth, 1), round(eff, 3))
    candidates = [(k, v) for k, v in _PVF_CACHE.items() if k[:6] == loc_key]
    if candidates:
        candidates.sort(key=lambda kv: kv[1][0], reverse=True)
        _k, (_ts, _df) = candidates[0]
        age_min = (now - _ts) / 60.0
        print(f"[_fetch_pv_cached] PVF API zlyhalo + žiadna cache pre {start} → cross-date fallback "
              f"z {_k[6]}..{_k[7]} ({age_min:.0f} min starý). Pozor: iný deň = iné počasie.")
        return _df
    # 3) MET Norway backup provider (free, no key, ~10 dní dopredu)
    try:
        if hasattr(ds, "fetch_pv_forecast_metno"):
            df = ds.fetch_pv_forecast_metno(lat, lon, kwp, tilt, azimuth, eff,
                                             start=start, end=end)
            if not df.empty:
                print(f"[_fetch_pv_cached] PVF Open-Meteo zlyhalo → MET Norway fallback "
                      f"({len(df)} hodín, peak {df['kw'].max():.1f} kW)")
                _PVF_CACHE[key] = (now, df)
                return df
    except Exception as _mn_e:
        print(f"[_fetch_pv_cached] MET Norway fallback chyba: {_mn_e}")
    # 4) plan_store fallback — ak má užívateľ pre daný dátum uložený plán, použij jeho pv_kwh
    #    ako pseudo-PVF. Výhoda: žiadne API, žiadne čakanie, dostupné aj pre úplne nové sessions.
    try:
        import plan_store as _ps
        sd = pd.Timestamp(start).date()
        ed = pd.Timestamp(end).date()
        frames = []
        for d in pd.date_range(sd, ed, freq="D"):
            diso = d.date().isoformat()
            sch_disk = _ps.load_plan_safe(diso, 60, "plan")
            if sch_disk is None:
                continue
            sched = sch_disk.get("schedule", {})
            pv_kwh = sched.get("pv_kwh", [])
            if not pv_kwh or len(pv_kwh) != 24:
                continue
            base = pd.Timestamp(diso)
            times = pd.date_range(base, periods=24, freq="1h")
            frames.append(pd.DataFrame({
                "time": times,
                "gti": [float("nan")] * 24,
                "temp": [25.0] * 24,
                "cloud": [float("nan")] * 24,
                "kw": [float(x) for x in pv_kwh],
                "kwh": [float(x) for x in pv_kwh],
            }))
        if frames:
            df = pd.concat(frames).reset_index(drop=True)
            print(f"[_fetch_pv_cached] PVF API zlyhalo + MET Norway nedostupný → plan_store fallback "
                  f"({len(df)} hodín z {len(frames)} dní uložených plánov)")
            _PVF_CACHE[key] = (now, df)
            return df
    except Exception as _fb_e:
        print(f"[_fetch_pv_cached] plan_store fallback chyba: {_fb_e}")
    # 5) PVGIS TMY fallback — Typical Meteorological Year (EC JRC, žiadny rate limit)
    #    Pre dni v ďalekom budúcnoste alebo keď live forecast trvale zlyháva.
    try:
        if hasattr(ds, "fetch_pv_forecast_pvgis_tmy"):
            df = ds.fetch_pv_forecast_pvgis_tmy(lat, lon, kwp, tilt, azimuth, eff,
                                                  start=start, end=end)
            if not df.empty:
                print(f"[_fetch_pv_cached] PVF Open-Meteo + MET Norway zlyhali → "
                      f"PVGIS TMY fallback ({len(df)} hodín, priemerný rok)")
                _PVF_CACHE[key] = (now, df)
                return df
    except Exception as _pg_e:
        print(f"[_fetch_pv_cached] PVGIS TMY fallback chyba: {_pg_e}")

    # 6) žiadny fallback k dispozícii — detailný error pre user
    err_type = last_error_type or "unknown"
    raise RuntimeError(
        f"PVF nedostupný pre {start}..{end}. "
        f"Skúsil som: (1) Open-Meteo {len(backoffs)}× retry → {err_type}, "
        f"(2) stale cache → prázdna, "
        f"(3) cross-date cache → prázdna, "
        f"(4) MET Norway fallback → nedostupný, "
        f"(5) plan_store fallback → žiadne staré plány, "
        f"(6) PVGIS TMY fallback → nedostupný. "
        f"Status: https://status.open-meteo.com/. "
        f"Tip: počkaj 5-10 min a skús jeden deň znova v /plan."
    )
