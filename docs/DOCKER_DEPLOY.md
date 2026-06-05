# Trading Fuergy — Docker Deploy (Windows produkcia)

Tento dokument popisuje nasadenie Trading Fuergy aplikácie na Windows server cez Docker Desktop + WSL2. Workflow: vývoj na Mac → push do GitHub → pull na Windows → docker compose rebuild.

## Architektúra

```
Windows server (LAN)
├── Docker Desktop (WSL2 backend)
│   └── Container: trading-fuergy
│       ├── FastAPI + uvicorn (port 8000)
│       ├── SQLAlchemy → /app/db/data/app.db
│       ├── Playwright Chromium (SEPS/historian refresh)
│       └── APScheduler (denné fetche, D-1 plán)
│
└── Host bind mounts → C:\Users\radoslav.stompf\Documents\_FUERGY\TradingFuergy\
    ├── data/db/                    # SQLite databáza
    ├── data/out/                   # plány, profily, livesim CSV, cache
    ├── data/okte_credentials/      # OKTE mTLS cert + kľúč
    ├── data/realio_cookies.json    # Bender session
    ├── data/seps_cookies.json      # SEPS cookies
    ├── data/historian_cookies.json # Historian cookies
    ├── .env                        # credentials (HISTORIAN_USER, ...)
    └── backups/                    # tar.gz snapshoty
```

**Kľúčové vlastnosti deploy-u:**
- **Stateless container** — všetky dáta na hostiteľovi, container sa môže rebuildnúť bez straty stavu.
- **Auto-restart** — `restart: unless-stopped` v compose, Docker Desktop reštartuje pri reboot Windows.
- **Health check** — `/me` endpoint kontrolovaný každých 30s, Docker reportuje stav.
- **LAN access** — bridge network module, container pristupuje k internej sieti (Bender, historian) cez NAT.

## Prvotná inštalácia

### 1. Predpoklady

- **Windows 10/11 Pro/Enterprise** alebo **Windows Server 2019+**
- **Docker Desktop** (najnovšia verzia, WSL2 backend povolený)
  - Stiahni: https://www.docker.com/products/docker-desktop
  - V settings: General → "Use the WSL 2 based engine" zaškrtnuté
- **Git for Windows**
  - Stiahni: https://git-scm.com/download/win
- **PowerShell 5.1+** (predinštalovaný)
- **Sieťový prístup** k internej sieti (rovnaký podnik VLAN ako Bender 10.200.136.21 a historian 192.168.34.31)

### 2. SSH kľúč pre GitHub

Generuj SSH kľúč pre prístup do privátneho repa:

```powershell
ssh-keygen -t ed25519 -C "trading-fuergy-windows"
# Akceptuj default path: C:\Users\<user>\.ssh\id_ed25519
# Heslo môže byť prázdne (server-only access)
cat C:\Users\<user>\.ssh\id_ed25519.pub
```

Skopíruj výstup `.pub` súboru do GitHub → Settings → SSH and GPG keys → New SSH key.

### 3. Spusti setup script

```powershell
# Z PowerShell ako Administrator
cd C:\Users\radoslav.stompf\Documents\_FUERGY
git clone --branch refactor-v2 git@github.com:fuergy/trading-fuergy.git TradingFuergy
cd TradingFuergy
.\scripts\setup_windows.ps1
```

Script urobí:
1. Skontroluje Docker + Git inštaláciu
2. Vytvorí `data/` adresár pre bind mounty
3. Skopíruje `.env.example` → `.env` (pýta sa na úpravu credentials)
4. Spustí `docker compose build` (~5-10 min pri prvom buildu — Chromium download)
5. Spustí container cez `docker compose up -d`
6. Sleduje health check

### 4. Doplň OKTE certifikáty

Ak používaš OKTE VDT (SK trh):

```powershell
# Skopíruj .p12 cert do Windows
Copy-Item D:\stiahnute\okte_cert.p12 C:\Users\radoslav.stompf\Documents\_FUERGY\TradingFuergy\data\okte_credentials\

# Spusti install_okte_cert.sh vnútri containeru
docker compose exec trading-fuergy bash
# Vnútri containeru:
./install_okte_cert.sh /app/okte_credentials/okte_cert.p12
exit
```

### 5. Prvý login

Otvor v prehliadači `http://<server-ip>:8000` alebo `http://localhost:8000` (na servery).

Default credentials:
- **Username:** `admin`
- **Password:** `admin`

**HNEĎ zmen heslo** cez `/admin/users` → Edit admin → nové heslo → Uložiť.

## Update workflow (po zmene v repe)

Workflow: zmeny push na Macu → pull na Windowse → rebuild container.

### Na Mac (vývoj)

```bash
cd /Users/.../Aplikacia
git checkout refactor-v2
# ... úpravy kódu ...
git add -A
git commit -m "feat: ..."
git push origin refactor-v2
```

### Na Windows server

```powershell
cd C:\Users\radoslav.stompf\Documents\_FUERGY\TradingFuergy
git pull origin refactor-v2
docker compose up -d --build
# Sleduj health check:
docker compose logs -f
```

Container sa rebuilduje s novým kódom, **dáta v `data/` ostávajú nezmenené** (DB, plány, certifikáty).

## Bežné operácie

### Sledovanie logov

```powershell
docker compose logs -f                # všetky logy realtime
docker compose logs --tail 100        # posledných 100 riadkov
docker compose logs --since 1h        # za poslednú hodinu
```

### Reštart bez rebuildu

```powershell
docker compose restart
```

### Zastavenie / spustenie

```powershell
docker compose down       # zastaví container, data zostávajú
docker compose up -d      # spustí znova
```

### Stav containeru

```powershell
docker compose ps         # bežiace containery
docker inspect --format '{{.State.Health.Status}}' trading-fuergy
```

### Vstup do containeru pre debug

```powershell
docker compose exec trading-fuergy bash
# Vnútri:
ls /app/out/
cat /app/db/data/app.db | sqlite3 -header
python -c "import profiles; print(profiles.list_profiles())"
```

## Backup a restore

### Pravidelný backup

V `scripts/backup.sh` je skript ktorý vytvorí tar.gz snapshot dát:

```bash
# Z WSL bash:
cd /mnt/c/Users/radoslav.stompf/Documents/_FUERGY/TradingFuergy
./scripts/backup.sh
# Vytvorí: backups/trading-fuergy_YYYYMMDD_HHMMSS.tar.gz
```

**Automatizácia (Windows Task Scheduler):**

1. Otvor Task Scheduler → Create Task
2. Trigger: Daily at 03:00
3. Action: Start a program
   - Program: `C:\Windows\System32\wsl.exe`
   - Arguments: `bash -c "cd /mnt/c/Users/radoslav.stompf/Documents/_FUERGY/TradingFuergy && ./scripts/backup.sh"`

Backup obsahuje:
- SQLite databáza (`data/db/app.db`)
- Plány, profily, cache (`data/out/`)
- OKTE certifikáty + cookies (`data/okte_credentials/`, cookies JSON)
- Konfigurácia (`.env`)

### Restore zo backupu

```powershell
# Najprv zastav container
docker compose down

# WSL:
wsl bash ./scripts/restore.sh backups/trading-fuergy_20260605_120000.tar.gz

# Spusti znova
docker compose up -d
```

## Network setup pre LAN prístup

Container má **bridge network mode** (default Docker). To znamená:

- **Outbound traffic** (z containera von) — funguje cez NAT, prístup k LAN aj internet ako z hostiteľa.
- **Inbound traffic** (zvonku do containera) — len cez port mapping v compose (`8000:8000`).

**Test LAN prístupu z containeru:**

```powershell
docker compose exec trading-fuergy bash
# Vnútri:
curl http://10.200.136.21       # Bender Trakany — malo by odpovedať
curl http://192.168.34.31:8088  # Historian — malo by odpovedať
```

Ak `curl` zlyhá → skontroluj že Windows server má prístup k tým IP (firewall, VPN).

## Riešenie problémov

### Container sa nespúšťa

```powershell
docker compose logs --tail 50
```

Hľadaj chyby ako:
- `db/data/app.db: permission denied` → priečinok nebol vytvorený scriptom, skontroluj `data/db/` existuje
- `Address already in use` → Iná appka beží na porte 8000. Zmen `DOCKER_PORT=8001` v `.env`.
- `Module not found` → rebuilduj: `docker compose build --no-cache`

### Bender / historian neodpovedá z containeru

1. Skontroluj že beží z Windows hosta: `curl http://10.200.136.21`
2. Ak host vidí ale container nie → reštartuj Docker Desktop (resetuje WSL2 NAT)
3. Posledný fallback: host network mode. V `docker-compose.yml` pridaj:
   ```yaml
   services:
     trading-fuergy:
       network_mode: host
   ```
   (POZOR: stratíš `ports:` mapping — appka beží priamo na host porte.)

### Playwright Chromium zlyhá

V containeri:
```bash
docker compose exec trading-fuergy bash
playwright install --with-deps chromium
```

### SQLite locked / DB error

Container má jediný bežiaci proces, ale ak si robil `docker compose down` brutálne (Ctrl+C v `up`):
```powershell
docker compose down
# Skontroluj že SQLite WAL nie je rozbity:
wsl bash -c "cd /mnt/c/Users/radoslav.stompf/Documents/_FUERGY/TradingFuergy/data/db && sqlite3 app.db 'PRAGMA integrity_check;'"
docker compose up -d
```

### Reset všetkého (CAUTION)

```powershell
docker compose down
docker rmi trading-fuergy:latest
docker volume prune
# Backup data!
Move-Item data data.OLD
.\scripts\setup_windows.ps1
```

## Bezpečnostné odporúčania

1. **Heslo admin** zmen IHNEĎ po prvom login.
2. **Firewall**: povoľ port 8000 (alebo `DOCKER_PORT`) len v internej LAN — nevystavuj na internet bez VPN/reverse proxy.
3. **HTTPS**: pre internetový prístup nasaď reverse proxy (Caddy / nginx / Traefik) s Let's Encrypt cert pred Docker port.
4. **Backup test**: aspoň raz za mesiac otestuj `restore.sh` na inom hoste — verifikuje že backupy sú použiteľné.
5. **GitHub privátny repo**: Trading Fuergy obsahuje business logiku. Nasadenie cez `git@github.com:...` (SSH s privátnym kľúčom), nie HTTPS s tokenom.
6. **Credentials rotation**: HISTORIAN_PASSWORD a OKTE cert pravidelne meniť (per IT policy).

## Sync workflow: Mac dev ↔ Windows prod

Branch model:
- `main` — pôvodná verzia (port 8000 na Macu, JSON-only storage). Hot-fixes only.
- `refactor-v2` — Fázy 1-4 (SQLite, auth, Jinja2, Docker). **Toto je deploy branch.**

**Štandardný cyklus:**

1. Mac: `git checkout refactor-v2 && git pull`
2. Mac: úpravy kódu, lokálny test (`./start_dev.sh 8001`)
3. Mac: commit + `git push origin refactor-v2`
4. Windows: `git pull origin refactor-v2`
5. Windows: `docker compose up -d --build`
6. Windows: skontrolovať `docker compose logs --tail 50`
7. Otvoriť `http://server:8000` — overiť že funguje

**Pri väčších zmenách:**

Pred deploy odporúčam:
1. Backup: `wsl bash ./scripts/backup.sh`
2. Tag pred-deploy state: `git tag pre-deploy-$(date +%Y%m%d) && git push --tags`
3. Deploy
4. Test (5-10 min)
5. Ak fail → rollback: `git checkout pre-deploy-YYYYMMDD && docker compose up -d --build`

## Príloha: Známe rozdiely Mac dev ↔ Windows Docker

| Aspekt | Mac dev | Windows Docker |
|---|---|---|
| Path separator | `/` | `/` v containeri, `\` na host |
| Port | 8000-8002 paralelne (`PORTS=...`) | jediný port (z .env `DOCKER_PORT`) |
| AUTH_REQUIRED | `0` (opt-in) | `1` (always-on) |
| USE_DB | `0` (JSON default) | `1` (DB primary) |
| Playwright | manuálny `playwright install` | súčasť Docker image |
| Background daemons | `launchd` (`.plist`) | Docker `restart: unless-stopped` |
| Logs | `out/app_<port>.log` | `docker compose logs` |
| Data path | `./out`, `./db/data/app.db` | `./data/out`, `./data/db/app.db` (bind) |

## Súvisiace dokumenty

- `docs/data_model.md` — Fáza 0 audit DB schémy
- `docs/migration_notes.md` — Fáza 1 SQLAlchemy refactor poznámky
- `docs/permissions.md` — Fáza 0 audit rolí + ACL
- `Dockerfile` — multi-stage build definícia
- `docker-compose.yml` — služba + bind mounty
- `.env.example` — referenčné env premenné
