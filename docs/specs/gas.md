# Gas meters

Check My Bill compares an electric meter that HA reads with the utility's readings. Gas
bills have the same problem and the same data. The Opower integration publishes gas
statistics, and HA's Energy dashboard has gas sources. This feature lets a config entry
validate a gas meter instead of an electric one.

Each entry stays one meter. A household with both adds two entries. They are independent:
cycle dates, tariff, tolerance, sensors and a card each.

## 1. Modules

- `core/units.py` (pure):
  - `Meter` (`StrEnum`): `electric`, `gas`.
  - `scale(from_unit, to_unit)` returns the factor that converts one unit
    to the other (§3). It uses HA's unit converters, passed in so the core stays
    HA-free, and returns `None` when no conversion is possible.
- `const.py`: `CONF_METER`, `CONF_UNIT`, `CONF_UTILITY_UNIT`;
  `DEFAULT_GAS_TOLERANCE_PCT = 5.0`.
- `stats.py`:
  - `StatIds` gains a per-statistic `scale` mapping.
  - `fetch_changes` fetches without unit conversion (`units=None`) and multiplies each
    change by its statistic's scale, so every series is already in the entry's unit.
  - `cycle_totals` takes a `daily_only` flag (§4).
- `discovery.py`: Opower gas accounts and the Energy dashboard's gas sources (§2.1).
- `config_flow.py`: the meter-type choice and the gas steps (§2).
- `opower_bills.py`: gas bill reads work unchanged; their `consumption` is scaled like the
  utility statistic (§3.3).
- `sensor.py`, `websocket.py`, the card: units come from the entry (§5–§7).

### 1.1 Unit-neutral names

Quantities are no longer named after kWh. Everything is in the entry's **billing unit**
(§2.1), and the unit is carried alongside, never implied by a name. This lands first as
its own refactor, with no behaviour change, so the gas work does not mix with a rename.

| before | after |
|---|---|
| `CycleTotals` / `CycleRecord`: `metered_kwh`, `export_kwh`, `netted_kwh`, `utility_kwh`, `utility_export_kwh` | `metered`, `export`, `netted`, `utility`, `utility_export` |
| `BilledAmounts.kwh` | `BilledAmounts.quantity` |
| `CostBreakdown.billed_kwh` | `billed_quantity` |
| `CostBasis.import_kwh`, `export_kwh` | `imports`, `exports` |
| tariff tier `up_to_kwh` | `up_to` |
| rider `per_kwh` | `per_unit` |
| `EXPORT_THRESHOLD_KWH` (issues) | `EXPORT_THRESHOLD` |
| `CALIBRATION_MAX_EXPORT_KWH` (reconcile) | `CALIBRATION_MAX_EXPORT` |
| last-cycle sensor attributes `metered_kwh`, `utility_kwh` | `metered`, `utility` |
| rate docstrings "$/kWh" | "per billing unit" |

- **Storage.** `CycleRecord.from_dict`, `cost_from_dict` and the tariff serde read both
  the old and the new keys. They write only the new ones. Stored cycles and tariff
  options from earlier betas load unchanged and are rewritten on the next save. The
  `Store` goes from version 1.1 to 1.2, with a migration that renames the keys. It is a
  minor bump, so a downgrade still opens the store; the older code then recomputes the
  records it cannot read. Tariff options are rewritten when the options flow saves them.
- **Sensor attributes.** The last-cycle sensor's `metered_kwh` and `utility_kwh`
  attributes become `metered` and `utility`. Templates that read the old names must be
  updated; the release notes say so.
- **WebSocket.** Records use the new keys: `metered`, `utility`, `bill.quantity`, and so
  on. The card is updated in the same change. It is the only client and ships with the
  integration, so there is no compatibility shim.
- **Services.** `recompute` and `set_cycle_dates` take no quantity fields, so they are
  unchanged.
- **Electric entries** have billing unit `kWh`, so everything they show is the same as
  today.

## 2. Configuration

### 2.1 Setup

The first step gains a **Meter** choice, electric or gas, before the statistic pickers.
Electric is the default and behaves exactly as today.

For **gas**:

- **user step**: one statistic, HA's gas consumption. There is no export statistic.
  - It is pre-filled from the Energy dashboard's first `gas` source (`stat_energy_from`).
  - Statistics with a volume unit (`m³`, `ft³`, `CCF`, `MCF`, `L`, `gal`) or an energy
    unit (`kWh`, `MJ`, `GJ`, `thm`, …) are accepted. Anything else is rejected with
    `not_a_gas_statistic`.
- **utility step**: lists Opower **gas** accounts. These are statistics whose prefix
  holds `_gas_`, and their date sensors end in `_gas_start_date` and `_gas_end_date`. The
  discovery key includes the meter type, so an electric and a gas account with the same
  account number are not confused.
- **units step** (new, gas only, always shown):
  - **Billing unit**: "What unit does your gas bill charge by?" The choices are therms
    (`thm`, the default), CCF, m³ and kWh. The answer becomes the entry's unit
    (`CONF_UNIT`). All quantities, rates, tiers and sensors use it.
  - **Utility data unit**: the unit of the utility statistic, when there is one. The
    Opower integration labels every gas statistic `CCF`, even for utilities whose data
    is in therms. The flow reads the account's real unit from the Opower coordinator's
    forecast (`unit_of_measure`), through the same guarded access as `opower_bills.py`
    §4. It pre-fills from that, and the user can correct it. For non-Opower statistics,
    it is taken from the statistic's metadata and not shown.
- **cycle** and **tariff** steps as today, except:
  - rate fields are labelled per entry unit (e.g. `$/thm`);
  - there is no export mode;
  - the tolerance default is 5% (§4).

The billing unit is stored in `entry.data`. Reconfigure can change it. A change reprices
and recomputes non-final cycles. Final cycles are converted in storage by the same scale,
so the history stays in one unit.

### 2.2 Existing entries

Entries without `CONF_METER` are electric with unit `kWh`. No migration step and no
`VERSION` bump are needed: defaults are read on load.

### 2.3 Options

- Settings: tolerance; no export mode for gas.
- Reconfigure keeps the meter type. Changing it means a new entry.

## 3. Units

### 3.1 Scales

Every statistic is converted to the billing unit by a fixed factor:

- Same class (volume to volume, energy to energy): HA's `VolumeConverter` or
  `EnergyConverter` factor. Examples: `m³` to `CCF` is 0.353147; `kWh` to `thm` is
  0.034121.
- Volume to energy: 1 CCF = 1 thm, as most US utilities bill. There is no heat-content
  setting. Other units go through the converters on each side. For example, `m³` to `kWh` is
  `m³ → CCF → thm → kWh`.
- Opower gas statistics use the utility data unit from §2.1, not the statistic's
  metadata.
- For example, with HA's meter in `ft³`, Opower data in CCF, and billing in therms, the HA
  scale is `ft³ → CCF` (0.01) × 1.037, and the utility scale is CCF × 1.037. Both
  sides of the comparison, and the tariff, see therms.

Scales are computed when the coordinator is set up and again after an options change.
A statistic whose unit has changed since setup (metadata differs from what the scale
assumed) raises a repair issue, `unit_changed`, and its scale is recomputed from the new
metadata.

### 3.2 Tariff

The tariff is unit-agnostic after §1.1: rates are per billing unit, and tiers are
cumulative quantity in it. The rate form's fields read `$/thm`, `$/CCF`, … from the
entry. The user types the rates exactly as their bill prints them.

### 3.3 Bills

Opower bill reads for gas carry `consumption` in the account's unit (therms or CCF). They
are scaled to the billing unit like the utility statistic, so `bill.quantity` is in the
billing unit too.

## 4. Computation

- **No export.** Gas entries have no export statistics; `ExportMode` is always `none`.
- **Daily comparison.** Opower gas data is daily at best, and many utilities post it a day
  or more late. Gas entries always use `day` slots (`daily_only=True`), for the open
  cycle too. Coverage is then per day, and an hourly query never makes it look complete.
- **Default tolerance 5%.** Gas meters register uncorrected volume. The utility corrects
  for temperature, pressure and altitude, and a bill in therms may apply a therm factor
  that this integration treats as 1. 2% would flag normal months; the user can tighten it.
- **Calibration ratio** works as for electric. It is informative only.
- Reconciliation, status bands, projection and pricing are otherwise unchanged.

## 5. Sensors

- `cycle_import`:
  - device class `GAS` when the unit is a volume unit, or `ENERGY` when it is `kWh`,
    `MJ`, `GJ`, …;
  - unit is the entry's unit;
  - state class `total_increasing` as today.
  - **Exception:** HA's `GAS` device class does not accept `thm`. A therm entry uses
    device class `ENERGY` with unit `thm`, which `EnergyConverter` supports.
- The translation keys stay the same. Names come from the entry title ("Gas" in the
  suggested title), not from the sensor names.
- Monetary sensors are unchanged.

## 6. WebSocket

`check_my_bill/cycles` and `check_my_bill/range` gain at top level:

- `meter`: `"electric"` or `"gas"`;
- `unit`: the billing unit, e.g. `"kWh"`, `"thm"`, `"CCF"` or `"m³"`.

Record keys are the unit-neutral names from §1.1.

## 7. Card

- Every "kWh" in the card comes from `unit`. That covers the meter labels, the band
  caption, the row line ("HA x · utility y CCF") and the rate units.
- The header badge uses a flame icon for gas and the lightning icon for electric.
  The default title is "Gas bill" or "Electric bill".
- There is no export block for gas (the export mode is `none` anyway).
- The lag note for gas says "utility data lags 1–3 days".

## 8. Entry title and unique ID

- The suggested title is "Check My Bill – Gas" or "Check My Bill – Electric". The user can
  rename it.
- The unique ID stays the utility statistic, or the HA statistic without one. Electric and
  gas statistics differ, so both entries can exist side by side.

## 9. Tests

Refactor (§1.1):
- **Renamed tests.** Existing tests are renamed to match and must pass with no other
  change.
- **`tests/test_record.py` and `tests/test_serde.py`.** A record or tariff dict with the
  old `_kwh` keys loads. Saving it writes the new keys.
- **`tests_ha`.** The store migration renames keys in a stored state from an earlier beta.

`tests/test_units.py`:
- Same-class scales (`m³` to `CCF`, `kWh` to `thm`).
- Cross-class scales at 1 CCF = 1 thm.
- `None` for an unsupported pair.

`tests/test_discovery.py` (or the existing discovery tests):
- Gas statistics and `_gas_` date sensors are found.
- An electric and a gas account with the same number get distinct keys.
- Electric discovery is unchanged.

`tests_ha/test_gas.py` (generated statistics only):
- **Setup, mixed units:** a gas flow with an HA statistic in `ft³`, an Opower-style
  utility statistic in CCF, and billing in therms. The flow stores unit `thm`. Both totals come out in therms.
- **Setup, therm data:** an Opower statistic labelled `CCF` whose account forecast says
  `THERM`. The utility data unit pre-fills to therms, and no scale is applied to it.
- **Changing the unit:** changing the billing unit by reconfigure converts final cycles
  in storage.
- **Daily slots:** an open gas cycle with daily-only utility data reports full utility
  coverage for the days present.
- **Sensors:** `cycle_import` has unit and device class per §5.
- **WebSocket:** returns `meter` and `unit`.
- **Old entries:** an entry without `CONF_METER` loads as electric, kWh. All existing
  tests pass unchanged.

`tests_ha/test_config_flow.py`:
- The meter choice.
- A gas statistic is rejected on an electric entry, and the reverse.

Card changes are checked by hand on the dev instance with a generated gas statistic. No
real account IDs or readings go in fixtures.

## 10. Deviations

- `bill-validation.md` and `config-flow.md` assumed an electric grid meter and named
  quantities in kWh. This generalises the meter, and §1.1 renames the fields. The WebSocket
  record keys change, so the card and integration must ship together. They already do.
- `utility-cost.md` uses the old names (`utility_kwh`, `bill.kwh`). Read them as the §1.1
  names.
- `opower-bills.md` §4 and `discovery.py` only handled electric accounts. Both now take
  the meter type into account.
- `utility-cost.md` (not built) carries over. Its rate becomes per entry unit. "Gas is
  out of scope" there is superseded once this ships.
