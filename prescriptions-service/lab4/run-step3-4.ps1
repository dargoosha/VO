[CmdletBinding()]
param([ValidateSet('all','step3','step4')][string]$Phase = 'all')

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Out = Join-Path $Root 'results\lab4'
$Ns = 'prescriptions-lab3'
$Python = (Resolve-Path (Join-Path $Root '..\Scripts\python.exe')).Path
$Ids = Join-Path $Root 'results\lab3\ids.json'

function Invoke-Sql([string]$Query) {
    $Bytes = [Text.Encoding]::UTF8.GetBytes($Query)
    $Base64 = [Convert]::ToBase64String($Bytes)
    $Value = kubectl -n $Ns exec postgres-0 -- sh -ec "echo '$Base64' | base64 -d | psql -v ON_ERROR_STOP=1 -U `$POSTGRES_USER -d `$POSTGRES_DB -At -F ','"
    if ($LASTEXITCODE -ne 0) { throw "SQL failed: $Query" }
    return $Value
}

function Get-WorkerMetrics([string[]]$Pods) {
    $Code = "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9000/metrics',timeout=5).read().decode())"
    $Text = foreach ($Pod in $Pods) {
        kubectl -n $Ns exec $Pod -- python -c $Code
        if ($LASTEXITCODE -ne 0) { throw "Cannot read metrics from $Pod" }
    }
    return ($Text -join "`n")
}

function Get-Counter([string]$Metrics, [string]$Metric, [string]$LabelExpression) {
    $Pattern = '(?m)^' + [regex]::Escape($Metric) + '\{[^\r\n]*' + $LabelExpression + '[^\r\n]*\}\s+([0-9.eE+-]+)$'
    return ([regex]::Matches($Metrics, $Pattern) | ForEach-Object { [double]$_.Groups[1].Value } | Measure-Object -Sum).Sum
}

function Get-HistogramQuantile([string]$Metrics, [string]$Step, [double]$Quantile) {
    $Buckets = @{}
    foreach ($Line in ($Metrics -split "`r?`n")) {
        if ($Line -notmatch '^worker_task_duration_seconds_bucket\{(.+)\}\s+([0-9.eE+-]+)$') { continue }
        $Labels, $Value = $Matches[1], [double]$Matches[2]
        if ($Labels -notmatch ('step="' + [regex]::Escape($Step) + '"')) { continue }
        if ($Labels -notmatch 'le="([^"]+)"') { continue }
        $Le = $Matches[1]
        if (-not $Buckets.ContainsKey($Le)) { $Buckets[$Le] = 0.0 }
        $Buckets[$Le] += $Value
    }
    if (-not $Buckets.ContainsKey('+Inf') -or $Buckets['+Inf'] -le 0) { return $null }
    $Target = $Quantile * $Buckets['+Inf']
    $PreviousLe = 0.0
    $PreviousCount = 0.0
    foreach ($Le in @($Buckets.Keys | Where-Object { $_ -ne '+Inf' } | ForEach-Object { [double]$_ } | Sort-Object)) {
        $Count = $Buckets[$Le.ToString([Globalization.CultureInfo]::InvariantCulture)]
        if ($Count -ge $Target) {
            if ($Count -le $PreviousCount) { return $Le }
            return $PreviousLe + ($Le - $PreviousLe) * ($Target - $PreviousCount) / ($Count - $PreviousCount)
        }
        $PreviousLe = $Le
        $PreviousCount = $Count
    }
    return $PreviousLe
}

function Run-Series([string]$Mode, [int]$PollMs, [string]$Prefix, [bool]$SampleLocks) {
    kubectl -n $Ns scale deployment/worker --replicas=3 | Out-Null
    & (Join-Path $PSScriptRoot 'set-mode.ps1') -Mode $Mode -BatchSize 1 -PollMs $PollMs -FailRate 0 -ResetTasks
    $Pods = @((kubectl -n $Ns get pods -l app=worker -o json | ConvertFrom-Json).items |
        Where-Object { $null -eq $_.metadata.deletionTimestamp } |
        ForEach-Object { $_.metadata.name })
    if ($Pods.Count -ne 3) { throw "Expected 3 worker pods, got $($Pods.Count)" }

    $MetricsBefore = Get-WorkerMetrics $Pods
    $EmptyBefore = Get-Counter $MetricsBefore 'worker_claims_total' 'result="empty"'
    $CreatedFile = Join-Path $Out "${Prefix}_created.csv"
    $Generator = Start-Process $Python -ArgumentList @(
        (Join-Path $PSScriptRoot 'task_generator.py'), '--ids', $Ids,
        '--count', '300', '--rate', '100', '--output', $CreatedFile
    ) -PassThru -WindowStyle Hidden

    $Samples = @()
    $Clock = [Diagnostics.Stopwatch]::StartNew()
    $NextTick = 0.0
    $Left = 1
    $Deadline = (Get-Date).AddMinutes(5)
    do {
        $Captured = Get-Date
        if ($SampleLocks) {
            $State = Invoke-Sql "SELECT (SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock'),(SELECT count(*) FROM tasks WHERE status IN ('queued','running'));"
            $Parts = $State -split ','
            $Samples += [pscustomobject]@{
                captured_at = $Captured.ToString('o')
                elapsed_seconds = [math]::Round($Clock.Elapsed.TotalSeconds, 6)
                locks = [int]$Parts[0]
                queued_or_running = [int]$Parts[1]
            }
            $Left = [int]$Parts[1]
        } else {
            $Left = [int](Invoke-Sql "SELECT count(*) FROM tasks WHERE status IN ('queued','running');")
        }
        $Generator.Refresh()
        $NextTick += 1.0
        $Delay = $NextTick - $Clock.Elapsed.TotalSeconds
        if ($Delay -gt 0) { Start-Sleep -Milliseconds ([int][math]::Round(1000 * $Delay)) }
    } while ((-not $Generator.HasExited -or $Left -gt 0) -and (Get-Date) -lt $Deadline)

    $Generator.WaitForExit()
    if ($Generator.ExitCode -ne 0) { throw "$Prefix generator failed with code $($Generator.ExitCode)" }
    if ($Left -gt 0) { throw "$Prefix did not drain" }

    $Logs = kubectl -n $Ns logs -l app=worker --prefix --tail=-1
    if ($LASTEXITCODE -ne 0) { throw "Cannot collect worker logs for $Prefix" }
    $LogFile = Join-Path $Out "${Prefix}_workers.log"
    $Logs | Set-Content -LiteralPath $LogFile -Encoding utf8
    $LogText = $Logs -join "`n"
    $DoneIds = [regex]::Matches($LogText, '"event":\s*"task_done"[^\r\n]*"task_id":\s*([0-9]+)') |
        ForEach-Object { [int]$_.Groups[1].Value }
    $DuplicateLogTasks = @($DoneIds | Group-Object | Where-Object Count -gt 1).Count

    $Db = (Invoke-Sql "SELECT count(*),(SELECT count(*) FROM (SELECT task_id FROM task_results GROUP BY task_id HAVING count(*)>1)d),(SELECT count(*) FROM task_results) FROM tasks;") -split ','
    $N = [int]$Db[0]
    $DuplicateTableTasks = [int]$Db[1]
    $ResultRows = [int]$Db[2]
    $MetricsAfter = Get-WorkerMetrics $Pods
    $EmptyAfter = Get-Counter $MetricsAfter 'worker_claims_total' 'result="empty"'
    $ClaimP99 = Get-HistogramQuantile $MetricsAfter 'claim' 0.99

    $Summary = [ordered]@{
        n = $N
        n_dup_table = $DuplicateTableTasks
        D_table = $DuplicateTableTasks / [math]::Max(1, $N)
        n_dup_logs = $DuplicateLogTasks
        D_logs = $DuplicateLogTasks / [math]::Max(1, $N)
        task_done_events = @($DoneIds).Count
        result_rows = $ResultRows
        claim_p99_seconds = $ClaimP99
        empty_claims = [math]::Max(0, $EmptyAfter - $EmptyBefore)
    }

    if ($SampleLocks) {
        $Samples | Export-Csv (Join-Path $Out "${Prefix}_locks.csv") -NoTypeInformation -Encoding utf8
        $Intervals = for ($Index = 1; $Index -lt $Samples.Count; $Index++) {
            [double]$Samples[$Index].elapsed_seconds - [double]$Samples[$Index - 1].elapsed_seconds
        }
        $Summary['lock_samples'] = $Samples.Count
        $Summary['mean_lock_sample_interval_seconds'] = if ($Intervals) { ($Intervals | Measure-Object -Average).Average } else { $null }
        $Summary['max_lock_sample_interval_seconds'] = if ($Intervals) { ($Intervals | Measure-Object -Maximum).Maximum } else { $null }
        $Summary['max_lock_waiters'] = if ($Samples) { ($Samples.locks | Measure-Object -Maximum).Maximum } else { $null }
    }

    [pscustomobject]$Summary | Export-Csv (Join-Path $Out "${Prefix}_summary.csv") -NoTypeInformation -Encoding utf8
    $Summary | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $Out "${Prefix}_summary.json") -Encoding utf8
    $Summary | ConvertTo-Json -Depth 5 | Write-Host
}

try {
    if ($Phase -in @('all','step3')) {
        Run-Series 'naive' 100 'step3_naive_100ms' $false
        Run-Series 'naive' 1 'step3_naive_1ms' $false
    }
    if ($Phase -in @('all','step4')) {
        Run-Series 'locked' 1 'step4_skip_locked' $false
        Run-Series 'locked_no_skip' 1 'step4_no_skip' $true
    }
} finally {
    & (Join-Path $PSScriptRoot 'set-mode.ps1') -Mode locked -BatchSize 1 -PollMs 100 -FailRate 0
}
