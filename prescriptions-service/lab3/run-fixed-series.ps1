[CmdletBinding()]
param(
    [double]$StartRps = 200,
    [double]$StepRps = 200,
    [int]$MaxSteps = 15
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Namespace = "prescriptions-lab3"
$Python = (Get-Command python -ErrorAction Stop).Source
$Generator = Join-Path $ProjectRoot "k8s_load_test.py"
$Collector = Join-Path $ProjectRoot "collect_lab3_metrics.py"
$Ids = Join-Path $ProjectRoot "results\lab3\ids.json"
$ResultRoot = Join-Path $ProjectRoot "results\lab3"

if (-not (Test-Path $Ids)) {
    throw "Не знайдено $Ids. Спочатку виконайте seed.py з output results/lab3/ids.json."
}

& (Join-Path $PSScriptRoot "verify.ps1")
if ($LASTEXITCODE -ne 0) { throw "Передекспериментальна перевірка завершилася з помилкою." }

kubectl -n $Namespace delete hpa web --ignore-not-found
if ($LASTEXITCODE -ne 0) { throw "Не вдалося вимкнути HPA." }

$Runs = @(
    @{ Replicas = 1; Repetitions = 1 },
    @{ Replicas = 2; Repetitions = 1 },
    @{ Replicas = 3; Repetitions = 3 }
)

foreach ($Item in $Runs) {
    $N = $Item.Replicas
    kubectl -n $Namespace scale deployment/web --replicas=$N
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося масштабувати web до N=$N." }
    kubectl -n $Namespace rollout status deployment/web --timeout=180s
    if ($LASTEXITCODE -ne 0) { throw "N=$N не став Ready." }
    Start-Sleep -Seconds 15
    for ($Run = 1; $Run -le $Item.Repetitions; $Run++) {
        if ($Run -gt 1) { Start-Sleep -Seconds 30 }
        $Prefix = Join-Path $ResultRoot "fixed_n${N}_run${Run}"
        & $Python $Generator steps --url http://localhost:8080 --ids $Ids `
            --output $Prefix --replicas $N --connections 24 --rotate-after 100 `
            --warmup-seconds 10 --start-rps $StartRps --step-rps $StepRps `
            --step-seconds 30 --window-seconds 20 --max-steps $MaxSteps
        if ($LASTEXITCODE -ne 0) { throw "Генератор завершився з помилкою для N=$N, run=$Run." }
        Start-Sleep -Seconds 10
        & $Python $Collector --meta "${Prefix}_meta.json" --output "${Prefix}_prometheus.csv"
        if ($LASTEXITCODE -ne 0) { throw "Не вдалося зібрати Prometheus для N=$N, run=$Run." }
    }
}

Write-Host "Фіксовану серію завершено. Дані: $ResultRoot"
