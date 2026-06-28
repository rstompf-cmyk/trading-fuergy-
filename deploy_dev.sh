#!/bin/bash
# DEV-only deploy: commit + push origin dev (BEZ FF na refactor-v2/prod). Rebuild 8001.
# Obsah: manager dashboard v2 (full-width, 1 profil/riadok, čísla vľavo + graf vpravo,
#  TERAZ marker, navigácia času deň/mesiac/picker/rozsah), SOC-leak fix (load_series/paths
#  per-profil), klik názov→/livesim graf→/dentrh, VDT pairs engine, SOC-CARRY/DRIFT, globálny
#  full-width .container.
set -e
cd "$(cd "$(dirname "$0")" && pwd)"
rm -f .git/index.lock
echo "=== branch ==="; cat .git/HEAD
echo "=== HEAD pred ==="; git log --oneline -1
git add app.py livesim.py vdt_state.py vdt_optimizer.py vdt_live_advisor.py vdt_pair_matcher.py \
        optimizer.py core/effect_db.py core/rt_audit.py core/caches.py core/holidays_skcz.py core/feasibility.py core/soc_source.py scheduler.py price_model_15m.py price_model.py d1_planner.py cdc.py report.py \
        auto_control.py cdc_reg_plan.py vdt_extras.py daily_settlement.py combined_backtest.py vdt_soc_feasible.py data_sources.py \
        profiles.py core/schemas/profile.py templates/pages/profiles_edit.html \
        tests/test_vdt_soc_feasible.py tests/test_holidays_skcz.py tests/test_feasibility_parity.py tests/test_soc_source.py tests/test_feasibility_gate_extras.py \
        docker-compose.yml static/css/app.css templates/components/nav.html templates/base.html \
        templates/pages/plans_list.html ui/html.py ui/templates.py \
        tools/diag_trades.py tools/export_livesim_xlsx.py tests/test_vdt_pair_matcher.py \
        scripts/upgrade_dev.ps1 deploy_dev.sh 2>/dev/null || true
git add -f out/price_model_15m.joblib 2>/dev/null || true
git commit -m "dev: SOC-CURRENT-FIX (kriticke) - current_engine_soc berie POSLEDNY REALIZOVANY SOC (batt_kw_realistic not NaN, zoradene casom), nie 'posledny <= now' co chytal PLANOVU PROJEKCIU (100%) -> trade-control phantom plna baterka pri realnych ~5% -> advisor idle/zle. + feasibility-unify KROK 1/2/3 (gate+gate_extras default ON). Golden 5/5 + testy. POZOR: dnesny trace WV_4 poskodeny (75 realiz + konfliktna projekcia) -> po deploy CLEAN re-sim." || echo "(nic nove)"
echo "=== HEAD po ==="; git log --oneline -1
git push origin dev
echo "=== origin/dev ==="; git log --oneline -1 origin/dev
echo ">>> HOTOVO (Mac). Na WINDOWS: .\\scripts\\upgrade_dev.ps1  (rebuild 8001)"
echo "    Potom hard-refresh (Ctrl+F5) a otvor http://localhost:8001/manager"
