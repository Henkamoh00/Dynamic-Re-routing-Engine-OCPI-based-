from datetime import datetime, timedelta, timezone
from threading import Lock

from schemas import (
    BookingSlot,
    EVSEStatus,
    Reservation,
    ReservationCreate,
    ReservationUpdate,
)
from services.cpo_repository import find_evse

_reservations: dict[str, Reservation] = {}
_lock = Lock()


class ReservationNotFoundError(Exception):
    pass


class ReservationConflictError(Exception):
    pass


class InvalidReservationTargetError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Creates a reservation; allow_charging lets a CHARGING EVSE be booked after a throttle-down.
def create_reservation(
    reservation_id: str, payload: ReservationCreate, allow_charging: bool = False
) -> Reservation:
    target = find_evse(payload.location_id, payload.evse_uid)
    if target is None:
        raise InvalidReservationTargetError(
            f"EVSE {payload.evse_uid} not found at location {payload.location_id}."
        )
    _, evse = target
    allowed = {EVSEStatus.AVAILABLE}
    if allow_charging:
        allowed.add(EVSEStatus.CHARGING)
    if evse.status not in allowed:
        raise InvalidReservationTargetError(
            f"EVSE {payload.evse_uid} is not AVAILABLE (current status: {evse.status.value})."
        )

    reservation = Reservation(
        reservation_id=reservation_id,
        location_id=payload.location_id,
        evse_uid=payload.evse_uid,
        truck_id=payload.truck_id,
        status="ACTIVE",
        booking_slot=payload.booking_slot,
        battery_percentage=payload.battery_percentage,
        battery_capacity_kwh=payload.battery_capacity_kwh,
        target_soc_percent=payload.target_soc_percent,
        curtailment_booked=allow_charging and evse.status == EVSEStatus.CHARGING,
        last_updated=_now(),
    )

    with _lock:
        if reservation_id in _reservations:
            raise ReservationConflictError(f"Reservation {reservation_id} already exists.")
        _reservations[reservation_id] = reservation
    return reservation


# Lists stored reservations, optionally only the ACTIVE ones.
def list_reservations(active_only: bool = False) -> list[Reservation]:
    with _lock:
        items = list(_reservations.values())
    if active_only:
        items = [item for item in items if item.status == "ACTIVE"]
    return items


def get_reservation(reservation_id: str) -> Reservation:
    with _lock:
        reservation = _reservations.get(reservation_id)
    if reservation is None:
        raise ReservationNotFoundError(f"Reservation {reservation_id} not found.")
    return reservation


# Replaces the whole booking slot (HTTP PUT semantics).
def replace_booking_slot(reservation_id: str, slot: BookingSlot) -> Reservation:
    return update_reservation(reservation_id, ReservationUpdate(booking_slot=slot))


def update_reservation(reservation_id: str, payload: ReservationUpdate) -> Reservation:
    with _lock:
        current = _reservations.get(reservation_id)
        if current is None:
            raise ReservationNotFoundError(f"Reservation {reservation_id} not found.")
        if current.status == "CANCELLED":
            raise ReservationConflictError(
                f"Reservation {reservation_id} is cancelled and cannot be modified."
            )

        updated = current.model_copy(deep=True)

        if payload.status == "CANCELLED":
            updated.status = "CANCELLED"
        elif payload.delay_minutes is not None:
            delta = timedelta(minutes=payload.delay_minutes)
            updated.booking_slot = BookingSlot(
                arrival_time=current.booking_slot.arrival_time + delta,
                expiry_date=current.booking_slot.expiry_date + delta,
            )
        elif payload.booking_slot is not None:
            updated.booking_slot = payload.booking_slot

        updated.last_updated = _now()
        _reservations[reservation_id] = updated
        return updated
