# Trading Fuergy — Quick Start na Windows

Tento návod prevedie tebou v 10 krokoch cez kompletnú inštaláciu na Windows server. Postupuj zhora dole, krok po kroku. **Cieľová cesta:** `C:\Users\radoslav.stompf\Documents\_FUERGY\TradingFuergy`.

> Pred začatím: skontroluj že máš Windows 10/11 Pro alebo Server 2019+. Home edícia nemá Hyper-V → nemôže WSL2.

---

## Krok 1: Nainštaluj Docker Desktop

1. Stiahni: <https://www.docker.com/products/docker-desktop>
2. Spusti inštalátor, **nech používa WSL 2 backend** (zaškrtni voľbu pri inštalácii).
3. Po reštarte otvor Docker Desktop, prihlás sa (alebo Skip).
4. V Settings → General overuj že **"Use the WSL 2 based engine"** je zaškrtnuté.
5. Otestuj v PowerShell:
   ```powershell
   docker --version
   docker run hello-world
   ```
   Musí vrátiť verziu a `Hello from Docker!`.

---

## Krok 2: Nainštaluj Git for Windows

1. Stiahni: <https://git-scm.com/download/win>
2. Pri inštalácii nechaj **defaultné voľby** (Git Bash, OpenSSH).
3. Otestuj:
   ```powershell
   git --version
   ```

---

## Krok 3: Vytvor SSH kľúč pre GitHub

```powershell
ssh-keygen -t ed25519 -C "trading-fuergy-windows"
```

- Pri otázke **"Enter file in which to save"** stlač Enter (default `C:\Users\radoslav.stompf\.ssh\id_ed25519`).
- Pri otázke **"passphrase"** nechaj prázdne (Enter 2×) — server-only kľúč, netreba heslo.

Zobraz public key a skopíruj do clipboardu:
```powershell
type C:\Users\radoslav.stompf\.ssh\id_ed25519.pub | clip
```

Otvor GitHub → klik na avatar → **Settings** → **SSH and GPG keys** → **New SSH key**:
- Title: `Trading Fuergy Windows server`
- Key type: `Authentication Key`
- Key: paste (Ctrl+V)
- **Add SSH key**

Otestuj pripojenie:
```powershell
ssh -T git@github.com
```
Odpovie: `Hi <username>! You've successfully authenticated...`

---

## Krok 4: Klonuj Trading Fuergy repo

```powershell
# Vytvor priečinok pre umiestnenie (ak ešte neexistuje)
mkdir C:\Users\radoslav.stompf\Documents\_FUERGY -ErrorAction SilentlyContinue

# Prejdi tam a klonuj
cd C:\Users\radoslav.stompf\Documents\_FUERGY
git clone --branch refactor-v2 git@github.com:fuergy/trading-fuergy.git TradingFuergy
cd TradingFuergy
```

> **Poznámka:** Ak repo ešte nie je vytvorené, daj mi vedieť — pomôžem s prvotným `git push` z Macu do nového GitHub repa.

---

## Krok 5: Uprav .env

V príkazovom riadku (rovnaký PowerShell):
```powershell
Copy-Item .env.example .env
notepad .env
```

V Notepade nastav konkrétne hodnoty (minimálne tieto):

```env
# Firemný historian
HISTORIAN_USER=admin
HISTORIAN_PASSWORD=admin#2023
HISTORIAN_HOST=http://192.168.34.31:8088

# Docker port — môže ostať default 8000
DOCKER_PORT=8000
```

Ulož a zatvor Notepad.

---

## Krok 6: Spusti setup script

```powershell
.\scripts\setup_windows.ps1
```

Script automaticky:
- Skontroluje Docker + Git inštaláciu
- Vytvorí `data/` adresár pre bind mounty (DB, plány, cookies)
- Buildne Docker image (**5-10 minút** — sťahuje Chromium pre Playwright)
- Spustí container `docker compose up -d`
- Čaká na health check (~30s)

Pri úspechu vidíš:
```
✓ Trading Fuergy beží!
  Otvor http://localhost:8000
```

---

## Krok 7: Prvý login + zmena admin hesla

1. Otvor v prehliadači `http://localhost:8000` (alebo z iného počítača `http://<windows-ip>:8000`).
2. Login:
   - **Username:** `admin`
   - **Password:** `admin`
3. Klikni v navigácii vpravo na **admin · Odhlásiť** chip → nedáš odhlásiť, ale zobrazí ti meno.
4. Choď na **`/admin/users`** (cez URL alebo Admin menu).
5. Klikni **Upraviť** vedľa admin usera.
6. Vlož **nové heslo** → **Uložiť**.
7. Odhláš sa a prihlás s novým heslom (overenie).

---

## Krok 8 (voliteľný): OKTE certifikát pre VDT (SK trh)

Iba ak chceš používať OKTE VDT (intraday trh):

```powershell
# Skopíruj .p12 z miesta kde ho máš
Copy-Item D:\stiahnute\okte_cert.p12 .\data\okte_credentials\

# Inštaluj cert v containeri
docker compose exec trading-fuergy bash -c "./install_okte_cert.sh /app/okte_credentials/okte_cert.p12"
```

Script pýta heslo k .p12 — zadaj a stlač Enter.

Otvor `http://localhost:8000/vdt` — mali by sa zobraziť OKTE data.

---

## Krok 9: Otestuj LAN prístup

V príkazovom riadku otestuj že container vidí internú sieť:

```powershell
docker compose exec trading-fuergy bash -c "curl -s -o /dev/null -w 'Bender: %{http_code}\n' http://10.200.136.21"
docker compose exec trading-fuergy bash -c "curl -s -o /dev/null -w 'Historian: %{http_code}\n' http://192.168.34.31:8088"
```

Očakávané: HTTP `200`, `301`, `302`, alebo `401` (= server odpovedá, hocijaký kód je OK; iba `000` znamená že connection failed).

Ak `000`: skontroluj firewall na Windows + že VPN/sieť pripája server na rovnakú LAN ako Mac.

---

## Krok 10: Nastav auto-backup

Otvor **Task Scheduler** (Win+R → `taskschd.msc`):

1. **Create Task** (nie "Basic Task" — chceme zahrnúť plný kontrol).
2. **General:**
   - Name: `Trading Fuergy daily backup`
   - **"Run whether user is logged on or not"** zaškrtnuté
3. **Triggers** → **New**:
   - Daily, Start at: `03:00`
4. **Actions** → **New**:
   - Action: `Start a program`
   - Program: `C:\Windows\System32\wsl.exe`
   - Arguments: `bash -c "cd /mnt/c/Users/radoslav.stompf/Documents/_FUERGY/TradingFuergy && ./scripts/backup.sh"`
5. **OK** → zadá heslo k Windows účtu.

Backupy sa budú ukladať do `C:\Users\radoslav.stompf\Documents\_FUERGY\TradingFuergy\backups\` ako `trading-fuergy_YYYYMMDD_HHMMSS.tar.gz`. Default retention: 7 backupov.

---

## Bežné operácie (denný workflow)

V PowerShell vždy z adresára `C:\Users\radoslav.stompf\Documents\_FUERGY\TradingFuergy`:

| Operácia | Príkaz |
|---|---|
| Aktualizovať appku po `git push` z Macu | `git pull; docker compose up -d --build` |
| Reštartovať bez rebuildu (po zmene .env) | `docker compose restart` |
| Sledovať logy realtime | `docker compose logs -f` |
| Sledovať len posledných 100 riadkov | `docker compose logs --tail 100` |
| Zastaviť (dáta ostávajú) | `docker compose down` |
| Spustiť znova | `docker compose up -d` |
| Stav health checku | `docker inspect --format '{{.State.Health.Status}}' trading-fuergy` |
| Vstúpiť do containeru pre debug | `docker compose exec trading-fuergy bash` |
| Manuálny backup | `wsl bash -c "cd /mnt/c/Users/radoslav.stompf/Documents/_FUERGY/TradingFuergy && ./scripts/backup.sh"` |

---

## Riešenie problémov

**`docker compose up` zlyhá s "image not found":**
```powershell
docker compose build --no-cache
docker compose up -d
```

**Bender / historian nereaguje:**
1. Otestuj z Windows hosta: `curl http://10.200.136.21`
2. Ak host vidí ale container nie → reštartuj Docker Desktop (resetuje WSL2 NAT)
3. Posledný fallback: zmen network mode na `host` v `docker-compose.yml`

**`Address already in use` na porte 8000:**
- Iná appka beží na 8000. V `.env` zmen:
  ```
  DOCKER_PORT=8001
  ```
- Reštartuj: `docker compose up -d`

**Zabudol som admin heslo:**
```powershell
docker compose exec trading-fuergy bash -c "python tools/seed_admin.py --force"
```
Vytvorí znova admin/admin. **Zmen heslo hneď po prihlásení.**

**Komplet reset (pozor — stratíš dáta!):**
```powershell
docker compose down
docker rmi trading-fuergy:latest
# Zachovaj backup pred:
Move-Item data data.OLD
.\scripts\setup_windows.ps1
```

---

## Bezpečnostné odporúčania

1. **Hneď zmen admin heslo** po prvom login (Krok 7).
2. **Firewall**: povoľ port 8000 len v internej LAN — nevystavuj na internet bez VPN.
3. **HTTPS**: pre externý prístup nasaď reverse proxy (Caddy / nginx) s Let's Encrypt cert.
4. **Backup test**: aspoň raz za mesiac otestuj že `restore.sh` skutočne obnoví dáta na inom hoste.
5. **SSH kľúč**: nevedz si ho na cloud/email — len lokálne.
6. **OKTE cert**: backupuj `.p12` súbor mimo Trading Fuergy adresára (napr. password manager).

---

## Súvisiace dokumenty

- `docs/DOCKER_DEPLOY.md` — kompletná architektúra + advanced
- `Dockerfile` — image build definícia
- `docker-compose.yml` — service config s bind mountmi
- `.env.example` — referenčné env premenné
