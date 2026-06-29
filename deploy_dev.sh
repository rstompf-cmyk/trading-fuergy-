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
git commit -m "dev: REAL-STATE-FALLBACK - compute_current_state: ked realizovany SOC nedostupny (skoro rano, livesim dnes nebezal), obchodnik berie START dna (carryover, posledny realny), NIE planovu projekciu soc_path[cur_idx] (predpoklada nabijanie -> fiktivny SOC -> neparove nakupy). User rozhodnutie: obchodnik berie skutocny stav. Plan immutable. Golden+testy zelene. + SOC-DISPLAY-FIX - (A) current_engine_soc berie PRVE engine meta today_soc_pct (autoritativny aktualny real SOC), az potom trace (batt_kw_realistic je riedke/zastarava). (B2) livesim projekcia SOC seeduje z posledneho REALIZOVANEHO soc_pct -> koniec 5% skoku v hlavicke. Realny SOC bol 100% (plna), 5% v hlavicke bol display bug (projekcia z day-start). Trade-control bol spravny. + feasibility-unify KROK1/2/3. Golden 5/5. Po deploy CLEAN re-sim WV_4." || echo "(nic nove)"
echo "=== HEAD po ==="; git log --oneline -1
git push origin dev
echo "=== origin/dev ==="; git log --oneline -1 origin/dev
echo ">>> HOTOVO (Mac). Na WINDOWS: .\\scripts\\upgrade_dev.ps1  (rebuild 8001)"
echo "    Potom hard-refresh (Ctrl+F5) a otvor http://localhost:8001/manager"
