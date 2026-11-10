[CmdletBinding()]
param([ValidateSet("Lab4", "Restore")][string]$Mode)
$ErrorActionPreference = "Stop"
$Nodes = @("prescriptions-lab3-worker", "prescriptions-lab3-worker2")
foreach ($Node in $Nodes) {
    if ($Mode -eq "Lab4") {
        docker exec $Node sh -ec "test -f /var/lib/kubelet/config.yaml.pre-lab4-backoff || cp /var/lib/kubelet/config.yaml /var/lib/kubelet/config.yaml.pre-lab4-backoff; sed -i 's/^crashLoopBackOff: {}$/crashLoopBackOff:\n  maxContainerRestartPeriod: 2s/' /var/lib/kubelet/config.yaml; systemctl restart kubelet"
    } else {
        docker exec $Node sh -ec "test -f /var/lib/kubelet/config.yaml.pre-lab4-backoff; cp /var/lib/kubelet/config.yaml.pre-lab4-backoff /var/lib/kubelet/config.yaml; systemctl restart kubelet"
    }
    if ($LASTEXITCODE -ne 0) { throw "Не вдалося змінити kubelet config на $Node" }
}
kubectl wait nodes --all --for=condition=Ready --timeout=180s
if ($LASTEXITCODE -ne 0) { throw "Вузли не повернулися у Ready" }
