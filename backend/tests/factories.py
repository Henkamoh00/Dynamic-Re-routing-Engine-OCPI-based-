
import json
import os
from pathlib import Path

from schemas import OCPILocation

TIMESTAMP = "2026-10-06T12:00:00Z"
TRUCK_STANDARD = "IEC_62196_T2_COMBO"


# Builds a raw OCPI connector dictionary with sensible defaults for a DC truck charger.
def make_connector(
    power_w: int | None = 150000,
    standard: str = TRUCK_STANDARD,
    power_type: str = "DC",
    max_voltage: int = 800,
    max_amperage: int = 200,
    connector_id: str = "1",
) -> dict[str, any]:
    connector: dict[str, any] = {
        "id": connector_id,
        "standard": standard,
        "format": "CABLE",
        "power_type": power_type,
        "max_voltage": max_voltage,
        "max_amperage": max_amperage,
        "last_updated": TIMESTAMP,
    }
    if power_w is not None:
        connector["max_electric_power"] = power_w
    return connector


# Builds a raw OCPI EVSE dictionary with the given status and connectors.
def make_evse(
    uid: str = "EVSE_1",
    status: str = "AVAILABLE",
    connectors: list[dict[str, any]] | None = None,
) -> dict[str, any]:
    return {
        "uid": uid,
        "evse_id": f"DE*FRY*{uid}",
        "status": status,
        "capabilities": [],
        "connectors": connectors if connectors is not None else [make_connector()],
        "last_updated": TIMESTAMP,
    }


# Builds a validated OCPI location object at the given coordinates.
def make_location(
    loc_id: str = "LOC_1",
    latitude: str = "48.100000",
    longitude: str = "11.000000",
    evses: list[dict[str, any]] | None = None,
    publish: bool = True,
    name: str | None = "Test Location",
) -> OCPILocation:
    return OCPILocation.model_validate(
        {
            "country_code": "DE",
            "party_id": "FRY",
            "id": loc_id,
            "publish": publish,
            "name": name,
            "address": "Test Street 1",
            "city": "Munich",
            "postal_code": "80331",
            "country": "DEU",
            "coordinates": {"latitude": latitude, "longitude": longitude},
            "evses": evses if evses is not None else [make_evse()],
            "time_zone": "Europe/Berlin",
            "last_updated": TIMESTAMP,
        }
    )


# Changes the status of one EVSE inside a raw data file and bumps its mtime so caches notice.
def set_evse_status(path: Path, evse_uid: str, status: str) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    for location in data:
        for evse in location["evses"]:
            if evse["uid"] == evse_uid:
                evse["status"] = status
    path.write_text(json.dumps(data), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))