"""Turn a ``tools/snapshot.py`` snapshot into a committable test fixture (spec §5).

- Statistic and entity IDs become synthetic ones (``sensor.test_grid_import``,
  ``opower:test_elec_0000_energy_consumption``, ...).
- Only hourly *changes* are kept, so no real meter or account totals survive; tests rebuild
  the cumulative sums from zero.
- Every value is multiplied by ``--scale`` and a per-hour factor within ``±--jitter``. HA and
  utility values for the same hour get the same factor, so their deviation is preserved.
- The window is the cycles the utility's date sensors imply: ``--cycles`` closed cycles
  before the current one, plus the current cycle up to the snapshot time.
- The result is refused if it contains any original ID or an account-like run of digits.

Review the diff of ``tests_ha/fixtures/`` before committing.

    .venv/bin/python tools/sanitize.py snapshots/snapshot-....json --name two_cycles
"""

from __future__ import annotations

import argparse
import calendar
import json
import random
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
HOUR = timedelta(hours=1)

GRID_IMPORT = "sensor.test_grid_import"
GRID_EXPORT = "sensor.test_grid_export"
UTILITY_PREFIX = "opower:test_elec_0000_"
START_ENTITY = "sensor.test_current_bill_start_date"
END_ENTITY = "sensor.test_current_bill_end_date"
UTILITY_KINDS = ("energy_consumption", "energy_return")
ACCOUNT_LIKE = re.compile(r"\d{7,}")


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def _cycle_dates(snapshot: dict[str, Any]) -> tuple[date, date]:
    """The utility's current start and inclusive end date."""
    found: dict[str, date] = {}
    for state in snapshot["states"]:
        entity_id = state["entity_id"]
        if "_elec" not in entity_id:
            continue
        for which in ("start", "end"):
            if entity_id.endswith(f"_{which}_date"):
                found[which] = date.fromisoformat(state["state"][:10])
    if set(found) != {"start", "end"}:
        raise SystemExit("snapshot has no electric bill start/end date states")
    return found["start"], found["end"]


def _id_map(snapshot: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for real in snapshot["grid"]["imports"][:1]:
        mapping[real] = GRID_IMPORT
    for real in snapshot["grid"]["exports"][:1]:
        mapping[real] = GRID_EXPORT
    for real in snapshot["opower_stats"]:
        for kind in UTILITY_KINDS:
            if real.endswith(f"_{kind}"):
                mapping[real] = UTILITY_PREFIX + kind
    return mapping


def _hourly(rows: list[dict[str, Any]], start: datetime, end: datetime) -> list[float | None]:
    """Hourly changes from ``start`` to ``end``; ``None`` where the recorder has no row."""
    by_hour = {datetime.fromtimestamp(row["start"] / 1000, UTC): row.get("change") for row in rows}
    out: list[float | None] = []
    hour = start
    while hour < end:
        out.append(by_hour.get(hour))
        hour += HOUR
    while out and out[-1] is None:
        out.pop()
    return out


def sanitize(
    snapshot: dict[str, Any], cycles: int, scale: float, jitter: float, seed: int
) -> dict[str, Any]:
    tz = ZoneInfo(snapshot["time_zone"])
    current_start, current_end = _cycle_dates(snapshot)
    first_day = _add_months(current_start, -cycles)
    start = datetime.combine(first_day, time.min, tzinfo=tz).astimezone(UTC)
    end = datetime.fromisoformat(snapshot["taken_at"])

    rng = random.Random(seed)  # noqa: S311  reproducible, not secret
    hours = int((end - start) / HOUR)
    factors = [scale * (1 + rng.uniform(-jitter, jitter)) for _ in range(hours)]

    statistics: dict[str, Any] = {}
    for real, synthetic in sorted(_id_map(snapshot).items(), key=lambda kv: kv[1]):
        changes = _hourly(snapshot["statistics"].get(real, []), start, end)
        statistics[synthetic] = {
            "source": "opower" if synthetic.startswith("opower:") else "recorder",
            "change": [
                None if value is None else round(max(value, 0.0) * factors[i], 3)
                for i, value in enumerate(changes)
            ],
        }

    return {
        "about": (
            "Sanitized by tools/sanitize.py: synthetic IDs, hourly changes only, values scaled "
            "and jittered identically for HA and utility statistics at each hour."
        ),
        "time_zone": snapshot["time_zone"],
        "start": start.isoformat(),
        "taken_at": end.isoformat(),
        "states": {
            START_ENTITY: current_start.isoformat(),
            END_ENTITY: current_end.isoformat(),
        },
        "statistics": statistics,
    }


def check_clean(snapshot: dict[str, Any], text: str) -> None:
    """Refuse output that still carries anything from the real instance."""
    real = [*_id_map(snapshot), *(s["entity_id"] for s in snapshot["states"])]
    leaked = [r for r in real if r in text]
    digits = ACCOUNT_LIKE.findall(text)
    if leaked or digits:
        raise SystemExit(f"refusing to write: leaked IDs {leaked}, digit runs {digits[:5]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--name", required=True, help="fixture name, without .json")
    parser.add_argument("--cycles", type=int, default=1, help="closed cycles before the current")
    parser.add_argument("--scale", type=float, default=0.9)
    parser.add_argument("--jitter", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, default=ROOT / "tests_ha" / "fixtures")
    args = parser.parse_args()

    snapshot = json.loads(args.snapshot.read_text())
    fixture = sanitize(snapshot, args.cycles, args.scale, args.jitter, args.seed)
    text = json.dumps(fixture, separators=(",", ":")) + "\n"
    check_clean(snapshot, text)

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{args.name}.json"
    path.write_text(text)
    print(f"wrote {path.relative_to(ROOT)} ({len(text) // 1024} KiB) from {fixture['start']}")
    for statistic_id, data in fixture["statistics"].items():
        values = [v for v in data["change"] if v is not None]
        print(f"  {statistic_id}: {len(values)} hours, {sum(values):.1f} total")


if __name__ == "__main__":
    main()
