from schemas import (
    EVSEStatus,
    SmartChargingProfileRequest,
    SmartChargingProfileResponse,
)
from services.cpo_repository import find_evse

CURTAILABLE_STATUSES = {EVSEStatus.CHARGING}
WATTS_PER_KILOWATT = 1000.0


class ChargingProfileTargetError(Exception):
    pass


def apply_charging_profile(
    request: SmartChargingProfileRequest,
) -> SmartChargingProfileResponse:
    target = find_evse(request.location_id, request.evse_uid)
    if target is None:
        raise ChargingProfileTargetError(
            f"EVSE {request.evse_uid} not found at location {request.location_id}."
        )
    _, evse = target

    if evse.status not in CURTAILABLE_STATUSES:
        raise ChargingProfileTargetError(
            f"EVSE {request.evse_uid} has status {evse.status.value}; "
            "smart charging curtailment applies only to EVSEs with status CHARGING."
        )

    connector = max(evse.connectors, key=lambda c: c.power_w)
    profile = request.charging_profile
    lowest_limit = min(period.limit for period in profile.charging_profile_period)

    if profile.charging_rate_unit == "A":
        requested_limit_w = lowest_limit * connector.max_voltage
    else:
        requested_limit_w = lowest_limit

    original_w = float(connector.power_w)
    curtailed_w = min(requested_limit_w, original_w)
    freed_w = original_w - curtailed_w

    return SmartChargingProfileResponse(
        status="ACCEPTED",
        location_id=request.location_id,
        evse_uid=request.evse_uid,
        original_max_power_kw=round(original_w / WATTS_PER_KILOWATT, 2),
        curtailed_max_power_kw=round(curtailed_w / WATTS_PER_KILOWATT, 2),
        freed_capacity_kw=round(freed_w / WATTS_PER_KILOWATT, 2),
        message="Charging profile accepted; capacity released for the incoming truck.",
    )