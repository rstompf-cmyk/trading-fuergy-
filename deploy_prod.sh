#!/bin/bash
# PROD deploy: dev -> refactor-v2 (FF) push. Prod (8000) bezi z refactor-v2.
# Predpoklad: 15-min sada uz je commitnuta na dev (cez deploy_vdt_fix.sh).
# Tento skript len pridá prod upgrade skript + FF-pushne dev do refactor-v2.
set -e
cd "$(cd "$(dirname "$0")" && pwd)"
rm -f .git/index.lock
echo "=== branch (musi byt dev) ==="; cat .git/HEAD
echo "=== HEAD dev ==="; git log --oneline -1
# pridaj prod upgrade skript na dev ak este nie je commitnuty
git add scripts/upgrade_prod.ps1 deploy_prod.sh 2>/dev/null || true
git commit -m "prod: upgrade_prod.ps1 (refactor-v2 rebuild + 15-min model + re-sim reminder)" || echo "(nic nove na commit)"
git push origin dev
echo "=== FF push dev -> refactor-v2 ==="
git push origin dev:refactor-v2
echo "=== origin/refactor-v2 ==="; git log --oneline -1 origin/refactor-v2
echo ">>> HOTOVO (Mac). Teraz na WINDOWS (z korena repa):"
echo "      .\\scripts\\upgrade_prod.ps1"
echo "    (git pull refactor-v2 + rebuild 8000 + 15-min model + verify)"
echo "    POTOM: pre kazdy zivy profil FULL prepocet (plan_batch full reset / re-sim)."
echo "    POZOR: na PRODE sa 15-min stane primarnym (default mod + plan_batch default 15)."
