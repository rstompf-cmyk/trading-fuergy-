#!/bin/bash
# restart_sluzby.sh — reštartne všetky FTV app inštancie (alebo vybrané porty).
#
# Použitie:
#   ./restart_sluzby.sh                # všetky porty (8000, 8001, 8002)
#   ./restart_sluzby.sh 8000           # iba jeden port
#   ./restart_sluzby.sh 8000 8001      # viaceré porty
#
# Robí: launchctl unload → 2s pauza → launchctl load -w
# Použi po update kódu (pull, edit .py súborov) aby sa natiahli zmeny.

set -e
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL_BASE="com.fuergy.ftv-app"

# Zber plistov: argumenty alebo všetky
PLISTS=()
if [ $# -eq 0 ]; then
    for f in "$LAUNCH_AGENTS_DIR/$LABEL_BASE."*.plist; do
        [ -f "$f" ] && PLISTS+=("$f")
    done
    if [ ${#PLISTS[@]} -eq 0 ]; then
        echo "✗ Žiadna nainštalovaná služba. Najprv ./install_sluzba.sh"
        exit 1
    fi
    echo "▸ Reštartujem VŠETKY inštancie (${#PLISTS[@]} ks)"
else
    for PORT in "$@"; do
        PLISTS+=("$LAUNCH_AGENTS_DIR/$LABEL_BASE.$PORT.plist")
    done
    echo "▸ Reštartujem porty: $*"
fi

for PLIST_PATH in "${PLISTS[@]}"; do
    if [ ! -f "$PLIST_PATH" ]; then
        echo "  ⚠ $PLIST_PATH neexistuje — preskakujem"
        continue
    fi
    LABEL=$(basename "$PLIST_PATH" .plist)
    PORT="${LABEL##*.}"
    echo "  ▸ port $PORT — unload..."
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
done

# Krátka pauza aby procesy stihli skončiť
sleep 2

for PLIST_PATH in "${PLISTS[@]}"; do
    if [ ! -f "$PLIST_PATH" ]; then
        continue
    fi
    LABEL=$(basename "$PLIST_PATH" .plist)
    PORT="${LABEL##*.}"
    launchctl load -w "$PLIST_PATH"
    echo "  ✓ port $PORT — load"
done

# Krátky test
echo ""
echo "▸ Overujem (počkám 3s)..."
sleep 3
for PLIST_PATH in "${PLISTS[@]}"; do
    if [ ! -f "$PLIST_PATH" ]; then continue; fi
    LABEL=$(basename "$PLIST_PATH" .plist)
    PORT="${LABEL##*.}"
    LCTL_LINE=$(launchctl list 2>/dev/null | grep "$LABEL" || true)
    PID=$(echo "$LCTL_LINE" | awk '{print $1}')
    if [ -n "$LCTL_LINE" ] && [ "$PID" != "-" ]; then
        echo "  ✓ port $PORT — PID $PID · http://127.0.0.1:$PORT"
    else
        echo "  ✗ port $PORT — nenaštartoval (pozri ~/Library/Logs/ftv-app-$PORT.err.log)"
    fi
done

echo ""
echo "✓ Hotovo. Logy: tail -f ~/Library/Logs/ftv-app-8000.out.log"
