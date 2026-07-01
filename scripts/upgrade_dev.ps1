# Trading Fuergy DEV (8001) — JEDEN spolahlivy upgrade VSETKYCH dev kontajnerov.
# Pouzitie:  .\scripts\upgrade_dev.ps1
# Spustaj z korena repa na Windows. PS 5.1 kompatibilne, ASCII-only.
#
# PRECO: app.py/livesim.py/vdt_live_advisor.py su ZAPECENE v image (mount je len core/tests/tools),
# takze "git pull" NESTACI — treba rebuild. A workery (vdt-trader, control-loop, watcher,
# supervisor) bezia z rovnakeho image -> po rebuilde ich treba RECREATE, inak bezia stary kod.
# Verify: GIT_COMMIT (peceny v Dockerfile) sa skontroluje v KAZDOM kontajneri == pulled HEAD.

$ErrorActionPreference = "Stop"

# VSETKY dev sluzby (web + workery). Kazda bezi z image trading-fuergy:dev.
$DevServices = @(
    "trading-fuergy-dev",
    "control-loop-dev",
    "profile-supervisor-dev",
    "vdt-watcher-dev",
    "vdt-trader-dev"
)

Write-Host "==> 1/5 git fetch + checkout dev + pull" -ForegroundColor Cyan
git fetch origin
git checkout dev
git pull origin dev
$expected = (git rev-parse --short HEAD).Trim()
$bt = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
Write-Host ("    dev HEAD: {0}" -f $expected) -ForegroundColor Yellow

Write-Host "==> 2/5 rebuild image trading-fuergy:dev (--no-cache)" -ForegroundColor Cyan
docker compose --profile dev build --no-cache --build-arg GIT_COMMIT=$expected --build-arg BUILD_TIME="$bt" trading-fuergy-dev

Write-Host "==> 3/5 recreate VSETKY dev sluzby (web + workery)" -ForegroundColor Cyan
docker compose --profile dev up -d --force-recreate $DevServices
Start-Sleep -Seconds 6

Write-Host "==> 4/5 15-min cenovy model (ak nie je v repe)" -ForegroundColor Cyan
if (Test-Path "out/price_model_15m.joblib") {
    Write-Host "    model je v repe (git) - trening netreba" -ForegroundColor Green
} else {
    try { docker exec trading-fuergy-dev python price_model_15m.py } catch {
        Write-Host "    model chyba aj trening zlyhal - app pojde na flat upsample" -ForegroundColor Yellow
    }
}

Write-Host ("==> 5/5 VERIFY GIT_COMMIT v KAZDOM kontajneri == {0}" -f $expected) -ForegroundColor Cyan
$fail = 0
foreach ($svc in $DevServices) {
    try { $gc = (docker exec $svc printenv GIT_COMMIT).Trim() } catch { $gc = "N/A" }
    if ($gc -eq $expected) {
        Write-Host ("    OK  {0,-24} {1}" -f $svc, $gc) -ForegroundColor Green
    } else {
        Write-Host ("    XX  {0,-24} {1}  (ocakavane {2})" -f $svc, $gc, $expected) -ForegroundColor Red
        $fail = 1
    }
}

Write-Host ""
if ($fail -eq 0) {
    Write-Host ("OK - VSETKY dev kontajnery bezia na {0}." -f $expected) -ForegroundColor Green
    Write-Host "   Testuj na 8001 (dev), NIE 8000 (prod je samostatny - upgrade_prod.ps1)." -ForegroundColor Yellow
    Write-Host "   Po zmene jadra: v /livesim sprav FULL re-sim (plan_batch full reset)." -ForegroundColor Yellow
} else {
    Write-Host "CHYBA - niektory kontajner NEBEZI na najnovsom kode (viz XX vyssie)." -ForegroundColor Red
    Write-Host "   Skontroluj: git rev-parse --short HEAD (si na dev?), docker compose logs <svc> --tail 50" -ForegroundColor Yellow
    exit 1
}
