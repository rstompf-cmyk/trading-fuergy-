# setup_windows.ps1 — Prvotná inštalácia Trading Fuergy na Windows serveri.
#
# Predpoklad: Docker Desktop + WSL2 už nainštalované a beží.
# Spusti raz pri prvom deploy. Pri ďalších update-och stačí: git pull + docker compose up -d --build
#
# Použitie (z PowerShell ako Administrator):
#   .\scripts\setup_windows.ps1
#   .\scripts\setup_windows.ps1 -RepoUrl https://github.com/<user>/trading-fuergy.git
#   .\scripts\setup_windows.ps1 -InstallDir C:\TradingFuergy

[CmdletBinding()]
param(
    [string]$RepoUrl = "https://github.com/fuergy/trading-fuergy.git",
    [string]$InstallDir = "C:\TradingFuergy",
    [string]$Branch = "refactor-v2"
)

$ErrorActionPreference = "Stop"

Write-Host "════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  Trading Fuergy — Windows Setup" -ForegroundColor Cyan
Write-Host "════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""

# 1) Skontroluj Docker
Write-Host "▸ Kontrolujem Docker..." -ForegroundColor Yellow
try {
    $dockerVersion = docker --version 2>&1
    Write-Host "  ✓ $dockerVersion"
} catch {
    Write-Host "  ❌ Docker nie je nainštalovaný alebo nebeží." -ForegroundColor Red
    Write-Host "     Stiahni Docker Desktop: https://www.docker.com/products/docker-desktop"
    exit 1
}

# 2) Skontroluj Git
try {
    $gitVersion = git --version 2>&1
    Write-Host "  ✓ $gitVersion"
} catch {
    Write-Host "  ❌ Git nie je nainštalovaný." -ForegroundColor Red
    Write-Host "     Stiahni: https://git-scm.com/download/win"
    exit 1
}

# 3) Vytvor install dir
if (-not (Test-Path $InstallDir)) {
    Write-Host "▸ Vytváram $InstallDir" -ForegroundColor Yellow
    New-Item -ItemType Directory -Path $InstallDir | Out-Null
}

Set-Location $InstallDir

# 4) Klonuj alebo aktualizuj repo
if (Test-Path ".git") {
    Write-Host "▸ Repo už existuje, aktualizujem..." -ForegroundColor Yellow
    git fetch origin
    git checkout $Branch
    git pull origin $Branch
} else {
    Write-Host "▸ Klonujem $RepoUrl" -ForegroundColor Yellow
    git clone --branch $Branch $RepoUrl .
}

# 5) Pripravím data adresár (bind mounty)
Write-Host "▸ Pripravujem data/ adresár pre bind mounty" -ForegroundColor Yellow
@("data\db", "data\out", "data\okte_credentials") | ForEach-Object {
    if (-not (Test-Path $_)) {
        New-Item -ItemType Directory -Path $_ -Force | Out-Null
        Write-Host "  + $_"
    }
}

# Prázdne JSON súbory pre cookies (Docker compose očakáva existujúce súbory pre bind mount)
@("data\realio_cookies.json", "data\seps_cookies.json", "data\historian_cookies.json") | ForEach-Object {
    if (-not (Test-Path $_)) {
        Set-Content -Path $_ -Value "{}"
        Write-Host "  + $_ (prázdny — doplní sa pri prvom login refresh)"
    }
}

# 6) .env súbor
if (-not (Test-Path ".env")) {
    Write-Host "▸ Vytváram .env z .env.example" -ForegroundColor Yellow
    Copy-Item ".env.example" ".env"
    Write-Host ""
    Write-Host "  ⚠ UPRAV .env a vyplň credentials:" -ForegroundColor Yellow
    Write-Host "     - HISTORIAN_USER / HISTORIAN_PASSWORD"
    Write-Host "     - DOCKER_PORT (ak chceš iný ako 8000)"
    Write-Host ""
    Write-Host "     notepad $InstallDir\.env"
    Write-Host ""
    Read-Host "Stlač ENTER keď máš .env vyplnený"
}

# 7) Build + spustenie
Write-Host "▸ Buildujem Docker image (prvé spustenie ~5-10 min)..." -ForegroundColor Yellow
docker compose build

Write-Host "▸ Spúšťam Trading Fuergy..." -ForegroundColor Yellow
docker compose up -d

# 8) Počkaj na health check
Write-Host "▸ Čakám na health check (max 60s)..." -ForegroundColor Yellow
$maxWait = 60
$waited = 0
$healthy = $false
while ($waited -lt $maxWait) {
    Start-Sleep -Seconds 5
    $waited += 5
    $status = docker inspect --format '{{.State.Health.Status}}' trading-fuergy 2>$null
    if ($status -eq "healthy") {
        $healthy = $true
        break
    }
    Write-Host "  ... $status ($waited s)"
}

Write-Host ""
if ($healthy) {
    Write-Host "✓ Trading Fuergy beží!" -ForegroundColor Green
    $port = if ($env:DOCKER_PORT) { $env:DOCKER_PORT } else { "8000" }
    Write-Host "  Otvor http://localhost:$port" -ForegroundColor Green
    Write-Host "  Login: admin / admin (zmen heslo cez /admin/users)"
} else {
    Write-Host "⚠ Health check neprešiel — pozri logy:" -ForegroundColor Yellow
    Write-Host "   docker compose logs --tail 50"
}

Write-Host ""
Write-Host "════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  Užitočné príkazy:" -ForegroundColor Cyan
Write-Host "════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  docker compose ps             # stav containerov"
Write-Host "  docker compose logs -f        # sledovať logy"
Write-Host "  docker compose down           # zastaviť"
Write-Host "  docker compose up -d --build  # update po git pull"
Write-Host "  wsl bash ./scripts/backup.sh  # backup dát"
