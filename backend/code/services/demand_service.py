from datetime import datetime, timedelta, timezone

from schemas import (
    DemandBucket,
    DemandForecastResponse,
    DemandTotals,
    IncomingTruck,
    OCPILocation,
    StationDemand,
)
from services import reservation_service
from services.cpo_repository import find_evse
from services.routing_service import TRUCK_CONNECTOR_STANDARD, best_connector

DEFAULT_ARRIVAL_SOC = 20.0
DEFAULT_CAPACITY_KWH = 300.0
EPSILON = 1e-9


# Returns a timezone-aware UTC datetime (naive values are interpreted as UTC).
def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# Rounds a datetime down to the start of its bucket.
def _floor(value: datetime, bucket_minutes: int) -> datetime:
    value = value.replace(second=0, microsecond=0)
    return value - timedelta(minutes=value.minute % bucket_minutes)


# Maximum site capacity in kW: sum of the best truck connector of every compatible EVSE.
def _site_capacity_kw(location: OCPILocation) -> float:
    total_w = 0
    for evse in location.evses:
        connector = best_connector(evse, TRUCK_CONNECTOR_STANDARD)
        if connector is not None:
            total_w += connector.power_w
    return total_w / 1000.0


# Adds the average power (kW) a charging interval contributes to each bucket.
def _spread(
    power_kw: float,
    start: datetime,
    end: datetime,
    bucket_starts: list[datetime],
    bucket_minutes: int,
    target: list[float],
) -> None:
    bucket_len = timedelta(minutes=bucket_minutes)
    for index, bucket_start in enumerate(bucket_starts):
        overlap = min(end, bucket_start + bucket_len) - max(start, bucket_start)
        seconds = overlap.total_seconds()
        if seconds > 0:
            target[index] += power_kw * seconds / bucket_len.total_seconds()


# Forecasts the electrical demand of incoming reserved trucks per station and per time bucket.
def compute_demand_forecast(
    horizon_hours: int = 12,
    bucket_minutes: int = 15,
    as_of: datetime | None = None,
    grid_limit_kw: float | None = None,
) -> DemandForecastResponse:
    now = _aware(as_of) if as_of else datetime.now(timezone.utc)
    start = _floor(now, bucket_minutes)
    count = max(1, (horizon_hours * 60) // bucket_minutes)
    bucket_starts = [start + timedelta(minutes=bucket_minutes * i) for i in range(count)]
    horizon_end = bucket_starts[-1] + timedelta(minutes=bucket_minutes)

    per_station: dict[str, dict] = {}
    totals = [0.0] * count

    for reservation in reservation_service.list_reservations(active_only=True):
        target = find_evse(reservation.location_id, reservation.evse_uid)
        if target is None:
            continue
        location, evse = target
        connector = best_connector(evse, TRUCK_CONNECTOR_STANDARD) or max(
            evse.connectors, key=lambda c: c.power_w
        )
        power_kw = connector.power_w / 1000.0

        assumed = reservation.battery_percentage is None or reservation.battery_capacity_kwh is None
        soc = DEFAULT_ARRIVAL_SOC if reservation.battery_percentage is None else reservation.battery_percentage
        capacity = (
            DEFAULT_CAPACITY_KWH
            if reservation.battery_capacity_kwh is None
            else reservation.battery_capacity_kwh
        )
        target_soc = reservation.target_soc_percent

        energy_kwh = max(0.0, target_soc - soc) / 100.0 * capacity
        charge_hours = energy_kwh / power_kw if power_kw > 0 else 0.0
        arrival = _aware(reservation.booking_slot.arrival_time)
        charge_end = arrival + timedelta(hours=charge_hours)

        if charge_end <= start and arrival < start:
            continue
        if arrival >= horizon_end:
            continue

        entry = per_station.setdefault(
            location.id,
            {"location": location, "trucks": [], "buckets": [0.0] * count},
        )
        entry["trucks"].append(
            IncomingTruck(
                reservation_id=reservation.reservation_id,
                truck_id=reservation.truck_id,
                evse_uid=reservation.evse_uid,
                arrival_time=arrival,
                arrival_soc_percent=round(soc, 1),
                target_soc_percent=round(target_soc, 1),
                battery_capacity_kwh=round(capacity, 1),
                energy_kwh=round(energy_kwh, 1),
                power_kw=round(power_kw, 1),
                charge_minutes=round(charge_hours * 60.0, 1),
                telemetry_assumed=assumed,
            )
        )
        if energy_kwh > 0:
            _spread(power_kw, arrival, charge_end, bucket_starts, bucket_minutes, entry["buckets"])
            _spread(power_kw, arrival, charge_end, bucket_starts, bucket_minutes, totals)

    stations: list[StationDemand] = []
    for location_id, entry in per_station.items():
        location = entry["location"]
        capacity_kw = _site_capacity_kw(location)
        buckets = entry["buckets"]
        peak = max(buckets) if buckets else 0.0
        stations.append(
            StationDemand(
                location_id=location_id,
                station_name=location.name or location_id,
                capacity_kw=round(capacity_kw, 1),
                incoming_trucks=len(entry["trucks"]),
                energy_kwh=round(sum(t.energy_kwh for t in entry["trucks"]), 1),
                peak_demand_kw=round(peak, 1),
                peak_utilization_pct=round(peak / capacity_kw * 100.0, 1) if capacity_kw else 0.0,
                over_capacity=peak > capacity_kw + EPSILON,
                trucks=sorted(entry["trucks"], key=lambda t: t.arrival_time),
                buckets=[
                    DemandBucket(start=bucket_starts[i], demand_kw=round(value, 1))
                    for i, value in enumerate(buckets)
                ],
            )
        )
    stations.sort(key=lambda s: s.peak_demand_kw, reverse=True)

    total_buckets: list[DemandBucket] = []
    over_limit = 0
    max_shed = 0.0
    for i, value in enumerate(totals):
        shed = max(0.0, value - grid_limit_kw) if grid_limit_kw is not None else 0.0
        if shed > EPSILON:
            over_limit += 1
            max_shed = max(max_shed, shed)
        total_buckets.append(
            DemandBucket(start=bucket_starts[i], demand_kw=round(value, 1), shed_kw=round(shed, 1))
        )

    peak_total = max(totals) if totals else 0.0
    peak_time = bucket_starts[totals.index(peak_total)] if peak_total > 0 else None
    all_trucks = [truck for station in stations for truck in station.trucks]

    return DemandForecastResponse(
        generated_at=now,
        horizon_hours=horizon_hours,
        bucket_minutes=bucket_minutes,
        stations=stations,
        totals=DemandTotals(
            incoming_trucks=len(all_trucks),
            energy_kwh=round(sum(t.energy_kwh for t in all_trucks), 1),
            peak_demand_kw=round(peak_total, 1),
            peak_time=peak_time,
            grid_limit_kw=grid_limit_kw,
            over_limit_buckets=over_limit,
            max_shed_kw=round(max_shed, 1),
            buckets=total_buckets,
        ),
    )
