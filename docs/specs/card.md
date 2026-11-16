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

- Label, dates (`start` – `last_day`, an "est." tag when `estimated`) and the status pill.
- While `in_progress`: a progress bar, "Day N of M" and the days left.
- With a tariff: the projected bill (`projected_cost.total`) as the main figure and the
  expected cost so far (`expected_cost.total`) beside it. Both are hidden when
  `has_tariff` is false.
- Two bars compare HA kWh (`metered_kwh`) with utility kWh (`utility_kwh`), scaled to the
  larger. Below 100% utility coverage the utility bar shows the coverage %, and an open
  cycle adds the note "utility data lags 1–2 days".

### Previous cycles

A list, newest first, `cycles` rows. Each row has a status dot, the dates (with "est."),
a line with the status label and both kWh totals, and, with a tariff, the expected cost
with the utility's billed cost (`bill.cost`) under it when known. A chevron shows the
row's state. Clicking a row (or Enter/Space on it) expands it:

- cost breakdown: supply, delivery, riders, fixed, tax, export credit (when nonzero),
  total, and "Utility billed" when `bill.cost` is known;
- the HA/utility kWh bars;
- reconciliation: a scale with the expected utility band `low`–`high` kWh shaded and a
  marker, in the status colour, at the utility's reading; the caption gives the band, the
  tolerance and the deviation % when outside it;
- export kWh (HA and utility) when the export mode is not `none`.

On cards at least 480px wide, the breakdown and the reconciliation sit side by side
(container query).

### Status pill

The pill is a tinted badge with a dot; the row dot uses the same colour.

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
