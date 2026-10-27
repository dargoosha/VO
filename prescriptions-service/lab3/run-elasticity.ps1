[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Namespace = "prescriptions-lab3"
$Python = (Get-Command python -ErrorAction Stop).Source
$ResultRoot = Join-Path $ProjectRoot "results\lab3"
$ProfilePath = Join-Path $ResultRoot "elasticity_profile.json"
$Ids = Join-Path $ResultRoot "ids.json"
$SummaryPath = Join-Path $ResultRoot "analysis_summary.json"
if (-not (Test-Path $SummaryPath)) {
    throw "Не знайдено $SummaryPath. Спочатку виконайте фіксовану серію та analyze_lab3.py."
}
$Summary = Get-Content -LiteralPath $SummaryPath -Raw | ConvertFrom-Json
$X1 = [double]$Summary.kubernetes_capacity.'1'
$X3 = [double]$Summary.kubernetes_capacity.'3'
if ($X1 -le 0 -or $X3 -le 0) {
    throw "analysis_summary.json не містить придатних Kubernetes X(1) та X(3)."
}
$LowRps = [math]::Round(0.3 * $X1, 3)
$SpikeRps = [math]::Round([math]::Min(2.0 * $X1, 0.9 * $X3), 3)

@(
    @{ name = "low_before"; duration_seconds = 30; target_rps = $LowRps },
    @{ name = "spike"; duration_seconds = 60; target_rps = $SpikeRps },
    @{ name = "low_after"; duration_seconds = 90; target_rps = $LowRps }
) | ConvertTo-Json | Set-Content -LiteralPath $ProfilePath -Encoding utf8

if (-not (Test-Path $Ids)) {
    throw "Не знайдено $Ids. Спочатку виконайте seed.py з output results/lab3/ids.json."
}

& (Join-Path $PSScriptRoot "verify.ps1")

& (Join-Path $PSScriptRoot "set-hpa-mode.ps1") -Mode Fixed -Replicas 3
$Fixed = Join-Path $ResultRoot "elasticity_fixed"
& $Python (Join-Path $ProjectRoot "k8s_load_test.py") profile `
    --url http://localhost:8080 --ids $Ids `
    --output $Fixed --replicas 3 --connections 24 --rotate-after 100 --profile-file $ProfilePath
if ($LASTEXITCODE -ne 0) { throw "Фіксований профіль завершився з помилкою." }
Start-Sleep -Seconds 10
& $Python (Join-Path $ProjectRoot "collect_lab3_metrics.py") `
    --meta "${Fixed}_meta.json" --output "${Fixed}_prometheus.csv"
if ($LASTEXITCODE -ne 0) { throw "Не вдалося зібрати метрики фіксованого профілю." }

& (Join-Path $PSScriptRoot "set-hpa-mode.ps1") -Mode Hpa
$Hpa = Join-Path $ResultRoot "elasticity_hpa"
& $Python (Join-Path $ProjectRoot "k8s_load_test.py") profile `
    --url http://localhost:8080 --ids $Ids `
    --output $Hpa --replicas 3 --connections 24 --rotate-after 100 --profile-file $ProfilePath
if ($LASTEXITCODE -ne 0) { throw "HPA-профіль завершився з помилкою." }
Start-Sleep -Seconds 10
& $Python (Join-Path $ProjectRoot "collect_lab3_metrics.py") `
    --meta "${Hpa}_meta.json" --output "${Hpa}_prometheus.csv" --require-hpa
if ($LASTEXITCODE -ne 0) { throw "Не вдалося зібрати метрики HPA-профілю." }

Write-Host "Профілі завершено: low=$LowRps RPS, spike=$SpikeRps RPS."
