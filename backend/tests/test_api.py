import pytest

MUNICH_TRUCK = {"latitude": 48.1351, "longitude": 11.5820, "battery_percentage": 40}
SLOT = {"arrival_time": "2026-10-07T10:00:00Z", "expiry_date": "2026-10-07T11:00:00Z"}
RESERVATION_BODY = {"location_id": "LOC_MUNICH_SOUTH", "evse_uid": "EVSE_MUN_1", "truck_id": "TRUCK_1", "booking_slot": SLOT}
PROFILE_BODY = {
    "location_id": "LOC_AUGSBURG_WEST",
    "evse_uid": "EVSE_AUG_1",
    "charging_profile": {"charging_rate_unit": "W", "charging_profile_period": [{"start_period": 0, "limit": 100000}]},
}


# Points the application at a missing data file to simulate a CPO outage.
@pytest.fixture()
def broken_data(monkeypatch, tmp_path):
    from services import cpo_repository

    monkeypatch.setenv("OCPI_DATA_FILE", str(tmp_path / "missing.json"))
    cpo_repository.get_locations.cache_clear()


# The health endpoint reports the service is up.
def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


# A truck in Munich is routed to the nearest available station with the exact response shape.
def test_reroute_success_shape(client):
    response = client.post("/api/v1/reroute", json=MUNICH_TRUCK)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "message", "truck_summary", "recommended_charging_station"}
    assert set(body["truck_summary"]) == {"current_range_km", "estimated_battery_at_arrival"}
    assert set(body["recommended_charging_station"]) == {"station_id", "station_name", "evse_uid", "max_power_kw", "distance_km"}
    assert body["status"] == "success"
    assert body["recommended_charging_station"]["station_id"] == "LOC_MUNICH_SOUTH"
    assert body["recommended_charging_station"]["max_power_kw"] == 320.0
    assert body["truck_summary"]["current_range_km"] == 100.0
    assert body["truck_summary"]["estimated_battery_at_arrival"] == pytest.approx(36.7, abs=0.05)


# Default consumption and capacity are applied when omitted and honoured when provided.
def test_reroute_custom_consumption_and_capacity(client):
    payload = {**MUNICH_TRUCK, "consumption_rate": 2.0, "full_capacity_kwh": 400}
    body = client.post("/api/v1/reroute", json=payload).json()
    assert body["truck_summary"]["current_range_km"] == 80.0


# A truck with almost no battery gets the critical 404 message.
def test_reroute_no_charger_in_range(client):
    response = client.post("/api/v1/reroute", json={**MUNICH_TRUCK, "battery_percentage": 1})
    assert response.status_code == 404
    assert "No available chargers found within the truck's remaining battery range" in response.json()["detail"]


# A truck located far from every station gets a 404.
def test_reroute_truck_far_from_all_stations(client):
    payload = {"latitude": 0.0, "longitude": 0.0, "battery_percentage": 100}
    assert client.post("/api/v1/reroute", json=payload).status_code == 404


# The recommended station is never one whose EVSE is not AVAILABLE.
@pytest.mark.parametrize(
    "lat,lon,blocked_id",
    [(50.0264, 8.5432, "LOC_FRANKFURT_AIRPORT"), (49.0134, 12.1016, "LOC_REGENSBURG_EAST"), (49.4077, 8.6908, "LOC_HEIDELBERG_SOUTH"), (48.3665, 10.9001, "LOC_AUGSBURG_WEST")],
)
def test_reroute_never_picks_unavailable_station(client, lat, lon, blocked_id):
    payload = {"latitude": lat, "longitude": lon, "battery_percentage": 100}
    body = client.post("/api/v1/reroute", json=payload).json()
    assert body["recommended_charging_station"]["station_id"] != blocked_id


# Invalid telemetry values are rejected with 422.
@pytest.mark.parametrize(
    "overrides",
    [
        {"battery_percentage": 101},
        {"battery_percentage": -1},
        {"latitude": 91},
        {"longitude": 181},
        {"consumption_rate": 0},
        {"full_capacity_kwh": -1},
        {"battery_percentage": "abc"},
    ],
)
def test_reroute_invalid_input(client, overrides):
    assert client.post("/api/v1/reroute", json={**MUNICH_TRUCK, **overrides}).status_code == 422


# Missing required telemetry fields are rejected with 422.
@pytest.mark.parametrize("missing", ["latitude", "longitude", "battery_percentage"])
def test_reroute_missing_field(client, missing):
    payload = {key: value for key, value in MUNICH_TRUCK.items() if key != missing}
    assert client.post("/api/v1/reroute", json=payload).status_code == 422


# An empty body or wrong HTTP method is rejected.
def test_reroute_empty_body_and_wrong_method(client):
    assert client.post("/api/v1/reroute", json={}).status_code == 422
    assert client.get("/api/v1/reroute").status_code == 405


# A CPO data outage on reroute returns 503.
def test_reroute_data_outage(client, broken_data):
    assert client.post("/api/v1/reroute", json=MUNICH_TRUCK).status_code == 503


# Location search returns nearby truck-capable locations sorted by distance.
def test_locations_search(client):
    response = client.get("/api/v1/locations", params={"latitude": 48.1351, "longitude": 11.582, "radius_km": 100})
    assert response.status_code == 200
    body = response.json()
    assert body[0]["location_id"] == "LOC_MUNICH_SOUTH"
    distances = [item["distance_km"] for item in body]
    assert distances == sorted(distances)
    assert set(body[0]) == {"location_id", "name", "city", "coordinates", "distance_km", "available_evses", "max_power_kw"}
    assert set(body[0]["coordinates"]) == {"latitude", "longitude"}


# A tiny radius far from any station returns an empty list.
def test_locations_search_empty(client):
    response = client.get("/api/v1/locations", params={"latitude": 0, "longitude": 0, "radius_km": 10})
    assert response.status_code == 200 and response.json() == []


# The radius filter excludes stations beyond the requested distance.
def test_locations_radius_filter(client):
    small = client.get("/api/v1/locations", params={"latitude": 48.1351, "longitude": 11.582, "radius_km": 20}).json()
    large = client.get("/api/v1/locations", params={"latitude": 48.1351, "longitude": 11.582, "radius_km": 400}).json()
    assert len(small) < len(large)


# A connector standard without matching stations yields an empty list.
def test_locations_connector_filter(client):
    params = {"latitude": 48.1351, "longitude": 11.582, "radius_km": 400, "connector_standard": "CHADEMO"}
    assert client.get("/api/v1/locations", params=params).json() == []


# Invalid search parameters are rejected with 422.
@pytest.mark.parametrize(
    "params",
    [
        {"latitude": 91, "longitude": 11},
        {"latitude": 48, "longitude": 181},
        {"latitude": 48, "longitude": 11, "radius_km": 0},
        {"latitude": 48, "longitude": 11, "connector_standard": "NOT_A_TYPE"},
        {"longitude": 11},
    ],
)
def test_locations_invalid_params(client, params):
    assert client.get("/api/v1/locations", params=params).status_code == 422


# A CPO data outage on location search returns 503.
def test_locations_data_outage(client, broken_data):
    assert client.get("/api/v1/locations", params={"latitude": 48, "longitude": 11}).status_code == 503


# The status endpoint returns the official OCPI status of each EVSE.
@pytest.mark.parametrize(
    "evse_uid,expected",
    [("EVSE_MUN_1", "AVAILABLE"), ("EVSE_FRA_1", "CHARGING"), ("EVSE_REG_1", "BLOCKED"), ("EVSE_HEI_1", "OUTOFORDER")],
)
def test_status_endpoint(client, evse_uid, expected):
    response = client.get(f"/api/v1/status/evses/{evse_uid}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == expected and body["evse_uid"] == evse_uid
    assert set(body) == {"location_id", "evse_uid", "evse_id", "status", "last_updated"}


# The status endpoint returns 404 for an unknown EVSE.
def test_status_unknown_evse(client):
    assert client.get("/api/v1/status/evses/EVSE_NOPE").status_code == 404


# A CPO data outage on the status endpoint returns 503.
def test_status_data_outage(client, broken_data):
    assert client.get("/api/v1/status/evses/EVSE_MUN_1").status_code == 503


# Creating a reservation returns 201 with an ACTIVE reservation.
def test_create_reservation_endpoint(client):
    response = client.post("/api/v1/reservations/R1", json=RESERVATION_BODY)
    assert response.status_code == 201
    body = response.json()
    assert body["reservation_id"] == "R1" and body["status"] == "ACTIVE"
    assert body["booking_slot"]["arrival_time"] == SLOT["arrival_time"]


# Creating the same reservation id twice returns 409.
def test_create_reservation_duplicate(client):
    client.post("/api/v1/reservations/R1", json=RESERVATION_BODY)
    assert client.post("/api/v1/reservations/R1", json=RESERVATION_BODY).status_code == 409


# Reserving a non-available EVSE or an unknown EVSE returns 422.
@pytest.mark.parametrize(
    "location_id,evse_uid",
    [("LOC_FRANKFURT_AIRPORT", "EVSE_FRA_1"), ("LOC_REGENSBURG_EAST", "EVSE_REG_1"), ("LOC_HEIDELBERG_SOUTH", "EVSE_HEI_1"), ("LOC_NOPE", "EVSE_NOPE")],
)
def test_create_reservation_invalid_target(client, location_id, evse_uid):
    body = {**RESERVATION_BODY, "location_id": location_id, "evse_uid": evse_uid}
    assert client.post("/api/v1/reservations/R1", json=body).status_code == 422


# Malformed reservation bodies are rejected with 422.
@pytest.mark.parametrize(
    "mutation",
    [
        {"booking_slot": {"arrival_time": "2026-10-07T11:00:00Z", "expiry_date": "2026-10-07T10:00:00Z"}},
        {"booking_slot": {"arrival_time": "2026-10-07T10:00:00Z", "expiry_date": "2026-10-07T10:00:00Z"}},
        {"booking_slot": {"arrival_time": "not a date", "expiry_date": "2026-10-07T10:00:00Z"}},
        {"booking_slot": None},
        {"evse_uid": None},
    ],
)
def test_create_reservation_invalid_body(client, mutation):
    assert client.post("/api/v1/reservations/R1", json={**RESERVATION_BODY, **mutation}).status_code == 422


# A CPO data outage when creating a reservation returns 503.
def test_create_reservation_data_outage(client, broken_data):
    assert client.post("/api/v1/reservations/R1", json=RESERVATION_BODY).status_code == 503


# A delay patch shifts the booking slot forward by the given minutes.
def test_patch_delay(client):
    client.post("/api/v1/reservations/R1", json=RESERVATION_BODY)
    response = client.patch("/api/v1/reservations/R1", json={"delay_minutes": 30})
    assert response.status_code == 200
    assert response.json()["booking_slot"] == {"arrival_time": "2026-10-07T10:30:00Z", "expiry_date": "2026-10-07T11:30:00Z"}


# A patch with an explicit new slot replaces the booking slot.
def test_patch_new_slot(client):
    client.post("/api/v1/reservations/R1", json=RESERVATION_BODY)
    new_slot = {"arrival_time": "2026-10-08T08:00:00Z", "expiry_date": "2026-10-08T09:00:00Z"}
    response = client.patch("/api/v1/reservations/R1", json={"booking_slot": new_slot})
    assert response.status_code == 200 and response.json()["booking_slot"] == new_slot


# A cancel patch marks the reservation as CANCELLED.
def test_patch_cancel(client):
    client.post("/api/v1/reservations/R1", json=RESERVATION_BODY)
    response = client.patch("/api/v1/reservations/R1", json={"status": "CANCELLED"})
    assert response.status_code == 200 and response.json()["status"] == "CANCELLED"


# Patching an unknown reservation returns 404.
def test_patch_unknown_reservation(client):
    assert client.patch("/api/v1/reservations/NOPE", json={"delay_minutes": 10}).status_code == 404


# Patching a cancelled reservation returns 409.
def test_patch_cancelled_reservation_conflict(client):
    client.post("/api/v1/reservations/R1", json=RESERVATION_BODY)
    client.patch("/api/v1/reservations/R1", json={"status": "CANCELLED"})
    assert client.patch("/api/v1/reservations/R1", json={"delay_minutes": 10}).status_code == 409


# Invalid patch bodies are rejected with 422.
@pytest.mark.parametrize(
    "body",
    [{}, {"delay_minutes": 0}, {"delay_minutes": 1441}, {"delay_minutes": -3}, {"status": "ACTIVE"}, {"delay_minutes": 10, "status": "CANCELLED"}],
)
def test_patch_invalid_body(client, body):
    client.post("/api/v1/reservations/R1", json=RESERVATION_BODY)
    assert client.patch("/api/v1/reservations/R1", json=body).status_code == 422


# The full delay workflow: reserve, shift for a delay, then cancel.
def test_reservation_lifecycle(client):
    assert client.post("/api/v1/reservations/R1", json=RESERVATION_BODY).status_code == 201
    assert client.patch("/api/v1/reservations/R1", json={"delay_minutes": 15}).status_code == 200
    assert client.patch("/api/v1/reservations/R1", json={"status": "CANCELLED"}).status_code == 200
    assert client.patch("/api/v1/reservations/R1", json={"delay_minutes": 15}).status_code == 409


# A valid smart charging profile on a CHARGING EVSE is accepted and reports freed capacity.
def test_smart_charging_success(client):
    response = client.post("/api/v1/smart-charging/profile", json=PROFILE_BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ACCEPTED"
    assert body["original_max_power_kw"] == 320.0
    assert body["curtailed_max_power_kw"] == 100.0
    assert body["freed_capacity_kw"] == 220.0


# Smart charging on an AVAILABLE, blocked or unknown EVSE returns 422.
@pytest.mark.parametrize(
    "location_id,evse_uid",
    [("LOC_MUNICH_SOUTH", "EVSE_MUN_1"), ("LOC_REGENSBURG_EAST", "EVSE_REG_1"), ("LOC_NOPE", "EVSE_NOPE")],
)
def test_smart_charging_invalid_target(client, location_id, evse_uid):
    body = {**PROFILE_BODY, "location_id": location_id, "evse_uid": evse_uid}
    assert client.post("/api/v1/smart-charging/profile", json=body).status_code == 422


# Malformed charging profiles are rejected with 422.
@pytest.mark.parametrize(
    "profile",
    [
        {"charging_rate_unit": "kW", "charging_profile_period": [{"start_period": 0, "limit": 10}]},
        {"charging_rate_unit": "W", "charging_profile_period": []},
        {"charging_rate_unit": "W", "charging_profile_period": [{"start_period": 0, "limit": -5}]},
        {"charging_rate_unit": "W"},
    ],
)
def test_smart_charging_invalid_profile(client, profile):
    body = {**PROFILE_BODY, "charging_profile": profile}
    assert client.post("/api/v1/smart-charging/profile", json=body).status_code == 422


# A CPO data outage on smart charging returns 503.
def test_smart_charging_data_outage(client, broken_data):
    assert client.post("/api/v1/smart-charging/profile", json=PROFILE_BODY).status_code == 503


# Unknown routes return 404 and the OpenAPI schema is served.
def test_unknown_route_and_openapi(client):
    assert client.get("/api/v1/does-not-exist").status_code == 404
    schema = client.get("/openapi.json").json()
    assert "/api/v1/reroute" in schema["paths"]
    assert "/api/v1/smart-charging/profile" in schema["paths"]


# The data source endpoint reports which file the backend reads and whether it is the live file.
def test_health_data_reports_source(client, live_data):
    body = client.get("/health/data").json()
    assert body["data_file"] == "live.json"
    assert body["is_live"] is False
    assert "last_modified" in body


# The data source endpoint flags a *.live.json file as live and returns 503 when the file is missing.
def test_health_data_live_flag_and_error(client, tmp_path, monkeypatch):
    live = tmp_path / "OCPI_data.live.json"
    live.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("OCPI_DATA_FILE", str(live))
    assert client.get("/health/data").json()["is_live"] is True
    monkeypatch.setenv("OCPI_DATA_FILE", str(tmp_path / "missing.json"))
    assert client.get("/health/data").status_code == 503

# Builds a reservation body for the API tests.
def reservation_body(evse_uid="EVSE_MUN_1", location_id="LOC_MUNICH_SOUTH", hours=1, **extra):
    from datetime import datetime, timedelta, timezone

    arrival = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc) + timedelta(hours=hours)
    body = {
        "location_id": location_id,
        "evse_uid": evse_uid,
        "booking_slot": {
            "arrival_time": arrival.isoformat(),
            "expiry_date": (arrival + timedelta(hours=2)).isoformat(),
        },
    }
    body.update(extra)
    return body


# PUT replaces the booking slot of an existing reservation.
def test_put_reservation_replaces_slot(client):
    client.post("/api/v1/reservations/R1", json=reservation_body())
    new_body = reservation_body(hours=5)
    response = client.put("/api/v1/reservations/R1", json={"booking_slot": new_body["booking_slot"]})
    assert response.status_code == 200
    assert response.json()["booking_slot"]["arrival_time"].startswith("2026-10-09T13:00")


# PUT maps missing, cancelled and invalid requests to 404, 409 and 422.
def test_put_reservation_errors(client):
    slot = reservation_body()["booking_slot"]
    assert client.put("/api/v1/reservations/NOPE", json={"booking_slot": slot}).status_code == 404
    client.post("/api/v1/reservations/R1", json=reservation_body())
    client.patch("/api/v1/reservations/R1", json={"status": "CANCELLED"})
    assert client.put("/api/v1/reservations/R1", json={"booking_slot": slot}).status_code == 409
    assert client.put("/api/v1/reservations/R1", json={}).status_code == 422


# GET lists reservations and supports the active_only filter.
def test_list_reservations_endpoint(client):
    assert client.get("/api/v1/reservations").json() == []
    client.post("/api/v1/reservations/R1", json=reservation_body())
    client.post("/api/v1/reservations/R2", json=reservation_body("EVSE_STR_1", "LOC_STUTTGART_EAST"))
    client.patch("/api/v1/reservations/R2", json={"status": "CANCELLED"})
    assert len(client.get("/api/v1/reservations").json()) == 2
    active = client.get("/api/v1/reservations", params={"active_only": True}).json()
    assert [item["reservation_id"] for item in active] == ["R1"]


# Telemetry sent when creating a reservation is returned in the response.
def test_create_reservation_with_telemetry(client):
    body = reservation_body(battery_percentage=15, battery_capacity_kwh=400, target_soc_percent=90)
    data = client.post("/api/v1/reservations/R1", json=body).json()
    assert data["battery_percentage"] == 15 and data["battery_capacity_kwh"] == 400
    assert data["target_soc_percent"] == 90 and data["curtailment_booked"] is False


# The demand forecast endpoint returns the structure and the computed peak.
def test_demand_forecast_endpoint(client):
    body = reservation_body(battery_percentage=20, battery_capacity_kwh=300, target_soc_percent=80)
    client.post("/api/v1/reservations/R1", json=body)
    response = client.get(
        "/api/v1/cpo/demand-forecast",
        params={"as_of": "2026-10-09T08:00:00Z", "grid_limit_kw": 200},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["stations"][0]["location_id"] == "LOC_MUNICH_SOUTH"
    assert data["totals"]["peak_demand_kw"] == 320.0
    assert data["totals"]["over_limit_buckets"] == 2
    assert data["totals"]["grid_limit_kw"] == 200


# The demand forecast validates its query parameters.
@pytest.mark.parametrize(
    "params",
    [
        {"horizon_hours": 0},
        {"horizon_hours": 73},
        {"bucket_minutes": 4},
        {"bucket_minutes": 61},
        {"bucket_minutes": 7},
        {"grid_limit_kw": 0},
    ],
)
def test_demand_forecast_invalid_params(client, params):
    assert client.get("/api/v1/cpo/demand-forecast", params=params).status_code == 422


# An empty store gives an empty forecast with a successful response.
def test_demand_forecast_empty(client):
    data = client.get("/api/v1/cpo/demand-forecast").json()
    assert data["stations"] == [] and data["totals"]["incoming_trucks"] == 0


# The auto endpoint throttles a charging EVSE and rebooks when no charger is free.
def test_reroute_auto_curtailment_over_http(client, live_data):
    from factories import set_evse_status

    client.post("/api/v1/reservations/RES_C", json=reservation_body())
    for uid in ["EVSE_MUN_1", "EVSE_STR_1", "EVSE_NUR_1", "EVSE_ULM_1", "EVSE_ING_1", "EVSE_KAR_1"]:
        set_evse_status(live_data, uid, "BLOCKED")
    truck = {"latitude": 48.1, "longitude": 11.6, "battery_percentage": 80}
    body = client.post("/api/v1/reroute/auto", json={"truck": truck, "reservation_id": "RES_C"}).json()
    assert body["action"] == "REBOOKED_WITH_CURTAILMENT"
    assert body["smart_charging"]["freed_capacity_kw"] == 160.0
    assert body["active_reservation"]["curtailment_booked"] is True


# The auto endpoint reports the ETA delay and the applied delay.
def test_reroute_auto_eta_over_http(client, live_data):
    client.post("/api/v1/reservations/RES_E", json=reservation_body())
    truck = {"latitude": 48.1, "longitude": 11.6, "battery_percentage": 80}
    response = client.post(
        "/api/v1/reroute/auto",
        json={"truck": truck, "reservation_id": "RES_E", "average_speed_kmh": 1.0, "as_of": "2026-10-09T08:00:00Z"},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["eta_delay_minutes"] > 0 and body["applied_delay_minutes"] == body["eta_delay_minutes"]
