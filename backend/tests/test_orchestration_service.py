from datetime import datetime, timezone

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
from services import orchestration_service, reservation_service, smart_charging_service
from services.cpo_repository import find_location
from services.routing_service import NoChargerAvailableError, distance_to_location

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

# Statuses of every EVSE that is AVAILABLE in the shipped data, used to remove all free chargers.
FREE_EVSES = ["EVSE_MUN_1", "EVSE_STR_1", "EVSE_NUR_1", "EVSE_ULM_1", "EVSE_ING_1", "EVSE_KAR_1"]
AS_OF = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)


# Blocks every free EVSE so only CHARGING ones remain as options.
def block_all_free(path, status="BLOCKED"):
    for uid in FREE_EVSES:
        set_evse_status(path, uid, status)


# Returns the speed (km/h) at which the truck needs exactly the given hours to reach Munich South.
def speed_for_hours(hours):
    location = find_location(MUNICH_LOC)
    distance = distance_to_location(48.1, 11.6, location)
    return distance / hours


# Builds an auto request with ETA inputs.
def make_eta_request(hours, delay=0, as_of=AS_OF):
    return AutoRerouteRequest(
        truck=TruckStatus(latitude=48.1, longitude=11.6, battery_percentage=80.0),
        reservation_id="RES_1",
        delay_minutes=delay,
        average_speed_kmh=speed_for_hours(hours),
        as_of=as_of,
    )


# An ETA later than the booked arrival is detected as a delay and the booking is shifted.
def test_eta_delay_triggers_shift(live_data):
    book_munich()
    result = orchestration_service.auto_reroute(make_eta_request(hours=1.5))
    assert result.action == AutoAction.SHIFTED
    assert result.eta_delay_minutes == 30
    assert result.applied_delay_minutes == 30
    shift = result.active_reservation.booking_slot.arrival_time - datetime.fromisoformat(ARRIVAL.replace("Z", "+00:00"))
    assert shift.total_seconds() == 30 * 60


# An ETA more than the shift limit late forces a rebook with the delay reason.
def test_eta_delay_over_limit_triggers_rebook(live_data):
    book_munich()
    result = orchestration_service.auto_reroute(make_eta_request(hours=3))
    assert result.action == AutoAction.REBOOKED
    assert ConflictReason.DELAY_EXCEEDS_SHIFT_LIMIT in result.conflict_reasons
    assert result.eta_delay_minutes == 120


# An ETA before the booked arrival means no delay and no action.
def test_eta_early_means_no_action(live_data):
    book_munich()
    result = orchestration_service.auto_reroute(make_eta_request(hours=0.25))
    assert result.action == AutoAction.NO_ACTION
    assert result.eta_delay_minutes == 0
    assert result.applied_delay_minutes == 0


# The applied delay is the larger of the reported delay and the ETA delay.
def test_applied_delay_is_maximum(live_data):
    book_munich()
    result = orchestration_service.auto_reroute(make_eta_request(hours=1.5, delay=45))
    assert result.eta_delay_minutes == 30
    assert result.applied_delay_minutes == 45


# Without an average speed no ETA is computed and the reported delay is used as is.
def test_no_speed_means_no_eta(live_data):
    book_munich()
    result = orchestration_service.auto_reroute(make_request(delay=20))
    assert result.eta_delay_minutes is None
    assert result.applied_delay_minutes == 20


# When no charger is free the nearest CHARGING one is throttled and booked.
def test_curtailment_fallback_when_no_free_charger(live_data):
    book_munich()
    block_all_free(live_data)
    result = orchestration_service.auto_reroute(make_request())
    assert result.action == AutoAction.REBOOKED_WITH_CURTAILMENT
    assert result.charging_station.station_id == "LOC_AUGSBURG_WEST"
    assert result.smart_charging.original_max_power_kw == 320.0
    assert result.smart_charging.curtailed_max_power_kw == 160.0
    assert result.smart_charging.freed_capacity_kw == 160.0
    assert result.active_reservation.curtailment_booked is True
    assert result.previous_reservation.status == "CANCELLED"


# A normal rebook never carries smart charging data.
def test_normal_rebook_has_no_smart_charging(live_data):
    book_munich()
    set_evse_status(live_data, MUNICH_EVSE, "OUTOFORDER")
    result = orchestration_service.auto_reroute(make_request())
    assert result.action == AutoAction.REBOOKED
    assert result.smart_charging is None
    assert result.active_reservation.curtailment_booked is False


# A reservation made through curtailment is not flagged because its EVSE is CHARGING.
def test_curtailed_booking_is_stable_on_next_call(live_data):
    book_munich()
    block_all_free(live_data)
    first = orchestration_service.auto_reroute(make_request())
    again = orchestration_service.auto_reroute(
        AutoRerouteRequest(
            truck=TruckStatus(latitude=48.1, longitude=11.6, battery_percentage=80.0),
            reservation_id=first.active_reservation.reservation_id,
        )
    )
    assert again.action == AutoAction.NO_ACTION
    assert again.conflict_detected is False


# A curtailed booking is still flagged when its EVSE later becomes unusable.
def test_curtailed_booking_conflicts_when_station_fails(live_data):
    book_munich()
    block_all_free(live_data)
    first = orchestration_service.auto_reroute(make_request())
    set_evse_status(live_data, "EVSE_AUG_1", "OUTOFORDER")
    set_evse_status(live_data, "EVSE_MUN_1", "AVAILABLE")
    again = orchestration_service.auto_reroute(
        AutoRerouteRequest(
            truck=TruckStatus(latitude=48.1, longitude=11.6, battery_percentage=80.0),
            reservation_id=first.active_reservation.reservation_id,
        )
    )
    assert ConflictReason.STATION_UNAVAILABLE in again.conflict_reasons
    assert again.action == AutoAction.REBOOKED


# If the throttle-down fails the new booking is rolled back and the original stays active.
def test_curtailment_failure_rolls_back(live_data, monkeypatch):
    book_munich()
    block_all_free(live_data)

    def fail(*args, **kwargs):
        raise smart_charging_service.ChargingProfileTargetError("boom")

    monkeypatch.setattr(orchestration_service, "_request_curtailment", fail)
    with pytest.raises(smart_charging_service.ChargingProfileTargetError):
        orchestration_service.auto_reroute(make_request())
    assert reservation_service.get_reservation("RES_1").status == "ACTIVE"
    assert reservation_service.get_reservation("RES_1-REBOOK").status == "CANCELLED"


# With no free and no throttle-able charger in range the call fails and keeps the reservation.
def test_no_curtailment_candidate_keeps_original(live_data):
    book_munich()
    block_all_free(live_data)
    set_evse_status(live_data, "EVSE_AUG_1", "BLOCKED")
    with pytest.raises(NoChargerAvailableError):
        orchestration_service.auto_reroute(make_request())
    assert reservation_service.get_reservation("RES_1").status == "ACTIVE"


# A rebook carries the truck telemetry and target SoC into the new reservation.
def test_rebook_inherits_telemetry(live_data):
    reservation_service.create_reservation(
        "RES_1",
        ReservationCreate(
            location_id=MUNICH_LOC,
            evse_uid=MUNICH_EVSE,
            truck_id="TRUCK_1",
            booking_slot=BookingSlot(arrival_time=ARRIVAL, expiry_date=EXPIRY),
            target_soc_percent=90.0,
        ),
    )
    set_evse_status(live_data, MUNICH_EVSE, "OUTOFORDER")
    result = orchestration_service.auto_reroute(make_request())
    active = result.active_reservation
    assert active.target_soc_percent == 90.0
    assert active.battery_capacity_kwh == 300.0
    assert active.battery_percentage == result.truck_summary.estimated_battery_at_arrival
