[CmdletBinding()]
param(
    [string]$ClusterName = "prescriptions-lab3",
    [string]$EnvFile = ".env"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$K8sRoot = Join-Path $ProjectRoot "k8s"
$Namespace = "prescriptions-lab3"
$EnvPath = (Resolve-Path (Join-Path $ProjectRoot $EnvFile)).Path
$MetricsServerManifest = Join-Path $K8sRoot "44-metrics-server.yaml"

function Assert-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Не знайдено команду '$Name'. Встановіть її та повторіть запуск."
    }
}

function Invoke-Checked([scriptblock]$Command, [string]$Description) {
    Write-Host "`n==> $Description"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Команда завершилася з кодом ${LASTEXITCODE}: $Description"
    }
}

Assert-Command docker
Assert-Command kind
Assert-Command kubectl

if (-not (Test-Path -LiteralPath $MetricsServerManifest)) {
    throw "Відсутній обов'язковий k8s\44-metrics-server.yaml; розгортання HPA було б неповним."
}

$EnvValues = @{}
foreach ($Line in Get-Content -LiteralPath $EnvPath) {
    $Trimmed = $Line.Trim()
    if (-not $Trimmed -or $Trimmed.StartsWith("#") -or -not $Trimmed.Contains("=")) { continue }
    $Pair = $Trimmed.Split("=", 2)
    $EnvValues[$Pair[0].Trim()] = $Pair[1].Trim()
}
foreach ($RequiredKey in @("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")) {
    if (-not $EnvValues.ContainsKey($RequiredKey) -or [string]::IsNullOrWhiteSpace($EnvValues[$RequiredKey])) {
        throw "У .env відсутнє непорожнє значення $RequiredKey."
    }
}

Invoke-Checked { docker info | Out-Null } "Перевірка Docker Desktop"

$Clusters = @(kind get clusters 2>$null)
if ($Clusters -notcontains $ClusterName) {
    Invoke-Checked {
        kind create cluster --name $ClusterName --config (Join-Path $K8sRoot "kind-config.yaml")
    } "Створення kind-кластера $ClusterName"
} else {
    Write-Host "Кластер $ClusterName уже існує; повторно використовую його без видалення даних."
}

Invoke-Checked { kubectl config use-context "kind-$ClusterName" | Out-Null } "Вибір контексту kind-$ClusterName"
Invoke-Checked { kubectl wait nodes --all --for=condition=Ready --timeout=180s } "Очікування готовності вузлів"
$Version = kubectl version -o json | ConvertFrom-Json
$Minor = [int](([string]$Version.serverVersion.minor) -replace "[^0-9].*$", "")
if ($Minor -lt 30) {
    throw "Kubernetes 1.30+ потрібен для lifecycle.preStop.sleep; сервер має $($Version.serverVersion.gitVersion)."
}

$Nodes = kubectl get nodes -o json | ConvertFrom-Json
$ControlPlanes = @($Nodes.items | Where-Object {
    $null -ne $_.metadata.labels.'node-role.kubernetes.io/control-plane'
})
$Workers = @($Nodes.items | Where-Object {
    $null -eq $_.metadata.labels.'node-role.kubernetes.io/control-plane'
})
if ($ControlPlanes.Count -ne 1 -or $Workers.Count -ne 2) {
    throw "Кластер $ClusterName має неправильну топологію: control-plane=$($ControlPlanes.Count), worker=$($Workers.Count). Видаліть його через .\lab3\destroy.ps1 і створіть знову."
}
$NotReady = @($Nodes.items | Where-Object {
    (@($_.status.conditions | Where-Object type -eq "Ready").status) -ne "True"
})
if ($NotReady.Count -gt 0) {
    throw "Не всі вузли кластера перебувають у стані Ready."
}

$InfraNode = kubectl get nodes -l node-role.kubernetes.io/control-plane -o jsonpath='{.items[0].metadata.name}'
if (-not $InfraNode) {
    throw "Не знайдено control-plane вузол."
}
Invoke-Checked { kubectl label node $InfraNode role=infra --overwrite } "Мітка role=infra для $InfraNode"

Invoke-Checked { docker build --tag prescriptions-web:lab3 $ProjectRoot } "Збирання образу web"
Invoke-Checked { kind load docker-image prescriptions-web:lab3 --name $ClusterName } "Завантаження образу у kind"

Invoke-Checked { kubectl apply -f (Join-Path $K8sRoot "00-namespace.yaml") } "Створення namespace"

Write-Host "`n==> Створення Secret з .env без запису пароля до репозиторію"
kubectl -n $Namespace create secret generic postgres-secret --from-env-file=$EnvPath --dry-run=client -o yaml |
    kubectl apply -f -
if ($LASTEXITCODE -ne 0) { throw "Не вдалося створити postgres-secret." }

Write-Host "`n==> Створення ConfigMap зі схемою БД"
$SchemaPath = Join-Path $ProjectRoot "db\init.sql"
kubectl -n $Namespace create configmap postgres-schema --from-file="init.sql=$SchemaPath" --dry-run=client -o yaml |
    kubectl apply -f -
if ($LASTEXITCODE -ne 0) { throw "Не вдалося створити postgres-schema." }

Invoke-Checked { kubectl apply -f (Join-Path $K8sRoot "10-postgres.yaml") } "Розгортання PostgreSQL"
Invoke-Checked { kubectl -n $Namespace rollout status statefulset/postgres --timeout=180s } "Очікування PostgreSQL"

Invoke-Checked {
    kubectl -n $Namespace delete job postgres-migration --ignore-not-found | Out-Null
} "Видалення попереднього Job міграції"
Invoke-Checked { kubectl apply -f (Join-Path $K8sRoot "20-migration-job.yaml") } "Запуск міграції"
Invoke-Checked { kubectl -n $Namespace wait --for=condition=complete job/postgres-migration --timeout=180s } "Очікування міграції"

Invoke-Checked { kubectl apply -f (Join-Path $K8sRoot "30-web.yaml") } "Розгортання web"
Invoke-Checked { kubectl -n $Namespace rollout status deployment/web --timeout=180s } "Очікування web"

Invoke-Checked { kubectl apply -f (Join-Path $K8sRoot "40-monitoring-rbac.yaml") } "RBAC моніторингу"
Invoke-Checked { kubectl apply -f (Join-Path $K8sRoot "41-prometheus.yaml") } "Розгортання Prometheus"
Invoke-Checked { kubectl apply -f (Join-Path $K8sRoot "42-kube-state-metrics.yaml") } "Розгортання kube-state-metrics"
Invoke-Checked { kubectl apply -f (Join-Path $K8sRoot "43-postgres-exporter.yaml") } "Розгортання postgres-exporter"

Invoke-Checked { kubectl apply -f $MetricsServerManifest } "Розгортання metrics-server"

Invoke-Checked { kubectl -n $Namespace rollout status deployment/prometheus --timeout=180s } "Очікування Prometheus"
Invoke-Checked { kubectl -n $Namespace rollout status deployment/kube-state-metrics --timeout=180s } "Очікування kube-state-metrics"
Invoke-Checked { kubectl -n $Namespace rollout status deployment/postgres-exporter --timeout=180s } "Очікування postgres-exporter"
Invoke-Checked { kubectl -n kube-system rollout status deployment/metrics-server --timeout=180s } "Очікування metrics-server"
Invoke-Checked {
    kubectl wait --for=condition=Available apiservice/v1beta1.metrics.k8s.io --timeout=180s
} "Очікування Metrics API"

$TopReady = $false
for ($Attempt = 1; $Attempt -le 12; $Attempt++) {
    kubectl -n $Namespace top pods *> $null
    if ($LASTEXITCODE -eq 0) {
        $TopReady = $true
        break
    }
    Start-Sleep -Seconds 5
}
if (-not $TopReady) {
    throw "Metrics API доступний, але kubectl top pods не повернув дані протягом 60 секунд."
}

Write-Host "`n==> Базові HTTP-перевірки"
$Live = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:8080/livez" -TimeoutSec 10
$Health = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:8080/healthz" -TimeoutSec 10
$Prometheus = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:9090/-/ready" -TimeoutSec 10
if ($Live.StatusCode -ne 200 -or $Health.StatusCode -ne 200 -or $Prometheus.StatusCode -ne 200) {
    throw "Одна з HTTP-перевірок не повернула 200."
}

Write-Host "`nРозгортання завершено."
kubectl get nodes -o wide
kubectl -n $Namespace get pods,services,statefulsets,deployments,jobs -o wide
Write-Host "web: http://localhost:8080  Prometheus: http://localhost:9090"

$ResultRoot = Join-Path $ProjectRoot "results\lab3"
New-Item -ItemType Directory -Force -Path $ResultRoot | Out-Null
$Environment = [ordered]@{
    captured_at = (Get-Date).ToString("o")
    kind = ((kind version) -join "`n")
    kubectl = (kubectl version -o json | ConvertFrom-Json)
    docker = (docker version --format '{{json .}}' | ConvertFrom-Json)
    nodes = (kubectl get nodes -o json | ConvertFrom-Json).items | ForEach-Object {
        [ordered]@{
            name = $_.metadata.name
            kubelet_version = $_.status.nodeInfo.kubeletVersion
            container_runtime = $_.status.nodeInfo.containerRuntimeVersion
        }
    }
}
$Environment | ConvertTo-Json -Depth 8 |
    Set-Content -LiteralPath (Join-Path $ResultRoot "environment.json") -Encoding utf8
