#!/bin/bash
# SOC-CARRY-CASE fix (2026-06-20):
#  15-min vetva (_gen_one_plan + /dentrh POST) volala _resolve_soc_init_carryover s
#  case="dentrh", ale livesim 15-min beží pod MODES kľúčom "dt_15min". carried_soc_for_date
#  preto nenašlo meta → manual fallback soc_init=5% → plán štartoval na 5% napriek carried
#  SOC → trvalá SOC diskontinuita → SOC-CONT-V3 auto-regen donekonečna (regen tiež zlý case
#  → 5% sa nikdy neopravil) → cache -1.0 → banner "prepočítava sa" + iné čísla pri každom
#  prepnutí profilu. Fix: case="dt_15min" na oboch miestach. (60-min plan_d1 bol vždy OK.)
set -e
cd "$(cd "$(dirname "$0")" && pwd)"
rm -f .git/index.lock
echo "=== HEAD pred ==="; git log --oneline -1
echo "=== branch ==="; cat .git/HEAD
git add app.py deploy_prod.sh
git commit -m "fix SOC-DRIFT-CHECK: _find_stale_future_plans porovnaval carried voci schedule.soc_pct[0] (=SOC PO slote 0, nie start dna) -> trvaly falosny drift -> SOC-CONT-V3 auto-regen loop -> 'prepocitava sa' banner kazde prepnutie profilu. Fix: plan uklada params.soc_init_used (realne pouzity carried), drift-check porovnava jeho. + (uz nasadene) SOC-CARRY-CASE 15-min case=dt_15min. golden 5/5"
echo "=== HEAD po ==="; git log --oneline -1
git push origin dev
echo "=== origin/dev ==="; git log --oneline -1 origin/dev
echo ">>> HOTOVO (Mac). Teraz na WINDOWS: .\\scripts\\upgrade_dev.ps1"
echo "    Po rebuilde: prepni profil 1-2x. Auto-regen sa spusti EŠTE RAZ na profil"
echo "    (prepise 06-20 plan na spravny carried SOC), potom uz banner zmizne (drift==0)."
echo "    Over: docker logs trading-fuergy-dev --tail 40 | findstr SOC-CONT-V3   (ma utichnut)"
