# setup_dev_windows.ps1 - DEV environment vedla produkcie (port 8001, dev branch).
#
# Predpoklad: prod uz bezi (.\scripts\setup_windows.ps1 uz prebehol).
# Tento skript:
#   1. Pripravi data-dev/ adresar (oddelene od prod data/)
#   2. Vytvori dev branch lokalne ak este nie je
#   3. Buildne trading-fuergy-dev image z dev branch
#   4. Spusti dev container na porte 8001 (configurable cez DEV_PORT v .env)
#   5. Inicializuje DB schemu + admin user (rovnako ako prod)
#
# Workflow:
#   # Po prvom spusteni dev pokial chces refresh:
#   cd C:\Users\radoslav.stompf\Documents\_FUERGY\TradingFuergy
#   git checkout dev
#   git pull origin dev
#   docker compose up -d --build trading-fuergy-dev
#
# Prod (8000) bezi nezavisle. Dev moze byt down/up bez vplyvu na prod.

[CmdletBinding()]
param(
    [string]$Branch  = "dev",
    [string]$Service = "trading-fuergy-dev"
)

$ErrorActionPreference = "Stop"

Write-Host "===================================================="  -ForegroundColor Cyan
Write-Host "  Trading Fuergy - Windows Setup (DEV port 8001)"      -ForegroundColor Cyan
Write-Host "===================================================="  -ForegroundColor Cyan
Write-Host ""

# 0) Over ze sme v repo
if (-not (Test-Path "docker-compose.yml")) {
    Write-Host "[X] Nie si v Trading Fuergy adresari." -ForegroundColor Red
    Write-Host "    cd C:\Users\radoslav.stompf\Documents\_FUERGY\TradingFuergy"
    exit 1
}

# 1) Skontroluj Docker (rychly check)
try {
    $dockerVersion = docker --version 2>&1
    Write-Host "   [OK] $dockerVersion"
} catch {
    Write-Host "   [X] Docker nebezi." -ForegroundColor Red
    exit 1
}

# 2) Pripravim data-dev/ adresar (samostatne od prod data/)
Write-Host ">> Pripravujem data-dev/ adresar" -ForegroundColor Yellow
@("data-dev\db", "data-dev\out", "data-dev\okte_credentials") | ForEach-Object {
    if (-not (Test-Path $_)) {
        New-Item -ItemType Directory -Path $_ -Force | Out-Null
        Write-Host "   + $_"
    }
}
@("data-dev\realio_cookies.json", "data-dev\seps_cookies.json", "data-dev\historian_cookies.json") | ForEach-Object {
    if (-not (Test-Path $_)) {
        Set-Content -Path $_ -Value "{}"
        Write-Host "   + $_ (prazdny)"
    }
}

# 3) .env musi mat DEV_PORT (defaultne 8001)
if (-not (Test-Path ".env")) {
    Write-Host "[X] .env neexistuje. Najprv setup_windows.ps1 (prod)." -ForegroundColor Red
    exit 1
}
$envContent = Get-Content ".env" -Raw
if ($envContent -notmatch "DEV_PORT") {
    Write-Host ">> Pridavam DEV_PORT=8001 do .env" -ForegroundColor Yellow
    Add-Content -Path ".env" -Value "`nDEV_PORT=8001"
}

# 4) Zabezpec ze dev branch existuje
git fetch origin 2>&1 | Out-Null
$devExists = git ls-remote --heads origin $Branch
if (-not $devExists) {
    Write-Host ">> Branch '$Branch' este nie je na remote. Vytvor ju z refactor-v2:" -ForegroundColor Yellow
    Write-Host "    git checkout refactor-v2"
    Write-Host "    git checkout -b $Branch"
    Write-Host "    git push -u origin $Branch"
    Write-Host ""
    Read-Host "Stlac ENTER ked je dev branch na remote (alebo Ctrl+C na ukoncenie)"
}

# 5) Build dev service (Dockerfile rovnaky, len iny tag + bind mount)
Write-Host ">> Buildujem $Service (z dev branch po pulle v containeri pri starte)..." -ForegroundColor Yellow
docker compose build $Service

Write-Host ">> Spustam $Service..." -ForegroundColor Yellow
docker compose up -d $Service

# 6) Health check
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

# 7) DB init (idempotentne)
Write-Host ">> Inicializujem dev DB schemu..." -ForegroundColor Yellow
docker compose exec -T $Service alembic upgrade head 2>&1 | ForEach-Object { Write-Host "   $_" }

Write-Host ">> Seedujem dev admin user-a..." -ForegroundColor Yellow
docker compose exec -T $Service python tools/seed_admin.py --username admin --password admin --role admin --update 2>&1 | ForEach-Object { Write-Host "   $_" }

# 8) Restart pre cisty stav
docker compose restart $Service
Start-Sleep -Seconds 15
$finalStatus = docker inspect --format '{{.State.Health.Status}}' $Service 2>$null

Write-Host ""
if ($finalStatus -eq "healthy") {
    Write-Host "[OK] Dev environment bezi!" -ForegroundColor Green
    $port = if ($env:DEV_PORT) { $env:DEV_PORT } else { "8001" }
    Write-Host "     Otvor http://localhost:$port  (dev, samostatna DB!)" -ForegroundColor Green
    Write-Host "     Prod stale bezi na http://localhost:8000"
    Write-Host "     Login: admin / admin"
} else {
    Write-Host "[!] Health check po restarte: $finalStatus" -ForegroundColor Yellow
    Write-Host "    docker compose logs --tail 50 $Service"
}

Write-Host ""
Write-Host "===================================================="  -ForegroundColor Cyan
Write-Host "  Dev workflow:"                                       -ForegroundColor Cyan
Write-Host "===================================================="  -ForegroundColor Cyan
Write-Host "  # Po push do dev branch z Macu:"
Write-Host "  git fetch origin"
Write-Host "  git checkout $Branch"
Write-Host "  git pull origin $Branch"
Write-Host "  docker compose up -d --build $Service"
Write-Host ""
Write-Host "  # Sleduj dev logy:"
Write-Host "  docker compose logs -f $Service"
Write-Host ""
Write-Host "  # Zastavit dev (prod ostava):"
Write-Host "  docker compose stop $Service"
Write-Host ""
Write-Host "  # Promote dev -> prod (po overeni):"
Write-Host "  git checkout refactor-v2"
Write-Host "  git merge --no-ff $Branch"
Write-Host "  git push origin refactor-v2"
Write-Host "  docker compose up -d --build trading-fuergy   # prod rebuild"
