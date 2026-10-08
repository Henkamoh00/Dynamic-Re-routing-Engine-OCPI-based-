import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
CODE_DIR = BACKEND_DIR / "code"
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from fastapi.testclient import TestClient
from main import app
from services import cpo_repository, reservation_service

DATA_FILE = BACKEND_DIR / "data" / "OCPI_data.json"


# Resets all shared state (data cache, env override, in-memory reservations) around every test.
@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.delenv("OCPI_DATA_FILE", raising=False)
    cpo_repository.get_locations.cache_clear()
    reservation_service._reservations.clear()
    yield
    cpo_repository.get_locations.cache_clear()
    reservation_service._reservations.clear()


# Provides a FastAPI test client bound to the real application.
@pytest.fixture()
def client():
    return TestClient(app)


# Returns the path of the real mock CPO data file used by the application.
@pytest.fixture()
def data_file() -> Path:
    return DATA_FILE


# Copies the real data file to a temp location and points the application at the copy.
@pytest.fixture()
def live_data(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "live.json"
    path.write_text(DATA_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("OCPI_DATA_FILE", str(path))
    cpo_repository.get_locations.cache_clear()
    return path