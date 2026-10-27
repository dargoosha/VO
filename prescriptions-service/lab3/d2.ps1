[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("Starve", "Fix", "Restore")]
    [string]$Mode
)

$ErrorActionPreference = "Stop"
$Namespace = "prescriptions-lab3"

function Apply-PostgresLimit([int]$Value) {
    kubectl -n $Namespace create configmap postgres-config `
        --from-literal="POSTGRES_MAX_CONNECTIONS=$Value" --dry-run=client -o yaml |
        kubectl apply -f -
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося оновити postgres-config." }
    kubectl -n $Namespace rollout restart statefulset/postgres
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося перезапустити PostgreSQL." }
    kubectl -n $Namespace rollout status statefulset/postgres --timeout=180s
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL не став Ready." }
}

function Apply-WebPool([int]$Value) {
    kubectl -n $Namespace create configmap web-config `
        --from-literal="DB_HOST=postgres" --from-literal="DB_POOL_SIZE=$Value" `
        --dry-run=client -o yaml | kubectl apply -f -
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося оновити web-config." }
    kubectl -n $Namespace rollout restart deployment/web
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося перезапустити web." }
    kubectl -n $Namespace rollout status deployment/web --timeout=180s
    if ($LASTEXITCODE -ne 0) { throw "web не став Ready." }
}

switch ($Mode) {
    "Starve" {
        Apply-WebPool 15
        Apply-PostgresLimit 35
        Write-Host "Д2: max_connections=35, DB_POOL_SIZE=15. Перевірте N=1,2,3 однаковим профілем."
    }
    "Fix" {
        Apply-PostgresLimit 35
        Apply-WebPool 10
        kubectl -n $Namespace scale deployment/web --replicas=3
        if ($LASTEXITCODE -ne 0) { throw "Не вдалося масштабувати web до трьох реплік." }
        kubectl -n $Namespace rollout status deployment/web --timeout=180s
        if ($LASTEXITCODE -ne 0) { throw "Три репліки web не стали Ready." }
        Write-Host "Д2: max_connections=35, DB_POOL_SIZE=10, replicas=3. Повторіть той самий прогін."
    }
    "Restore" {
        Apply-PostgresLimit 100
        Apply-WebPool 15
        Write-Host "Базові значення відновлено: max_connections=100, DB_POOL_SIZE=15."
    }
}

Write-Host "Фактичне значення PostgreSQL:"
kubectl -n $Namespace exec postgres-0 -- sh -ec 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SHOW max_connections"'
