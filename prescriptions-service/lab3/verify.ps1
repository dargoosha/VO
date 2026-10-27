[CmdletBinding()]
param([switch]$Persistence)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Namespace = "prescriptions-lab3"
$ResultName = if ($Persistence) { "smoke_persistence.json" } else { "smoke_crud.json" }
$ResultPath = Join-Path $ProjectRoot "results\lab3\$ResultName"
$BaseUrl = "http://localhost:8080"
$PrometheusUrl = "http://localhost:9090"
$Checks = [ordered]@{}

function Invoke-Api([string]$Method, [string]$Uri, [string]$Body = "") {
    $Parameters = @{
        Method = $Method
        Uri = $Uri
        UseBasicParsing = $true
        TimeoutSec = 15
    }
    if ($Body) {
        $Parameters.ContentType = "application/json"
        $Parameters.Body = $Body
    }
    Invoke-WebRequest @Parameters
}

function Get-PrometheusResult([string]$Query) {
    $Encoded = [uri]::EscapeDataString($Query)
    $Response = Invoke-RestMethod -Uri "$PrometheusUrl/api/v1/query?query=$Encoded" -TimeoutSec 15
    if ($Response.status -ne "success" -or @($Response.data.result).Count -eq 0) {
        throw "Prometheus не повернув ряд для запиту: $Query"
    }
    @($Response.data.result)
}

$Payload = @{
    patient_code = "LAB3-SMOKE"
    drug = "Test drug"
    dose = 1.25
    unit = "mg"
    frequency_per_day = 2
    start_date = "2026-10-26"
    duration_days = 7
} | ConvertTo-Json -Compress

$Create = Invoke-Api POST "$BaseUrl/prescriptions" $Payload
if ($Create.StatusCode -ne 201) { throw "POST повернув $($Create.StatusCode), очікувалося 201." }
$Created = $Create.Content | ConvertFrom-Json
$Checks.create = @{ status = $Create.StatusCode; id = $Created.id; pod = $Create.Headers["X-Pod"] }

$Read = Invoke-Api GET "$BaseUrl/prescriptions/$($Created.id)"
if ($Read.StatusCode -ne 200) { throw "GET повернув $($Read.StatusCode), очікувалося 200." }
$Checks.read = @{ status = $Read.StatusCode; pod = $Read.Headers["X-Pod"] }

$UpdatedPayload = ($Payload | ConvertFrom-Json)
$UpdatedPayload.drug = "Updated test drug"
$Update = Invoke-Api PUT "$BaseUrl/prescriptions/$($Created.id)" ($UpdatedPayload | ConvertTo-Json -Compress)
if ($Update.StatusCode -ne 200) { throw "PUT повернув $($Update.StatusCode), очікувалося 200." }
$Checks.update = @{ status = $Update.StatusCode; pod = $Update.Headers["X-Pod"] }

if ($Persistence) {
    kubectl -n $Namespace delete pod postgres-0 --wait=true
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося видалити pod PostgreSQL." }
    kubectl -n $Namespace wait --for=condition=Ready pod/postgres-0 --timeout=180s
    if ($LASTEXITCODE -ne 0) { throw "Новий pod PostgreSQL не став Ready." }
    kubectl -n $Namespace rollout status deployment/web --timeout=180s
    if ($LASTEXITCODE -ne 0) { throw "web не відновив готовність після перезапуску PostgreSQL." }
    $AfterRestart = Invoke-Api GET "$BaseUrl/prescriptions/$($Created.id)"
    if ($AfterRestart.StatusCode -ne 200) { throw "Дані не прочитано після перезапуску PostgreSQL." }
    $Checks.persistence = @{ status = $AfterRestart.StatusCode; pod = $AfterRestart.Headers["X-Pod"] }
}

$Delete = Invoke-Api DELETE "$BaseUrl/prescriptions/$($Created.id)"
if ($Delete.StatusCode -ne 204) { throw "DELETE повернув $($Delete.StatusCode), очікувалося 204." }
$Checks.delete = @{ status = $Delete.StatusCode; pod = $Delete.Headers["X-Pod"] }

$Checks.max_connections = kubectl -n $Namespace exec postgres-0 -- sh -ec 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SHOW max_connections"'
$Checks.nodes = @(kubectl get nodes -o wide)
$Checks.workloads = @(kubectl -n $Namespace get pods -o wide)
$TargetsResponse = Invoke-RestMethod -Uri "$PrometheusUrl/api/v1/targets" -TimeoutSec 15
$RequiredJobs = @("web", "kubernetes-cadvisor", "kube-state-metrics", "postgres-exporter")
$TargetSummary = @{}
foreach ($Job in $RequiredJobs) {
    $Targets = @($TargetsResponse.data.activeTargets | Where-Object scrapePool -eq $Job)
    $UpTargets = @($Targets | Where-Object health -eq "up")
    if ($Targets.Count -eq 0 -or $UpTargets.Count -ne $Targets.Count) {
        throw "Prometheus job '$Job' не має всіх цілей у стані UP."
    }
    $TargetSummary[$Job] = @{ total = $Targets.Count; up = $UpTargets.Count }
}
$Checks.prometheus_targets = $TargetSummary

$Queries = [ordered]@{
    web_up = 'up{job="web"}'
    cfs_periods = 'container_cpu_cfs_periods_total{namespace="prescriptions-lab3",container="web"}'
    cfs_throttled = 'container_cpu_cfs_throttled_periods_total{namespace="prescriptions-lab3",container="web"}'
    ready_replicas = 'kube_deployment_status_replicas_ready{namespace="prescriptions-lab3",deployment="web"}'
    postgres_up = 'pg_up'
    postgres_activity = 'pg_stat_activity_count'
}
$QuerySummary = @{}
foreach ($Entry in $Queries.GetEnumerator()) {
    $Result = Get-PrometheusResult $Entry.Value
    $QuerySummary[$Entry.Key] = @{ series = $Result.Count }
    if ($Entry.Key -eq "postgres_up" -and -not ($Result | Where-Object { [double]$_.value[1] -eq 1 })) {
        throw "postgres-exporter доступний, але pg_up не дорівнює 1."
    }
}
$Checks.prometheus_queries = $QuerySummary

$TopOutput = @(kubectl -n $Namespace top pods)
if ($LASTEXITCODE -ne 0 -or $TopOutput.Count -eq 0) {
    throw "kubectl top pods не повернув дані."
}
$Checks.top_pods = $TopOutput

$Checks | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ResultPath -Encoding utf8
Write-Host "Smoke test пройдено. Результат: $ResultPath"
