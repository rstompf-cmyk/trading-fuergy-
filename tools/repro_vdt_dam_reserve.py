# -*- coding: utf-8 -*-
"""repro_vdt_dam_reserve.py — #28 izolovaná reprodukcia.

Otázka: keď DAM plán vybíja večer batériu (takmer) na min, ZAMIETNE / DOWNSCALE
audit_action navrhnutý VDT OTVÁRACÍ PREDAJ (discharge) skôr cez deň, ktorý by
zožral SOC potrebný pre DAM večerný záväzok? (VDT SOC rezervácia).

Test 3 scenáre × 2 audit módy (absolute vs no-worsen):
  A) DAM baseline FEASIBILNÝ (končí presne na min), VDT sell skôr → MUSÍ downscale/reject
  B) DAM baseline TESNE feasibilný, malý VDT sell v slacku → smie prejsť
  C) DAM baseline UŽ infeasibilný (pod min večer), VDT sell ktorý to zhorší → reject
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core.soc_use_audit as sua


def _state(start_pct, dam):
    return {"ok": True, "batt_kwh": 1000.0, "eff_c": 0.95, "eff_d": 0.95,
            "soc_min_pct": 5.0, "soc_max_pct": 100.0, "soc_reserve_pct": 0.0,
            "current_soc_pct": start_pct, "start_soc_pct": start_pct,
            "dam_nomination_kwh": dam, "vdt_realized_kwh": [0.0] * 96}


def _dam_evening_discharge(total_kwh, n_slots=8, start_slot=80):
    """DAM vybíja total_kwh rozložene do večerných slotov [start_slot, start_slot+n)."""
    d = [0.0] * 96
    per = total_kwh / n_slots
    for i in range(start_slot, start_slot + n_slots):
        d[i] = per          # + = discharge (batt-view)
    return d


def run(mode):
    os.environ["VDT_AUDIT_NOWORSEN"] = "1" if mode == "noworsen" else "0"
    print(f"\n===== AUDIT MÓD: {mode} =====")

    # SOC 50% = 500 kWh nad min(5%=50) → použiteľných ~450 kWh (÷eff_d ~ 428 dodá).
    # DAM večer vybíja 420 kWh → baseline končí tesne nad min (feasibilný).
    start = 50.0

    # A) VDT open-sell 200 kWh o 10:00 (slot 40) — spolu s DAM 420 večer to pretlačí pod min
    stA = _state(start, _dam_evening_discharge(420.0))
    rA = sua.audit_action("t", "2026-07-01", 40, "discharge", 200.0,
                          source="vdt", today_state=stA,
                          current_soc_pct_at_si=start)
    print(f"A) DAM 420 večer + VDT sell 200@10:00 → {rA['decision']} "
          f"(allowed={rA.get('allowed_kwh')}) {rA.get('reason','')[:80]}")

    # B) malý VDT sell 20 kWh — má sa zmestiť do slacku
    stB = _state(start, _dam_evening_discharge(300.0))   # menší DAM záväzok = viac slacku
    rB = sua.audit_action("t", "2026-07-01", 40, "discharge", 20.0,
                          source="vdt", today_state=stB,
                          current_soc_pct_at_si=start)
    print(f"B) DAM 300 večer + VDT sell 20@10:00  → {rB['decision']} "
          f"(allowed={rB.get('allowed_kwh')})")

    # C) baseline UŽ infeasibilný: DAM 700 večer (viac než SOC dovolí) → pod min
    stC = _state(start, _dam_evening_discharge(700.0))
    rC = sua.audit_action("t", "2026-07-01", 40, "discharge", 200.0,
                          source="vdt", today_state=stC,
                          current_soc_pct_at_si=start)
    print(f"C) DAM 700 večer (infeas) + VDT sell 200@10:00 → {rC['decision']} "
          f"(allowed={rC.get('allowed_kwh')}) {rC.get('reason','')[:80]}")
    return rA, rB, rC


def run_realsoc_leak():
    """DIERA #28: plán štartuje deň na 50 %, ale REALITA je teraz (slot 40) na 20 %.
    DAM večer vybíja 420 kWh — feasibilné z 50 %, ALE NIE z 20 %. VDT sell 100@10:00.
    - BEZ reálneho SOC (dnešný stav auditu): simuluje od 00:00 z 50 % → myslí že OK → ACCEPT.
    - S reálnym SOC (fix): seed 20 % v slote 40 → večer pod min → REJECT/DOWNSCALE.
    """
    os.environ["VDT_AUDIT_NOWORSEN"] = "1"
    print("\n===== DIERA: reálny SOC (20%) < plán štart (50%) =====")
    st = _state(50.0, _dam_evening_discharge(420.0))
    st["current_soc_pct"] = 20.0            # realita teraz
    # BEZ reálneho SOC — tak ako to volá append_paper_trade DNES
    r_now = sua.audit_action("t", "2026-07-01", 40, "discharge", 100.0,
                             source="vdt", today_state=st)
    print(f"  DNES (bez real SOC, seed=start 50%)  → {r_now['decision']} "
          f"(allowed={r_now.get('allowed_kwh')})")
    # S reálnym SOC v slote 40 (fix)
    r_fix = sua.audit_action("t", "2026-07-01", 40, "discharge", 100.0,
                             source="vdt", today_state=st,
                             current_soc_pct_at_si=20.0)
    print(f"  FIX  (real SOC 20% seed v slot 40)   → {r_fix['decision']} "
          f"(allowed={r_fix.get('allowed_kwh')}) {r_fix.get('reason','')[:70]}")


if __name__ == "__main__":
    run("absolute")
    run("noworsen")
    run_realsoc_leak()
