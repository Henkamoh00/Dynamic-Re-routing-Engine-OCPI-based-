import pytest
from schemas import SmartChargingProfileRequest
from services.cpo_repository import DataSourceError
from services.smart_charging_service import (
    ChargingProfileTargetError,
    apply_charging_profile,
)

CHARGING_LOCATION = "LOC_AUGSBURG_WEST"
CHARGING_EVSE = "EVSE_AUG_1"


# Builds a smart charging request for a given unit and list of limits.
def make_request(limits, unit="W", location_id=CHARGING_LOCATION, evse_uid=CHARGING_EVSE):
    return SmartChargingProfileRequest(
        location_id=location_id,
        evse_uid=evse_uid,
        requesting_truck_id="TRUCK_1",
        charging_profile={
            "charging_rate_unit": unit,
            "charging_profile_period": [
                {"start_period": index * 600, "limit": limit} for index, limit in enumerate(limits)
            ],
        },
    )


# A watt limit below the connector power curtails it and frees the difference.
def test_curtail_with_watt_limit():
    result = apply_charging_profile(make_request([100000]))
    assert result.status == "ACCEPTED"
    assert result.original_max_power_kw == 320.0
    assert result.curtailed_max_power_kw == 100.0
    assert result.freed_capacity_kw == 220.0


# An ampere limit is converted to Watts using the connector maximum voltage.
def test_curtail_with_ampere_limit():
    result = apply_charging_profile(make_request([100], unit="A"))
    assert result.curtailed_max_power_kw == 80.0
    assert result.freed_capacity_kw == 240.0


# When several periods exist the lowest limit is applied.
def test_lowest_period_limit_is_used():
    result = apply_charging_profile(make_request([200000, 50000, 150000]))
    assert result.curtailed_max_power_kw == 50.0


# A limit above the connector power cannot increase power and frees nothing.
def test_limit_above_original_frees_nothing():
    result = apply_charging_profile(make_request([900000]))
    assert result.curtailed_max_power_kw == 320.0
    assert result.freed_capacity_kw == 0.0


# A zero limit throttles the EVSE completely.
def test_zero_limit_frees_everything():
    result = apply_charging_profile(make_request([0]))
    assert result.curtailed_max_power_kw == 0.0
    assert result.freed_capacity_kw == 320.0


# The response echoes the targeted location and EVSE.
def test_response_echoes_target():
    result = apply_charging_profile(make_request([100000]))
    assert result.location_id == CHARGING_LOCATION and result.evse_uid == CHARGING_EVSE


# Curtailment is only allowed for EVSEs that are CHARGING.
@pytest.mark.parametrize(
    "location_id,evse_uid,status",
    [
        ("LOC_MUNICH_SOUTH", "EVSE_MUN_1", "AVAILABLE"),
        ("LOC_REGENSBURG_EAST", "EVSE_REG_1", "BLOCKED"),
        ("LOC_HEIDELBERG_SOUTH", "EVSE_HEI_1", "OUTOFORDER"),
    ],
)
def test_non_charging_evse_rejected(location_id, evse_uid, status):
    with pytest.raises(ChargingProfileTargetError) as excinfo:
        apply_charging_profile(make_request([100000], location_id=location_id, evse_uid=evse_uid))
    assert status in str(excinfo.value)


# Targeting an unknown location or EVSE is rejected.
@pytest.mark.parametrize("location_id,evse_uid", [("LOC_NOPE", CHARGING_EVSE), (CHARGING_LOCATION, "EVSE_NOPE")])
def test_unknown_target_rejected(location_id, evse_uid):
    with pytest.raises(ChargingProfileTargetError):
        apply_charging_profile(make_request([100000], location_id=location_id, evse_uid=evse_uid))


# A missing data file surfaces as a data source error.
def test_missing_data_file_propagates(monkeypatch, tmp_path):
    monkeypatch.setenv("OCPI_DATA_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(DataSourceError):
        apply_charging_profile(make_request([100000]))