param(
    [int]$Ticks = 1000,
    [int]$Interval = 10,
    [int]$Seed = 7,
    [switch]$NoSimulator
)

Set-Location -Path $PSScriptRoot

# 1) The Docker command line must exist.
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "Docker is not installed. Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and run this script again."
    exit 1
}

# Returns $true when the Docker engine answers.
function Test-DockerEngine {
    docker info 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
}

# 2) Start Docker Desktop when the engine is not running, then wait for it.
if (-not (Test-DockerEngine)) {
    $desktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path $desktop)) {
        Write-Host "The Docker engine is not running and Docker Desktop was not found. Start it manually and run this script again."
        exit 1
    }
    Write-Host "Starting Docker Desktop (this can take a minute)..."
    Start-Process $desktop
    $waited = 0
    while (-not (Test-DockerEngine)) {
        if ($waited -ge 180) {
            Write-Host "Docker did not become ready within 3 minutes. Open Docker Desktop, wait until it is running, then run this script again."
            exit 1
        }
        Start-Sleep -Seconds 3
        $waited += 3
    }
    Write-Host "Docker is ready."
}

# 3) Choose static data or live data with the simulator.
if ($NoSimulator) {
    $env:OCPI_DATA_FILE = ""
    $composeArgs = @("up", "--build")
}
else {
    $env:OCPI_DATA_FILE = "/var/lib/ocpi/OCPI_data.live.json"
    $env:SIM_TICKS = "$Ticks"
    $env:SIM_INTERVAL = "$Interval"
    $env:SIM_SEED = "$Seed"
    $composeArgs = @("--profile", "simulator", "up", "--build")
}
Write-Host "OCPI_DATA_FILE = '$($env:OCPI_DATA_FILE)'"

# 4) Open the console in the browser as soon as the API can serve data.
$port = if ($env:API_PORT) { $env:API_PORT } else { "8000" }
$opener = Start-Job -ArgumentList $port -ScriptBlock {
    param($apiPort)
    for ($i = 0; $i -lt 150; $i++) {
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$apiPort/health/data" -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                Start-Process "http://127.0.0.1:$apiPort/"
                return
            }
        }
        catch { }
        Start-Sleep -Seconds 2
    }
}

# 5) Run the stack in this window; Ctrl+C stops it, and the containers are removed afterwards.
Write-Host "Console: http://127.0.0.1:$port/   API docs: http://127.0.0.1:$port/docs   (Ctrl+C to stop)"
try {
    docker compose @composeArgs
}
finally {
    Stop-Job $opener -ErrorAction SilentlyContinue
    Remove-Job $opener -Force -ErrorAction SilentlyContinue
    Write-Host "Stopping containers..."
    docker compose --profile simulator down
}