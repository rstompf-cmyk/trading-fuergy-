#!/bin/bash
# stop_dev.sh — zastaví bežiace inštancie aplikácie.
#
# Použitie:
#   ./stop_dev.sh             # zastaví všetky inštancie ktoré start_dev.sh spustil
#   ./stop_dev.sh 8001        # zastaví iba konkrétny port

cd "$(dirname "$0")"

PORTS=(8000 8001 8002)
if [ -n "$1" ]; then
    PORTS=("$1")
fi

for PORT in "${PORTS[@]}"; do
    PIDFILE="out/app_${PORT}.pid"
    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            echo "▸ Zastavujem port $PORT (pid $PID)..."
            kill "$PID"
            sleep 1
            if ps -p "$PID" > /dev/null 2>&1; then
                echo "  (graceful nezabral, posielam SIGKILL)"
                kill -9 "$PID"
            fi
            echo "  ✓ port $PORT zastavený"
        else
            echo "▸ Port $PORT: PID $PID už nebeží"
        fi
        rm -f "$PIDFILE"
    else
        # Fallback: skús nájsť beziaci app.py s týmto APP_PORT cez ps
        FOUND=$(ps aux | grep "[a]pp.py" | grep -E "APP_PORT=${PORT}\b" | awk '{print $2}')
        if [ -n "$FOUND" ]; then
            echo "▸ Nájdený bez PID súboru port $PORT (pid $FOUND), zastavujem..."
            kill "$FOUND" 2>/dev/null || true
        else
            echo "▸ Port $PORT: nebeží žiadny app.py (alebo PID súbor neexistuje)"
        fi
    fi
done
