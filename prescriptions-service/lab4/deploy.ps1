[CmdletBinding()]
param([string]$ClusterName = "prescriptions-lab3", [string]$EnvFile = ".env")
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Namespace = "prescriptions-lab3"
$EnvPath = (Resolve-Path (Join-Path $ProjectRoot $EnvFile)).Path

function Invoke-Checked([scriptblock]$Command, [string]$Description) {
    Write-Host "`n==> $Description"
    $global:LASTEXITCODE = 0
    & $Command
    $Code = $global:LASTEXITCODE
    if ($Code -ne 0) { throw "Помилка: $Description (код $Code)" }
}

function Import-KindImage([string]$Image) {
    if (Get-Command kind -ErrorAction SilentlyContinue) {
        Invoke-Checked { kind load docker-image $Image --name $ClusterName } "Завантаження $Image через kind"
        return
    }
    foreach ($Node in @("$ClusterName-control-plane", "$ClusterName-worker", "$ClusterName-worker2")) {
        Write-Host "`n==> Потоковий імпорт образу в $Node"
        docker image save $Image | docker exec -i $Node ctr --namespace=k8s.io images import -
        if ($LASTEXITCODE -ne 0) { throw "streamed ctr import failed for $Node" }
    }
}

Invoke-Checked { docker info | Out-Null } "Перевірка Docker Desktop"
Invoke-Checked { kubectl config use-context "kind-$ClusterName" | Out-Null } "Вибір Kubernetes context"
Invoke-Checked { kubectl wait nodes --all --for=condition=Ready --timeout=180s } "Очікування вузлів"
Invoke-Checked { docker build -t prescriptions-web:lab4 -f (Join-Path $ProjectRoot "Dockerfile") $ProjectRoot } "Збирання web"
Invoke-Checked { docker build -t prescriptions-worker:lab4 -f (Join-Path $ProjectRoot "Dockerfile.worker") $ProjectRoot } "Збирання worker"
Import-KindImage "prescriptions-web:lab4"
Import-KindImage "prescriptions-worker:lab4"

kubectl -n $Namespace create secret generic postgres-secret --from-env-file=$EnvPath --dry-run=client -o yaml |
    kubectl apply -f -
if ($LASTEXITCODE -ne 0) { throw "Не вдалося оновити postgres-secret" }

$Migration = Join-Path $PSScriptRoot "migration.sql"
kubectl -n $Namespace create configmap postgres-schema-lab4 --from-file="migration.sql=$Migration" --dry-run=client -o yaml |
    kubectl apply -f -
if ($LASTEXITCODE -ne 0) { throw "Не вдалося створити postgres-schema-lab4" }

Invoke-Checked { kubectl -n $Namespace delete job postgres-migration-lab4 --ignore-not-found } "Оновлення migration Job"
Invoke-Checked { kubectl apply -f (Join-Path $PSScriptRoot "migration-job.yaml") } "Міграція tasks/task_results"
Invoke-Checked { kubectl -n $Namespace wait --for=condition=complete job/postgres-migration-lab4 --timeout=180s } "Очікування міграції"
Invoke-Checked { kubectl -n $Namespace set image deployment/web web=prescriptions-web:lab4 } "Оновлення web"
Invoke-Checked { kubectl -n $Namespace set env deployment/web ALLOW_TASK_SEED_OVERRIDE=true } "Тестовий seed override"
Invoke-Checked { kubectl -n $Namespace rollout status deployment/web --timeout=180s } "Очікування web"
Invoke-Checked { kubectl apply -f (Join-Path $PSScriptRoot "worker.yaml") } "Розгортання worker"
Invoke-Checked { kubectl -n $Namespace rollout status deployment/worker --timeout=180s } "Очікування worker"
Invoke-Checked { kubectl apply -f (Join-Path $PSScriptRoot "prometheus-config.yaml") } "Конфігурація Prometheus"
Invoke-Checked { kubectl -n $Namespace rollout restart deployment/prometheus } "Перезапуск Prometheus"
Invoke-Checked { kubectl -n $Namespace rollout status deployment/prometheus --timeout=180s } "Очікування Prometheus"

$ResultRoot = Join-Path $ProjectRoot "results\lab4"
New-Item -ItemType Directory -Force -Path $ResultRoot | Out-Null
kubectl get nodes -o json | Set-Content -LiteralPath (Join-Path $ResultRoot "nodes.json") -Encoding utf8
kubectl -n $Namespace get pods,deploy,statefulset,svc -o json |
    Set-Content -LiteralPath (Join-Path $ResultRoot "workloads.json") -Encoding utf8
Write-Host "`nLab 4 розгорнуто. Web: http://localhost:8080, Prometheus: http://localhost:9090"
