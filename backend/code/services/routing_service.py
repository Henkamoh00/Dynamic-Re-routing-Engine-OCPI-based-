from typing import List, Optional, Sequence, Tuple

from schemas import (
    ConnectorType,
    EVSEStatus,
    LocationSummary,
    OCPIConnector,
    OCPIEVSE,
    OCPILocation,
    RecommendedChargingStation,
    RerouteResponse,
    TruckStatus,
    TruckSummary,
)
from services.cpo_repository import get_locations
from geo import haversine_distance

TRUCK_CONNECTOR_STANDARD = ConnectorType.IEC_62196_T2_COMBO
WATTS_PER_KILOWATT = 1000.0


class NoChargerAvailableError(Exception):
    pass


def watts_to_kw(power_w: int) -> float:
    return power_w / WATTS_PER_KILOWATT


def calculate_max_range_km(truck: TruckStatus) -> float:
    current_energy_kwh = (truck.battery_percentage / 100.0) * truck.full_capacity_kwh
    return current_energy_kwh / truck.consumption_rate


def calculate_arrival_soc(truck: TruckStatus, distance_km: float) -> float:
    energy_consumed_kwh = distance_km * truck.consumption_rate
    soc_drop_pct = (energy_consumed_kwh / truck.full_capacity_kwh) * 100.0
    return truck.battery_percentage - soc_drop_pct


def best_connector(
    evse: OCPIEVSE, connector_standard: ConnectorType
) -> Optional[OCPIConnector]:
    matching = [c for c in evse.connectors if c.standard == connector_standard]
    if not matching:
        return None
    return max(matching, key=lambda c: c.power_w)


def distance_to_location(latitude: float, longitude: float, location: OCPILocation) -> float:
    return haversine_distance(
        latitude,
        longitude,
        float(location.coordinates.latitude),
        float(location.coordinates.longitude),
    )


def build_station_view(
    truck: TruckStatus,
    location: OCPILocation,
    evse: OCPIEVSE,
    connector: OCPIConnector,
    distance_km: float,
) -> Tuple[RecommendedChargingStation, TruckSummary]:
    max_range_km = calculate_max_range_km(truck)
    arrival_soc = calculate_arrival_soc(truck, distance_km)
    station = RecommendedChargingStation(
        station_id=location.id,
        station_name=location.name or location.id,
        evse_uid=evse.uid,
        max_power_kw=watts_to_kw(connector.power_w),
        distance_km=round(distance_km, 2),
    )
    summary = TruckSummary(
        current_range_km=round(max_range_km, 2),
        estimated_battery_at_arrival=round(arrival_soc, 1),
    )
    return station, summary


def find_best_charger(
    truck: TruckStatus,
    locations: Optional[Sequence[OCPILocation]] = None,
    connector_standard: ConnectorType = TRUCK_CONNECTOR_STANDARD,
) -> RerouteResponse:
    source = locations if locations is not None else get_locations()
    max_range_km = calculate_max_range_km(truck)

    best: Optional[Tuple[float, int, OCPILocation, OCPIEVSE, OCPIConnector]] = None

    for location in source:
        distance_km = distance_to_location(truck.latitude, truck.longitude, location)
        if distance_km >= max_range_km:
            continue

        for evse in location.evses:
            if evse.status != EVSEStatus.AVAILABLE:
                continue
            connector = best_connector(evse, connector_standard)
            if connector is None:
                continue

            candidate = (distance_km, -connector.power_w, location, evse, connector)
            if best is None or candidate[:2] < best[:2]:
                best = candidate

    if best is None:
        raise NoChargerAvailableError(
            "🚨 Critical: No available chargers found within the truck's remaining battery range!"
        )

    distance_km, _, location, evse, connector = best
    station, summary = build_station_view(truck, location, evse, connector, distance_km)

    return RerouteResponse(
        status="success",
        message="Dynamic route calculated successfully.",
        truck_summary=summary,
        recommended_charging_station=station,
    )


def search_locations(
    latitude: float,
    longitude: float,
    radius_km: float,
    connector_standard: ConnectorType = TRUCK_CONNECTOR_STANDARD,
) -> List[LocationSummary]:
    results: List[LocationSummary] = []

    for location in get_locations():
        if not location.publish:
            continue
        distance_km = distance_to_location(latitude, longitude, location)
        if distance_km > radius_km:
            continue

        connectors = [
            c
            for evse in location.evses
            for c in evse.connectors
            if c.standard == connector_standard
        ]
        if not connectors:
            continue

        available = sum(
            1
            for evse in location.evses
            if evse.status == EVSEStatus.AVAILABLE
            and any(c.standard == connector_standard for c in evse.connectors)
        )
        results.append(
            LocationSummary(
                location_id=location.id,
                name=location.name,
                city=location.city,
                coordinates=location.coordinates,
                distance_km=round(distance_km, 2),
                available_evses=available,
                max_power_kw=watts_to_kw(max(c.power_w for c in connectors)),
            )
        )

    results.sort(key=lambda item: item.distance_km)
    return results