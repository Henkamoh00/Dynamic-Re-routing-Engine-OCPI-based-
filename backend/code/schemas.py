from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class EVSEStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    BLOCKED = "BLOCKED"
    CHARGING = "CHARGING"
    INOPERATIVE = "INOPERATIVE"
    OUTOFORDER = "OUTOFORDER"
    PLANNED = "PLANNED"
    REMOVED = "REMOVED"
    RESERVED = "RESERVED"
    UNKNOWN = "UNKNOWN"


class Capability(str, Enum):
    CHARGING_PROFILE_CAPABLE = "CHARGING_PROFILE_CAPABLE"
    CHARGING_PREFERENCES_CAPABLE = "CHARGING_PREFERENCES_CAPABLE"
    CHIP_CARD_SUPPORT = "CHIP_CARD_SUPPORT"
    CONTACTLESS_CARD_SUPPORT = "CONTACTLESS_CARD_SUPPORT"
    CREDIT_CARD_PAYABLE = "CREDIT_CARD_PAYABLE"
    DEBIT_CARD_PAYABLE = "DEBIT_CARD_PAYABLE"
    PED_TERMINAL = "PED_TERMINAL"
    REMOTE_START_STOP_CAPABLE = "REMOTE_START_STOP_CAPABLE"
    RESERVABLE = "RESERVABLE"
    RFID_READER = "RFID_READER"
    START_SESSION_CONNECTOR_REQUIRED = "START_SESSION_CONNECTOR_REQUIRED"
    TOKEN_GROUP_CAPABLE = "TOKEN_GROUP_CAPABLE"
    UNLOCK_CAPABLE = "UNLOCK_CAPABLE"


class ConnectorType(str, Enum):
    CHADEMO = "CHADEMO"
    CHAOJI = "CHAOJI"
    DOMESTIC_A = "DOMESTIC_A"
    DOMESTIC_B = "DOMESTIC_B"
    DOMESTIC_C = "DOMESTIC_C"
    DOMESTIC_D = "DOMESTIC_D"
    DOMESTIC_E = "DOMESTIC_E"
    DOMESTIC_F = "DOMESTIC_F"
    DOMESTIC_G = "DOMESTIC_G"
    DOMESTIC_H = "DOMESTIC_H"
    DOMESTIC_I = "DOMESTIC_I"
    DOMESTIC_J = "DOMESTIC_J"
    DOMESTIC_K = "DOMESTIC_K"
    DOMESTIC_L = "DOMESTIC_L"
    DOMESTIC_M = "DOMESTIC_M"
    DOMESTIC_N = "DOMESTIC_N"
    DOMESTIC_O = "DOMESTIC_O"
    GBT_AC = "GBT_AC"
    GBT_DC = "GBT_DC"
    IEC_60309_2_single_16 = "IEC_60309_2_single_16"
    IEC_60309_2_three_16 = "IEC_60309_2_three_16"
    IEC_60309_2_three_32 = "IEC_60309_2_three_32"
    IEC_60309_2_three_64 = "IEC_60309_2_three_64"
    IEC_62196_T1 = "IEC_62196_T1"
    IEC_62196_T1_COMBO = "IEC_62196_T1_COMBO"
    IEC_62196_T2 = "IEC_62196_T2"
    IEC_62196_T2_COMBO = "IEC_62196_T2_COMBO"
    IEC_62196_T3A = "IEC_62196_T3A"
    IEC_62196_T3C = "IEC_62196_T3C"
    NEMA_5_20 = "NEMA_5_20"
    NEMA_6_30 = "NEMA_6_30"
    NEMA_6_50 = "NEMA_6_50"
    NEMA_10_30 = "NEMA_10_30"
    NEMA_10_50 = "NEMA_10_50"
    NEMA_14_30 = "NEMA_14_30"
    NEMA_14_50 = "NEMA_14_50"
    PANTOGRAPH_BOTTOM_UP = "PANTOGRAPH_BOTTOM_UP"
    PANTOGRAPH_TOP_DOWN = "PANTOGRAPH_TOP_DOWN"
    TESLA_R = "TESLA_R"
    TESLA_S = "TESLA_S"


class ConnectorFormat(str, Enum):
    SOCKET = "SOCKET"
    CABLE = "CABLE"


class PowerType(str, Enum):
    AC_1_PHASE = "AC_1_PHASE"
    AC_2_PHASE = "AC_2_PHASE"
    AC_2_PHASE_SPLIT = "AC_2_PHASE_SPLIT"
    AC_3_PHASE = "AC_3_PHASE"
    DC = "DC"


class TruckStatus(BaseModel):
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    battery_percentage: float = Field(..., ge=0.0, le=100.0)
    consumption_rate: float = Field(1.2, gt=0.0, description="Energy consumption in kWh per km")
    full_capacity_kwh: float = Field(300.0, gt=0.0, description="Total battery capacity in kWh")


class GeoLocation(BaseModel):
    latitude: str = Field(..., max_length=10)
    longitude: str = Field(..., max_length=11)

    @field_validator("latitude")
    @classmethod
    def validate_latitude(cls, value: str) -> str:
        if not -90.0 <= float(value) <= 90.0:
            raise ValueError("latitude must be between -90 and 90")
        return value

    @field_validator("longitude")
    @classmethod
    def validate_longitude(cls, value: str) -> str:
        if not -180.0 <= float(value) <= 180.0:
            raise ValueError("longitude must be between -180 and 180")
        return value


class OCPIConnector(BaseModel):
    id: str
    standard: ConnectorType
    format: ConnectorFormat
    power_type: PowerType
    max_voltage: int = Field(..., gt=0, description="Maximum voltage in Volts")
    max_amperage: int = Field(..., gt=0, description="Maximum amperage in Amperes")
    max_electric_power: int | None = Field(
        None, gt=0, description="Maximum electric power in Watts (OCPI 2.2.1)"
    )
    tariff_ids: list[str] = Field(default_factory=list)
    terms_and_conditions: str | None = None
    last_updated: datetime

    @property
    def power_w(self) -> int:
        if self.max_electric_power is not None:
            return self.max_electric_power
        base = self.max_voltage * self.max_amperage
        if self.power_type == PowerType.AC_3_PHASE:
            return int(base * 3 ** 0.5)
        if self.power_type in (PowerType.AC_2_PHASE, PowerType.AC_2_PHASE_SPLIT):
            return base * 2
        return base


class OCPIEVSE(BaseModel):
    uid: str
    evse_id: str | None = Field(None, max_length=48)
    status: EVSEStatus
    capabilities: list[Capability] = Field(default_factory=list)
    connectors: list[OCPIConnector] = Field(..., min_length=1)
    last_updated: datetime


class OCPILocation(BaseModel):
    country_code: str = Field(..., min_length=2, max_length=2)
    party_id: str = Field(..., min_length=3, max_length=3)
    id: str
    publish: bool
    name: str | None = None
    address: str
    city: str
    postal_code: str | None = Field(None, max_length=10)
    country: str = Field(..., min_length=3, max_length=3)
    coordinates: GeoLocation
    evses: list[OCPIEVSE] = Field(default_factory=list)
    time_zone: str
    last_updated: datetime


class TruckSummary(BaseModel):
    current_range_km: float
    estimated_battery_at_arrival: float


class RecommendedChargingStation(BaseModel):
    station_id: str
    station_name: str
    evse_uid: str
    max_power_kw: float
    distance_km: float


class RerouteResponse(BaseModel):
    status: str
    message: str
    truck_summary: TruckSummary
    recommended_charging_station: RecommendedChargingStation


class LocationSummary(BaseModel):
    location_id: str
    name: str | None = None
    city: str
    coordinates: GeoLocation
    distance_km: float
    available_evses: int
    max_power_kw: float


class EVSEStatusResponse(BaseModel):
    location_id: str
    evse_uid: str
    evse_id: str | None = None
    status: EVSEStatus
    last_updated: datetime


class BookingSlot(BaseModel):
    arrival_time: datetime
    expiry_date: datetime

    @model_validator(mode="after")
    def validate_window(self) -> "BookingSlot":
        if self.expiry_date <= self.arrival_time:
            raise ValueError("expiry_date must be later than arrival_time")
        return self


class ReservationCreate(BaseModel):
    location_id: str
    evse_uid: str
    truck_id: str | None = None
    booking_slot: BookingSlot
    battery_percentage: float | None = Field(
        None, ge=0.0, le=100.0, description="Expected state of charge on arrival (%)"
    )
    battery_capacity_kwh: float | None = Field(
        None, gt=0.0, description="Total battery capacity of the truck (kWh)"
    )
    target_soc_percent: float = Field(
        80.0, gt=0.0, le=100.0, description="State of charge the truck will charge up to (%)"
    )


class ReservationReplace(BaseModel):
    booking_slot: BookingSlot


class ReservationUpdate(BaseModel):
    delay_minutes: int | None = Field(None, gt=0, le=1440)
    booking_slot: BookingSlot | None = None
    status: Literal["CANCELLED"] | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> "ReservationUpdate":
        provided = [
            self.delay_minutes is not None,
            self.booking_slot is not None,
            self.status is not None,
        ]
        if sum(provided) != 1:
            raise ValueError(
                "Provide exactly one of delay_minutes, booking_slot or status"
            )
        return self


class Reservation(BaseModel):
    reservation_id: str
    location_id: str
    evse_uid: str
    truck_id: str | None = None
    status: Literal["ACTIVE", "CANCELLED"]
    booking_slot: BookingSlot
    battery_percentage: float | None = None
    battery_capacity_kwh: float | None = None
    target_soc_percent: float = 80.0
    curtailment_booked: bool = False
    last_updated: datetime


class ChargingProfilePeriod(BaseModel):
    start_period: int = Field(..., ge=0, description="Seconds from the profile start")
    limit: float = Field(..., ge=0.0)


class ChargingProfile(BaseModel):
    start_date_time: datetime | None = None
    duration: int | None = Field(None, gt=0, description="Profile duration in seconds")
    charging_rate_unit: Literal["W", "A"]
    charging_profile_period: list[ChargingProfilePeriod] = Field(..., min_length=1)


class SmartChargingProfileRequest(BaseModel):
    location_id: str
    evse_uid: str
    requesting_truck_id: str | None = None
    charging_profile: ChargingProfile


class SmartChargingProfileResponse(BaseModel):
    status: Literal["ACCEPTED"]
    location_id: str
    evse_uid: str
    original_max_power_kw: float
    curtailed_max_power_kw: float
    freed_capacity_kw: float
    message: str


class ConflictReason(str, Enum):
    STATION_UNAVAILABLE = "STATION_UNAVAILABLE"
    CONNECTOR_INCOMPATIBLE = "CONNECTOR_INCOMPATIBLE"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    DELAY_EXCEEDS_SHIFT_LIMIT = "DELAY_EXCEEDS_SHIFT_LIMIT"


class AutoAction(str, Enum):
    NO_ACTION = "NO_ACTION"
    SHIFTED = "SHIFTED"
    REBOOKED = "REBOOKED"
    REBOOKED_WITH_CURTAILMENT = "REBOOKED_WITH_CURTAILMENT"


class AutoRerouteRequest(BaseModel):
    truck: TruckStatus
    reservation_id: str = Field(..., min_length=1)
    delay_minutes: int = Field(0, ge=0, le=1440)
    new_reservation_id: str | None = Field(None, min_length=1)
    average_speed_kmh: float | None = Field(
        None, gt=0.0, le=200.0, description="Used to estimate the ETA delay from the distance"
    )
    as_of: datetime | None = Field(
        None, description="Reference time for the ETA estimate (defaults to now, UTC)"
    )


class AutoRerouteResponse(BaseModel):
    status: Literal["success"]
    action: AutoAction
    conflict_detected: bool
    conflict_reasons: list[ConflictReason]
    previous_reservation: Reservation
    active_reservation: Reservation
    truck_summary: TruckSummary
    charging_station: RecommendedChargingStation
    applied_delay_minutes: int = 0
    eta_delay_minutes: int | None = None
    smart_charging: SmartChargingProfileResponse | None = None
    message: str


class IncomingTruck(BaseModel):
    reservation_id: str
    truck_id: str | None = None
    evse_uid: str
    arrival_time: datetime
    arrival_soc_percent: float
    target_soc_percent: float
    battery_capacity_kwh: float
    energy_kwh: float
    power_kw: float
    charge_minutes: float
    telemetry_assumed: bool


class DemandBucket(BaseModel):
    start: datetime
    demand_kw: float
    shed_kw: float = 0.0


class StationDemand(BaseModel):
    location_id: str
    station_name: str
    capacity_kw: float
    incoming_trucks: int
    energy_kwh: float
    peak_demand_kw: float
    peak_utilization_pct: float
    over_capacity: bool
    trucks: list[IncomingTruck]
    buckets: list[DemandBucket]


class DemandTotals(BaseModel):
    incoming_trucks: int
    energy_kwh: float
    peak_demand_kw: float
    peak_time: datetime | None = None
    grid_limit_kw: float | None = None
    over_limit_buckets: int
    max_shed_kw: float
    buckets: list[DemandBucket]


class DemandForecastResponse(BaseModel):
    generated_at: datetime
    horizon_hours: int
    bucket_minutes: int
    stations: list[StationDemand]
    totals: DemandTotals
