#!/bin/bash
# PROD deploy: commit VSETKYCH poslednych zmien na dev + FF push dev -> refactor-v2.
# Prod (8000) bezi z refactor-v2. Samostatny: aj keby nebezal deploy_vdt_fix.sh, commitne
# kompletny balik. Idempotentny (ak uz commitnute -> "nic nove na commit").
set -e
cd "$(cd "$(dirname "$0")" && pwd)"
rm -f .git/index.lock
echo "=== branch (musi byt dev) ==="; cat .git/HEAD
echo "=== HEAD dev pred ==="; git log --oneline -1
# Kompletny balik poslednych zmien (15-min merge + cenovy model + auto-retrain + LP fixy +
# DTPROF fix + cache-wipe-on-switch-2 + Excel + deploy skripty).
git add app.py livesim.py optimizer.py vdt_state.py vdt_optimizer.py vdt_live_advisor.py \
        vdt_pair_matcher.py core/effect_db.py core/rt_audit.py core/caches.py core/holidays_skcz.py core/feasibility.py core/soc_source.py scheduler.py price_model_15m.py price_model.py \
        d1_planner.py cdc.py report.py auto_control.py cdc_reg_plan.py vdt_extras.py daily_settlement.py combined_backtest.py vdt_soc_feasible.py data_sources.py \
        profiles.py core/schemas/profile.py templates/pages/profiles_edit.html \
        tests/test_vdt_soc_feasible.py tests/test_holidays_skcz.py tests/test_feasibility_parity.py tests/test_soc_source.py tests/test_feasibility_gate_extras.py \
        docker-compose.yml static/css/app.css \
        templates/components/nav.html templates/base.html templates/pages/plans_list.html ui/html.py ui/templates.py \
        tools/export_livesim_xlsx.py tools/diag_trades.py tests/test_vdt_pair_matcher.py \
        scripts/upgrade_dev.ps1 scripts/upgrade_prod.ps1 deploy_dev.sh deploy_prod.sh 2>/dev/null || true
git add -f out/price_model_15m.joblib 2>/dev/null || true   # pribalit model (Windows nema historian na trening)
git commit -m "PROD: PLAN-SOURCE-FIX - realny profil (mode=real) -> plan_source VZDY 'dentrh' (realny Denny trh 15-min, nie predikcia) + PLAN-KIND-GUARD: _gen_one_plan vynuti kind podla profiloveho plan_source (dentrh profil->dentrh, predicted->plan) -> neda sa vyrobit nespravny typ planu. + SOC fixy (A meta-first reader, B2 proj seed, REAL-STATE fallback, uprimny SOC label) + FEASIBILITY-UNIFY KROK 1+2+3 - core/feasibility.py (battery_step+gate+gate_extras = single source fyziky baterie) + core/soc_source.py (kanonicky SOC reader, vdt_state deleguje). KROK 3 DEFAULT ON: advisor VDT clip cez jednu branu gate_extras (SOC^grid^vykon z 1 realneho SOC) namiesto 3 prekryvajucich poistiek (overene DEV VW_3/4). Kill-switch VDT_FEASIBILITY_UNIFIED=0. Golden 5/5 + 122 testov." || echo "(nic nove na commit)"
git push origin dev
echo "=== FF push dev -> refactor-v2 ==="
git push origin dev:refactor-v2
echo "=== origin/refactor-v2 ==="; git log --oneline -1 origin/refactor-v2
echo ">>> HOTOVO (Mac). Teraz na WINDOWS (z korena repa):"
echo "      .\\scripts\\upgrade_prod.ps1"
echo "    (git pull refactor-v2 + rebuild 8000 + 15-min model + verify)"
echo "    POTOM: pre kazdy zivy profil FULL prepocet (plan_batch full reset / re-sim)."
echo "    POZOR: na PRODE sa 15-min stane primarnym (default mod + plan_batch default 15)."
