param(
    [switch]$PauseKind
)

$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root

$processor = Get-CimInstance Win32_Processor | Select-Object -First 1
$computer = Get-CimInstance Win32_ComputerSystem
$operatingSystem = Get-CimInstance Win32_OperatingSystem
$dockerEngine = docker version --format '{{.Server.Version}}'

docker build -f Dockerfile.lab6 -t prescriptions-lab6:cpu .
if ($LASTEXITCODE -ne 0) {
    throw "Lab6 image build failed with exit code $LASTEXITCODE"
}

$common = @(
    '--rm',
    '--init',
    '--volume', "$($root):/workspace",
    '--workdir', '/workspace',
    '--env', "LAB6_HOST_PROCESSOR=$($processor.Name)",
    '--env', "LAB6_HOST_PHYSICAL_CORES=$($processor.NumberOfCores)",
    '--env', "LAB6_HOST_LOGICAL_CORES=$($processor.NumberOfLogicalProcessors)",
    '--env', "LAB6_HOST_MEMORY_BYTES=$($computer.TotalPhysicalMemory)",
    '--env', "LAB6_HOST_OS=$($operatingSystem.Caption) $($operatingSystem.Version) build $($operatingSystem.BuildNumber)",
    '--env', "LAB6_DOCKER_ENGINE=$dockerEngine",
    'prescriptions-lab6:cpu'
)

docker run @common python -m unittest -v lab6.test_lab6
if ($LASTEXITCODE -ne 0) {
    throw "Lab6 unit tests failed with exit code $LASTEXITCODE"
}

$kindNames = @(
    'prescriptions-lab3-control-plane',
    'prescriptions-lab3-worker',
    'prescriptions-lab3-worker2'
)
$pausedByScript = @()
try {
    if ($PauseKind) {
        foreach ($name in $kindNames) {
            $running = docker inspect --format '{{.State.Running}}' $name 2>$null
            if ($LASTEXITCODE -eq 0 -and $running -eq 'true') {
                docker pause $name | Out-Null
                $pausedByScript += $name
            }
        }
    }
    docker run @common python lab6/run_experiments.py
    if ($LASTEXITCODE -ne 0) {
        throw "Lab6 experiment series failed with exit code $LASTEXITCODE"
    }
}
finally {
    foreach ($name in $pausedByScript) {
        docker unpause $name | Out-Null
    }
}

