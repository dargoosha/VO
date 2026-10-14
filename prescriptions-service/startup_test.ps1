$results = @()

for ($i = 1; $i -le 3; $i++) {
    docker compose down -v | Out-Null

    $timer = [System.Diagnostics.Stopwatch]::StartNew()

    docker compose up -d --scale web=1 | Out-Null

    while ($true) {
        try {
            $response = Invoke-WebRequest `
                -Uri "http://localhost:8080/healthz" `
                -TimeoutSec 2

            if ($response.StatusCode -eq 200) {
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 200
        }
    }

    $timer.Stop()
    $seconds = [math]::Round($timer.Elapsed.TotalSeconds, 2)

    Write-Host "Run $i : $seconds seconds"

    $results += [PSCustomObject]@{
        Run = $i
        Seconds = $seconds
    }
}

$results | Export-Csv `
    -Path "results/startup_times.csv" `
    -NoTypeInformation `
    -Encoding UTF8

$results | Format-Table