#!/bin/bash
# restore.sh — Obnoví dáta z backup.tar.gz vytvoreného cez backup.sh.
#
# Použitie:
#   ./scripts/restore.sh backups/trading-fuergy_20260605_120000.tar.gz
#
# POZOR: Prepíše ./data/ a .env aktuálnymi hodnotami z backupu.
# Pred spustením treba zastaviť Docker container: docker compose down

set -euo pipefail

cd "$(dirname "$0")/.."

if [ -z "${1:-}" ]; then
  echo "Použitie: $0 <path-to-backup.tar.gz>"
  echo
  echo "Dostupné backupy:"
  ls -lh backups/trading-fuergy_*.tar.gz 2>/dev/null || echo "  (žiadne)"
  exit 1
fi

BACKUP="$1"

if [ ! -f "$BACKUP" ]; then
  echo "❌ Backup neexistuje: $BACKUP"
  exit 1
fi

# Skontroluj že Docker container nebeží (inak je risk korupcie DB)
if docker compose ps --status running 2>/dev/null | grep -q trading-fuergy; then
  echo "❌ Trading Fuergy container BEŽÍ. Najprv zastav:"
  echo "   docker compose down"
  exit 1
fi

echo "▸ Obnova z $BACKUP"
echo "  Veľkosť: $(du -h "$BACKUP" | cut -f1)"

# Posledná šanca na zastavenie
read -rp "Pokračovať? Prepíše ./data/ a .env. [y/N] " ANS
if [[ "$ANS" != "y" && "$ANS" != "Y" ]]; then
  echo "Zrušené."
  exit 0
fi

# Backup aktuálneho stavu pred prepisom (safety net)
if [ -d "./data" ]; then
  SAFETY="data.pre-restore.$(date +%Y%m%d_%H%M%S)"
  echo "▸ Safety backup aktuálnych dát do $SAFETY"
  mv ./data "./$SAFETY"
fi

# Rozbalíme backup
tar xzf "$BACKUP"

echo "✓ Restore dokončený. Spusti znova:"
echo "   docker compose up -d"
