"""Joint LP — koherentné plánovanie FTV + Batt + Load + DAM + VDT v jednom kuse.

Rozdiel oproti `optimizer.optimize_day` + samostatné `vdt_extras`:
- Optimalizuje VŠETKO naraz (FTV curtail, batt arbitráž, grid im/ex, VDT extras)
  cez jednu účelovú funkciu — globálne optimálne riešenie.
- Toggle flags umožňujú vypnúť jednotlivé zložky → vidíš čistý efekt
  (napr. „bez VDT extras" alebo „bez FTV obchodu").
- Voliteľne zarátava distribučné náklady (TOU import sadzba per slot).

Premenné per slot t (T = 24 hodín alebo 96 × 15-min):
    ch[t]       — batéria nabíja (kWh, ≥0)
    di[t]       — batéria vybíja (kWh, ≥0)
    ex_dam[t]   — export cez DAM (kWh, ≥0)
    im_dam[t]   — import cez DAM (kWh, ≥0)
    ex_vdt[t]   — VDT extra predaj (kWh, ≥0)
    im_vdt[t]   — VDT extra nákup (kWh, ≥0)
    cu[t]       — orezanie FTV (kWh, ≥0)
    soc[t]      — stav nabitia (kWh, derived)

Energetická bilancia per slot:
    pv[t] − cu[t] + di[t] + im_dam[t] + im_vdt[t]
        = load[t] + ch[t] + ex_dam[t] + ex_vdt[t]
    ⇔  rearrange:
    di + im_dam + im_vdt − ex_dam − ex_vdt − ch − cu = load − pv

SOC dynamika:
    soc[t] = soc[t-1] + eff_c·ch[t] − di[t]/eff_d
    soc_min ≤ soc[t] ≤ soc_max
    soc[T-1] ≥ terminal_soc (ak je)

Účelová funkcia (maximalizovať profit):
    + Σ dam_price[t]   × ex_dam[t] / 1000           // predaj cez DAM
    + Σ vdt_sell_p[t]  × ex_vdt[t] / 1000           // predaj cez VDT
    − Σ dam_price[t]   × im_dam[t] / 1000           // nákup cez DAM
    − Σ vdt_buy_p[t]   × im_vdt[t] / 1000           // nákup cez VDT
    − Σ grid_fee       × (ex_*+im_*) / 1000         // sieťové poplatky
    − Σ cycle_cost     × (ch+di) / 1000 / 2         // opotrebenie batt
    − Σ tou_price[t]   × im_dam[t] / 1000           // distribúcia: TOU import sadzba (ak enabled)

Toggle flags (default = všetko zapnuté):
    trade_batt: bool          — povolí ch/di > 0 (batt arbitráž)
    trade_ftv: bool           — povolí FTV export (inak ex z FTV ide do cu)
    trade_load: bool          — povolí load z grid (im pokrýva nielen ch ale aj load)
    use_vdt: bool             — povolí im_vdt / ex_vdt (inak = 0)
    optimize_distribution: bool — zahrnúť tou_price do účelovky

API:
    optimize_joint_day(pv_kwh, load_kwh, dam_price_eur, *,
                       batt_kw, batt_kwh, eff_c, eff_d, soc_init_pct, ...,
                       vdt_buy_price=None, vdt_sell_price=None,
                       tou_price_eur=None,
                       trade_batt=True, trade_ftv=True, trade_load=True,
                       use_vdt=True, optimize_distribution=False,
                       dt=1.0) -> dict

Vracia dict v rovnakom tvare ako optimize_day (kompatibilita), plus extra
keys vdt_buy / vdt_sell schedules a economic breakdown.
"""
from __future__ import annotations
import numpy as np
from typing import Optional, Dict, Any


def optimize_joint_day(pv_kwh, load_kwh, dam_price_eur, *,
                        batt_kw: float = 100.0, batt_kwh: float = 200.0,
                        eff_c: float = 0.95, eff_d: float = 0.95,
                        soc_min_pct: float = 5.0, soc_max_pct: float = 95.0,
                        soc_init_pct: float = 50.0,
                        soc_reserve_pct: float = 0.0,
                        terminal_soc_pct: Optional[float] = None,
                        grid_kw_import: Optional[float] = None,
                        grid_kw_export: Optional[float] = None,
                        rt_grid_reserve_pct: float = 0.0,
                        grid_fee: float = 22.0, cycle_cost: float = 2.0,
                        min_spread_eur: float = 0.0,   # Bug JOINT-MIN-SPREAD (2026-06-11): parita s optimize_day
                        vdt_buy_price: Optional[np.ndarray] = None,
                        vdt_sell_price: Optional[np.ndarray] = None,
                        tou_price_eur: Optional[np.ndarray] = None,
                        # Toggle flags
                        trade_batt: bool = True,
                        trade_ftv: bool = True,
                        trade_load: bool = True,
                        use_vdt: bool = True,
                        optimize_distribution: bool = False,
                        # ostatné
                        max_cycles: Optional[float] = None,
                        # parita s optimizer.optimize_day — chýbajúce parametre
                        batt_kw_override=None,
                        max_export_kwh_day: Optional[float] = None,
                        max_import_kwh_day: Optional[float] = None,
                        allow_curtail: bool = True,
                        allow_grid_charge: bool = True,
                        block_planned_discharge: bool = False,
                        block_neg_import: bool = False,
                        dt: float = 1.0,
                        # Bug LP-VDT-BOUNDS (2026-06-11): smerové per-slot stropy (kW)
                        # z už uzavretých VDT obchodov dňa. None = plný batt_kw.
                        batt_dis_cap_kw=None, batt_chg_cap_kw=None) -> Dict[str, Any]:
    """Joint LP optimalizácia.

    Args:
        pv_kwh: [T] FTV výroba per slot (kWh)
        load_kwh: [T] spotreba per slot (kWh)
        dam_price_eur: [T] DAM clearing cena (€/MWh)
        batt_kw, batt_kwh: parametre batérie
        eff_c, eff_d: účinnosti
        soc_*_pct: SOC limity
        terminal_soc_pct: koncový SOC (None = ako start)
        grid_kw_import, grid_kw_export: limity siete kW (None = neobmedzené)
        grid_fee: poplatok €/MWh za prenos siete (DAM aj VDT smer)
        cycle_cost: opotrebenie €/MWh za 1 cyklus
        vdt_buy_price: [T] VDT ask €/MWh (None = use_vdt=False)
        vdt_sell_price: [T] VDT bid €/MWh (None = use_vdt=False)
        tou_price_eur: [T] distribučná TOU sadzba €/MWh per slot
        trade_batt: ak False, ch[t] = di[t] = 0 (batt vypnutá).
                    Ak True, batt môže ísť do/zo siete cez DAM (charge zo siete + discharge do siete = arbitráž).
        trade_ftv: ak False, FTV nemôže ísť do siete (musí kryť load alebo curtail).
                   Ak True, FTV nadbytky môžu byť exportované cez DAM.
        trade_load: ak False, grid nesmie kryť load (load musí byť pokrytá FTV alebo batt).
                    Ak True, grid môže importovať na pokrytie load.
        use_vdt: ak False, im_vdt[t] = ex_vdt[t] = 0
        optimize_distribution: ak True, do účelovky pripočítaj tou_price × im_dam
        max_cycles: max počet cyklov za deň
        dt: dĺžka slotu v hodinách (1.0 pre 60-min, 0.25 pre 15-min)

    Returns:
        dict s kľúčmi rovnakými ako optimize_day + vdt arrays:
        {
            "ok": bool, "error": str | None,
            "T": int, "dt": float,
            "ch_kwh": [T], "di_kwh": [T],
            "ex_dam_kwh": [T], "im_dam_kwh": [T],
            "ex_vdt_kwh": [T], "im_vdt_kwh": [T],
            "ex_kwh": [T] (= ex_dam + ex_vdt),  # backward compat
            "im_kwh": [T] (= im_dam + im_vdt),  # backward compat
            "cu_kwh": [T],
            "soc_pct": [T],
            "soc_after": [T],
            "_charge_kw": [T], "_discharge_kw": [T],
            "_export_kwh": [T], "_import_kwh": [T],
            "economics": {
                "dam_revenue_eur": float, "dam_cost_eur": float,
                "vdt_revenue_eur": float, "vdt_cost_eur": float,
                "grid_fee_eur": float, "cycle_cost_eur": float,
                "tou_cost_eur": float, "net_profit_eur": float,
            },
            "flags": {trade_batt, trade_ftv, trade_load, use_vdt, optimize_distribution},
        }
    """
    from scipy.optimize import linprog

    pv = np.asarray(pv_kwh, float)
    load = np.asarray(load_kwh, float).reshape(-1)
    pr_dam = np.asarray(dam_price_eur, float)
    T = len(pv)

    # Align lengths
    if load.size < T:
        load = np.concatenate([load, np.zeros(T - load.size)])
    else:
        load = load[:T]
    if pr_dam.size < T:
        # Pad poslednou hodnotou (alebo 0)
        pad = [pr_dam[-1] if len(pr_dam) > 0 else 0.0] * (T - pr_dam.size)
        pr_dam = np.concatenate([pr_dam, pad])
    else:
        pr_dam = pr_dam[:T]

    # VDT prices — ak nie sú zadané, vypnúť use_vdt
    if not use_vdt or vdt_buy_price is None or vdt_sell_price is None:
        use_vdt = False
        vdt_buy = np.zeros(T)
        vdt_sell = np.zeros(T)
    else:
        vdt_buy = np.asarray(vdt_buy_price, float).reshape(-1)[:T]
        vdt_sell = np.asarray(vdt_sell_price, float).reshape(-1)[:T]
        if vdt_buy.size < T:
            vdt_buy = np.concatenate([vdt_buy, np.zeros(T - vdt_buy.size)])
        if vdt_sell.size < T:
            vdt_sell = np.concatenate([vdt_sell, np.zeros(T - vdt_sell.size)])

    # TOU distribution price
    if tou_price_eur is None or not optimize_distribution:
        tou = np.zeros(T)
    else:
        tou = np.asarray(tou_price_eur, float).reshape(-1)[:T]
        if tou.size < T:
            tou = np.concatenate([tou, np.zeros(T - tou.size)])

    # Per-slot batt multiplier (× šablóna) — parita s optimizer.optimize_day.
    # mult=1.0 → bez zmeny, mult=0.5 → polovica batt_kw v slote, mult=0 → batt zablokovaná.
    if batt_kw_override is None:
        mults = np.ones(T, dtype=float)
    else:
        mults = np.asarray(batt_kw_override, float).reshape(-1)
        if mults.size < T:
            mults = np.concatenate([mults, np.ones(T - mults.size)])
        else:
            mults = mults[:T]
        # Sanity: clamp do [0, 5] (mults > 1 sú "boost" sloty)
        mults = np.clip(mults, 0.0, 5.0)

    # Bug LP-VDT-BOUNDS (2026-06-11): smerové per-slot stropy z uzavretých VDT
    # obchodov — |dam + vdt| ≤ batt_kw. None = plný batt_kw (back-compat).
    if batt_dis_cap_kw is not None:
        _dis_caps = np.clip(np.asarray(batt_dis_cap_kw, float).reshape(-1)[:T], 0.0, batt_kw)
        if _dis_caps.size < T:
            _dis_caps = np.concatenate([_dis_caps, np.full(T - _dis_caps.size, batt_kw)])
    else:
        _dis_caps = np.full(T, float(batt_kw))
    if batt_chg_cap_kw is not None:
        _chg_caps = np.clip(np.asarray(batt_chg_cap_kw, float).reshape(-1)[:T], 0.0, batt_kw)
        if _chg_caps.size < T:
            _chg_caps = np.concatenate([_chg_caps, np.full(T - _chg_caps.size, batt_kw)])
    else:
        _chg_caps = np.full(T, float(batt_kw))

    # SOC limity
    # Bug SOC-RESERVE-AUDIT-ONLY (2026-06-10): reserve sa NEAPLIKUJE na LP plánovanie.
    # User: "ak je pouzite pravido 15% rezerva, tak to ma sluzit pre audit nie pre
    # realitu tam sa bateria ma vybijat a nabijat podla nastavenia s ty pocita aj plan".
    # Plán pracuje s celou kapacitou [soc_min, soc_max] (typicky 5%-100%).
    # Reserve ostáva aktívna IBA v core/soc_use_audit.py pre RT/VDT/auto_control audit
    # (eff_min=soc_min+reserve, eff_max=soc_max-reserve), kde slúži ako bezpečnostný
    # buffer pred preplnením/podvybitím cez RT zásahy.
    _soc_reserve_pct = max(0.0, min(50.0, float(soc_reserve_pct or 0.0)))
    socmin = batt_kwh * float(soc_min_pct) / 100.0
    socmax = batt_kwh * float(soc_max_pct) / 100.0
    soc0 = batt_kwh * float(soc_init_pct) / 100.0
    if terminal_soc_pct is not None:
        term = batt_kwh * float(terminal_soc_pct) / 100.0
    else:
        term = soc0
    # Auto-adjust SOC bound ak soc_init mimo [soc_min, soc_max] (krajný fail-safe)
    if soc0 < socmin:
        socmin = soc0
    if soc0 > socmax:
        socmax = soc0
    print(f"[optimize_joint_day] batt={batt_kw:.0f}kW/{batt_kwh:.0f}kWh, "
          f"soc_init={soc_init_pct:.1f}%, soc_min={soc_min_pct:.1f}%, "
          f"soc_max={soc_max_pct:.1f}%, term={terminal_soc_pct}, "
          f"socmin_kwh={socmin:.0f}, term_kwh={term:.0f} "
          f"[reserve {_soc_reserve_pct:.0f}% IBA pre audit, NIE pre plán]")

    # Grid limits — Bug #661: rt_grid_reserve_pct headroom pre RT (analógia k soc_reserve_pct).
    # D-1 LP nominuje max (1 - reserve/100) × grid_kw → RT engine má voľný priestor
    # na korekciu odchýlky bez fyzického prekročenia siete.
    _rt_grid_reserve = max(0.0, min(50.0, float(rt_grid_reserve_pct or 0.0)))
    _g_im_raw = grid_kw_import if grid_kw_import is not None else 1e6
    _g_ex_raw = grid_kw_export if grid_kw_export is not None else 1e6
    g_im = _g_im_raw * (1.0 - _rt_grid_reserve / 100.0)
    g_ex = _g_ex_raw * (1.0 - _rt_grid_reserve / 100.0)
    if _rt_grid_reserve > 0:
        print(f"[joint_lp Bug #661] rt_grid_reserve={_rt_grid_reserve:.1f}% → "
              f"eff_im={g_im:.0f} (raw {_g_im_raw:.0f}), "
              f"eff_ex={g_ex:.0f} (raw {_g_ex_raw:.0f})")
    g_im_kwh = g_im * dt
    g_ex_kwh = g_ex * dt
    batt_kwh_per_slot = batt_kw * dt

    # Premenné per slot — rozšírený model so sub-streams pre fyzicky správny
    # toggle behavior (trade_ftv=False naozaj zakáže FTV export, atď.).
    #
    # Aggregate streams (cieľ obchodu — DAM vs VDT):
    #   CH, DI                         — batéria total charge/discharge
    #   EX_DAM, IM_DAM                 — grid export/import cez DAM
    #   EX_VDT, IM_VDT                 — grid export/import cez VDT (intraday)
    #   CU                             — orezanie FTV
    #
    # Sub-streams (ZDROJ exportu / CIEĽ importu — toggle-able):
    #   PV_LOAD                        — PV → load (intern, free)
    #   PV_BATT                        — PV → batt (intern, free)
    #   EX_FTV                         — PV → grid (gated trade_ftv)
    #   DI_LOAD                        — batt → load (intern, free)
    #   EX_BATT                        — batt → grid (gated trade_batt)
    #   IM_LOAD                        — grid → load (gated trade_load)
    #   IM_BATT                        — grid → batt (gated trade_batt)
    #
    # 6 decomposition constraints per slot zabezpečia konzistentnosť:
    #   PV bilancia:   pv[t]      = PV_LOAD + PV_BATT + EX_FTV + CU
    #   Load bilancia: load[t]    = PV_LOAD + DI_LOAD + IM_LOAD
    #   Batt charge:   CH         = PV_BATT + IM_BATT
    #   Batt discharge:DI         = DI_LOAD + EX_BATT
    #   Grid export:   EX_DAM+EX_VDT = EX_FTV + EX_BATT
    #   Grid import:   IM_DAM+IM_VDT = IM_LOAD + IM_BATT
    #
    # Tieto rovnice nahrádzajú pôvodnú aggregate balance — sú s ňou matematicky
    # ekvivalentné, ale exponujú zdroje, takže toggle-y fungujú fyzicky.
    n_groups = 14
    n = n_groups * T
    def idx(g, t):
        return g * T + t

    (CH, DI, EX_DAM, IM_DAM, EX_VDT, IM_VDT, CU,
     PV_LOAD, PV_BATT, EX_FTV, DI_LOAD, EX_BATT, IM_LOAD, IM_BATT) = range(n_groups)

    # Účelová funkcia — minimalizujeme -profit
    # Konvencia rovnaká ako optimizer.optimize_day:
    #   - export: revenue = price × kwh (BEZ grid_fee — predaj za hrubú cenu)
    #   - import: cost = (price + grid_fee) × kwh
    #   - cycle cost: penalty na ch aj di (každá strana 1/2 cyklu)
    c = np.zeros(n)
    # Bug JOINT-MIN-SPREAD (2026-06-11): min_spread bol v joint LP IGNOROVANÝ
    # (classic optimize_day ho má ako trecí náklad cyklu) → joint LP točil aj
    # marginálne cykly pod prahom. Parita: pen = (cycle_cost + min_spread)/2/1000.
    pen = (cycle_cost + float(min_spread_eur or 0.0)) / 2.0 / 1000.0
    for t in range(T):
        # DAM
        c[idx(EX_DAM, t)] = -pr_dam[t] / 1000.0                # predaj: +cena (bez fee)
        c[idx(IM_DAM, t)] = (pr_dam[t] + grid_fee) / 1000.0    # nákup: cena + fee
        # VDT
        c[idx(EX_VDT, t)] = -vdt_sell[t] / 1000.0
        c[idx(IM_VDT, t)] = (vdt_buy[t] + grid_fee) / 1000.0
        # Battery opotrebenie
        c[idx(CH, t)] = pen
        c[idx(DI, t)] = pen
        # Distribučný náklad (TOU sa platí pri importe, oboma smermi DAM/VDT)
        if optimize_distribution:
            c[idx(IM_DAM, t)] += tou[t] / 1000.0
            c[idx(IM_VDT, t)] += tou[t] / 1000.0
        # Curtailment — žiadny náklad (FTV je voľná)

    # Decomposition constraints per slot (6 rovníc namiesto pôvodnej aggregate):
    #   PV bilancia:    pv[t]      = PV_LOAD + PV_BATT + EX_FTV + CU
    #   Load bilancia:  load[t]    = PV_LOAD + DI_LOAD + IM_LOAD
    #   Batt charge:    CH         = PV_BATT + IM_BATT
    #   Batt discharge: DI         = DI_LOAD + EX_BATT
    #   Grid export:    EX_DAM+EX_VDT = EX_FTV + EX_BATT
    #   Grid import:    IM_DAM+IM_VDT = IM_LOAD + IM_BATT
    A_eq = []
    b_eq = []
    for t in range(T):
        # PV: PV_LOAD + PV_BATT + EX_FTV + CU = pv[t]
        row = np.zeros(n)
        row[idx(PV_LOAD, t)] = 1
        row[idx(PV_BATT, t)] = 1
        row[idx(EX_FTV, t)] = 1
        row[idx(CU, t)] = 1
        A_eq.append(row)
        b_eq.append(pv[t])

        # Load: PV_LOAD + DI_LOAD + IM_LOAD = load[t]
        row = np.zeros(n)
        row[idx(PV_LOAD, t)] = 1
        row[idx(DI_LOAD, t)] = 1
        row[idx(IM_LOAD, t)] = 1
        A_eq.append(row)
        b_eq.append(load[t])

        # Batt charge: PV_BATT + IM_BATT − CH = 0
        row = np.zeros(n)
        row[idx(PV_BATT, t)] = 1
        row[idx(IM_BATT, t)] = 1
        row[idx(CH, t)] = -1
        A_eq.append(row)
        b_eq.append(0.0)

        # Batt discharge: DI_LOAD + EX_BATT − DI = 0
        row = np.zeros(n)
        row[idx(DI_LOAD, t)] = 1
        row[idx(EX_BATT, t)] = 1
        row[idx(DI, t)] = -1
        A_eq.append(row)
        b_eq.append(0.0)

        # Grid export: EX_DAM + EX_VDT − EX_FTV − EX_BATT = 0
        row = np.zeros(n)
        row[idx(EX_DAM, t)] = 1
        row[idx(EX_VDT, t)] = 1
        row[idx(EX_FTV, t)] = -1
        row[idx(EX_BATT, t)] = -1
        A_eq.append(row)
        b_eq.append(0.0)

        # Grid import: IM_DAM + IM_VDT − IM_LOAD − IM_BATT = 0
        row = np.zeros(n)
        row[idx(IM_DAM, t)] = 1
        row[idx(IM_VDT, t)] = 1
        row[idx(IM_LOAD, t)] = -1
        row[idx(IM_BATT, t)] = -1
        A_eq.append(row)
        b_eq.append(0.0)

    # SOC constraints — running cumulative
    # SOC[t] = soc0 + Σ_{i=0..t} (eff_c·ch[i] − di[i]/eff_d)
    # soc_min ≤ SOC[t] ≤ soc_max
    A_ub = []
    b_ub = []
    for t in range(T):
        # Upper: SOC[t] ≤ soc_max ⇔ Σ (eff_c·ch − di/eff_d) ≤ soc_max − soc0
        row_u = np.zeros(n)
        for i in range(t + 1):
            row_u[idx(CH, i)] = eff_c
            row_u[idx(DI, i)] = -1.0 / eff_d
        A_ub.append(row_u)
        b_ub.append(socmax - soc0)
        # Lower: −Σ ≤ soc0 − soc_min
        A_ub.append(-row_u)
        b_ub.append(soc0 - socmin)

    # Terminal SOC (rovnaký ako start ak nie je zadaný)
    # Σ (eff_c·ch − di/eff_d) ≥ term − soc0  ⇔  −Σ ≤ soc0 − term
    row_term = np.zeros(n)
    for i in range(T):
        row_term[idx(CH, i)] = -eff_c
        row_term[idx(DI, i)] = 1.0 / eff_d
    A_ub.append(row_term)
    b_ub.append(soc0 - term)

    # Max cycles (sum discharge ≤ max_cycles × batt_kwh)
    if max_cycles is not None and max_cycles > 0:
        row_c = np.zeros(n)
        for i in range(T):
            row_c[idx(DI, i)] = 1.0
        A_ub.append(row_c)
        b_ub.append(max_cycles * batt_kwh)

    # Denné kapy DAM (parita s optimizer.optimize_day):
    #   sum(EX_DAM + EX_VDT) ≤ max_export_kwh_day
    #   sum(IM_DAM + IM_VDT) ≤ max_import_kwh_day
    if max_export_kwh_day is not None and float(max_export_kwh_day) > 0:
        row_ex = np.zeros(n)
        for i in range(T):
            row_ex[idx(EX_DAM, i)] = 1.0
            row_ex[idx(EX_VDT, i)] = 1.0
        A_ub.append(row_ex)
        b_ub.append(float(max_export_kwh_day))
    if max_import_kwh_day is not None and float(max_import_kwh_day) > 0:
        row_im = np.zeros(n)
        for i in range(T):
            row_im[idx(IM_DAM, i)] = 1.0
            row_im[idx(IM_VDT, i)] = 1.0
        A_ub.append(row_im)
        b_ub.append(float(max_import_kwh_day))

    # Bounds — toggle gating je na sub-streams (EX_FTV/EX_BATT/IM_LOAD/IM_BATT).
    # Aplikujeme aj per-slot mults (× šablóna) a ďalšie flagy parity s optimizer.optimize_day:
    #   - allow_curtail=False → CU = 0
    #   - allow_grid_charge=False → IM_BATT = 0 (grid nesmie nabíjať batériu)
    #   - block_planned_discharge=True → DI_LOAD = EX_BATT = 0 (žiadny D-1 výboj)
    #   - block_neg_import=True a price < 0 → IM_DAM[t] = 0 (žiadny import za zápornú cenu)
    bounds = []
    for g in range(n_groups):
        for t in range(T):
            lb = 0.0
            ub = None
            # Per-slot batt cap (× šablóna). mult=0 znamená "slot vypnutý — batt sa nehýbe".
            batt_slot_cap = batt_kwh_per_slot * float(mults[t])
            # Bug LP-VDT-BOUNDS: smerové stropy z uzavretých VDT obchodov (kWh/slot)
            _chg_slot_cap = min(batt_slot_cap, float(_chg_caps[t]) * dt)
            _dis_slot_cap = min(batt_slot_cap, float(_dis_caps[t]) * dt)
            # ---- Aggregate batt streams (gated cez trade_batt + mults + block_planned_discharge) ----
            if g == CH:
                ub = _chg_slot_cap if trade_batt else 0.0
            elif g == DI:
                # block_planned_discharge: D-1 plán nesmie vybíjať (mults=0 alebo flag)
                if not trade_batt or block_planned_discharge:
                    ub = 0.0
                else:
                    ub = _dis_slot_cap
            # ---- Aggregate grid streams (limit iba sieťovou kapacitou + block_neg_import) ----
            elif g == EX_DAM:
                ub = g_ex_kwh
            elif g == IM_DAM:
                # Žiadny import keď je cena záporná (block_neg_import).
                ub = 0.0 if (block_neg_import and pr_dam[t] < 0) else g_im_kwh
            elif g == EX_VDT:
                ub = g_ex_kwh if use_vdt else 0.0
            elif g == IM_VDT:
                if not use_vdt:
                    ub = 0.0
                elif block_neg_import and vdt_buy[t] < 0:
                    ub = 0.0
                else:
                    ub = g_im_kwh
            elif g == CU:
                # max curtailment = aktuálna PV. Ak allow_curtail=False, blokované.
                ub = float(max(pv[t], 0)) if allow_curtail else 0.0
            # ---- Sub-streams (toggle-gated + mults) ----
            elif g == PV_LOAD:
                # PV → load: voľný (intern), max = min(pv, load). Nezávislé od mults (free path).
                ub = float(min(max(pv[t], 0), max(load[t], 0)))
            elif g == PV_BATT:
                # PV → batt: gated cez trade_batt + mults (+ VDT chg cap)
                ub = float(min(max(pv[t], 0), _chg_slot_cap)) if trade_batt else 0.0
            elif g == EX_FTV:
                # Bug TT (2026-06-08): trade_ftv=false nesmie zakazat fyzicky export
                # FTV do siete — to by spravilo z FTV peak-u curtail (= straty
                # tisicov kWh/den voci baseline kde FTV ide pasivne do siete).
                # Semantika trade_ftv je o EVIDENCII v 'obchod' aggregate (DAM
                # nominacia user-a), nie o fyzickom obmedzeni toku. Revenue ide
                # cez EX_DAM (do ktoreho EX_FTV decomposes), takze SVET vie ze
                # passive feed-in je realny — len uzivatel to neuvadza ako svoj
                # aktivny DAM obchod.
                ub = float(max(pv[t], 0))
            elif g == DI_LOAD:
                # batt → load: gated cez trade_batt + mults + block_planned_discharge (+ VDT dis cap)
                if not trade_batt or block_planned_discharge:
                    ub = 0.0
                else:
                    ub = float(min(_dis_slot_cap, max(load[t], 0)))
            elif g == EX_BATT:
                # batt → grid: gated cez trade_batt + mults + block_planned_discharge (+ VDT dis cap)
                if not trade_batt or block_planned_discharge:
                    ub = 0.0
                else:
                    ub = _dis_slot_cap
            elif g == IM_LOAD:
                # grid → load: gated cez trade_load
                ub = float(max(load[t], 0)) if trade_load else 0.0
            elif g == IM_BATT:
                # grid → batt: gated cez trade_batt + mults + allow_grid_charge (+ VDT chg cap)
                if not trade_batt or not allow_grid_charge:
                    ub = 0.0
                else:
                    ub = _chg_slot_cap
            bounds.append((lb, ub))

    # Solver
    A_eq_np = np.array(A_eq) if A_eq else None
    b_eq_np = np.array(b_eq) if b_eq else None
    A_ub_np = np.array(A_ub) if A_ub else None
    b_ub_np = np.array(b_ub) if b_ub else None

    try:
        res = linprog(c, A_ub=A_ub_np, b_ub=b_ub_np,
                       A_eq=A_eq_np, b_eq=b_eq_np,
                       bounds=bounds, method="highs")
    except Exception as e:
        return {"ok": False, "error": f"linprog exception: {e}"}

    if not res.success:
        return {"ok": False, "error": f"LP nemá riešenie: {res.message}"}

    x = res.x
    ch = x[idx(CH, 0):idx(CH, 0)+T]
    di = x[idx(DI, 0):idx(DI, 0)+T]
    ex_dam = x[idx(EX_DAM, 0):idx(EX_DAM, 0)+T]
    im_dam = x[idx(IM_DAM, 0):idx(IM_DAM, 0)+T]
    ex_vdt = x[idx(EX_VDT, 0):idx(EX_VDT, 0)+T]
    im_vdt = x[idx(IM_VDT, 0):idx(IM_VDT, 0)+T]
    cu = x[idx(CU, 0):idx(CU, 0)+T]
    # Sub-streams (intern + per-toggle gated)
    pv_load = x[idx(PV_LOAD, 0):idx(PV_LOAD, 0)+T]
    pv_batt = x[idx(PV_BATT, 0):idx(PV_BATT, 0)+T]
    ex_ftv = x[idx(EX_FTV, 0):idx(EX_FTV, 0)+T]
    di_load = x[idx(DI_LOAD, 0):idx(DI_LOAD, 0)+T]
    ex_batt = x[idx(EX_BATT, 0):idx(EX_BATT, 0)+T]
    im_load = x[idx(IM_LOAD, 0):idx(IM_LOAD, 0)+T]
    im_batt = x[idx(IM_BATT, 0):idx(IM_BATT, 0)+T]

    # Obchod kWh per slot — zahŕňa IBA streams ktoré sú aktuálne zaškrtnuté.
    # Konvencia: + export (predaj), − import (nákup).
    # DIST sa nikdy neobchoduje (je iba v účelovke).
    obchod = np.zeros(T)
    if trade_ftv:
        obchod += ex_ftv
    if trade_batt:
        obchod += ex_batt - im_batt
    if trade_load:
        obchod -= im_load

    # SOC trajektória
    soc_traj = np.zeros(T)
    soc_running = soc0
    for t in range(T):
        soc_running += eff_c * ch[t] - di[t] / eff_d
        soc_traj[t] = soc_running

    # Ekonomický rozklad — grid_fee len na import (rovnaké ako optimize_day)
    dam_rev = float(np.sum(pr_dam * ex_dam) / 1000.0)
    dam_cost = float(np.sum(pr_dam * im_dam) / 1000.0)
    vdt_rev = float(np.sum(vdt_sell * ex_vdt) / 1000.0)
    vdt_cost = float(np.sum(vdt_buy * im_vdt) / 1000.0)
    fee_total = float(np.sum(grid_fee * (im_dam + im_vdt)) / 1000.0)
    cycle_total = float(np.sum(cycle_cost * (ch + di)) / 1000.0 / 2.0)
    tou_total = float(np.sum(tou * (im_dam + im_vdt)) / 1000.0) if optimize_distribution else 0.0
    # Distribučná úspora — koľko by si zaplatil za TOU bez batt arbitráže:
    #   baseline = sum(tou × load)  — všetka spotreba ide z gridu za TOU sadzbu
    #   actual   = tou_total = sum(tou × (im_dam + im_vdt))  — len reálny import
    #   úspora   = baseline - actual
    # Pri load=0 (žiadna spotreba v profile) úspora = 0.
    # Pri trade_load=False sa nezohľadňuje (load nepokrýva grid, ale batt/PV).
    tou_baseline = float(np.sum(tou * np.asarray(load, float)) / 1000.0) if optimize_distribution else 0.0
    tou_savings = tou_baseline - tou_total
    net = dam_rev - dam_cost + vdt_rev - vdt_cost - fee_total - cycle_total - tou_total

    return {
        "ok": True, "error": None,
        "T": T, "dt": dt,
        "ch_kwh": ch.tolist(), "di_kwh": di.tolist(),
        "ex_dam_kwh": ex_dam.tolist(), "im_dam_kwh": im_dam.tolist(),
        "ex_vdt_kwh": ex_vdt.tolist(), "im_vdt_kwh": im_vdt.tolist(),
        "ex_kwh": (ex_dam + ex_vdt).tolist(),
        "im_kwh": (im_dam + im_vdt).tolist(),
        "cu_kwh": cu.tolist(),
        # Sub-streams — per-toggle gated, ukázané v "Obchod" stĺpci
        "pv_load_kwh": pv_load.tolist(),
        "pv_batt_kwh": pv_batt.tolist(),
        "ex_ftv_kwh": ex_ftv.tolist(),
        "di_load_kwh": di_load.tolist(),
        "ex_batt_kwh": ex_batt.tolist(),
        "im_load_kwh": im_load.tolist(),
        "im_batt_kwh": im_batt.tolist(),
        # Obchod per slot (iba toggle-aktívne streams; + export, − import)
        "obchod_kwh": obchod.tolist(),
        "soc_kwh": soc_traj.tolist(),
        "soc_pct": (soc_traj / batt_kwh * 100.0).tolist(),
        "_charge_kw": (ch / dt).tolist(),
        "_discharge_kw": (di / dt).tolist(),
        "_export_kwh": (ex_dam + ex_vdt).tolist(),
        "_import_kwh": (im_dam + im_vdt).tolist(),
        "economics": {
            "dam_revenue_eur": round(dam_rev, 3),
            "dam_cost_eur": round(dam_cost, 3),
            "vdt_revenue_eur": round(vdt_rev, 3),
            "vdt_cost_eur": round(vdt_cost, 3),
            "grid_fee_eur": round(fee_total, 3),
            "cycle_cost_eur": round(cycle_total, 3),
            "tou_cost_eur": round(tou_total, 3),
            "tou_baseline_eur": round(tou_baseline, 3),
            "tou_savings_eur": round(tou_savings, 3),
            "net_profit_eur": round(net, 3),
        },
        "flags": {
            "trade_batt": trade_batt, "trade_ftv": trade_ftv,
            "trade_load": trade_load, "use_vdt": use_vdt,
            "optimize_distribution": optimize_distribution,
        },
    }


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Joint LP smoke test")
    parser.add_argument("--T", type=int, default=24, help="počet slotov (default 24)")
    parser.add_argument("--dt", type=float, default=1.0, help="dĺžka slotu v hodinách")
    parser.add_argument("--batt_kw", type=float, default=200.0)
    parser.add_argument("--batt_kwh", type=float, default=400.0)
    parser.add_argument("--soc_init_pct", type=float, default=50.0)
    parser.add_argument("--flags", type=str,
                         default="all", help="all | no_batt | no_ftv | no_load | no_vdt | "
                                              "no_dist | dist_on")
    args = parser.parse_args()

    T = args.T
    # Synthetic PV: solar peak okolo poludnia
    hours = np.arange(T)
    pv = np.maximum(0, 100 * np.sin(np.pi * (hours - 6) / 12)) * args.dt

    # Synthetic load: konštantná
    load = np.full(T, 30.0 * args.dt)

    # Synthetic DAM ceny: nízke v noci a poludnie, vysoké večer
    dam = 50 + 40 * np.sin(np.pi * (hours - 12) / 24) + 20 * np.sin(np.pi * (hours - 18) / 6)

    # VDT ceny: +/- 5 €/MWh spread
    vdt_buy = dam - 5
    vdt_sell = dam - 8

    # TOU: 100 €/MWh v špičke (8-12, 16-20), 30 €/MWh inak
    tou = np.where(((hours >= 8) & (hours < 12)) | ((hours >= 16) & (hours < 20)),
                    100.0, 30.0)

    flags_map = {
        "all":      dict(trade_batt=True, trade_ftv=True, trade_load=True,
                          use_vdt=True, optimize_distribution=False),
        "dist_on":  dict(trade_batt=True, trade_ftv=True, trade_load=True,
                          use_vdt=True, optimize_distribution=True),
        "no_batt":  dict(trade_batt=False, trade_ftv=True, trade_load=True,
                          use_vdt=True, optimize_distribution=False),
        "no_ftv":   dict(trade_batt=True, trade_ftv=False, trade_load=True,
                          use_vdt=True, optimize_distribution=False),
        "no_load":  dict(trade_batt=True, trade_ftv=True, trade_load=False,
                          use_vdt=True, optimize_distribution=False),
        "no_vdt":   dict(trade_batt=True, trade_ftv=True, trade_load=True,
                          use_vdt=False, optimize_distribution=False),
    }
    flag_set = flags_map.get(args.flags, flags_map["all"])

    print(f"=== Joint LP smoke test (flags: {args.flags}) ===")
    print(f"T={T}, dt={args.dt}h, batt {args.batt_kw} kW / {args.batt_kwh} kWh, "
           f"SOC start {args.soc_init_pct}%")
    print(f"Flags: {flag_set}")

    res = optimize_joint_day(
        pv_kwh=pv, load_kwh=load, dam_price_eur=dam,
        vdt_buy_price=vdt_buy, vdt_sell_price=vdt_sell,
        tou_price_eur=tou,
        batt_kw=args.batt_kw, batt_kwh=args.batt_kwh,
        soc_init_pct=args.soc_init_pct,
        dt=args.dt,
        **flag_set,
    )

    if not res["ok"]:
        print(f"\n❌ LP zlyhal: {res['error']}")
        exit(1)

    print(f"\n✅ OK")
    print(f"Ekonomika: {json.dumps(res['economics'], indent=2, ensure_ascii=False)}")
    # Sumár objemov
    print(f"\nSumár objemov (kWh):")
    print(f"  charge:   {sum(res['ch_kwh']):.1f}")
    print(f"  discharge:{sum(res['di_kwh']):.1f}")
    print(f"  DAM ex:   {sum(res['ex_dam_kwh']):.1f}")
    print(f"  DAM im:   {sum(res['im_dam_kwh']):.1f}")
    print(f"  VDT ex:   {sum(res['ex_vdt_kwh']):.1f}")
    print(f"  VDT im:   {sum(res['im_vdt_kwh']):.1f}")
    print(f"  curtail:  {sum(res['cu_kwh']):.1f}")
    print(f"  load:     {sum(load):.1f}")
    print(f"  PV:       {sum(pv):.1f}")
    print(f"\nSOC trajektória (každá 4. hodina):")
    for i in range(0, T, max(1, T // 6)):
        print(f"  t={i:2d}: SOC = {res['soc_pct'][i]:.1f}% ({res['soc_kwh'][i]:.0f} kWh)")
