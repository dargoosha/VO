[CmdletBinding()]
param([ValidateSet(1,10)][int]$BatchSize = 1, [string]$Prefix = "step6", [int[]]$Workers = @(1,2,3))
$ErrorActionPreference = "Stop"
$Root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Out=Join-Path $Root 'results\lab4'; $Ns='prescriptions-lab3'
$Python=(Resolve-Path (Join-Path $Root '..\Scripts\python.exe')).Path
function Sql([string]$Query) {
  $cmd='psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "," -c "'+$Query.Replace('"','\"')+'"'
  $v=kubectl -n $Ns exec postgres-0 -- sh -ec $cmd
  if($LASTEXITCODE -ne 0){throw 'SQL failed'}; return $v
}
function Prom([string]$Query) {
  $uri='http://localhost:9090/api/v1/query?query='+[uri]::EscapeDataString($Query)
  return (Invoke-RestMethod $uri).data.result
}
$summaries=@()
foreach($W in $Workers) {
  kubectl -n $Ns scale deployment/worker --replicas=0 | Out-Null
  kubectl -n $Ns rollout status deployment/worker --timeout=180s | Out-Null
  & (Join-Path $PSScriptRoot 'set-mode.ps1') -Mode locked -BatchSize $BatchSize -PollMs 100 -FailRate 0 -ResetTasks
  & $Python (Join-Path $PSScriptRoot 'task_generator.py') --ids (Join-Path $Root 'results\lab3\ids.json') --count 400 --rate 200 --output (Join-Path $Out "${Prefix}_w${W}_created.csv")
  $RunStart=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
  kubectl -n $Ns scale deployment/worker --replicas=$W | Out-Null
  kubectl -n $Ns rollout status deployment/worker --timeout=180s | Out-Null
  $deadline=(Get-Date).AddSeconds(240)
  do { Start-Sleep 1; $done=[int](Sql "SELECT count(*) FROM tasks WHERE status='done';") } while($done -lt 400 -and (Get-Date) -lt $deadline)
  if($done -lt 400){throw "W=$W completed only $done"}
  $RunEnd=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
  $series=Sql "WITH b AS (SELECT date_trunc('second',min(started_at)) first_sec,date_trunc('second',max(finished_at)) last_sec FROM tasks) SELECT extract(epoch from sec)::bigint,coalesce(c,0) FROM b,generate_series(first_sec,last_sec,interval '1 second') sec LEFT JOIN (SELECT date_trunc('second',created_at) s,count(*) c FROM task_results GROUP BY 1) x ON x.s=sec ORDER BY sec;"
  Set-Content (Join-Path $Out "${Prefix}_w${W}_seconds.csv") -Value ("unix_second,completed`n"+($series -join "`n")) -Encoding utf8
  $summary=Sql "WITH b AS (SELECT date_trunc('second',min(started_at)) first_sec,date_trunc('second',max(finished_at)) last_sec,extract(epoch from max(finished_at)-min(started_at)) active_seconds FROM tasks),p AS (SELECT sec,coalesce(c,0)::numeric c FROM b,generate_series(first_sec,last_sec,interval '1 second') sec LEFT JOIN (SELECT date_trunc('second',created_at) s,count(*) c FROM task_results GROUP BY 1) x ON x.s=sec),p30 AS (SELECT sec,coalesce(c,0)::numeric c FROM generate_series((SELECT date_trunc('second',max(created_at))-interval '29 second' FROM task_results),(SELECT date_trunc('second',max(created_at)) FROM task_results),interval '1 second') sec LEFT JOIN (SELECT date_trunc('second',created_at) s,count(*) c FROM task_results GROUP BY 1) x ON x.s=sec) SELECT round(400/(SELECT active_seconds FROM b),6),round(stddev_pop(c),6),(SELECT active_seconds FROM b),round((SELECT avg(c) FROM p30),6) FROM p;"
  $parts=$summary -split ','
  $claim=Prom 'histogram_quantile(0.99,sum by (le)(worker_task_duration_seconds_bucket{step="claim"}))'
  $dbx=Prom 'sum(rate(pg_stat_database_xact_commit[1m])+rate(pg_stat_database_xact_rollback[1m]))'
  $dbcpu=Prom 'sum(rate(container_cpu_usage_seconds_total{namespace="prescriptions-lab3",pod="postgres-0",container="postgres"}[1m]))'
  $row=[ordered]@{workers=$W;batch=$BatchSize;X_active=[double]$parts[0];std_active=[double]$parts[1];active_seconds=[double]$parts[2];X_30s_reference=[double]$parts[3];tasks=400;run_start=$RunStart;run_end=$RunEnd;claim_p99_seconds=if($claim){[double]$claim[0].value[1]}else{$null};db_transactions_per_second=if($dbx){[double]$dbx[0].value[1]}else{$null};db_cpu_cores=if($dbcpu){[double]$dbcpu[0].value[1]}else{$null}}
  $row | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $Out "${Prefix}_w${W}_summary.json") -Encoding utf8
  $summaries += [pscustomobject]$row
}
$summaries | Export-Csv (Join-Path $Out "${Prefix}_summary.csv") -NoTypeInformation -Encoding utf8
$summaries | Format-Table | Out-String | Write-Host
