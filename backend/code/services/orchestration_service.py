from datetime import timedelta

from schemas import (
    AutoAction,
    AutoRerouteRequest,
    AutoRerouteResponse,
    BookingSlot,
    ConflictReason,
    EVSEStatus,
    ReservationCreate,
    ReservationUpdate,
)
from services import reservation_service
from services.cpo_repository import find_evse
from services.routing_service import (
    TRUCK_CONNECTOR_STANDARD,
    best_connector,
    build_station_view,
    calculate_max_range_km,
    distance_to_location,
    find_best_charger,
)

MAX_SHIFT_DELAY_MINUTES = 60


def _detect_conflicts(request: AutoRerouteRequest, location_id: str, evse_uid: str) -> tuple:
    reasons: list[ConflictReason] = []
    target = find_evse(location_id, evse_uid)
    if target is None:
        return [ConflictReason.STATION_UNAVAILABLE], None

    location, evse = target
    if evse.status != EVSEStatus.AVAILABLE:
        reasons.append(ConflictReason.STATION_UNAVAILABLE)

    connector = best_connector(evse, TRUCK_CONNECTOR_STANDARD)
    if connector is None:
        reasons.append(ConflictReason.CONNECTOR_INCOMPATIBLE)

    truck = request.truck
    distance_km = distance_to_location(truck.latitude, truck.longitude, location)
    if distance_km >= calculate_max_range_km(truck):
        reasons.append(ConflictReason.OUT_OF_RANGE)

    if request.delay_minutes > MAX_SHIFT_DELAY_MINUTES:
        reasons.append(ConflictReason.DELAY_EXCEEDS_SHIFT_LIMIT)

    return reasons, (location, evse, connector, distance_km)


def auto_reroute(request: AutoRerouteRequest) -> AutoRerouteResponse:
    previous = reservation_service.get_reservation(request.reservation_id)
    if previous.status == "CANCELLED":
        raise reservation_service.ReservationConflictError(
            f"Reservation {request.reservation_id} is cancelled and cannot be modified."
        )

    reasons, resolved = _detect_conflicts(request, previous.location_id, previous.evse_uid)

    if not reasons:
        location, evse, connector, distance_km = resolved
        station, summary = build_station_view(
            request.truck, location, evse, connector, distance_km
        )
        if request.delay_minutes > 0:
            active = reservation_service.update_reservation(
                request.reservation_id,
                ReservationUpdate(delay_minutes=request.delay_minutes),
            )
            action = AutoAction.SHIFTED
            message = (
                f"Delay of {request.delay_minutes} min detected; reservation "
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
            message=message,
        )

    best = find_best_charger(request.truck)
    station = best.recommended_charging_station
    delta = timedelta(minutes=request.delay_minutes)
    new_id = request.new_reservation_id or f"{request.reservation_id}-REBOOK"

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
        ),
    )
    reservation_service.update_reservation(
        request.reservation_id, ReservationUpdate(status="CANCELLED")
    )
    cancelled = reservation_service.get_reservation(request.reservation_id)

    return AutoRerouteResponse(
        status="success",
        action=AutoAction.REBOOKED,
        conflict_detected=True,
        conflict_reasons=reasons,
        previous_reservation=cancelled,
        active_reservation=new_reservation,
        truck_summary=best.truck_summary,
        charging_station=station,
        message=(
            f"Conflict detected ({', '.join(r.value for r in reasons)}); reservation "
            f"{request.reservation_id} cancelled and rebooked as {new_id} at {station.station_id}."
        ),
    )