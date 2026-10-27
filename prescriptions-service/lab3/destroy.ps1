[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "High")]
param([string]$ClusterName = "prescriptions-lab3")

$ErrorActionPreference = "Stop"
if (-not (Get-Command kind -ErrorAction SilentlyContinue)) {
    throw "Не знайдено kind у PATH."
}
if ($PSCmdlet.ShouldProcess("kind cluster $ClusterName", "Повністю видалити кластер разом із PVC")) {
    kind delete cluster --name $ClusterName
    if ($LASTEXITCODE -ne 0) { throw "kind delete cluster завершився з помилкою." }
}
