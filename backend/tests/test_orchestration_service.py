import pytest
from factories import set_evse_status
from schemas import (
    AutoAction,
    AutoRerouteRequest,
    BookingSlot,
    ConflictReason,
    ReservationCreate,
    TruckStatus,
)
from services import orchestration_service, reservation_service
from services.routing_service import NoChargerAvailableError

MUNICH_LOC = "LOC_MUNICH_SOUTH"
MUNICH_EVSE = "EVSE_MUN_1"
ARRIVAL = "2026-10-07T10:00:00Z"
EXPIRY = "2026-10-07T11:00:00Z"


# Creates the baseline reservation at Munich South used by the scenarios.
def book_munich(reservation_id="RES_1"):
    return reservation_service.create_reservation(
        reservation_id,
        ReservationCreate(
            location_id=MUNICH_LOC,
            evse_uid=MUNICH_EVSE,
            truck_id="TRUCK_1",
            booking_slot=BookingSlot(arrival_time=ARRIVAL, expiry_date=EXPIRY),
        ),
    )


# Builds a request for a truck near Munich with a comfortable 200 km range.
def make_request(delay=0, battery=80.0, reservation_id="RES_1", new_id=None):
    return AutoRerouteRequest(
        truck=TruckStatus(latitude=48.1, longitude=11.6, battery_percentage=battery),
        reservation_id=reservation_id,
        delay_minutes=delay,
        new_reservation_id=new_id,
    )


# With a healthy station and no delay nothing changes.
def test_no_conflict_no_action(live_data):
    book_munich()
    result = orchestration_service.auto_reroute(make_request())
    assert result.action == AutoAction.NO_ACTION
    assert result.conflict_detected is False
    assert result.active_reservation.reservation_id == "RES_1"
    assert result.charging_station.station_id == MUNICH_LOC


# A small delay shifts the existing reservation at the same station.
def test_small_delay_shifts_reservation(live_data):
    original = book_munich()
    result = orchestration_service.auto_reroute(make_request(delay=30))
    assert result.action == AutoAction.SHIFTED
    shift = result.active_reservation.booking_slot.arrival_time - original.booking_slot.arrival_time
    assert shift.total_seconds() == 30 * 60
    assert result.active_reservation.status == "ACTIVE"


# A delay at the limit is still shifted, one minute above it triggers a rebook.
def test_delay_limit_boundary(live_data):
    book_munich()
    at_limit = orchestration_service.auto_reroute(make_request(delay=orchestration_service.MAX_SHIFT_DELAY_MINUTES))
    assert at_limit.action == AutoAction.SHIFTED
    over = orchestration_service.auto_reroute(make_request(delay=orchestration_service.MAX_SHIFT_DELAY_MINUTES + 1))
    assert over.action == AutoAction.REBOOKED
    assert over.conflict_reasons == [ConflictReason.DELAY_EXCEEDS_SHIFT_LIMIT]


# When the station goes out of order the booking is cancelled and rebooked elsewhere.
def test_station_outage_triggers_rebook(live_data):
    book_munich()
    set_evse_status(live_data, MUNICH_EVSE, "OUTOFORDER")
    result = orchestration_service.auto_reroute(make_request())
    assert result.action == AutoAction.REBOOKED
    assert ConflictReason.STATION_UNAVAILABLE in result.conflict_reasons
    assert result.previous_reservation.status == "CANCELLED"
    assert result.active_reservation.status == "ACTIVE"
    assert result.active_reservation.reservation_id == "RES_1-REBOOK"
    assert result.charging_station.station_id == "LOC_INGOLSTADT_HUB"
    assert result.active_reservation.location_id == "LOC_INGOLSTADT_HUB"


# A custom id for the new reservation is honoured.
def test_custom_new_reservation_id(live_data):
    book_munich()
    set_evse_status(live_data, MUNICH_EVSE, "BLOCKED")
    result = orchestration_service.auto_reroute(make_request(new_id="NEW_7"))
    assert result.active_reservation.reservation_id == "NEW_7"
    assert reservation_service.get_reservation("NEW_7").status == "ACTIVE"


# The rebooked slot keeps the original window shifted by the reported delay.
def test_rebook_applies_delay_to_slot(live_data):
    original = book_munich()
    result = orchestration_service.auto_reroute(make_request(delay=90))
    shift = result.active_reservation.booking_slot.arrival_time - original.booking_slot.arrival_time
    assert shift.total_seconds() == 90 * 60
    window = result.active_reservation.booking_slot
    assert window.expiry_date - window.arrival_time == original.booking_slot.expiry_date - original.booking_slot.arrival_time


# A truck that can no longer reach the reserved station triggers an out of range rebook.
def test_out_of_range_conflict(live_data):
    book_munich()
    truck = TruckStatus(latitude=50.0, longitude=8.5, battery_percentage=60)
    request = AutoRerouteRequest(truck=truck, reservation_id="RES_1")
    result = orchestration_service.auto_reroute(request)
    assert ConflictReason.OUT_OF_RANGE in result.conflict_reasons
    assert result.action == AutoAction.REBOOKED


# The previous reservation is returned in its cancelled state.
def test_previous_reservation_is_cancelled_in_store(live_data):
    book_munich()
    set_evse_status(live_data, MUNICH_EVSE, "OUTOFORDER")
    orchestration_service.auto_reroute(make_request())
    assert reservation_service.get_reservation("RES_1").status == "CANCELLED"


# If no alternative exists the original reservation stays untouched.
def test_no_alternative_keeps_original(live_data):
    book_munich()
    set_evse_status(live_data, MUNICH_EVSE, "OUTOFORDER")
    with pytest.raises(NoChargerAvailableError):
        orchestration_service.auto_reroute(make_request(battery=1.0))
    assert reservation_service.get_reservation("RES_1").status == "ACTIVE"


# An id clash for the new reservation fails without cancelling the old one.
def test_new_id_clash_keeps_original(live_data):
    book_munich()
    book_munich("TAKEN")
    set_evse_status(live_data, MUNICH_EVSE, "OUTOFORDER")
    with pytest.raises(reservation_service.ReservationConflictError):
        orchestration_service.auto_reroute(make_request(new_id="TAKEN"))
    assert reservation_service.get_reservation("RES_1").status == "ACTIVE"


# Unknown and cancelled reservations are rejected.
def test_unknown_and_cancelled_reservations(live_data):
    with pytest.raises(reservation_service.ReservationNotFoundError):
        orchestration_service.auto_reroute(make_request(reservation_id="NOPE"))
    book_munich()
    set_evse_status(live_data, MUNICH_EVSE, "OUTOFORDER")
    orchestration_service.auto_reroute(make_request())
    with pytest.raises(reservation_service.ReservationConflictError):
        orchestration_service.auto_reroute(make_request())


# Request validation rejects bad delays and empty ids.
@pytest.mark.parametrize("payload", [{"delay_minutes": -1}, {"delay_minutes": 1441}, {"reservation_id": ""}, {"new_reservation_id": ""}])
def test_request_validation(payload):
    base = {"truck": {"latitude": 48.1, "longitude": 11.6, "battery_percentage": 80}, "reservation_id": "R"}
    base.update(payload)
    with pytest.raises(Exception):
        AutoRerouteRequest(**base)