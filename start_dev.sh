#!/bin/bash
# start_dev.sh — spustí všetky paralelné inštancie aplikácie na pozadí.
#
# Použitie:
#   ./start_dev.sh                # spustí všetky porty (PORTS nižšie)
#   ./start_dev.sh 8005           # spustí navyše jednu inštanciu na konkrétnom porte
#
# Per-port stav (ui_settings, aktívny profil, livesim CSV) je izolovaný.
# Trh, plány, profily a šablóny sú zdieľané — zmena v jednej inštancii sa objaví
# vo všetkých ostatných po refresh-i.

set -e
cd "$(dirname "$0")"

# Aktivuj venv ak existuje
if [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

# Default sada portov ktoré sa spúšťajú. Pridaj/odober podľa potreby.
PORTS=(8000 8001 8002)

# Argument = jeden extra port (napr. ./start_dev.sh 8005)
if [ -n "$1" ]; then
    PORTS=("$1")
fi

mkdir -p out

for PORT in "${PORTS[@]}"; do
    # Skontroluj či už beží na tomto porte (lsof môže chýbať na minimal Linux → skip check)
    if command -v lsof >/dev/null 2>&1; then
        if lsof -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
            echo "⚠  Port $PORT už obsadený — preskakujem (zastav cez stop_dev.sh $PORT)"
            continue
        fi
    fi
    echo "▸ Štartujem inštanciu na porte $PORT..."
    # PORT aj APP_PORT — per-port state (ui_settings, _active_market, _active profile)
    # používa PORT, uvicorn bind používa APP_PORT. Bez oboch by inštancie zdieľali state.
    APP_HOST="${APP_HOST:-127.0.0.1}" APP_PORT="$PORT" PORT="$PORT" nohup python app.py \
        > "out/app_${PORT}.log" 2>&1 &
    echo "$!" > "out/app_${PORT}.pid"
done

sleep 2
echo ""
echo "Spustené inštancie:"
for PORT in "${PORTS[@]}"; do
    if [ -f "out/app_${PORT}.pid" ]; then
        PID=$(cat "out/app_${PORT}.pid")
        if ps -p "$PID" > /dev/null 2>&1; then
            echo "  ✓ http://127.0.0.1:${PORT}    (pid $PID, log: out/app_${PORT}.log)"
        else
            echo "  ✗ port ${PORT} — proces hneď spadol, pozri out/app_${PORT}.log"
        fi
    fi
done
