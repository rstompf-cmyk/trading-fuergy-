# Trading Fuergy DEV (8001) — upgrade na najnovsi dev branch + 15-min model.
# Pouzitie:  .\scripts\upgrade_dev.ps1
# Spustaj z korena repa na Windows. PS 5.1 kompatibilne, ASCII-only.

param(
    [string]$Service = "trading-fuergy-dev",
    [int]$Port = 8001
)
$ErrorActionPreference = "Stop"

Write-Host "==> 1/6 git fetch + checkout dev + pull" -ForegroundColor Cyan
git fetch origin
git checkout dev
git pull origin dev
$expected = (git rev-parse --short HEAD)
$bt = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
Write-Host ("    dev commit: {0}" -f $expected) -ForegroundColor Yellow

Write-Host "==> 2/6 rebuild $Service (--no-cache)" -ForegroundColor Cyan
docker compose --profile dev build --no-cache --build-arg GIT_COMMIT=$expected --build-arg BUILD_TIME="$bt" $Service

Write-Host "==> 3/6 up -d" -ForegroundColor Cyan
docker compose --profile dev up -d $Service
Start-Sleep -Seconds 6

Write-Host "==> 4/6 generuj 15-min cenovy model (nepovinne, fallback ak chyba historian)" -ForegroundColor Cyan
try {
    docker exec $Service python price_model_15m.py
} catch {
    Write-Host "    (model sa nevygeneroval - app pojde na flat upsample; pozri historian data)" -ForegroundColor Yellow
}

Write-Host "==> 5/6 verify novy kod v kontajneri (grep markery)" -ForegroundColor Cyan
$m1 = (docker exec $Service grep -c "price_model_15m" /app/app.py)
$m2 = (docker exec $Service grep -c "BUG 15-MIN-DTPROF" /app/livesim.py)
$m3 = (docker exec $Service sh -c "test -f tools/export_livesim_xlsx.py && echo 1 || echo 0")
Write-Host ("    app.py 15-min model wire-in : {0}" -f $m1)
Write-Host ("    livesim.py DTPROF fix       : {0}" -f $m2)
Write-Host ("    export tool pritomny        : {0}" -f $m3)

Write-Host "==> 6/6 hotovo" -ForegroundColor Cyan
if (([int]$m1 -ge 1) -and ([int]$m2 -ge 1)) {
    Write-Host ""
    Write-Host ("OK - DEV 8001 bezi na novom kode ({0})." -f $expected) -ForegroundColor Green
    Write-Host "   DALSI KROK: v /livesim sprav FULL prepocet profilu (plan_batch full reset / re-sim)," -ForegroundColor Yellow
    Write-Host "   aby sa effect_db prepocital z opraveneho DT (stare karty maju 4x hodnoty z cache)."
} else {
    Write-Host ""
    Write-Host "CHYBA - novy kod nie je v kontajneri. Build nezdvihol zmenu." -ForegroundColor Red
    Write-Host "   Skontroluj: cat .git/HEAD (si na dev?), docker compose logs $Service --tail 50"
    exit 1
}
