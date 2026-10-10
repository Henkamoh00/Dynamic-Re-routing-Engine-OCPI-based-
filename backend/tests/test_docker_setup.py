import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

import simulate_cpo_updates as sim
from main import FRONTEND_DIR
from services import cpo_repository

PROJECT_ROOT = BACKEND_DIR.parent
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
COMPOSE = PROJECT_ROOT / "docker-compose.yml"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
SOURCE = BACKEND_DIR / "data" / "OCPI_data.json"


# Reads a project-level text file as UTF-8.
def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# Returns the indented body of one top-level compose service.
def service_block(compose_text: str, name: str) -> str:
    pattern = rf"^  {re.escape(name)}:\n((?:    .*\n|\n)*)"
    match = re.search(pattern, compose_text, re.MULTILINE)
    assert match is not None, f"service {name} not found"
    return match.group(1)


# An empty OCPI_DATA_FILE, which compose passes when the variable is unset, falls back to the default file.
def test_empty_data_file_variable_uses_default(monkeypatch):
    monkeypatch.setenv("OCPI_DATA_FILE", "")
    cpo_repository.get_locations.cache_clear()
    assert len(cpo_repository.get_locations()) == 10
    info = cpo_repository.describe_data_source()
    assert info["data_file"] == "OCPI_data.json" and info["is_live"] is False


# Simulator output written into a separate volume folder is served as live data by the API.
def test_simulator_output_in_custom_directory_is_served(client, tmp_path, monkeypatch):
    live = tmp_path / "volume" / "OCPI_data.live.json"
    sim.run(SOURCE, live, 2, 0, 3, 3, True)
    monkeypatch.setenv("OCPI_DATA_FILE", str(live))
    cpo_repository.get_locations.cache_clear()
    body = client.get("/health/data").json()
    assert body["data_file"] == "OCPI_data.live.json" and body["is_live"] is True
    params = {"latitude": 48.1351, "longitude": 11.582, "radius_km": 400}
    assert client.get("/api/v1/locations", params=params).status_code == 200


# The image starts uvicorn with --app-dir code, without the reload watcher.
def test_dockerfile_runs_uvicorn_with_app_dir():
    text = read_text(DOCKERFILE)
    assert '"uvicorn", "main:app", "--app-dir", "code"' in text
    assert "--reload" not in text


# The runtime image drops root, exposes the API port and defines a health check.
def test_dockerfile_runs_as_non_root_with_healthcheck():
    text = read_text(DOCKERFILE)
    assert re.search(r"^USER app$", text, re.MULTILINE)
    assert re.search(r"^EXPOSE 8000$", text, re.MULTILINE)
    assert "HEALTHCHECK" in text and "/health" in text


# The image keeps frontend next to backend, matching the path main.py resolves.
def test_dockerfile_keeps_frontend_next_to_backend():
    text = read_text(DOCKERFILE)
    assert "WORKDIR /app/backend" in text
    assert re.search(r"^COPY .*frontend /app/frontend$", text, re.MULTILINE)
    assert FRONTEND_DIR == PROJECT_ROOT / "frontend"
    assert (FRONTEND_DIR / "index.html").is_file()


# The default stack is only the API; the simulator is an opt-in profile.
def test_compose_declares_api_and_optional_simulator():
    text = read_text(COMPOSE)
    api = service_block(text, "api")
    simulator = service_block(text, "simulator")
    assert "profiles:" not in api and ":8000" in api
    assert 'profiles: ["simulator"]' in simulator
    assert "scripts/simulate_cpo_updates.py" in simulator and "--output" in simulator


# The data path is configurable through OCPI_DATA_FILE and the live file lives on a shared volume.
def test_compose_shares_live_volume_and_data_file_variable():
    text = read_text(COMPOSE)
    api = service_block(text, "api")
    simulator = service_block(text, "simulator")
    assert "OCPI_DATA_FILE: ${OCPI_DATA_FILE:-}" in api
    assert "ocpi-live:/var/lib/ocpi" in api and "ocpi-live:/var/lib/ocpi" in simulator
    assert re.search(r"^volumes:\n  ocpi-live:", text, re.MULTILINE)


# The ignore file removes local and generated files but keeps what the build and test stages copy.
def test_dockerignore_excludes_generated_and_local_files():
    lines = {
        line.strip()
        for line in read_text(DOCKERIGNORE).splitlines()
        if line.strip() and not line.startswith("#")
    }
    required = {".git", "**/.venv", "**/__pycache__", "**/.pytest_cache", "backend/data/OCPI_data.live.json"}
    assert required <= lines
    assert not {"Dockerfile", "docker-compose.yml", "backend", "frontend", "backend/tests"} & lines