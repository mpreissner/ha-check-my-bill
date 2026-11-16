"""Pull a read-only snapshot of a live Home Assistant's energy data (spec §6.2).

Reads ``HA_URL`` and ``HA_TOKEN`` from ``.env`` and writes ``snapshots/snapshot-<date>.json``:

- the Energy dashboard grid statistic IDs (from ``energy/get_prefs``),
- the Opower entity-registry rows and states,
- hourly statistics for the grid and Opower electric statistics over ``--days`` days.

The output holds real account and meter data; ``snapshots/`` is gitignored. Run
``tools/sanitize.py`` on it before anything goes near a commit.

    .venv/bin/python tools/snapshot.py --days 90
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import aiohttp

ROOT = Path(__file__).resolve().parent.parent


def read_env(path: Path) -> dict[str, str]:
    """``KEY=value`` lines; quotes and ``export`` are tolerated."""
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip().removeprefix("export ")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("'\"")
    return env


def grid_stats(prefs: dict[str, Any]) -> dict[str, list[str]]:
    """Grid import and export statistic IDs, in either the flow list or flat prefs layout."""
    out: dict[str, list[str]] = {"imports": [], "exports": []}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.startswith("stat_energy_from") and isinstance(value, str):
                    out["imports"].append(value)
                elif key.startswith("stat_energy_to") and isinstance(value, str):
                    out["exports"].append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk([s for s in prefs.get("energy_sources", []) if s.get("type") == "grid"])
    return out


class Client:
    """Minimal HA WebSocket client."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._ids = count(1)

    async def call(self, msg_type: str, **payload: Any) -> Any:
        msg_id = next(self._ids)
        await self._ws.send_json({"id": msg_id, "type": msg_type, **payload})
        while True:
            reply = await self._ws.receive_json()
            if reply.get("id") != msg_id:
                continue
            if not reply.get("success"):
                raise RuntimeError(f"{msg_type}: {reply.get('error')}")
            return reply["result"]


async def snapshot(url: str, token: str, days: int) -> dict[str, Any]:
    ws_url = url.rstrip("/").replace("http", "ws", 1) + "/api/websocket"
    # The system resolver handles mDNS names such as homeassistant.local; aiodns does not.
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    async with (
        aiohttp.ClientSession(connector=connector) as session,
        session.ws_connect(ws_url, max_msg_size=0) as ws,
    ):
        await ws.receive_json()  # auth_required
        await ws.send_json({"type": "auth", "access_token": token})
        if (await ws.receive_json()).get("type") != "auth_ok":
            raise RuntimeError("authentication failed")
        client = Client(ws)

        prefs = await client.call("energy/get_prefs")
        grid = grid_stats(prefs)

        registry = await client.call("config/entity_registry/list")
        opower_rows = [r for r in registry if r.get("platform") == "opower"]
        opower_ids = {r["entity_id"] for r in opower_rows}
        states = [s for s in await client.call("get_states") if s["entity_id"] in opower_ids]

        listed = await client.call("recorder/list_statistic_ids", statistic_type="sum")
        opower_stats = [
            s["statistic_id"]
            for s in listed
            if s["statistic_id"].startswith("opower:") and "_elec_" in s["statistic_id"]
        ]

        end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=days)
        ids = sorted({*grid["imports"], *grid["exports"], *opower_stats})
        stats = await client.call(
            "recorder/statistics_during_period",
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            statistic_ids=ids,
            period="hour",
            types=["change", "sum"],
        )
        config = await client.call("get_config")

    return {
        "taken_at": end.isoformat(),
        "time_zone": config.get("time_zone"),
        "grid": grid,
        "opower_stats": opower_stats,
        "registry": opower_rows,
        "states": states,
        "statistics": stats,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--out", type=Path, default=ROOT / "snapshots")
    args = parser.parse_args()

    env = read_env(args.env)
    data = asyncio.run(snapshot(env["HA_URL"], env["HA_TOKEN"], args.days))
    args.out.mkdir(exist_ok=True)
    path = args.out / f"snapshot-{datetime.now(UTC):%Y%m%dT%H%M}.json"
    path.write_text(json.dumps(data, indent=1))
    rows = sum(len(v) for v in data["statistics"].values())
    print(f"wrote {path.relative_to(ROOT)}: {len(data['statistics'])} statistics, {rows} rows")


if __name__ == "__main__":
    main()
