import json
import os
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from schemas import OCPIEVSE, OCPILocation

DATA_FILE_NAME = "OCPI_data.json"

_cache_lock = Lock()
_cache_key: tuple[Path, int] | None = None
_cache_value: tuple[OCPILocation, ...] | None = None


class DataSourceError(Exception):
    pass


def _candidate_paths() -> Iterator[Path]:
    for base in Path(__file__).resolve().parents[:4]:
        yield base / DATA_FILE_NAME
        yield base / "data" / DATA_FILE_NAME


def _resolve_data_file() -> Path:
    override = os.getenv("OCPI_DATA_FILE")
    if override:
        override_path = Path(override)
        if not override_path.is_file():
            raise DataSourceError(
                f"OCPI_DATA_FILE points to a missing file: {override_path}"
            )
        return override_path

    candidates = list(_candidate_paths())
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(path) for path in candidates)
    raise DataSourceError(f"{DATA_FILE_NAME} not found. Searched: {searched}")


def describe_data_source() -> dict:
    data_file = _resolve_data_file()
    try:
        modified = data_file.stat().st_mtime
    except OSError as exc:
        raise DataSourceError(f"Unable to read CPO data from {data_file}: {exc}") from exc
    return {
        "data_file": data_file.name,
        "is_live": data_file.name.endswith(".live.json"),
        "last_modified": datetime.fromtimestamp(modified, timezone.utc).isoformat(),
    }


def get_locations() -> tuple[OCPILocation, ...]:
    global _cache_key, _cache_value
    data_file = _resolve_data_file()
    try:
        key = (data_file, data_file.stat().st_mtime_ns)
    except OSError as exc:
        raise DataSourceError(f"Unable to read CPO data from {data_file}: {exc}") from exc

    with _cache_lock:
        if _cache_key == key and _cache_value is not None:
            return _cache_value
        try:
            with data_file.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            locations = tuple(OCPILocation.model_validate(item) for item in raw)
        except (OSError, ValueError) as exc:
            raise DataSourceError(f"Unable to load CPO data from {data_file}: {exc}") from exc
        _cache_key, _cache_value = key, locations
        return locations


def _clear_cache() -> None:
    global _cache_key, _cache_value
    with _cache_lock:
        _cache_key = None
        _cache_value = None


get_locations.cache_clear = _clear_cache  # type: ignore[attr-defined]


def reload_locations() -> None:
    _clear_cache()


def find_location(location_id: str) -> OCPILocation | None:
    for location in get_locations():
        if location.id == location_id:
            return location
    return None


def find_evse(location_id: str, evse_uid: str) -> tuple[OCPILocation, OCPIEVSE] | None:
    location = find_location(location_id)
    if location is None:
        return None
    for evse in location.evses:
        if evse.uid == evse_uid:
            return location, evse
    return None


def find_evse_by_uid(evse_uid: str) -> tuple[OCPILocation, OCPIEVSE] | None:
    for location in get_locations():
        for evse in location.evses:
            if evse.uid == evse_uid:
                return location, evse
    return None