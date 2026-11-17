"""Seed a local dev Home Assistant with a live instance's energy data (spec §6.4).

Reads ``HA_URL`` and ``HA_TOKEN`` from ``.env`` (the source, read-only) and copies into the
dev instance at ``--dev-url``:

- the core settings that matter for billing (time zone, currency, country, units);
- hourly statistics for every statistic the Energy dashboard uses (energy sums and the
  power means behind ``stat_rate``), plus the Opower electric statistics, over ``--days``
  days;
- the Energy dashboard preferences;
- the Opower sensors (bill dates and so on), with their unique IDs, through a stub
  ``opower`` integration (``tools/dev_opower``) that it installs into ``--config``. The
  sensors are owned by the ``opower`` platform in the entity registry, as on the source,
  so Check My Bill's Opower discovery finds them. Restart the dev instance once after the
  first run to load the stub.

On a fresh dev instance it also completes onboarding with ``--username``/``--password``.
Nothing is written to the repo: the data lands in the dev instance's own database, which
lives in the gitignored ``scratch_config/``.

    .venv/bin/hass -c scratch_config          # in another terminal; port 8124
    .venv/bin/python tools/seed_dev.py --days 400
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
from snapshot import Client, read_env

ROOT = Path(__file__).resolve().parent.parent
CHUNK = 2000
STUB = ROOT / "tools" / "dev_opower"
STUB_YAML = "\nsensor:\n  - platform: opower\n"
SOURCE_TYPES = {"grid", "solar", "battery", "gas", "water"}


def stat_ids(prefs: dict[str, Any]) -> set[str]:
    """Every ``stat_*`` statistic ID referenced anywhere in the Energy preferences."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.startswith("stat_") and isinstance(value, str):
                    found.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(prefs)
    return found


async def connect(session: aiohttp.ClientSession, url: str, token: str) -> Client:
    ws = await session.ws_connect(
        url.rstrip("/").replace("http", "ws", 1) + "/api/websocket", max_msg_size=0
    )
    await ws.receive_json()
    await ws.send_json({"type": "auth", "access_token": token})
    if (await ws.receive_json()).get("type") != "auth_ok":
        raise RuntimeError(f"authentication failed at {url}")
    return Client(ws)


async def pull(session: aiohttp.ClientSession, url: str, token: str, days: int) -> dict[str, Any]:
    client = await connect(session, url, token)
    prefs = await client.call("energy/get_prefs")
    config = await client.call("get_config")
    listed = await client.call("recorder/list_statistic_ids")
    wanted = stat_ids(prefs) | {
        s["statistic_id"]
        for s in listed
        if s["statistic_id"].startswith("opower:") and "_elec_" in s["statistic_id"]
    }
    meta = {s["statistic_id"]: s for s in listed if s["statistic_id"] in wanted}

    registry = await client.call("config/entity_registry/list")
    opower = {
        r["entity_id"]: r["unique_id"]
        for r in registry
        if r.get("platform") == "opower" and r["entity_id"].startswith("sensor.")
    }
    states = [
        {**s, "unique_id": opower[s["entity_id"]]}
        for s in await client.call("get_states")
        if s["entity_id"] in opower
    ]

    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    rows: dict[str, list[dict[str, Any]]] = {}
    for statistic_id in sorted(meta):
        result = await client.call(
            "recorder/statistics_during_period",
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            statistic_ids=[statistic_id],
            period="hour",
            types=["sum", "state"] if meta[statistic_id].get("has_sum") else ["mean", "min", "max"],
        )
        rows[statistic_id] = result.get(statistic_id, [])
    return {"prefs": prefs, "config": config, "meta": meta, "rows": rows, "states": states}


async def dev_token(session: aiohttp.ClientSession, url: str, username: str, password: str) -> str:
    """Onboard a fresh instance, or log in to an onboarded one; returns an access token."""
    base = url.rstrip("/")
    client_id = f"{base}/"
    async with session.get(f"{base}/api/onboarding") as resp:
        # Once onboarding is finished, a restarted instance no longer serves this view.
        steps = {} if resp.status == 404 else {s["step"]: s["done"] for s in await resp.json()}

    if not steps.get("user", True):
        async with session.post(
            f"{base}/api/onboarding/users",
            json={
                "client_id": client_id,
                "name": username,
                "username": username,
                "password": password,
                "language": "en",
            },
        ) as resp:
            resp.raise_for_status()
            code = (await resp.json())["auth_code"]
    else:
        async with session.post(
            f"{base}/auth/login_flow",
            json={
                "client_id": client_id,
                "handler": ["homeassistant", None],
                "redirect_uri": client_id,
            },
        ) as resp:
            flow = await resp.json()
        async with session.post(
            f"{base}/auth/login_flow/{flow['flow_id']}",
            json={"client_id": client_id, "username": username, "password": password},
        ) as resp:
            result = await resp.json()
        if result.get("type") != "create_entry":
            raise RuntimeError(f"login failed: {result.get('errors')}")
        code = result["result"]

    async with session.post(
        f"{base}/auth/token",
        data={"grant_type": "authorization_code", "code": code, "client_id": client_id},
    ) as resp:
        resp.raise_for_status()
        token = (await resp.json())["access_token"]

    headers = {"Authorization": f"Bearer {token}"}
    for step, body in (
        ("core_config", {}),
        ("analytics", {}),
        ("integration", {"client_id": client_id, "redirect_uri": client_id}),
    ):
        if not steps.get(step, True):
            async with session.post(
                f"{base}/api/onboarding/{step}", json=body, headers=headers
            ) as resp:
                resp.raise_for_status()
    return token


def source_of(statistic_id: str) -> str:
    return statistic_id.split(":", 1)[0] if ":" in statistic_id else "recorder"


async def push(session: aiohttp.ClientSession, url: str, token: str, data: dict[str, Any]) -> None:
    client = await connect(session, url, token)
    config = data["config"]
    core = {k: config[k] for k in ("time_zone", "currency", "country", "language") if config.get(k)}
    # get_config reports the unit system as its units; config/core/update takes its name.
    miles = (config.get("unit_system") or {}).get("length") == "mi"
    core["unit_system"] = "us_customary" if miles else "metric"
    await client.call("config/core/update", **core)

    for statistic_id, rows in data["rows"].items():
        meta = data["meta"][statistic_id]
        stats = [
            {
                "start": datetime.fromtimestamp(r["start"] / 1000, UTC).isoformat(),
                **{k: r[k] for k in ("sum", "state", "mean", "min", "max") if r.get(k) is not None},
            }
            for r in rows
        ]
        metadata = {
            "statistic_id": statistic_id,
            "source": source_of(statistic_id),
            "name": meta.get("name"),
            "has_sum": bool(meta.get("has_sum")),
            # 1 is arithmetic; the circular mean (2) is for angles, never power.
            "mean_type": meta.get("mean_type", 1 if meta.get("has_mean") else 0),
            "unit_class": meta.get("unit_class"),
            "unit_of_measurement": meta.get("statistics_unit_of_measurement"),
        }
        for i in range(0, len(stats), CHUNK):
            await client.call(
                "recorder/import_statistics", metadata=metadata, stats=stats[i : i + CHUNK]
            )
        print(f"  {len(stats):6d} hours  {statistic_id}")

    prefs = {
        k: data["prefs"][k]
        for k in ("energy_sources", "device_consumption", "device_consumption_water")
        if k in data["prefs"]
    }
    # A newer source can have Energy source types the dev instance does not know yet.
    sources = prefs.get("energy_sources", [])
    prefs["energy_sources"] = [s for s in sources if s.get("type") in SOURCE_TYPES]
    dropped = sorted({s.get("type") for s in sources} - SOURCE_TYPES)
    if dropped:
        print(f"  skipped Energy source types the dev instance lacks: {', '.join(dropped)}")
    await client.call("energy/save_prefs", **prefs)


def install_opower_stub(config_dir: Path, states: list[dict[str, Any]]) -> bool:
    """Write the seed file and the stub integration; True if a restart is needed."""
    seeds = [{k: s[k] for k in ("entity_id", "unique_id", "state", "attributes")} for s in states]
    seed_file = config_dir / "opower_seed.json"
    changed = not seed_file.exists() or json.loads(seed_file.read_text()) != seeds
    seed_file.write_text(json.dumps(seeds, indent=2))

    target = config_dir / "custom_components" / "opower"
    for src in STUB.glob("*.*"):
        dst = target / src.name
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            changed = True

    yaml = config_dir / "configuration.yaml"
    text = yaml.read_text() if yaml.exists() else "default_config:\n"
    if "platform: opower" not in text:
        yaml.write_text(text.rstrip("\n") + "\n" + STUB_YAML)
        changed = True
    print(f"  {len(seeds)} Opower sensors in {seed_file.name}")
    return changed


async def run(args: argparse.Namespace) -> None:
    env = read_env(args.env)
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    async with aiohttp.ClientSession(connector=connector) as session:
        print(f"reading {args.days} days from the source instance")
        data = await pull(session, env["HA_URL"], env["HA_TOKEN"], args.days)
        token = await dev_token(session, args.dev_url, args.username, args.password)
        print(f"writing to {args.dev_url}")
        await push(session, args.dev_url, token, data)
    if install_opower_stub(args.config, data["states"]):
        print("Opower stub or its sensors changed: restart the dev instance to load them.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    parser.add_argument("--dev-url", default="http://localhost:8124")
    parser.add_argument("--config", type=Path, default=ROOT / "scratch_config")
    parser.add_argument("--username", default="dev")
    parser.add_argument("--password", default="dev")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
