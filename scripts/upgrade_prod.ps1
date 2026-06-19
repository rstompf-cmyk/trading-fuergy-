# Trading Fuergy PROD (8000) — upgrade na najnovsi refactor-v2 + 15-min model.
# Pouzitie:  .\scripts\upgrade_prod.ps1
# Spustaj z korena repa na Windows. PS 5.1 kompatibilne, ASCII-only.
# POZN: prod bezi z branchu refactor-v2 (nie dev). Najprv treba na Macu spustit
#       deploy_prod.sh (dev -> refactor-v2 FF push).

param(
    [string]$Service = "trading-fuergy",
    [int]$Port = 8000
)
$ErrorActionPreference = "Stop"

Write-Host "==> 1/6 git fetch + checkout refactor-v2 + pull" -ForegroundColor Cyan
git fetch origin
git checkout refactor-v2
git pull origin refactor-v2
$expected = (git rev-parse --short HEAD)
$bt = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
Write-Host ("    prod commit: {0}" -f $expected) -ForegroundColor Yellow

Write-Host "==> 2/6 rebuild $Service (--no-cache)" -ForegroundColor Cyan
docker compose build --no-cache --build-arg GIT_COMMIT=$expected --build-arg BUILD_TIME="$bt" $Service

Write-Host "==> 3/6 up -d" -ForegroundColor Cyan
docker compose up -d $Service
Start-Sleep -Seconds 6

Write-Host "==> 4/6 15-min cenovy model" -ForegroundColor Cyan
if (Test-Path "out/price_model_15m.joblib") {
    Write-Host "    model je pribaleny v repe (git) - trening netreba" -ForegroundColor Green
} else {
    try {
        docker exec $Service python price_model_15m.py
    } catch {
        Write-Host "    model chyba aj trening zlyhal (historian?) - app pojde na flat upsample" -ForegroundColor Yellow
    }
}

Write-Host "==> 5/6 verify novy kod v kontajneri (grep markery)" -ForegroundColor Cyan
$m1 = (docker exec $Service grep -c "price_model_15m" /app/app.py)
$m2 = (docker exec $Service grep -c "BUG 15-MIN-DTPROF" /app/livesim.py)
Write-Host ("    app.py 15-min model wire-in : {0}" -f $m1)
Write-Host ("    livesim.py DTPROF fix       : {0}" -f $m2)

Write-Host "==> 6/6 hotovo" -ForegroundColor Cyan
if (([int]$m1 -ge 1) -and ([int]$m2 -ge 1)) {
    Write-Host ""
    Write-Host ("OK - PROD 8000 bezi na novom kode ({0})." -f $expected) -ForegroundColor Green
    Write-Host "   DALSI KROK: pre kazdy zivy profil FULL prepocet (plan_batch full reset / re-sim)," -ForegroundColor Yellow
    Write-Host "   aby sa effect_db prepocital z opraveneho DT (stare karty maju 4x z cache)."
    Write-Host "   POZOR: 15-min sa stane primarnym aj na PRODE (default mod + plan_batch default 15)."
} else {
    Write-Host ""
    Write-Host "CHYBA - novy kod nie je v kontajneri. Build nezdvihol zmenu." -ForegroundColor Red
    Write-Host "   Skontroluj: cat .git/HEAD (refactor-v2?), docker compose logs $Service --tail 50"
    exit 1
}
