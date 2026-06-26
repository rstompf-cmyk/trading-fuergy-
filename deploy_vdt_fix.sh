#!/bin/bash
# 15-min sada:
#  (1) /plan_batch vynuti kind<->step (koniec "nesulad step_min=15 a kind=plan").
#  (2) DEDIKOVANY 15-min cenovy model (price_model_15m.py): vnutrohodinovy tvar pre D+1 (OOS +24%).
#  (3) BUG 15-MIN-DTPROF FIX (livesim.py): _decompose_dtprof miesalo kW (ch/di) s kWh (ex/im)
#      -> pri 15-min bol bateriovy DT clen ~4x nafuknuty. Fix: ch/di -> kWh/slot cez dt.
#      Overene: NOVY 15-min DT efekt == LP net; golden 5/5 (60-min dt=1 = no-op).
set -e
cd "$(cd "$(dirname "$0")" && pwd)"
rm -f .git/index.lock
echo "=== HEAD pred ==="; git log --oneline -1
echo "=== branch ==="; cat .git/HEAD
git add app.py livesim.py optimizer.py vdt_state.py vdt_optimizer.py vdt_live_advisor.py core/effect_db.py core/rt_audit.py price_model_15m.py scheduler.py tools/export_livesim_xlsx.py tools/diag_trades.py scripts/upgrade_dev.ps1
git add -f out/price_model_15m.joblib   # model pribalime do gitu (out/ je gitignored) - Windows nema historian na trening
git commit -m "perf+vdt: AUDIT-RESERVE-PARAM (audit_rt_slot dostane soc_reserve_pct -> koniec load_profile SQLite query NA MINUTU v RT-PRE-AUDIT, ~1.5M dotazov/batch prec; bit-exact, golden 5/5) + effect_db upsert_minute_batch vektorizovany (iterrows->to_dict, tz konverzia raz) + optimizer _DIAG_BUDGET cap (infeasible diag nesposobi LP explóziu) + VDT residual selloff cost_basis guard (<=0 -> selloff OFF, bezpecne); golden 5/5"
echo "=== HEAD po ==="; git log --oneline -1
git push origin dev
echo "=== origin/dev ==="; git log --oneline -1 origin/dev
echo ">>> HOTOVO (Mac). Teraz na WINDOWS (z korena repa) spusti:"
echo "      .\\scripts\\upgrade_dev.ps1"
echo "    (spravi: git pull dev + rebuild 8001 + 15-min model + verify)"
echo "    POTOM: v /livesim pre profil FULL prepocet (plan_batch full reset / re-sim),"
echo "    aby sa effect_db prepocital z opraveneho DT (stare karty maju 4x z cache)."
