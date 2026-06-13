# -*- coding: utf-8 -*-
"""
rt_controller.py – MINÚTOVÝ reaktívny RT regulátor riadený VEĽKOSŤAMI V MW.
Žiadne ceny aFRR/mFRR. Signál = veľkosť systémovej odchýlky [MW] + veľkosť aktivácie
FRR služieb [MW] (mFRR ako event/extrém ho zosilní). Smer:
  - prevažuje UP regulácia (aFRR+/mFRR+) / systém v deficite  -> ZCO hore -> VYBI
  - prevažuje DOWN regulácia (aFRR-/mFRR-) / systém v prebytku -> ZCO dole -> NABI
Ide po minútach, drží priebežný priemer signálu v 15-min perióde, necitlivostné pásmo
je v MW; pri aktivácii mFRR (vzácne, stres) sa reaguje výraznejšie. Zúčtuje sa skutočnou ZCO.

Dáta: out/imbalance_minute.csv.  python rt_controller.py
"""
from __future__ import annotations
import numpy as np, pandas as pd

BATT_KW, BATT_KWH = 100.0, 200.0
EFFC, EFFD = 0.95, 0.95
SOC_MIN, SOC_MAX, SOC0 = 0.05, 0.95, 0.50
CYCLE, MAX_CYCLES = 2.0, 2.0
# regulátor (MW) – ASYMETRICKÉ pásmo
BANDS_DIS = [20.0, 30.0, 50.0, 75.0]    # VYBÍJACIE pásmo (selektívne – drahá ZCO je vzácna)
BANDS_CHG = [5.0, 10.0, 20.0]           # NABÍJACIE pásmo (agresívne – lacná/záporná ZCO je častá)
W_SYS = 3.0                              # váha systémovej odchýlky vs aktivácia
W_MFRR = 0.5                             # zosilnenie pri aktivácii mFRR (event)
STRONG_MW = 150.0                        # MW nad pásmom = plný výkon (dynamický výkon)
AFRR_MIN = 30.0     # min. absolútna aFRR+ [MW], aby sa VYBI event bral vážne (floor)
AFRR_MIN_CHG = 15.0 # min. absolútna aFRR− [MW] pre NABI event (agresívnejšie, bod 1)
EVENT_K_SIGMA = 1.0 # event keď aktivácia > priemer + K·σ (z dát: 0.5–1.5; 1.0 = čistá separácia)
MFRR_MIN = 1.0      # mFRR aktivované nad toto [MW] = event → plný výkon
ROLL_WIN = 180      # minút na priemer+σ aktivácie (pár hodín – základ pre detekciu výrazného skoku)
PROD_BAND_DIS = 30.0
PROD_BAND_CHG = 10.0
PROD_W_SYS = 3.0
PROD_SYS_ORIENT = -1.0
SYS_ORIENT = -1.0     # orientácia systémovej odchýlky (kladná odchýlka → systém dlhý → NABI)
SYS_DIR_GATE = True   # smer systémovej odchýlky je tvrdá hranica – event/aktivácia ho NESMIE obrátiť
SYS_GATE_MIN = 5.0    # mŕtve pásmo [MW]: pod túto |odchýlku| gate neaktivuje (filter šumu okolo 0)
AUTO_KDIS, AUTO_KCHG = 1.5, 0.5     # vybíjacie/nabíjacie pásmo = K × σ(signálu)
AUTO_MIN_DIS, AUTO_MIN_CHG = 15.0, 5.0
DT_BIAS_K = 0.5       # posun hranice [MW na €/MWh odchýlky DT od denného priemeru]
# FLIP-event: zmena smeru ktorejkoľvek služby oproti poslednému priemeru = výrazná cena (aj pri malej veľkosti)
FLIP_EVENT = True     # zapnúť detekciu flip-u smeru net aktivácie
FLIP_MIN_NET = 8.0    # aktuálna net aktivácia musí presiahnuť toto [MW] (filter šumu)
FLIP_ROLL_MIN = 15.0  # nedávny priemer net aktivácie musí byť opačný aspoň o toto [MW]
# Realizmus RT: haircut = zachytená časť teoretického zisku, latency = oneskorenie reakcie [min]
RT_HAIRCUT = 1.0      # 1.0 = teoretický strop; ~0.65 = realistický odhad (slippage, ramp, settle)
RT_LATENCY = 0        # 0 = okamžite; 1–2 = koná na rozhodnutie spred N minút
# Reversal boost: čím dlhšie beží jeden smer, tým nižší prah pre OPAČNÝ smer (skôr sa preklopí)
REVERSAL_BOOST = 0.0  # 0 = vypnuté; 0.5 = až −50 % prahu opačného smeru po dlhom behu
REVERSAL_SCALE = 30.0 # po koľkých minútach v jednom smere dosiahne boost plnú hodnotu
# SOC-citlivé prahy: pri vysokom SOC ľahšie vybíjať, pri nízkom ľahšie nabíjať (vracia SOC do stredu)
SOC_BIAS_K = 0.0      # 0 = vypnuté; 0.5 = až −50 % prahu v smere, ktorý vracia SOC do stredu
SOC_BIAS_HI = 80.0    # nad týmto SOC [%] sa znižuje VYBÍJACÍ prah
SOC_BIAS_LO = 20.0    # pod týmto SOC [%] sa znižuje NABÍJACÍ prah
ACT_COLS = ["aFRR_plus", "aFRR_minus", "mFRR_plus", "mFRR_minus", "mFRR5"]


def apply_case(cfg):
    """Nastaví konštanty jadra z CaseConfig. Logika ostáva nedotknutá – mení sa len konfigurácia.
    Volaj pred backtestom / živým rozhodovaním pre daný prípad."""
    global BATT_KW, BATT_KWH, EFFC, EFFD, SOC_MIN, SOC_MAX, SOC0, CYCLE, MAX_CYCLES
    global W_SYS, W_MFRR, STRONG_MW, AFRR_MIN, AFRR_MIN_CHG, EVENT_K_SIGMA, MFRR_MIN, ROLL_WIN
    global PROD_BAND_DIS, PROD_BAND_CHG, PROD_W_SYS, PROD_SYS_ORIENT
    global SYS_ORIENT, SYS_DIR_GATE, SYS_GATE_MIN
    global AUTO_KDIS, AUTO_KCHG, AUTO_MIN_DIS, AUTO_MIN_CHG, DT_BIAS_K
    global FLIP_EVENT, FLIP_MIN_NET, FLIP_ROLL_MIN, RT_HAIRCUT, RT_LATENCY
    global REVERSAL_BOOST, REVERSAL_SCALE, SOC_BIAS_K, SOC_BIAS_HI, SOC_BIAS_LO
    BATT_KW, BATT_KWH = cfg.batt_kw, cfg.batt_kwh
    EFFC, EFFD = cfg.eff_c, cfg.eff_d
    SOC_MIN, SOC_MAX, SOC0 = cfg.soc_min, cfg.soc_max, cfg.soc_init
    CYCLE, MAX_CYCLES = cfg.cycle, cfg.max_cycles
    W_SYS, W_MFRR, STRONG_MW = cfg.w_sys, cfg.w_mfrr, cfg.strong_mw
    AFRR_MIN, AFRR_MIN_CHG = cfg.afrr_min, cfg.afrr_min_chg
    EVENT_K_SIGMA, MFRR_MIN, ROLL_WIN = cfg.event_k_sigma, cfg.mfrr_min, int(cfg.roll_win)
    PROD_BAND_DIS, PROD_BAND_CHG = cfg.prod_band_dis, cfg.prod_band_chg
    PROD_W_SYS, PROD_SYS_ORIENT = cfg.w_sys, cfg.sys_orient
    SYS_ORIENT = float(cfg.sys_orient)
    SYS_DIR_GATE = bool(getattr(cfg, "sys_dir_gate", True))
    SYS_GATE_MIN = float(getattr(cfg, "sys_gate_min", 5.0))
    AUTO_KDIS, AUTO_KCHG = cfg.auto_kdis, cfg.auto_kchg
    AUTO_MIN_DIS, AUTO_MIN_CHG = cfg.auto_min_dis, cfg.auto_min_chg
    DT_BIAS_K = cfg.dt_bias_k
    FLIP_EVENT = bool(getattr(cfg, "flip_event", True))
    FLIP_MIN_NET = float(getattr(cfg, "flip_min_net", 6.0))
    FLIP_ROLL_MIN = float(getattr(cfg, "flip_roll_min", 10.0))
    RT_HAIRCUT = float(getattr(cfg, "rt_haircut", 1.0))
    RT_LATENCY = int(getattr(cfg, "rt_latency_min", 0))
    REVERSAL_BOOST = float(getattr(cfg, "reversal_boost", 0.0))
    REVERSAL_SCALE = float(getattr(cfg, "reversal_scale_min", 30.0))
    SOC_BIAS_K = float(getattr(cfg, "soc_bias_k", 0.0))
    SOC_BIAS_HI = float(getattr(cfg, "soc_bias_hi", 80.0))
    SOC_BIAS_LO = float(getattr(cfg, "soc_bias_lo", 20.0))


def auto_bands(sig_std):
    """Adaptívne pásmo z volatility signálu: vybíjanie selektívne (1.5σ), nabíjanie agresívne (0.5σ)."""
    if sig_std is None or pd.isna(sig_std) or sig_std <= 0:
        return PROD_BAND_DIS, PROD_BAND_CHG
    return (max(AUTO_MIN_DIS, round(AUTO_KDIS*sig_std)),
            max(AUTO_MIN_CHG, round(AUTO_KCHG*sig_std)))


def nz(x, d=0.0):
    return d if (x is None or pd.isna(x)) else float(x)


def mw_signal(rd, w_sys, sys_orient):
    """Smerový MW signál: + = tlak na VYBI (drahá ZCO), − = NABI."""
    up = nz(rd.get("aFRR_plus")) + nz(rd.get("mFRR_plus")) + nz(rd.get("mFRR5"))
    dn = nz(rd.get("aFRR_minus")) + nz(rd.get("mFRR_minus"))
    net = up - dn
    mfrr = nz(rd.get("mFRR_plus")) + nz(rd.get("mFRR_minus")) + nz(rd.get("mFRR5"))
    sig = net + w_sys * sys_orient * nz(rd.get("sys_MW"))
    if mfrr > 1:                       # mFRR sa štandardne neaktivuje → event/extrém
        sig *= (1.0 + W_MFRR)
    return sig


def decide_reason(rd, avg, band_dis, band_chg, strong_mw, dt_rel=0.0, dt_bias_k=None):
    """Ako decide(), ale vracia (direction, frac, reason) – reason = uplatnené pravidlo:
    'mfrr_up','afrr_up','mfrr_dn','afrr_dn','band_vybi','band_nabi','hold'."""
    mfrr_up = nz(rd.get("mFRR_plus")) + nz(rd.get("mFRR5"))
    mfrr_dn = nz(rd.get("mFRR_minus"))
    afrr_up, afrr_dn = nz(rd.get("aFRR_plus")), nz(rd.get("aFRR_minus"))
    up_thr = nz(rd.get("afrr_up_roll")) + EVENT_K_SIGMA * nz(rd.get("afrr_up_std"))
    dn_thr = nz(rd.get("afrr_dn_roll")) + EVENT_K_SIGMA * nz(rd.get("afrr_dn_std"))
    up_excess = (afrr_up - up_thr) if (afrr_up > AFRR_MIN) else -1e9
    dn_excess = (afrr_dn - dn_thr) if (afrr_dn > AFRR_MIN_CHG) else -1e9
    ev, ev_src = 0, None
    if mfrr_up > MFRR_MIN:
        ev, ev_src = 1, "mfrr_up"
    elif mfrr_dn > MFRR_MIN:
        ev, ev_src = -1, "mfrr_dn"
    elif up_excess > 0 and up_excess >= dn_excess:
        ev, ev_src = 1, "afrr_up"
    elif dn_excess > 0:
        ev, ev_src = -1, "afrr_dn"
    if ev == 0 and FLIP_EVENT:                              # sekundárne: FLIP smeru ktorejkoľvek služby
        cur_net = (afrr_up + mfrr_up) - (afrr_dn + mfrr_dn)
        nr = rd.get("act_net_roll")
        if nr is not None and not (isinstance(nr, float) and pd.isna(nr)):
            if cur_net > FLIP_MIN_NET and nr < -FLIP_ROLL_MIN:
                ev, ev_src = 1, "flip_up"
            elif cur_net < -FLIP_MIN_NET and nr > FLIP_ROLL_MIN:
                ev, ev_src = -1, "flip_dn"
    b = (DT_BIAS_K if dt_bias_k is None else dt_bias_k) * dt_rel
    bd = max(AUTO_MIN_DIS, band_dis - b)
    bc = max(AUTO_MIN_CHG, band_chg + b)
    if avg > bd:                                            # signál jasne VYBI
        d_, f_, s_ = (1.0, 1.0, ev_src) if ev == 1 else (1.0, min(1.0, (avg - bd)/strong_mw), "band_vybi")
    elif avg < -bc:                                         # signál jasne NABI
        d_, f_, s_ = (-1.0, 1.0, ev_src) if ev == -1 else (-1.0, min(1.0, (-avg - bc)/strong_mw), "band_nabi")
    elif ev != 0 and ev_src not in ("flip_up", "flip_dn"):  # mŕtve pásmo: koná len silný event (nie flip)
        d_, f_, s_ = float(ev), 1.0, ev_src
    else:
        d_, f_, s_ = 0.0, 0.0, "hold"
    # TVRDÁ HRANICA: smer systémovej odchýlky. Regulátor NESMIE konať proti odchýlke
    # (kladná odchýlka → systém dlhý → len NABI/DRŽ; záporná → len VYBI/DRŽ), nech aktivácia/event tvrdia čokoľvek.
    if SYS_DIR_GATE:
        sys_mw = nz(rd.get("sys_MW"))
        if abs(sys_mw) > SYS_GATE_MIN:
            sys_pref = SYS_ORIENT * sys_mw                  # >0 → odchýlka žiada VYBI, <0 → NABI
            if d_ > 0 and sys_pref < 0:                     # chcelo VYBI proti odchýlke
                return 0.0, 0.0, "hold_sys"
            if d_ < 0 and sys_pref > 0:                     # chcelo NABI proti odchýlke
                return 0.0, 0.0, "hold_sys"
    return d_, f_, s_


def decide(rd, avg, band_dis, band_chg, strong_mw, dt_rel=0.0, dt_bias_k=None):
    """Vráti (direction, frac): +1 VYBI, −1 NABI, 0 DRŽ; frac = výkon 0..1.
    Smer určuje MW signál (avg) vs DT-posunuté pásmo; výrazný skok aktivácie (event) dá plný
    výkon v zhode so signálom / v mŕtvom pásme, no NIKDY neobráti smer proti silnému signálu."""
    d, f, _ = decide_reason(rd, avg, band_dis, band_chg, strong_mw, dt_rel, dt_bias_k)
    return d, f


def soc_bias_bands(bd, bc, soc_pct):
    """SOC-citlivé prahy: vysoký SOC → nižší vybíjací prah; nízky SOC → nižší nabíjací prah.
    Posúva prah v smere, ktorý vracia SOC do stredu (mimo pásma SOC_BIAS_LO..HI sa rampuje)."""
    if SOC_BIAS_K <= 0:
        return bd, bc
    if soc_pct > SOC_BIAS_HI:
        f = min(1.0, (soc_pct - SOC_BIAS_HI) / max(1.0, SOC_MAX*100 - SOC_BIAS_HI))
        bd = bd * (1.0 - SOC_BIAS_K*f)              # ľahšie VYBÍJAŤ keď je plno
    elif soc_pct < SOC_BIAS_LO:
        f = min(1.0, (SOC_BIAS_LO - soc_pct) / max(1.0, SOC_BIAS_LO - SOC_MIN*100))
        bc = bc * (1.0 - SOC_BIAS_K*f)              # ľahšie NABÍJAŤ keď je prázdno
    return bd, bc


def run_day_physical(g, plan_kw_arr, day_start, step_min, band_dis, band_chg, w_sys, sys_orient,
                     soc0=None, dev_budget_kwh=None, dt_bias_k=None, strong_mw=STRONG_MW,
                     grid_kw_arr=None, grid_cap=None, return_trace=False, rt_mask=None,
                     soc_min_arr=None, pv_min_kw=None, ftv_balance_on=False,
                     ftv_lookahead_h=4.0, ftv_persistence_throttle=True,
                     rt_no_worsen_dev=True, ftv_strict_plan=True,
                     pv_plan_kw=None, ftv_strict_deadband_kw=5.0,
                     grid_cap_import=None, grid_cap_export=None,
                     load_min_kw=None, load_plan_kw=None,
                     # Bug RT-INLINE-AUDIT (2026-06-11): realistic batt clip + audit dovnútra loop.
                     # Bez tohto rt_controller-internal soc DIVERGÍ od reality (= predčasné vyčerpanie
                     # SOC cez deň, dezolatne SOC clip aktivácie v drahých hodinách → píla 20-21h).
                     # User 2026-06-11: "vnutorne sa soc vycerpala pricom realne nie".
                     enforce_realistic=False, audit_today_state=None,
                     audit_soc_reserve_pct=0.0,
                     audit_rt_persistence_slots=4,
                     audit_future_horizon_slots=96,
                     # RT poradca 2.0 (2026-06-11): rt_engine="v2" → ekonomické rozhodnutie
                     # (E[ZCO] z kalibrovaného spreadu vs náklady) namiesto signálovej
                     # heuristiky v1. Downstream vrstvy (no-worsen, lookahead, persistencia,
                     # grid, inline audit) ostávajú IDENTICKÉ pre obe verzie.
                     rt_engine: str = "v1", rt2_params=None):
    """JEDNA fyzická batéria: plán (nominácia, plan_kw_arr po periódach, +vybi/−nabi) + RT odchýlka
    zdieľajú SOC aj výkon (±BATT_KW). Odchýlka = skutočná práca − plán, zúčtovaná na ZCO.
    grid_kw_arr/grid_cap: nominovaná sieťová pozícia [kW] po periódach (už ZAHŔŇA FTV) a limit prípojky;
    obmedzia odchýlku tak, aby FTV+batéria neprekročili prípojku. Keď SOC/výkon nestačí ani na plán,
    výpadok je tiež odchýlka. Používa globály z apply_case.

    pv_min_kw: voliteľný 1440-prvkový rad minútovej REALITY FTV (z ftv_minute.hourly_to_minute).
        Ak je zadaný, minútová odchýlka FTV oproti hodinovému plánu sa berie ako "pre_dev"
        (kandidát na threshold odchýlku voči obchodnému plánu).
    pv_plan_kw: voliteľný rad hodinových (24) alebo per-period (npn) hodnôt PVF PLÁNU = čo bolo
        nominované D-1. Používa sa na výpočet pre_dev keď je pv_min_kw zo scenára (= realita ≠ plán).
        Ak nie je posunutý, pv_period_plan sa derivuje z pv_min_kw (mean per perióda).
    ftv_balance_on: keď True a v slote rt_mask=1, batéria sa snaží neutralizovať pre_dev
        za podmienky že znak pre_dev je OPAČNÝ ako znak mw_signal (= len keď hrozí pokuta).
        Pridáva sa NA VRCH existujúceho MW signal enginu (additívne k jeho rozhodnutiu).
    ftv_lookahead_h: koľko hodín dopredu sa pozeráme na plán pri obmedzovaní RT zásahu.
        Ak je plánované nabíjanie v najbližších N hodinách → RT nabíjanie sa zníži tak, aby zostala kapacita.
        Ak je plánované vybíjanie → RT vybíjanie sa zníži tak, aby zostal SOC. (Default 2.0 h)
    ftv_persistence_throttle: keď True, agresivita RT sa zníži ak signál je pretrvávajúci v jednom smere
        (príležitostí je veľa, netreba grobsknúť každú minútu).
    rt_no_worsen_dev: keď True, RT zásah nikdy nezhorší threshold odchýlku — t.j. ak FTV minútová realita
        už spôsobuje under-deliver oproti plánu (pre_dev<0), RT nesmie batériu ešte nabíjať; ak over-deliver
        (pre_dev>0), RT nesmie ešte vybíjať. Smie iba zmenšiť odchýlku alebo nezasahovať.
        Toto má prednosť pred MW signal engine arbitrážou (chráni plán pred zhoršením).
    ftv_strict_plan: keď True (default), FTV-balance ZÁVÄZNE drží plán pri VÝZNAMNEJ FTV odchýlke
        (|pre_dev| > ftv_strict_deadband_kw) — batt_extra = −pre_dev a MW engine je POTLAČENÝ.
        Pre menšie šumové fluktuácie (|pre_dev| ≤ deadband) sa použije pôvodná FTV-balance logika
        (additive na vrch MW, fire iba pri opačných znakoch). Plán má prednosť LEN tam, kde naozaj hrozí.
        Keď False, fire iba pri opačných znakoch bez ohľadu na veľkosť odchýlky.
    ftv_strict_deadband_kw: prah šumu pre strict_plan override (default 5 kW). Šumové fluktuácie pod
        toto neblokujú MW arbitráž — ten ide bežne. Nad to sa preberá kontrola nad plánovou adherenciou.
    enforce_realistic: keď True, celkový batt výkon `tot` sa per minútu oreže na fyzicky dostupný
        výkon (= grid_export + max(load_min − ftv_min, 0) pre vybi; grid_import + max(ftv_min − load_min, 0)
        pre nabi). Soc sa potom integruje s týmto ORZANÝM výkonom — žiadna divergencia rt_controller-internal
        soc vs realistic. (default False = backward compat)
    audit_today_state: dict pre `core.soc_use_audit.audit_capacity` (batt_kwh, eff_c/d, soc_min/max_pct,
        dam_nomination_kwh [96], vdt_realized_kwh [96]). Keď zadaný, audit sa volá per minútu PRED
        zápisom trace a orezáva RT zložku tak, aby nebola porušená SOC rezerva pre budúce zazmluvnené sloty.
    audit_soc_reserve_pct: rezerva pásma pre audit (typicky 15 = SOC musí ostať v [soc_min+15, soc_max-15]).

    Vracia rev (zisk z odchýlky), cykly_spolu; pri return_trace aj minútový DataFrame."""
    import numpy as _np
    BK, BKWH = BATT_KW, BATT_KWH
    lo, hi = SOC_MIN*BKWH, SOC_MAX*BKWH
    plan = _np.asarray(plan_kw_arr, float); npn = len(plan)
    # RT mask: 1=povolené, 0=zablokované (drží sa plán). Default None → všade povolené.
    rt_on = _np.asarray(rt_mask, float) if rt_mask is not None else None
    if rt_on is not None and rt_on.shape != (npn,):
        raise ValueError(f"rt_mask má dĺžku {rt_on.shape}, očakávam ({npn},)")
    # per-period SOC floor (v %, 0-100). Ak nezadané, použije sa globálne SOC_MIN.
    # Slúži na "rezervu kapacity" — napr. ráno SOC_min=80% (RT nemôže vybíjať pod 80% pred večerom).
    soc_lo_arr = None
    if soc_min_arr is not None:
        soc_lo_arr = _np.maximum(_np.asarray(soc_min_arr, float), SOC_MIN * 100.0) * BKWH / 100.0
        if soc_lo_arr.shape != (npn,):
            raise ValueError(f"soc_min_arr má dĺžku {soc_lo_arr.shape}, očakávam ({npn},)")
    e_h = 1/60.0
    soc = (SOC0*BKWH) if soc0 is None else float(soc0)
    dev_budget = (MAX_CYCLES*BKWH) if dev_budget_kwh is None else max(0.0, dev_budget_kwh)
    # asymetrické grid limity: ak nie sú zadané, použijú symetrický grid_cap (backward compat)
    _grid_imp = float(grid_cap_import) if grid_cap_import is not None else (
        float(grid_cap) if grid_cap is not None else None)
    _grid_exp = float(grid_cap_export) if grid_cap_export is not None else (
        float(grid_cap) if grid_cap is not None else None)
    dts = g["isot_eur"].dropna(); day_mean = float(dts.mean()) if len(dts) else 0.0
    day_start = pd.Timestamp(day_start)
    cur = None; psum = pcount = 0.0; run_dir = 0; run_min = 0; thru = 0.0
    rows = [] if return_trace else None
    rev = 0.0
    # ── LOAD: minútová realita + per-perióda priemer ──
    # threshold odchýlka = (pv_min - pv_plan) − (load_min − load_plan).
    # Spotreba ktorá je vyššia ako plánovaná = under-deliver (negatívny príspevok k pre_dev).
    load_min = None
    load_period_plan = None
    if load_min_kw is not None:
        load_min = _np.asarray(load_min_kw, float).ravel()
        if load_min.size != 1440:
            load_min = None
    if load_min is not None:
        if load_plan_kw is not None:
            _lp = _np.asarray(load_plan_kw, float).ravel()
            if _lp.size == npn:
                load_period_plan = _lp
            elif _lp.size == 24 and npn == 96:
                load_period_plan = _np.repeat(_lp, 4)
            elif _lp.size == 96 and npn == 24:
                load_period_plan = _lp.reshape(24, 4).mean(axis=1)
        if load_period_plan is None:
            mins_per_period = int(step_min)
            if 1440 % mins_per_period == 0:
                load_period_plan = load_min.reshape(-1, mins_per_period).mean(axis=1)
    # ── FTV-driven balansovanie: priprav minútovú realitu + per-perióda priemer (= "FTV plán") ──
    pv_min = None
    pv_period_plan = None
    if pv_min_kw is not None:
        pv_min = _np.asarray(pv_min_kw, float).ravel()
        if pv_min.size == 1440:
            # 1) prefer EXPLICIT pv_plan_kw (= pôvodný PVF plán nominovaný D-1)
            if pv_plan_kw is not None:
                _pp = _np.asarray(pv_plan_kw, float).ravel()
                if _pp.size == npn:
                    pv_period_plan = _pp
                elif _pp.size == 24 and npn in (24, 96):
                    # 24 hodinových → na npn periód
                    if npn == 24:
                        pv_period_plan = _pp
                    else:  # 96 = 4× každá hodina
                        pv_period_plan = _np.repeat(_pp, 4)
                elif _pp.size == 96 and npn == 24:
                    pv_period_plan = _pp.reshape(24, 4).mean(axis=1)
            # 2) fallback: derive z minútovej reality (= keď scenár nie je / plán = realita)
            if pv_period_plan is None:
                mins_per_period = int(step_min)
                if 1440 % mins_per_period == 0:
                    pv_period_plan = pv_min.reshape(-1, mins_per_period).mean(axis=1)
                else:
                    pv_period_plan = None
        else:
            pv_min = None
    # RT v2.1 (substitučné referencie): per-perióda DT cena za celý deň (známa D-1).
    # Použité len pri rt_engine="v2" + restore_mode="auto" — ocenenie zásahu ako
    # substitúcie budúcej plánovanej akcie (viď rt_engine_v2.decide_v2).
    _pp_sum = _np.zeros(npn); _pp_cnt = _np.zeros(npn)
    if str(rt_engine) in ("v2", "v3"):
        try:
            for _r0 in g.itertuples(index=False):
                _rd0 = _r0._asdict()
                _dtp0 = _rd0.get("isot_eur")
                if pd.isna(_dtp0):
                    continue
                _pi0 = int((pd.Timestamp(_rd0.get("ts15")) - day_start).total_seconds()
                           // (step_min * 60))
                if 0 <= _pi0 < npn:
                    _pp_sum[_pi0] += float(_dtp0); _pp_cnt[_pi0] += 1
        except Exception:
            pass
    _price_per_period = _np.where(_pp_cnt > 0, _pp_sum / _np.maximum(_pp_cnt, 1), _np.nan)
    for r in g.itertuples(index=False):
        rd = r._asdict(); zco = rd.get("zco_eur"); dtp = rd.get("isot_eur")
        # DT je povinný (jadro arbitrážnej logiky). ZCO môže byť NaN (napr. SK dnes — ZCO je
        # publikované až D-1) — vtedy len zúčtovanie odchýlky = 0, ale rozhodnutie bežíme normálne.
        if pd.isna(dtp):
            continue
        if pd.isna(zco):
            zco = 0.0
        if rd.get("ts15") != cur:
            cur = rd.get("ts15"); psum = pcount = 0.0
        sig = mw_signal(rd, w_sys, sys_orient)
        psum += sig; pcount += 1; avg = psum/pcount
        dt_rel = float(dtp) - day_mean
        pidx = int((pd.Timestamp(rd.get("ts15")) - day_start).total_seconds() // (step_min*60))
        pidx = min(npn-1, max(0, pidx))
        plan_kw = float(plan[pidx])
        # per-period SOC floor (lo_eff) — počítame VŽDY skôr, lebo lookahead aj energy clipping ho používajú
        lo_eff = lo if soc_lo_arr is None else float(soc_lo_arr[pidx])
        bd_eff, bc_eff = band_dis, band_chg
        if REVERSAL_BOOST > 0 and run_dir != 0:
            boost = REVERSAL_BOOST * min(1.0, run_min/max(1.0, REVERSAL_SCALE))
            if run_dir < 0:
                bd_eff = band_dis*(1.0-boost)
            else:
                bc_eff = band_chg*(1.0-boost)
        bd_eff, bc_eff = soc_bias_bands(bd_eff, bc_eff, soc/BKWH*100)
        if str(rt_engine) == "v3":
            # RT poradca 3.0 (2026-06-12): marginálna hodnota energie — jediný
            # princíp namiesto veže pravidiel. Viď rt_value.decide_v3. Nastavenia
            # profilu sú vstupom (rt_on maska, voľný výkon nad plánom, committed
            # plán ako rezerva). No-worsen/persistencia/lookahead sa pre v3
            # PRESKAKUJÚ — ich úlohu preberá ocenenie; fyzika (SOC/grid/audit)
            # clipy ostávajú.
            from rt_engine_v2 import expected_zco_spread as _ezs_v3
            from rt_value import decide_v3 as _decide_v3
            _sig_raw_v3 = avg * (float(sys_orient) if sys_orient in (1, -1, 1.0, -1.0) else 1.0)
            try:
                _hr_v3 = pd.Timestamp(rd.get("time")).hour
            except Exception:
                _hr_v3 = None
            _p3 = rt2_params or {}
            _spread_v3, _src3 = _ezs_v3(_sig_raw_v3, float(_p3.get("rt2_zco_k") or 0.6),
                                        hour=_hr_v3)
            _zco_exp_v3 = float(dtp) + _spread_v3
            _period_h_v3 = step_min / 60.0
            _fut_plan3 = _np.asarray(plan[pidx + 1:], float) if pidx + 1 < npn else _np.zeros(0)
            _fp3 = _price_per_period[pidx + 1: pidx + 1 + _fut_plan3.size]
            if rt_on is not None and _fut_plan3.size:
                _mask3 = (_np.asarray(rt_on[pidx + 1: pidx + 1 + _fut_plan3.size],
                                      float) >= 0.5)
            else:
                _mask3 = _np.ones(_fut_plan3.size, dtype=bool)
            _free_dis3 = _np.where(_mask3, _np.clip(BK - _np.clip(_fut_plan3, 0, None), 0, None), 0.0)
            _free_chg3 = _np.where(_mask3, _np.clip(BK - _np.clip(-_fut_plan3, 0, None), 0, None), 0.0)
            _fd_kwh3 = float(_np.sum(_np.clip(_fut_plan3, 0, None)) * _period_h_v3)
            _fc_kwh3 = float(_np.sum(_np.clip(-_fut_plan3, 0, None)) * _period_h_v3)
            _rt_kw3, reason = _decide_v3(
                _zco_exp_v3, soc, lo_eff, hi, BK, _period_h_v3, EFFC, EFFD,
                float(_p3.get("rt2_cycle_cost") or 5.0),
                _fp3, _free_dis3, _free_chg3, _fd_kwh3, _fc_kwh3,
                margin_min_eur=float(_p3.get("rt2_margin_min_eur") or 10.0),
                margin_min_chg_eur=_p3.get("rt2_margin_min_chg_eur"))
            if abs(_rt_kw3) > 1e-6:
                d = 1 if _rt_kw3 > 0 else -1
                f = min(1.0, abs(_rt_kw3) / max(BK, 1e-9))
            else:
                d, f = 0, 0.0
            reason = f"{reason} ({_src3})"
        elif str(rt_engine) == "v2":
            # RT poradca 2.0: ekonomický zámer (kalibrovaný E[ZCO] spread vs náklady).
            # Bug RT2-SIGN (2026-06-12, user: "pri nedostatku sa nabíja"): kalibrácia
            # beží na SUROVOM sys_MWh z imbalance_history, ale `avg` má aplikovanú
            # orientáciu (sys_orient) z mw_signal → vráť orientáciu pred kalibráciou
            # (orient ∈ {±1}: raw = avg × orient).
            from rt_engine_v2 import decide_v2 as _decide_v2
            _sig_raw_cal = avg * (float(sys_orient) if sys_orient in (1, -1, 1.0, -1.0) else 1.0)
            # Bod 3+4: hodina pre daypart kalibráciu + zostávajúci plán dnes
            # (kWh nabíjania/vybíjania od ďalšej periódy) pre adaptívnu váhu obnovy.
            try:
                _hr_v2 = pd.Timestamp(rd.get("time")).hour
            except Exception:
                _hr_v2 = None
            _period_h_v2 = step_min / 60.0
            _fut_plan = _np.asarray(plan[pidx + 1:], float) if pidx + 1 < npn else _np.zeros(0)
            _fut_chg_kwh_v2 = float(_np.sum(-_fut_plan[_fut_plan < 0]) * _period_h_v2)
            _fut_dis_kwh_v2 = float(_np.sum(_fut_plan[_fut_plan > 0]) * _period_h_v2)
            # Substitučné referenčné ceny: vážený priemer DT cien budúcich
            # plánovaných nabíjacích / vybíjacích slotov (váha = |kW| slotu)
            _ref_chg_v2 = _ref_dis_v2 = None
            try:
                if _fut_plan.size:
                    _pp_fut = _price_per_period[pidx + 1: pidx + 1 + _fut_plan.size]
                    _m_chg = (_fut_plan < 0) & _np.isfinite(_pp_fut)
                    if _m_chg.any():
                        _ref_chg_v2 = float(_np.average(_pp_fut[_m_chg],
                                                        weights=-_fut_plan[_m_chg]))
                    _m_dis = (_fut_plan > 0) & _np.isfinite(_pp_fut)
                    if _m_dis.any():
                        _ref_dis_v2 = float(_np.average(_pp_fut[_m_dis],
                                                        weights=_fut_plan[_m_dis]))
            except Exception:
                pass
            # Bug RT2-DAY-ANCHOR (2026-06-12): bez plánovanej akcie kotvi marže na
            # kvantily DT cien ZVYŠKU dňa — nákup len pod lacnou hladinou (p25),
            # predaj len nad drahou (p75). Bez toho čistý RT profil (žiadny obchod)
            # nakupoval večer za drahé ZCO 120 a predával v noci za lacné 81
            # (smer voči DT OK, absolútna hladina dňa zlá).
            try:
                _pp_rest = _price_per_period[pidx + 1:]
                _pp_fin = _pp_rest[_np.isfinite(_pp_rest)]
                if _pp_fin.size >= 4:
                    if _ref_chg_v2 is None:
                        _ref_chg_v2 = float(_np.quantile(_pp_fin, 0.25))
                    if _ref_dis_v2 is None:
                        _ref_dis_v2 = float(_np.quantile(_pp_fin, 0.75))
            except Exception:
                pass
            # SURPLUS/DEFICIT bilancia: koľko energie nad SOC floor batéria má vs
            # koľko jej plán ešte reálne uplatní (predaj/η_d − nákup×η_c). Plus
            # overflow: časť plánovaného nákupu, ktorá sa už NEZMESTÍ pod strop.
            _avail_kwh_v2 = max(0.0, soc - lo_eff)
            _need_kwh_v2 = max(0.0, _fut_dis_kwh_v2 / max(EFFD, 0.01)
                               - _fut_chg_kwh_v2 * EFFC)
            _chg_overflow_v2 = max(0.0, _fut_chg_kwh_v2 * EFFC - max(0.0, hi - soc))
            _surplus_kwh_v2 = max(_avail_kwh_v2 - _need_kwh_v2, _chg_overflow_v2) \
                if (_avail_kwh_v2 - _need_kwh_v2) > 0 or _chg_overflow_v2 > 0 \
                else (_avail_kwh_v2 - _need_kwh_v2)
            d, f, reason = _decide_v2(_sig_raw_cal, float(dtp), soc / BKWH * 100.0,
                                      rt2_params, hour=_hr_v2,
                                      future_chg_kwh=_fut_chg_kwh_v2,
                                      future_dis_kwh=_fut_dis_kwh_v2,
                                      batt_kwh=BKWH,
                                      ref_chg_price=_ref_chg_v2,
                                      ref_dis_price=_ref_dis_v2,
                                      surplus_kwh=_surplus_kwh_v2)
        else:
            d, f, reason = decide_reason(rd, avg, bd_eff, bc_eff, strong_mw, dt_rel, dt_bias_k)
        if rt_on is not None and rt_on[pidx] < 0.5:           # RT v tomto slote zablokovaná → drž plán
            d, f = 0, 0.0
            reason = "rt_blocked"
        if d != 0:
            run_min = run_min + 1 if d == run_dir else 1; run_dir = d
        tot = max(-BK, min(BK, plan_kw + d*f*BK))         # celkový výkon (±BK)
        # ── pre_dev = threshold odchýlka voči obchodnému plánu (= FTV + load posunutie) ──
        # threshold_realita = pv_min − load_min, threshold_plan = pv_period_plan − load_period_plan
        # pre_dev = threshold_realita − threshold_plan = (pv_min − pv_plan) − (load_min − load_plan)
        pre_dev = 0.0
        t_ts = rd.get("time")
        if t_ts is not None and (pv_min is not None or load_min is not None):
            m_idx = int((pd.Timestamp(t_ts) - day_start).total_seconds() // 60)
            if pv_min is not None and pv_period_plan is not None and 0 <= m_idx < pv_min.size:
                pre_dev += float(pv_min[m_idx]) - float(pv_period_plan[pidx])
            if load_min is not None and load_period_plan is not None and 0 <= m_idx < load_min.size:
                pre_dev -= float(load_min[m_idx]) - float(load_period_plan[pidx])
        # ── FTV-driven balansovanie threshold odchýlky ──
        # strict_plan + no-worsen majú zmysel IBA keď je čo defendovať = plánovaná batt akcia.
        # Pri plan_kw ≈ 0 (× = 0 alebo prirodzene nulový slot) máme freedom pre MW arbitráž.
        _strict_fired = False
        _has_plan_action = abs(plan_kw) > 1e-3                              # plán s batt akciou
        _strict_significant = (ftv_strict_plan and _has_plan_action
                               and abs(pre_dev) > float(ftv_strict_deadband_kw))
        if ftv_balance_on and rt_on is not None and rt_on[pidx] >= 0.5 and abs(pre_dev) > 1e-3:
            _fire = _strict_significant or (pre_dev * sig < 0.0)
            if _fire:
                batt_extra = -pre_dev
                if _strict_significant:
                    # významná FTV odchýlka + plánovaná akcia: prepíš tot, MW engine ignoruje
                    tot = max(-BK, min(BK, plan_kw + batt_extra))
                    d, f, reason = 0, 0.0, "strict_plan_override"
                    _strict_fired = True
                else:
                    # malá odchýlka alebo opačné znaky bez strict: pridaj na vrch MW (additívne)
                    tot = max(-BK, min(BK, tot + batt_extra))

        # ═══════════════════════════════════════════════════════════════════════
        # PLAN-AWARE THROTTLE pre CELÝ RT zásah (MW engine + FTV-balance)
        # Cieľ: nech sa batéria zbytočne nezaplní/nevyprázdni keď ju budeme potrebovať na plán.
        # Aplikuje sa na rt_action = tot − plan_kw  (= čokoľvek mimo nominácie).
        # ═══════════════════════════════════════════════════════════════════════
        rt_action = tot - plan_kw                          # signed kW: + nad plán = navyše vybíja, − = navyše nabíja
        # ───── NO-WORSEN: RT zásah nesmie zhoršovať threshold odchýlku ─────
        # Aplikuje sa LEN keď je plánovaná batt akcia (= defendovať plán). Pri plan_kw ≈ 0
        # arbitráž ide podľa MW signálu bez tejto bariéry.
        # Dve vetvy:
        #   A) FTV/load vytvorili odchýlku (|pre_dev| > 0): RT nesmie zhoršiť
        #      pre_dev > 0 (over-deliver): rt_action ≤ 0 (RT smie iba zmenšiť, nie zvýšiť threshold)
        #      pre_dev < 0 (under-deliver): rt_action ≥ 0 (RT smie iba zvýšiť, nie ešte znížiť threshold)
        #   B) Žiadna FTV/load odchýlka (pre_dev ≈ 0): RT nesmie ÍSŤ PROTI smeru plánu —
        #      ak plán hovorí VYBI/NABI a MW engine by ho chcel zvrátiť, máme držať plán
        #      (kontrakt voči trhu = obchod, MW arbitrage nesmie znegovať planovaný zisk).
        if rt_no_worsen_dev and _has_plan_action and str(rt_engine) != "v3":
            if abs(pre_dev) > 1e-3:
                # vetva A — FTV/load odchýlka (LEGITIMNA: RT nesmie zhorsit
                # already-existing threshold odchylku spôsobenú FTV/load driftom)
                if pre_dev > 0 and rt_action > 0:
                    rt_action = 0.0
                elif pre_dev < 0 and rt_action < 0:
                    rt_action = 0.0
            # Bug RT-OPPOSITE-PLAN (2026-06-10): vetva B (RT nesmie ist proti
            # smeru planu pri pre_dev≈0) ODSTRANENA. User report: "implementacia
            # auditu je zal teraz sa len nabija a nevybija". Vetva B blokovala
            # RT vybijanie kedykolvek plán nabija (aj pri vysokom MW signali).
            # Povodne pridana v task #222 ako "obrana zmluveneho obchodu", ale:
            # 1. RT signál (sys_MW) je výrazne ne-arbitrárny — sluzi balansovaniu
            #    systému + RT je platený cez ZCO. Blokovat ho je strata.
            # 2. Capacity ledger + rt_audit už chránia SOC trajektoriu pred preplnením.
            # 3. Pri pre_dev≈0 znamená že trhova nominacia ide podla planu, takze
            #    RT zásah v opacnom smere len mení batt timing — settlement cez ZCO
            #    si poradí (a typicky zarobi keď ide so signálom).
        # Lookahead a persistencia spracujú aj rt_action=0 (no-op) bezpečne. Tot však treba vždy
        # rekonštruovať, aby no-worsen nulovanie skutočne zrušilo RT zásah.
        # POZOR: ak strict_plan fire batt_extra, NEDOTÝKAME sa rt_action — plán dnes > rezerva zajtra.
        if not _strict_fired:
            # ───── LOOKAHEAD: pozri ~ftv_lookahead_h hodín dopredu na plán ─────
            if ftv_lookahead_h > 0 and rt_action != 0.0 and str(rt_engine) != "v3":
                _period_h = step_min / 60.0
                _n_look = max(1, int(round(ftv_lookahead_h / _period_h)))
                _future_disch_kwh = 0.0   # SOC ktorá musí ostať pre plánované vybíjanie
                _future_charge_kwh = 0.0  # voľná kapacita ktorá musí ostať pre plánované nabíjanie
                for _k in range(1, _n_look + 1):
                    _pi = pidx + _k
                    if _pi >= npn:
                        break
                    _pkw = float(plan[_pi])
                    if _pkw > 0:
                        _future_disch_kwh += _pkw * _period_h
                    else:
                        _future_charge_kwh += -_pkw * _period_h
                # efektívne SOC hranice s rezervou pre plán
                _soc_floor_eff_la = min(hi, lo_eff + _future_disch_kwh)
                _soc_ceiling_eff_la = max(lo_eff, hi - _future_charge_kwh)
                # SOC po PLÁNE samotnom (bez RT zásahu) v tejto minúte
                _plan_eg = plan_kw * e_h
                if _plan_eg > 0:
                    _soc_after_plan = soc - _plan_eg / EFFD
                else:
                    _soc_after_plan = soc - _plan_eg * EFFC
                # voľný priestor pre RT zásah (kWh) — koľko RT môže ešte nabiť/vybiť bez porušenia rezervy
                _chg_room_kwh = max(0.0, _soc_ceiling_eff_la - _soc_after_plan)
                _disch_room_kwh = max(0.0, _soc_after_plan - _soc_floor_eff_la)
                # konvertuj na max kW pre 1-min RT akciu
                _max_chg_kw = _chg_room_kwh / max(e_h, 1e-9) * EFFC      # rt_action < 0 = nabíjanie
                _max_disch_kw = _disch_room_kwh / max(e_h, 1e-9) * EFFD  # rt_action > 0 = vybíjanie
                if rt_action > 0:
                    rt_action = min(rt_action, _max_disch_kw)
                else:
                    rt_action = max(rt_action, -_max_chg_kw)
            # ───── PERSISTENCIA: keď systém pretrváva v jednom smere, zníž RT zásah ─────
            if ftv_persistence_throttle and abs(sig) > 1e-3 and str(rt_engine) != "v3":
                _align = avg / max(abs(sig), 1.0)
                _align_pos = max(0.0, min(1.0, _align * _np.sign(sig)))
                _throttle = 1.0 - 0.5 * _align_pos
                rt_action *= _throttle
            # rekomponuj tot
            tot = max(-BK, min(BK, plan_kw + rt_action))
        if (_grid_imp is not None or _grid_exp is not None) and grid_kw_arr is not None:
            pg = float(grid_kw_arr[pidx])                  # nominovaná pozícia do siete (už s FTV)
            dev = tot - plan_kw                            # actual_grid = pg + dev (FTV fixná = predikcia)
            # asymetrické limity: actual_grid ∈ [-grid_imp, +grid_exp]; dev = actual_grid - pg
            _hi = (_grid_exp - pg) if _grid_exp is not None else float("inf")
            _lo = (-_grid_imp - pg) if _grid_imp is not None else float("-inf")
            dev = max(_lo, min(_hi, dev))
            tot = max(-BK, min(BK, plan_kw + dev))
        # ═════════════════════════════════════════════════════════════════════
        # Bug RT-INLINE-AUDIT (2026-06-11): realistic clip + audit DOVNÚTRA loop
        # ═════════════════════════════════════════════════════════════════════
        # User postreh: "vnutorne sa soc vycerpala pricom realne nie". rt_controller
        # interne integroval plný RT zámer cez deň → soc divergovala od reality
        # (= post Bug SOC-FROM-REALISTIC recompute v livesim). V drahých hodinách
        # (napr. 20:00-21:00) rt_controller-internal soc dosiahla soc_min PREDČASNE,
        # SOC clip aktivoval → rt_dir=-1, rt_pct=60 → batt nevybíja plánovaný profit.
        # Fix: clip tot na fyzicky dostupný výkon (avail_for_dis/chg) + audit RT cez
        # audit_capacity PRED zápisom trace. Tým sa internal soc zhoduje s realitou.
        if enforce_realistic and pv_min is not None and load_min is not None:
            try:
                m_idx_re = int((pd.Timestamp(rd.get("time")) - day_start).total_seconds() // 60)
                if 0 <= m_idx_re < pv_min.size and 0 <= m_idx_re < load_min.size:
                    _ftv_re = float(pv_min[m_idx_re])
                    _load_re = float(load_min[m_idx_re])
                    _gi_re = (_grid_imp if _grid_imp is not None else 1e9)
                    _ge_re = (_grid_exp if _grid_exp is not None else 1e9)
                    _avail_chg_re = max(_ftv_re - _load_re, 0.0) + _gi_re
                    _avail_dis_re = _ge_re + max(_load_re - _ftv_re, 0.0)
                    if tot > 0:    # vybi
                        tot = min(tot, _avail_dis_re)
                    elif tot < 0:  # nabi
                        tot = max(tot, -_avail_chg_re)
            except Exception:
                pass
        # Audit per minútu: orež RT zložku ak by porušila SOC rezervu pre budúce sloty
        if audit_today_state is not None and abs(tot - plan_kw) >= 1.0:
            try:
                from core.soc_use_audit import audit_capacity as _cap_audit_in
                si_15 = int(((pd.Timestamp(rd.get("time")) - day_start).total_seconds() // 60) // 15)
                si_15 = max(0, min(95, si_15))
                _rt_int_kw = tot - plan_kw
                _ax = _cap_audit_in(soc/BKWH*100.0, plan_kw, _rt_int_kw,
                                     today_state=audit_today_state,
                                     step_min=15,
                                     soc_reserve_pct=float(audit_soc_reserve_pct or 0.0),
                                     si=si_15,
                                     rt_persistence_slots=int(audit_rt_persistence_slots or 4),
                                     future_horizon_slots=int(audit_future_horizon_slots or 96))
                _scale_in = float(_ax.get("scale_factor", 1.0))
                if _scale_in < 0.999:
                    _rt_new = _rt_int_kw * _scale_in
                    tot = max(-BK, min(BK, plan_kw + _rt_new))
            except Exception:
                pass
        # lo_eff sa už nastavil hore (pred lookahead blokom)
        eg = tot*e_h
        if eg > 0:
            eg = min(eg, max(0.0, (soc-lo_eff))*EFFD)
        elif eg < 0:
            eg = max(eg, -max(0.0, (hi-soc))/EFFC)
        plan_eg = plan_kw*e_h
        dev_eg = eg - plan_eg
        if abs(dev_eg) > dev_budget:
            dev_eg = _np.copysign(dev_budget, dev_eg)
            eg = plan_eg + dev_eg
            eg = max(-BK*e_h, min(BK*e_h, eg))
            if eg > 0:
                eg = min(eg, max(0.0, (soc-lo_eff))*EFFD)
            elif eg < 0:
                eg = max(eg, -max(0.0, (hi-soc))/EFFC)
            dev_eg = eg - plan_eg
        dev_budget = max(0.0, dev_budget - abs(dev_eg))
        if eg > 0:
            soc -= eg/EFFD
        elif eg < 0:
            soc += (-eg)*EFFC
        thru += abs(eg)
        dev_eg_cr = dev_eg
        if (_grid_imp is not None or _grid_exp is not None) and grid_kw_arr is not None:
            # asymetrické cap-clip: actual_grid_kwh ∈ [-grid_imp*e_h, +grid_exp*e_h]
            pgg = float(grid_kw_arr[pidx])
            _hi_e = ((_grid_exp - pgg) * e_h) if _grid_exp is not None else float("inf")
            _lo_e = ((-_grid_imp - pgg) * e_h) if _grid_imp is not None else float("-inf")
            dev_eg_cr = max(_lo_e, min(_hi_e, dev_eg))
        dev_rev = RT_HAIRCUT*float(zco)*dev_eg_cr/1000.0
        cyc_cost = CYCLE*abs(dev_eg_cr)/1000.0/2
        rmin = dev_rev - cyc_cost
        rev += rmin
        if return_trace:
            rows.append(dict(time=rd.get("time"), ts15=rd.get("ts15"), sys_MW=rd.get("sys_MW"),
                             zco_eur=float(zco), dt_eur=float(dtp), mw_sig=float(sig), avg_react=float(avg),
                             band_dis=float(bd_eff), band_chg=float(bc_eff),
                             rt_dir=int(_np.sign(dev_eg_cr)),
                             rt_power_pct=int(round(abs(dev_eg_cr)/e_h/BK*100)) if BK else 0,
                             rt_reason=reason, act_batt_kw=float(eg/e_h),
                             soc_kwh=float(soc), soc_pct=round(soc/BKWH*100, 1),
                             rt_rev_min=float(rmin)))
    cyc_total = thru/2.0/BKWH
    if return_trace:
        return rev, cyc_total, pd.DataFrame(rows)
    return rev, cyc_total


def run_day(g, band_dis, band_chg, w_sys, sys_orient, strong_mw=STRONG_MW,
            budget_kwh=None, soc0=None, dt_bias_k=None, return_cycles=False, return_trace=False):
    soc = (SOC0*BATT_KWH) if soc0 is None else soc0
    budget = (MAX_CYCLES*BATT_KWH) if budget_kwh is None else max(0.0, budget_kwh)
    market = 0.0; cyc = 0.0; thru = 0.0                     # market=toky, cyc=opotrebenie, thru=prietok kWh
    lo, hi = SOC_MIN*BATT_KWH, SOC_MAX*BATT_KWH
    e_min = BATT_KW*(1/60.0)
    dts = g["isot_eur"].dropna()
    day_mean_dt = float(dts.mean()) if len(dts) else 0.0   # denný priemer DT (známy z D-1)
    cur = None; psum = pcount = 0.0
    buf = []; lat = int(RT_LATENCY)                          # latencia: rozhodnutia čakajúce na vykonanie
    run_dir = 0; run_min = 0                                  # smer a dĺžka aktuálneho behu (pre reversal boost)
    trace = [] if return_trace else None
    for r in g.itertuples(index=False):
        rd = r._asdict()
        zco, dtp = rd.get("zco_eur"), rd.get("isot_eur")
        if pd.isna(zco) or pd.isna(dtp):
            continue
        if rd.get("ts15") != cur:
            cur = rd.get("ts15"); psum = pcount = 0.0
        psum += mw_signal(rd, w_sys, sys_orient); pcount += 1
        avg = psum/pcount
        dt_rel = float(dtp) - day_mean_dt
        # REVERSAL BOOST: čím dlhšie beží jeden smer, tým nižší prah pre OPAČNÝ smer (skôr sa preklopí)
        bd_eff, bc_eff = band_dis, band_chg
        if REVERSAL_BOOST > 0 and run_dir != 0:
            boost = REVERSAL_BOOST * min(1.0, run_min/max(1.0, REVERSAL_SCALE))
            if run_dir < 0:                                  # dlho NABÍJAL → ľahšie VYBÍJAŤ
                bd_eff = band_dis * (1.0 - boost)
            else:                                            # dlho VYBÍJAL → ľahšie NABÍJAŤ
                bc_eff = band_chg * (1.0 - boost)
        bd_eff, bc_eff = soc_bias_bands(bd_eff, bc_eff, soc/BATT_KWH*100)   # SOC-citlivé prahy
        dr = decide_reason(rd, avg, bd_eff, bc_eff, strong_mw, dt_rel, dt_bias_k)
        dd = dr[0]                                           # smer rozhodnutia (−1/0/+1)
        if dd != 0:                                          # aktualizuj beh (hold beh nereštartuje)
            run_min = run_min + 1 if dd == run_dir else 1
            run_dir = dd
        buf.append(dr)
        if len(buf) <= lat:
            continue                                         # rozhodnutie ešte „v potrubí" (latencia)
        direction, frac, reason = buf.pop(0)                 # vykonaj rozhodnutie spred `lat` min za AKTUÁLNU cenu
        e = 0.0; mdelta = 0.0; cdelta = 0.0
        if direction > 0 and soc > lo and budget > 0:        # VYBI (predaj do odchýlky za ZCO)
            e = min(e_min*frac, soc-lo, budget)
            mdelta = zco*(e*EFFD)/1000; cdelta = CYCLE*e/1000/2
            market += mdelta; cyc += cdelta; soc -= e; budget -= e; thru += e
        elif direction < 0 and soc < hi:                     # NABI (nákup z odchýlky za ZCO)
            e = min(e_min*frac, hi-soc)
            mdelta = -zco*(e/EFFC)/1000; cdelta = CYCLE*e/1000/2
            market += mdelta; cyc += cdelta; soc += e; thru += e
        else:
            direction = 0                                    # nevykonané (limit SOC/rozpočet) → DRŽ
        if return_trace:
            trace.append(dict(time=rd.get("time"), ts15=rd.get("ts15"),
                              sys_MW=rd.get("sys_MW"), zco_eur=float(zco), dt_eur=float(dtp),
                              mw_sig=float(mw_signal(rd, w_sys, sys_orient)), avg_react=float(avg),
                              band_dis=float(bd_eff), band_chg=float(bc_eff),
                              rt_dir=int(direction), rt_power_pct=int(round(frac*100)) if direction != 0 else 0,
                              rt_reason=reason, e_kwh=float(e),
                              soc_kwh=float(soc), soc_pct=round(soc/BATT_KWH*100, 1),
                              budget_left_kwh=float(budget),
                              rt_rev_min=float(RT_HAIRCUT*mdelta - cdelta)))
    rev = RT_HAIRCUT*market - cyc                            # haircut = zachytená časť teoretického zisku
    if return_trace:
        return rev, thru/2.0/BATT_KWH, pd.DataFrame(trace)
    if return_cycles:
        return rev, thru/2.0/BATT_KWH                        # ekvivalentné plné cykly (prietok/2/kapacita)
    return rev


def run_length_today(lf, band_dis, band_chg, w_sys=PROD_W_SYS, sys_orient=PROD_SYS_ORIENT,
                     dt_bias_k=None):
    """Pre živý poradca: prehrá per-minútové rozhodnutia za dnešok (rovnaká logika ako run_day,
    vrátane reversal-boostu) a vráti AKTUÁLNY (run_dir, run_min) = smer a dĺžku behu [min] do teraz."""
    if lf is None or lf.empty:
        return 0, 0
    g = lf.sort_values("time")
    dts = g["isot_eur"].dropna(); day_mean_dt = float(dts.mean()) if len(dts) else 0.0
    cur = None; psum = pcount = 0.0; run_dir = 0; run_min = 0
    for r in g.itertuples(index=False):
        rd = r._asdict()
        if pd.isna(rd.get("sys_MW")) and all(pd.isna(rd.get(c)) for c in ACT_COLS):
            continue
        if rd.get("ts15") != cur:
            cur = rd.get("ts15"); psum = pcount = 0.0
        psum += mw_signal(rd, w_sys, sys_orient); pcount += 1
        avg = psum/pcount
        dt_rel = (float(rd["isot_eur"]) - day_mean_dt) if pd.notna(rd.get("isot_eur")) else 0.0
        bd, bc = band_dis, band_chg
        if REVERSAL_BOOST > 0 and run_dir != 0:
            boost = REVERSAL_BOOST * min(1.0, run_min/max(1.0, REVERSAL_SCALE))
            if run_dir < 0:
                bd = band_dis * (1.0 - boost)
            else:
                bc = band_chg * (1.0 - boost)
        dd = decide(rd, avg, bd, bc, STRONG_MW, dt_rel, dt_bias_k)[0]
        if dd != 0:
            run_min = run_min + 1 if dd == run_dir else 1
            run_dir = dd
    return run_dir, run_min


def live_decision(period_min, band_dis=PROD_BAND_DIS, band_chg=PROD_BAND_CHG,
                  w_sys=PROD_W_SYS, sys_orient=PROD_SYS_ORIENT, strong_mw=STRONG_MW,
                  dt_rel=0.0, dt_bias_k=None):
    """Pre živý poradca: z minút AKTUÁLNEJ periódy (do teraz) vráti dict s odporúčaním.
    dt_rel = DT aktuálnej periódy − denný priemer DT (posuvná hranica)."""
    if period_min is None or len(period_min) == 0:
        return dict(reco="DRŽ", power_pct=0, avg=float("nan"), last=float("nan"))
    sigs = [mw_signal(r._asdict(), w_sys, sys_orient) for r in period_min.itertuples(index=False)]
    avg = float(np.mean(sigs)) if sigs else 0.0
    last_rd = period_min.iloc[-1].to_dict()
    if "sys_MW" in period_min.columns:                      # gate na PRIEMERNÚ odchýlku okna (čo vidí user)
        last_rd["sys_MW"] = float(pd.to_numeric(period_min["sys_MW"], errors="coerce").mean())
    direction, frac = decide(last_rd, avg, band_dis, band_chg, strong_mw, dt_rel, dt_bias_k)
    reco = "VYBI" if direction > 0 else ("NABI" if direction < 0 else "DRŽ")
    return dict(reco=reco, power_pct=int(round(frac*100)), avg=avg, last=float(sigs[-1]))


def prep(df):
    """Doplní kĺzavý priemer + σ aFRR aktivácie (na detekciu výrazného skoku nad priemer)
    a net aktiváciu + jej kĺzavý priemer (na detekciu FLIP-u smeru ktorejkoľvek služby)."""
    df = df.sort_values("time").copy()
    df["afrr_up_roll"] = df["aFRR_plus"].shift(1).rolling(ROLL_WIN, min_periods=20).mean()
    df["afrr_dn_roll"] = df["aFRR_minus"].shift(1).rolling(ROLL_WIN, min_periods=20).mean()
    df["afrr_up_std"] = df["aFRR_plus"].shift(1).rolling(ROLL_WIN, min_periods=20).std()
    df["afrr_dn_std"] = df["aFRR_minus"].shift(1).rolling(ROLL_WIN, min_periods=20).std()
    up = df["aFRR_plus"] + df.get("mFRR_plus", 0) + df.get("mFRR5", 0)
    dn = df["aFRR_minus"] + df.get("mFRR_minus", 0)
    df["act_net"] = up - dn
    df["act_net_roll"] = df["act_net"].shift(1).rolling(ROLL_WIN, min_periods=20).mean()
    return df


def perfect_day(g, budget_kwh=None, soc0=None):
    """Strop: koná v správnom smere podľa SKUTOČNEJ ZCO (plný výkon, rozpočet)."""
    soc = (SOC0*BATT_KWH) if soc0 is None else soc0
    budget = (MAX_CYCLES*BATT_KWH) if budget_kwh is None else max(0.0, budget_kwh)
    rev = 0.0; lo, hi = SOC_MIN*BATT_KWH, SOC_MAX*BATT_KWH; e_min = BATT_KW*(1/60.0)
    for r in g.itertuples(index=False):
        rd = r._asdict(); zco, dtp = rd.get("zco_eur"), rd.get("isot_eur")
        if pd.isna(zco) or pd.isna(dtp):
            continue
        if zco > dtp and soc > lo and budget > 0:
            e = min(e_min, soc-lo, budget); rev += zco*(e*EFFD)/1000 - CYCLE*e/1000/2; soc -= e; budget -= e
        elif zco < dtp and soc < hi:
            e = min(e_min, hi-soc); rev -= zco*(e/EFFC)/1000 + CYCLE*e/1000/2; soc += e
    return rev


def _ensure_act(df):
    for c in ACT_COLS + ["sys_MW", "zco_eur", "isot_eur"]:
        if c not in df.columns:
            df[c] = np.nan
    return df


def main():
    df = pd.read_csv("out/imbalance_minute.csv", parse_dates=["time", "ts15"])
    df["date"] = df.time.dt.date
    df = _ensure_act(df)
    df = prep(df)                       # kĺzavý priemer aFRR (spike detekcia)
    days = sorted(df.date.unique())

    # orientácia systémovej odchýlky (aby sys_orient*sys malo kladný vzťah so spreadom ZCO-DT)
    per = df.dropna(subset=["zco_eur", "isot_eur"]).drop_duplicates("ts15")
    spread = (per.zco_eur - per.isot_eur).values
    up = per.aFRR_plus.fillna(0)+per.mFRR_plus.fillna(0)+per.mFRR5.fillna(0)
    dn = per.aFRR_minus.fillna(0)+per.mFRR_minus.fillna(0)
    netp = (up-dn).values
    sysp = per.sys_MW.fillna(0).values
    c_net = np.corrcoef(netp, spread)[0, 1] if len(per) > 30 else float("nan")
    c_sys = np.corrcoef(sysp, spread)[0, 1] if len(per) > 30 else float("nan")
    sys_orient = 1.0 if (np.isnan(c_sys) or c_sys >= 0) else -1.0

    print("="*66)
    print("DIAGNOSTIKA – ktorá MW veličina predpovedá spread ZCO-DT:")
    print(f"   net aktivácia (up-dn) ~ spread : {c_net:+.3f}")
    print(f"   systémová odchýlka     ~ spread : {c_sys:+.3f}  → orientácia {sys_orient:+.0f}")
    # event-chvost: berie MW signál ako detektor extrémov
    mag = np.abs(netp) + np.abs(sysp)
    if len(per) > 50:
        thr = np.quantile(mag, 0.90)
        top = mag >= thr
        big_spread = np.abs(spread)
        share = big_spread[top].sum() / big_spread.sum()
        hit = (np.sign(spread[top]) == np.sign((sys_orient*sysp + netp)[top])).mean()
        print(f"   TOP 10% periód podľa veľkosti MW: priemer |spread| {big_spread[top].mean():.0f} € "
              f"(zvyšok {big_spread[~top].mean():.0f} €)")
        print(f"   → tieto 4 % času držia {share:.0%} celkovej veľkosti spreadu | trafený smer {hit:.0%}")
    print("="*66)
    print(f"MINÚTOVÝ RT REGULÁTOR (asymetrické pásmo, w_sys={W_SYS}) – {len(days)} dní")
    print("Scan: VYBÍJACIE pásmo (riadky) × NABÍJACIE pásmo (stĺpce) → € spolu / €deň")
    print("="*66)

    hdr = "  VYBI\\NABI " + "".join(f"{(str(c)+'MW'):>15}" for c in BANDS_CHG)
    print(hdr)
    best = (None, -1e9)
    for bd in BANDS_DIS:
        line = f"{bd:6.0f}MW  "
        for bc in BANDS_CHG:
            tot = sum(run_day(df[df.date == d].sort_values("time"), bd, bc, W_SYS, sys_orient) for d in days)
            line += f"{tot:8.0f}/{tot/max(len(days),1):4.1f}€d"
            if tot > best[1]:
                best = ((bd, bc), tot)
        print(line)
    (bd, bc), btot = best
    print("="*66)
    print(f"NAJLEPŠIE: VYBI pásmo {bd:.0f} MW, NABI pásmo {bc:.0f} MW → {btot:.0f} € spolu | "
          f"{btot/max(len(days),1):.1f} €/deň | ~{btot/max(len(days),1)*30:.0f} €/mes")
    print(f"(w_sys={W_SYS}, W_MFRR={W_MFRR}, STRONG_MW={STRONG_MW}; orientácia sys {sys_orient:+.0f})")

    # KONCENTRÁCIA
    daily = sorted((run_day(df[df.date == d].sort_values("time"), bd, bc, W_SYS, sys_orient) for d in days),
                   reverse=True)
    da = np.array(daily); n = len(da)
    med = float(np.median(da)); pos = int((da > 1).sum()); neg = int((da < -1).sum())
    print("-"*66)
    print("KONCENTRÁCIA (pri najlepšom nastavení):")
    print(f"   najlepší deň {da[0]:.0f} € ({da[0]/btot:.0%} celku) | top 5 dní {da[:5].sum():.0f} € "
          f"({da[:5].sum()/btot:.0%})")
    print(f"   bez najlepšieho dňa {btot-da[0]:.0f} € → {(btot-da[0])/max(n-1,1):.1f} €/deň")
    print(f"   medián dňa {med:.1f} € | ziskových dní {pos}/{n} | stratových {neg}/{n}")
    print("="*66)


if __name__ == "__main__":
    main()
