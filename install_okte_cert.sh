#!/bin/bash
# install_okte_cert.sh — exportuje OKTE klientsky certifikát z .p12 do .crt + .key.
#
# Predpoklady:
#   1. Otvor Keychain Access (macOS aplikácia)
#   2. Nájdi OKTE certifikát (typicky v "login" keychain → My Certificates)
#   3. Right-click → Export → vyber formát "Personal Information Exchange (.p12)"
#   4. Zadaj heslo (zapamätaj si — budeš ho potrebovať aj tu)
#   5. Ulož niekam (napr. ~/Downloads/okte_cert.p12)
#
# Použitie:
#   ./install_okte_cert.sh ~/Downloads/okte_cert.p12
#   ./install_okte_cert.sh ~/Downloads/okte_cert.p12 "okte-vdt-base-url"
#
# Skript:
#   - Spýta sa na heslo (interaktívne, neukladá sa)
#   - Cez openssl extrahuje cert (.crt) a privátny kľúč (.key) do out/<market>/okte_vdt/
#   - Aktualizuje okte_vdt_config.json (cert_path + key_path + enabled=true)
#   - Otestuje pripojenie cez okte_vdt.probe()

set -e
cd "$(dirname "$0")"

if [ -z "$1" ]; then
    echo "Použitie: $0 <path-to-okte.p12> [base_url]"
    echo ""
    echo "Postup ako exportovať z Keychain:"
    echo "  1. Otvor 'Keychain Access' (Cmd+Space → Keychain Access)"
    echo "  2. Sidebar: 'login' → 'My Certificates'"
    echo "  3. Nájdi OKTE certifikát (typicky 'isot.okte.sk' alebo meno tvojej firmy)"
    echo "  4. Right-click → Export → format '.p12'"
    echo "  5. Ulož napr. do ~/Downloads/okte_cert.p12 + zadaj heslo"
    echo "  6. Spusti: $0 ~/Downloads/okte_cert.p12"
    exit 1
fi

P12_FILE="$1"
BASE_URL="${2:-https://isot.okte.sk/api/v1}"

if [ ! -f "$P12_FILE" ]; then
    echo "✗ Súbor neexistuje: $P12_FILE"
    exit 1
fi

# Aktívny trh
MARKET="${MARKET:-sk}"
OUT_DIR="out/$MARKET/okte_vdt"
mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"

CRT_PATH="$OUT_DIR/cert.crt"
KEY_PATH="$OUT_DIR/cert.key"

echo "▸ Konvertujem $P12_FILE → $CRT_PATH + $KEY_PATH"
echo "  Zadaj heslo k .p12 súboru:"

# Extrahuj cert (verejnú časť)
openssl pkcs12 -in "$P12_FILE" -clcerts -nokeys -out "$CRT_PATH" -legacy 2>/dev/null || \
openssl pkcs12 -in "$P12_FILE" -clcerts -nokeys -out "$CRT_PATH"

# Extrahuj privátny kľúč (bez šifrovania na disku — chránime cez permissions 600)
openssl pkcs12 -in "$P12_FILE" -nocerts -nodes -out "$KEY_PATH" -legacy 2>/dev/null || \
openssl pkcs12 -in "$P12_FILE" -nocerts -nodes -out "$KEY_PATH"

chmod 600 "$CRT_PATH" "$KEY_PATH"

# Validuj že súbory majú očakávaný obsah
if ! grep -q "BEGIN CERTIFICATE" "$CRT_PATH"; then
    echo "✗ $CRT_PATH neobsahuje validný certifikát"
    exit 1
fi
if ! grep -qE "BEGIN (PRIVATE|RSA PRIVATE|EC PRIVATE) KEY" "$KEY_PATH"; then
    echo "✗ $KEY_PATH neobsahuje validný privátny kľúč"
    exit 1
fi

# Info o certifikáte
echo ""
echo "▸ Informácie o certifikáte:"
openssl x509 -in "$CRT_PATH" -noout -subject -issuer -dates | sed 's/^/  /'

# Aktualizuj okte_vdt_config.json (zachovaj ostatné fieldy)
ABS_CRT=$(cd "$(dirname "$CRT_PATH")" && pwd)/$(basename "$CRT_PATH")
ABS_KEY=$(cd "$(dirname "$KEY_PATH")" && pwd)/$(basename "$KEY_PATH")
CFG_PATH="out/$MARKET/okte_vdt_config.json"

# Vytvor minimálny config ak ešte neexistuje
if [ ! -f "$CFG_PATH" ]; then
    cat > "$CFG_PATH" << EOF
{
  "enabled": true,
  "base_url": "$BASE_URL",
  "cert_path": "$ABS_CRT",
  "key_path": "$ABS_KEY",
  "verify_ssl": true,
  "username": "",
  "password": "",
  "timeout_s": 20,
  "endpoints": {
    "orders":     "/participant/idm/orders",
    "orderbook":  "/idm/orderbook",
    "trades":     "/participant/idm/trades",
    "account":    "/participant/account",
    "products":   "/idm/products"
  }
}
EOF
    echo "  ✓ Config vytvorený: $CFG_PATH"
else
    # Update existujúceho cez python (zachovaj všetky ostatné kľúče)
    python3 -c "
import json
p = '$CFG_PATH'
with open(p) as f:
    cfg = json.load(f)
cfg['enabled'] = True
cfg['cert_path'] = '$ABS_CRT'
cfg['key_path']  = '$ABS_KEY'
if not cfg.get('base_url'):
    cfg['base_url'] = '$BASE_URL'
with open(p, 'w') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print('  ✓ Config aktualizovaný: ' + p)
"
fi

# Doplníme username/password (interaktívne)
echo ""
echo "▸ Voliteľné: ISOT username + password (basic auth okrem cert-u)"
echo "  Ak OKTE nepoužíva basic auth (iba cert), nechaj prázdne"
read -p "  Username [enter=skip]: " ISOT_USER
if [ -n "$ISOT_USER" ]; then
    read -s -p "  Password: " ISOT_PASS
    echo ""
    python3 -c "
import json
p = '$CFG_PATH'
with open(p) as f:
    cfg = json.load(f)
cfg['username'] = '$ISOT_USER'
cfg['password'] = '$ISOT_PASS'
with open(p, 'w') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print('  ✓ Credentials uložené')
"
fi

echo ""
echo "▸ Otestujem pripojenie cez okte_vdt.probe()..."
echo ""
python3 -c "
import os
os.environ.setdefault('MARKET', '$MARKET')
import okte_vdt
res = okte_vdt.probe()
import json
print(json.dumps(res, ensure_ascii=False, indent=2))
"

echo ""
echo "✓ Hotovo. Stránka /vdt by mala fungovať."
echo ""
echo "Pripomienka bezpečnosti:"
echo "  • $ABS_CRT a $ABS_KEY sú chránené cez chmod 600 (iba ty)"
echo "  • Tieto súbory NEZDIEĽAJ — sú ekvivalentné k tvojmu OKTE prístupu"
echo "  • Pôvodný $P12_FILE môžeš zmazať, alebo nechaj ako zálohu"
