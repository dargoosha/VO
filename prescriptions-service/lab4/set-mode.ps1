[CmdletBinding()]
param(
    [ValidateSet("naive", "locked", "locked_no_skip")][string]$Mode = "locked",
    [ValidateSet(1, 10)][int]$BatchSize = 1,
    [ValidateSet(1, 100)][int]$PollMs = 100,
    [double]$FailRate = 0,
    [bool]$Heartbeat = $true,
    [switch]$ResetTasks
)
$ErrorActionPreference = "Stop"
$Namespace = "prescriptions-lab3"

function Invoke-Sql([string]$Sql) {
    $Bytes = [Text.Encoding]::UTF8.GetBytes($Sql)
    $Base64 = [Convert]::ToBase64String($Bytes)
    kubectl -n $Namespace exec postgres-0 -- sh -ec "echo '$Base64' | base64 -d | psql -v ON_ERROR_STOP=1 -U `$POSTGRES_USER -d `$POSTGRES_DB"
    if ($LASTEXITCODE -ne 0) { throw "SQL command failed" }
}

if ($ResetTasks) { Invoke-Sql "TRUNCATE task_results, tasks RESTART IDENTITY CASCADE;" }
if ($Mode -eq "naive") {
    Invoke-Sql @"
DO `$`$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='task_results'::regclass AND contype='p') THEN
    ALTER TABLE task_results DROP CONSTRAINT task_results_pkey;
  END IF;
END `$`$;
CREATE INDEX IF NOT EXISTS idx_task_results_task_id_nonunique ON task_results(task_id);
"@
} else {
    Invoke-Sql @"
DROP INDEX IF EXISTS idx_task_results_task_id_nonunique;
DELETE FROM task_results a USING task_results b WHERE a.ctid < b.ctid AND a.task_id=b.task_id;
DO `$`$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='task_results'::regclass AND contype='p') THEN
    ALTER TABLE task_results ADD PRIMARY KEY(task_id);
  END IF;
END `$`$;
"@
}

kubectl -n $Namespace set env deployment/worker "CLAIM_MODE=$Mode" "CLAIM_BATCH_SIZE=$BatchSize" `
    "T_POLL_MS=$PollMs" "FAIL_RATE=$FailRate" "HEARTBEAT_ENABLED=$($Heartbeat.ToString().ToLowerInvariant())"
if ($LASTEXITCODE -ne 0) { throw "Не вдалося оновити worker env" }
kubectl -n $Namespace rollout status deployment/worker --timeout=180s
if ($LASTEXITCODE -ne 0) { throw "Worker rollout не завершився" }
