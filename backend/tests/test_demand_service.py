from datetime import datetime, timedelta, timezone

import pytest
from schemas import BookingSlot, ReservationCreate, ReservationUpdate
from services import demand_service, reservation_service

AS_OF = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc)
MUNICH = ("LOC_MUNICH_SOUTH", "EVSE_MUN_1")
STUTTGART = ("LOC_STUTTGART_EAST", "EVSE_STR_1")


# Creates an ACTIVE reservation arriving the given number of minutes after AS_OF.
def book(reservation_id, station, minutes=60, soc=20.0, capacity=300.0, target=80.0):
    arrival = AS_OF + timedelta(minutes=minutes)
    return reservation_service.create_reservation(
        reservation_id,
        ReservationCreate(
            location_id=station[0],
            evse_uid=station[1],
            truck_id=f"T_{reservation_id}",
            booking_slot=BookingSlot(arrival_time=arrival, expiry_date=arrival + timedelta(hours=2)),
            battery_percentage=soc,
            battery_capacity_kwh=capacity,
            target_soc_percent=target,
        ),
    )


# Without reservations the forecast is empty and has no peak.
def test_empty_forecast():
    result = demand_service.compute_demand_forecast(as_of=AS_OF)
    assert result.stations == []
    assert result.totals.incoming_trucks == 0
    assert result.totals.peak_demand_kw == 0.0
    assert result.totals.peak_time is None
    assert len(result.totals.buckets) == 48


# One truck needs (target - arrival) x capacity at the connector power starting at arrival.
def test_single_truck_energy_and_buckets():
    book("R1", MUNICH)
    result = demand_service.compute_demand_forecast(as_of=AS_OF)
    station = result.stations[0]
    truck = station.trucks[0]
    assert truck.energy_kwh == 180.0
    assert truck.power_kw == 320.0
    assert truck.charge_minutes == 33.8
    demands = {b.start.strftime("%H:%M"): b.demand_kw for b in station.buckets}
    assert demands["09:00"] == 320.0 and demands["09:15"] == 320.0
    assert demands["09:30"] == pytest.approx(80.0, abs=0.1)
    assert demands["09:45"] == 0.0 and demands["08:45"] == 0.0


# The bucket energy adds up to the energy of the truck.
def test_bucket_energy_matches_truck_energy():
    book("R1", MUNICH)
    result = demand_service.compute_demand_forecast(as_of=AS_OF)
    energy = sum(b.demand_kw for b in result.totals.buckets) * 15 / 60
    assert energy == pytest.approx(180.0, abs=0.5)


# Peak, utilization and peak time are reported for a station and for the total.
def test_peak_and_utilization():
    book("R1", MUNICH)
    result = demand_service.compute_demand_forecast(as_of=AS_OF)
    station = result.stations[0]
    assert station.capacity_kw == 320.0
    assert station.peak_demand_kw == 320.0
    assert station.peak_utilization_pct == 100.0
    assert station.over_capacity is False
    assert result.totals.peak_time == datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)


# Two overlapping trucks on a single-EVSE site exceed its capacity and are flagged.
def test_overlapping_trucks_flag_over_capacity():
    book("R1", MUNICH)
    book("R2", MUNICH)
    station = demand_service.compute_demand_forecast(as_of=AS_OF).stations[0]
    assert station.peak_demand_kw == 640.0
    assert station.over_capacity is True
    assert station.incoming_trucks == 2


# Trucks that do not overlap in time never exceed capacity.
def test_non_overlapping_trucks_not_flagged():
    book("R1", MUNICH, minutes=60)
    book("R2", MUNICH, minutes=240)
    station = demand_service.compute_demand_forecast(as_of=AS_OF).stations[0]
    assert station.peak_demand_kw == 320.0
    assert station.over_capacity is False


# Missing telemetry falls back to defaults and is flagged as assumed.
def test_missing_telemetry_uses_defaults():
    reservation_service.create_reservation(
        "R1",
        ReservationCreate(
            location_id=MUNICH[0],
            evse_uid=MUNICH[1],
            booking_slot=BookingSlot(
                arrival_time=AS_OF + timedelta(hours=1), expiry_date=AS_OF + timedelta(hours=3)
            ),
        ),
    )
    truck = demand_service.compute_demand_forecast(as_of=AS_OF).stations[0].trucks[0]
    assert truck.telemetry_assumed is True
    assert truck.arrival_soc_percent == 20.0
    assert truck.battery_capacity_kwh == 300.0


# Complete telemetry is not flagged as assumed.
def test_complete_telemetry_not_assumed():
    book("R1", MUNICH)
    assert demand_service.compute_demand_forecast(as_of=AS_OF).stations[0].trucks[0].telemetry_assumed is False


# Cancelled reservations do not contribute any demand.
def test_cancelled_reservations_excluded():
    book("R1", MUNICH)
    reservation_service.update_reservation("R1", ReservationUpdate(status="CANCELLED"))
    assert demand_service.compute_demand_forecast(as_of=AS_OF).stations == []


# A truck already at or above its target SoC is listed but adds no demand.
def test_truck_above_target_adds_no_demand():
    book("R1", MUNICH, soc=90.0, target=80.0)
    result = demand_service.compute_demand_forecast(as_of=AS_OF)
    assert result.stations[0].trucks[0].energy_kwh == 0.0
    assert result.totals.peak_demand_kw == 0.0
    assert result.totals.peak_time is None


# Reservations that arrive after the forecast horizon are ignored.
def test_arrival_after_horizon_excluded():
    book("R1", MUNICH, minutes=13 * 60)
    assert demand_service.compute_demand_forecast(horizon_hours=12, as_of=AS_OF).stations == []


# Reservations whose charging finished before the forecast start are ignored.
def test_finished_charging_excluded():
    book("R1", MUNICH, minutes=-180)
    assert demand_service.compute_demand_forecast(as_of=AS_OF).stations == []


# A charging session that started before the window still adds demand inside it.
def test_session_in_progress_counts():
    book("R1", MUNICH, minutes=-10)
    result = demand_service.compute_demand_forecast(as_of=AS_OF)
    assert result.totals.peak_demand_kw == 320.0


# Stations are sorted by peak demand, highest first.
def test_stations_sorted_by_peak():
    book("R1", MUNICH)
    book("R2", STUTTGART)
    stations = demand_service.compute_demand_forecast(as_of=AS_OF).stations
    assert [s.location_id for s in stations] == ["LOC_STUTTGART_EAST", "LOC_MUNICH_SOUTH"]


# Totals aggregate trucks and energy over all stations.
def test_totals_aggregate_stations():
    book("R1", MUNICH)
    book("R2", STUTTGART)
    totals = demand_service.compute_demand_forecast(as_of=AS_OF).totals
    assert totals.incoming_trucks == 2
    assert totals.energy_kwh == 360.0
    assert totals.peak_demand_kw == 720.0


# A grid limit produces shed kilowatts for the buckets that exceed it.
def test_grid_limit_sheds_excess():
    book("R1", MUNICH)
    book("R2", STUTTGART)
    totals = demand_service.compute_demand_forecast(as_of=AS_OF, grid_limit_kw=500.0).totals
    assert totals.over_limit_buckets == 2
    assert totals.max_shed_kw == 220.0
    assert totals.grid_limit_kw == 500.0
    assert max(b.shed_kw for b in totals.buckets) == 220.0


# Without a grid limit nothing is shed.
def test_no_grid_limit_no_shed():
    book("R1", MUNICH)
    totals = demand_service.compute_demand_forecast(as_of=AS_OF).totals
    assert totals.over_limit_buckets == 0 and totals.max_shed_kw == 0.0
    assert all(b.shed_kw == 0.0 for b in totals.buckets)


# The number of buckets follows horizon and bucket size and the start is floored.
def test_bucket_count_and_floor():
    odd_start = AS_OF + timedelta(minutes=7)
    result = demand_service.compute_demand_forecast(horizon_hours=2, bucket_minutes=30, as_of=odd_start)
    assert len(result.totals.buckets) == 4
    assert result.totals.buckets[0].start == AS_OF


# A naive as_of is interpreted as UTC.
def test_naive_as_of_is_utc():
    result = demand_service.compute_demand_forecast(as_of=AS_OF.replace(tzinfo=None))
    assert result.generated_at == AS_OF


# Reservations whose EVSE no longer exists are skipped.
def test_unknown_evse_skipped(monkeypatch):
    book("R1", MUNICH)
    monkeypatch.setattr(demand_service, "find_evse", lambda *args: None)
    assert demand_service.compute_demand_forecast(as_of=AS_OF).stations == []
