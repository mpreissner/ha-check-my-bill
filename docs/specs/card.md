# Card (phase 5)

Implements `bill-validation.md` §3.7. This file settles the details that section leaves
open. Where the two disagree, this one wins, and the deviation is listed in §7.

## 1. Modules

- `custom_components/check_my_bill/frontend/check-my-bill-card.js`: the card, its editor
  and the `window.customCards` entry. One file, no build step, no imports.
- `custom_components/check_my_bill/card.py`: `async_register_card(hass)`, called from
  `async_setup`:
  - serves the `frontend/` directory at `/check_my_bill_static` with
    `hass.http.async_register_static_paths([StaticPathConfig(..., cache_headers=True)])`;
  - when the `frontend` component is loaded, calls
    `frontend.add_extra_js_url(hass, "/check_my_bill_static/check-my-bill-card.js?v=<version>")`,
    with the version from the manifest, so a release busts the browser cache.
- `manifest.json`: `http` joins `dependencies`; `frontend` goes in `after_dependencies`.
  Every HA install with a UI loads `frontend`, so the card is registered there. Making it a
  hard dependency would only break setups (and the test harness) that run without the
  frontend package.

## 2. Config

```yaml
type: custom:check-my-bill-card
entry_id: 01J...        # optional when exactly one Check My Bill entry exists
mode: cycles            # cycles | follow_energy
cycles: 6               # past cycles listed, 1-24
title: Electric bill    # optional
collection_key: energy_my_dashboard   # optional, follow_energy only
```

- Without `entry_id` the card asks `config_entries/get` for the domain's entries and uses
  the only one. With none or several it shows a message asking for `entry_id`.
- The visual editor is `ha-form` with a `config_entry` selector (`integration:
  check_my_bill`), a mode select, a number for `cycles` and a text for `title`.
- `getStubConfig` returns `{mode: "cycles", cycles: 6}`.

## 3. Data

- `check_my_bill/cycles` on first render, when the config changes, every 5 minutes while
  connected, and when the tab becomes visible again. The coordinator refreshes hourly, so
  this is plenty.
- `check_my_bill/range` in `follow_energy` mode, each time the selected range changes.
- A failed call shows the error message in the card instead of the content.

## 4. Content

### Current cycle

- Dates (`start` – `last_day`, "est." when `estimated`), "day N of M".
- HA kWh (`metered_kwh`), utility kWh (`utility_kwh`) with its coverage %. Below 100%
  utility coverage, a note: "utility data lags 1–2 days".
- Expected cost to date (`expected_cost.total`) and projected (`projected_cost.total`),
  both hidden when `has_tariff` is false.
- Status pill.

### Previous cycles

A table, newest first, `cycles` rows: dates (with "est."), HA kWh, utility kWh, expected
$ (column hidden without a tariff) and status. Clicking a row (or Enter on it) expands:

- cost breakdown: supply, delivery, riders, fixed, tax, export credit (when nonzero),
  total;
- reconciliation: expected utility band `low`–`high` kWh at the configured tolerance and
  the deviation % when outside it;
- export kWh (HA and utility) when the export mode is not `none`.

### Status pill

| status | label | colour |
|---|---|---|
| ok | OK | `--success-color` |
| warn | Check | `--warning-color` |
| mismatch | Mismatch | `--error-color` |
| no_data | No utility data | `--disabled-text-color` |
| in_progress | In progress | `--info-color` |

Numbers use `Intl.NumberFormat` with the user's language; costs use the `currency` from
the response.

## 5. `follow_energy` mode

- One function, `energyCollection(hass, key)`, touches the private API:
  `hass.connection["_" + key]` when `collection_key` is set, otherwise
  `"_energy_" + <first path segment>` and then `"_energy"`. It returns null on any
  failure.
- The Energy cards create the collection when they load, possibly after this card. The
  card retries the lookup every second for 10 seconds before falling back.
- With a collection, the card subscribes and maps `{start, end}` to dates: `start`'s
  local date, and the exclusive end is the local date after `end` (the picker's `end` is
  the last moment of the last day; when missing, tomorrow). It then renders one "Selected
  range" block in the current-cycle layout, with no projection, followed by the cycle
  table.
- Ranges over 400 days show a note instead of a summary.
- Without a collection it renders `cycles` mode with the note "Energy date picker not
  found; showing billing cycles."
- The subscription is released when the card disconnects.

## 6. Tests

`tests_ha/test_card.py`:
- After setup, `GET /check_my_bill_static/check-my-bill-card.js` returns 200 with the
  script.
- With the `frontend` component marked loaded, the versioned URL is registered through
  `add_extra_js_url`; without it, it is not, and setup still succeeds.

The JavaScript has no automated tests (no build or JS toolchain in the repo). It is checked
with `node --check` and by hand against a dev instance (`bill-validation.md` §6.4).

## 7. Deviations from `bill-validation.md`

- **No lit.** The card is a plain `HTMLElement` that renders escaped template strings.
  Reaching into HA's bundle for lit (`customElements.get("hui-...")` prototypes) is itself a
  private API; the card is small enough to do without it.
- **`frontend` is an after-dependency**, not a dependency (§1).
- **Static path `/check_my_bill_static`**, so it cannot collide with an HTTP view the
  integration may add under `/api/check_my_bill`.
- **`title` and `collection_key` options** added.
