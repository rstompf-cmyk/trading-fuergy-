# -*- coding: utf-8 -*-
"""
optimizer.py – optimálny rozvrh batérie + obchodná pozícia na deň (plán D-1).
Lineárny program (scipy.optimize.linprog, metóda HiGHS). Hodinové kroky.

optimize_day(pv_kwh, price_eur, ...) -> (schedule_df, summary)
  pv_kwh    : list/np.array výroby FTV [kWh] po hodinách
  price_eur : list/np.array predpovedaného ISOT [EUR/MWh] po hodinách
"""
from __future__ import annotations
import numpy as np, pandas as pd
from scipy.optimize import linprog


# Rozpočet diagnostiky na proces — každé volanie robí ~6 extra LP solveov. Pri veľkom
# batchi s veľa infeasible dňami (napr. TBB 15-min) by to inak násobilo čas. Po vyčerpaní
# vrátime len krátku hlášku (bez bisektu). Reset nie je nutný (stačí pár vzoriek na diagnózu).
_DIAG_BUDGET = [25]


def _diagnose_infeasible(c, A_ub, b_ub, A_eq, b_eq, bounds, T, pv, socmin, socmax, batt_kwh):
    """Beží LEN keď je LP infeasible — zistí, ktorý constraint to spôsobuje (jednotlivé
    aj kumulatívne uvoľnenie). Vráti string do chybovej hlášky. Bezpečné — golden cesta
    (úspešný LP) sem nikdy nepríde."""
    if _DIAG_BUDGET[0] <= 0:
        return " | DIAG: (rozpočet diagnostiky vyčerpaný — viď skoršie DIAG riadky)"
    _DIAG_BUDGET[0] -= 1
    try:
        from scipy.optimize import linprog as _lp
        EX, IM, CU, SOC = 2*T, 3*T, 4*T, 5*T
        Aeq = np.array(A_eq); beq = np.array(b_eq)

        def _solve(b):
            rr = _lp(c, A_ub=A_ub, b_ub=b_ub, A_eq=Aeq, b_eq=beq, bounds=b, method="highs")
            return bool(rr.success)

        def m_term(b):  b[SOC + T - 1] = (socmin, socmax)
        def m_curt(b):
            for t in range(T): b[CU + t] = (0.0, max(float(pv[t]), 0.0))
        def m_exp(b):
            for t in range(T): b[EX + t] = (0.0, 1e9)
        def m_imp(b):
            for t in range(T): b[IM + t] = (0.0, 1e9)
        def m_soc(b):
            for t in range(T): b[SOC + t] = (0.0, float(batt_kwh))

        relax = [("terminal_soc", m_term), ("allow_curtail", m_curt),
                 ("grid_export", m_exp), ("grid_import/block_neg", m_imp), ("soc_band", m_soc)]
        singles = []
        for nm, fn in relax:
            b = list(bounds); fn(b)
            if _solve(b):
                singles.append(nm)
        # kumulatívne (over že aspoň všetko spolu rieši — inak je problém v rovnostiach/bilancii)
        b_all = list(bounds)
        for _, fn in relax: fn(b_all)
        all_ok = _solve(b_all)
        if singles:
            return f" | DIAG: feasible ak uvoľním JEDEN z: {singles}"
        if all_ok:
            return " | DIAG: feasible len pri uvoľnení VIACERÝCH naraz (kombinácia limitov: terminal/curtail/grid/SOC)"
        return " | DIAG: infeasible aj po uvoľnení všetkých bound-ov → problém v BILANCII uzla (load−pv vs limity siete/curtail)"
    except Exception as _e:
        return f" | DIAG zlyhal: {_e}"


def optimize_day(pv_kwh, price_eur, *, batt_kw=100.0, batt_kwh=200.0,
                 eff_c=0.95, eff_d=0.95, soc_min_pct=5, soc_max_pct=95,
                 soc_init_pct=50, grid_kw=100.0, grid_fee=22.0, cycle_cost=2.0,
                 allow_grid_charge=True, terminal_soc_pct=None, dt=1.0,
                 allow_curtail=True,
                 min_spread_eur=0.0, min_trade_mwh=0.0, block_neg_import=False,
                 max_cycles=None, batt_kw_override=None,
                 block_planned_discharge=False,
                 settle_price=None,
                 grid_kw_export=None, grid_kw_import=None,
                 load_kwh=None,
                 max_export_kwh_day=None, max_import_kwh_day=None,
                 soc_reserve_pct=0.0,
                 rt_grid_reserve_pct=0.0,
                 batt_dis_cap_kw=None, batt_chg_cap_kw=None):
    """`load_kwh` = spotreba zákazníka [kWh/perióda] (net-meter setup): pv + di + im − ex − ch − cu = load.
    Ak má profil naimportovanú spotrebu, predáva sa najprv self-consumption (zadarmo),
    zvyšok ide do siete/batérie. Pri load > pv treba import alebo battery discharge.

    Bug #624 (Krok E): `soc_reserve_pct` = safety buffer nad soc_min_pct. LP nikdy
    nesmie nominovať plán ktorý by spotreboval SOC pod (soc_min_pct + soc_reserve_pct).
    Slúži na pokrytie nepredikovaných strát počas dňa (VDT trade, RT zásahy, eff_d
    nepresnosti). Default 0 = back-compat. Typicky 5-10 % je rozumné."""
    # asymetrické limity siete: default = grid_kw (backward compat)
    grid_kw_export = float(grid_kw_export) if grid_kw_export is not None else float(grid_kw)
    grid_kw_import = float(grid_kw_import) if grid_kw_import is not None else float(grid_kw)
    # Bug #661: rt_grid_reserve_pct = headroom v grid kapacite pre RT zásahy.
    # D-1 LP nominuje na max (1 - reserve/100) × grid_kw, takže RT engine môže
    # zvýšiť/znížiť import/export bez prekročenia siete (analógia k soc_reserve_pct).
    _rt_grid_reserve = max(0.0, min(50.0, float(rt_grid_reserve_pct or 0.0)))
    _gki_eff = grid_kw_import * (1.0 - _rt_grid_reserve / 100.0)
    _gke_eff = grid_kw_export * (1.0 - _rt_grid_reserve / 100.0)
    if _rt_grid_reserve > 0:
        print(f"[optimizer Bug #661] rt_grid_reserve={_rt_grid_reserve:.1f}% → "
              f"eff_im={_gki_eff:.0f} (raw {grid_kw_import:.0f}), "
              f"eff_ex={_gke_eff:.0f} (raw {grid_kw_export:.0f})")
    grid_kw_export = _gke_eff
    grid_kw_import = _gki_eff
    pv = np.asarray(pv_kwh, float)
    pr = np.asarray(price_eur, float)
    T = len(pv)
    load = np.zeros(T, dtype=float) if load_kwh is None else np.asarray(load_kwh, float).reshape(-1)[:T]
    if load.size < T:
        load = np.concatenate([load, np.zeros(T - load.size)])
    _has_load = bool(load.sum() > 1e-6)
    # Bug SOC-RESERVE-AUDIT-ONLY (2026-06-10): reserve sa NEAPLIKUJE na LP plánovanie.
    # Plán pracuje s celou kapacitou [soc_min, soc_max] (typicky 5%-100%).
    # Reserve ostáva aktívna IBA v core/soc_use_audit.py pre RT/VDT/auto_control audit.
    _soc_reserve_pct = max(0.0, min(50.0, float(soc_reserve_pct or 0.0)))
    socmin, socmax = batt_kwh * float(soc_min_pct) / 100, batt_kwh * float(soc_max_pct) / 100
    soc0 = batt_kwh * float(soc_init_pct) / 100
    if terminal_soc_pct is not None:
        term = batt_kwh * float(terminal_soc_pct) / 100
    else:
        term = soc0
    print(f"[optimize_day] batt={batt_kw:.0f}kW/{batt_kwh:.0f}kWh, "
          f"eff_c={eff_c:.3f}/eff_d={eff_d:.3f}, "
          f"soc_init={soc_init_pct:.1f}%, soc_min={soc_min_pct:.1f}%, "
          f"soc_max={soc_max_pct:.1f}%, term={terminal_soc_pct}, "
          f"grid_im={grid_kw_import:.0f}/ex={grid_kw_export:.0f}, "
          f"max_dam_im={max_import_kwh_day}/ex={max_export_kwh_day}, "
          f"dt={dt:.2f}h, T={T} "
          f"[reserve {_soc_reserve_pct:.0f}% IBA pre audit, NIE pre plán]")

    # poradie premenných: ch, di, ex, im, cu, soc  (každá dĺžky T)
    CH, DI, EX, IM, CU, SOC = (g*T for g in range(6))
    n = 6*T
    def idx(g, t): return g*T + t

    # cieľ: max tržba -> min (-tržba).  €/MWh * kWh/1000 = €
    c = np.zeros(n)
    pen = (cycle_cost + min_spread_eur) / 2 / 1000.0   # trecí náklad na každú stranu cyklu
    for t in range(T):
        c[idx(2, t)] = -pr[t]/1000.0                 # export: +cena
        c[idx(3, t)] = (pr[t]+grid_fee)/1000.0       # import: -(cena+poplatok)
        c[idx(0, t)] = pen                           # nábeh: bráni cyklu pod prahom min_spread
        c[idx(1, t)] = pen                           # výboj: rovnako

    # rovnosti: bilancia uzla + dynamika SOC
    # NET-METER: pv + di + im − ex − ch − cu = load  →  b_eq = load − pv
    A_eq, b_eq = [], []
    for t in range(T):
        row = np.zeros(n)
        row[idx(1, t)] = 1; row[idx(3, t)] = 1
        row[idx(2, t)] = -1; row[idx(0, t)] = -1; row[idx(4, t)] = -1
        A_eq.append(row); b_eq.append(load[t] - pv[t])
    for t in range(T):                               # soc[t]-soc[t-1]-eff_c*ch+di/eff_d = 0
        row = np.zeros(n)
        row[idx(5, t)] = 1
        row[idx(0, t)] = -eff_c
        row[idx(1, t)] = 1.0/eff_d
        if t > 0:
            row[idx(5, t-1)] = -1; A_eq.append(row); b_eq.append(0.0)
        else:
            A_eq.append(row); b_eq.append(soc0)

    # medze premenných (LP beží BEZ vedomia o batt_kw_override — × sa aplikuje až POST-procesom)
    # Bug LP-VDT-BOUNDS (2026-06-11): batt_dis_cap_kw / batt_chg_cap_kw = per-slot
    # smerové stropy (kW, len T) — typicky z UŽ uzavretých VDT obchodov dňa:
    # |dam + vdt| ≤ batt_kw ⇒ dis_cap = clip(batt_kw − vdt, 0, batt_kw),
    # chg_cap = clip(batt_kw + vdt, 0, batt_kw). None = plný batt_kw (back-compat).
    _dis_caps = (np.clip(np.asarray(batt_dis_cap_kw, float).reshape(-1)[:T], 0.0, batt_kw)
                 if batt_dis_cap_kw is not None else np.full(T, batt_kw))
    _chg_caps = (np.clip(np.asarray(batt_chg_cap_kw, float).reshape(-1)[:T], 0.0, batt_kw)
                 if batt_chg_cap_kw is not None else np.full(T, batt_kw))
    if _dis_caps.size < T:
        _dis_caps = np.concatenate([_dis_caps, np.full(T - _dis_caps.size, batt_kw)])
    if _chg_caps.size < T:
        _chg_caps = np.concatenate([_chg_caps, np.full(T - _chg_caps.size, batt_kw)])
    bounds = []
    for t in range(T): bounds.append((0, float(_chg_caps[t])*dt))   # ch
    # ak block_planned_discharge=True, D-1 plán nikdy nevybíja (di[t]=0 vo všetkých slotoch).
    # SOC sa môže nabíjať z PV/siete, ale vybíjanie ide cez RT odchýlku až za behom.
    for t in range(T):
        _di_cap = 0.0 if block_planned_discharge else float(_dis_caps[t])*dt
        bounds.append((0, _di_cap))                                  # di
    for t in range(T): bounds.append((0, grid_kw_export*dt))   # ex (limit dodávky do siete)
    _im_open = []          # ub_im BEZ block_neg (pre feasibility-fallback nižšie)
    _blocked_any = False   # bol aspoň jeden slot zúžený kvôli block_neg_import?
    for t in range(T):
        _load_t = float(load[t]) if _has_load else 0.0
        # SIEŤ VŽDY KRYJE SPOTREBU: per-slot import strop = max(grid_limit, load[t]).
        # Limit obmedzuje NABÍJANIE/obchod, NIE povinné pokrytie spotreby (parita s joint_lp,
        # bug GRID-LIMIT-LOAD-INFEASIBLE). Bug 15-MIN-LOAD-PEAK (2026-06-18): pri 60-min sa
        # load posiela ako HODINOVÝ PRIEMER (zhladený), pri 15-min ako REÁLNY load za slot
        # (so špičkami). Starý strop grid_kw_import*dt bez load-reliefu → keď 15-min load špička
        # > grid_import*dt, rovnosť pokrytia load infeasible KAŽDÝ deň (pri 60-min priemer ukryl
        # špičku → preto 60-min prešiel, 15-min padal).
        _g_im_kwh = grid_kw_import * dt
        if allow_grid_charge:
            ub_open = max(_g_im_kwh, _load_t)      # grid limit pre nabíjanie/obchod, load vždy pokrytý
        elif _has_load:
            ub_open = _load_t                      # iba pokrytie load (žiadne nabíjanie zo siete)
        else:
            ub_open = 0.0
        _im_open.append(ub_open)
        ub_im = ub_open
        if block_neg_import and pr[t] < 0:         # nenakupovať pri zápornej cene (load výnimka)
            ub_im = _load_t                        # load treba pokryť aj pri zápornej cene
            if ub_open > _load_t + 1e-9:
                _blocked_any = True
        bounds.append((0, ub_im))                                # im
    for t in range(T): bounds.append((0, (max(pv[t], 0) if allow_curtail else 0.0)))  # cu (orezanie FTV)
    # Pri block_planned_discharge LP nemôže vybíjať → ak terminal_soc > soc_init, LP nemá ako
    # dosiahnuť terminal (nemôže klesnúť). Preto terminal floor relaxujeme na socmin (SOC smie
    # skončiť kdekoľvek v [socmin, socmax]; obvykle skončí plný — to je očakávané pre stratégiu
    # "nabíj v D-1, vybitie nechaj na RT").
    _term_floor = socmin if block_planned_discharge else term
    for t in range(T):                                       # soc
        lo = _term_floor if t == T-1 else socmin
        bounds.append((lo, socmax))

    # voliteľné inequality stropy: zbieram riadky do listu
    A_ub_rows, b_ub_vals = [], []
    # strop cyklov plánu (voliteľný): sum(ch+di) <= 2*max_cycles*batt_kwh  (cyklus = prietok/2/kapacita)
    if max_cycles is not None:
        row = np.zeros(n)
        for t in range(T):
            row[idx(0, t)] = 1.0; row[idx(1, t)] = 1.0
        A_ub_rows.append(row); b_ub_vals.append(2.0*float(max_cycles)*batt_kwh)
    # POZOR: max_export_kwh_day / max_import_kwh_day NEUKLADÁME ako LP constraint —
    # to by spôsobilo, že LP vyberie len top-profit sloty a obchody by sa skoncentrovali
    # do mála slotov. Namiesto toho po LP urobíme PROPORCIONÁLNE ŠKÁLOVANIE celého
    # plánu, ktoré zachová rozloženie cez deň a iba zníži objemy.
    A_ub = np.array(A_ub_rows) if A_ub_rows else None
    b_ub = np.array(b_ub_vals) if b_ub_vals else None
    r = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=np.array(A_eq), b_eq=np.array(b_eq),
                bounds=bounds, method="highs")
    if not r.success and block_neg_import and _blocked_any:
        # FEASIBILITY-FALLBACK (BUG 15-MIN-BLOCK-NEG, 2026-06-18, profil Simulacia_Coop):
        # block_neg_import je EKONOMICKÁ preferencia („nenakupuj pri zápornej cene"), NIE
        # fyzická nutnosť — import pri zápornej cene je vždy možný (a vlastne ziskový).
        # Ak blokovanie spôsobilo infeasibilitu (typicky pri 15-min: viac záporných slotov +
        # batéria sa nemá ako nabiť na terminál), povolíme neg-price import a skúsime znova.
        for t in range(T):
            bounds[IM + t] = (0, _im_open[t])
        r = linprog(c, A_ub=A_ub, b_ub=b_ub, A_eq=np.array(A_eq), b_eq=np.array(b_eq),
                    bounds=bounds, method="highs")
        if r.success:
            print("[optimize_day] block_neg_import uvoľnené (inak infeasible) — "
                  "import pri zápornej cene povolený pre tento deň")
    if not r.success:
        _diag = _diagnose_infeasible(c, A_ub, b_ub, A_eq, b_eq, bounds, T, pv, socmin, socmax, batt_kwh)
        raise RuntimeError("LP sa nevyriešil: " + r.message + _diag)
    x = r.x
    x = np.where(np.abs(x) < 1e-6, 0.0, x)           # očisti numerický šum (-0.0)
    g = lambda k: x[k*T:(k+1)*T]
    ch, di, ex, im, cu, soc = g(0), g(1), g(2), g(3), g(4), g(5)

    # ── Proporcionálne škálovanie ak je nastavený strop ──────────────────
    # Cieľ: zachovať TVAR plánu (kedy obchodovať), len znížiť amplitúdy aby celkový
    # ex/im bol pod stropom. Riešenie: rerun LP s per-slot upper boundami
    # proportional k pôvodnému plánu. LP doplní ex/im/ch/di s rešpektom k bilancii.
    _scale_ex = 1.0
    _scale_im = 1.0
    if max_export_kwh_day is not None and float(max_export_kwh_day) > 0:
        _tot_ex = float(np.sum(ex))
        if _tot_ex > float(max_export_kwh_day):
            _scale_ex = float(max_export_kwh_day) / _tot_ex
    if max_import_kwh_day is not None and float(max_import_kwh_day) > 0:
        _tot_im = float(np.sum(im))
        if _tot_im > float(max_import_kwh_day):
            _scale_im = float(max_import_kwh_day) / _tot_im
    cap_applied = False
    cap_warning = None
    if _scale_ex < 1.0 - 1e-9 or _scale_im < 1.0 - 1e-9:
        # Vyrob nové bounds: ex[t] ≤ ex_orig[t] × scale_ex, im[t] ≤ im_orig[t] × scale_im
        # Tým udržíme RELATÍVNY tvar ex/im cez deň, len obmedzíme objem.
        bounds_scaled = list(bounds)   # copy
        for t in range(T):
            ex_cap_t = max(0.0, float(ex[t]) * _scale_ex)
            im_cap_t = max(0.0, float(im[t]) * _scale_im)
            bounds_scaled[idx(2, t)] = (0.0, ex_cap_t)
            bounds_scaled[idx(3, t)] = (0.0, im_cap_t)
        # 1. pokus: pôvodné cu bounds (rešpektuje allow_curtail)
        r2 = None
        try:
            r2 = linprog(c, A_ub=A_ub, b_ub=b_ub,
                         A_eq=np.array(A_eq), b_eq=np.array(b_eq),
                         bounds=bounds_scaled, method="highs")
        except Exception:
            r2 = None
        if r2 is not None and r2.success:
            x2 = np.where(np.abs(r2.x) < 1e-6, 0.0, r2.x)
            ch, di, ex, im, cu, soc = (x2[k*T:(k+1)*T] for k in range(6))
            cap_applied = True
        else:
            # 2. pokus (auto-degrade): pri tightenom ex_cap môže byť LP infeasible
            # ak je `allow_curtail=False` + veľa PV (PV nemá kam ísť). Skús ešte raz
            # s **uvoľneným curtail-om** — len pre tento rerun, profilov nastavenie
            # sa nemení. Pridá sa warning do summary aby user vedel.
            if not allow_curtail:
                bounds_relaxed = list(bounds_scaled)
                for t in range(T):
                    bounds_relaxed[idx(4, t)] = (0.0, max(float(pv[t]), 0.0))
                try:
                    r3 = linprog(c, A_ub=A_ub, b_ub=b_ub,
                                 A_eq=np.array(A_eq), b_eq=np.array(b_eq),
                                 bounds=bounds_relaxed, method="highs")
                    if r3.success:
                        x3 = np.where(np.abs(r3.x) < 1e-6, 0.0, r3.x)
                        ch, di, ex, im, cu, soc = (x3[k*T:(k+1)*T] for k in range(6))
                        cap_applied = True
                        cap_warning = ("Cap rerun vyžadoval auto-uvoľnenie curtail-u "
                                       "(allow_curtail=False + veľa PV). PV nadbytok "
                                       "sa orezal aby export ostal ≤ cap. "
                                       "Riešenie: zapni allow_curtail v profile.")
                except Exception:
                    pass
            if not cap_applied:
                cap_warning = ("Cap rerun zlyhal (LP infeasible). Plán ostal "
                               "neorezaný — Max DAM cap sa NEAPLIKOVAL. "
                               "Skús zapnúť allow_curtail v profile, alebo "
                               "zvýš cap.")

    grid = (ex - im)
    order = np.round(grid/1000.0, 3)                  # obchodná pozícia [MWh]
    if min_trade_mwh > 0:
        order = np.where(np.abs(order) < min_trade_mwh, 0.0, order)
    # cena pre ZÚČTOVANIE (settle_price): ak nezadané, použije sa rozhodovacia (pr).
    # Tým biased decision-making nezdvíha papierový "ZISK_EUR" — len cieľ LP.
    spr = pr if settle_price is None else np.asarray(settle_price, float)
    # POZN: LP premenné ch, di, ex, im, cu sú v kWh/perióda (bounded by batt_kw*dt).
    # Pre VÝSTUP batt_kw, _charge_kw, _discharge_kw delíme dt aby boli v skutočných kW.
    # Pre dt=1.0 (hodinový plán) je delenie no-op (kWh/h = kW).
    # Pre dt=0.25 (15-min) sa hodnoty znásobia 4× a zodpovedajú reálnemu výkonu batérie.
    sch = pd.DataFrame({
        "hour": range(T), "pv_kwh": pv.round(1), "price_eur": spr.round(1),
        "batt_kw": ((di - ch) / dt).round(1),     # kW: + = vybíjanie, − = nabíjanie
        "grid_kwh": grid.round(1),                # + = predaj do siete, − = nákup
        "order_mwh": order,                       # obchod na DT (po prahu min_trade)
        "curtail_kwh": cu.round(1),
        "soc_pct": (soc/batt_kwh*100).round(0),
        "_charge_kw": (ch / dt).round(1), "_discharge_kw": (di / dt).round(1),
        "_export_kwh": ex.round(1), "_import_kwh": im.round(1), "soc_kwh": soc.round(1),
    })
    rev_ex = float((spr*ex).sum()/1000)
    cost_im = float(((spr+grid_fee)*im).sum()/1000)
    cyc = float((cycle_cost*di).sum()/1000)
    net = rev_ex - cost_im - cyc
    base = float((np.where(spr > 0, spr, 0)*pv).sum()/1000)   # bez batérie: predaj len keď cena>0
    summary = {
        "trzba_export_EUR": round(rev_ex, 2), "naklad_import_EUR": round(cost_im, 2),
        "naklad_cyklus_EUR": round(cyc, 2), "ZISK_EUR": round(net, 2),
        "bez_baterie_EUR": round(base, 2), "prinos_baterie_EUR": round(net-base, 2),
        "nabite_kWh": round(float(ch.sum()), 1), "vybite_kWh": round(float(di.sum()), 1),
        "import_kWh": round(float(im.sum()), 1), "orezane_kWh": round(float(cu.sum()), 1),
        "block_planned_discharge": bool(block_planned_discharge),
        # Max DAM cap diagnostika — pre debug v UI
        "cap_export_kWh": (float(max_export_kwh_day)
                            if max_export_kwh_day and float(max_export_kwh_day) > 0 else None),
        "cap_import_kWh": (float(max_import_kwh_day)
                            if max_import_kwh_day and float(max_import_kwh_day) > 0 else None),
        "cap_applied": bool(cap_applied) if (
            max_export_kwh_day or max_import_kwh_day) else None,
        "cap_warning": cap_warning,
    }
    # Bug #641: log cap warning ak existuje (predtým len v summary, neviditeľné)
    if cap_warning:
        print(f"[optimize_day cap_warning] {cap_warning}")
    # Diagnostika peak batt + SOC trajektórie (rýchla detekcia infeasibility)
    try:
        _peak_di = float(np.max(np.abs(sch.get("batt_kw", [0])))) if len(sch) else 0.0
        _peak_ex = float(np.max(np.abs(sch.get("grid_kwh", [0])))) if len(sch) else 0.0
        _soc_min_seen = float(np.min(sch["soc_pct"])) if "soc_pct" in sch and len(sch) else 0.0
        _soc_max_seen = float(np.max(sch["soc_pct"])) if "soc_pct" in sch and len(sch) else 0.0
        print(f"[optimize_day result] peak_batt={_peak_di:.0f}kW, peak_grid={_peak_ex:.0f}kWh/period, "
              f"SOC range {_soc_min_seen:.0f}–{_soc_max_seen:.0f}% (limit {soc_min_pct:.0f}–{soc_max_pct:.0f}%), "
              f"export={summary.get('trzba_export_EUR',0):.2f}€, import={summary.get('naklad_import_EUR',0):.2f}€")
        # Sanity: ak peak_batt > batt_kw → LP nominoval cez fyzický limit (bug)
        if _peak_di > batt_kw * 1.01:
            print(f"[optimize_day WARN] peak_batt {_peak_di:.0f} kW > batt_kw {batt_kw:.0f} kW "
                  f"(LP nominoval cez fyzický limit!)")
        # Sanity: ak SOC klesol pod soc_min → LP porušil fyzický limit
        if _soc_min_seen < float(soc_min_pct) - 0.5:
            print(f"[optimize_day WARN] SOC min {_soc_min_seen:.1f}% < soc_min {float(soc_min_pct):.1f}% "
                  f"(LP porušil fyzický limit!)")
    except Exception:
        pass
    # ── voliteľný post-process: ručné násobitele nad batt_kw (D-1 plán) ─────
    if batt_kw_override is not None:
        sch, summary = _apply_batt_override(
            sch, summary, np.asarray(batt_kw_override, float), pv, spr,
            batt_kw=batt_kw, batt_kwh=batt_kwh, eff_c=eff_c, eff_d=eff_d,
            soc_min_pct=soc_min_pct, soc_max_pct=soc_max_pct, soc_init_pct=soc_init_pct,
            terminal_soc_pct=terminal_soc_pct,
            grid_kw=grid_kw, grid_kw_export=grid_kw_export, grid_kw_import=grid_kw_import,
            grid_fee=grid_fee, cycle_cost=cycle_cost,
            allow_grid_charge=allow_grid_charge, allow_curtail=allow_curtail,
            min_trade_mwh=min_trade_mwh, dt=dt, load_kwh=load,
        )
    # do summary ulož load info (pre audit)
    if _has_load:
        summary["load_total_kWh"] = round(float(load.sum()), 1)
    # do schedule pridaj stĺpec load_kwh (informačne, aby si user vedel skontrolovať)
    sch["load_kwh"] = np.round(load, 2)
    return sch, summary


def _apply_batt_override(sch, summary, mult, pv, pr, *,
                          batt_kw, batt_kwh, eff_c, eff_d,
                          soc_min_pct, soc_max_pct, soc_init_pct, terminal_soc_pct,
                          grid_kw, grid_fee, cycle_cost,
                          allow_grid_charge, allow_curtail, min_trade_mwh, dt,
                          grid_kw_export=None, grid_kw_import=None,
                          load_kwh=None):
    # asymetrické limity siete: default = grid_kw (backward compat)
    grid_kw_export = float(grid_kw_export) if grid_kw_export is not None else float(grid_kw)
    grid_kw_import = float(grid_kw_import) if grid_kw_import is not None else float(grid_kw)
    # load: ak None → zeros (= no load, legacy behavior)
    load = np.zeros(len(pv), dtype=float) if load_kwh is None else np.asarray(load_kwh, float).reshape(-1)[:len(pv)]
    if load.size < len(pv):
        load = np.concatenate([load, np.zeros(len(pv) - load.size)])
    """Aplikuje násobiteľ na batt_kw stĺpec rozvrhu (post-process za LP).
    `mult` má dĺžku T (rovnakú ako pv); 1.0 = nič nemeniť, 0 = blokovať slot, záporné = obrátiť.
    Bilancia, SOC, grid/curtail sa prepočítajú konzistentne; SOC mimo limitov sa orezáva s warningom.
    Vracia (sch_new, summary_new) s pridanými kľúčmi `override_*` v summary."""
    T = len(pv)
    if mult.shape != (T,):
        raise ValueError(f"batt_kw_override má dĺžku {mult.shape}, očakávam ({T},)")
    if np.all(np.isclose(mult, 1.0)):
        # nič aktívne → vrátime nezmenené
        summary = {**summary, "override_active": 0, "override_warnings": []}
        return sch, summary
    # sch["batt_kw"] je teraz v skutočných kW (po fixe optimizer-output). Pre SOC sweep
    # potrebujeme kWh/perióda → násobíme dt.
    base_kw = sch["batt_kw"].values.astype(float)            # kW
    new_kw = np.clip(mult * base_kw, -batt_kw, batt_kw)      # kW, clip na nominálny výkon
    base = base_kw * dt                                       # kWh/perióda (pre SOC matematiku nižšie)
    new = new_kw * dt                                         # kWh/perióda
    # forward SOC sweep s tvrdým orezaním na SOC limity (di/eff_d, eff_c*ch)
    smin = batt_kwh * soc_min_pct / 100.0
    smax = batt_kwh * soc_max_pct / 100.0
    soc = batt_kwh * soc_init_pct / 100.0
    socs = np.zeros(T)
    warnings = []
    for t in range(T):
        if new[t] >= 0:                                      # vybíjanie kWh/perióda
            cap = max(0.0, (soc - smin)) * eff_d
            if new[t] > cap + 1e-9:
                warnings.append({"t": int(t), "kind": "soc_min",
                                  "msg": f"slot {t}: SOC by išiel pod min — di orezané z {new[t]:.2f} na {cap:.2f} kWh"})
                new[t] = cap
            soc -= new[t] / eff_d
        else:                                                # nabíjanie kWh/perióda (záporné)
            ch_kwh = -new[t]
            cap = max(0.0, (smax - soc)) / eff_c
            if ch_kwh > cap + 1e-9:
                warnings.append({"t": int(t), "kind": "soc_max",
                                  "msg": f"slot {t}: SOC by išiel nad max — ch orezané z {ch_kwh:.2f} na {cap:.2f} kWh"})
                ch_kwh = cap
                new[t] = -ch_kwh
            soc += ch_kwh * eff_c
        socs[t] = soc
    if terminal_soc_pct is not None:
        term_kwh = batt_kwh * float(terminal_soc_pct) / 100.0
        if socs[-1] + 1e-6 < term_kwh:
            warnings.append({"t": T-1, "kind": "terminal_soc",
                              "msg": f"finálne SOC {socs[-1]/batt_kwh*100:.1f} % je pod cielom {terminal_soc_pct:.0f} %"})
    di = np.maximum(new, 0.0)
    ch = -np.minimum(new, 0.0)
    # bilancia: pv + di + im − ex − ch − cu = load  →  ex − im = pv − cu + di − ch − load
    # × škáluje IBA batériu (di, ch). PV a load ostávajú nemenné, obchod sa dopočíta.
    cu = sch["curtail_kwh"].values.astype(float)             # držíme baseline curtail
    grid = pv - cu + di - ch - load                           # kWh/perióda (kladné = export, záporné = import)
    # asymetrický limit: export cap = grid_kw_export, import cap = grid_kw_import
    cap_export = grid_kw_export * dt
    cap_import = grid_kw_import * dt
    over = np.maximum(grid - cap_export, 0.0)                  # export nad limit dodávky
    if over.any():
        if not allow_curtail:
            warnings.append({"t": int(np.argmax(over)), "kind": "grid_cap",
                              "msg": f"export by prekročil prípojku, no allow_curtail=False — bilancia nie je riešená"})
        else:
            cu = cu + over                                      # zvýši orezanie aby export sedel na prípojku
            grid = pv - cu + di - ch - load
    if (not allow_grid_charge) and (grid < -1e-6).any():
        ts = np.where(grid < -1e-6)[0]
        warnings.append({"t": int(ts[0]), "kind": "grid_charge_blocked",
                          "msg": f"plán potrebuje import v slot(och) {list(map(int, ts))}, no allow_grid_charge=False — v realite sa to prejaví ako odchýlka"})
    ex = np.maximum(grid, 0.0)
    im = -np.minimum(grid, 0.0)
    # výsledný sch
    sch_new = sch.copy()
    # Výstup v skutočných kW (rovnaká konvencia ako optimizer.optimize_day)
    sch_new["batt_kw"] = np.round(new / dt, 1)
    sch_new["_charge_kw"] = np.round(ch / dt, 1)
    sch_new["_discharge_kw"] = np.round(di / dt, 1)
    sch_new["_export_kwh"] = np.round(ex, 1)
    sch_new["_import_kwh"] = np.round(im, 1)
    sch_new["soc_kwh"] = np.round(socs, 1)
    sch_new["soc_pct"] = np.round(socs / batt_kwh * 100, 0)
    sch_new["grid_kwh"] = np.round(grid, 1)
    order = np.round(grid / 1000.0, 3)
    if min_trade_mwh > 0:
        order = np.where(np.abs(order) < min_trade_mwh, 0.0, order)
    sch_new["order_mwh"] = order
    sch_new["curtail_kwh"] = np.round(cu, 1)
    # ekonomika prepočítaná z nového rozvrhu
    rev_ex = float((pr * ex).sum() / 1000)
    cost_im = float(((pr + grid_fee) * im).sum() / 1000)
    cyc_cost = float((cycle_cost * di).sum() / 1000)
    net = rev_ex - cost_im - cyc_cost
    base_rev = float((np.where(pr > 0, pr, 0) * pv).sum() / 1000)
    summary_new = {
        "trzba_export_EUR": round(rev_ex, 2), "naklad_import_EUR": round(cost_im, 2),
        "naklad_cyklus_EUR": round(cyc_cost, 2), "ZISK_EUR": round(net, 2),
        "bez_baterie_EUR": round(base_rev, 2), "prinos_baterie_EUR": round(net - base_rev, 2),
        "nabite_kWh": round(float(ch.sum()), 1), "vybite_kWh": round(float(di.sum()), 1),
        "import_kWh": round(float(im.sum()), 1), "orezane_kWh": round(float(cu.sum()), 1),
        "override_active": int(np.sum(~np.isclose(mult, 1.0))),
        "override_warnings": warnings,
        "ZISK_EUR_baseline": summary.get("ZISK_EUR"),
    }
    return sch_new, summary_new


# ----------------- demo: plán na zajtra -----------------
def _tomorrow_plan(**kw):
    import datetime as dt
    import data_sources as ds
    from price_model import PriceModel
    LAT, LON, KWP, TILT, AZ, EFF = 49.5961, 17.3634, 99.0, 30.0, 0.0, 0.85
    day = dt.date.today() + dt.timedelta(days=1)
    try:
        pm = PriceModel.load("out/price_model.joblib")
    except Exception:
        pm = PriceModel().fit(pd.read_csv("out/price_train_2026.csv", parse_dates=["time"]))
    wx = ds.fetch_pv_forecast(LAT, LON, KWP, TILT, AZ, EFF, start=day, end=day)
    wx["time"] = pd.to_datetime(wx["time"]); wx = wx[wx.time.dt.date == day].copy()
    wx["hour"] = wx.time.dt.hour; wx["dow"] = wx.time.dt.dayofweek; wx["month"] = wx.time.dt.month
    pr = pm.predict(wx).sort_values("hour")
    sch, summ = optimize_day(pr.kw.values, pr.pred_isot.values, **kw)
    cols = ["hour", "pv_kwh", "price_eur", "batt_kw", "grid_kwh", "order_mwh", "curtail_kwh", "soc_pct"]
    print(f"\nPLÁN D-1 na {day}  (batt_kw: + vybíja / − nabíja;  grid_kwh: + predaj / − nákup)")
    print(sch[cols].to_string(index=False))
    print("\nSúhrn:")
    for k, v in summ.items():
        print(f"  {k:22s}: {v:>8}")
    return sch, summ


if __name__ == "__main__":
    _tomorrow_plan()
