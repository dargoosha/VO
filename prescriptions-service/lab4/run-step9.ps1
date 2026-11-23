[CmdletBinding()]param([double]$Rate,[ValidateSet('both','baseline','poison')][string]$Phase='both')
$ErrorActionPreference='Stop';$Root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path;$Out=Join-Path $Root 'results\lab4';$Ns='prescriptions-lab3';$Python=(Resolve-Path (Join-Path $Root '..\Scripts\python.exe')).Path
function Sql([string]$Query){$cmd='psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "," -c "'+$Query.Replace('"','\"')+'"';$v=kubectl -n $Ns exec postgres-0 -- sh -ec $cmd;if($LASTEXITCODE-ne 0){throw 'SQL failed'};return $v}
function Run-One([string]$Name,[int]$Poison){
 & (Join-Path $PSScriptRoot 'set-mode.ps1') -Mode locked -BatchSize 1 -PollMs 100 -FailRate 0.15 -ResetTasks
 kubectl -n $Ns scale deployment/worker --replicas=3|Out-Null;kubectl -n $Ns rollout restart deployment/worker|Out-Null;kubectl -n $Ns rollout status deployment/worker --timeout=180s|Out-Null
 $Count=[math]::Floor($Rate*300);$Start=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
 $GeneratorArgs=@((Join-Path $PSScriptRoot 'task_generator.py'),'--ids',(Join-Path $Root 'results\lab3\ids.json'),'--count',$Count,'--rate',$Rate,'--poison-count',$Poison,'--output',(Join-Path $Out "${Name}_created.csv"))
 if($Poison -gt 0){$GeneratorArgs += @('--poison-at-second','150')}
 $p=Start-Process $Python -ArgumentList $GeneratorArgs -PassThru -WindowStyle Hidden
 $samples=@();do{Start-Sleep 5;$p.Refresh();$state=Sql "SELECT count(*) FILTER(WHERE status='queued'),count(*) FILTER(WHERE status='running'),count(*) FILTER(WHERE status='done'),count(*) FILTER(WHERE status='failed') FROM tasks;";$s=$state-split',';$pods=(kubectl -n $Ns get pods -l app=worker -o json|ConvertFrom-Json).items;$samples += [pscustomobject]@{unix_second=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds();queued=[int]$s[0];running=[int]$s[1];done=[int]$s[2];failed=[int]$s[3];restarts=($pods.status.containerStatuses.restartCount|Measure-Object -Sum).Sum;ready=@($pods|Where-Object{$_.status.containerStatuses[0].ready}).Count}}while(-not$p.HasExited)
 $End=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds();$samples|Export-Csv (Join-Path $Out "${Name}_timeline.csv") -NoTypeInformation -Encoding utf8
 $queuedAtEnd=$samples[-1].queued; $doneAtEnd=$samples[-1].done
 $deadline=(Get-Date).AddMinutes(45);do{Start-Sleep 5;$left=[int](Sql "SELECT count(*) FROM tasks WHERE status IN ('queued','running');")}while($left-gt 0-and(Get-Date)-lt$deadline)
 $audit=Sql "SELECT count(*),count(*) FILTER(WHERE attempts>1),count(*) FILTER(WHERE attempts>1 AND seed<>0),count(*) FILTER(WHERE status='failed'),count(*) FILTER(WHERE status='failed' AND seed<>0),count(*) FILTER(WHERE status='done'),(SELECT count(*) FROM (SELECT task_id FROM task_results GROUP BY task_id HAVING count(*)>1)d),percentile_cont(0.99) WITHIN GROUP(ORDER BY extract(epoch from started_at-created_at)) FILTER(WHERE seed<>0 AND started_at IS NOT NULL) FROM tasks;";$a=$audit-split','
 $dist=Sql "SELECT attempts,count(*) FROM tasks GROUP BY attempts ORDER BY attempts;";Set-Content (Join-Path $Out "${Name}_attempts.csv") -Value ("attempts,count`n"+($dist-join"`n")) -Encoding utf8
 $poisonRows=Sql "SELECT id,status,attempts,extract(epoch from coalesce(finished_at,clock_timestamp())-created_at) FROM tasks WHERE seed=0 ORDER BY id;";Set-Content (Join-Path $Out "${Name}_poison.csv") -Value ("id,status,attempts,seconds_to_final`n"+($poisonRows-join"`n")) -Encoding utf8
 kubectl -n $Ns logs -l app=worker --prefix --since=30m|Set-Content (Join-Path $Out "${Name}_workers.log") -Encoding utf8
 $first=$samples[0];$last=$samples[-1];$x=($last.done-$first.done)/[math]::Max(1,$last.unix_second-$first.unix_second)
 $summary=[ordered]@{name=$Name;target_rate=$Rate;duration_seconds=300;created=[int]$a[0];poison_count=$Poison;poison_at_second=if($Poison){150}else{$null};queued_at_profile_end=$queuedAtEnd;done_at_profile_end=$doneAtEnd;attempts_gt_1=[int]$a[1];attempts_gt_1_nonpoison=[int]$a[2];attempts_gt_1_fraction_nonpoison=[int]$a[2]/[math]::Max(1,[int]$a[0]-$Poison);expected_attempts_gt_1_fraction=0.15;failed=[int]$a[3];failed_nonpoison=[int]$a[4];failed_fraction_nonpoison=[int]$a[4]/[math]::Max(1,[int]$a[0]-$Poison);expected_failed_fraction=0.003375;done=[int]$a[5];duplicate_result_tasks=[int]$a[6];p99_wait_nonpoison_seconds=[double]$a[7];restarts_max=($samples.restarts|Measure-Object -Maximum).Maximum;ready_min=($samples.ready|Measure-Object -Minimum).Minimum;X_during_profile=$x;drain_complete=($left-eq 0);queued_after_timeout=$left;start=$Start;end=$End}
 $summary|ConvertTo-Json|Set-Content (Join-Path $Out "${Name}_summary.json") -Encoding utf8;$summary|ConvertTo-Json|Write-Host
 if($left -gt 0){throw "$Name did not drain before timeout"}
}
try{
 if($Phase -in @('both','baseline')){Run-One 'step9_baseline' 0}
 if($Phase -in @('both','poison')){Run-One 'step9_poison' 10}
}finally{
 & (Join-Path $PSScriptRoot 'set-mode.ps1') -Mode locked -BatchSize 1 -PollMs 100 -FailRate 0
}
