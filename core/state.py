# -*- coding: utf-8 -*-
"""
core.state — globálna konfigurácia a perzistencia UI nastavení.

Extrahované z app.py (Fáza 1 refactoringu).

Obsahuje:
  • _PORT, UI_PATH  — per-port stav (8000 = legacy, ostatné porty = vlastný súbor)
  • _ui_load, _ui_save — pre /plan, /dentrh, /rt formulárové polia
  • DEF — defaultné hodnoty pre /plan formulár (lat/lon/kwp/batt/SOC…)
  • FX_CZK — kurz CZK→EUR pre ČEPS odhady ZCO
"""
from __future__ import annotations
import os
import json


# Per-port instance: port 8000 = legacy ui_settings.json (back-compat),
# ostatné porty (napr. 8001 pre druhú inštanciu) → vlastný súbor.
# Fallback chain: PORT → APP_PORT → "8000" (start_dev.sh nastavuje APP_PORT).
_PORT = os.environ.get("PORT") or os.environ.get("APP_PORT") or "8000"
UI_PATH = "out/ui_settings.json" if _PORT == "8000" else f"out/ui_settings_{_PORT}.json"


# ── Dual storage prepínač (Fáza 1.13 migrácie) ──────────────────────────
_USE_DB = os.environ.get("USE_DB", "0").strip() in ("1", "true", "True", "yes")


def _db_available() -> bool:
    if not _USE_DB:
        return False
    try:
        from db import get_session   # noqa: F401
        return True
    except Exception:
        return False


def _ui_load(key: str, defaults: dict) -> dict:
    """Načíta uložený stav formulára pre `key` (napr. 'plan', 'dentrh', 'rt').
    Vracia merge defaults + uložené hodnoty. Pri chybe vracia kópiu defaults."""
    # DB read prvé
    if _db_available():
        try:
            from db import get_session
            from db.models import UiSettings as _DbUi
            with get_session() as s:
                row = s.query(_DbUi).filter_by(port=_PORT, key=str(key)).one_or_none()
                if row is not None:
                    return {**defaults, **(row.data or {})}
        except Exception as e:
            print(f"[core.state._ui_load DB] zlyhal: {e}")
    # JSON fallback
    try:
        with open(UI_PATH) as fh:
            return {**defaults, **json.load(fh).get(key, {})}
    except Exception:
        return dict(defaults)


def _ui_save(key: str, values: dict) -> None:
    """Uloží `values` pod `key` do UI_PATH (in-place merge s existujúcim JSON)."""
    # JSON write (vždy)
    try:
        d = {}
        if os.path.exists(UI_PATH):
            with open(UI_PATH) as fh:
                d = json.load(fh)
        d[key] = values
        with open(UI_PATH, "w") as fh:
            json.dump(d, fh)
    except Exception:
        pass
    # DB dual write
    if _db_available():
        try:
            from db import get_session
            from db.models import UiSettings as _DbUi
            from datetime import datetime as _dt
            now_iso = _dt.now().isoformat(timespec="seconds")
            with get_session() as s:
                row = s.query(_DbUi).filter_by(port=_PORT, key=str(key)).one_or_none()
                if row:
                    row.data = dict(values or {})
                    row.updated_at = now_iso
                else:
                    s.add(_DbUi(port=_PORT, key=str(key),
                                  data=dict(values or {}), updated_at=now_iso))
        except Exception as e:
            print(f"[core.state._ui_save DB] zlyhal: {e}")


# Defaultné hodnoty /plan formulára. Užívateľ ich môže prepísať v UI, profiles tieto override-ujú.
DEF = dict(lat=49.5961, lon=17.3634, kwp=99.0, tilt=30.0, azimuth=0.0, eff=0.85,
           batt_kw=100.0, batt_kwh=200.0, eff_c=0.95, eff_d=0.95,
           soc_min=5.0, soc_max=95.0, soc_init=50.0, terminal_soc=50.0,
           grid_kw=100.0, grid_fee=22.0, cycle_cost=2.0,
           min_spread=30.0, min_trade=0.0, price_scale=1.0, pv_scale=1.0, allow_curtail=True,
           zco_bias_w=0.0,
           # #27: rozsah dní pre VDT oceňovanie reálnymi UZAVRETÝMI cenami (len história).
           # Prázdne = vypnuté (živé oceňovanie pre celú históriu, default správanie).
           vdt_closed_from="", vdt_closed_to="")


# Kurz EUR/CZK — pre ČEPS odhady ZCO ktoré sú v CZK.
FX_CZK = 24.3

# Branding — názov aplikácie zobrazený v titulku, navigácii, login stránke.
# Override cez env: APP_NAME="Iný názov".
APP_NAME = os.environ.get("APP_NAME", "Trading Fuergy")
