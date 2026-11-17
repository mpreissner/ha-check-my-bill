# Services, WebSocket API and repairs (phase 4)

Implements `bill-validation.md` §3.6 (services, WebSocket API, repairs) and the export
signal from §3.3. This file settles the details those sections leave open. Where the two
disagree, this one wins, and the deviation is listed in §7.

## 1. Modules

- `services.py`: registers `set_cycle_dates` and `recompute` in `async_setup` (once per
  domain, not per entry, so the actions exist even while an entry is not loaded).
- `websocket.py`: registers `check_my_bill/cycles` and `check_my_bill/range` in
  `async_setup`.
- `issues.py` (not `repairs.py`, which HA would load as a repairs platform): creates and deletes the two issues. It is called from the coordinator after
  each refresh. There are no fix flows; both issues point the user at an existing flow.
- `core/record.py` (pure) gains:
  - `CycleRecord.totals`, which rebuilds the `CycleTotals` a record was computed from;
  - `reprice(record, *, tariff, export_mode, tolerance)`, which re-runs `build_record` on
    those totals;
  - `Source.USER` for boundaries the user entered;
  - a public `cost_to_dict`, used by the WebSocket API for the projected cost.
- `services.yaml`, `strings.json` / `translations/en.json`: actions, exceptions, issues.
- `manifest.json`: adds `websocket_api` to `dependencies`.

## 2. Stored records follow the settings

Phase 3 kept final records at the cost they were computed with. That leaves history wrong
after a typo fix in a rate version, and leaves old statuses on the old tolerance.

- When the coordinator loads the store, every stored record is **repriced** with the
  current tariff, export mode and tolerance: `build_record` runs again on the record's own
  totals (`metered_kwh`, `export_kwh`, `netted_kwh`, `utility_kwh`, `utility_export_kwh`,
  `coverage`). No statistics are read, so this is cheap and runs on every load.
- Options changes already reload the entry, so the history follows the options from the
  next load on. A tariff version only applies to the days it covers, so adding a new
  version does not change past cycles; correcting an old version does.
- `source` and `final` are kept as they were.

## 3. Services

Both take an optional `config_entry_id` (config entry selector). Without it, the single
loaded entry is used (`service.async_get_config_entry`), which raises the standard
validation errors for a missing, foreign or unloaded entry.

### `check_my_bill.set_cycle_dates`

Fields: `config_entry_id?`, `cycle_start` (date, required), `start` (date, required),
`end` (date, required).

- `cycle_start` picks the closed record to edit by its current start date.
- `start` and `end` are the bill's dates as printed: `end` is the **last day** of the
  cycle (inclusive). The record's exclusive end is `end + 1 day`.
- Validation, each a `ServiceValidationError` with its own translation key:
  - `cycle_not_found`: no closed record starts on `cycle_start`;
  - `cycle_not_editable`: the record's source is `utility` (only estimated boundaries are
    edited);
  - `invalid_dates`: `end` is before `start`;
  - `cycle_overlap`: the new range overlaps another closed record, or ends after the
    current cycle starts.
  Neighbours are never moved automatically: to widen a cycle into its neighbour, shrink the
  neighbour first.
- The record is computed again from statistics for the new range, with `source: user` and
  `final` as for any closed cycle (`is_final`). The history is re-sorted and saved, and the
  coordinator refreshes so sensors and the card see the change.
- `Source.USER` is not estimated (`estimated_dates: false`): the dates came from a bill.
  A `user` record can be edited again.

### `check_my_bill.recompute`

Fields: `config_entry_id?`, `cycle_start?` (date).

- Reads the statistics again for one closed record (`cycle_start`) or all of them, and
  rebuilds them with the current settings. This is for statistics that changed after a
  record became final, such as Opower importing history late.
- `cycle_not_found` if `cycle_start` matches no closed record.
- Ends with a coordinator refresh, which also recomputes the current cycle.

The coordinator serialises these with its refresh through an `asyncio.Lock`, so a service
call and the hourly refresh never edit the history at the same time.

## 4. WebSocket API

Both commands are read-only and do not require admin. An unknown or unloaded entry returns
the error code `not_found`.

### `check_my_bill/cycles` `{entry_id}`

```json
{
  "currency": "USD",
  "export_mode": "none",
  "tolerance_pct": 2.0,
  "has_tariff": true,
  "current": { "...record...": "", "projected_cost": { "...cost...": "" } },
  "cycles": [ { "...record...": "" } ]
}
```

- Records use `CycleRecord.to_dict()` (the Store format) plus `estimated` (bool) and
  `last_day` (the inclusive end, for display).
- `current` is the open cycle, costed to date; `projected_cost` is null without a tariff
  or before any data. `cycles` are the closed records, newest first.

### `check_my_bill/range` `{entry_id, start, end}`

- `start` and `end` are ISO dates; `end` is exclusive, matching the record's `end`. The
  card converts the Energy date picker's inclusive last day.
- `start < end`, at most 400 days (`invalid_range` otherwise).
- Returns one record-shaped object (without `source` and `final`) computed exactly like a
  cycle: closed if `end` is not after today, otherwise in progress and costed to date.
  Fixed monthly charges are prorated over the range's days.

## 5. Repairs

Issue IDs carry the entry ID, so each entry has its own. Both are `WARNING`, not fixable,
and deleted when the condition clears, on unload and on removal.

- **`export_detected_<entry_id>`**: `export_mode` is `none`, a utility export statistic
  is configured, and the utility recorded more than 0.1 kWh of export in the current cycle
  or the most recent closed one. The text says the utility is now recording export, asks
  whether net metering started, and points to the options (**Settings → Export mode**).
  Users with uncompensated spill that their utility does record can ignore the issue; HA
  keeps an ignored issue ignored.
- **`missing_statistic_<entry_id>`**: one or more configured statistic IDs are not in
  the recorder's metadata (`async_list_statistic_ids` with the configured IDs). The text
  lists the missing IDs and points to **Reconfigure**. Checked on every refresh.

## 6. Tests

`tests/test_record.py`:
- `reprice` with a changed tariff changes the cost and keeps source and final;
  a changed tolerance changes the status; `totals` round-trips.

`tests_ha/test_services.py`:
- `set_cycle_dates` moves a backfilled record, recomputes its kWh, marks it `user`, and the
  sensors' last-cycle attributes follow.
- Each validation error: not found, utility record, end before start, overlap.
- `recompute` picks up statistics added after a record was built (one cycle and all).
- Options change reprices stored history (tariff and tolerance).

`tests_ha/test_websocket.py`:
- `cycles` returns current plus history in the documented shape; unknown entry →
  `not_found`.
- `range` over a past range matches the stored record for the same dates; invalid ranges.

`tests_ha/test_repairs.py`:
- Export issue raised when the utility return statistic is nonzero with `export_mode`
  `none`, not raised with `net_metering`, and cleared once the condition goes away.
- Missing-statistic issue raised for an unknown statistic ID and cleared after reconfigure.

## 7. Deviations from `bill-validation.md`

- **Service field `config_entry_id`, not `entry_id`, and optional.** That is HA's
  convention for actions that target an entry, and it lets the single-entry case omit it.
- **`set_cycle_dates` takes the bill's inclusive last day as `end`.** Users copy dates from
  a bill; the exclusive end stays internal.
- **History is repriced on settings changes** (§2), where `config-flow.md` §4 kept final
  costs.
