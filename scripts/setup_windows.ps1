# setup_windows.ps1 - Prvotna instalacia Trading Fuergy na Windows serveri (PROD).
#
# Predpoklad: Docker Desktop + WSL2 + Git for Windows nainstalovane a Docker bezi.
# Spusti raz pri prvom deploy. Pri dalsich update-och staci:
#   git pull
#   docker compose up -d --build trading-fuergy
#
# DOLEZITE: skript je ulozeny ako ASCII (ziadne diakritiky / emoji / em-dash),
# aby fungoval v PowerShell 5.1 ktory nema UTF-8 default encoding.
#
# Pouzitie (z PowerShell ako Administrator):
#   .\scripts\setup_windows.ps1
#   .\scripts\setup_windows.ps1 -RepoUrl git@github.com:rstompf-cmyk/trading-fuergy-.git
#   .\scripts\setup_windows.ps1 -InstallDir C:\Users\radoslav.stompf\Documents\_FUERGY\TradingFuergy

[CmdletBinding()]
param(
    [string]$RepoUrl    = "git@github.com:rstompf-cmyk/trading-fuergy-.git",
    [string]$InstallDir = "C:\Users\radoslav.stompf\Documents\_FUERGY\TradingFuergy",
    [string]$Branch     = "refactor-v2",
    [string]$Service    = "trading-fuergy"
)

$ErrorActionPreference = "Stop"

Write-Host "===================================================="  -ForegroundColor Cyan
Write-Host "  Trading Fuergy - Windows Setup (PROD)"                -ForegroundColor Cyan
Write-Host "===================================================="  -ForegroundColor Cyan
Write-Host ""

# 1) Skontroluj Docker
Write-Host ">> Kontrolujem Docker..." -ForegroundColor Yellow
try {
    $dockerVersion = docker --version 2>&1
    Write-Host "   [OK] $dockerVersion"
} catch {
    Write-Host "   [X] Docker nie je nainstalovany alebo nebezi." -ForegroundColor Red
    Write-Host "       Stiahni Docker Desktop: https://www.docker.com/products/docker-desktop"
    exit 1
}

# 2) Skontroluj Git
try {
    $gitVersion = git --version 2>&1
    Write-Host "   [OK] $gitVersion"
} catch {
    Write-Host "   [X] Git nie je nainstalovany." -ForegroundColor Red
    Write-Host "       Stiahni: https://git-scm.com/download/win"
    exit 1
}

# 2b) Git SSH config -- pouzi systemovy OpenSSH (nie Git Bash bundled)
# Bez tohto git@github.com clone moze hadzat "Permission denied (publickey)"
# aj ked `ssh -T git@github.com` funguje.
$sysSSH = "C:/Windows/System32/OpenSSH/ssh.exe"
if (Test-Path "C:\Windows\System32\OpenSSH\ssh.exe") {
    git config --global core.sshCommand $sysSSH 2>&1 | Out-Null
    Write-Host "   [OK] git core.sshCommand = $sysSSH"
}

# 3) Vytvor install dir (rodicovsky moze obsahovat diakritiku, to je OK)
$parent = Split-Path $InstallDir -Parent
if (-not (Test-Path $parent)) {
    Write-Host ">> Vytvaram rodicovsky adresar $parent" -ForegroundColor Yellow
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
}
if (-not (Test-Path $InstallDir)) {
    Write-Host ">> Vytvaram $InstallDir" -ForegroundColor Yellow
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}

Set-Location $InstallDir

# 4) Klonuj alebo aktualizuj repo
if (Test-Path ".git") {
    Write-Host ">> Repo uz existuje, aktualizujem..." -ForegroundColor Yellow
    git fetch origin
    git checkout $Branch
    git pull origin $Branch
} else {
    Write-Host ">> Klonujem $RepoUrl branch $Branch" -ForegroundColor Yellow
    git clone --branch $Branch $RepoUrl .
}

# 5) Pripravim data adresar (bind mounty)
Write-Host ">> Pripravujem data/ adresar pre bind mounty" -ForegroundColor Yellow
@("data\db", "data\out", "data\okte_credentials") | ForEach-Object {
    if (-not (Test-Path $_)) {
        New-Item -ItemType Directory -Path $_ -Force | Out-Null
        Write-Host "   + $_"
    }
}

# Prazdne JSON subory pre cookies (Docker compose vyzaduje existujuce subory pre bind mount)
@("data\realio_cookies.json", "data\seps_cookies.json", "data\historian_cookies.json") | ForEach-Object {
    if (-not (Test-Path $_)) {
        Set-Content -Path $_ -Value "{}"
        Write-Host "   + $_ (prazdny - doplni sa pri prvom login refresh)"
    }
}

# 6) .env subor
if (-not (Test-Path ".env")) {
    Write-Host ">> Vytvaram .env z .env.example" -ForegroundColor Yellow
    Copy-Item ".env.example" ".env"
    Write-Host ""
    Write-Host "   [!] UPRAV .env a vypln credentials:" -ForegroundColor Yellow
    Write-Host "       - HISTORIAN_USER / HISTORIAN_PASSWORD"
    Write-Host "       - DOCKER_PORT (ak chces iny ako 8000)"
    Write-Host ""
    Write-Host "       notepad $InstallDir\.env"
    Write-Host ""
    Read-Host "Stlac ENTER ked mas .env vyplneny"
}

# 7) Build + spustenie
Write-Host ">> Buildujem Docker image (prve spustenie 5-10 min)..." -ForegroundColor Yellow
docker compose build $Service

Write-Host ">> Spustam $Service..." -ForegroundColor Yellow
docker compose up -d $Service

# 8) Pockaj na health check
Write-Host ">> Cakam na health check (max 90s)..." -ForegroundColor Yellow
$maxWait = 90
$waited = 0
$healthy = $false
while ($waited -lt $maxWait) {
    Start-Sleep -Seconds 5
    $waited += 5
    $status = docker inspect --format '{{.State.Health.Status}}' $Service 2>$null
    if ($status -eq "healthy") {
        $healthy = $true
        break
    }
    Write-Host "   ... $status ($waited s)"
}

if (-not $healthy) {
    Write-Host ""
    Write-Host "   [!] Health check neprisiel za 90s. Posledne logy:" -ForegroundColor Yellow
    docker compose logs --tail 30 $Service
    Write-Host ""
    Write-Host "   Pokracujem v init kroku -- moze to byt iba pomaly start."
}

# 9) DB init: alembic upgrade head (idempotentne -- bezpecne pri opakovanom spusteni)
Write-Host ">> Inicializujem DB schemu (alembic upgrade head)..." -ForegroundColor Yellow
$alembicOut = docker compose exec -T $Service alembic upgrade head 2>&1
$alembicOut | ForEach-Object { Write-Host "   $_" }

# 10) Seed admin user (idempotentne -- --update nastavi heslo aj ked existuje)
Write-Host ">> Seedujem admin user-a (username=admin, password=admin)..." -ForegroundColor Yellow
$seedOut = docker compose exec -T $Service python tools/seed_admin.py --username admin --password admin --role admin --update 2>&1
$seedOut | ForEach-Object { Write-Host "   $_" }

# 11) Restart pre cisty stav po init
Write-Host ">> Restartujem container pre cisty stav..." -ForegroundColor Yellow
docker compose restart $Service
Start-Sleep -Seconds 15
$finalStatus = docker inspect --format '{{.State.Health.Status}}' $Service 2>$null

Write-Host ""
if ($finalStatus -eq "healthy") {
    Write-Host "[OK] Trading Fuergy bezi!" -ForegroundColor Green
    $port = if ($env:DOCKER_PORT) { $env:DOCKER_PORT } else { "8000" }
    Write-Host "     Otvor http://localhost:$port" -ForegroundColor Green
    Write-Host "     Login: admin / admin"
    Write-Host ""
    Write-Host "[!]  PRVE TVOJE TLACIDLO: zmen admin heslo cez /admin/users" -ForegroundColor Yellow
} else {
    Write-Host "[!] Health check po restarte: $finalStatus" -ForegroundColor Yellow
    Write-Host "    Pozri logy: docker compose logs --tail 50 $Service"
}

Write-Host ""
Write-Host "===================================================="  -ForegroundColor Cyan
Write-Host "  Uzitocne prikazy:"                                    -ForegroundColor Cyan
Write-Host "===================================================="  -ForegroundColor Cyan
Write-Host "  docker compose ps                                # stav containerov"
Write-Host "  docker compose logs -f $Service                  # sleduj logy"
Write-Host "  docker compose down                              # zastav"
Write-Host "  docker compose up -d --build $Service            # update po git pull"
Write-Host "  wsl bash ./scripts/backup.sh                     # backup dat"
Write-Host ""
Write-Host "  Pre DEV environment (port 8001, dev branch):"
Write-Host "    .\scripts\setup_dev_windows.ps1"
