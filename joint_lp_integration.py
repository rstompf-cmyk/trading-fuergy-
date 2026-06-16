"""Wrapper pre joint_lp → drop-in nahrádza optimize_day keď je Joint LP zapnutý.

API:
    optimize_day_or_joint(pv_kwh, price_eur, *, joint_flags=None, ...) -> (sch, summary)

`joint_flags` je dict:
    {
      "enabled": bool,
      "trade_batt": bool,
      "trade_ftv": bool,
      "trade_load": bool,
      "use_vdt": bool,
      "optimize_distribution": bool,
    }

Ak `enabled=False` (alebo `joint_flags=None`), volá pôvodný `optimizer.optimize_day`.
Inak volá `joint_lp.optimize_joint_day` a prevedie výstup do rovnakého formátu
(DataFrame `sch` + dict `summary`) ako optimize_day.

Týmto dosiahneme:
- /plan a /dentrh môžu transparentne použiť Joint LP bez zmeny render kódu.
- Plan store dostane rovnaký schedule formát (kompatibilný s livesim, VDT chart).
"""
from __future__ import annotations
from typing import Optional, Dict, Any, Tuple
import numpy as np
import pandas as pd


DEFAULT_FLAGS = {
    "enabled": False,
    "trade_batt": True,
    "trade_ftv": True,
    "trade_load": True,
    "use_vdt": False,           # VDT default OFF (vyžaduje VDT ceny ako input)
    "optimize_distribution": False,
}


def normalize_flags(flags: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    """Vráti kompletný dict všetkých 6 flagov so správnymi defaultami."""
    out = dict(DEFAULT_FLAGS)
    if flags:
        for k in DEFAULT_FLAGS:
            if k in flags:
                out[k] = bool(flags[k])
    return out


def get_flags_from_profile(profile_name: Optional[str] = None) -> Dict[str, bool]:
    """Načíta joint_lp flags z profilu (profile.plan.joint_lp).

    Ak profil nemá joint_lp sekciu, vráti DEFAULT_FLAGS.
    """
    if not profile_name or profile_name == "default":
        return dict(DEFAULT_FLAGS)
    try:
        import profiles as _pr
        prof = _pr.load_profile(profile_name) or {}
        plan = prof.get("plan") or {}
        flags = plan.get("joint_lp") or {}
        return normalize_flags(flags)
    except Exception:
        return dict(DEFAULT_FLAGS)


def save_flags_to_profile(profile_name: str, flags: Dict[str, bool]) -> bool:
    """Uloží joint_lp flags do profilu (sekcia plan.joint_lp).

    Returns True ak save prešiel.
    """
    try:
        import profiles as _pr
        prof = _pr.load_profile(profile_name) or {}
        if "plan" not in prof:
            prof["plan"] = {}
        prof["plan"]["joint_lp"] = normalize_flags(flags)
        _pr.save_profile(profile_name, prof)
        return True
    except Exception as e:
        print(f"[joint_lp_integration] save flags zlyhal: {e}")
        return False


def _joint_to_optimize_day_format(
        joint_res: Dict[str, Any],
        pv: np.ndarray, price: np.ndarray,
        batt_kwh: float, dt: float,
        grid_fee: float, cycle_cost: float,
        settle_price: Optional[np.ndarray] = None,
        max_export_kwh_day: Optional[float] = None,
        max_import_kwh_day: Optional[float] = None,
        load_kwh: Optional[np.ndarray] = None,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Prevedie výstup joint_lp na (DataFrame sch, dict summary) formát.

    Cieľ: byť bit-by-bit kompatibilný s optimizer.optimize_day výstupom
    aby downstream kód (plan_store, livesim, VDT chart) fungoval bez zmeny.
    """
    T = joint_res["T"]
    ch = np.asarray(joint_res["ch_kwh"])
    di = np.asarray(joint_res["di_kwh"])
    ex = np.asarray(joint_res["ex_kwh"])    # DAM + VDT spolu
    im = np.asarray(joint_res["im_kwh"])
    cu = np.asarray(joint_res["cu_kwh"])
    soc_kwh = np.asarray(joint_res["soc_kwh"])

    # grid_kwh = fyzický grid balance (zachované pre downstream — livesim, VDT chart, settlement)
    grid = ex - im

    # order_mwh = stĺpec "Obchod MWh" — IBA toggle-aktívne streams (decomposition fix #513).
    # Tým pri trade_ftv=False FTV export sa nezobrazí ako obchod (lebo je len intern cez batt),
    # pri trade_load=False import na load sa nezobrazí (lebo je iba interný flow), atď.
    # DIST je iba v účelovke — nikdy nie v obchode.
    if "obchod_kwh" in joint_res:
        order = np.round(np.asarray(joint_res["obchod_kwh"]) / 1000.0, 3)
    else:
        # Fallback ak je starý joint_lp bez decomposition
        order = np.round(grid / 1000.0, 3)
    spr = price if settle_price is None else np.asarray(settle_price, float)

    # Spotreba (informačne) — parita s optimizer.optimize_day výstupom (sch["load_kwh"])
    if load_kwh is not None:
        _load_arr = np.asarray(load_kwh, float).reshape(-1)[:T]
        if _load_arr.size < T:
            _load_arr = np.concatenate([_load_arr, np.zeros(T - _load_arr.size)])
    else:
        _load_arr = np.zeros(T, dtype=float)
    sch = pd.DataFrame({
        "hour": range(T),
        "pv_kwh": np.round(pv, 1),
        "price_eur": np.round(spr, 1),
        "batt_kw": np.round((di - ch) / dt, 1),
        "grid_kwh": np.round(grid, 1),
        "order_mwh": order,
        "curtail_kwh": np.round(cu, 1),
        "soc_pct": np.round(soc_kwh / batt_kwh * 100, 0),
        "_charge_kw": np.round(ch / dt, 1),
        "_discharge_kw": np.round(di / dt, 1),
        "_export_kwh": np.round(ex, 1),
        "_import_kwh": np.round(im, 1),
        "soc_kwh": np.round(soc_kwh, 1),
        "load_kwh": np.round(_load_arr, 2),
    })

    econ = joint_res.get("economics", {})
    rev_ex = float(econ.get("dam_revenue_eur", 0) + econ.get("vdt_revenue_eur", 0))
    cost_im = float(econ.get("dam_cost_eur", 0) + econ.get("vdt_cost_eur", 0)
                     + econ.get("grid_fee_eur", 0))
    cyc = float(econ.get("cycle_cost_eur", 0))
    net = float(econ.get("net_profit_eur", 0))
    base = float((np.where(spr > 0, spr, 0) * pv).sum() / 1000)

    flags = joint_res.get("flags", {})
    summary = {
        "trzba_export_EUR": round(rev_ex, 2),
        "naklad_import_EUR": round(cost_im, 2),
        "naklad_cyklus_EUR": round(cyc, 2),
        "ZISK_EUR": round(net, 2),
        "bez_baterie_EUR": round(base, 2),
        "prinos_baterie_EUR": round(net - base, 2),
        "nabite_kWh": round(float(ch.sum()), 1),
        "vybite_kWh": round(float(di.sum()), 1),
        "import_kWh": round(float(im.sum()), 1),
        "orezane_kWh": round(float(cu.sum()), 1),
        "block_planned_discharge": False,
        "cap_export_kWh": (float(max_export_kwh_day)
                            if max_export_kwh_day and float(max_export_kwh_day) > 0 else None),
        "cap_import_kWh": (float(max_import_kwh_day)
                            if max_import_kwh_day and float(max_import_kwh_day) > 0 else None),
        "cap_applied": None,
        "cap_warning": "",
        # Joint LP specific
        "_joint_lp": True,
        "_joint_flags": flags,
        "_joint_economics": econ,
        "_joint_dam_revenue": econ.get("dam_revenue_eur", 0),
        "_joint_vdt_revenue": econ.get("vdt_revenue_eur", 0),
        "_joint_dam_cost": econ.get("dam_cost_eur", 0),
        "_joint_vdt_cost": econ.get("vdt_cost_eur", 0),
        "_joint_tou_cost": econ.get("tou_cost_eur", 0),
        "_joint_tou_baseline": econ.get("tou_baseline_eur", 0),
        "_joint_tou_savings": econ.get("tou_savings_eur", 0),
    }
    return sch, summary


def vdt_committed_kw_for_day(profile: str, date_iso: str, T: int,
                              step_min: int = 60):
    """Bug LP-VDT-BOUNDS (2026-06-11): per-slot kW UŽ uzavretých VDT obchodov dňa
    (+ = discharge, − = charge) v rozlíšení plánu (T=24 → hodinový priemer 4×15-min,
    T=96 → 1:1). None ak obchody nie sú / profil neexistuje.

    Použitie: regen plánu pre deň s obchodmi musí dať LP smerové stropy
    |dam + vdt| ≤ batt_kw, inak nominácia + obchody prekročia fyziku batérie."""
    try:
        import vdt_state as _vs
        arr96 = _vs.get_realized_batt_kw(profile, today_iso=str(date_iso)[:10],
                                          dt_h=0.25)
        if not isinstance(arr96, list) or len(arr96) < 96:
            return None
        if not any(abs(float(v or 0.0)) > 0.01 for v in arr96):
            return None
        a = np.asarray([float(v or 0.0) for v in arr96], float)
        if int(step_min) == 60 and T == 24:
            return a.reshape(24, 4).mean(axis=1)
        return a[:T]
    except Exception as _e:
        print(f"[vdt_committed_kw_for_day] {profile}/{date_iso}: {_e}")
        return None


def optimize_day_or_joint(
        pv_kwh, price_eur, *,
        joint_flags: Optional[Dict[str, bool]] = None,
        profile: Optional[str] = None,
        # joint_lp extra inputs
        vdt_buy_price: Optional[np.ndarray] = None,
        vdt_sell_price: Optional[np.ndarray] = None,
        tou_price_eur: Optional[np.ndarray] = None,
        # parametre rovnaké ako optimize_day
        batt_kw: float = 100.0, batt_kwh: float = 200.0,
        eff_c: float = 0.95, eff_d: float = 0.95,
        soc_min_pct: float = 5, soc_max_pct: float = 95,
        soc_init_pct: float = 50,
        soc_reserve_pct: float = 0.0,
        rt_grid_reserve_pct: float = 0.0,
        terminal_soc_pct: Optional[float] = None,
        grid_kw: float = 100.0,
        grid_kw_export: Optional[float] = None,
        grid_kw_import: Optional[float] = None,
        grid_fee: float = 22.0,
        cycle_cost: float = 2.0,
        allow_grid_charge: bool = True,
        allow_curtail: bool = True,
        min_spread_eur: float = 0.0,
        min_trade_mwh: float = 0.0,
        block_neg_import: bool = False,
        max_cycles: Optional[float] = None,
        batt_kw_override=None,
        block_planned_discharge: bool = False,
        settle_price: Optional[np.ndarray] = None,
        load_kwh: Optional[np.ndarray] = None,
        max_export_kwh_day: Optional[float] = None,
        max_import_kwh_day: Optional[float] = None,
        dt: float = 1.0,
        vdt_committed_kw=None,   # Bug LP-VDT-BOUNDS: per-slot kW uzavretých VDT obchodov dňa
        vdt_capacity_reserve_kw: float = 0.0,   # Bug VDT-CAP-RESERVE: headroom kW pre VDT/RT
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Drop-in nahradenie optimize_day s podporou Joint LP.

    Ak `joint_flags` je None alebo má `enabled=False`, volá pôvodný optimize_day.
    Ak `enabled=True`, volá joint_lp s ostatnými parametrami.

    Args:
        joint_flags: dict s kľúčmi enabled/trade_batt/trade_ftv/trade_load/use_vdt/
                     optimize_distribution. Ak None, načíta z profilu.
        profile: meno profilu pre lookup flagov (ak joint_flags je None).
        vdt_buy_price, vdt_sell_price: VDT ask/bid €/MWh per slot (potrebné pre use_vdt).
        tou_price_eur: TOU distribučná sadzba €/MWh per slot (potrebná pre optimize_distribution).

    Returns:
        (DataFrame sch, dict summary) — rovnaký formát ako optimize_day.
    """
    # Resolve flags
    if joint_flags is None:
        joint_flags = get_flags_from_profile(profile)
    else:
        joint_flags = normalize_flags(joint_flags)
    # Bug #641: diagnostika — vidieť či sa volá joint LP alebo classic optimize_day
    print(f"[optimize_day_or_joint] profile={profile!r}, "
          f"joint_enabled={joint_flags.get('enabled')}, "
          f"batt_kw={batt_kw:.0f}, batt_kwh={batt_kwh:.0f}, "
          f"soc_reserve_pct={soc_reserve_pct:.1f}, "
          f"max_dam_im={max_import_kwh_day}/ex={max_export_kwh_day}")

    # ── Toggle = LEN OBCHOD, NIE FYZICKÁ BILANCIA ──────────────────────────
    # User (2026-06-16): odber a FTV (ak sú v profile) MUSIA vždy vstúpiť do
    # bilancie pre RIADENIE batérie a fyzické limity prahového elektromera
    # (jeden odberné/odovzdávacie miesto). Toggle BAT/FTV/LOAD riadi LEN
    # vyhodnotenie a plánovanie OBCHODU (predaj/nominácia), NIE fyzickú prítomnosť.
    #   - z pohľadu stavu v odbernom mieste je load+FTV vždy reálny,
    #   - ak ich užívateľ nechce, jednoducho ich nedá do profilu (žiadne dáta).
    # Preto load/FTV NEVYNULUJEME na vstupe. Predaj FTV ostáva gejtovaný v solveri
    # cez trade_ftv (EX_FTV=0 → FTV kryje load/batériu, ale nepredáva sa).
    pv_arr_real = np.asarray(pv_kwh, float)
    load_arr_real = (np.asarray(load_kwh, float).reshape(-1)[:len(pv_arr_real)]
                     if load_kwh is not None else None)

    pv_for_lp = pv_arr_real.copy()
    load_for_lp = load_arr_real.copy() if load_arr_real is not None else None

    # Bug LP-VDT-BOUNDS: smerové per-slot stropy z uzavretých VDT obchodov.
    # |dam + vdt| ≤ batt_kw ⇒ dis_cap = batt_kw − vdt, chg_cap = batt_kw + vdt
    # (vdt: + discharge, − charge; clip do [0, batt_kw] robí LP).
    _vdt_dis_cap = None
    _vdt_chg_cap = None
    if vdt_committed_kw is not None:
        try:
            _vdt_arr = np.asarray(vdt_committed_kw, float).reshape(-1)[:len(pv_arr_real)]
            if _vdt_arr.size and np.any(np.abs(_vdt_arr) > 0.01):
                _vdt_dis_cap = float(batt_kw) - _vdt_arr
                _vdt_chg_cap = float(batt_kw) + _vdt_arr
                _n_aff = int(np.sum(np.abs(_vdt_arr) > 0.01))
                print(f"[LP-VDT-BOUNDS] {profile}: {_n_aff} slotov s uzavretými VDT "
                      f"obchodmi → LP dostáva smerové stropy (max vdt "
                      f"{np.max(np.abs(_vdt_arr)):.0f} kW)")
        except Exception as _e_vb:
            print(f"[LP-VDT-BOUNDS] príprava stropov zlyhala: {_e_vb}")
    # Bug VDT-CAP-RESERVE (2026-06-11): explicitný headroom pre VDT/RT — D-1 LP
    # nominuje max (batt_kw − rezerva) v oboch smeroch. Kombinuje sa (min) so
    # stropmi z už uzavretých obchodov. Generické: hodnota z parametra profilu.
    try:
        _res_kw = max(0.0, float(vdt_capacity_reserve_kw or 0.0))
    except (TypeError, ValueError):
        _res_kw = 0.0
    if _res_kw > 0:
        _cap_base = max(0.0, float(batt_kw) - _res_kw)
        _base_arr = np.full(len(pv_arr_real), _cap_base, dtype=float)
        _vdt_dis_cap = _base_arr if _vdt_dis_cap is None else np.minimum(_vdt_dis_cap, _base_arr)
        _vdt_chg_cap = _base_arr if _vdt_chg_cap is None else np.minimum(_vdt_chg_cap, _base_arr)
        print(f"[VDT-CAP-RESERVE] {profile}: D-1 LP strop {float(batt_kw):.0f}−{_res_kw:.0f}"
              f" = {_cap_base:.0f} kW (headroom pre VDT/RT)")

    # POZN.: load/FTV sa už NEVYNULUJÚ podľa toggle (viď komentár vyššie).
    # Fyzická bilancia (riadenie batérie + grid limity) vždy vidí reálny load+FTV.
    # Obchodné gejty (trade_ftv = predaj FTV, trade_load = grid kryje load) sa
    # propagujú do solvera nižšie a riadia LEN obchodnú/nominačnú stránku.
    # Joint LP vypnutý → klasický optimizer používa pôvodné vstupy (toggle len pre Joint LP)

    # Fallback na pôvodný optimize_day ak joint LP nie je zapnutý
    if not joint_flags.get("enabled"):
        from optimizer import optimize_day as _od
        return _od(
            pv_arr_real, price_eur,
            batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
            soc_min_pct=soc_min_pct, soc_max_pct=soc_max_pct, soc_init_pct=soc_init_pct,
            soc_reserve_pct=soc_reserve_pct,
            rt_grid_reserve_pct=rt_grid_reserve_pct,
            grid_kw=grid_kw, grid_kw_export=grid_kw_export, grid_kw_import=grid_kw_import,
            grid_fee=grid_fee, cycle_cost=cycle_cost,
            allow_grid_charge=allow_grid_charge, terminal_soc_pct=terminal_soc_pct,
            allow_curtail=allow_curtail,
            min_spread_eur=min_spread_eur, min_trade_mwh=min_trade_mwh,
            block_neg_import=block_neg_import,
            max_cycles=max_cycles, batt_kw_override=batt_kw_override,
            block_planned_discharge=block_planned_discharge,
            settle_price=settle_price,
            load_kwh=load_arr_real,
            max_export_kwh_day=max_export_kwh_day, max_import_kwh_day=max_import_kwh_day,
            dt=dt,
            batt_dis_cap_kw=_vdt_dis_cap, batt_chg_cap_kw=_vdt_chg_cap,
        )

    # Joint LP path
    import joint_lp as _jlp
    # LP dostane už ZFILTROVANÉ pv_for_lp / load_for_lp (toggle vypnutý → 0)
    pv = pv_for_lp
    pr = np.asarray(price_eur, float)
    T = len(pv)
    load = (load_for_lp if load_for_lp is not None else np.zeros(T))
    if load.size < T:
        load = np.concatenate([load, np.zeros(T - load.size)])

    # Auto-load TOU prices ak optimize_distribution=True a tou_price_eur nie je zadané
    # + vynulovať grid_fee aby sa nezdvojovalo s per-slot TOU cenami (single source of truth)
    if joint_flags.get("optimize_distribution") and profile:
        try:
            import distribution_cost as _dc
            import datetime as _dt_dc
            dc_cfg = _dc.get_config(profile)
            if dc_cfg.get("enabled"):
                if tou_price_eur is None:
                    # Použijeme dnešný dátum ako fallback (LP horizont je 24h)
                    _date_for_tou = _dt_dc.date.today()
                    tou_price_eur = _dc.tou_prices_for_day(dc_cfg, _date_for_tou,
                                                            T=T, dt=dt)
                # Distribučné poplatky idú per-slot cez tou_price_eur — grid_fee
                # by spôsobil duplicitu (raz v c[IM]+=tou, raz v c[IM]+=grid_fee).
                # Single source of truth = distribution config.
                if grid_fee and grid_fee > 0:
                    print(f"[joint_lp_integration] optimize_distribution=True → grid_fee={grid_fee} ignorovaný (TOU zo distribúcie)")
                    grid_fee = 0.0
        except Exception as _e_dc:
            print(f"[joint_lp_integration] TOU auto-load zlyhal: {_e_dc}")

    res = _jlp.optimize_joint_day(
        pv_kwh=pv, load_kwh=load, dam_price_eur=pr,
        batt_kw=batt_kw, batt_kwh=batt_kwh,
        eff_c=eff_c, eff_d=eff_d,
        soc_min_pct=soc_min_pct, soc_max_pct=soc_max_pct,
        soc_init_pct=soc_init_pct,
        soc_reserve_pct=soc_reserve_pct,         # Bug #644: chýbalo
        terminal_soc_pct=terminal_soc_pct,
        # Bug GRID-EXPORT-ZERO: `x or grid_kw` bralo 0.0 ako falsy → tvrdý limit
        # export=0 (TBB: žiadny predaj do siete) sa prepísal na grid_kw=1100.
        # Správne: 0.0 je platný limit, fallback len pri None.
        grid_kw_import=(grid_kw_import if grid_kw_import is not None else grid_kw),
        grid_kw_export=(grid_kw_export if grid_kw_export is not None else grid_kw),
        grid_fee=grid_fee, cycle_cost=cycle_cost,
        min_spread_eur=min_spread_eur,   # Bug JOINT-MIN-SPREAD: parita s classic optimize_day
        vdt_buy_price=vdt_buy_price, vdt_sell_price=vdt_sell_price,
        tou_price_eur=tou_price_eur,
        trade_batt=joint_flags["trade_batt"],
        trade_ftv=joint_flags["trade_ftv"],
        trade_load=joint_flags["trade_load"],
        use_vdt=joint_flags["use_vdt"],
        optimize_distribution=joint_flags["optimize_distribution"],
        max_cycles=max_cycles,
        # parita s optimizer.optimize_day — propagovať šablónu × + denné kapy + flagy
        batt_kw_override=batt_kw_override,
        max_export_kwh_day=max_export_kwh_day,
        max_import_kwh_day=max_import_kwh_day,
        allow_curtail=allow_curtail,
        allow_grid_charge=allow_grid_charge,
        block_planned_discharge=block_planned_discharge,
        block_neg_import=block_neg_import,
        dt=dt,
        batt_dis_cap_kw=_vdt_dis_cap, batt_chg_cap_kw=_vdt_chg_cap,
    )

    if not res.get("ok"):
        # Fallback: ak Joint LP zlyhal, skús pôvodný optimize_day a pridaj warning
        from optimizer import optimize_day as _od
        sch, summary = _od(
            pv_kwh, price_eur,
            batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
            soc_min_pct=soc_min_pct, soc_max_pct=soc_max_pct, soc_init_pct=soc_init_pct,
            soc_reserve_pct=soc_reserve_pct,
            rt_grid_reserve_pct=rt_grid_reserve_pct,
            grid_kw=grid_kw, grid_kw_export=grid_kw_export, grid_kw_import=grid_kw_import,
            grid_fee=grid_fee, cycle_cost=cycle_cost,
            allow_grid_charge=allow_grid_charge, terminal_soc_pct=terminal_soc_pct,
            allow_curtail=allow_curtail,
            min_spread_eur=min_spread_eur, min_trade_mwh=min_trade_mwh,
            block_neg_import=block_neg_import,
            max_cycles=max_cycles, batt_kw_override=batt_kw_override,
            block_planned_discharge=block_planned_discharge,
            settle_price=settle_price,
            load_kwh=load_kwh,
            max_export_kwh_day=max_export_kwh_day, max_import_kwh_day=max_import_kwh_day,
            dt=dt,
            batt_dis_cap_kw=_vdt_dis_cap, batt_chg_cap_kw=_vdt_chg_cap,
        )
        summary["_joint_lp_attempted"] = True
        summary["_joint_lp_error"] = res.get("error", "?")
        summary["_joint_lp_fallback"] = True
        return sch, summary

    # Pre sched display použijeme PÔVODNÉ PV (pv_arr_real) — užívateľ vidí
    # skutočnú FTV výrobu v stĺpci, aj keď LP počítal s nulou. Tým je tabuľka
    # informatívna: vidíš čo FTV vyrobilo aj keď ho LP "ignoroval" kvôli toggle.
    return _joint_to_optimize_day_format(
        res, pv_arr_real, pr, batt_kwh, dt, grid_fee, cycle_cost,
        settle_price=settle_price,
        max_export_kwh_day=max_export_kwh_day,
        max_import_kwh_day=max_import_kwh_day,
        load_kwh=load_arr_real,
    )


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== joint_lp_integration smoke test ===")
    T = 24
    pv = np.maximum(0, 100 * np.sin(np.pi * (np.arange(T) - 6) / 12))
    load = np.full(T, 30.0)
    dam = 50 + 40 * np.sin(np.pi * (np.arange(T) - 12) / 24)

    print("\n1. joint_flags=None → pôvodný optimize_day:")
    sch, summ = optimize_day_or_joint(
        pv, dam,
        batt_kw=200.0, batt_kwh=400.0,
        load_kwh=load,
        joint_flags=None,
    )
    print(f"   ZISK_EUR = {summ['ZISK_EUR']}")
    print(f"   _joint_lp v summary? {'_joint_lp' in summ}")

    print("\n2. joint_flags={'enabled':True}, default ostatné:")
    sch2, summ2 = optimize_day_or_joint(
        pv, dam,
        batt_kw=200.0, batt_kwh=400.0,
        load_kwh=load,
        joint_flags={"enabled": True},
    )
    print(f"   ZISK_EUR = {summ2['ZISK_EUR']}")
    print(f"   _joint_lp = {summ2.get('_joint_lp')}")
    print(f"   _joint_flags = {summ2.get('_joint_flags')}")
    print(f"   schedule shape: {sch2.shape}, columns: {list(sch2.columns)}")

    print("\n3. enabled + trade_ftv=False:")
    sch3, summ3 = optimize_day_or_joint(
        pv, dam,
        batt_kw=200.0, batt_kwh=400.0,
        load_kwh=load,
        joint_flags={"enabled": True, "trade_ftv": False},
    )
    print(f"   ZISK_EUR = {summ3['ZISK_EUR']}")
    print(f"   orezane_kWh = {summ3['orezane_kWh']}")
