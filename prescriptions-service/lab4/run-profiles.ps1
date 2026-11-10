[CmdletBinding()]
param([double]$X3)
$ErrorActionPreference='Stop'
$Root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path; $Out=Join-Path $Root 'results\lab4'; $Ns='prescriptions-lab3'
$Python=(Resolve-Path (Join-Path $Root '..\Scripts\python.exe')).Path
function Sql([string]$Query){$cmd='psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "," -c "'+$Query.Replace('"','\"')+'"';$v=kubectl -n $Ns exec postgres-0 -- sh -ec $cmd;if($LASTEXITCODE -ne 0){throw 'SQL failed'};return $v}
function Run-Profile([string]$Name,[double]$Rate,[int]$Duration){
  kubectl -n $Ns scale deployment/worker --replicas=3 | Out-Null
  & (Join-Path $PSScriptRoot 'set-mode.ps1') -Mode locked -BatchSize 1 -PollMs 100 -FailRate 0 -ResetTasks
  $Count=[math]::Floor($Rate*$Duration)
  $Start=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
  $p=Start-Process -FilePath $Python -ArgumentList @((Join-Path $PSScriptRoot 'task_generator.py'),'--ids',(Join-Path $Root 'results\lab3\ids.json'),'--count',$Count,'--rate',$Rate,'--output',(Join-Path $Out "${Name}_created.csv")) -PassThru -WindowStyle Hidden
  $samples=@()
  do {
    Start-Sleep 1; $p.Refresh()
    $state=Sql "SELECT count(*) FILTER(WHERE status='queued'),count(*) FILTER(WHERE status='running'),count(*) FILTER(WHERE status='done') FROM tasks;"
    $s=$state -split ','
    $samples += [pscustomobject]@{unix_second=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds();queued=[int]$s[0];running=[int]$s[1];done=[int]$s[2]}
  } while(-not $p.HasExited)
  $End=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); $EndQueued=$samples[-1].queued
  $samples | Export-Csv (Join-Path $Out "${Name}_queue.csv") -NoTypeInformation -Encoding utf8
  $deadline=(Get-Date).AddMinutes(8)
  do {Start-Sleep 2;$left=[int](Sql "SELECT count(*) FROM tasks WHERE status IN ('queued','running');")}while($left -gt 0 -and (Get-Date)-lt $deadline)
  if($left -gt 0){throw "$Name did not drain"}
  if($Name -eq 'step7_steady'){
    $WindowStart=$End-120
    $db=Sql "SELECT count(*)/120.0,avg(extract(epoch from started_at-created_at)) FROM tasks WHERE extract(epoch from created_at)>=$WindowStart AND extract(epoch from created_at)<=$End;"
    $d=$db -split ','; $lambda=[double]$d[0];$wq=[double]$d[1]
    $window=@($samples|Where-Object unix_second -ge $WindowStart);$lq=($window|Measure-Object queued -Average).Average
    $summary=[ordered]@{name=$Name;target_rate=$Rate;duration_seconds=$Duration;created=$Count;window_seconds=120;lambda=$lambda;Wq_seconds=$wq;lambda_Wq=$lambda*$wq;Lq_measured=$lq;discrepancy_percent=if($lq){100*[math]::Abs($lq-$lambda*$wq)/$lq}else{0};start=$Start;end=$End}
  }else{
    $first=Sql "SELECT avg(extract(epoch from started_at-created_at)) FROM (SELECT * FROM tasks ORDER BY created_at,id LIMIT 100)s;"
    $last=Sql "SELECT avg(extract(epoch from started_at-created_at)) FROM (SELECT * FROM tasks ORDER BY created_at DESC,id DESC LIMIT 100)s;"
    $summary=[ordered]@{name=$Name;target_rate=$Rate;duration_seconds=$Duration;created=$Count;queue_at_end=$EndQueued;Wq_first100_seconds=[double]$first;Wq_last100_seconds=[double]$last;start=$Start;end=$End}
  }
  $summary|ConvertTo-Json|Set-Content (Join-Path $Out "${Name}_summary.json") -Encoding utf8
  $summary|ConvertTo-Json|Write-Host
}
Run-Profile 'step7_steady' (0.7*$X3) 180
Run-Profile 'step7_overload' (1.3*$X3) 120
