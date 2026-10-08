import json
import random
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

import simulate_cpo_updates as sim
from schemas import EVSEStatus
from services import cpo_repository

SOURCE = BACKEND_DIR / "data" / "OCPI_data.json"
OFFICIAL = {s.value for s in EVSEStatus}


# Loads a fresh copy of the pristine source data.
def fresh():
    return sim.load_data(SOURCE)


# Collects every EVSE status in the data set.
def statuses(data):
    return [e["status"] for loc in data for e in loc["evses"]]


# The same seed must always produce the same sequence of states.
def test_same_seed_is_deterministic():
    a, b = fresh(), fresh()
    ra, rb = random.Random(3), random.Random(3)
    for _ in range(10):
        sim.step(a, ra)
        sim.step(b, rb)
    assert statuses(a) == statuses(b)


# Every simulated status must remain an official OCPI 2.2.1 value and validate.
def test_statuses_stay_official_and_valid():
    data, rng = fresh(), random.Random(11)
    for _ in range(50):
        sim.step(data, rng)
        sim.validate(data)
        assert set(statuses(data)) <= OFFICIAL


# The minimum number of AVAILABLE EVSEs is always maintained.
def test_min_available_enforced():
    data, rng = fresh(), random.Random(5)
    for _ in range(100):
        sim.step(data, rng, min_available=4)
        assert statuses(data).count("AVAILABLE") >= 4


# Changed EVSEs get a refreshed last_updated timestamp.
def test_last_updated_refreshed_on_change():
    data = fresh()
    before = {e["uid"]: e["last_updated"] for loc in data for e in loc["evses"]}
    rng = random.Random(1)
    for _ in range(5):
        sim.step(data, rng)
    after = {e["uid"]: e["last_updated"] for loc in data for e in loc["evses"]}
    assert before != after


# The run writes the live file and leaves the source untouched.
def test_run_writes_output_not_source(tmp_path):
    source_before = SOURCE.read_text(encoding="utf-8")
    output = tmp_path / "live.json"
    counts = sim.run(SOURCE, output, 3, 0, 1, 3, False)
    assert len(counts) == 3 and output.exists()
    assert SOURCE.read_text(encoding="utf-8") == source_before
    assert not (tmp_path / "live.json.tmp").exists()


# Without reset the run continues from the existing live file, with reset it restarts.
def test_continue_and_reset(tmp_path):
    output = tmp_path / "live.json"
    sim.run(SOURCE, output, 5, 0, 2, 3, False)
    state = statuses(json.loads(output.read_text()))
    assert statuses(sim.initial_state(SOURCE, output, False)) == state
    assert statuses(sim.initial_state(SOURCE, output, True)) == statuses(fresh())


# Invalid source content is reported as a simulator error.
def test_invalid_source_errors(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(sim.SimulatorError):
        sim.load_data(bad)
    obj = tmp_path / "obj.json"
    obj.write_text("{}", encoding="utf-8")
    with pytest.raises(sim.SimulatorError):
        sim.load_data(obj)


# The command line entry point succeeds and fails with proper exit codes.
def test_main_exit_codes(tmp_path):
    out = tmp_path / "live.json"
    assert sim.main(["--once", "--seed", "1", "--output", str(out)]) == 0
    assert sim.main(["--once", "--source", str(tmp_path / "missing.json"), "--output", str(out), "--reset"]) == 1
    with pytest.raises(SystemExit):
        sim.main(["--ticks", "0"])


# The API picks up simulator output through the mtime-aware cache without a restart.
def test_repository_hot_reloads_on_file_change(live_data):
    assert {e.uid: e.status.value for l in cpo_repository.get_locations() for e in l.evses}["EVSE_MUN_1"] == "AVAILABLE"
    from factories import set_evse_status
    set_evse_status(live_data, "EVSE_MUN_1", "OUTOFORDER")
    assert {e.uid: e.status.value for l in cpo_repository.get_locations() for e in l.evses}["EVSE_MUN_1"] == "OUTOFORDER"


# The auto endpoint rebooks a reservation when the station fails and reports the result.
def test_reroute_auto_rebooks_after_outage(client, live_data):
    from factories import set_evse_status

    slot = {"arrival_time": "2026-10-07T10:00:00Z", "expiry_date": "2026-10-07T11:00:00Z"}
    created = client.post(
        "/api/v1/reservations/RES_A",
        json={"location_id": "LOC_MUNICH_SOUTH", "evse_uid": "EVSE_MUN_1", "booking_slot": slot},
    )
    assert created.status_code == 201 or created.status_code == 200
    set_evse_status(live_data, "EVSE_MUN_1", "OUTOFORDER")
    truck = {"latitude": 48.1, "longitude": 11.6, "battery_percentage": 80}
    response = client.post("/api/v1/reroute/auto", json={"truck": truck, "reservation_id": "RES_A", "delay_minutes": 20})
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "REBOOKED" and body["conflict_detected"] is True
    assert body["previous_reservation"]["status"] == "CANCELLED"
    assert body["charging_station"]["station_id"] == "LOC_INGOLSTADT_HUB"


# The auto endpoint maps unknown reservations, cancelled ones, empty ranges and bad input to HTTP errors.
def test_reroute_auto_error_mapping(client, live_data):
    from factories import set_evse_status

    truck = {"latitude": 48.1, "longitude": 11.6, "battery_percentage": 80}
    assert client.post("/api/v1/reroute/auto", json={"truck": truck, "reservation_id": "NOPE"}).status_code == 404
    assert client.post("/api/v1/reroute/auto", json={"truck": truck, "reservation_id": ""}).status_code == 422
    slot = {"arrival_time": "2026-10-07T10:00:00Z", "expiry_date": "2026-10-07T11:00:00Z"}
    client.post("/api/v1/reservations/R2", json={"location_id": "LOC_MUNICH_SOUTH", "evse_uid": "EVSE_MUN_1", "booking_slot": slot})
    set_evse_status(live_data, "EVSE_MUN_1", "OUTOFORDER")
    low = {"latitude": 48.1, "longitude": 11.6, "battery_percentage": 1}
    assert client.post("/api/v1/reroute/auto", json={"truck": low, "reservation_id": "R2"}).status_code == 404
    assert client.post("/api/v1/reroute/auto", json={"truck": truck, "reservation_id": "R2"}).status_code == 200
    assert client.post("/api/v1/reroute/auto", json={"truck": truck, "reservation_id": "R2"}).status_code == 409