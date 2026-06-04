#!/bin/bash
# install_sluzba.sh — nainštaluje FTV app ako macOS službu na pozadí.
#
# Po inštalácii sa Python proces (FastAPI + polling Bender + scheduler joby)
# spúšťa automaticky pri každom prihlásení do Macu, beží stále na pozadí.
# HTML rozhranie (HMI) je dostupné na http://127.0.0.1:PORT — zatvorenie
# prehliadača NEZASTAVÍ službu, polling/control beží ďalej.
#
# Použitie:
#   ./install_sluzba.sh                       # default: porty 8000 8001 8002, market=sk
#   ./install_sluzba.sh 8000                  # iba 1 inštancia na porte 8000
#   ./install_sluzba.sh 8000 8001 8002        # explicitne 3 porty
#   MARKET=cz ./install_sluzba.sh 8000        # market cz
#
# Logy per port v ~/Library/Logs/ftv-app-<PORT>.{out,err}.log
#
# Stav: ./stav_sluzby.sh
# Odinštalácia: ./uninstall_sluzba.sh [PORT...]   # bez argumentov = všetky inštancie

set -e
cd "$(dirname "$0")"

APP_DIR="$(pwd)"
MARKET="${MARKET:-sk}"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
TEMPLATE="$APP_DIR/sluzba.plist.template"
LABEL_BASE="com.fuergy.ftv-app"

# Default sada portov — rovnaká ako start_dev.sh
if [ $# -eq 0 ]; then
    PORTS=(8000 8001 8002)
else
    PORTS=("$@")
fi

# Detekuj Python — preferuj venv, inak system
if [ -x "$APP_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$APP_DIR/.venv/bin/python"
    PYTHON_INFO="venv"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
    PYTHON_INFO="system"
else
    echo "✗ Python3 nenájdený. Nainštaluj cez brew alebo aktivuj venv (.venv/)"
    exit 1
fi

if [ ! -f "$TEMPLATE" ]; then
    echo "✗ Template '$TEMPLATE' neexistuje"
    exit 1
fi

mkdir -p "$LAUNCH_AGENTS_DIR" "$HOME/Library/Logs"

echo "▸ Inštalujem FTV app ako službu na pozadí"
echo "  APP_DIR:  $APP_DIR"
echo "  PYTHON:   $PYTHON_BIN ($PYTHON_INFO)"
echo "  MARKET:   $MARKET"
echo "  PORTY:    ${PORTS[*]}"
echo ""

# Zabráň konfliktu — ak beží start_dev.sh inštancia na danom porte, varuj
for PORT in "${PORTS[@]}"; do
    if command -v lsof >/dev/null 2>&1; then
        EXISTING_PID=$(lsof -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)
        if [ -n "$EXISTING_PID" ]; then
            # Zisti či to je launchd alebo manuálny proces
            if launchctl list 2>/dev/null | grep -q "$LABEL_BASE\.$PORT"; then
                echo "  ▸ Port $PORT už spravovaný službou — reload pri ďalšom kroku"
            else
                echo "  ⚠ Port $PORT je obsadený procesom PID $EXISTING_PID (NIE službou)"
                echo "    Pred pokračovaním: ./stop_dev.sh $PORT   alebo:  kill $EXISTING_PID"
                exit 1
            fi
        fi
    fi
done

# Vytvor + nahraj plist pre každý port
for PORT in "${PORTS[@]}"; do
    LABEL="$LABEL_BASE.$PORT"
    PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
    echo "▸ Inštalujem inštanciu na porte $PORT (label: $LABEL)"

    # Substitúcia placeholderov
    sed -e "s|{{PYTHON_BIN}}|$PYTHON_BIN|g" \
        -e "s|{{APP_DIR}}|$APP_DIR|g" \
        -e "s|{{HOME}}|$HOME|g" \
        -e "s|{{PORT}}|$PORT|g" \
        -e "s|{{MARKET}}|$MARKET|g" \
        "$TEMPLATE" > "$PLIST_PATH"

    # Reload ak už existuje
    if launchctl list 2>/dev/null | grep -q "$LABEL"; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        sleep 1
    fi
    launchctl load -w "$PLIST_PATH"
    echo "  ✓ Plist: $PLIST_PATH"
    echo "  ✓ Logy:  ~/Library/Logs/ftv-app-$PORT.{out,err}.log"
done

# Krátky test — 3s počkať a overiť že všetky procesy bežia
echo ""
echo "▸ Overujem stav (počkám 3s)..."
sleep 3
ALL_OK=1
for PORT in "${PORTS[@]}"; do
    LABEL="$LABEL_BASE.$PORT"
    LCTL_LINE=$(launchctl list 2>/dev/null | grep "$LABEL" || true)
    PID=$(echo "$LCTL_LINE" | awk '{print $1}')
    if [ -n "$LCTL_LINE" ] && [ "$PID" != "-" ]; then
        echo "  ✓ port $PORT — PID $PID · http://127.0.0.1:$PORT"
    else
        echo "  ✗ port $PORT — neštartoval (pozri ~/Library/Logs/ftv-app-$PORT.err.log)"
        ALL_OK=0
    fi
done

echo ""
if [ $ALL_OK -eq 1 ]; then
    echo "✓ Hotovo. Služba beží, autoštart pri prihlásení."
else
    echo "⚠ Niektoré inštancie nenaštartovali — pozri error logy"
fi

echo ""
echo "Príkazy:"
echo "  Stav:        ./stav_sluzby.sh"
echo "  Logy:        tail -f ~/Library/Logs/ftv-app-8000.out.log"
echo "  HTML:        open http://127.0.0.1:8000"
echo "  Restart 1:   launchctl unload ~/Library/LaunchAgents/$LABEL_BASE.8000.plist && \\"
echo "               launchctl load -w ~/Library/LaunchAgents/$LABEL_BASE.8000.plist"
echo "  Odinštal.:   ./uninstall_sluzba.sh         (všetky porty)"
echo "               ./uninstall_sluzba.sh 8001    (len jeden port)"
