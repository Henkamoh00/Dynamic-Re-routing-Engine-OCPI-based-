import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LAUNCHER_PS1 = PROJECT_ROOT / "run_docker.ps1"
LAUNCHER_BAT = PROJECT_ROOT / "run_docker.bat"
COMPOSE = PROJECT_ROOT / "docker-compose.yml"


# Reads a project-level file, skipping the test when it is not part of this tree (for example inside the test image).
def read_project_file(path: Path) -> str:
    if not path.is_file():
        pytest.skip(f"{path.name} is not available in this environment")
    return path.read_text(encoding="utf-8")


# The data file the launcher gives the API is the file the simulator service writes.
def test_launcher_points_api_to_simulator_output():
    launcher = read_project_file(LAUNCHER_PS1)
    compose = read_project_file(COMPOSE)
    api_path = re.search(r'\$env:OCPI_DATA_FILE\s*=\s*"(/[^"]+)"', launcher)
    sim_default = re.search(r"\$\{SIM_OUTPUT:-([^}]+)\}", compose)
    assert api_path is not None and sim_default is not None
    assert api_path.group(1) == sim_default.group(1)


# Live mode enables the simulator profile and passes every setting the compose file reads.
def test_launcher_enables_simulator_and_passes_settings():
    launcher = read_project_file(LAUNCHER_PS1)
    compose = read_project_file(COMPOSE)
    assert '"--profile", "simulator", "up", "--build"' in launcher
    for name in ("SIM_TICKS", "SIM_INTERVAL", "SIM_SEED"):
        assert f"$env:{name}" in launcher
        assert f"${{{name}:-" in compose


# Static mode clears the variable so the API falls back to the bundled data file.
def test_launcher_static_mode_clears_data_file_variable():
    launcher = read_project_file(LAUNCHER_PS1)
    assert "[switch]$NoSimulator" in launcher
    assert re.search(r'\$env:OCPI_DATA_FILE\s*=\s*""', launcher)


# The launcher checks the Docker engine and always removes the containers when it stops.
def test_launcher_checks_engine_and_cleans_up():
    launcher = read_project_file(LAUNCHER_PS1)
    assert "docker info" in launcher
    assert "finally" in launcher
    assert "docker compose --profile simulator down" in launcher


# The batch file delegates to the PowerShell script with the execution policy bypassed.
def test_batch_file_delegates_to_powershell_script():
    batch = read_project_file(LAUNCHER_BAT)
    assert "-ExecutionPolicy Bypass" in batch
    assert "run_docker.ps1" in batch