[CmdletBinding()]
param([string]$IdsFile = "results/lab3/ids.json")
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ResultRoot = Join-Path $ProjectRoot "results\lab4"
New-Item -ItemType Directory -Force -Path $ResultRoot | Out-Null
$Ids = Get-Content -Raw (Join-Path $ProjectRoot $IdsFile) | ConvertFrom-Json
if (@($Ids).Count -lt 1) { throw "IDs file is empty" }
$RecordId = [int]$Ids[0]
$Created = Invoke-RestMethod -Method Post -Uri "http://localhost:8080/prescriptions/$RecordId/tasks"
$Deadline = (Get-Date).AddSeconds(45)
do {
    Start-Sleep -Milliseconds 500
    $Task = Invoke-RestMethod -Uri "http://localhost:8080/tasks/$($Created.id)"
} while ($Task.status -notin @("done", "failed") -and (Get-Date) -lt $Deadline)
if ($Task.status -ne "done" -or $null -eq $Task.result) { throw "Smoke task did not complete: $($Task | ConvertTo-Json -Compress)" }
$Metrics = (Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:8080/metrics").Content
if ($Metrics -notmatch "tasks_queued" -or $Metrics -notmatch "tasks_running") { throw "Web task metrics missing" }
$Evidence = [ordered]@{captured_at=(Get-Date).ToString("o"); created=$Created; completed=$Task}
$Evidence | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $ResultRoot "smoke.json") -Encoding utf8
kubectl -n prescriptions-lab3 logs deployment/worker --tail=100 |
    Set-Content -LiteralPath (Join-Path $ResultRoot "smoke-worker.log") -Encoding utf8
Write-Host "Smoke test passed for task $($Created.id)"
