from datetime import datetime, timedelta, timezone

import pytest
from schemas import BookingSlot, ReservationCreate, ReservationUpdate
from services import reservation_service
from services.reservation_service import (
    InvalidReservationTargetError,
    ReservationConflictError,
    ReservationNotFoundError,
    create_reservation,
    get_reservation,
    update_reservation,
)

ARRIVAL = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
EXPIRY = datetime(2026, 10, 7, 11, 0, tzinfo=timezone.utc)


# Builds a valid reservation request for the given EVSE.
def make_payload(location_id="LOC_MUNICH_SOUTH", evse_uid="EVSE_MUN_1", truck_id="TRUCK_1"):
    return ReservationCreate(
        location_id=location_id,
        evse_uid=evse_uid,
        truck_id=truck_id,
        booking_slot=BookingSlot(arrival_time=ARRIVAL, expiry_date=EXPIRY),
    )


# Creating a reservation on an AVAILABLE EVSE stores it as ACTIVE.
def test_create_reservation_success():
    reservation = create_reservation("R1", make_payload())
    assert reservation.reservation_id == "R1"
    assert reservation.status == "ACTIVE"
    assert reservation.truck_id == "TRUCK_1"
    assert reservation.booking_slot.arrival_time == ARRIVAL
    assert reservation.last_updated.tzinfo is not None


# A stored reservation can be read back.
def test_get_reservation_returns_stored():
    create_reservation("R1", make_payload())
    assert get_reservation("R1").evse_uid == "EVSE_MUN_1"


# Reading an unknown reservation raises not found.
def test_get_reservation_missing():
    with pytest.raises(ReservationNotFoundError):
        get_reservation("NOPE")


# Reserving an unknown location or EVSE is rejected.
@pytest.mark.parametrize("location_id,evse_uid", [("LOC_NOPE", "EVSE_MUN_1"), ("LOC_MUNICH_SOUTH", "EVSE_NOPE")])
def test_create_reservation_unknown_target(location_id, evse_uid):
    with pytest.raises(InvalidReservationTargetError):
        create_reservation("R1", make_payload(location_id, evse_uid))


# Only AVAILABLE EVSEs can be reserved; CHARGING, BLOCKED and OUTOFORDER are rejected.
@pytest.mark.parametrize(
    "location_id,evse_uid,status",
    [
        ("LOC_FRANKFURT_AIRPORT", "EVSE_FRA_1", "CHARGING"),
        ("LOC_REGENSBURG_EAST", "EVSE_REG_1", "BLOCKED"),
        ("LOC_HEIDELBERG_SOUTH", "EVSE_HEI_1", "OUTOFORDER"),
    ],
)
def test_create_reservation_non_available(location_id, evse_uid, status):
    with pytest.raises(InvalidReservationTargetError) as excinfo:
        create_reservation("R1", make_payload(location_id, evse_uid))
    assert status in str(excinfo.value)


# A failed creation must not store anything.
def test_failed_creation_stores_nothing():
    with pytest.raises(InvalidReservationTargetError):
        create_reservation("R1", make_payload("LOC_FRANKFURT_AIRPORT", "EVSE_FRA_1"))
    with pytest.raises(ReservationNotFoundError):
        get_reservation("R1")


# Reusing a reservation id is a conflict and keeps the original.
def test_create_duplicate_id_conflict():
    create_reservation("R1", make_payload())
    with pytest.raises(ReservationConflictError):
        create_reservation("R1", make_payload(truck_id="OTHER"))
    assert get_reservation("R1").truck_id == "TRUCK_1"


# A delay shifts both arrival and expiry by the same number of minutes.
def test_update_delay_shifts_both_times():
    create_reservation("R1", make_payload())
    updated = update_reservation("R1", ReservationUpdate(delay_minutes=45))
    assert updated.booking_slot.arrival_time == ARRIVAL + timedelta(minutes=45)
    assert updated.booking_slot.expiry_date == EXPIRY + timedelta(minutes=45)
    assert updated.status == "ACTIVE"


# Successive delays accumulate.
def test_update_delay_accumulates():
    create_reservation("R1", make_payload())
    update_reservation("R1", ReservationUpdate(delay_minutes=30))
    updated = update_reservation("R1", ReservationUpdate(delay_minutes=30))
    assert updated.booking_slot.arrival_time == ARRIVAL + timedelta(minutes=60)


# A delay update refreshes the last_updated timestamp.
def test_update_refreshes_last_updated():
    created = create_reservation("R1", make_payload())
    updated = update_reservation("R1", ReservationUpdate(delay_minutes=5))
    assert updated.last_updated >= created.last_updated


# Providing a new booking slot replaces the old one.
def test_update_replaces_booking_slot():
    create_reservation("R1", make_payload())
    new_slot = BookingSlot(arrival_time=ARRIVAL + timedelta(hours=3), expiry_date=EXPIRY + timedelta(hours=4))
    updated = update_reservation("R1", ReservationUpdate(booking_slot=new_slot))
    assert updated.booking_slot == new_slot


# Cancelling marks the reservation as CANCELLED and keeps its slot.
def test_update_cancel():
    create_reservation("R1", make_payload())
    updated = update_reservation("R1", ReservationUpdate(status="CANCELLED"))
    assert updated.status == "CANCELLED"
    assert updated.booking_slot.arrival_time == ARRIVAL


# A cancelled reservation can no longer be modified.
def test_update_cancelled_conflict():
    create_reservation("R1", make_payload())
    update_reservation("R1", ReservationUpdate(status="CANCELLED"))
    with pytest.raises(ReservationConflictError):
        update_reservation("R1", ReservationUpdate(delay_minutes=10))


# Updating an unknown reservation raises not found.
def test_update_missing_reservation():
    with pytest.raises(ReservationNotFoundError):
        update_reservation("NOPE", ReservationUpdate(delay_minutes=10))


# The stored object is replaced, not mutated, so earlier references stay unchanged.
def test_update_does_not_mutate_previous_object():
    created = create_reservation("R1", make_payload())
    update_reservation("R1", ReservationUpdate(delay_minutes=60))
    assert created.booking_slot.arrival_time == ARRIVAL


# Many reservations can coexist independently.
def test_multiple_reservations_are_independent():
    create_reservation("R1", make_payload())
    create_reservation("R2", make_payload("LOC_STUTTGART_EAST", "EVSE_STR_1"))
    update_reservation("R1", ReservationUpdate(delay_minutes=30))
    assert get_reservation("R2").booking_slot.arrival_time == ARRIVAL
    assert len(reservation_service._reservations) == 2