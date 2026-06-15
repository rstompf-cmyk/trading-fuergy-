"""VDT kapacitná poistka (2026-06-13).

Bug VDT-CAPACITY (user: "pred uzavretím nákupu a predaja musí prebehnúť simulácia
SOC aj s toleranciou; ak niekde prekročí, musí sa upraviť a až potom uzavrieť").

Problém: VDT obchody sa uzatvárajú bez spoločnej SOC-feasibility kontroly voči PLNEJ
trajektórii DAM plánu + už uzavretým VDT. Plánovač beží každých 15 min, obchody sa
kumulujú, a nikde sa neoverí, že KOMBINOVANÁ SOC krivka ostane v [min, max] v každom
slote. Dôsledok (overené na VW_simulacia_3 06-13): DAM+VDT spolu SOC −11.8 %..+158 %.

Riešenie: `clip_extras_to_capacity` forward-simuluje kombinovanú SOC (DAM committed +
VDT extras) a oreže každý extra tak, aby SOC ostala v [eff_min, eff_max] vo VŠETKÝCH
budúcich slotoch (nie len v slote obchodu — discharge teraz nesmie vyprázdniť SOC
potrebnú pre neskorší committed DAM výdaj; symetricky charge).
"""
from __future__ import annotations

from typing import Dict, List, Tuple

__all__ = ["clip_extras_to_capacity", "simulate_combined_soc", "clip_extras_to_grid"]


def clip_extras_to_grid(extras: Dict[int, Tuple[str, float]],
                        dam_grid_kwh: List[float],
                        grid_import_kwh: float,
                        grid_export_kwh: float) -> Tuple[Dict[int, Tuple[str, float]], List[dict]]:
    """Bug VDT-PENALTY (Koreň 2, 2026-06-15): oreže VDT extras tak, aby KOMBINOVANÁ grid
    pozícia (DAM grid + VDT extra) ostala v [−grid_import, +grid_export] v každom slote.

    Prečo: VDT poistka predtým kontrolovala iba SOC, nie grid prípojku. Po GRID-LIMIT-REALITY
    engine reálnu dodávku oreže na grid limit → ak VDT nominoval nad limit, nominované > dodané
    → dev_kwh ≠ 0 → POKUTA cez ZCO. Týmto orežeme nomináciu na to, čo je fyzicky dodateľné.

    dam_grid_kwh: D-1 grid pozícia per slot (kWh, + = export/dodávka, − = import/odber);
                  už zahŕňa FTV→export aj load→import + DAM batt. VDT extra ju len posúva.
    extras[t] = ('BUY'|'SELL', kwh): SELL (batt discharge) zvyšuje export; BUY (charge) import.
    grid_import_kwh / grid_export_kwh: limit prípojky za 1 slot (kW × dĺžka slotu v h).

    Vracia (orezané_extras, report).
    """
    work = dict(extras)
    report: List[dict] = []
    n = len(dam_grid_kwh)
    gimp = abs(float(grid_import_kwh))
    gexp = abs(float(grid_export_kwh))
    for t in sorted(work.keys()):
        if t >= n:
            continue
        direction, kwh0 = work[t]
        kwh0 = float(kwh0)
        if kwh0 <= 0.0:
            continue
        base = float(dam_grid_kwh[t])                       # DAM grid pozícia (export +)
        is_sell = str(direction).upper() in ("SELL", "DISCHARGE")
        if is_sell:
            # SELL zvyšuje export: base + kwh ≤ +grid_export
            max_kwh = max(0.0, gexp - base)
        else:
            # BUY zvyšuje import (znižuje export): base − kwh ≥ −grid_import
            max_kwh = max(0.0, base + gimp)
        clip = min(kwh0, max_kwh)
        if clip < kwh0 - 1e-6:
            report.append({"slot": t, "direction": direction,
                           "orig_kwh": round(kwh0, 1), "clip_kwh": round(clip, 1),
                           "reason": ("sell→grid_export" if is_sell else "buy→grid_import")})
        if clip <= 1e-6:
            work.pop(t, None)
        else:
            work[t] = (direction, clip)
    return work, report


def simulate_combined_soc(soc_start_kwh: float,
                          dam_charge_kwh: List[float],
                          dam_discharge_kwh: List[float],
                          extras: Dict[int, Tuple[str, float]],
                          eff_c: float, eff_d: float) -> List[float]:
    """SOC (kWh) po každom slote pre DAM + VDT extras. extras[t]=('BUY'|'SELL', kwh)."""
    n = max(len(dam_charge_kwh), len(dam_discharge_kwh))
    soc = float(soc_start_kwh)
    out = []
    for t in range(n):
        c = float(dam_charge_kwh[t]) if t < len(dam_charge_kwh) else 0.0
        d = float(dam_discharge_kwh[t]) if t < len(dam_discharge_kwh) else 0.0
        ex = extras.get(t)
        if ex:
            direction, kwh = ex
            if str(direction).upper() in ("BUY", "CHARGE"):
                c += float(kwh)
            else:
                d += float(kwh)
        soc += c * eff_c - d / max(eff_d, 0.01)
        out.append(soc)
    return out


def clip_extras_to_capacity(soc_start_kwh: float,
                            dam_charge_kwh: List[float],
                            dam_discharge_kwh: List[float],
                            extras: Dict[int, Tuple[str, float]],
                            batt_kwh: float,
                            eff_c: float = 0.95, eff_d: float = 0.95,
                            soc_min_pct: float = 5.0, soc_max_pct: float = 100.0,
                            reserve_pct: float = 0.0
                            ) -> Tuple[Dict[int, Tuple[str, float]], List[dict]]:
    """Oreže VDT extras tak, aby kombinovaná SOC (DAM + extras) ostala v efektívnom
    pásme [soc_min+reserve, soc_max−reserve] vo VŠETKÝCH slotoch.

    Vracia (orezané_extras, report). Report = zoznam zmien {slot, orig_kwh, clip_kwh, dôvod}.

    Postup: sloty spracuj v poradí; pre každý extra spočítaj forward trajektóriu
    s ostatnými už-orezanými extras a clipni jeho kWh na maximum, ktoré nikde
    nepretlačí pásmo. Discharge je limitovaný NAJNIŽŠÍM budúcim bodom, charge
    NAJVYŠŠÍM (rovnaký princíp ako audit_capacity forward trajektória).
    """
    eff_min = (float(soc_min_pct) + float(reserve_pct)) / 100.0 * batt_kwh
    eff_max = (float(soc_max_pct) - float(reserve_pct)) / 100.0 * batt_kwh
    work = dict(extras)
    report: List[dict] = []

    for t in sorted(work.keys()):
        direction, kwh0 = work[t]
        kwh0 = float(kwh0)
        if kwh0 <= 0.0:
            continue
        # trajektória BEZ tohto extra (ostatné orezané ostávajú)
        probe = dict(work); probe.pop(t, None)
        base = simulate_combined_soc(soc_start_kwh, dam_charge_kwh, dam_discharge_kwh,
                                     probe, eff_c, eff_d)
        n = len(base)
        if t >= n:
            continue
        is_charge = str(direction).upper() in ("BUY", "CHARGE")
        # efekt 1 kWh extra na SOC vo VŠETKÝCH slotoch ≥ t:
        #   charge: +eff_c na každý slot ≥ t
        #   discharge: −1/eff_d na každý slot ≥ t
        if is_charge:
            # najvyšší budúci bod + kwh*eff_c ≤ eff_max
            head = eff_max - max(base[i] for i in range(t, n))
            max_kwh = max(0.0, head / max(eff_c, 0.01))
        else:
            # najnižší budúci bod − kwh/eff_d ≥ eff_min
            head = min(base[i] for i in range(t, n)) - eff_min
            max_kwh = max(0.0, head * eff_d)
        clip = min(kwh0, max_kwh)
        if clip < kwh0 - 1e-6:
            report.append({"slot": t, "direction": direction,
                           "orig_kwh": round(kwh0, 1), "clip_kwh": round(clip, 1),
                           "reason": ("charge→soc_max" if is_charge else "discharge→soc_min")})
        if clip <= 1e-6:
            work.pop(t, None)
        else:
            work[t] = (direction, clip)
    return work, report
