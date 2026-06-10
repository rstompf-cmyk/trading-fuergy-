# Trading Fuergy — robustný deploy s verifikáciou commit hashu
# Použitie: .\scripts\deploy.ps1 [-Service trading-fuergy|trading-fuergy-dev]
# Default: trading-fuergy (prod port 8000)

param(
    [string]$Service = "trading-fuergy",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

Write-Host "==> 1/6 git pull" -ForegroundColor Cyan
git pull origin refactor-v2
$expected_commit = (git rev-parse --short HEAD)
$build_time = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
Write-Host "    expected commit: $expected_commit" -ForegroundColor Yellow

Write-Host "==> 2/6 docker compose down $Service" -ForegroundColor Cyan
docker compose stop $Service
docker compose rm -f $Service

Write-Host "==> 3/6 docker compose build --no-cache (s GIT_COMMIT build-arg)" -ForegroundColor Cyan
docker compose build --no-cache --build-arg GIT_COMMIT=$expected_commit --build-arg BUILD_TIME="$build_time" $Service

Write-Host "==> 4/6 docker compose up -d" -ForegroundColor Cyan
docker compose up -d $Service

Write-Host "==> 5/6 wait for healthy (max 60s)" -ForegroundColor Cyan
$tries = 0
while ($tries -lt 30) {
    Start-Sleep -Seconds 2
    $tries++
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:$Port/version" -UseBasicParsing -TimeoutSec 3
        if ($resp.StatusCode -eq 200) {
            $version_data = $resp.Content | ConvertFrom-Json
            Write-Host "    container started, got /version response" -ForegroundColor Green
            break
        }
    } catch {
        Write-Host "    waiting... ($tries/30)" -ForegroundColor Gray
    }
}

Write-Host "==> 6/6 verify commit match" -ForegroundColor Cyan
$container_commit = $version_data.git_commit
Write-Host "    container commit: $container_commit"
Write-Host "    expected commit:  $expected_commit"
if ($container_commit -eq $expected_commit) {
    Write-Host ""
    Write-Host "✅ DEPLOY OK — kontajner beží na $container_commit" -ForegroundColor Green
    Write-Host "   Build time: $($version_data.build_time)" -ForegroundColor Gray
} else {
    Write-Host ""
    Write-Host "❌ COMMIT MISMATCH — kontajner má $container_commit, očakávam $expected_commit" -ForegroundColor Red
    Write-Host "   Pravdepodobne build použil stary kontext alebo neproshla pull. Skontroluj:" -ForegroundColor Yellow
    Write-Host "   - git status (nepushnuté zmeny?)"
    Write-Host "   - docker compose logs $Service --tail 50"
    exit 1
}
