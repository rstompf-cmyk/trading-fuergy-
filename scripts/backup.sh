#!/bin/bash
# backup.sh — Backup perzistentných dát Docker deploy-u Trading Fuergy.
#
# Použitie:
#   ./scripts/backup.sh              # default: zapíše do ./backups/
#   ./scripts/backup.sh /mnt/backup  # vlastné cieľové umiestnenie
#   ./scripts/backup.sh --keep 14    # zachovať len posledných 14 backupov
#
# Vyžaduje: tar, gzip (dostupné v Git Bash / WSL na Windows).
# Beží mimo Docker containeru — len páče host ./data/.

set -euo pipefail

cd "$(dirname "$0")/.."

DEST="${1:-./backups}"
KEEP=7
TS=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="trading-fuergy_${TS}.tar.gz"

# Parse --keep N
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep) KEEP="$2"; shift 2 ;;
    *) DEST="$1"; shift ;;
  esac
done

if [ ! -d "./data" ]; then
  echo "❌ Adresár ./data/ neexistuje — najprv spusti Docker compose."
  exit 1
fi

mkdir -p "$DEST"

echo "▸ Backup do $DEST/$BACKUP_NAME"

# Backup obsahuje:
# - data/db/         (SQLite app.db)
# - data/out/        (livesim, plány, profily, scenare, cache, realio_db)
# - data/okte_credentials/  (mTLS certifikáty — citlivé!)
# - .env             (credentials)
tar czf "$DEST/$BACKUP_NAME" \
    --exclude="data/out/cache" \
    --exclude="data/out/*.tmp" \
    --exclude="data/out/__*" \
    data/ \
    .env 2>/dev/null || true

SIZE=$(du -h "$DEST/$BACKUP_NAME" | cut -f1)
echo "✓ Backup vytvorený: $BACKUP_NAME ($SIZE)"

# Cleanup starých backupov — zachová len posledných $KEEP
echo "▸ Cleanup — zachová posledných $KEEP backupov"
cd "$DEST"
# shellcheck disable=SC2012
ls -1t trading-fuergy_*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

REMAINING=$(ls -1 trading-fuergy_*.tar.gz 2>/dev/null | wc -l)
echo "✓ Hotovo. Aktuálne backupov v $DEST: $REMAINING"
