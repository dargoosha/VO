[CmdletBinding()]param([double]$Rate,[ValidateSet(1,10)][int]$BatchSize=1,[string]$Prefix='step8')
$ErrorActionPreference='Stop';$Root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path;$Out=Join-Path $Root 'results\lab4';$Ns='prescriptions-lab3';$Python=(Resolve-Path (Join-Path $Root '..\Scripts\python.exe')).Path
function Sql([string]$Query){$cmd='psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "," -c "'+$Query.Replace('"','\"')+'"';$v=kubectl -n $Ns exec postgres-0 -- sh -ec $cmd;if($LASTEXITCODE-ne 0){throw 'SQL failed'};return $v}
& (Join-Path $PSScriptRoot 'set-mode.ps1') -Mode locked -BatchSize $BatchSize -PollMs 100 -FailRate 0 -ResetTasks
kubectl -n $Ns scale deployment/worker --replicas=3|Out-Null;kubectl -n $Ns rollout status deployment/worker --timeout=180s|Out-Null
$Count=[math]::Floor($Rate*120);$p=Start-Process $Python -ArgumentList @((Join-Path $PSScriptRoot 'task_generator.py'),'--ids',(Join-Path $Root 'results\lab3\ids.json'),'--count',$Count,'--rate',$Rate,'--output',(Join-Path $Out "${Prefix}_created.csv")) -PassThru -WindowStyle Hidden
Start-Sleep 30
$OldItems=@((kubectl -n $Ns get pods -l app=worker -o json|ConvertFrom-Json).items|Where-Object{$null-eq$_.metadata.deletionTimestamp})
$old=@($OldItems.metadata.name)
$ContainerMap=@($OldItems|ForEach-Object{[pscustomobject]@{pod=$_.metadata.name;node=$_.spec.nodeName;container_id=($_.status.containerStatuses[0].containerID-replace'^containerd://','')}})
$ContainerMap|Export-Csv (Join-Path $Out "${Prefix}_old_containers.csv") -NoTypeInformation -Encoding utf8
$Watchers=@();foreach($Node in @($ContainerMap.node|Select-Object -Unique)){
  $EventFile=Join-Path $Out "${Prefix}_${Node}_containerd_events.log";$ErrorFile=Join-Path $Out "${Prefix}_${Node}_containerd_events.err"
  $Watchers+=Start-Process docker -ArgumentList @('exec',$Node,'ctr','--namespace=k8s.io','events') -RedirectStandardOutput $EventFile -RedirectStandardError $ErrorFile -WindowStyle Hidden -PassThru
}
Start-Sleep 1
kubectl -n $Ns rollout restart deployment/worker|Out-Null
$snapshots=@();$logs=@();$deadline=(Get-Date).AddMinutes(3)
do{
  Start-Sleep 1;$pods=(kubectl -n $Ns get pods -l app=worker -o json|ConvertFrom-Json).items
  foreach($pod in $pods){$cs=@($pod.status.containerStatuses)[0];if($null-eq$cs){continue};$snapshots += [pscustomobject]@{captured_at=(Get-Date).ToString('o');pod=$pod.metadata.name;old=[int]($old-contains $pod.metadata.name);deleting=[bool]$pod.metadata.deletionTimestamp;phase=$pod.status.phase;ready=$cs.ready;restart_count=$cs.restartCount;terminated_exit_code=$cs.state.terminated.exitCode;terminated_reason=$cs.state.terminated.reason}}
  foreach($name in $old){$line=kubectl -n $Ns logs $name --tail=20 2>$null;if($LASTEXITCODE-eq 0){$logs += $line}}
  $ready=@($pods|Where-Object{$_.metadata.deletionTimestamp-eq$null -and @($_.status.containerStatuses)[0].ready}).Count
}while(($ready-lt 3 -or @($pods|Where-Object{$old-contains$_.metadata.name}).Count-gt 0)-and(Get-Date)-lt$deadline)
foreach($Watcher in $Watchers){if(-not$Watcher.HasExited){Stop-Process -Id $Watcher.Id -Force}}
$snapshots|Export-Csv (Join-Path $Out "${Prefix}_pod_states.csv") -NoTypeInformation -Encoding utf8
$logs|Select-Object -Unique|Set-Content (Join-Path $Out "${Prefix}_worker_shutdown.log") -Encoding utf8
$p.WaitForExit();$deadline=(Get-Date).AddMinutes(5);do{Start-Sleep 2;$left=[int](Sql "SELECT count(*) FROM tasks WHERE status IN ('queued','running');")}while($left-gt 0-and(Get-Date)-lt$deadline)
$audit=Sql "SELECT count(*) FILTER(WHERE status='failed'),count(*) FILTER(WHERE attempts>1),(SELECT count(*) FROM (SELECT task_id FROM task_results GROUP BY task_id HAVING count(*)>1)d),count(*) FILTER(WHERE status='done') FROM tasks;"
$returned=([regex]::Matches(($logs-join "`n"),'"returned_to_queue":\s*([1-9][0-9]*)')|ForEach-Object{[int]$_.Groups[1].Value}|Measure-Object -Sum).Sum;if($null-eq$returned){$returned=0}
$ExitCodes=[ordered]@{};$ExitCodeRows=@();foreach($Item in $ContainerMap){
  $EventFile=Join-Path $Out "${Prefix}_$($Item.node)_containerd_events.log"
  $Line=Get-Content $EventFile -ErrorAction SilentlyContinue|Where-Object{$_-match [regex]::Escape($Item.container_id)-and$_-match 'TaskExit|/tasks/exit'}|Select-Object -Last 1
  $Match=[regex]::Match([string]$Line,'exit_status["'' :=]+([0-9]+)')
  # containerd uses protobuf JSON: the default numeric value 0 is omitted.
  $Code=if($Match.Success){[int]$Match.Groups[1].Value}elseif($Line){0}else{$null}
  $ExitCodes[$Item.pod]=$Code
  $ExitCodeRows+=[pscustomobject]@{pod=$Item.pod;node=$Item.node;container_id=$Item.container_id;exit_code=$Code;source_event=[string]$Line}
}
$ExitCodeRows|Export-Csv (Join-Path $Out "${Prefix}_container_exit_codes.csv") -NoTypeInformation -Encoding utf8
$parts=$audit-split',';$summary=[ordered]@{rate=$Rate;batch_size=$BatchSize;duration_seconds=120;created=$Count;old_pods=$old;container_exit_codes=$ExitCodes;returned_to_queue=$returned;failed=[int]$parts[0];attempts_gt_1=[int]$parts[1];duplicate_result_tasks=[int]$parts[2];done=[int]$parts[3]}
$summary|ConvertTo-Json -Depth 5|Set-Content (Join-Path $Out "${Prefix}_summary.json") -Encoding utf8;$summary|ConvertTo-Json -Depth 5
