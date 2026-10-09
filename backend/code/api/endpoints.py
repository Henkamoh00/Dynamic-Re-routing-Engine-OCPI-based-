from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from schemas import (
    AutoRerouteRequest,
    AutoRerouteResponse,
    ConnectorType,
    DemandForecastResponse,
    EVSEStatusResponse,
    LocationSummary,
    RerouteResponse,
    Reservation,
    ReservationCreate,
    ReservationReplace,
    ReservationUpdate,
    SmartChargingProfileRequest,
    SmartChargingProfileResponse,
    TruckStatus,
)
from services import (
    demand_service,
    orchestration_service,
    reservation_service,
    smart_charging_service,
)
from services.cpo_repository import DataSourceError, find_evse_by_uid
from services.routing_service import (
    TRUCK_CONNECTOR_STANDARD,
    NoChargerAvailableError,
    find_best_charger,
    search_locations,
)

router = APIRouter(prefix="/api/v1")


@router.post("/reroute", response_model=RerouteResponse)
def dynamic_reroute(truck: TruckStatus) -> RerouteResponse:
    try:
        return find_best_charger(truck)
    except NoChargerAvailableError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except DataSourceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.post("/reroute/auto", response_model=AutoRerouteResponse)
def auto_reroute(payload: AutoRerouteRequest) -> AutoRerouteResponse:
    try:
        return orchestration_service.auto_reroute(payload)
    except reservation_service.ReservationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except reservation_service.ReservationConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except smart_charging_service.ChargingProfileTargetError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except NoChargerAvailableError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except DataSourceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.get("/locations", response_model=list[LocationSummary])
def list_locations(
    latitude: float = Query(..., ge=-90.0, le=90.0),
    longitude: float = Query(..., ge=-180.0, le=180.0),
    radius_km: float = Query(200.0, gt=0.0),
    connector_standard: ConnectorType = Query(TRUCK_CONNECTOR_STANDARD),
) -> list[LocationSummary]:
    try:
        return search_locations(latitude, longitude, radius_km, connector_standard)
    except DataSourceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.get("/status/evses/{evse_uid}", response_model=EVSEStatusResponse)
def get_evse_status(evse_uid: str) -> EVSEStatusResponse:
    try:
        found = find_evse_by_uid(evse_uid)
    except DataSourceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"EVSE {evse_uid} not found."
        )
    location, evse = found
    return EVSEStatusResponse(
        location_id=location.id,
        evse_uid=evse.uid,
        evse_id=evse.evse_id,
        status=evse.status,
        last_updated=evse.last_updated,
    )


@router.post(
    "/reservations/{reservation_id}",
    response_model=Reservation,
    status_code=status.HTTP_201_CREATED,
)
def create_reservation(reservation_id: str, payload: ReservationCreate) -> Reservation:
    try:
        return reservation_service.create_reservation(reservation_id, payload)
    except reservation_service.InvalidReservationTargetError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except reservation_service.ReservationConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except DataSourceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.patch("/reservations/{reservation_id}", response_model=Reservation)
def update_reservation(reservation_id: str, payload: ReservationUpdate) -> Reservation:
    try:
        return reservation_service.update_reservation(reservation_id, payload)
    except reservation_service.ReservationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except reservation_service.ReservationConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.put("/reservations/{reservation_id}", response_model=Reservation)
def replace_reservation(reservation_id: str, payload: ReservationReplace) -> Reservation:
    try:
        return reservation_service.replace_booking_slot(reservation_id, payload.booking_slot)
    except reservation_service.ReservationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except reservation_service.ReservationConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/reservations", response_model=list[Reservation])
def list_reservations(active_only: bool = Query(False)) -> list[Reservation]:
    return reservation_service.list_reservations(active_only=active_only)


@router.get("/cpo/demand-forecast", response_model=DemandForecastResponse)
def demand_forecast(
    horizon_hours: int = Query(12, ge=1, le=72),
    bucket_minutes: int = Query(15, ge=5, le=60),
    as_of: datetime | None = Query(None),
    grid_limit_kw: float | None = Query(None, gt=0.0),
) -> DemandForecastResponse:
    if 60 % bucket_minutes != 0:
        raise HTTPException(
            status_code=422, detail="bucket_minutes must divide 60 (5, 6, 10, 12, 15, 20, 30 or 60)."
        )
    try:
        return demand_service.compute_demand_forecast(
            horizon_hours, bucket_minutes, as_of, grid_limit_kw
        )
    except DataSourceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.post("/smart-charging/profile", response_model=SmartChargingProfileResponse)
def set_charging_profile(
    payload: SmartChargingProfileRequest,
) -> SmartChargingProfileResponse:
    try:
        return smart_charging_service.apply_charging_profile(payload)
    except smart_charging_service.ChargingProfileTargetError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except DataSourceError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
