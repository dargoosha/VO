[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("Starved", "Fixed")]
    [string]$Phase,
    [double]$StartRps = 200,
    [double]$StepRps = 200,
    [int]$MaxSteps = 8
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Namespace = "prescriptions-lab3"
$Python = (Get-Command python -ErrorAction Stop).Source
$Ids = Join-Path $ProjectRoot "results\lab3\ids.json"
$ResultRoot = Join-Path $ProjectRoot "results\lab3"
if (-not (Test-Path $Ids)) { throw "Не знайдено $Ids." }

if ($Phase -eq "Starved") {
    & (Join-Path $PSScriptRoot "d2.ps1") -Mode Starve
    $ReplicaValues = @(1, 2, 3)
    $Label = "starved"
} else {
    & (Join-Path $PSScriptRoot "d2.ps1") -Mode Fix
    $ReplicaValues = @(3)
    $Label = "fixed"
}

& (Join-Path $PSScriptRoot "verify.ps1")

kubectl -n $Namespace delete hpa web --ignore-not-found | Out-Null
foreach ($N in $ReplicaValues) {
    kubectl -n $Namespace scale deployment/web --replicas=$N
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося встановити N=$N." }
    kubectl -n $Namespace rollout status deployment/web --timeout=180s
    if ($LASTEXITCODE -ne 0) { throw "web N=$N не готовий." }
    Start-Sleep -Seconds 10
    $Prefix = Join-Path $ResultRoot "d2_${Label}_n${N}"
    & $Python (Join-Path $ProjectRoot "k8s_load_test.py") steps `
        --url http://localhost:8080 --ids $Ids --output $Prefix --replicas $N `
        --connections 24 --rotate-after 100 --warmup-seconds 10 `
        --start-rps $StartRps --step-rps $StepRps --step-seconds 30 `
        --window-seconds 20 --max-steps $MaxSteps
    if ($LASTEXITCODE -ne 0) { throw "Д2 генератор завершився з помилкою для N=$N." }
    Start-Sleep -Seconds 10
    & $Python (Join-Path $ProjectRoot "collect_lab3_metrics.py") `
        --meta "${Prefix}_meta.json" --output "${Prefix}_prometheus.csv"
    kubectl -n $Namespace logs -l app=web --prefix --tail=-1 |
        Set-Content -LiteralPath "${Prefix}_web.log" -Encoding utf8
    kubectl -n $Namespace logs postgres-0 --tail=-1 |
        Set-Content -LiteralPath "${Prefix}_postgres.log" -Encoding utf8
    $PgActivity = @(kubectl -n $Namespace exec postgres-0 -- sh -ec `
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT state, count(*) FROM pg_stat_activity GROUP BY state ORDER BY state"' 2>&1)
    $PgActivityExitCode = $LASTEXITCODE
    if ($PgActivityExitCode -ne 0) {
        @("query_failed_exit_code=$PgActivityExitCode") + $PgActivity |
            Set-Content -LiteralPath "${Prefix}_pg_stat_activity.txt" -Encoding utf8
    } else {
        $PgActivity | Set-Content -LiteralPath "${Prefix}_pg_stat_activity.txt" -Encoding utf8
    }
}

Write-Host "Д2 $Phase завершено. Після обох фаз виконайте d2.ps1 -Mode Restore."
