# -*- coding: utf-8 -*-
"""
case_config.py – JEDEN zdroj pravdy pre celý "prípad" (elektráreň + batéria + ekonomika
+ RT regulátor + stratégia). Cieľ: simulácia, backtest a živý /rt používajú TIE ISTÉ
parametre z jedného miesta → nemôžu sa rozísť. Nový prípad = nový JSON, žiadne kopírovanie kódu.

Profily: out/cases/<nazov>.json   ('default' = aktuálna overená konfigurácia, golden 3278 €).
Defaulty CaseConfig sa MUSIA zhodovať s pôvodnými konštantami (golden sa nesmie pohnúť).
"""
from __future__ import annotations
from dataclasses import dataclass, asdict, fields
import json
import os

CASES_DIR = "out/cases"


@dataclass
class CaseConfig:
    name: str = "default"
    # ── elektráreň (FTV) ──
    lat: float = 49.5961
    lon: float = 17.3634
    kwp: float = 99.0
    tilt: float = 30.0
    azimuth: float = 0.0
    eff: float = 0.85
    fx_czk: float = 24.3
    # ── batéria ──
    batt_kw: float = 100.0
    batt_kwh: float = 200.0
    eff_c: float = 0.95
    eff_d: float = 0.95
    soc_min: float = 0.05          # podiel (0..1)
    soc_max: float = 0.95
    soc_init: float = 0.50
    terminal_soc: float = 0.50
    grid_kw: float = 100.0
    max_cycles: float = 3.0           # denný strop cyklov (pracovný predpoklad)
    cycle: float = 2.0             # cena/penalizácia cyklu v RT settle [€/MWh ekvivalent]
    # ── ekonomika D-1 ──
    grid_fee: float = 22.0
    cycle_cost: float = 2.0
    allow_grid_charge: bool = True
    allow_curtail: bool = True        # orezať/znížiť FTV pri nevýhodných cenách (záporné + plná batéria)
    # ── RT jadro (event + signál) ──
    w_sys: float = 3.0
    sys_orient: float = -1.0
    w_mfrr: float = 0.5
    strong_mw: float = 150.0
    afrr_min: float = 30.0
    afrr_min_chg: float = 15.0
    event_k_sigma: float = 1.0
    mfrr_min: float = 1.0
    roll_win: int = 180
    auto_kdis: float = 1.5
    auto_kchg: float = 0.5
    auto_min_dis: float = 15.0
    auto_min_chg: float = 5.0
    dt_bias_k: float = 1.5         # GOLDEN: DT citlivosť použitá v backteste/ /rt
    prod_band_dis: float = 30.0    # manuálne fallback pásmo
    prod_band_chg: float = 10.0
    # FLIP-event: zmena smeru ktorejkoľvek služby = výrazná cena (aj pri malej veľkosti)
    flip_event: bool = True
    flip_min_net: float = 8.0
    flip_roll_min: float = 15.0
    # realizmus RT (default = teoretický strop; realistický prípad: haircut 0.65, latency 1)
    rt_haircut: float = 1.0
    rt_latency_min: int = 0
    reversal_boost: float = 0.0       # 0=vyp; >0 = po dlhom behu jedného smeru znížiť prah opačného (skôr preklopí)
    reversal_scale_min: float = 30.0  # po koľkých min v jednom smere dosiahne boost plnú hodnotu
    soc_bias_k: float = 0.0           # 0=vyp; >0 = SOC-citlivé prahy (plný→ľahšie vybíjať, prázdny→ľahšie nabíjať)
    soc_bias_hi: float = 80.0         # nad týmto SOC [%] sa znižuje vybíjací prah
    soc_bias_lo: float = 20.0         # pod týmto SOC [%] sa znižuje nabíjací prah
    sys_dir_gate: bool = True         # smer systémovej odchýlky = tvrdá hranica (regulátor proti nej nekoná)
    sys_gate_min: float = 5.0         # mŕtve pásmo gate [MW] (pod túto |odchýlku| sa gate neaktivuje)
    plan_cycles: float = None         # strop cyklov D-1 plánu (None=bez stropu); nižší → viac batérie pre odchýlku
    # režim modelu: use_rt=False → iba denný trh (bez odchýlky); d1_step_min=15 → 15-min arbitráž
    use_rt: bool = True
    d1_step_min: int = 60
    # ── RT stratégia (default pre /rt aj backtest) ──
    rt_auto: bool = True
    rt_kdis: float = 0.3           # škála vybíjacieho pásma
    rt_kchg: float = 0.5           # škála nabíjacieho pásma

    # ---- pomocné ----
    def dparams(self) -> dict:
        """Parametre pre optimizer/D-1 plán (formát ako pôvodné DPARAMS/DEF, SOC v %)."""
        return dict(batt_kw=self.batt_kw, batt_kwh=self.batt_kwh, eff_c=self.eff_c, eff_d=self.eff_d,
                    soc_min_pct=self.soc_min*100, soc_max_pct=self.soc_max*100,
                    soc_init_pct=self.soc_init*100, grid_kw=self.grid_kw,
                    grid_fee=self.grid_fee, cycle_cost=self.cycle_cost,
                    allow_grid_charge=self.allow_grid_charge,
                    allow_curtail=self.allow_curtail)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CaseConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


def _path(name: str) -> str:
    safe = "".join(c for c in str(name) if c.isalnum() or c in ("_", "-")) or "default"
    return os.path.join(CASES_DIR, f"{safe}.json")


# ── Dual storage prepínač (Fáza 1.13 migrácie) ─────────────────────────────
_USE_DB = os.environ.get("USE_DB", "0").strip() in ("1", "true", "True", "yes")


def _db_available() -> bool:
    if not _USE_DB:
        return False
    try:
        from db import get_session   # noqa: F401
        return True
    except Exception:
        return False


def list_cases() -> list[str]:
    if _db_available():
        try:
            from db import get_session
            from db.models import Case as _DbCase
            with get_session() as s:
                names = sorted(c.name for c in s.query(_DbCase).all())
                if names:
                    return names
        except Exception as e:
            print(f"[case_config.list_cases DB] zlyhal: {e}")
    if not os.path.isdir(CASES_DIR):
        return ["default"]
    out = sorted(f[:-5] for f in os.listdir(CASES_DIR) if f.endswith(".json"))
    return out or ["default"]


def load_case(name: str = "default") -> CaseConfig:
    # DB read prvé
    if _db_available():
        try:
            from db import get_session
            from db.models import Case as _DbCase
            with get_session() as s:
                row = s.query(_DbCase).filter_by(name=name).one_or_none()
                if row is not None:
                    cfg = CaseConfig.from_dict(dict(row.config or {}))
                    cfg.name = name
                    return cfg
        except Exception as e:
            print(f"[case_config.load_case DB] zlyhal: {e}")
    # JSON fallback
    p = _path(name)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                cfg = CaseConfig.from_dict(json.load(fh))
                cfg.name = name
                return cfg
        except Exception:
            pass
    return CaseConfig(name=name)


def save_case(cfg: CaseConfig) -> str:
    os.makedirs(CASES_DIR, exist_ok=True)
    p = _path(cfg.name)
    # JSON write (vždy back-compat)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(cfg.to_dict(), fh, ensure_ascii=False, indent=2)
    # DB dual write
    if _db_available():
        try:
            from db import get_session
            from db.models import Case as _DbCase
            from datetime import datetime as _dt
            now_iso = _dt.now().isoformat(timespec="seconds")
            with get_session() as s:
                row = s.query(_DbCase).filter_by(name=cfg.name).one_or_none()
                if row:
                    row.config = cfg.to_dict()
                    row.updated_at = now_iso
                else:
                    s.add(_DbCase(name=cfg.name, config=cfg.to_dict(),
                                    locked=False, updated_at=now_iso))
        except Exception as e:
            print(f"[case_config.save_case DB] zlyhal: {e}")
    return p


def ensure_default():
    """Zapíše out/cases/default.json, ak chýba (golden konfigurácia)."""
    if not os.path.exists(_path("default")):
        save_case(CaseConfig(name="default"))


if __name__ == "__main__":
    ensure_default()
    print("Dostupné prípady:", list_cases())
    print("default:", load_case("default").to_dict())
