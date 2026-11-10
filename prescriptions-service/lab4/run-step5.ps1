[CmdletBinding()]
param([string]$IdsFile = "results/lab3/ids.json")
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Out = Join-Path $Root "results\lab4"
$Ns = "prescriptions-lab3"
function Sql([string]$Query) {
    $cmd = 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "," -c "' + $Query.Replace('"','\"') + '"'
    $value = kubectl -n $Ns exec postgres-0 -- sh -ec $cmd
    if ($LASTEXITCODE -ne 0) { throw "SQL failed: $Query" }
    return $value
}
function Set-PodPaused([string]$PodName, [bool]$Paused) {
    $Node = kubectl -n $Ns get pod $PodName -o jsonpath='{.spec.nodeName}'
    $ContainerId = kubectl -n $Ns get pod $PodName -o jsonpath='{.status.containerStatuses[0].containerID}'
    $ContainerId = $ContainerId -replace '^containerd://',''
    $Action = if ($Paused) { 'pause' } else { 'resume' }
    docker exec $Node ctr --namespace=k8s.io tasks $Action $ContainerId | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "ctr tasks $Action failed for $PodName" }
}

& (Join-Path $PSScriptRoot "set-mode.ps1") -Mode locked -BatchSize 1 -PollMs 100 -FailRate 0 -ResetTasks
kubectl -n $Ns scale deployment/worker --replicas=2 | Out-Null
kubectl -n $Ns rollout status deployment/worker --timeout=180s | Out-Null
$Python = (Resolve-Path (Join-Path $Root "..\Scripts\python.exe")).Path
$Generator = Start-Process -FilePath $Python -ArgumentList @(
    (Join-Path $PSScriptRoot "task_generator.py"), "--ids", (Join-Path $Root $IdsFile),
    "--count", "100", "--rate", "100", "--output", (Join-Path $Out "step5_created.csv")
) -WindowStyle Hidden -PassThru

$Deadline = (Get-Date).AddSeconds(30)
$Caught = $false
$PodItems = (kubectl -n $Ns get pods -l app=worker -o json | ConvertFrom-Json).items
$Candidates = @($PodItems | Where-Object {
    $null -eq $_.metadata.deletionTimestamp -and $_.status.containerStatuses[0].ready
} | ForEach-Object { $_.metadata.name })
if ($Candidates.Count -ne 2) { throw "Очікувалося 2 Ready worker pods, отримано $($Candidates.Count)" }
$CandidateIndex = 0
do {
    Start-Sleep -Milliseconds 50
    $Pod = $Candidates[$CandidateIndex % $Candidates.Count]
    $CandidateIndex++
    Set-PodPaused $Pod $true
    $Before = Sql "SELECT id,worker_id,attempts,token,extract(epoch from lease_until) FROM tasks WHERE status='running' AND worker_id='$Pod' ORDER BY started_at DESC LIMIT 1;"
    if ($Before) {
        $Fields = $Before -split ','
        $TaskId = [long]$Fields[0]
        $Caught = $true
        break
    }
    Set-PodPaused $Pod $false
} while (-not $Caught -and (Get-Date) -lt $Deadline)
if (-not $Caught) { throw "Не вдалося призупинити task під час обробки" }
Set-Content -LiteralPath (Join-Path $Out "step5_before.csv") -Value "task_id,worker_id,attempts,token,lease_until_epoch`n$Before" -Encoding utf8
$StoppedAt = Get-Date
Start-Sleep -Seconds 30
Set-PodPaused $Pod $false
$Generator.WaitForExit()
$Deadline = (Get-Date).AddSeconds(120)
do {
    Start-Sleep -Seconds 1
    $State = Sql "SELECT status FROM tasks WHERE id=$TaskId;"
} while ($State -notin @("done", "failed") -and (Get-Date) -lt $Deadline)
$After = Sql "SELECT t.id,t.status,t.attempts,t.token,t.worker_id,coalesce(r.worker_id,''),count(r.task_id) FROM tasks t LEFT JOIN task_results r ON r.task_id=t.id WHERE t.id=$TaskId GROUP BY t.id,r.worker_id;"
Set-Content -LiteralPath (Join-Path $Out "step5_after.csv") -Value "task_id,status,attempts,token,current_worker,result_worker,result_count`n$After" -Encoding utf8
kubectl -n $Ns logs $Pod --timestamps | Set-Content -LiteralPath (Join-Path $Out "step5_stopped_worker.log") -Encoding utf8
$Evidence = [ordered]@{task_id=$TaskId; stopped_worker=$Pod; stopped_at=$StoppedAt.ToString('o'); resumed_at=(Get-Date).ToString('o'); before=$Before; after=$After}
$Evidence | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Out "step5_summary.json") -Encoding utf8
$Evidence | ConvertTo-Json
