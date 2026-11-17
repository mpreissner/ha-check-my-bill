# Config and options flow (phase 3)

Implements `bill-validation.md` §3.1. This file settles the details that section leaves
open. Where the two disagree, this one wins, and the deviation is listed in §6.

## 1. Modules

- `discovery.py` (HA-level, no flow logic):
  - `grid_statistics(hass) -> list[GridPair]`: `(import_stat, export_stat | None)` for
    every grid source in the Energy prefs. Both layouts are read: the legacy `flow_from` /
    `flow_to` lists and the flat `stat_energy_from` / `stat_energy_to` source.
  - `opower_accounts(hass) -> list[OpowerAccount]`: electric accounts that have an
    `opower:<prefix>_energy_consumption` sum statistic.
- `config_flow.py`: the config flow, a reconfigure step and the options flow.
- No change to the coordinator's inputs: entry data and options keep the keys in
  `const.py`.

### Matching an Opower account to its date sensors

Opower builds the two names differently:

| What | Format |
|---|---|
| statistic | `opower:{subdomain}_{elec}_{account}_energy_consumption`, lowercased, `-` → `_` |
| entity unique_id | `{subdomain}_{account}_elec_start_date` / `_elec_end_date` |

Both normalize to the same key: take the statistic prefix and replace its first `_elec_`
with `_`; take the unique_id without its `_elec_{start,end}_date` suffix, lowercased with
`-` → `_`. Entities are looked up in the entity registry (platform `opower`), so they are
found even when disabled.

`OpowerAccount` holds:
- `key`
- `consumption_stat`
- `return_stat` (only if that statistic exists)
- `start_entity` and `end_entity` (either can be `None`)
- `config_entry_id` and `title`, taken from the Opower config entry

## 2. Config flow

1. **`user`: meter.** Only the meter choice, electric (default) or gas. Gas goes to the
   `gas` step (gas.md §2.1); electric to `electric`.
   - **`electric`: grid.** `import_stat` is required; `export_stat` is optional.
   - Both are dropdowns of the recorder's cumulative statistics in an energy unit other
     than therms, leaving out Opower's (they are the utility's data). HA's statistic
     picker cannot filter by unit, so the options come from the statistic metadata.
   - Both default to the first grid source in the Energy prefs, when the dropdown lists it.
     With no Energy grid source the dropdowns start empty, because the user may track
     import with a statistic the Energy dashboard doesn't use.
   - With no fitting statistic at all, the flow aborts with `no_electric_statistics`.
2. **`utility`**, shown only when at least one Opower account is found.
   - `account` is a select of the accounts plus **"Don't use utility data"**, defaulting
     to the first account.
   - `enable_date_sensors` is a boolean, default on, shown only if a date entity of any
     account is disabled by the integration.
   - Choosing an account stores `utility_import_stat` and, if it exists, `utility_export_stat`.
   - **Date sensors** (`cycle_start_entity` and `cycle_end_entity`) are stored only when both
     exist and neither is disabled by the user. If any date entity is disabled by the
     integration:
     - with `enable_date_sensors` on, every such entity is enabled with
       `async_update_entity(..., disabled_by=None)`, and the Opower entry is reloaded after
       the flow creates the entry;
     - with it off, the sensors are not stored.
   - If the sensors are not stored, go to `cycle`; otherwise go to `tariff`.
3. **`cycle`**:
   - Fields are `cycle_day` (1–28) or `last_bill_end` (date), and exactly one is required
     (error `cycle_one`).
   - `last_bill_end` converts to `cycle_day = (last_bill_end + 1 day).day`, clamped to
     1–28. Only `cycle_day` is stored.
4. **`tariff`**:
   - Fields:
     - `supply` and `delivery` ($/kWh)
     - `fixed_monthly` and `fixed_daily` ($)
     - `tax_pct`
     - `export_mode` (select: none / net_metering / separate_rate)
     - `export_rate` ($/kWh)
   - Leaving both `supply` and `delivery` empty skips the tariff: no cost sensors, kWh only.
     Filling only one of them is an error (`rates_both`).
   - The first version's `effective_from` is today. `Tariff.version_on` applies the first
     version to earlier days, so backfilled cycles are still priced.
   - The step creates the entry. Data goes to `entry.data`; `tariff`, `export_mode` and
     `tolerance` (2.0) go to `entry.options`.

**Entry identity:**
- `unique_id` is `utility_import_stat` when it is set, otherwise `import_stat`. A
  duplicate aborts with `already_configured`.
- The title is `Check My Bill (<Opower entry title>)`, or `Check My Bill` without a
  utility. Account numbers never appear in titles.

**After entry creation:** if sensors were enabled, call
`hass.config_entries.async_schedule_reload(opower_entry_id)`. Until the sensors report, the entry
retries setup (`no_cycle`), which is the existing coordinator behaviour.

## 3. Reconfigure (`async_step_reconfigure`)

- Edits entry data. It is a single form with all of these fields:
  - `import_stat` and `export_stat`
  - `utility_import_stat` and `utility_export_stat`
  - `cycle_start_entity` and `cycle_end_entity`, as entity pickers on the `sensor` domain
  - `cycle_day`
- Pre-filled from the entry.
- **Validation:**
  - `import_stat` is required.
  - Date entities must come as a pair (`cycle_pair`).
  - Without date entities, `cycle_day` is required (`cycle_one`).
- Changing any statistic discards the stored history, so the next setup backfills with the
  new sources. The store records the statistic IDs its records were computed from
  (`StoredState.sources`); on load, the coordinator drops the history when they differ
  from the entry's. A store written before this field existed adopts the current IDs.
  Changing only the cycle source keeps the history; a moved read day closes the cycle in
  progress as usual.
- Ends with `async_update_reload_and_abort`.

## 4. Options flow

A menu with three steps:

- **`rates`: add or change a rate version.**
  - The tariff step's rate and charge fields (not `export_mode`, which is in `settings`),
    plus `effective_from` (default today).
  - It is pre-filled from the version in effect today.
  - Submitting replaces the version with the same `effective_from`, or inserts a new one.
    Tiers and riders always carry over from the version it was pre-filled from. A rate's
    seasonal overrides carry over only when that rate's default was not changed.
- **`rate_versions`: all versions as YAML.**
  - Uses an `ObjectSelector` holding the list in the `serde.py` shape. This is how users
    reach tiers, seasonal rates and riders.
  - Validated with `tariff_from_options`. Errors show as `invalid_tariff`.
  - An empty list removes the tariff.
- **`settings`**:
  - `tolerance` (%, 0.1–20)
  - `export_mode`, which is also a field of the config flow's `tariff` step
  - `export_rate` lives on the version.

Option changes reload the entry through the existing update listener. On that load the
stored records are repriced with the new settings (`services-api.md` §2).

## 5. Tests (`tests_ha/test_config_flow.py`)

- **Opower present with disabled date sensors:**
  - Every step pre-fills.
  - Enabling flips `disabled_by` and reloads Opower.
  - The entry data matches the account's IDs, and the title is correct.
- Opower absent goes grid → cycle → tariff. The `last_bill_end` conversion is tested,
  along with the `cycle_one` error.
- The skip choice ("Don't use utility data") leads to cycle.
- Date sensors disabled by the user are not enabled, and the flow goes to `cycle`.
- A duplicate account aborts.
- Rates: `rates_both` error, and an empty tariff leaves options without a tariff.
- Reconfigure: validation errors, history removed on a stat change, and history kept on a
  cycle-only change.
- **Options flow:**
  - `rates` adds a version and replaces a same-day one.
  - Bad YAML in `rate_versions` gives `invalid_tariff`.
  - `settings` saves.
- The normalization in `discovery.py` (`key_from_statistic`, `key_from_unique_id`) and
  `grid_pairs` for both layouts. These live in `tests_ha` because `discovery.py` imports HA.

The placeholder flow handlers in `test_coordinator.py` and `test_fixture_replay.py` are
removed, and `manifest.json` sets `config_flow: true`.

## 6. Deviations from `bill-validation.md` §3.1

- **Data sources are edited through reconfigure, not options.** HA's guidelines reserve
  options for settings that don't change what the entry is; changing statistics redefines
  the entry and resets its history.
- **`last_bill_end` is converted to a `cycle_day`, not stored.** The coordinator's manual
  mode is month-anchored, so a separate ~30-day cadence would add a second manual mode for
  the same estimate.
