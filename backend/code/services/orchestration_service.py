import math
from datetime import datetime, timedelta, timezone

from schemas import (
    AutoAction,
    AutoRerouteRequest,
    AutoRerouteResponse,
    BookingSlot,
    ChargingProfile,
    ChargingProfilePeriod,
    ConflictReason,
    EVSEStatus,
    OCPIConnector,
    OCPIEVSE,
    OCPILocation,
    RecommendedChargingStation,
    Reservation,
    ReservationCreate,
    ReservationUpdate,
    SmartChargingProfileRequest,
    SmartChargingProfileResponse,
    TruckSummary,
)
from services import reservation_service, smart_charging_service
from services.cpo_repository import find_evse, get_locations
from services.routing_service import (
    TRUCK_CONNECTOR_STANDARD,
    NoChargerAvailableError,
    best_connector,
    build_station_view,
    calculate_max_range_km,
    distance_to_location,
    find_best_charger,
)

MAX_SHIFT_DELAY_MINUTES = 60
CURTAILMENT_RATIO = 0.5

Candidate = tuple[float, int, OCPILocation, OCPIEVSE, OCPIConnector]


# Returns a timezone-aware UTC datetime (naive values are interpreted as UTC).
def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# Estimates how many minutes late the truck will be, from its distance and average speed.
def _eta_delay_minutes(
    request: AutoRerouteRequest, reservation: Reservation, distance_km: float | None
) -> int | None:
    if request.average_speed_kmh is None or distance_km is None:
        return None
    as_of = _aware(request.as_of) if request.as_of else datetime.now(timezone.utc)
    travel = timedelta(hours=distance_km / request.average_speed_kmh)
    arrival = _aware(reservation.booking_slot.arrival_time)
    late_minutes = (as_of + travel - arrival).total_seconds() / 60.0
    return max(0, math.ceil(late_minutes))


# Detects every conflict on the reserved EVSE and returns the resolved target for reuse.
def _detect_conflicts(
    request: AutoRerouteRequest, reservation: Reservation, delay_minutes: int
) -> tuple[list[ConflictReason], tuple | None]:
    reasons: list[ConflictReason] = []
    target = find_evse(reservation.location_id, reservation.evse_uid)
    if target is None:
        return [ConflictReason.STATION_UNAVAILABLE], None

    location, evse = target
    acceptable = {EVSEStatus.AVAILABLE}
    if reservation.curtailment_booked:
        acceptable.add(EVSEStatus.CHARGING)
    if evse.status not in acceptable:
        reasons.append(ConflictReason.STATION_UNAVAILABLE)

    connector = best_connector(evse, TRUCK_CONNECTOR_STANDARD)
    if connector is None:
        reasons.append(ConflictReason.CONNECTOR_INCOMPATIBLE)

    truck = request.truck
    distance_km = distance_to_location(truck.latitude, truck.longitude, location)
    if distance_km >= calculate_max_range_km(truck):
        reasons.append(ConflictReason.OUT_OF_RANGE)

    if delay_minutes > MAX_SHIFT_DELAY_MINUTES:
        reasons.append(ConflictReason.DELAY_EXCEEDS_SHIFT_LIMIT)

    return reasons, (location, evse, connector, distance_km)


# Finds the nearest reachable CHARGING EVSE whose session can be throttled to free capacity.
def _find_curtailment_candidate(request: AutoRerouteRequest) -> Candidate | None:
    truck = request.truck
    max_range_km = calculate_max_range_km(truck)
    best: Candidate | None = None
    for location in get_locations():
        distance_km = distance_to_location(truck.latitude, truck.longitude, location)
        if distance_km >= max_range_km:
            continue
        for evse in location.evses:
            if evse.status not in smart_charging_service.CURTAILABLE_STATUSES:
                continue
            connector = best_connector(evse, TRUCK_CONNECTOR_STANDARD)
            if connector is None:
                continue
            candidate = (distance_km, -connector.power_w, location, evse, connector)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
    return best


# Requests a throttle-down on the occupied EVSE so capacity is freed for the delayed truck.
def _request_curtailment(
    location: OCPILocation, evse: OCPIEVSE, connector: OCPIConnector, truck_id: str | None
) -> SmartChargingProfileResponse:
    limit_w = int(connector.power_w * CURTAILMENT_RATIO)
    return smart_charging_service.apply_charging_profile(
        SmartChargingProfileRequest(
            location_id=location.id,
            evse_uid=evse.uid,
            requesting_truck_id=truck_id,
            charging_profile=ChargingProfile(
                charging_rate_unit="W",
                charging_profile_period=[ChargingProfilePeriod(start_period=0, limit=limit_w)],
            ),
        )
    )


# Keeps, shifts, or rebooks a reservation so the truck is never sent to a compromised charger.
def auto_reroute(request: AutoRerouteRequest) -> AutoRerouteResponse:
    previous = reservation_service.get_reservation(request.reservation_id)
    if previous.status == "CANCELLED":
        raise reservation_service.ReservationConflictError(
            f"Reservation {request.reservation_id} is cancelled and cannot be modified."
        )

    reserved = find_evse(previous.location_id, previous.evse_uid)
    reserved_distance = None
    if reserved is not None:
        reserved_distance = distance_to_location(
            request.truck.latitude, request.truck.longitude, reserved[0]
        )
    eta_delay = _eta_delay_minutes(request, previous, reserved_distance)
    applied_delay = max(request.delay_minutes, eta_delay or 0)

    reasons, resolved = _detect_conflicts(request, previous, applied_delay)

    if not reasons:
        location, evse, connector, distance_km = resolved
        station, summary = build_station_view(
            request.truck, location, evse, connector, distance_km
        )
        if applied_delay > 0:
            active = reservation_service.update_reservation(
                request.reservation_id,
                ReservationUpdate(delay_minutes=applied_delay),
            )
            action = AutoAction.SHIFTED
            message = (
                f"Delay of {applied_delay} min detected; reservation "
                f"{active.reservation_id} shifted at the same station."
            )
        else:
            active = previous
            action = AutoAction.NO_ACTION
            message = "No conflict detected; the existing reservation remains valid."
        return AutoRerouteResponse(
            status="success",
            action=action,
            conflict_detected=False,
            conflict_reasons=[],
            previous_reservation=previous,
            active_reservation=active,
            truck_summary=summary,
            charging_station=station,
            applied_delay_minutes=applied_delay,
            eta_delay_minutes=eta_delay,
            message=message,
        )

    delta = timedelta(minutes=applied_delay)
    new_id = request.new_reservation_id or f"{request.reservation_id}-REBOOK"
    curtail_target: tuple[OCPILocation, OCPIEVSE, OCPIConnector] | None = None
    station: RecommendedChargingStation
    summary: TruckSummary

    try:
        best = find_best_charger(request.truck)
        station = best.recommended_charging_station
        summary = best.truck_summary
        action = AutoAction.REBOOKED
    except NoChargerAvailableError:
        candidate = _find_curtailment_candidate(request)
        if candidate is None:
            raise
        distance_km, _, location, evse, connector = candidate
        station, summary = build_station_view(
            request.truck, location, evse, connector, distance_km
        )
        curtail_target = (location, evse, connector)
        action = AutoAction.REBOOKED_WITH_CURTAILMENT

    new_reservation = reservation_service.create_reservation(
        new_id,
        ReservationCreate(
            location_id=station.station_id,
            evse_uid=station.evse_uid,
            truck_id=previous.truck_id,
            booking_slot=BookingSlot(
                arrival_time=previous.booking_slot.arrival_time + delta,
                expiry_date=previous.booking_slot.expiry_date + delta,
            ),
            battery_percentage=max(0.0, min(100.0, summary.estimated_battery_at_arrival)),
            battery_capacity_kwh=request.truck.full_capacity_kwh,
            target_soc_percent=previous.target_soc_percent,
        ),
        allow_charging=curtail_target is not None,
    )

    smart: SmartChargingProfileResponse | None = None
    if curtail_target is not None:
        try:
            smart = _request_curtailment(*curtail_target, previous.truck_id)
        except smart_charging_service.ChargingProfileTargetError:
            reservation_service.update_reservation(new_id, ReservationUpdate(status="CANCELLED"))
            raise

    reservation_service.update_reservation(
        request.reservation_id, ReservationUpdate(status="CANCELLED")
    )
    cancelled = reservation_service.get_reservation(request.reservation_id)

    reason_text = ", ".join(reason.value for reason in reasons)
    if smart is not None:
        message = (
            f"Conflict detected ({reason_text}); no AVAILABLE station in range. "
            f"Throttled {station.evse_uid} to {smart.curtailed_max_power_kw} kW "
            f"(freed {smart.freed_capacity_kw} kW) and rebooked {request.reservation_id} "
            f"as {new_id} at {station.station_id}."
        )
    else:
        message = (
            f"Conflict detected ({reason_text}); reservation {request.reservation_id} "
            f"cancelled and rebooked as {new_id} at {station.station_id}."
        )

    return AutoRerouteResponse(
        status="success",
        action=action,
        conflict_detected=True,
        conflict_reasons=reasons,
        previous_reservation=cancelled,
        active_reservation=new_reservation,
        truck_summary=summary,
        charging_station=station,
        applied_delay_minutes=applied_delay,
        eta_delay_minutes=eta_delay,
        smart_charging=smart,
        message=message,
    )
