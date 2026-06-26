#!/bin/bash
# VDT PÁROVÝ MATCHER (engine="pairs") — 2026-06-20:
#  Nová VDT logika ako párové cykly (greedy): nájdi nákup↔predaj pár so spreadom ≥
#  breakeven+min_spread, oba smery (buy→sell aj sell→buyback), priorita closest/profit/
#  balanced, opakuj kým SOC[min+rez,max−rez] a výkon batérie dovolí. Poplatok LEN na
#  nabíjaní (import). Žiadne nepárové nákupy → koniec stratových večerných nákupov.
#  Aktivácia per-profil: plan.vdt_engine="pairs" + plan.vdt_pair_priority=closest|profit|balanced.
#  LP cesta (golden) NEDOTKNUTÁ — pairs je opt-in early-return. golden 5/5 + matcher 8/8.
set -e
cd "$(cd "$(dirname "$0")" && pwd)"
rm -f .git/index.lock
echo "=== HEAD pred ==="; git log --oneline -1
echo "=== branch ==="; cat .git/HEAD
git add vdt_pair_matcher.py vdt_optimizer.py vdt_live_advisor.py app.py tests/test_vdt_pair_matcher.py
git commit -m "VDT pairs engine: greedy parovy matcher (vdt_pair_matcher.py) + optimize_vdt_day engine=pairs early-return (LP/golden nedotknuty) + advisor cita plan.vdt_engine/vdt_pair_priority + UI select v /dentrh sablone (uklada sa do profilu cez snapshot). Parove cykly so spreadom, oba smery, priorita closest/profit/balanced, poplatok len na nabijani. golden 5/5 + matcher 8/8"
echo "=== HEAD po ==="; git log --oneline -1
git push origin dev
echo "=== origin/dev ==="; git log --oneline -1 origin/dev
echo ">>> HOTOVO (Mac). Teraz na WINDOWS: .\\scripts\\upgrade_dev.ps1"
echo ""
echo ">>> AKTIVACIA na profile (napr. VW_simulacia_3) — v kontajneri:"
echo "    docker exec trading-fuergy-dev python -c \"import profiles as p; d=p.load_profile('VW_simulacia_3'); d['plan']['vdt_engine']='pairs'; d['plan']['vdt_pair_priority']='closest'; p.save_profile('VW_simulacia_3', d); print('OK', d['plan'].get('vdt_engine'), d['plan'].get('vdt_pair_priority'))\""
echo "    (priorita: closest = najblizsi par / profit = najziskovejsi / balanced = marza/vzdialenost)"
echo "    Potom: /plan_batch FULL reset pre VW_simulacia_3 -> re-sim. Over cez:"
echo "    docker exec trading-fuergy-dev python tools/diag_trades.py --profile VW_simulacia_3 --date 2026-06-20 --from-hour 14"
echo "    Ocakavanie: ziadne nakupy @180-250, saldo ~0, vsetky pary ziskove."
