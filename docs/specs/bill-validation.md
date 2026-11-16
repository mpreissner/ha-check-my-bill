# Bill validation: billing-cycle kWh and expected cost from Energy dashboard data

Status: spec rev 2. Base: dev @ f061a56 (scaffold only).
Rev 2 applies the user's answers to Q6–Q8 (§10). Bill entry is removed, because the
integration is informational and not a bill tracker.
Author: architect (main session). Scope: v1 of the integration. Electric only.
It covers the config flow, tariff model, cycle records, reconciliation, sensors, a
WebSocket API and a Lovelace card. Gas is out of scope (§9).

## 1. The request

> "The purpose of this integration will be to validate utility bills against metrics
> collected for the energy dashboard. For someone who has already configured all relevant
> sensors, billing rates, etc., we should simply be able to use the relevant entities from
> the energy dashboard. If they are missing an entity, we need to prompt them for the
> correct entity (or allow Skip). Ideally, I would want this integration to provide a
> widget to the energy dashboard that shows their data according to their previous and
> current-to-date billing cycles (billing dates pulled from integrations like Opower ...).
> If we can't provide a widget to the energy dashboard, then we should include a card that
> gets configured with the relevant entities during setup and can be easily added to any
> user-managed dashboard."

The user's answers to the clarifying questions:

- **Validation depth:** kWh **and** a full tariff (supply, delivery, fixed charges, taxes).
- **Cycle dates:** offer to enable the utility's cycle-date sensors. Keep a historical
  record each time a cycle rolls over, so earlier cycles can be viewed.
- **Gas:** electric only for v1.
- **Repo:** public on GitHub, so CI can run on GitHub runners.
- **Export:** no net metering. The site runs zero-export, so a small amount of export is
  expected and is not compensated.

## 2. Evidence

These findings come from the author's production instance (HA 2026.11.1, frontend
20260826.7), read through WebSocket on 2026-11-16. Entity IDs and account numbers are
deliberately left out of this public repo.

### 2.1 Energy dashboard configuration (`energy/get_prefs`)

- **Grid source:** `flow_from` and `flow_to` are the lifetime import and export counters
  of a solar gateway (CT-measured). `stat_cost`, `entity_energy_price` and
  `number_energy_price` are all **null**, so no cost or price is configured.
- **Gas source:** an Opower external statistic.
- **Also configured:** solar, battery, a "home" total (panel site energy) and about 30
  device consumption entries.
- **Implication:** "use the Energy dashboard's entities" means the grid import and export
  stats. Price is usually missing, which is why the integration owns the tariff (§3.3)
  rather than reading prices from Energy.

### 2.2 Opower (core integration)

- **External statistics** `opower:<utility>_elec_<account>_energy_{consumption,return,cost,compensation}`.
  - These are hourly or daily, and lag about 2 days.
  - `cost` is a flat ~$0.20/kWh estimate. It has no fixed charges and no taxes, so it
    cannot be the bill.
- **Sensors** `sensor.<service>_current_bill_electric_*`: usage to date, forecasted usage,
  cost to date, forecasted cost, typical usage and cost.
  - `..._start_date` and `..._end_date` come from `OpowerData.forecast` and are created
    **disabled by default** (`entity_registry_enabled_default=False`, DIAGNOSTIC).
  - On this instance they are `disabled_by=integration`.
- **No past bill totals.** The coordinator fetches `AggregateType.BILL` cost reads but folds
  them into statistics. Bill amounts and past cycle boundaries are not exposed as entities.

### 2.3 Utility meter vs. Energy grid counters (Oct 1 to Nov 14)

| | kWh |
|---|---|
| Opower consumption (daily sum) | 1050.47 |
| Gateway import (daily sum) | 1074.74 |
| Opower return | 0 |
| Gateway export | 27.7 |

The hourly analysis covers 196 hours that contained any export:

- **Opower import:** 74.3 kWh in those hours. That is between the gateway's gross import
  (82.6) and its hourly-netted import, max(0, import − export) per hour (60.4).
- **Hypothesis:** the AMI meter nets export against import within sub-hourly intervals,
  probably 15 minutes. **Unverified.**
- **Hours with no export:** Opower reads about 1.9% *above* the gateway, which points to CT
  calibration.

**Implications:**

1. Agreement within about 2% is the realistic bar.
2. When a site exports, the meter figure has to be compared against a *range*, not a single
   point (§3.5).

### 2.4 Energy panel extensibility (frontend 20260826.7)

- **No slot for third-party cards.** The Energy panel is strategy-generated
  (`energy-view-strategy`, with electricity, gas and water views).
  `dialog-energy-customise.ts` only toggles built-in cards from `ENERGY_CARD_CATALOG`,
  saved as `frontend/system_data` `energy.hidden_cards`.
- **The date picker is reachable.** The Energy collection lives on the connection:
  - `getEnergyDataCollection` stores it at `hass.connection["_" + key]`.
  - The default `key` is `"energy_" + hass.panelUrl`; with no panel URL it is the legacy
    `_energy`.
  - It exposes `subscribe(cb)`, where `cb` receives `{start, end, ...}`, and
    `setPeriod(start, end)`.
  - This is a private API. The card must tolerate it being missing (§3.7).

**Conclusion:** there is no supported way to inject into the Energy panel itself. Ship a
custom card. It can sit on any dashboard, including a user dashboard that also uses the
built-in energy cards and an `energy-date-selection` card, and it can optionally follow
that date selection.

## 3. Decisions (recommendation first)

### 3.1 Config flow (`config_flow: true`, single instance per utility account)

1. **Grid step**
   - Read `energy/get_prefs` server-side by calling
     `homeassistant.components.energy.data.async_get_manager(hass)` and reading
     `manager.data`.
   - Pre-fill `import_stat` from the grid source's `flow_from`. With several sources, offer
     a selector; if there are none, leave it blank.
   - Pre-fill `export_stat` from `flow_to`.
   - Each field is a statistic picker. `export_stat` can be skipped. `import_stat` is
     required, because without it there is nothing to validate.
2. **Utility step**
   - **Auto-detect** an Opower config entry and match its electric account.
   - Offer three things:
     - The `utility_import_stat` (consumption) and `utility_export_stat` (return)
       statistics. Both are skippable.
     - The `cycle_start` and `cycle_end` date sensors. If they are disabled, show a
       checkbox: **"Enable the utility's billing-date sensors"** (default on).
     - **Skip**, which falls back to manual cycle dates (step 3).
   - Enabling uses `er.async_update_entity(entity_id, disabled_by=None)`, but only when
     `disabled_by` is `INTEGRATION`, never `USER`.
   - The Opower entry is reloaded so that the sensors appear.
3. **Cycle step** (only if there are no cycle sensors)
   - `cycle_day` is the meter-read day, 1–28. Alternatively, `last_bill_end` is a date with
     an approximately 30-day cadence; this alternative is stored but marked approximate.
   - Utility read dates drift by a few days, so manual mode is labelled "estimated" in the
     UI.
4. **Tariff step**: see §3.3. It can be skipped, in which case the integration validates
   kWh only.
5. **Options flow:** edit everything above, plus the tariff, the tolerance (default 2%) and
   the export mode.

The config flow stores **statistic IDs, not entity IDs**, where possible. Opower data
exists only as external statistics, and grid flows in Energy prefs are statistic IDs.

### 3.2 Cycle source of truth and history

**Recommendation:** use the utility date sensors when available, and persist every cycle
as a record when it closes.

- **Current cycle:** `[start, end)` from the cycle sensors. Opower dates are local
  calendar dates, so `start` is local midnight on the start date and `end` is local
  midnight on the day after the end date. In manual mode, the boundaries are derived from
  `cycle_day`.
- **Rollover detection:** watch the start-date sensor (state change), plus a daily time
  check for manual mode. When `start` advances, close the previous cycle by setting its end
  to the new start and computing its record (§3.4). The record is computed again on each of
  the next **3 days**, and then frozen, because Opower data lags about 2 days.
- **Store:** `homeassistant.helpers.storage.Store`, key `check_my_bill.<entry_id>`,
  version 1:
  ```json
  {"cycles": [{"start": "2026-10-03", "end": "2026-11-02", "source": "utility|manual|backfill",
               "final": true, "metered_kwh": 0.0, "utility_kwh": 0.0, "export_kwh": 0.0,
               "netted_kwh": 0.0, "expected_cost": {"supply": 0.0, "delivery": 0.0,
               "fixed": 0.0, "tax": 0.0, "total": 0.0}, "tariff_version": 3,
               "coverage": {"import": 1.0, "utility": 1.0},
               "status": "ok|warn|mismatch|no_data"}]}
  ```
- **Backfill on first setup:** create records for **up to 12** previous cycles, limited
  to the history that actually exists.
  - Step back cycle by cycle and stop at the first cycle where neither `import_stat` nor
    `utility_import_stat` has any hourly data.
  - A cycle that is only partly covered is kept, with its `coverage` recorded and its status
    set to `no_data` if the utility side is missing.
  - Boundaries are estimated by stepping back from the current start in whole-month steps
    on the same day.
  - These records are marked `source: backfill` and labelled "estimated dates".
  - Each estimated boundary can be edited (§3.6). Editing recomputes the record.
- **No bill entry.** The integration is informational, not a bill tracker. It shows
  measured kWh, utility kWh and expected $ for each cycle, and the user compares those
  against their bill themselves.

### 3.3 Tariff model

**Recommendation:** a small, explicit model edited in the options flow. The integration
does not try to import utility rate schedules.

```text
Tariff (versioned; each version has effective_from date)
  supply_rate        $/kWh, or seasonal: [{months: [6,7,8,9], rate}, ...]
  delivery_rate      $/kWh, same shape (distribution + transmission riders lumped)
  tiers (optional)   [{up_to_kwh, supply_rate?, delivery_rate?}] — applied per cycle
  fixed_monthly      $ per cycle (customer charge)
  fixed_daily        $ per day (alternative/extra; some utilities bill per day)
  riders_per_kwh     list of {name, $/kWh}   (surcharges, EmPOWER-style programs)
  tax_pct            % applied to subtotal
  export_mode        none | net_metering | separate_rate
  export_rate        $/kWh (separate_rate only)
```

- **Time-of-use** is out of scope for v1 (§9). The model leaves room for it: a rate can
  later become a schedule.
- **Rate changes:** a cycle that spans a rate change is prorated by day across tariff
  versions. Utilities do this too.
- **Export modes:**
  - `none`, which is this user's mode: export earns nothing and does not offset import.
    The cost basis is the utility-measured import (§3.5).
  - `net_metering`: billed kWh = import − export, floored at 0 per cycle. Carry-over credit
    is out of scope.
  - `separate_rate`: export earns credit at `export_rate`.
- **Signal for a mode change:** if `export_mode` is `none` but the utility return statistic
  becomes nonzero, raise a repair issue: "Your utility is now recording export. Check
  whether net metering started."

### 3.4 Computation (pure core)

The inputs are a cycle `[start, end)` plus hourly statistic sums (`change`) for:

- `import_stat` (the Energy grid import)
- `export_stat` (optional)
- `utility_import_stat` (optional)
- `utility_export_stat` (optional)

Values per cycle:

- `metered_kwh`: Σ import. This is the gross import seen by HA.
- `export_kwh`: Σ export.
- `netted_kwh`: Σ max(0, import_h − export_h). This is the lower bound for meters that net
  sub-hourly.
- `utility_kwh`: Σ utility import (null if there is no utility stat).
- `coverage`: the fraction of hours in the cycle that have data for each statistic. When
  the utility statistic covers less than 100% of the hours (because of the lag), the cycle
  is "in progress" and the utility figure is partial.
- `expected_cost`: the tariff applied to the **cost basis kWh**:
  1. `utility_kwh` when its coverage is 100%;
  2. otherwise `metered_kwh` (labelled "estimate from HA meter").

Reading the data:

- Use `recorder.statistics.statistics_during_period(hass, start, end, ids, "hour", None, {"change"})`
  in the recorder executor.
- For long-term statistics, hourly resolution is retained indefinitely, so backfilled
  cycles get hourly netting too.
- If hourly data is missing for a stat, fall back to `"day"` and set `netted_kwh = null`.

### 3.5 Reconciliation

The status for each cycle compares `utility_kwh` (the utility's metered figure) against
the HA-measured band.

- **Expected band:**
  - `export_mode` is `none` or `separate_rate`:
    `[netted_kwh × (1 − tol), metered_kwh × (1 + tol)]`.
  - `export_mode` is `net_metering`: `(metered − export) × (1 ± tol)`.
  - `tol` defaults to 2%.
- **Status:**
  - `ok`: the utility kWh is inside the band.
  - `warn`: within 2 × tol of the band.
  - `mismatch`: further out than that.
  - `no_data`: there is no utility statistic. Only the HA kWh and expected cost are shown.
- **Why 2%:** the user's CTs are revenue grade, so measured and billed kWh should agree
  closely. 2% is the starting default and can be changed in options.
- **Calibration hint (display only):** in cycles with no export, show the ratio
  `utility_kwh / metered_kwh`. A stable offset means a calibration difference between
  the CTs and the meter, not a billing problem. With revenue-grade CTs it should stay well
  under the tolerance; the ~1.9% seen in §2.3 is worth watching.

### 3.6 Entities, services, WebSocket API

All entities hang off one service-type device per entry.

**Sensors**

| key | unit | notes |
|---|---|---|
| `cycle_import` | kWh | `metered_kwh` of the current cycle, refreshed hourly |
| `cycle_expected_cost` | $ (currency from `hass.config.currency`) | current cycle, to date |
| `cycle_projected_cost` | $ | linear projection to the cycle end |
| `cycle_status` | enum | ok/warn/mismatch/no_data/in_progress |
| `cycle_start`, `cycle_end` | date | the cycle in use, including when it comes from manual mode |
| `last_cycle_expected_cost` | $ | last closed cycle, with attributes for kWh, utility kWh and status |

**Services**

- `check_my_bill.set_cycle_dates` takes `entry_id`, `cycle_start`, `start` and `end`. It
  edits estimated boundaries and recomputes the record.
- `check_my_bill.recompute` takes `entry_id` and `cycle_start?`.

**WebSocket API** (`websocket_api.async_register_command`, admin not required, read-only):

- `check_my_bill/cycles` takes `{entry_id}` and returns `[cycle record]` plus the current
  cycle (live).
- `check_my_bill/range` takes `{entry_id, start, end}` and returns the same computation for
  an arbitrary range. The card uses this when it follows the Energy date picker.

The card is read-only; there is no write path over WebSocket.

**Repairs**

- The utility is now recording export but `export_mode` is `none` (§3.3).
- A configured statistic has disappeared, for example after an Opower re-auth renamed it.

### 3.7 Card (`custom:check-my-bill-card`)

**Serving**

- The JavaScript is served from `custom_components/check_my_bill/frontend/check-my-bill-card.js`
  using `hass.http.async_register_static_paths([StaticPathConfig(URL, path, cache_headers=True)])`.
- `frontend.add_extra_js_url(hass, URL + "?v=" + VERSION)` is called at integration setup,
  so users never add a resource by hand.
- The card is plain JS built on `HTMLElement` and `lit` from HA's bundle, with **no build
  step**. It is a single file under 1000 lines.
- It registers itself in `window.customCards`, so it appears in the card picker.

**Config**

```yaml
type: custom:check-my-bill-card
entry_id: <picked in the visual editor; auto-filled if only one entry exists>
mode: cycles          # cycles | follow_energy
cycles: 6             # how many past cycles to list
```

**Content**

1. **Current cycle (to date):** the dates, days elapsed out of the total, HA kWh, the
   utility kWh with coverage % and lag note, expected $ to date, projected $, and a status
   pill.
2. **Previous cycles table:** dates (marked "est." when backfilled), HA kWh, utility kWh,
   expected $, and status. Clicking a row expands a cost breakdown (supply, delivery,
   riders, fixed, tax) and the reconciliation band.

**`follow_energy` mode**

- The card looks up `hass.connection["_energy_" + panelUrl]`, then the legacy `_energy`.
- When it finds one, it subscribes and renders a single "selected range" summary from
  `check_my_bill/range`.
- When it does not, it falls back to `cycles` mode with a small note.
- The private-API access is isolated in one function, so a frontend change breaks one
  feature, not the card.

## 4. Module layout

Keep a pure core that can be tested without HA, following the pattern of the sibling
integrations.

```text
custom_components/check_my_bill/
  __init__.py        setup entry, register static path + extra JS, WS commands, services
  const.py
  config_flow.py     ConfigFlow + OptionsFlow (§3.1, §3.3)
  energy_prefs.py    read Energy prefs → candidate stat ids; detect Opower entry & sensors
  coordinator.py     DataUpdateCoordinator (hourly): current-cycle compute, rollover, Store
  store.py           CycleStore wrapper around helpers.storage.Store (versioned, migrate)
  stats.py           recorder access: hourly change series for a set of stat ids
  sensor.py
  websocket.py
  services.yaml, strings.json, translations/en.json
  frontend/check-my-bill-card.js
  core/              # pure Python, no homeassistant imports
    tariff.py        Tariff, TariffVersion, cost(kwh, start, end) with proration & tiers
    cycles.py        boundary math: from sensor dates, from cycle_day, backfill estimates
    reconcile.py     band(metered, netted, export, mode, tol) → status; dollar check
    compute.py       hourly series → CycleTotals (metered, export, netted, coverage)
```

`tests/` covers `core/` (the pure unit tests). `tests_ha/` covers config flow, coordinator,
Store and WebSocket, using `pytest-homeassistant-custom-component`.

## 5. Privacy

- **Public repo.** No account numbers, entity IDs, utility names tied to the author or
  meter values from the author's instance go into committed files. Fixtures use synthetic
  IDs (`opower:test_elec_0000_energy_consumption`).
- **Snapshots stay out of git.** `tools/snapshot.py` writes to the gitignored `snapshots/`.
  `tools/sanitize.py` maps real IDs to synthetic ones, scales or jitters values when asked,
  and writes to `tests_ha/fixtures/`. The diff is reviewed before any commit.
- **Diagnostics** (`diagnostics.py`) redact statistic IDs that contain account-like digit
  runs.
- **Nothing leaves HA.** There are no network calls at all.

## 6. Testing without a dev instance

1. **Pure core** (`tests/`): tariff proration, tiers, cycle boundary math across DST and
   month ends, reconciliation bands, and netting. Synthetic hourly series reproduce the
   §2.3 pattern: the meter lands between gross and netted.
2. **Snapshot tool** (`tools/snapshot.py`, reads `.env` `HA_URL`/`HA_TOKEN`):
   - Pulls `energy/get_prefs`, the Opower states and entity-registry rows, and 90 days of
     hourly statistics for the grid and Opower stat IDs.
   - Writes JSON to `snapshots/`.
   - `tools/sanitize.py` produces the fixtures described in §5.
3. **HA-layer tests** (`tests_ha/`):
   - Seed the recorder with `async_import_statistics` and `async_add_external_statistics`
     from the sanitized fixtures.
   - Seed fake Opower entities: registry entries with `disabled_by=integration`, plus
     states.
   - Seed Energy prefs via `EnergyManager.async_update`.
   - Then exercise the config flow (pre-fill, Skip, enable sensors), rollover (advance the
     start-date sensor state, then assert that the Store has a closed record and that it
     re-finalizes over 3 days), the WebSocket commands, and the repair issue.
4. **Visual check of the card:**
   - Run a throwaway `hass -c ./scratch_config` from `.venv`, gitignored.
   - `tools/seed_dev.py` imports the same fixtures over WebSocket (`recorder/import_statistics`)
     and creates input_datetime stand-ins for the cycle dates.
   - The user does not need to configure any feeder integration.
5. **Production smoke test, read-only:** install from a branch with HACS. The integration
   writes nothing outside its own Store. The only external mutation is the opt-in enabling
   of the Opower date sensors.

CI (the existing workflow) runs ruff, `tests/`, `tests_ha/`, hassfest and HACS validation.

## 7. Phasing (one PR each, targeting `dev`)

1. **Core:** `core/*` with unit tests. No HA code.
2. **Statistics and Store:** `stats.py`, `store.py`, `coordinator.py`, `sensor.py`, plus
   the snapshot and sanitize tools and fixtures.
3. **Config and options flow:** includes Opower detection and sensor enabling.
4. **Interfaces:** WebSocket API, services and repairs.
5. **Card:** `cycles` mode, then `follow_energy`.
6. **Release:** backfill, diagnostics, README, then `v0.1.0`.

## 8. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | Private Energy collection API changes | isolated accessor; `cycles` mode never depends on it |
| R2 | Opower renames stats or sensors after re-auth | repair issue plus a reconfigure step |
| R3 | Backfilled cycle boundaries are wrong by days | marked "est.", editable, and the reconciliation band absorbs about 1 day |
| R4 | Utility lag makes the current cycle look like a mismatch | coverage < 100% → `in_progress`, never `mismatch` |
| R5 | Tariff entry errors make expected $ wrong | the card shows the cost breakdown per line, so a bad rate is easy to spot; status is based on kWh only, never on $ |
| R6 | Users with a utility other than Opower | manual cycle mode; the utility statistic can be any statistic |

## 9. Scope

**In v1:**

- One electric account per config entry.
- Flat or seasonal rates, optional tiers, fixed charges, riders, tax.
- Three export modes.
- Cycle history, backfill (up to 12 cycles, as far as data exists), the card, sensors.

**Out of v1:**

- Gas, which has the same shape and comes later through a `commodity` field.
- Time-of-use rates.
- Net-metering credit carry-over.
- Demand charges.
- Entering actual bill amounts or tracking bills (the integration is informational).
- Importing rate schedules (OpenEI/URDB).
- Reading actual bill totals from Opower. That would need an upstream change to expose
  `AggregateType.BILL` reads.
- Injecting into the built-in Energy panel, which is not possible (§2.4).

## 10. Questions and answers

| # | Question | Answer |
|---|---|---|
| Q1 | How deep is validation? | kWh plus full tariff (user) |
| Q2 | Where do cycle dates come from? | Offer to enable Opower's date sensors; keep a history at each rollover (user) |
| Q3 | Gas? | Electric only for v1 (user) |
| Q4 | Repo visibility? | Public, for GitHub-hosted CI (user) |
| Q5 | Export compensation? | None. Zero-export with small uncompensated spill, so `export_mode: none` (user) |
| Q6 | Backfill depth? | Up to 12 cycles, limited to the history actually available (user) |
| Q7 | Bill entry in v1? | No. The integration is informational, not a bill tracker (user) |
| Q8 | Default tolerance? | 2%. The CTs are revenue grade, so measured and billed values should agree closely (user) |
