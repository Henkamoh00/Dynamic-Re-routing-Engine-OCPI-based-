import pytest
from factories import make_connector, make_evse, make_location
from pydantic import ValidationError
from schemas import (
    OCPIEVSE,
    AutoAction,
    AutoRerouteRequest,
    BookingSlot,
    Capability,
    ChargingProfile,
    ConnectorType,
    EVSEStatus,
    GeoLocation,
    OCPIConnector,
    OCPILocation,
    ReservationCreate,
    ReservationReplace,
    ReservationUpdate,
    TruckStatus,
)

OCPI_221_STATUSES = {
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


# Optional truck fields must fall back to the documented defaults.
def test_truck_defaults():
    truck = TruckStatus(latitude=48.0, longitude=11.0, battery_percentage=50)
    assert truck.consumption_rate == 1.2
    assert truck.full_capacity_kwh == 300.0


# Boundary battery values 0 and 100 are valid.
@pytest.mark.parametrize("battery", [0, 100, 55.5])
def test_truck_valid_battery_values(battery):
    truck = TruckStatus(latitude=0, longitude=0, battery_percentage=battery)
    assert truck.battery_percentage == battery


# Every out-of-range or non-positive truck field must be rejected.
@pytest.mark.parametrize(
    "overrides",
    [
        {"battery_percentage": -0.1},
        {"battery_percentage": 100.1},
        {"latitude": 90.1},
        {"latitude": -90.1},
        {"longitude": 180.1},
        {"longitude": -180.1},
        {"consumption_rate": 0},
        {"consumption_rate": -1},
        {"full_capacity_kwh": 0},
        {"full_capacity_kwh": -5},
    ],
)
def test_truck_invalid_values(overrides):
    payload = {"latitude": 48.0, "longitude": 11.0, "battery_percentage": 50}
    payload.update(overrides)
    with pytest.raises(ValidationError):
        TruckStatus(**payload)


# Required truck fields cannot be omitted.
@pytest.mark.parametrize("missing", ["latitude", "longitude", "battery_percentage"])
def test_truck_missing_required_fields(missing):
    payload = {"latitude": 48.0, "longitude": 11.0, "battery_percentage": 50}
    payload.pop(missing)
    with pytest.raises(ValidationError):
        TruckStatus(**payload)


# A well-formed GeoLocation keeps coordinates as strings.
def test_geolocation_valid():
    geo = GeoLocation(latitude="48.065100", longitude="11.622000")
    assert geo.latitude == "48.065100"
    assert isinstance(geo.longitude, str)


# Coordinates outside the valid range or non numeric must be rejected.
@pytest.mark.parametrize(
    "latitude,longitude",
    [("91.0", "10.0"), ("-91.0", "10.0"), ("10.0", "181.0"), ("10.0", "-181.0"), ("abc", "10.0"), ("10.0", "xyz")],
)
def test_geolocation_invalid(latitude, longitude):
    with pytest.raises(ValidationError):
        GeoLocation(latitude=latitude, longitude=longitude)


# OCPI limits latitude to 10 characters and longitude to 11 characters.
def test_geolocation_length_limits():
    with pytest.raises(ValidationError):
        GeoLocation(latitude="48.12345678", longitude="11.0")
    with pytest.raises(ValidationError):
        GeoLocation(latitude="48.0", longitude="11.1234567890")


# The status enum must contain exactly the nine values defined by OCPI 2.2.1.
def test_status_enum_matches_ocpi_221():
    assert {status.value for status in EVSEStatus} == OCPI_221_STATUSES


# OCCUPIED is not an OCPI 2.2.1 status and must not exist in the enum.
def test_occupied_is_not_a_valid_status():
    assert "OCCUPIED" not in {status.value for status in EVSEStatus}


# The truck connector type and key capability must exist in their enums.
def test_enums_contain_required_members():
    assert ConnectorType.IEC_62196_T2_COMBO.value == "IEC_62196_T2_COMBO"
    assert Capability.REMOTE_START_STOP_CAPABLE.value == "REMOTE_START_STOP_CAPABLE"
    assert Capability.RESERVABLE.value == "RESERVABLE"


# An explicit max_electric_power in Watts is used as is.
def test_connector_power_explicit():
    connector = OCPIConnector.model_validate(make_connector(power_w=320000))
    assert connector.power_w == 320000


# When max_electric_power is missing a DC connector derives power from V x A.
def test_connector_power_fallback_dc():
    connector = OCPIConnector.model_validate(
        make_connector(power_w=None, max_voltage=800, max_amperage=400)
    )
    assert connector.power_w == 320000


# When max_electric_power is missing a single phase AC connector derives power from V x A.
def test_connector_power_fallback_ac_single_phase():
    connector = OCPIConnector.model_validate(
        make_connector(power_w=None, power_type="AC_1_PHASE", max_voltage=230, max_amperage=32)
    )
    assert connector.power_w == 230 * 32


# When max_electric_power is missing a three phase AC connector applies the square root of three.
def test_connector_power_fallback_ac_three_phase():
    connector = OCPIConnector.model_validate(
        make_connector(power_w=None, power_type="AC_3_PHASE", max_voltage=400, max_amperage=32)
    )
    assert connector.power_w == int(400 * 32 * 3 ** 0.5)


# When max_electric_power is missing a two phase AC connector doubles the single phase power.
def test_connector_power_fallback_ac_two_phase():
    connector = OCPIConnector.model_validate(
        make_connector(power_w=None, power_type="AC_2_PHASE", max_voltage=230, max_amperage=16)
    )
    assert connector.power_w == 230 * 16 * 2


# Connector validation rejects non-positive power, voltage and amperage.
@pytest.mark.parametrize(
    "overrides",
    [{"power_w": 0}, {"power_w": -1}, {"max_voltage": 0}, {"max_amperage": 0}],
)
def test_connector_invalid_numbers(overrides):
    with pytest.raises(ValidationError):
        OCPIConnector.model_validate(make_connector(**overrides))


# Unknown connector standards, formats and power types are rejected.
def test_connector_invalid_enums():
    for key, bad in (("standard", "NOT_A_STANDARD"), ("format", "PLUG"), ("power_type", "DC_PLUS")):
        data = make_connector()
        data[key] = bad
        with pytest.raises(ValidationError):
            OCPIConnector.model_validate(data)


# Connector last_updated is mandatory in OCPI 2.2.1.
def test_connector_requires_last_updated():
    data = make_connector()
    data.pop("last_updated")
    with pytest.raises(ValidationError):
        OCPIConnector.model_validate(data)


# Every one of the nine official statuses is accepted by the EVSE model.
@pytest.mark.parametrize("status", sorted(OCPI_221_STATUSES))
def test_evse_accepts_official_statuses(status):
    evse = OCPIEVSE.model_validate(make_evse(status=status))
    assert evse.status.value == status


# Non-official or wrongly cased statuses are rejected.
@pytest.mark.parametrize("status", ["OCCUPIED", "available", "Available", "BUSY", ""])
def test_evse_rejects_invalid_statuses(status):
    with pytest.raises(ValidationError):
        OCPIEVSE.model_validate(make_evse(status=status))


# An EVSE must expose at least one connector.
def test_evse_requires_connectors():
    with pytest.raises(ValidationError):
        OCPIEVSE.model_validate(make_evse(connectors=[]))


# Capabilities outside the OCPI enum are rejected.
def test_evse_rejects_invalid_capability():
    data = make_evse()
    data["capabilities"] = ["REMOTE_START_STOP_ALLOWED"]
    with pytest.raises(ValidationError):
        OCPIEVSE.model_validate(data)


# The evse_id is limited to 48 characters by the standard.
def test_evse_id_length_limit():
    data = make_evse()
    data["evse_id"] = "X" * 49
    with pytest.raises(ValidationError):
        OCPIEVSE.model_validate(data)


# EVSE last_updated is mandatory in OCPI 2.2.1.
def test_evse_requires_last_updated():
    data = make_evse()
    data.pop("last_updated")
    with pytest.raises(ValidationError):
        OCPIEVSE.model_validate(data)


# A minimal location is valid and defaults to no EVSEs when omitted.
def test_location_valid_and_evses_default():
    location = make_location()
    assert location.country == "DEU"
    data = location.model_dump(mode="json")
    data.pop("evses")
    assert OCPILocation.model_validate(data).evses == []


# Country must be an ISO alpha-3 code and country_code an alpha-2 code.
@pytest.mark.parametrize(
    "field,value",
    [("country", "DE"), ("country", "DEUU"), ("country_code", "DEU"), ("party_id", "FR"), ("party_id", "FRYY")],
)
def test_location_code_lengths(field, value):
    data = make_location().model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValidationError):
        OCPILocation.model_validate(data)


# OCPI requires nested coordinates; top-level latitude/longitude alone is invalid.
def test_location_requires_coordinates_object():
    data = make_location().model_dump(mode="json")
    data.pop("coordinates")
    data["latitude"] = "48.1"
    data["longitude"] = "11.0"
    with pytest.raises(ValidationError):
        OCPILocation.model_validate(data)


# A booking slot whose expiry is later than arrival is valid.
def test_booking_slot_valid():
    slot = BookingSlot(arrival_time="2026-10-07T10:00:00Z", expiry_date="2026-10-07T11:00:00Z")
    assert slot.expiry_date > slot.arrival_time


# A booking slot with equal or reversed times is rejected.
@pytest.mark.parametrize(
    "arrival,expiry",
    [("2026-10-07T10:00:00Z", "2026-10-07T10:00:00Z"), ("2026-10-07T11:00:00Z", "2026-10-07T10:00:00Z")],
)
def test_booking_slot_invalid_window(arrival, expiry):
    with pytest.raises(ValidationError):
        BookingSlot(arrival_time=arrival, expiry_date=expiry)


# A reservation update must contain exactly one change; none is invalid.
def test_reservation_update_requires_one_field():
    with pytest.raises(ValidationError):
        ReservationUpdate()


# A reservation update with two changes at once is invalid.
def test_reservation_update_rejects_multiple_fields():
    with pytest.raises(ValidationError):
        ReservationUpdate(delay_minutes=10, status="CANCELLED")
    with pytest.raises(ValidationError):
        ReservationUpdate(
            delay_minutes=10,
            booking_slot={"arrival_time": "2026-10-07T10:00:00Z", "expiry_date": "2026-10-07T11:00:00Z"},
        )


# Each single valid change form is accepted.
def test_reservation_update_single_valid_forms():
    assert ReservationUpdate(delay_minutes=30).delay_minutes == 30
    assert ReservationUpdate(status="CANCELLED").status == "CANCELLED"
    slot = {"arrival_time": "2026-10-07T10:00:00Z", "expiry_date": "2026-10-07T11:00:00Z"}
    assert ReservationUpdate(booking_slot=slot).booking_slot is not None


# Delay minutes must be between 1 and 1440 and status may only be CANCELLED.
@pytest.mark.parametrize("payload", [{"delay_minutes": 0}, {"delay_minutes": -5}, {"delay_minutes": 1441}, {"status": "ACTIVE"}])
def test_reservation_update_invalid_values(payload):
    with pytest.raises(ValidationError):
        ReservationUpdate(**payload)


# Charging profiles reject unknown units, empty period lists and negative values.
@pytest.mark.parametrize(
    "payload",
    [
        {"charging_rate_unit": "kW", "charging_profile_period": [{"start_period": 0, "limit": 10}]},
        {"charging_rate_unit": "W", "charging_profile_period": []},
        {"charging_rate_unit": "W", "charging_profile_period": [{"start_period": 0, "limit": -1}]},
        {"charging_rate_unit": "W", "charging_profile_period": [{"start_period": -1, "limit": 10}]},
        {"charging_rate_unit": "W", "duration": 0, "charging_profile_period": [{"start_period": 0, "limit": 10}]},
    ],
)
def test_charging_profile_invalid(payload):
    with pytest.raises(ValidationError):
        ChargingProfile(**payload)

# New reservation telemetry fields reject out-of-range values.
@pytest.mark.parametrize(
    "overrides",
    [
        {"battery_percentage": -1},
        {"battery_percentage": 100.5},
        {"battery_capacity_kwh": 0},
        {"target_soc_percent": 0},
        {"target_soc_percent": 100.5},
    ],
)
def test_reservation_create_invalid_telemetry(overrides):
    payload = {
        "location_id": "L",
        "evse_uid": "E",
        "booking_slot": {"arrival_time": "2026-10-07T10:00:00Z", "expiry_date": "2026-10-07T11:00:00Z"},
    }
    payload.update(overrides)
    with pytest.raises(ValidationError):
        ReservationCreate(**payload)


# The PUT body requires a valid booking slot.
def test_reservation_replace_requires_slot():
    with pytest.raises(ValidationError):
        ReservationReplace()
    with pytest.raises(ValidationError):
        ReservationReplace(booking_slot={"arrival_time": "2026-10-07T11:00:00Z", "expiry_date": "2026-10-07T10:00:00Z"})


# The auto request validates the optional ETA inputs.
@pytest.mark.parametrize("speed", [0, -5, 200.5])
def test_auto_request_invalid_speed(speed):
    with pytest.raises(ValidationError):
        AutoRerouteRequest(
            truck={"latitude": 48, "longitude": 11, "battery_percentage": 50},
            reservation_id="R",
            average_speed_kmh=speed,
        )


# The curtailment action exists in the action enum.
def test_auto_action_contains_curtailment():
    assert AutoAction.REBOOKED_WITH_CURTAILMENT.value == "REBOOKED_WITH_CURTAILMENT"
