import json
import re

import pytest
from services import cpo_repository
from services.cpo_repository import (
    DataSourceError,
    find_evse,
    find_evse_by_uid,
    find_location,
    get_locations,
    reload_locations,
)

OFFICIAL_STATUSES = {
    "AVAILABLE",
    "BLOCKED",
    "CHARGING",
    "INOPERATIVE",
    "OUTOFORDER",
    "PLANNED",
    "REMOVED",
    "RESERVED",
    "UNKNOWN",
}
LEGACY_CONNECTOR_KEYS = {"max_power", "voltage", "amperage"}


# Loads the raw JSON of the real data file without going through the models.
def load_raw(data_file):
    return json.loads(data_file.read_text(encoding="utf-8"))


# Writes a JSON payload to a temporary data file and points the app to it.
def use_temp_data(monkeypatch, tmp_path, payload, raw_text=None):
    path = tmp_path / "OCPI_data.json"
    path.write_text(raw_text if raw_text is not None else json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("OCPI_DATA_FILE", str(path))
    cpo_repository.get_locations.cache_clear()
    return path


# The shipped data file holds exactly ten German locations.
def test_data_file_has_ten_locations(data_file):
    raw = load_raw(data_file)
    assert len(raw) == 10
    assert all(item["country_code"] == "DE" and item["country"] == "DEU" for item in raw)


# The shipped data covers the requested German cities.
def test_data_file_contains_required_cities(data_file):
    cities = {item["city"] for item in load_raw(data_file)}
    assert {"Munich", "Stuttgart", "Frankfurt", "Nuremberg"} <= cities


# The shipped data must parse successfully through the OCPI models.
def test_data_file_loads_through_models():
    locations = get_locations()
    assert len(locations) == 10


# Every EVSE status in the data is an official OCPI 2.2.1 value.
def test_data_statuses_are_official(data_file):
    statuses = {evse["status"] for loc in load_raw(data_file) for evse in loc["evses"]}
    assert statuses <= OFFICIAL_STATUSES
    assert "OCCUPIED" not in statuses


# The data set covers AVAILABLE, CHARGING, BLOCKED and OUTOFORDER for filter testing.
def test_data_status_variety(data_file):
    statuses = [evse["status"] for loc in load_raw(data_file) for evse in loc["evses"]]
    assert {"AVAILABLE", "CHARGING", "BLOCKED", "OUTOFORDER"} <= set(statuses)
    assert statuses.count("AVAILABLE") >= 5


# Connectors must use max_electric_power as integer Watts between 160 kW and 400 kW.
def test_data_power_in_watts_and_range(data_file):
    for loc in load_raw(data_file):
        for evse in loc["evses"]:
            for connector in evse["connectors"]:
                power = connector["max_electric_power"]
                assert isinstance(power, int)
                assert 160000 <= power <= 400000


# Every connector in the data is the truck standard IEC_62196_T2_COMBO.
def test_data_connectors_are_truck_standard(data_file):
    for loc in load_raw(data_file):
        for evse in loc["evses"]:
            for connector in evse["connectors"]:
                assert connector["standard"] == "IEC_62196_T2_COMBO"


# The legacy non-OCPI connector field names must not appear in the data.
def test_data_has_no_legacy_connector_keys(data_file):
    for loc in load_raw(data_file):
        for evse in loc["evses"]:
            for connector in evse["connectors"]:
                assert not (LEGACY_CONNECTOR_KEYS & set(connector))


# Coordinates must be nested string objects inside Germany, with no top-level lat/lon.
def test_data_coordinates_structure(data_file):
    for loc in load_raw(data_file):
        assert "latitude" not in loc and "longitude" not in loc
        coordinates = loc["coordinates"]
        assert isinstance(coordinates["latitude"], str) and isinstance(coordinates["longitude"], str)
        assert 47.0 <= float(coordinates["latitude"]) <= 55.5
        assert 5.5 <= float(coordinates["longitude"]) <= 15.5


# Location, EVSE and connector identifiers are unique and EVSE ids follow the eMI3 pattern.
def test_data_identifiers_unique_and_well_formed(data_file):
    raw = load_raw(data_file)
    location_ids = [loc["id"] for loc in raw]
    evse_uids = [evse["uid"] for loc in raw for evse in loc["evses"]]
    evse_ids = [evse["evse_id"] for loc in raw for evse in loc["evses"]]
    assert len(set(location_ids)) == len(location_ids)
    assert len(set(evse_uids)) == len(evse_uids)
    assert all(re.fullmatch(r"[A-Z]{2}\*[A-Z0-9]{3}\*E[A-Z0-9]+", value) for value in evse_ids)


# Every mandatory OCPI 2.2.1 location, EVSE and connector field is present in the data.
def test_data_has_mandatory_ocpi_fields(data_file):
    location_fields = {"country_code", "party_id", "id", "publish", "address", "city", "country", "coordinates", "time_zone", "last_updated"}
    evse_fields = {"uid", "status", "connectors", "last_updated"}
    connector_fields = {"id", "standard", "format", "power_type", "max_voltage", "max_amperage", "last_updated"}
    for loc in load_raw(data_file):
        assert location_fields <= set(loc)
        for evse in loc["evses"]:
            assert evse_fields <= set(evse)
            for connector in evse["connectors"]:
                assert connector_fields <= set(connector)


# The repository caches the parsed data between calls.
def test_get_locations_is_cached():
    assert get_locations() is get_locations()


# Clearing the cache forces the data to be loaded again.
def test_reload_locations_clears_cache():
    first = get_locations()
    reload_locations()
    assert get_locations() is not first


# An existing location is returned by id.
def test_find_location_hit():
    location = find_location("LOC_MUNICH_SOUTH")
    assert location is not None and location.city == "Munich"


# An unknown location id returns None.
def test_find_location_miss():
    assert find_location("LOC_DOES_NOT_EXIST") is None


# An EVSE is found by location and uid together.
def test_find_evse_hit():
    found = find_evse("LOC_MUNICH_SOUTH", "EVSE_MUN_1")
    assert found is not None
    location, evse = found
    assert location.id == "LOC_MUNICH_SOUTH" and evse.uid == "EVSE_MUN_1"


# A wrong location or wrong uid returns None.
@pytest.mark.parametrize("location_id,uid", [("LOC_NOPE", "EVSE_MUN_1"), ("LOC_MUNICH_SOUTH", "EVSE_NOPE"), ("LOC_STUTTGART_EAST", "EVSE_MUN_1")])
def test_find_evse_miss(location_id, uid):
    assert find_evse(location_id, uid) is None


# An EVSE can be found by uid across all locations.
def test_find_evse_by_uid_hit_and_miss():
    found = find_evse_by_uid("EVSE_AUG_1")
    assert found is not None and found[0].id == "LOC_AUGSBURG_WEST"
    assert find_evse_by_uid("EVSE_NOPE") is None


# The OCPI_DATA_FILE environment variable overrides the default data location.
def test_env_override_is_used(monkeypatch, tmp_path):
    use_temp_data(monkeypatch, tmp_path, [])
    assert get_locations() == ()


# A path set in OCPI_DATA_FILE that does not exist raises a clear data source error.
def test_env_override_missing_file_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("OCPI_DATA_FILE", str(tmp_path / "missing.json"))
    cpo_repository.get_locations.cache_clear()
    with pytest.raises(DataSourceError) as excinfo:
        get_locations()
    assert "missing" in str(excinfo.value)


# Malformed JSON is reported as a data source error.
def test_invalid_json_raises(monkeypatch, tmp_path):
    use_temp_data(monkeypatch, tmp_path, None, raw_text="{not valid json")
    with pytest.raises(DataSourceError):
        get_locations()


# A non-official status in the file is rejected at load time.
def test_invalid_status_in_file_raises(monkeypatch, tmp_path, data_file):
    raw = load_raw(data_file)
    raw[0]["evses"][0]["status"] = "OCCUPIED"
    use_temp_data(monkeypatch, tmp_path, raw)
    with pytest.raises(DataSourceError):
        get_locations()


# Legacy top-level coordinates without the nested object are rejected at load time.
def test_legacy_coordinates_in_file_raise(monkeypatch, tmp_path, data_file):
    raw = load_raw(data_file)
    location = raw[0]
    location["latitude"] = location["coordinates"]["latitude"]
    location["longitude"] = location["coordinates"]["longitude"]
    del location["coordinates"]
    use_temp_data(monkeypatch, tmp_path, raw)
    with pytest.raises(DataSourceError):
        get_locations()


# When no file is found anywhere the error lists the searched paths.
def test_no_file_found_anywhere_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(cpo_repository, "_candidate_paths", lambda: iter([tmp_path / "a.json", tmp_path / "b.json"]))
    cpo_repository.get_locations.cache_clear()
    with pytest.raises(DataSourceError) as excinfo:
        get_locations()
    assert "a.json" in str(excinfo.value) and "b.json" in str(excinfo.value)


# The automatic search locates the real data file without any override.
def test_default_search_finds_real_file():
    paths = [path for path in cpo_repository._candidate_paths() if path.is_file()]
    assert paths and paths[0].name == "OCPI_data.json"