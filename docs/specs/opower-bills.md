# Past bill periods from Opower

The Opower integration's date sensors only describe the **current** cycle. Closed cycles
before Check My Bill was installed are backfilled by stepping back whole months
(`Source.BACKFILL`), but utility read dates drift by several days each month, so those
boundaries are estimates and the per-cycle comparison is off by the drift.

Opower knows every past bill period. This feature asks Opower for them and replaces the
estimated boundaries with the real ones.

## 1. Modules

- `core/bills.py` (pure):
  - `UtilityBill(start, end, kwh, cost)` is one bill period. `end` is exclusive, like a
    `Cycle`. `kwh` is the billed consumption; `cost` is Opower's `providedCost` for the
    period. Depending on the utility, that cost may leave out taxes and fees.
  - `bills_from_reads(reads)` turns `(start, end, kwh, cost)` tuples with aware datetimes
    into `UtilityBill`s, oldest first (§2).
  - `merge_bills(records, bills, current_start, max_count)` decides which stored records
    to keep and which bill periods still need computing (§3).
- `opower_bills.py` (HA layer): `async_fetch_bills(hass, utility_stat, since)` finds the
  Opower account behind the configured utility statistic and returns its bill periods, or
  `None` when that is not possible (§4).
- `core/record.py`: `CycleRecord` gains `bill: BilledAmounts | None` (`kwh`, `cost`).
  `build_record` and `reprice` carry it through. It is stored, and the WebSocket API
  returns it as `bill: {kwh, cost} | null`.
- `coordinator.py`: fetches bills at most once a day and merges them (§5).

## 2. Bill periods

Opower returns `CostRead`s for `AggregateType.BILL` with aware `start_time` and
`end_time` in the utility's time zone. The time zone conversion keeps their own tzinfo:
bill dates are the utility's local dates.

- `start` is the local date of `start_time`.
- `end` (exclusive): if `end_time` falls exactly on local midnight, `end` is its date;
  otherwise it is the next day. This makes both `…T00:00` of the next period and
  `…T23:59:59` of the last day give the same exclusive end.
- Periods with `end <= start` are dropped, and so are duplicates with the same start.
- Bills are contiguous in practice. If a bill ends exactly one day before the next one
  starts, its end moves to the next start. This covers utilities that report the last
  day at midnight.

## 3. Merging into the history

Inputs: the stored closed records, the bill periods, the current cycle's start, and
`BACKFILL_MAX_CYCLES`.

1. Only bills that have ended are used: `end <= current_start`. Of those, the newest
   `max_count` are kept.
2. A bill that overlaps a `Source.USER` record is skipped. The user typed those dates
   from a bill, and they win.
3. A stored record with the same dates as a bill is kept. It becomes `Source.UTILITY` if
   it was estimated, and gets the bill's amounts. Its kWh totals are not recomputed.
4. Any other non-`USER` record that overlaps a used bill is dropped, whatever its source.
   That includes `UTILITY` records closed by a rollover: the bill is the authoritative
   record of the period.
5. Each used bill without a matching record is computed from the statistics
   (`_closed_record`, `Source.UTILITY`, finality by `is_final`).
6. Records that overlap no bill are kept as they are. That covers estimates older than
   the bills Opower returned, and a cycle that closed before its bill was posted.

Bill periods that end on or before the first day with statistics are left out, the same
rule `backfill` uses.

## 4. Getting the bills

- The configured utility import statistic is `opower:<prefix>_energy_consumption`.
  `<prefix>` is built by the Opower integration from
  `utility.subdomain()`, `account.meter_type.name` and `account.utility_account_id`,
  lower-cased with `-` replaced by `_`.
- Each loaded `opower` config entry's `runtime_data` is its coordinator. Its `data` values
  have an `account`, and the coordinator has the `api` (`opower.Opower`). The account
  whose prefix matches is used.
- The call is `api.async_get_cost_reads(account, AggregateType.BILL, since, now)`, where
  `since` is local midnight `BACKFILL_MAX_CYCLES + 1` months before the current cycle's start.
- Returns `None`, and logs at debug level, when:
  - there is no utility statistic, or it is not an Opower one;
  - no Opower entry is loaded, or none has the account;
  - the `opower` package or the coordinator's attributes are not shaped as expected
    (these are internals of another integration).
- Raises `BillFetchError` when the account was found but the call failed (auth expiry,
  network, API errors). The coordinator logs one warning per entry and nothing more until
  a fetch succeeds again.
  Nothing here can make the refresh fail.

No credentials are read and no login is made: the Opower integration's own session is
reused.

## 5. When it runs

- On each refresh, after the current cycle is known and after backfill or rollover, if no
  fetch has succeeded in the last 24 hours. Restarts fetch again.
- A failed or `None` fetch is retried on the next hourly refresh.
- After a merge, non-final records are recomputed as before (`bill` is kept), the history
  is re-sorted newest first and saved.
- `recompute` keeps each record's `bill`. `set_cycle_dates` still rejects `utility`
  records. A `user` record has no `bill`.

## 6. Tests

`tests/test_bills.py` (pure):
- `bills_from_reads`: midnight end, 23:59:59 end, 1-day gap snapping, dropping empty or
  duplicate periods, ordering.
- `merge_bills`:
  - backfilled records replaced by bills;
  - a record with matching dates is kept and gets the amounts;
  - user records win;
  - bills that have not ended are ignored;
  - only `max_count` bills are used;
  - a cycle newer than the last bill is kept.

`tests/test_record.py`: `bill` round-trips through `to_dict`/`from_dict` and survives
`reprice`.

`tests_ha/test_opower_bills.py` (a fake loaded `opower` entry whose `runtime_data` has
`api` and `data`):
- Setup with a manual cycle day: backfilled cycles are replaced by `utility` records on
  the bill dates, with `bill` amounts and kWh from the statistics.
- No Opower entry, or an `api` that raises: the backfilled history stays, and setup
  succeeds.
- The fetch runs once a day, not every hour.
- A `user` record overlapping a bill is kept.

## 7. Deviations

- `bill-validation.md` treated past utility dates as unavailable, because the date sensors
  have no history. This uses an internal of the Opower integration (`runtime_data.api`).
  It is guarded, so a change there only falls back to the estimates.
