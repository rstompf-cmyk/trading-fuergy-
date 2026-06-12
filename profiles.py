# -*- coding: utf-8 -*-
"""
profiles.py — pomenované **profile** so všetkými parametrami pre plánovanie + RT.

Profile = jeden JSON súbor v `out/profiles/<name>.json`, obsahuje:
- form polia z `/plan` (Plán D-1)
- form polia z `/dentrh` (Denný trh 15-min)
- nastavenia z `/rt` (RT poradca: kdis, kchg, dtk, rboost)
- šablónu mult96 + rt_on96 (24×4 = 96 slotov)

Použitie:
  1. Užívateľ vytvorí profile (napr. "konzervativny") so všetkými svojimi obľúbenými nastaveniami.
  2. Pri Generovať plán / Spustiť simuláciu / Otvoriť /rt poradcu → vyberie profile z dropdownu.
  3. Hodnoty z profilu sa ihneď použijú (form fields sa pred-vyplnia, plan_overrides template sa prepíše).

Aktívny profile sa zapisuje do `out/profiles/_active.json` (kľúč "name").
Pre nedostatok aktívneho profilu sa použijú default UI hodnoty (DEF).
"""
from __future__ import annotations
import os, json, re
from datetime import datetime
from typing import Optional, Dict, Any, List

def _root() -> str:
    """Koreň profiles — **ZDIEĽANÝ medzi trhmi** v `out/profiles/`.

    Profil je sablona ktorá môže byť použitá pre CZ aj SK trh — pri prepnutí
    market-u sa rovnaký profil aplikuje, ale výpočty (plány, livesim logy,
    plan_overrides) idú per-market do `out/cz/...` resp. `out/sk/...`.
    """
    env = os.environ.get("PROFILES_DIR")
    if env:
        return env
    # Zdieľaný root — root je parent z market.data_dir() ("out/cz" → "out")
    try:
        import market as _mk
        root = os.path.dirname(_mk.data_dir().rstrip("/").rstrip(os.sep)) or "out"
    except Exception:
        root = "out"
    return os.path.join(root, "profiles")


DIR = _root()        # back-compat const


# ── VDT advisor + state defaults (Bug P) ────────────────────────────────────
# Tieto polia sú per-profile. Default hodnoty platia LEN pre nový/legacy profile
# kde polia chýbajú. Pri load_profile() sa chýbajúce polia auto-doplnia (lazy migration).
# Žiadny VDT modul nesmie mať tieto hodnoty hard-coded — vždy musia prísť z profile.plan.
_PLAN_VDT_DEFAULTS: Dict[str, Any] = {
    # VDT economics (€/MWh)
    "grid_fee_vdt": 22.0,                   # distribučný poplatok pre VDT advisor (môže byť iný ako plan.grid_fee)
    "cycle_cost_vdt": 2.0,                  # cena cyklu pre VDT advisor
    "min_spread_eur": 5.0,                  # minimálna marža LP pre obchod
    # VDT trading limits
    "soc_end_min_pct": 20.0,                # terminálny SOC v 23:59
    "max_cycles_per_day": 3.0,              # LP cap počet cyklov denne
    "soc_max_pct_operational": 95.0,        # operačný strop pre LP (5% safety rezerva)
    # Fallback (worst case keď livesim aj D-1 yesterday chýbajú)
    "fallback_soc_pct": 50.0,               # default SOC ak žiadny zdroj nie je
    # State integrácia (presný = profile.plan kópia, ale auto-fallback ak chýbajú)
    "vdt_eff_c": None,                      # None = použiť plan.eff_c (single source)
    "vdt_eff_d": None,                      # None = použiť plan.eff_d
    # Bug CC5 (2026-06-07): Joint MPC kontrolér flags
    "joint_mpc_enabled": False,             # zapne rolling MPC tick (mpc_controller) každú minútu
    "trade_batt": True,                     # batt arbitráž povolená v joint_lp
    "trade_ftv": True,                      # FTV export povolený
    "trade_load": True,                     # load z grid povolený
    "use_vdt": True,                        # VDT intraday trade povolený
    "allow_rt_correction": True,            # RT korekcia odchýlky povolená
}


def _ensure_plan_vdt_defaults(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Doplní VDT-specific polia s defaults ak v `plan` chýbajú. Returns same dict (in-place).

    Bug P: žiadna konstanta v kóde — VDT advisor musí čítať z profile.plan. Tento helper
    zabezpečí že každý profil má kompletný plan dict pri loade.
    """
    if not isinstance(plan, dict):
        return plan
    for k, v in _PLAN_VDT_DEFAULTS.items():
        if k not in plan:
            plan[k] = v
    return plan


# ── Dual storage prepínač (Fáza 1.9 migrácie) ──────────────────────────────
# USE_DB=1 v env → čítame z DB (zdroj pravdy), write je dual (DB + JSON).
# USE_DB=0 (default) → pôvodný JSON-only režim. Toto umožňuje paralelný beh
# main branch (port 8000) a refactor-v2 (port 8001) bez konfliktov.
_USE_DB = os.environ.get("USE_DB", "0").strip() in ("1", "true", "True", "yes")

def _db_available() -> bool:
    """Vráti True ak `db` package je importovateľný a DB súbor existuje.

    Pri USE_DB=1 ale chýbajúcej DB sa vrátime na JSON-only — žiadny crash.
    """
    if not _USE_DB:
        return False
    try:
        from db import get_session   # noqa: F401
        return True
    except Exception:
        return False


# Mode konstanty — typ profilu fixovaný pri vzniku, NEDÁ SA prepnúť.
#   MODE_SIM = 'simulation' — historická simulácia (livesim.advance cez minulé dni,
#                              PVGIS + scenár, profit chC z modelu). Žiadny realio
#                              overlay; manual setpoint write zamietnutý.
#   MODE_REAL = 'real'      — reálny chod: livesim ukazuje len plán + reálne meranie
#                              z realio CSV. Editor FTV scenára skrytý.
#                              Manuálny setpoint + FVE write povolené.
MODE_SIM = "simulation"
MODE_REAL = "real"
VALID_MODES = (MODE_SIM, MODE_REAL)

# Aktívny profil je per-inštancia (per-port). Default port 8000 = legacy _active.json.
# Fallback chain: PORT → APP_PORT → "8000" (start_dev.sh nastavuje APP_PORT, nie PORT).
_PORT = os.environ.get("PORT") or os.environ.get("APP_PORT") or "8000"
def _active_path() -> str:
    """Aktuálna cesta k _active json — market-aware + per-port."""
    r = _root()
    return os.path.join(r, "_active.json") if _PORT == "8000" else os.path.join(r, f"_active_{_PORT}.json")
# ACTIVE_PATH const odstránený — používa sa _active_path() dynamicky (market+port aware)
N96 = 96


def _ensure_dir() -> None:
    os.makedirs(_root(), exist_ok=True)


def _safe_name(name: str) -> str:
    """Bezpečný názov súboru — len alfanum, podtržník, pomlčka."""
    s = re.sub(r"[^A-Za-z0-9_\-]", "_", str(name).strip())
    s = s.strip("_-")
    return s or "default"


def _path(name: str) -> str:
    """Cesta k profile config JSONu.

    Fáza B.1: deleguje na core/paths ak je FTV_SANDBOX=1 (sandbox layout).
    Inak (default) → legacy out/profiles/<name>.json.
    """
    try:
        from core.paths import profile_config_path, is_sandbox_mode
        if is_sandbox_mode():
            return profile_config_path(_safe_name(name))
    except Exception:
        pass
    return os.path.join(_root(), f"{_safe_name(name)}.json")


def list_profiles() -> List[str]:
    """Vráti zoznam názvov dostupných profilov (bez '_active')."""
    if _db_available():
        try:
            from db import get_session
            from db.models import Profile as _DbProfile
            with get_session() as s:
                return sorted([p.name for p in s.query(_DbProfile).all()])
        except Exception as e:
            print(f"[profiles.list_profiles] DB read zlyhal, fallback na JSON: {e}")
    # JSON storage (default + DB fallback)
    _ensure_dir()
    out = []
    _d = _root()
    if not os.path.isdir(_d):
        return []
    # Fáza B.1: detekuj sandbox layout
    try:
        from core.paths import is_sandbox_mode
        _sandbox = is_sandbox_mode()
    except Exception:
        _sandbox = False
    for fn in os.listdir(_d):
        if fn.startswith("_"):
            continue
        full = os.path.join(_d, fn)
        if _sandbox:
            # Sandbox: <name>/ je adresár obsahujúci config.json
            if os.path.isdir(full) and os.path.isfile(os.path.join(full, "config.json")):
                out.append(fn)
        else:
            # Legacy: <name>.json je súbor
            if fn.endswith(".json") and os.path.isfile(full):
                out.append(fn[:-5])
    return sorted(out)


def save_profile(name: str, data: Dict[str, Any]) -> str:
    """Uloží profile. Aktualizuje updated_at, zachová created_at + **mode** ak existuje.

    Mode je FIXOVANÝ pri vzniku — ak profile existuje a má `mode`, neresetuje sa
    bez ohľadu na to čo je v `data['mode']`. Staré profily bez mode dostanú
    default 'simulation'. Vracia cestu.
    """
    _ensure_dir()
    # Fáza B.1: v sandbox móde vytvor profile sub-directory + plans/livesim/
    try:
        from core.paths import is_sandbox_mode, ensure_profile_dirs
        if is_sandbox_mode():
            ensure_profile_dirs(_safe_name(name))
    except Exception:
        pass
    p = _path(name)
    existing = {}
    if os.path.exists(p):
        try:
            with open(p) as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError):
            existing = {}
    # Mode logika:
    #  • Ak existing má 'mode' → použij ho (immutable)
    #  • Inak ak je v data['mode'] valid hodnota → použij ju (nový profil)
    #  • Inak default = simulation (back-compat pre staré profily)
    if existing.get("mode") in VALID_MODES:
        mode = existing["mode"]
    else:
        m = str(data.get("mode") or "").strip().lower()
        mode = m if m in VALID_MODES else MODE_SIM
    body = {
        "name": _safe_name(name),
        "mode": mode,
        "created_at": existing.get("created_at", datetime.now().isoformat(timespec="seconds")),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "plan": dict(data.get("plan") or {}),
        "dentrh": dict(data.get("dentrh") or {}),
        "rt": dict(data.get("rt") or {}),
        "mult96": list(data.get("mult96") or []),
        "rt_on96": list(data.get("rt_on96") or []),
        "note": str(data.get("note", "")),
        # Distribučné tarify (TOU + peak) — voliteľné, používa joint_lp F3
        "distribution": dict(data.get("distribution")
                              or existing.get("distribution") or {}),
    }
    # validácia mult96 / rt_on96 (ak prítomné, musia byť dĺžky 96)
    for k in ("mult96", "rt_on96"):
        if body[k] and len(body[k]) != N96:
            raise ValueError(f"{k} musí mať dĺžku {N96} alebo byť prázdne (dostal som {len(body[k])})")
    # JSON write (vždy — back-compat pre moduly ktoré ešte nepoznajú DB)
    with open(p, "w") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)
    # DB write (dual storage)
    if _db_available():
        try:
            from db import get_session
            from db.models import Profile as _DbProfile
            with get_session() as s:
                existing_db = s.query(_DbProfile).filter_by(name=body["name"]).one_or_none()
                if existing_db:
                    existing_db.mode = body["mode"]
                    existing_db.note = body["note"]
                    existing_db.updated_at = body["updated_at"]
                    existing_db.plan = body["plan"]
                    existing_db.dentrh = body["dentrh"]
                    existing_db.rt = body["rt"]
                    existing_db.distribution = body["distribution"]
                    existing_db.mult96 = body["mult96"]
                    existing_db.rt_on96 = body["rt_on96"]
                else:
                    s.add(_DbProfile(
                        name=body["name"], mode=body["mode"], note=body["note"],
                        created_at=body["created_at"], updated_at=body["updated_at"],
                        plan=body["plan"], dentrh=body["dentrh"], rt=body["rt"],
                        distribution=body["distribution"],
                        mult96=body["mult96"], rt_on96=body["rt_on96"],
                    ))
        except Exception as e:
            print(f"[profiles.save_profile {body['name']}] DB write zlyhal: {e}")
    # Audit log — profile_save je relevantný write event (Fáza B.4)
    try:
        from core.audit_log import log_event
        log_event(actor="profile_writer", action="profile_save",
                   profile=body["name"], mode=body["mode"],
                   created_now=(body["created_at"] == body["updated_at"]))
    except Exception:
        pass
    return p


def get_mode(name: str) -> str:
    """Vráti mode daného profilu ('simulation'|'real'). Default 'simulation' pri zlom mene
    alebo starých profiloch bez mode."""
    if not name:
        return MODE_SIM
    p = load_profile(name)
    if not p:
        return MODE_SIM
    m = str(p.get("mode") or "").strip().lower()
    return m if m in VALID_MODES else MODE_SIM


def is_real(name: str) -> bool:
    """True ak je profile real (povolený realio overlay a manual writes)."""
    return get_mode(name) == MODE_REAL


def list_by_mode(mode: str) -> List[str]:
    """Vráti zoznam profilov daného mode-u."""
    if mode not in VALID_MODES:
        return []
    return [n for n in list_profiles() if get_mode(n) == mode]


def load_profile(name: str) -> Optional[Dict[str, Any]]:
    """Načíta profile alebo None ak neexistuje / je poškodený."""
    safe = _safe_name(name)
    if _db_available():
        try:
            from db import get_session
            from db.models import Profile as _DbProfile
            with get_session() as s:
                p = s.query(_DbProfile).filter_by(name=safe).one_or_none()
                if p is not None:
                    return {
                        "name": p.name, "mode": p.mode, "note": p.note,
                        "created_at": p.created_at, "updated_at": p.updated_at,
                        "plan": _ensure_plan_vdt_defaults(dict(p.plan or {})),
                        "dentrh": dict(p.dentrh or {}),
                        "rt": dict(p.rt or {}),
                        "distribution": dict(p.distribution or {}),
                        "mult96": list(p.mult96 or []),
                        "rt_on96": list(p.rt_on96 or []),
                    }
        except Exception as e:
            print(f"[profiles.load_profile {safe}] DB zlyhal, fallback na JSON: {e}")
    # JSON storage
    p = _path(name)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            data = json.load(f)
        # Bug P: auto-add VDT defaults do plan dict (lazy migration)
        if isinstance(data, dict) and "plan" in data:
            data["plan"] = _ensure_plan_vdt_defaults(data["plan"] or {})
        return data
    except (OSError, json.JSONDecodeError):
        return None


# ── Fáza A.1: Pydantic ProfileConfig API ────────────────────────────────────
# Validated načítanie + zápis cez core.schemas.ProfileConfig. Tieto funkcie
# NEnahrádzajú load_profile/save_profile (back-compat) — sú DOPLNKOVÉ pre
# kód ktorý chce type-safe access (Bug UU, TT, 441 by sa nedali vyrobiť).

def load_profile_validated(name: str):
    """Vráti ProfileConfig (Pydantic) alebo None ak neexistuje/poškodený.

    Validuje schemu + auto-fill defaults pre chýbajúce polia. Pri parse error
    zachová pôvodný dict cez load_profile() — len logne warning.
    """
    raw = load_profile(name)
    if raw is None:
        return None
    try:
        from core.schemas import ProfileConfig
        # Pred validation zabezpečíme name field (load_profile niekedy chýba meno
        # v starých JSON-och — bere ho z filename)
        if not raw.get("name"):
            raw["name"] = _safe_name(name)
        return ProfileConfig.model_validate(raw)
    except Exception as e:
        print(f"[profiles.load_profile_validated {name}] validation chyba: {e}")
        return None


def save_profile_validated(cfg) -> str:
    """Uloží ProfileConfig (Pydantic) cez existujúce save_profile API.

    Zápis stále ide cez save_profile() (zachová DB + JSON dual storage). Tu sa
    iba validuje, že vstup je validný ProfileConfig — a vyserialize do dict.
    """
    from core.schemas import ProfileConfig
    if not isinstance(cfg, ProfileConfig):
        raise TypeError(f"save_profile_validated potrebuje ProfileConfig, dostal {type(cfg)}")
    data = cfg.model_dump(exclude_none=False)
    return save_profile(cfg.name, data)


def delete_profile(name: str) -> bool:
    """Zmaže profile (DB aj JSON). True ak existoval aspoň v jednom úložisku.

    Bug #640 (2026-06-09): explicitne mažeme FK závislosti BEZ cascade pred
    samotným Profile (Plan, ActiveProfile, AutoControlEvent, BattCapacityReservation).
    Ostatné (VdtPaperTrade, PlanOverride, LoadProfile, UserProfileAccess) majú
    ORM cascade="all, delete-orphan" → vymažú sa automaticky.
    """
    safe = _safe_name(name)
    found = False
    # DB delete (dual storage)
    if _db_available():
        try:
            from db import get_session
            from db.models import Profile as _DbProfile
            with get_session() as s:
                p_db = s.query(_DbProfile).filter_by(name=safe).one_or_none()
                if p_db is None:
                    # Skús pôvodný (neescapovaný) názov — pri sandbox sa nemusí trafiť
                    p_db = s.query(_DbProfile).filter_by(name=str(name).strip()).one_or_none()
                if p_db is not None:
                    pid = p_db.id
                    # 1. Plans (Plan + PlanSlot cez cascade na plan_id)
                    try:
                        from db.models import Plan as _DbPlan
                        s.query(_DbPlan).filter_by(profile_id=pid).delete(synchronize_session=False)
                    except Exception as _e1:
                        print(f"[delete_profile #640] Plan delete zlyhal: {_e1}")
                    # 2. ActiveProfile (per-port lookup)
                    try:
                        from db.models import ActiveProfile as _DbAP
                        s.query(_DbAP).filter_by(profile_id=pid).delete(synchronize_session=False)
                    except Exception as _e2:
                        print(f"[delete_profile #640] ActiveProfile delete zlyhal: {_e2}")
                    # 3. AutoControlEvent (Optional FK)
                    try:
                        from db.models import AutoControlEvent as _DbACE
                        s.query(_DbACE).filter_by(profile_id=pid).delete(synchronize_session=False)
                    except Exception as _e3:
                        print(f"[delete_profile #640] AutoControlEvent delete zlyhal: {_e3}")
                    # 4. BattCapacityReservation (ondelete CASCADE — pre istotu manuálne)
                    try:
                        from db.models import BattCapacityReservation as _DbBCR
                        s.query(_DbBCR).filter_by(profile_id=pid).delete(synchronize_session=False)
                    except Exception as _e4:
                        print(f"[delete_profile #640] BattCapacityReservation delete zlyhal: {_e4}")
                    # 5. D1SocTrajectory (ondelete CASCADE — pre istotu manuálne)
                    try:
                        from db.models import D1SocTrajectory as _DbD1S
                        s.query(_DbD1S).filter_by(profile_id=pid).delete(synchronize_session=False)
                    except Exception:
                        pass            # tabuľka môže neexistovať v starých DB
                    # 6. UserProfileAccess (ORM cascade existuje, ale pre istotu)
                    try:
                        from db.models import UserProfileAccess as _DbUPA
                        s.query(_DbUPA).filter_by(profile_id=pid).delete(synchronize_session=False)
                    except Exception:
                        pass
                    # 7. Samotný Profile (cascade odstráni zvyšné rels: VDT, plan_overrides,
                    #    load_profiles)
                    s.delete(p_db)
                    found = True
                    print(f"[delete_profile #640] {safe} (id={pid}) odstránený z DB")
        except Exception as e:
            print(f"[profiles.delete_profile {safe}] DB delete zlyhal: {e}")
            raise   # bug #640: prebublať chybu nahor, nech UI vidí dôvod
    # JSON delete (vždy) — sandbox aj legacy path
    p = _path(name)
    if os.path.exists(p):
        try:
            os.remove(p)
            found = True
        except OSError as _e_fs:
            print(f"[profiles.delete_profile {safe}] FS delete zlyhal: {_e_fs}")
    return found


def _active_market() -> str:
    """Aktuálny market ('cz' alebo 'sk') pre per-port ActiveProfile lookup."""
    try:
        import market as _mk
        return str(_mk.active_market() or "cz")
    except Exception:
        return "cz"


def get_active() -> Optional[str]:
    """Vráti názov aktívneho profilu alebo None."""
    if _db_available():
        try:
            from db import get_session
            from db.models import ActiveProfile, Profile as _DbProfile
            with get_session() as s:
                ap = s.query(ActiveProfile).filter_by(
                    port=_PORT, market=_active_market()
                ).one_or_none()
                if ap and ap.profile_id:
                    p = s.query(_DbProfile).filter_by(id=ap.profile_id).one_or_none()
                    if p:
                        return p.name
        except Exception as e:
            print(f"[profiles.get_active] DB read zlyhal, fallback na JSON: {e}")
    # JSON storage
    if not os.path.exists(_active_path()):
        return None
    try:
        with open(_active_path()) as f:
            d = json.load(f)
        n = d.get("name")
        if n and os.path.exists(_path(n)):
            return n
    except (OSError, json.JSONDecodeError):
        pass
    return None


def set_active(name: Optional[str]) -> None:
    """Označí profile ako aktívny (alebo None = žiadny aktívny).

    DIAG: vypíše krátky stack pre audit Bug G (per-port inconsistency).
    Plus zapíše do audit_log pre post-mortem analýzu (Fáza B.4).
    """
    try:
        import traceback as _tb
        _stack = _tb.extract_stack(limit=8)[:-1]
        _caller = " <- ".join(
            f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}"
            for f in _stack[-4:]
        )
        print(f"[profiles.set_active] name={name!r} | caller: {_caller}")
        # Audit log — low-frequency event (každý profile switch je relevantný)
        try:
            from core.audit_log import log_event
            log_event(actor="profile_resolver", action="profile_set_active",
                       profile=name, port=str(_PORT),
                       caller=_caller[:200])
        except Exception:
            pass
    except Exception:
        pass
    _ensure_dir()
    # JSON write (vždy back-compat)
    if name is None:
        if os.path.exists(_active_path()):
            try:
                os.remove(_active_path())
            except OSError:
                pass
    else:
        with open(_active_path(), "w") as f:
            json.dump({"name": _safe_name(name),
                        "set_at": datetime.now().isoformat(timespec="seconds")}, f)
    # DB write (dual storage)
    if _db_available():
        try:
            from db import get_session
            from db.models import ActiveProfile, Profile as _DbProfile
            with get_session() as s:
                mkt = _active_market()
                ap = s.query(ActiveProfile).filter_by(port=_PORT, market=mkt).one_or_none()
                if name is None:
                    if ap:
                        s.delete(ap)
                else:
                    p_db = s.query(_DbProfile).filter_by(name=_safe_name(name)).one_or_none()
                    if p_db:
                        if ap:
                            ap.profile_id = p_db.id
                            ap.set_at = datetime.now().isoformat(timespec="seconds")
                        else:
                            s.add(ActiveProfile(port=_PORT, market=mkt,
                                                  profile_id=p_db.id,
                                                  set_at=datetime.now().isoformat(timespec="seconds")))
        except Exception as e:
            print(f"[profiles.set_active {name}] DB write zlyhal: {e}")


def apply_to_ui_and_overrides(name: str, ui_save_fn, po_module) -> Dict[str, Any]:
    """Aplikuje profile na bežiacu aplikáciu:
    - prepíše ui_settings.plan / dentrh / rt
    - prepíše plan_overrides._template.json (mult96 + rt_on96)
    Vracia dict so súhrnom (čo sa zmenilo).

    ui_save_fn: callable(key, dict) — typicky app._ui_save
    po_module: plan_overrides modul
    """
    p = load_profile(name)
    if p is None:
        raise FileNotFoundError(f"profile '{name}' neexistuje")
    summary = {"applied": _safe_name(name), "updated": []}
    # ── DÔLEŽITÉ poradie: set_active MUSÍ byť PRED save_template ──
    # po.save_template() volá _resolve_profile() ktorý vracia aktívny profil.
    # Keby sme zavolali save_template PRED set_active, mult96 nového profilu by sa zapísalo
    # do priečinka STARÉHO aktívneho profilu → strata dát.
    # Bug Q: cez core.profile_resolver — aby sa zároveň cleanupol env var FTV_PROFILE
    # a legacy ui_settings.realio_profile (single source of truth).
    try:
        from core.profile_resolver import set_active as _resolver_set
        _resolver_set(name)
    except Exception:
        set_active(name)                          # fallback na lokálnu funkciu
    if p.get("plan"):
        ui_save_fn("plan", p["plan"])
        summary["updated"].append("ui.plan")
    if p.get("dentrh"):
        ui_save_fn("dentrh", p["dentrh"])
        summary["updated"].append("ui.dentrh")
    if p.get("rt"):
        ui_save_fn("rt", p["rt"])
        summary["updated"].append("ui.rt")
    # prepíš plan_overrides.template ak má profile vlastnú šablónu (do priečinka NOVÉHO aktívneho profilu)
    if po_module is not None and p.get("mult96") and len(p["mult96"]) == N96:
        import numpy as _np
        arr = _np.array([(float(x) if x is not None else _np.nan) for x in p["mult96"]], dtype=float)
        po_module.save_template(arr)
        summary["updated"].append("po.template_mult")
    if po_module is not None and p.get("rt_on96") and len(p["rt_on96"]) == N96:
        import numpy as _np
        arr = _np.array([(float(x) if x is not None else _np.nan) for x in p["rt_on96"]], dtype=float)
        po_module.save_template_rt(arr)
        summary["updated"].append("po.template_rt")
    return summary


def snapshot_current(name: str, ui_load_fn, po_module, note: str = "",
                      mode: str = MODE_SIM) -> str:
    """Vytvorí profile zo SÚČASNÝCH UI hodnôt + plan_overrides template.
    Užitočné pre 'Uložiť aktuálne nastavenia ako profil'.

    Mode: 'simulation' (default — historická simulácia) alebo 'real' (reálny chod).
    Mode je fixovaný pri vzniku — nedá sa neskôr zmeniť.
    """
    data = {
        "plan": ui_load_fn("plan", {}),
        "dentrh": ui_load_fn("dentrh", {}),
        "rt": ui_load_fn("rt", {}),
        "note": note,
        "mode": mode,
    }
    # Bug SNAPSHOT-MERGE (2026-06-12): snapshot NAHRÁDZAL celé sekcie UI stavom —
    # kľúče, ktoré UI stav nepozná (rt.engine + rt.rt2_* z RT poradcu 2.0, prípadné
    # ďalšie profile-only polia), sa pri každom auto-save po /plan ticho MAZALI
    # (užívateľ: "zmením engine a vráti sa na v1"). Merge: existujúci profil je
    # podklad, UI stav prepíše len kľúče, ktoré reálne nesie.
    try:
        _existing = load_profile(name) or {}
        for _sec in ("plan", "dentrh", "rt"):
            _ex = _existing.get(_sec) or {}
            if _ex:
                data[_sec] = {**_ex, **(data.get(_sec) or {})}
    except Exception:
        pass
    if po_module is not None:
        try:
            m = po_module.load_template().tolist()
            rt = po_module.load_template_rt().tolist()
            # mult/rt sú np arrays s NaN — pre JSON treba None
            data["mult96"] = [None if x != x else float(x) for x in m]   # NaN check
            data["rt_on96"] = [None if x != x else float(x) for x in rt]
        except Exception:
            pass
    return save_profile(name, data)
