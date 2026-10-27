[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("Fixed", "Hpa")]
    [string]$Mode,
    [ValidateRange(1, 3)]
    [int]$Replicas = 3
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Namespace = "prescriptions-lab3"

if ($Mode -eq "Fixed") {
    kubectl -n $Namespace delete hpa web --ignore-not-found
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося видалити HPA." }
    kubectl -n $Namespace scale deployment/web --replicas=$Replicas
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося масштабувати web." }
    kubectl -n $Namespace rollout status deployment/web --timeout=180s
    if ($LASTEXITCODE -ne 0) { throw "web не досяг заданої кількості готових реплік." }
    Write-Host "Фіксований режим: $Replicas готових реплік."
} else {
    kubectl -n $Namespace delete hpa web --ignore-not-found
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося видалити попередній HPA." }
    kubectl -n $Namespace scale deployment/web --replicas=1
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося встановити одну репліку." }
    kubectl -n $Namespace rollout status deployment/web --timeout=180s
    if ($LASTEXITCODE -ne 0) { throw "Одна репліка web не стала Ready." }
    kubectl apply -f (Join-Path $ProjectRoot "k8s\50-hpa.yaml")
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося застосувати HPA." }
    Write-Host "HPA застосовано; очікую current/desired/ready=1 протягом 60 секунд."
    $StableSeconds = 0
    $ElapsedSeconds = 0
    while ($StableSeconds -lt 60 -and $ElapsedSeconds -lt 300) {
        Start-Sleep -Seconds 5
        $Current = kubectl -n $Namespace get hpa web -o jsonpath='{.status.currentReplicas}'
        $Desired = kubectl -n $Namespace get hpa web -o jsonpath='{.status.desiredReplicas}'
        $Ready = kubectl -n $Namespace get deployment web -o jsonpath='{.status.readyReplicas}'
        if ($Current -eq "1" -and $Desired -eq "1" -and $Ready -eq "1") {
            $StableSeconds += 5
        } else {
            $StableSeconds = 0
        }
        $ElapsedSeconds += 5
        Write-Host "current=$Current desired=$Desired ready=$Ready stable=${StableSeconds}s"
    }
    if ($StableSeconds -lt 60) {
        throw "HPA не утримував одну готову репліку 60 секунд протягом 5 хвилин."
    }
    Write-Host "HPA готовий до профільного прогону."
}
