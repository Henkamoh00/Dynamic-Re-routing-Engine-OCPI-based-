param(
    [int]$Ticks = 30,
    [int]$Interval = 10,
    [int]$Seed = 7
)

Set-Location -Path $PSScriptRoot

$env:OCPI_DATA_FILE = Join-Path $PSScriptRoot "data\OCPI_data.live.json"
Write-Host "OCPI_DATA_FILE = $env:OCPI_DATA_FILE"

# 1) Create the live file first so the server never starts without it.
poetry run python scripts/simulate_cpo_updates.py --reset --once --seed $Seed
if ($LASTEXITCODE -ne 0) {
    Write-Host "The simulator failed to create the live data file."
    exit 1
}

# 2) Run the continuous simulator in its own window.
$simCommand = "poetry run python scripts/simulate_cpo_updates.py --reset --ticks $Ticks --interval $Interval --seed $Seed"
Start-Process powershell -WorkingDirectory $PSScriptRoot -ArgumentList "-NoExit", "-Command", $simCommand

# 3) Run the API and the console UI in this window.
Write-Host "Console: http://127.0.0.1:8000/   API docs: http://127.0.0.1:8000/docs"
poetry run uvicorn main:app --app-dir code --reload