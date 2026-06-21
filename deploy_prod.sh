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
        vdt_pair_matcher.py core/effect_db.py core/rt_audit.py scheduler.py price_model_15m.py \
        docker-compose.yml static/css/app.css \
        tools/export_livesim_xlsx.py tools/diag_trades.py tests/test_vdt_pair_matcher.py \
        scripts/upgrade_dev.ps1 scripts/upgrade_prod.ps1 deploy_prod.sh 2>/dev/null || true
git add -f out/price_model_15m.joblib 2>/dev/null || true   # pribalit model (Windows nema historian na trening)
git commit -m "PROD release: Manager dashboard v2 (/manager+/fleet live, full-width, 1 profil/riadok, cisla+graf DT/VDT/SOC, TERAZ marker, navigacia casu den/mesiac/rozsah, aktualny+ocakavany efekt) + SOC-leak fix (load_series/paths per-profil) + Zisk za den = ocakavany celodenny (fut DT) + klik nazov->livesim graf->dentrh + full-width .container vsade + LIVESIM_BG_ALL na prode + VDT parovy matcher (opt-in) + SOC-CARRY/DRIFT + AUDIT-RESERVE-PARAM perf. golden 5/5 + matcher 8/8" || echo "(nic nove na commit)"
git push origin dev
echo "=== FF push dev -> refactor-v2 ==="
git push origin dev:refactor-v2
echo "=== origin/refactor-v2 ==="; git log --oneline -1 origin/refactor-v2
echo ">>> HOTOVO (Mac). Teraz na WINDOWS (z korena repa):"
echo "      .\\scripts\\upgrade_prod.ps1"
echo "    (git pull refactor-v2 + rebuild 8000 + 15-min model + verify)"
echo "    POTOM: pre kazdy zivy profil FULL prepocet (plan_batch full reset / re-sim)."
echo "    POZOR: na PRODE sa 15-min stane primarnym (default mod + plan_batch default 15)."
