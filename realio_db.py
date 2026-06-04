# -*- coding: utf-8 -*-
"""
realio_db.py — SQLite úložisko pre realio merania.

Prečo SQLite namiesto CSV:
  • Per-stĺpec UPSERT (`INSERT ... ON CONFLICT DO UPDATE`) — pri history backfill
    pre 1 tag sa NEPREPÍŠU ostatné stĺpce na NULL. CSV pristup vždy napísal celý
    riadok, čo viedlo k 90% NaN-och pri per-tag fetch.
  • Atomické writes (jeden file lock cez sqlite3) — žiadne race condition keď
    polling job aj history backfill zapisujú súčasne.
  • Rýchle range query cez time_ms index (radix sort) — žiadne `read_csv` celého
    súboru pri `read_recent(120 min)`.
  • Jeden súbor `realio_measurements.sqlite` namiesto rozkúskovaného CSV.

Schema:
    realio_measurements (
      time_ms INTEGER PRIMARY KEY,    -- unix ms, sorted, indexed
      time_iso TEXT,                   -- ISO8601 (debug, ľudsky čitateľné)
      ftv_power_kw REAL,
      load_power_kw REAL,
      batt_power_kw REAL,
      batt_soc_pct REAL,
      grid_power_kw REAL,
      batt_setpoint_kw_cmd REAL,
      ftv_curtail_kw_cmd REAL
    )

Backward compat: funkcie vracia `pd.DataFrame` s rovnakými stĺpcami ako pôvodné
CSV CSV_COLS, takže volajúci kód v `realio.py` a `app.py` nemusí byť masívne refaktorovaný.
"""
from __future__ import annotations
import os
import sqlite3
import datetime as dt
from typing import Optional, Dict, Any, List
import pandas as pd

# Stĺpce s reálnymi meraniami (numeric)
DATA_COLS = (
    "ftv_power_kw", "load_power_kw", "load_power_kw_15m", "batt_power_kw",
    "batt_soc_pct", "grid_power_kw",
    "batt_setpoint_kw_cmd", "ftv_curtail_kw_cmd",
)
# Plný zoznam stĺpcov (čo vracia read_recent) — kompat s CSV_COLS v realio.py
COLS = ("time",) + DATA_COLS


def _data_dir() -> str:
    try:
        import market as _mk
        return _mk.data_dir()
    except Exception:
        return "out"


def db_path() -> str:
    return os.path.join(_data_dir(), "realio_measurements.sqlite")


# ─── Connection management ───────────────────────────────────────────────────
def _conn(path: Optional[str] = None) -> sqlite3.Connection:
    """Otvorí SQLite connection s rozumnými PRAGMA defaultmi.

    WAL mode skúšame, ale ak FS nepodporuje (napr. niektoré FUSE mounty), fallback
    na DELETE journal — to je default a funguje všade.
    """
    p = path or db_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    c = sqlite3.connect(p, timeout=10.0, isolation_level=None)  # autocommit
    try:
        c.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass  # FS nepodporuje WAL → ostane default DELETE journal
    try:
        c.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.OperationalError:
        pass
    return c


# ─── Schema ──────────────────────────────────────────────────────────────────
def init_db(path: Optional[str] = None) -> None:
    """Vytvorí tabuľku ak neexistuje. Idempotentné."""
    c = _conn(path)
    try:
        c.execute("""
            CREATE TABLE IF NOT EXISTS realio_measurements (
                time_ms INTEGER PRIMARY KEY,
                time_iso TEXT,
                ftv_power_kw REAL,
                load_power_kw REAL,
                load_power_kw_15m REAL,
                batt_power_kw REAL,
                batt_soc_pct REAL,
                grid_power_kw REAL,
                batt_setpoint_kw_cmd REAL,
                ftv_curtail_kw_cmd REAL
            )
        """)
        # Time index nie je nutný (PRIMARY KEY už zaisťuje), ale stáva sa že sa hľadá podľa time_iso
        c.execute("CREATE INDEX IF NOT EXISTS idx_time_iso ON realio_measurements(time_iso)")
        # Migrácia: pridaj load_power_kw_15m stĺpec ak chýba (existujúce DB pred F2.4)
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(realio_measurements)").fetchall()]
            if "load_power_kw_15m" not in cols:
                c.execute("ALTER TABLE realio_measurements ADD COLUMN load_power_kw_15m REAL")
        except sqlite3.OperationalError:
            pass
    finally:
        c.close()


# ─── Time helpers ────────────────────────────────────────────────────────────
# Lokálna timezone (Mac užívateľa: Europe/Bratislava — CEST/CET).
# `dt.datetime.now()` aj parseované CSV stringy bez TZ sú v lokálnom čase.
# UTC ukladáme v DB ako kanonický formát (žiadna ambiguita pri DST switch).
try:
    # Python 3.9+: zoneinfo (stdlib)
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo("Europe/Bratislava")
except Exception:
    # Fallback: použij system local time (môže byť rôzne na server vs Mac)
    _LOCAL_TZ = None


def _is_bender_format_string(s: str) -> bool:
    """Heuristika: Bender history endpoint vracia string s **medzerou** ako separátorom
    a **milisekundami** (napr. '2026-06-01 10:09:40.020'). Polling používa
    .isoformat() — T-separátor a bez ms ('2026-06-01T12:10:00').

    Ak je string Bender formát → pôvod je UTC (Bender server vracia UTC unix ms).
    Ak je T-formát bez ms → pôvod je local (`dt.datetime.now().isoformat()`).
    """
    if not isinstance(s, str):
        return False
    # Bender: space separator + ms fragmenty (napr. 10:09:40.020)
    return " " in s and "." in s.split(" ")[-1]


def _to_ms(t, *, assume_tz: str = "auto") -> Optional[int]:
    """Konverzia time → **UTC unix ms**, zaokrúhlené na sekundy.

    `assume_tz` parameter pre kontrolu konverzie naive timestampov:
    - "auto" (default): pre stringy heuristika `_is_bender_format_string`
      (UTC ak ma '... .ms'); pre datetime objekty `tzinfo` test (UTC ak tz-aware);
      inak predpokladaj **local**.
    - "utc": vždy treat as UTC (override).
    - "local": vždy treat as local (override).

    `int`/`float` sa vždy predpokladá ako UTC unix ms (Bender raw response).

    Sekundová granularita: floor na sekundu (pôvodné CSV-fragmenty .056/.057
    vyhotí sa do jedného DB riadku).
    """
    if t is None:
        return None
    try:
        if isinstance(t, (int, float)) and not isinstance(t, bool):
            # už predpokladáme UTC unix ms — len zaokrúhli na sekundy
            return (int(t) // 1000) * 1000
        # Detect source
        is_string_bender = False
        if isinstance(t, str):
            is_string_bender = _is_bender_format_string(t)
        ts = pd.to_datetime(t, errors="coerce")
        if ts is pd.NaT or pd.isna(ts):
            return None
        # Ak je už tz-aware → jednoducho konvertuj na UTC unix
        if ts.tzinfo is not None:
            return int(ts.timestamp()) * 1000
        # Naive → rozhodni si TZ
        if assume_tz == "utc" or (assume_tz == "auto" and is_string_bender):
            # Bender formát alebo explicit UTC override → naive = UTC
            ts = ts.tz_localize("UTC")
        elif assume_tz == "local" or assume_tz == "auto":
            # T-formát alebo explicit local → naive = local CEST
            if _LOCAL_TZ is not None:
                ts = ts.tz_localize(_LOCAL_TZ)
            else:
                ts = ts.tz_localize("UTC")  # fallback
        return int(ts.timestamp()) * 1000
    except Exception:
        return None


def _iso_from_ms(ms: int) -> Optional[str]:
    """UTC unix ms → ISO8601 string v **lokálnom čase**, sekundová granularita.

    Diagnostický stĺpec time_iso je deriváciou z time_ms (single source of truth).
    Vďaka tomu nie je možná nesúrodá interpretácia (UTC vs local) — vždy
    sa zobrazí ten istý lokálny čas pre rovnaký okamih.
    """
    if ms is None:
        return None
    try:
        if _LOCAL_TZ is not None:
            ts = pd.to_datetime(int(ms), unit="ms", utc=True).tz_convert(_LOCAL_TZ).tz_localize(None)
        else:
            ts = pd.to_datetime(int(ms), unit="ms")
        return ts.isoformat(timespec="seconds")
    except Exception:
        return None


def _ms_to_local_naive(ms: int) -> pd.Timestamp:
    """UTC unix ms → naive Timestamp v lokálnej TZ (pre charts, ktoré pracujú s naive lokálnym časom)."""
    if _LOCAL_TZ is not None:
        return pd.to_datetime(ms, unit="ms", utc=True).tz_convert(_LOCAL_TZ).tz_localize(None)
    return pd.to_datetime(ms, unit="ms")


# ─── Write (per-row UPSERT) ──────────────────────────────────────────────────
def insert_row(time_val, vals: Dict[str, Optional[float]],
                 path: Optional[str] = None) -> int:
    """UPSERT jeden riadok podľa `time_val` (akýkoľvek format → ms).

    `vals` je dict s kľúčmi z DATA_COLS. Iba kľúče ktoré sú v dict a majú
    non-None hodnotu sa UPSERT-nú. Pri konflikte (existujúci time_ms) sa
    cez COALESCE zachovajú existujúce hodnoty pre stĺpce ktoré nie sú v `vals`
    alebo sú None.

    Vracia 1 ak insert, 1 ak update existujúceho, 0 ak nothing to write.
    """
    init_db(path)
    time_ms = _to_ms(time_val)
    if time_ms is None:
        return 0
    # time_iso je derivácia z time_ms — konsistentný local čas pre admin diagnostiku
    time_iso = _iso_from_ms(time_ms)
    # Filter — len známe stĺpce s non-None hodnotou
    clean = {}
    for k, v in vals.items():
        if k not in DATA_COLS:
            continue
        if v is None:
            continue
        try:
            # Akceptuj iba čísla (NaN ignoruj)
            fv = float(v)
            if fv != fv:  # NaN check
                continue
            clean[k] = fv
        except (TypeError, ValueError):
            continue
    if not clean:
        # Pri prázdnych vals stále zaregistruj timestamp (aby polling vedel že beží)
        # ale len ak time_ms neexistuje. Inak skip.
        c = _conn(path)
        try:
            c.execute(
                "INSERT OR IGNORE INTO realio_measurements (time_ms, time_iso) VALUES (?, ?)",
                (time_ms, time_iso),
            )
            return c.total_changes
        finally:
            c.close()

    # Build UPSERT — len pre clean kľúče
    cols = list(clean.keys())
    placeholders = ", ".join(["?"] * (2 + len(cols)))   # time_ms, time_iso, + cols
    col_list = ", ".join(["time_ms", "time_iso"] + cols)
    # ON CONFLICT: pre každý dodaný stĺpec použij COALESCE(excluded.X, X) — ak nový NULL, nech ostane existing
    set_clauses = ["time_iso = COALESCE(excluded.time_iso, time_iso)"]
    for k in cols:
        set_clauses.append(f"{k} = COALESCE(excluded.{k}, {k})")
    sql = (
        f"INSERT INTO realio_measurements ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT(time_ms) DO UPDATE SET {', '.join(set_clauses)}"
    )
    params = [time_ms, time_iso] + [clean[k] for k in cols]
    c = _conn(path)
    try:
        c.execute(sql, params)
        return c.total_changes
    finally:
        c.close()


def insert_per_tag(time_val, tag_col: str, value, path: Optional[str] = None) -> int:
    """Helper pre per-tag history backfill. UPSERT iba jeden stĺpec."""
    if tag_col not in DATA_COLS:
        return 0
    return insert_row(time_val, {tag_col: value}, path=path)


# ─── Read ────────────────────────────────────────────────────────────────────
def _df_from_rows(rows) -> pd.DataFrame:
    """Konvertuje cursor rows na DataFrame s 'time' stĺpcom (**naive local datetime**) + DATA_COLS.

    DB ukladá UTC unix ms (kanonický), ale charts/app.py očakávajú naive local
    time (pôvodné CSV malo naive local stringy z `dt.datetime.now().isoformat()`).
    Tu sa robí konverzia UTC ms → local naive.
    """
    if not rows:
        return pd.DataFrame(columns=list(COLS))
    df = pd.DataFrame(rows, columns=("time_ms", "time_iso") + DATA_COLS)
    if _LOCAL_TZ is not None:
        df["time"] = (pd.to_datetime(df["time_ms"], unit="ms", utc=True)
                         .dt.tz_convert(_LOCAL_TZ)
                         .dt.tz_localize(None))
    else:
        df["time"] = pd.to_datetime(df["time_ms"], unit="ms")
    df = df.drop(columns=["time_ms", "time_iso"])
    df = df[list(COLS)]
    return df


def read_recent(n_minutes: int = 240, path: Optional[str] = None) -> pd.DataFrame:
    """Vráti posledných N minút (DataFrame), sorted by time. Bez stĺpcov s úplne NaN."""
    init_db(path)
    cutoff_ms = int((dt.datetime.now() - dt.timedelta(minutes=int(n_minutes))).timestamp() * 1000)
    c = _conn(path)
    try:
        rows = c.execute(
            "SELECT time_ms, time_iso, "
            + ", ".join(DATA_COLS)
            + " FROM realio_measurements WHERE time_ms >= ? ORDER BY time_ms ASC",
            (cutoff_ms,)
        ).fetchall()
    finally:
        c.close()
    return _df_from_rows(rows)


def read_range(from_dt: dt.datetime, to_dt: dt.datetime,
                 path: Optional[str] = None) -> pd.DataFrame:
    """Vráti riadky v intervalu [from_dt, to_dt]."""
    init_db(path)
    f_ms = _to_ms(from_dt)
    t_ms = _to_ms(to_dt)
    if f_ms is None or t_ms is None:
        return pd.DataFrame(columns=list(COLS))
    c = _conn(path)
    try:
        rows = c.execute(
            "SELECT time_ms, time_iso, "
            + ", ".join(DATA_COLS)
            + " FROM realio_measurements WHERE time_ms BETWEEN ? AND ? ORDER BY time_ms ASC",
            (f_ms, t_ms)
        ).fetchall()
    finally:
        c.close()
    return _df_from_rows(rows)


def count_rows(path: Optional[str] = None) -> int:
    init_db(path)
    c = _conn(path)
    try:
        r = c.execute("SELECT COUNT(*) FROM realio_measurements").fetchone()
        return int(r[0]) if r else 0
    finally:
        c.close()


def stats(path: Optional[str] = None) -> Dict[str, Any]:
    """Diag info — počet riadkov, časový rozsah, NaN coverage per-stĺpec."""
    init_db(path)
    c = _conn(path)
    try:
        total = c.execute("SELECT COUNT(*) FROM realio_measurements").fetchone()[0]
        if total == 0:
            return {"total": 0}
        first = c.execute("SELECT time_iso FROM realio_measurements ORDER BY time_ms ASC LIMIT 1").fetchone()
        last = c.execute("SELECT time_iso FROM realio_measurements ORDER BY time_ms DESC LIMIT 1").fetchone()
        out = {"total": total, "first": first[0] if first else None,
               "last": last[0] if last else None,
               "non_null": {}}
        for col in DATA_COLS:
            cnt = c.execute(
                f"SELECT COUNT({col}) FROM realio_measurements"
            ).fetchone()[0]
            out["non_null"][col] = cnt
        return out
    finally:
        c.close()


def fix_future_timestamps(path: Optional[str] = None,
                            now_buffer_min: int = 5,
                            shift_hours: int = 2) -> Dict[str, Any]:
    """Oprava riadkov ktoré buggy polling job (pred F2.2 fix) uložil s posunom +shift_hours hodin.

    Detekcia: time_ms > (now + buffer) — žiadny legitímny záznam by nemal byť v
    budúcnosti (polling vždy posiela current time, history backfill je v minulosti).
    Oprava: SET time_ms = time_ms - shift_hours*3600*1000.

    Použitie: po deployi F2.2 fixu, ak appka bežala v starom kóde a polling stihol
    napísať pár riadkov s posunom. Idempotentné — opakované volanie neurobí nič.

    Vracia diag info: rows_fixed, sample časov.
    """
    init_db(path)
    import time as _time
    now_ms = int(_time.time() * 1000) + int(now_buffer_min * 60 * 1000)
    shift_ms = int(shift_hours * 3600 * 1000)
    c = _conn(path)
    try:
        # Spočítaj koľko je future riadkov
        cnt = c.execute(
            "SELECT COUNT(*) FROM realio_measurements WHERE time_ms > ?", (now_ms,)
        ).fetchone()[0]
        if cnt == 0:
            return {"ok": True, "rows_fixed": 0, "msg": "Žiadne future riadky — netreba opravovať"}
        # Sample časov pred opravou
        sample_before = c.execute(
            "SELECT time_iso FROM realio_measurements WHERE time_ms > ? ORDER BY time_ms DESC LIMIT 3",
            (now_ms,)
        ).fetchall()
        # Posun späť o shift_hours
        # POZOR: time_ms posunúť o -shift_ms, time_iso prepočítať z nového time_ms
        # Najprv natiahnuť všetky time_ms ktoré sú v budúcnosti
        rows = c.execute(
            "SELECT time_ms FROM realio_measurements WHERE time_ms > ?", (now_ms,)
        ).fetchall()
        fixed = 0
        for (old_ms,) in rows:
            new_ms = old_ms - shift_ms
            new_iso = _iso_from_ms(new_ms)
            # Použij UPDATE OR REPLACE pre prípad konfliktu s existujúcim time_ms
            # (ak by sa nejaký riadok kolidoval s minulým, merge sa nepoužije — proste keep neopravený duplikát)
            try:
                c.execute(
                    "UPDATE realio_measurements SET time_ms = ?, time_iso = ? WHERE time_ms = ?",
                    (new_ms, new_iso, old_ms)
                )
                fixed += 1
            except sqlite3.IntegrityError:
                # Konflikt — riadok s new_ms už existuje. Vymaž stary buggy.
                c.execute("DELETE FROM realio_measurements WHERE time_ms = ?", (old_ms,))
        sample_after = [_iso_from_ms(s[0] - shift_ms) for s in sample_before]
        return {"ok": True, "rows_fixed": fixed,
                "sample_before": [s[0] for s in sample_before],
                "sample_after": sample_after,
                "msg": f"Oprava OK: {fixed} riadkov posunutých o -{shift_hours}h (z budúcnosti do správneho minulého času)"}
    finally:
        c.close()


def vacuum(path: Optional[str] = None) -> Dict[str, Any]:
    """VACUUM — reorganizácia DB, uvoľní fragmentované miesto."""
    init_db(path)
    p = path or db_path()
    size_before = os.path.getsize(p) if os.path.exists(p) else 0
    c = _conn(path)
    try:
        c.execute("VACUUM")
    finally:
        c.close()
    size_after = os.path.getsize(p) if os.path.exists(p) else 0
    return {"ok": True, "size_before": size_before, "size_after": size_after,
             "msg": f"VACUUM OK: {size_before} → {size_after} bytes"}


# ─── Migrácia CSV → SQLite ───────────────────────────────────────────────────
def migrate_from_csv(csv_path: str, path: Optional[str] = None) -> Dict[str, Any]:
    """Jednorazová migrácia existujúceho CSV → SQLite.

    Pre každý CSV riadok UPSERT podľa time. NaN hodnoty sa NEzapisujú (COALESCE zachová existing).
    Idempotentné — opakované volanie len doplní chýbajúce body, neztratí dáta.
    """
    if not os.path.exists(csv_path):
        return {"ok": False, "msg": f"CSV {csv_path} neexistuje", "migrated": 0}
    init_db(path)
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        return {"ok": False, "msg": f"CSV read zlyhalo: {e}", "migrated": 0}
    if df.empty or "time" not in df.columns:
        return {"ok": False, "msg": "CSV je prázdne alebo nemá 'time' stĺpec", "migrated": 0}

    inserted = 0
    skipped = 0
    c = _conn(path)
    try:
        # Pre rýchlosť: použijeme transaction
        c.execute("BEGIN")
        for _, row in df.iterrows():
            time_ms = _to_ms(row.get("time"))
            if time_ms is None:
                skipped += 1
                continue
            time_iso = _iso_from_ms(time_ms)
            clean = {}
            for k in DATA_COLS:
                v = row.get(k)
                if v is None or (isinstance(v, float) and v != v):  # NaN
                    continue
                try:
                    clean[k] = float(v)
                except (TypeError, ValueError):
                    continue
            cols = list(clean.keys())
            if not cols:
                # Nemá žiadne data → vlož len timestamp (ak ešte neexistuje)
                c.execute(
                    "INSERT OR IGNORE INTO realio_measurements (time_ms, time_iso) VALUES (?, ?)",
                    (time_ms, time_iso)
                )
                inserted += 1
                continue
            placeholders = ", ".join(["?"] * (2 + len(cols)))
            col_list = ", ".join(["time_ms", "time_iso"] + cols)
            set_clauses = ["time_iso = COALESCE(excluded.time_iso, time_iso)"]
            for k in cols:
                set_clauses.append(f"{k} = COALESCE(excluded.{k}, {k})")
            sql = (
                f"INSERT INTO realio_measurements ({col_list}) VALUES ({placeholders}) "
                f"ON CONFLICT(time_ms) DO UPDATE SET {', '.join(set_clauses)}"
            )
            params = [time_ms, time_iso] + [clean[k] for k in cols]
            c.execute(sql, params)
            inserted += 1
        c.execute("COMMIT")
    except Exception as e:
        try: c.execute("ROLLBACK")
        except Exception: pass
        return {"ok": False, "msg": f"Migration zlyhalo: {e}",
                 "migrated": inserted, "skipped": skipped}
    finally:
        c.close()
    return {"ok": True, "migrated": inserted, "skipped": skipped,
             "msg": f"Migrácia OK: {inserted} riadkov spracovaných, {skipped} preskočených"}
