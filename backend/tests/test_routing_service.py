import pytest
from factories import make_connector, make_evse, make_location
from geo import haversine_distance
from schemas import ConnectorType, TruckStatus
from services import routing_service
from services.routing_service import (
    NoChargerAvailableError,
    calculate_arrival_soc,
    calculate_max_range_km,
    find_best_charger,
    search_locations,
    watts_to_kw,
)

NON_AVAILABLE_STATUSES = [
    "BLOCKED",
    "CHARGING",
    "INOPERATIVE",
    "OUTOFORDER",
    "PLANNED",
    "REMOVED",
    "RESERVED",
    "UNKNOWN",
]

TRUCK_LAT = 48.0
TRUCK_LON = 11.0


# Creates a truck at a fixed position with configurable battery and consumption settings.
def make_truck(battery=80.0, consumption=1.2, capacity=300.0) -> TruckStatus:
    return TruckStatus(
        latitude=TRUCK_LAT,
        longitude=TRUCK_LON,
        battery_percentage=battery,
        consumption_rate=consumption,
        full_capacity_kwh=capacity,
    )


# Creates a location north of the truck; each 0.1 degree of latitude is about 11.12 km.
def north_location(loc_id, lat_offset_deg, evses=None, **kwargs):
    latitude = f"{TRUCK_LAT + lat_offset_deg:.6f}"
    return make_location(loc_id=loc_id, latitude=latitude, longitude=f"{TRUCK_LON:.6f}", evses=evses, **kwargs)


# Watts are converted to kilowatts by dividing by one thousand.
@pytest.mark.parametrize("watts,kw", [(320000, 320.0), (150000, 150.0), (160000, 160.0), (1500, 1.5), (0, 0.0)])
def test_watts_to_kw(watts, kw):
    assert watts_to_kw(watts) == kw


# Maximum range is the stored energy divided by the consumption rate.
def test_max_range_basic():
    assert calculate_max_range_km(make_truck(80, 1.2, 300)) == pytest.approx(200.0)


# Range scales with custom consumption and capacity values.
def test_max_range_custom_values():
    assert calculate_max_range_km(make_truck(50, 2.0, 400)) == pytest.approx(100.0)


# An empty battery gives zero range and a full battery gives the maximum range.
def test_max_range_extremes():
    assert calculate_max_range_km(make_truck(0)) == 0.0
    assert calculate_max_range_km(make_truck(100, 1.0, 300)) == pytest.approx(300.0)


# Arrival SoC follows: SoC - (distance x consumption / capacity x 100).
def test_arrival_soc_formula():
    assert calculate_arrival_soc(make_truck(80, 1.2, 300), 50) == pytest.approx(60.0)


# Zero distance leaves the battery percentage unchanged.
def test_arrival_soc_zero_distance():
    assert calculate_arrival_soc(make_truck(73.5), 0) == pytest.approx(73.5)


# Driving the full range drains the battery to zero percent.
def test_arrival_soc_full_range_reaches_zero():
    truck = make_truck(80, 1.2, 300)
    assert calculate_arrival_soc(truck, calculate_max_range_km(truck)) == pytest.approx(0.0, abs=1e-9)


# The new SoC formula must equal the legacy proportional formula for all inputs.
@pytest.mark.parametrize(
    "battery,capacity,consumption,distance",
    [(80, 300, 1.2, 50), (40, 300, 1.2, 8.33), (100, 500, 1.5, 120), (25, 200, 0.9, 30), (60, 350, 2.2, 10)],
)
def test_arrival_soc_matches_legacy_formula(battery, capacity, consumption, distance):
    truck = make_truck(battery, consumption, capacity)
    max_range = calculate_max_range_km(truck)
    legacy = ((max_range - distance) / max_range) * battery
    assert calculate_arrival_soc(truck, distance) == pytest.approx(legacy)


# The response must describe the single nearest available station with converted kW power.
def test_best_charger_response_content():
    location = north_location("LOC_NEAR", 0.1, evses=[make_evse("EVSE_A", "AVAILABLE", [make_connector(320000)])])
    result = find_best_charger(make_truck(80), [location])
    station = result.recommended_charging_station
    expected_distance = haversine_distance(TRUCK_LAT, TRUCK_LON, 48.1, 11.0)
    assert result.status == "success"
    assert station.station_id == "LOC_NEAR"
    assert station.station_name == "Test Location"
    assert station.evse_uid == "EVSE_A"
    assert station.max_power_kw == 320.0
    assert station.distance_km == pytest.approx(expected_distance, abs=0.01)
    assert result.truck_summary.current_range_km == 200.0
    expected_soc = 80 - expected_distance * 1.2 / 300 * 100
    assert result.truck_summary.estimated_battery_at_arrival == pytest.approx(expected_soc, abs=0.05)


# The estimated arrival battery is rounded to one decimal and the range to two decimals.
def test_response_rounding():
    location = north_location("LOC_NEAR", 0.1)
    result = find_best_charger(make_truck(80), [location])
    soc = result.truck_summary.estimated_battery_at_arrival
    assert soc == round(soc, 1)
    assert result.truck_summary.current_range_km == round(result.truck_summary.current_range_km, 2)


# When the location has no name the location id is used as the station name.
def test_station_name_falls_back_to_id():
    location = north_location("LOC_NONAME", 0.1, name=None)
    result = find_best_charger(make_truck(80), [location])
    assert result.recommended_charging_station.station_name == "LOC_NONAME"


# The closest available station wins regardless of input order.
def test_nearest_station_wins():
    locations = [
        north_location("LOC_FAR", 0.5),
        north_location("LOC_MID", 0.3),
        north_location("LOC_NEAR", 0.1),
    ]
    result = find_best_charger(make_truck(80), locations)
    assert result.recommended_charging_station.station_id == "LOC_NEAR"


# A nearer station that is not AVAILABLE is skipped in favour of a farther available one.
def test_skips_nearer_unavailable_station():
    locations = [
        north_location("LOC_NEAR_BUSY", 0.1, evses=[make_evse("E1", "CHARGING")]),
        north_location("LOC_FAR_FREE", 0.3, evses=[make_evse("E2", "AVAILABLE")]),
    ]
    result = find_best_charger(make_truck(80), locations)
    assert result.recommended_charging_station.station_id == "LOC_FAR_FREE"


# Every status other than AVAILABLE must be ignored by the status filter.
@pytest.mark.parametrize("status", NON_AVAILABLE_STATUSES)
def test_non_available_status_is_ignored(status):
    location = north_location("LOC_X", 0.1, evses=[make_evse("E1", status)])
    with pytest.raises(NoChargerAvailableError):
        find_best_charger(make_truck(80), [location])


# Inside one location only the AVAILABLE EVSE can be recommended.
def test_picks_available_evse_within_mixed_location():
    evses = [make_evse("E_BUSY", "CHARGING"), make_evse("E_FREE", "AVAILABLE"), make_evse("E_OOO", "OUTOFORDER")]
    result = find_best_charger(make_truck(80), [north_location("LOC_MIX", 0.1, evses=evses)])
    assert result.recommended_charging_station.evse_uid == "E_FREE"


# A station farther than the remaining range is excluded and an error is raised.
def test_out_of_range_station_raises():
    far = north_location("LOC_FAR", 1.0)
    with pytest.raises(NoChargerAvailableError):
        find_best_charger(make_truck(battery=10, consumption=1.2, capacity=300), [far])


# A station beyond range is skipped but a reachable one is still returned.
def test_in_range_station_chosen_over_out_of_range():
    locations = [north_location("LOC_FAR", 1.0), north_location("LOC_NEAR", 0.2)]
    result = find_best_charger(make_truck(battery=20, consumption=1.2, capacity=300), locations)
    assert result.recommended_charging_station.station_id == "LOC_NEAR"


# A distance exactly equal to the range is excluded because the range filter is strict.
def test_range_boundary_is_strict(monkeypatch):
    location = north_location("LOC_EDGE", 0.1)
    truck = make_truck(battery=50, consumption=1.0, capacity=100)
    assert calculate_max_range_km(truck) == 50.0
    monkeypatch.setattr(routing_service, "haversine_distance", lambda *args: 50.0)
    with pytest.raises(NoChargerAvailableError):
        find_best_charger(truck, [location])
    monkeypatch.setattr(routing_service, "haversine_distance", lambda *args: 49.999)
    assert find_best_charger(truck, [location]).recommended_charging_station.station_id == "LOC_EDGE"


# A completely empty battery can reach nothing.
def test_zero_battery_raises():
    with pytest.raises(NoChargerAvailableError):
        find_best_charger(make_truck(0), [north_location("LOC_NEAR", 0.1)])


# An empty location list raises the no-charger error.
def test_no_locations_raises():
    with pytest.raises(NoChargerAvailableError):
        find_best_charger(make_truck(80), [])


# A location without EVSEs is skipped.
def test_location_without_evses_is_skipped():
    empty = north_location("LOC_EMPTY", 0.1, evses=[])
    full = north_location("LOC_FULL", 0.2)
    result = find_best_charger(make_truck(80), [empty, full])
    assert result.recommended_charging_station.station_id == "LOC_FULL"


# The error message keeps the original critical warning text.
def test_error_message_text():
    with pytest.raises(NoChargerAvailableError) as excinfo:
        find_best_charger(make_truck(80), [])
    assert "No available chargers found within the truck's remaining battery range" in str(excinfo.value)


# Stations without a truck-compatible connector are never recommended.
def test_connector_standard_filter_excludes_other_types():
    evse = make_evse("E1", "AVAILABLE", [make_connector(50000, standard="CHADEMO")])
    with pytest.raises(NoChargerAvailableError):
        find_best_charger(make_truck(80), [north_location("LOC_CHADEMO", 0.1, evses=[evse])])


# With several connector types only the truck-compatible one is used for power reporting.
def test_connector_filter_uses_matching_connector_power():
    connectors = [make_connector(400000, standard="CHADEMO", connector_id="1"), make_connector(150000, connector_id="2")]
    evse = make_evse("E1", "AVAILABLE", connectors)
    result = find_best_charger(make_truck(80), [north_location("LOC_BOTH", 0.1, evses=[evse])])
    assert result.recommended_charging_station.max_power_kw == 150.0


# When one EVSE has several matching connectors the most powerful one is reported.
def test_best_connector_is_highest_power():
    connectors = [make_connector(150000, connector_id="1"), make_connector(350000, connector_id="2")]
    evse = make_evse("E1", "AVAILABLE", connectors)
    result = find_best_charger(make_truck(80), [north_location("LOC_MULTI", 0.1, evses=[evse])])
    assert result.recommended_charging_station.max_power_kw == 350.0


# Between two stations at the same distance the more powerful one wins.
def test_tie_on_distance_prefers_higher_power():
    low = north_location("LOC_LOW", 0.1, evses=[make_evse("E_LOW", "AVAILABLE", [make_connector(150000)])])
    high = north_location("LOC_HIGH", 0.1, evses=[make_evse("E_HIGH", "AVAILABLE", [make_connector(350000)])])
    result = find_best_charger(make_truck(80), [low, high])
    assert result.recommended_charging_station.station_id == "LOC_HIGH"


# A custom connector standard can be requested explicitly.
def test_custom_connector_standard_parameter():
    evse = make_evse("E1", "AVAILABLE", [make_connector(50000, standard="CHADEMO")])
    result = find_best_charger(
        make_truck(80), [north_location("LOC_CHADEMO", 0.1, evses=[evse])], connector_standard=ConnectorType.CHADEMO
    )
    assert result.recommended_charging_station.station_id == "LOC_CHADEMO"


# A connector without max_electric_power falls back to voltage x amperage for reporting.
def test_power_fallback_when_max_electric_power_missing():
    evse = make_evse("E1", "AVAILABLE", [make_connector(None, max_voltage=800, max_amperage=400)])
    result = find_best_charger(make_truck(80), [north_location("LOC_FALLBACK", 0.1, evses=[evse])])
    assert result.recommended_charging_station.max_power_kw == 320.0


# Location search returns only locations inside the radius sorted by distance.
def test_search_radius_and_sorting(monkeypatch):
    locations = (north_location("LOC_B", 0.2), north_location("LOC_A", 0.1), north_location("LOC_FAR", 2.0))
    monkeypatch.setattr(routing_service, "get_locations", lambda: locations)
    results = search_locations(TRUCK_LAT, TRUCK_LON, radius_km=50)
    assert [item.location_id for item in results] == ["LOC_A", "LOC_B"]
    assert results[0].distance_km < results[1].distance_km


# A location exactly on the radius boundary is included, one slightly beyond is excluded.
def test_search_radius_boundary_inclusive(monkeypatch):
    locations = (north_location("LOC_EDGE", 0.1),)
    monkeypatch.setattr(routing_service, "get_locations", lambda: locations)
    monkeypatch.setattr(routing_service, "haversine_distance", lambda *args: 100.0)
    assert len(search_locations(TRUCK_LAT, TRUCK_LON, radius_km=100.0)) == 1
    assert len(search_locations(TRUCK_LAT, TRUCK_LON, radius_km=99.99)) == 0


# Unpublished locations never appear in search results.
def test_search_excludes_unpublished(monkeypatch):
    locations = (north_location("LOC_HIDDEN", 0.1, publish=False), north_location("LOC_SHOWN", 0.2))
    monkeypatch.setattr(routing_service, "get_locations", lambda: locations)
    results = search_locations(TRUCK_LAT, TRUCK_LON, radius_km=100)
    assert [item.location_id for item in results] == ["LOC_SHOWN"]


# Locations without a matching connector standard are excluded from search.
def test_search_excludes_locations_without_matching_connector(monkeypatch):
    evse = make_evse("E1", "AVAILABLE", [make_connector(50000, standard="CHADEMO")])
    locations = (north_location("LOC_CHADEMO", 0.1, evses=[evse]), north_location("LOC_TRUCK", 0.2))
    monkeypatch.setattr(routing_service, "get_locations", lambda: locations)
    results = search_locations(TRUCK_LAT, TRUCK_LON, radius_km=100)
    assert [item.location_id for item in results] == ["LOC_TRUCK"]


# The available EVSE count only includes AVAILABLE EVSEs with a matching connector.
def test_search_available_count_and_max_power(monkeypatch):
    evses = [
        make_evse("E1", "AVAILABLE", [make_connector(150000)]),
        make_evse("E2", "CHARGING", [make_connector(350000)]),
        make_evse("E3", "AVAILABLE", [make_connector(200000)]),
    ]
    locations = (north_location("LOC_COUNT", 0.1, evses=evses),)
    monkeypatch.setattr(routing_service, "get_locations", lambda: locations)
    result = search_locations(TRUCK_LAT, TRUCK_LON, radius_km=100)[0]
    assert result.available_evses == 2
    assert result.max_power_kw == 350.0


# A location whose EVSEs are all busy is still listed with zero available EVSEs.
def test_search_lists_location_with_zero_available(monkeypatch):
    locations = (north_location("LOC_BUSY", 0.1, evses=[make_evse("E1", "CHARGING")]),)
    monkeypatch.setattr(routing_service, "get_locations", lambda: locations)
    result = search_locations(TRUCK_LAT, TRUCK_LON, radius_km=100)
    assert result[0].available_evses == 0


# An empty result list is returned when nothing matches the radius.
def test_search_empty_result(monkeypatch):
    monkeypatch.setattr(routing_service, "get_locations", lambda: (north_location("LOC_FAR", 3.0),))
    assert search_locations(TRUCK_LAT, TRUCK_LON, radius_km=10) == []