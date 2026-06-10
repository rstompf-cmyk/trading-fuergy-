# Trading Fuergy - rychly deploy bez --no-cache (Bug #662)
# Pouziti: .\scripts\deploy_fast.ps1 [-Service trading-fuergy|trading-fuergy-dev]
#
# Pouzije Docker cache:
#  - requirements.txt sa nezmenil  -> pip install layer reuse (najvacsia uspora)
#  - .py kod sa zmenil             -> rebuild len COPY layer (~10-30s)
# Cely deploy typicky: 30-60s namiesto 5+ min.
#
# Pouzi `deploy.ps1` (s --no-cache) iba ked:
#  - menis requirements.txt
#  - menis Dockerfile (system deps, Python verziu)
#  - cache je podozrivo stara / posypana

param(
    [string]$Service = "trading-fuergy",
    [int]$Port = 8000,
    [string]$Branch = "refactor-v2"
)

$ErrorActionPreference = "Stop"

Write-Host "==> 1/6 git pull ($Branch)" -ForegroundColor Cyan
git pull origin $Branch
$expected_commit = (git rev-parse --short HEAD)
$build_time = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
Write-Host "    expected commit: $expected_commit" -ForegroundColor Yellow

Write-Host "==> 2/6 docker compose build (s cache, build-arg GIT_COMMIT)" -ForegroundColor Cyan
docker compose build --build-arg GIT_COMMIT=$expected_commit --build-arg BUILD_TIME="$build_time" $Service

Write-Host "==> 3/6 docker compose stop + rm (zachovaj cache)" -ForegroundColor Cyan
docker compose stop $Service
docker compose rm -f $Service

Write-Host "==> 4/6 docker compose up -d" -ForegroundColor Cyan
docker compose up -d $Service

Write-Host "==> 5/6 wait for healthy (max 60s)" -ForegroundColor Cyan
$tries = 0
$version_data = $null
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
if ($null -eq $version_data) {
    Write-Host ""
    Write-Host "❌ /version unreachable - kontajner sa nestartoval" -ForegroundColor Red
    Write-Host "   docker compose logs $Service --tail 100" -ForegroundColor Yellow
    exit 1
}
$container_commit = $version_data.git_commit
Write-Host "    container commit: $container_commit"
Write-Host "    expected commit:  $expected_commit"
if ($container_commit -eq $expected_commit) {
    Write-Host ""
    Write-Host "✅ DEPLOY OK — kontajner bezi na $container_commit" -ForegroundColor Green
    Write-Host "   Build time: $($version_data.build_time)" -ForegroundColor Gray
} else {
    Write-Host ""
    Write-Host "❌ COMMIT MISMATCH — kontajner ma $container_commit, ocakavam $expected_commit" -ForegroundColor Red
    Write-Host "   Skuste fallback: .\scripts\deploy.ps1 -Service $Service  (full --no-cache rebuild)" -ForegroundColor Yellow
    exit 1
}
