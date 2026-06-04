#!/bin/bash
# uninstall_sluzba.sh — odinštaluje FTV app službu (jeden alebo všetky porty).
#
# Použitie:
#   ./uninstall_sluzba.sh                # všetky inštancie (com.fuergy.ftv-app.*)
#   ./uninstall_sluzba.sh 8001           # iba konkrétny port
#   ./uninstall_sluzba.sh 8000 8001      # viaceré porty
#
# Logy ostávajú v ~/Library/Logs/ — manuálne mazanie ak treba.

set -e
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL_BASE="com.fuergy.ftv-app"

PLISTS=()
if [ $# -eq 0 ]; then
    # Žiadne argumenty → nájdi všetky com.fuergy.ftv-app.* plisty (portable bash 3.2)
    for f in "$LAUNCH_AGENTS_DIR/$LABEL_BASE."*.plist; do
        [ -f "$f" ] && PLISTS+=("$f")
    done
    if [ ${#PLISTS[@]} -eq 0 ]; then
        echo "  ⚠ Žiadna nainštalovaná služba (com.fuergy.ftv-app.*)"
        exit 0
    fi
    echo "▸ Odinštalovávam VŠETKY inštancie (${#PLISTS[@]} ks):"
else
    for PORT in "$@"; do
        PLISTS+=("$LAUNCH_AGENTS_DIR/$LABEL_BASE.$PORT.plist")
    done
    echo "▸ Odinštalovávam porty: $*"
fi

for PLIST_PATH in "${PLISTS[@]}"; do
    if [ ! -f "$PLIST_PATH" ]; then
        echo "  ⚠ $PLIST_PATH neexistuje — preskakujem"
        continue
    fi
    LABEL=$(basename "$PLIST_PATH" .plist)
    # Unload — zastaví proces
    if launchctl list 2>/dev/null | grep -q "$LABEL"; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        echo "  ✓ Zastavená: $LABEL"
    fi
    rm -f "$PLIST_PATH"
    echo "  ✓ Odstránená: $PLIST_PATH"
done

echo ""
echo "✓ Hotovo."
echo "  Logy ostávajú v ~/Library/Logs/ftv-app-*.{out,err}.log"
echo "  Manuálny štart bez služby: ./start_dev.sh"
