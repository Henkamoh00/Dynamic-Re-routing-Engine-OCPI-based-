"""Simulates a CPO pushing live EVSE status changes (OCPI 2.2.1 Status updates).

The script never touches the pristine source file. It writes a separate live file
(default: data/OCPI_data.live.json) atomically, so a running API that points to it
via the OCPI_DATA_FILE environment variable picks up changes on the next request.

Examples:
    python scripts/simulate_cpo_updates.py --once
    python scripts/simulate_cpo_updates.py --ticks 20 --interval 2 --seed 7
    python scripts/simulate_cpo_updates.py --reset --once
"""

import argparse
import json
import os
import random
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
CODE_DIR = BACKEND_DIR / "code"
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from schemas import OCPILocation

DEFAULT_SOURCE = BACKEND_DIR / "data" / "OCPI_data.json"
DEFAULT_OUTPUT = BACKEND_DIR / "data" / "OCPI_data.live.json"

# Per-tick transition probabilities, keyed by current status. Only official OCPI 2.2.1 statuses.
TRANSITIONS: dict[str, list[tuple]] = {
    "AVAILABLE": [("CHARGING", 0.18), ("BLOCKED", 0.02), ("OUTOFORDER", 0.01)],
    "CHARGING": [("AVAILABLE", 0.30), ("OUTOFORDER", 0.01)],
    "BLOCKED": [("AVAILABLE", 0.30)],
    "OUTOFORDER": [("AVAILABLE", 0.10)],
}


class SimulatorError(Exception):
    pass


# Returns the current UTC time formatted as an OCPI timestamp.
def _timestamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Picks the next status for one EVSE, or None when it stays unchanged.
def _next_status(current: str, rng: random.Random) -> str | None:
    roll = rng.random()
    cumulative = 0.0
    for target, probability in TRANSITIONS.get(current, []):
        cumulative += probability
        if roll < cumulative:
            return target
    return None


# Applies a status change and refreshes the OCPI last_updated fields.
def _set_status(location: dict[str, any], evse: dict[str, any], status: str, stamp: str) -> None:
    evse["status"] = status
    evse["last_updated"] = stamp
    location["last_updated"] = stamp


# Advances the whole data set by one tick and returns the number of changed EVSEs.
def step(
    data: list[dict[str, any]],
    rng: random.Random,
    now: datetime | None = None,
    min_available: int = 3,
) -> int:
    stamp = _timestamp(now or datetime.now(timezone.utc))
    changed = 0

    for location in data:
        for evse in location.get("evses", []):
            target = _next_status(evse["status"], rng)
            if target is not None:
                _set_status(location, evse, target, stamp)
                changed += 1

    available = [
        (loc, evse)
        for loc in data
        for evse in loc.get("evses", [])
        if evse["status"] == "AVAILABLE"
    ]
    shortage = min_available - len(available)
    if shortage > 0:
        candidates = [
            (loc, evse)
            for loc in data
            for evse in loc.get("evses", [])
            if evse["status"] in ("CHARGING", "BLOCKED")
        ]
        rng.shuffle(candidates)
        for loc, evse in candidates[:shortage]:
            _set_status(loc, evse, "AVAILABLE", stamp)
            changed += 1

    return changed


# Validates every location against the OCPI models before it is written.
def validate(data: list[dict[str, any]]) -> None:
    for item in data:
        OCPILocation.model_validate(item)


# Reads a JSON file containing a list of OCPI locations.
def load_data(path: Path) -> list[dict[str, any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SimulatorError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(data, list):
        raise SimulatorError(f"{path} must contain a JSON list of locations.")
    return data


# Writes the data atomically so readers never observe a half-written file.
def write_atomic(path: Path, data: list[dict[str, any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp_path, path)


# Builds the starting state: the existing live file, or a fresh copy of the source.
def initial_state(source: Path, output: Path, reset: bool) -> list[dict[str, any]]:
    if output.exists() and not reset:
        return load_data(output)
    return load_data(source)


# Runs the simulation for the requested number of ticks and returns per-tick change counts.
def run(
    source: Path,
    output: Path,
    ticks: int,
    interval: float,
    seed: int | None,
    min_available: int,
    reset: bool,
    sleep=time.sleep,
) -> list[int]:
    data = initial_state(source, output, reset)
    validate(data)
    rng = random.Random(seed)
    counts: list[int] = []

    for index in range(ticks):
        counts.append(step(data, rng, min_available=min_available))
        validate(data)
        write_atomic(output, data)
        if index < ticks - 1 and interval > 0:
            sleep(interval)
    return counts


# Parses command line arguments.
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate live CPO status changes.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--once", action="store_true", help="Run a single tick and exit.")
    parser.add_argument("--ticks", type=int, default=10)
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between ticks.")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--min-available", type=int, default=3)
    parser.add_argument("--reset", action="store_true", help="Restart from the source file.")
    args = parser.parse_args(argv)
    if args.ticks < 1:
        parser.error("--ticks must be at least 1")
    if args.interval < 0:
        parser.error("--interval must not be negative")
    if args.min_available < 0:
        parser.error("--min-available must not be negative")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    ticks = 1 if args.once else args.ticks
    try:
        counts = run(
            args.source, args.output, ticks, args.interval,
            args.seed, args.min_available, args.reset,
        )
    except SimulatorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    print(f"Wrote {args.output} after {len(counts)} tick(s); changes per tick: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())