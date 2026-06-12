"""RT poradca v3 — marginálna hodnota energie v batérii (2026-06-12).

Jediný princíp namiesto veže pravidiel (v2: substitúcia/surplus/cover/kotvy):

    PREDAJ teraz, ak E[ZCO] − cc > hodnota najlepšej BUDÚCEJ predajnej alternatívy
    NÁKUP teraz, ak E[ZCO] + cc < cena najlepšej BUDÚCEJ nákupnej alternatívy

Alternatívy sú OBJEMOVÉ (supply curve): každé budúce okno má cenu (DT periódy)
a kapacitu v kWh (voľný výkon batérie nad committed plánom × trvanie, len v
slotoch kde je RT povolená). Výkon zásahu vyplynie z objemu energie, ktorá má
ešte kladnú maržu — žiadna lineárna rampa margin→f, žiadne f<0.

Nastavenia profilu sú VSTUPOM (user 2026-06-12: "musí zohľadniť nastavenie,
nie vždy sú všetky veci možné — RT, VDT, DT"):
  - rt_allowed[t]   : RT maska per budúca perióda (rt_on96 / template)
  - free_dis_kw[t]  : voľný vybíjací výkon nad |plánom| (0 ak slot plný/blokovaný)
  - free_chg_kw[t]  : voľný nabíjací výkon
  - committed plán  : energiu pre plán v3 NEPREDÁ (rezervu drží need_kwh)
  - allow_grid_chg  : zákaz nabíjania zo siete vynuluje nákupné alternatívy aj akciu

Vrstvy nad týmto: len fyzika (SOC/grid/ledger clipy). Žiadny no-worsen,
persistencia ani lookahead — ich úlohu preberá ocenenie.
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np

__all__ = ["decide_v3"]


def _future_curve(prices: np.ndarray, cap_kwh: np.ndarray, desc: bool):
    """Zotriedené budúce okná: (cena, kapacita kWh). desc=True pre predaj."""
    ok = np.isfinite(prices) & (cap_kwh > 1e-9)
    if not ok.any():
        return np.zeros(0), np.zeros(0)
    pr, cp = prices[ok], cap_kwh[ok]
    order = np.argsort(-pr if desc else pr)
    return pr[order], cp[order]


def _marginal_value(curve_p: np.ndarray, curve_c: np.ndarray, q_kwh: float) -> float:
    """Cena okna, do ktorého by padla q-tá kWh (marginálna alternatíva).
    Za koncom kriviek (energia sa už nikam nezmestí) → -inf pre predaj kriviek
    volá caller cez default."""
    if curve_p.size == 0 or q_kwh <= 0:
        return float("nan")
    cum = np.cumsum(curve_c)
    i = int(np.searchsorted(cum, q_kwh))
    if i >= curve_p.size:
        return float("nan")          # nezmestí sa do žiadneho okna
    return float(curve_p[i])


def decide_v3(zco_exp: float,
              soc_kwh: float, lo_kwh: float, hi_kwh: float,
              batt_kw: float, period_h: float,
              eff_c: float, eff_d: float, cycle_cost: float,
              fut_prices: np.ndarray,        # DT €/MWh per budúca perióda (zvyšok dňa)
              fut_free_dis_kw: np.ndarray,   # voľný vybíjací výkon per perióda (po maske+pláne)
              fut_free_chg_kw: np.ndarray,   # voľný nabíjací výkon per perióda
              fut_plan_dis_kwh: float,       # committed plánovaný predaj zvyšku dňa [kWh na prahu]
              fut_plan_chg_kwh: float,       # committed plánovaný nákup [kWh na prahu]
              margin_min_eur: float = 10.0,
              margin_min_chg_eur: Optional[float] = None,
              allow_grid_charge: bool = True,
              max_step_kwh: Optional[float] = None,
              ) -> Tuple[float, str]:
    """Vráti (rt_kw, reason). rt_kw > 0 = vybíjaj nad plán, < 0 = nabíjaj nad plán.

    Všetky ceny €/MWh, energie kWh, výkony kW. zco_exp = E[ZCO] teraz
    (DT_now + kalibrovaný spread — dodáva rt_engine_v2 kalibrácia).
    """
    eff_c = min(1.0, max(0.5, float(eff_c)))
    eff_d = min(1.0, max(0.5, float(eff_d)))
    cc = max(0.0, float(cycle_cost))
    m_min = float(margin_min_eur)
    m_min_c = float(margin_min_chg_eur) if margin_min_chg_eur is not None else m_min
    soc, lo, hi = float(soc_kwh), float(lo_kwh), float(hi_kwh)

    # ── rezerva pre committed plán: energiu, ktorú plán ešte minie, v3 nechytá ──
    need_kwh = max(0.0, float(fut_plan_dis_kwh) / eff_d
                   - float(fut_plan_chg_kwh) * eff_c)
    sellable_kwh = max(0.0, soc - lo - need_kwh)            # voľná energia na predaj
    room_kwh = max(0.0, hi - soc)                            # voľné miesto na nákup
    # miesto si nárokuje aj committed budúci nákup — nový RT nákup ho nesmie vytlačiť
    room_kwh = max(0.0, room_kwh - float(fut_plan_chg_kwh) * eff_c)

    fp = np.asarray(fut_prices, float)
    cap_dis = np.asarray(fut_free_dis_kw, float).clip(min=0.0) * period_h
    cap_chg = np.asarray(fut_free_chg_kw, float).clip(min=0.0) * period_h
    n = min(fp.size, cap_dis.size, cap_chg.size)
    fp, cap_dis, cap_chg = fp[:n], cap_dis[:n], cap_chg[:n]

    step_kwh = float(batt_kw) * period_h if max_step_kwh is None else float(max_step_kwh)

    # ── PREDAJ teraz? — porovnaj E[ZCO] s marginálnou budúcou predajnou alternatívou ──
    # Budúca alternatíva q-tej kWh: predaj v q-tom najdrahšom ešte voľnom okne.
    # Ak sa energia do okien nezmestí (kriviek je málo), alternatíva = 0 (bez odbytu).
    sell_p, sell_c = _future_curve(fp, cap_dis, desc=True)
    q = 0.0
    sell_now_kwh = 0.0
    lim = min(sellable_kwh, step_kwh)
    while q < lim - 1e-9:
        chunk = min(lim - q, max(1.0, lim / 8.0))
        alt = _marginal_value(sell_p, sell_c, q + chunk)
        alt_eff = 0.0 if alt != alt else alt                # NaN → bez odbytu → 0
        margin = (zco_exp - cc) - alt_eff
        if margin < m_min:
            break
        sell_now_kwh = q + chunk
        q += chunk
    if sell_now_kwh > 1e-6:
        kw = min(float(batt_kw), sell_now_kwh / max(period_h, 1e-9))
        alt0 = _marginal_value(sell_p, sell_c, max(sell_now_kwh, 1e-6))
        return kw, (f"v3:dis q={sell_now_kwh:.0f}kWh zco={zco_exp:.0f}"
                    f" alt={0.0 if alt0 != alt0 else alt0:.0f}")

    # ── NÁKUP teraz? — porovnaj E[ZCO] s marginálnou budúcou nákupnou alternatívou ──
    if allow_grid_charge and room_kwh > 1e-6:
        buy_p, buy_c = _future_curve(fp, cap_chg, desc=False)
        q = 0.0
        buy_now_kwh = 0.0
        lim = min(room_kwh, step_kwh)
        while q < lim - 1e-9:
            chunk = min(lim - q, max(1.0, lim / 8.0))
            alt = _marginal_value(buy_p, buy_c, q + chunk)
            if alt != alt:                                   # žiadne budúce okno
                # energia kúpená teraz sa musí dať aspoň predať v budúcom okne
                resale = _marginal_value(sell_p, sell_c, q + chunk)
                if resale != resale:
                    break
                margin = eff_c * eff_d * resale - (zco_exp + cc)
            else:
                margin = alt - (zco_exp + cc)                # kúp teraz namiesto neskôr
                # nákup "navyše" (nie substitúcia) má zmysel len ak ho predaj unesie
                resale = _marginal_value(sell_p, sell_c,
                                         need_kwh + q + chunk)
                if resale == resale:
                    margin = max(margin, eff_c * eff_d * resale - (zco_exp + cc))
            if margin < m_min_c:
                break
            buy_now_kwh = q + chunk
            q += chunk
        if buy_now_kwh > 1e-6:
            kw = min(float(batt_kw), buy_now_kwh / max(period_h, 1e-9))
            return -kw, (f"v3:chg q={buy_now_kwh:.0f}kWh zco={zco_exp:.0f}")

    return 0.0, f"v3:idle zco={zco_exp:.0f}"
