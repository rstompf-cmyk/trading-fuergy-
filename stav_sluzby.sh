#!/bin/bash
# stav_sluzby.sh — prehľad stavu všetkých FTV app inštancií spravovaných službou.
#
# Použitie:
#   ./stav_sluzby.sh              # všetky inštancie
#   ./stav_sluzby.sh 8000         # iba konkrétny port

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL_BASE="com.fuergy.ftv-app"

echo "═══════════════════════════════════════════════════════════════"
echo " FTV app — stav služby na pozadí"
echo "═══════════════════════════════════════════════════════════════"

# Nájdi všetky plisty — portable bash 3.2 spôsob (žiadny mapfile)
PLISTS=()
if [ $# -gt 0 ]; then
    # Filter podľa zadaných portov
    for PORT in "$@"; do
        f="$LAUNCH_AGENTS_DIR/$LABEL_BASE.$PORT.plist"
        if [ -f "$f" ]; then
            PLISTS+=("$f")
        else
            echo "  ⚠ Plist pre port $PORT neexistuje: $f"
        fi
    done
else
    # Všetky — iteruj cez glob s nullglob ochranou
    for f in "$LAUNCH_AGENTS_DIR/$LABEL_BASE."*.plist; do
        [ -f "$f" ] && PLISTS+=("$f")
    done
fi

if [ ${#PLISTS[@]} -eq 0 ]; then
    echo "✗ Žiadne nainštalované inštancie"
    echo "  Inštalácia: ./install_sluzba.sh"
    exit 1
fi

echo "Nájdených: ${#PLISTS[@]}"
echo ""

for PLIST_PATH in "${PLISTS[@]}"; do
    LABEL=$(basename "$PLIST_PATH" .plist)
    PORT="${LABEL##*.}"
    OUT_LOG="$HOME/Library/Logs/ftv-app-$PORT.out.log"
    ERR_LOG="$HOME/Library/Logs/ftv-app-$PORT.err.log"

    LCTL_LINE=$(launchctl list 2>/dev/null | grep "$LABEL" || true)
    PID=$(echo "$LCTL_LINE" | awk '{print $1}')
    EXIT=$(echo "$LCTL_LINE" | awk '{print $2}')

    echo "── Port $PORT (label: $LABEL)"

    if [ -z "$LCTL_LINE" ]; then
        echo "   ✗ NIE JE načítaná v launchctl"
        echo "   Načítanie: launchctl load -w $PLIST_PATH"
    elif [ "$PID" = "-" ]; then
        echo "   ⚠ Registrovaná ale proces NEBEŽÍ (last exit: $EXIT)"
        echo "   Pozri error log: tail ~/Library/Logs/ftv-app-$PORT.err.log"
    else
        # Detail z ps
        PS_INFO=$(ps -p "$PID" -o pid,etime,pcpu,pmem,rss 2>/dev/null | tail -1 | awk '{print "pid="$1" beží="$2" CPU="$3"% MEM="$4"% RSS="$5"KB"}')
        echo "   ✓ Beží — $PS_INFO"

        # Port test
        if command -v nc >/dev/null 2>&1; then
            if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
                echo "   ✓ HTTP počúva → http://127.0.0.1:$PORT"
            else
                echo "   ⚠ Port $PORT zatiaľ neodpovedá (možno sa štartuje)"
            fi
        fi
    fi

    # Posledné 2 riadky logov
    if [ -f "$ERR_LOG" ] && [ -s "$ERR_LOG" ]; then
        LAST_ERR=$(tail -2 "$ERR_LOG" | head -1 | head -c 100)
        if [ -n "$LAST_ERR" ]; then
            echo "   ⚠ Posledný stderr: $LAST_ERR..."
        fi
    fi
    echo ""
done

echo "Užitočné:"
echo "  Logy live (port 8000):  tail -f ~/Library/Logs/ftv-app-8000.out.log"
echo "  Reštart 1 port:         launchctl unload ~/Library/LaunchAgents/$LABEL_BASE.8000.plist && \\"
echo "                          launchctl load -w ~/Library/LaunchAgents/$LABEL_BASE.8000.plist"
echo "  Stop 1 port:            launchctl unload ~/Library/LaunchAgents/$LABEL_BASE.8000.plist"
echo "  Odinštalácia:           ./uninstall_sluzba.sh [PORT...]"
