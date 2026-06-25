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
        optimizer.py core/effect_db.py core/rt_audit.py scheduler.py price_model_15m.py d1_planner.py cdc.py \
        docker-compose.yml static/css/app.css templates/components/nav.html templates/base.html \
        ui/html.py ui/templates.py \
        tools/diag_trades.py tools/export_livesim_xlsx.py tests/test_vdt_pair_matcher.py \
        scripts/upgrade_dev.ps1 deploy_dev.sh 2>/dev/null || true
git add -f out/price_model_15m.joblib 2>/dev/null || true
git commit -m "dev: UI redizajn vlna 1 - zoskupene rozbalovacie menu (5 skupin) mode-aware (sim/real filter), CSS dizajn systém (.card hlavicka/telo, .kpi, nav dropdown) + TRACE-DB _trace_from_db 39 stlpcov (graf z DB bez rt_dir padu)" || echo "(nic nove)"
echo "=== HEAD po ==="; git log --oneline -1
git push origin dev
echo "=== origin/dev ==="; git log --oneline -1 origin/dev
echo ">>> HOTOVO (Mac). Na WINDOWS: .\\scripts\\upgrade_dev.ps1  (rebuild 8001)"
echo "    Potom hard-refresh (Ctrl+F5) a otvor http://localhost:8001/manager"
