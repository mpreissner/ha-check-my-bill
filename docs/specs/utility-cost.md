# Utility cost and the possible billing error

A manual tariff is the only way Check My Bill can put a price on a cycle today, and most
users never enter one. Opower already publishes a cost statistic next to its consumption
statistic (`opower:<prefix>_energy_cost`). This feature reads that statistic, derives a
blended rate per kWh for each cycle, and uses that rate to price HA's own reading. The
card can then show what the utility charged next to the utility's kWh, an estimate next to
HA's kWh, and the difference: roughly how much the bill **may** be off by.

The figure is an indication, not a re-bill. It assumes the utility's average price per kWh
for the cycle also applies to the kWh HA saw and the utility did not (or the reverse).

## 1. Modules

- `core/utility_cost.py` (pure):
  - `CostEstimate(utility_cost, billed_kwh, rate, ha_kwh, ha_cost, difference, source)`.
    `source` is `"bill"` or `"statistic"` (§3.1).
  - `estimate(record, export_mode, *, min_coverage=1.0) -> CostEstimate | None` computes
    the figures for one `CycleRecord` (§3). No HA imports.
- `stats.py`:
  - `StatIds` gains `utility_cost: str | None`. It is part of `configured()`.
  - Costs are not energy, so `fetch_changes` cannot convert them to kWh. It gains a
    `units` argument; the cost statistic is fetched in a second call with `units=None`
    (values stay in the statistic's own currency, see §3.4).
  - `cycle_totals` returns the summed cost and its coverage along with the kWh totals,
    the same way the utility import statistic is summed (same slots, same lag handling).
- `core/compute.py`: `CycleTotals` gains `utility_cost: float | None`; `Coverage` gains
  `utility_cost: float | None`. `compute_totals` takes an optional cost series.
- `core/record.py`: `CycleRecord` gains `utility_cost: float | None = None`, stored next to
  `utility_kwh`, and `coverage.utility_cost`. The derived figures (rate, HA estimate,
  difference) are **not** stored: they are recomputed from the record by `estimate`
  whenever they are needed, so a change of export mode or of the coverage rule never
  leaves stale numbers behind.
- `config_flow.py`: a `utility_cost` options step (§2).
- `coordinator.py`, `websocket.py`, `sensor.py`, the card: §4–§7.

## 2. Configuration

- New option `CONF_UTILITY_COST_STAT` (optional, in `entry.options`).
- The options menu becomes `["rates", "rate_versions", "utility_cost", "settings"]`,
  labelled "Utility cost".
- The step has one field, a `StatisticSelector` (or an entity/statistic picker if that
  selector is unavailable), not required.
  - When unset and the utility import statistic is `opower:<prefix>_energy_consumption`,
    the suggested value is `opower:<prefix>_energy_cost`, if that statistic exists in the
    recorder's metadata.
  - Clearing the field removes the option.
- Validation:
  - the statistic must exist (`get_metadata`);
  - its `unit_of_measurement` must be a currency code; a kWh statistic is rejected
    with `not_a_cost_statistic`;
  - when it differs from `hass.config.currency`, the step accepts it but the card and
    sensors show the statistic's own unit (§3.4).
- The setup flow's utility step also suggests the sibling cost statistic when it fills in
  the Opower consumption statistic. Users with no Opower account can pick any
  cumulative cost statistic (a utility's own integration, a template sensor). Nothing
  here is Opower-specific except the suggestion.
- Saving the option reloads the entry. On the next refresh the coordinator fills
  `utility_cost` for stored records (§4).

## 3. Computation (`estimate`)

### 3.1 The utility's figures

For a closed cycle, in order of preference:

1. **Bill**: `record.bill.cost` and `record.bill.kwh`, when both are known and
   `bill.kwh > 0`. Opower's bill read is the period the utility actually billed, so it
   wins over the summed statistic. `source = "bill"`.
2. **Statistic**: `record.utility_cost` with the utility kWh, when
   `coverage.utility_cost >= min_coverage` and `coverage.utility >= min_coverage`.
   `source = "statistic"`.

For the open cycle only (2) applies, with whatever has been reported so far. The open
cycle uses `min_coverage = 0` because its data always lags (§3.3).

Otherwise `estimate` returns `None`. In particular there is no estimate for a `no_data`
cycle, or for a cycle where the cost statistic has gaps.

### 3.2 Which kWh

The rate must divide the cost by the kWh the utility charged for. HA's side uses the
matching quantity:

| export mode | billed kWh (utility) | HA kWh |
|---|---|---|
| `none` | `utility_kwh` | `metered_kwh` |
| `separate_rate` | `utility_kwh` | `metered_kwh` |
| `net_metering` | `max(utility_kwh − utility_export_kwh, 0)` | `netted_kwh` |

With `source = "bill"`, billed kWh is `bill.kwh`. For net metering that is already net.

- `rate = utility_cost / billed_kwh`. With `billed_kwh <= 0` (a net-zero or net-export
  month) there is no rate, and `estimate` returns `None`.
- `ha_cost = ha_kwh × rate`.
- `difference = utility_cost − ha_cost` (equivalently `(billed_kwh − ha_kwh) × rate`).
  Positive means the utility charged for more than HA saw, i.e. a possible overcharge.

Fixed charges, customer charges and most taxes are flat per cycle or proportional to the
total. They cancel out of the difference, so a blended rate that includes or leaves out
them changes the difference only through the rate itself. That is why a single blended
rate is good enough here.

### 3.3 The open cycle

- Utility cost and kWh to date both lag by 1–2 days. The rate to date is their quotient.
  It is only shown once utility kWh to date is at least `MIN_RATE_KWH` (10 kWh), so an
  early-cycle rate of a few kWh does not mislead.
- HA's estimate to date is `metered_kwh` (or `netted_kwh`) × that rate.
- The difference to date is **not** shown for the open cycle. The two sides cover
  different days because of the lag, so it would show the lag, not a billing error.
  The card shows the utility cost so far and HA's estimate instead (§6).
- Without a tariff, the projected bill becomes `project(HA kWh to date) × rate to date`,
  labelled as an estimate. With a tariff the tariff's projection is kept.

### 3.4 Currency

Costs are summed in the statistic's unit. If that is not `hass.config.currency`, the
WebSocket API returns `utility_currency` and the card formats those figures with it. No
conversion is made.

### 3.5 Tariff precedence

The two features are independent:
- `cost` / `expected_cost` stays tariff-only.
- The utility cost, rate, HA estimate and difference come only from the cost statistic
  and the bill.

With both configured, the card shows both: the tariff's expected cost, and the utility's
charge with HA's estimate at the utility's rate. A tariff never feeds `estimate`. The
cost statistic never feeds the tariff.

## 4. Coordinator and storage

- `cycle_totals` sums the cost statistic for every cycle it computes: open, closed,
  backfilled, and bill periods from `opower-bills.md` §3.5.
- Stored records written before this feature have no `utility_cost`. `from_dict` reads a
  missing key as `None`.
- **Fill on configure.** When the cost statistic is configured and a stored record has
  `utility_cost is None`, the coordinator sums the statistic for that record's dates and
  stores it with its coverage. It runs once per refresh over all such records, final ones
  included. Only `utility_cost` changes: kWh totals, status and finality are kept. Old
  records outside the statistic's history keep `None` with coverage 0.
- When the option is cleared, stored `utility_cost` values are kept but not shown. The
  WebSocket and sensors only report them while a cost statistic is configured.
- The `recompute` action recomputes `utility_cost` with everything else.
- Changing the statistic to a different ID clears `utility_cost` on all records, and the
  fill runs again.

## 5. WebSocket (`check_my_bill/cycles`, `check_my_bill/range`)

- Top level: `has_utility_cost: bool` and `utility_currency: str | null`.
- Each record gains `utility_cost` (the raw sum, or `null`) and:

```json
"cost_estimate": {
  "source": "bill",
  "utility_cost": 142.18,
  "billed_kwh": 711.0,
  "rate": 0.19997,
  "ha_kwh": 694.2,
  "ha_cost": 138.82,
  "difference": 3.36
}
```

  or `null` when `estimate` returns `None`. For `current`, `difference` is always `null`
  (§3.3), and `projected_ha_cost` is added when §3.3's projection applies.
- `range` uses the statistic only (no bill), with `min_coverage = 1.0`.

## 6. Card

Shown whenever `cost_estimate` is present, with or without a tariff.

- **Meters** (current cycle and the expanded row): the utility bar's caption gains
  "· $X billed" ("$X so far" for the open cycle). The HA bar's caption gains "· ≈ $Y".
- **Difference line** under the meters for closed cycles:
  - "Possibly overbilled by $Z" when `difference > 0`;
  - "Possibly underbilled by $Z" when `difference < 0`.

  It uses the status colour. When the status is `ok`, it uses the secondary text colour
  and reads "Within tolerance (±$Z)". The line has a tooltip/caption: "At the utility's
  average rate of $R/kWh this cycle."
- **Past-cycle rows** without a tariff show the utility cost on the right, with "≈ $Y HA"
  under it. With a tariff the current layout is kept (expected cost, "billed $" under it),
  and the difference line goes in the expanded row.
- **Current cycle** without a tariff: the big figure is the projected HA estimate (§3.3),
  labelled "Projected (est.)". "Billed so far" sits beside it. With a tariff the tariff's
  figures stay and the estimate appears only in the meter captions.
- The source is noted in small text in the expanded row: "from the bill" or "from Opower's
  cost data". The card also notes when `source = "statistic"` for a utility whose cost
  statistic is a flat estimate. That flag cannot be detected, so instead the card says
  "Utility cost data may leave out fees and taxes" once, under the past-cycles title.

## 7. Sensors

Only created while a cost statistic is configured:

| key | unit | value |
|---|---|---|
| `cycle_utility_cost` | currency | utility cost to date for the open cycle |
| `last_cycle_utility_cost` | currency | `cost_estimate.utility_cost` of the last closed cycle |
| `last_cycle_cost_difference` | currency | `cost_estimate.difference` of the last closed cycle |
| `last_cycle_rate` | currency/kWh | `cost_estimate.rate` of the last closed cycle |

All are `MONETARY` except the rate, which has no device class. They are `None`
(unknown) when `estimate` returns `None`. The difference sensor gets
`source`, `billed_kwh` and `ha_kwh` attributes.

## 8. Limits and caveats

- **Blended, not itemised.** Opower's cost does not split supply from delivery. For
  monthly net metering at full retail, and for no export, a blended rate is right. It is
  wrong where export is credited at a different rate from import:
  - supply-only netting;
  - annual true-ups;
  - net billing / avoided-cost credits (`separate_rate`).

  In `separate_rate` mode the estimate covers import only. The export credit is not
  compared. Those users need a tariff for a precise figure. The README says so.
- **Estimated cost data.** Some utilities publish a cost statistic that is a flat
  per-kWh estimate without fixed charges or taxes. The rate is then that flat figure.
  Because fixed charges cancel (§3.2) the difference is still meaningful; only the
  absolute "billed" amount understates the bill. `source = "bill"` is better where
  available.
- **Tiered and time-of-use rates.** A blended rate averages tiers and TOU periods. If the
  kWh HA saw and the utility did not fell in a dearer tier or period, the difference
  understates it. That is acceptable for an indication.
- **Gas** is out of scope. The cost statistic must belong to the same meter as the
  utility import statistic. The flow does not check this beyond the suggestion.

## 9. Tests

`tests/test_utility_cost.py` (pure):
- Bill preferred over statistic; statistic used without a bill; `None` with low coverage,
  no utility data, or zero billed kWh.
- Each export mode's kWh pairing (§3.2), including a net-export month.
- Sign of `difference` (over and under).
- Open cycle: no difference, `MIN_RATE_KWH` threshold, projection.

`tests/test_record.py`: `utility_cost` and `coverage.utility_cost` round-trip; a dict
without them loads as `None`.

`tests/test_stats.py` (or `tests_ha`): cost series fetched without unit conversion and
summed per cycle with coverage.

`tests_ha/test_utility_cost.py`:
- The options step suggests the `_energy_cost` sibling, rejects a kWh statistic, and
  removes the option when cleared.
- After configuring, stored records (including final ones) get `utility_cost` filled
  once, and kWh totals are unchanged.
- WebSocket returns `cost_estimate` with and without a tariff; `has_utility_cost` false
  hides it.
- Sensors exist only with the option, and report the last closed cycle's difference.

The card is checked by hand against the dev instance with a fake cost statistic. Use
generated data only; no real account IDs or readings go in fixtures.

## 10. Deviations

- `bill-validation.md` priced cycles only from a manual tariff. This adds a second,
  independent source of cost. It does not change the reconciliation status, which stays
  kWh-based.
- `card.md` §4: costs were hidden without a tariff. With a cost statistic they are shown
  (§6).
