# Check My Bill for Home Assistant

Checks your electricity or gas bill against what your own meters recorded.

Check My Bill reads the grid statistics already set up on the Energy dashboard, and, if you
use the Opower integration, what your utility reports. For the current and past billing
cycles it shows:

- the kWh your meter recorded next to the kWh the utility reported, and whether they agree
  within a tolerance;
- what the bill should cost at your rates;
- a projection of the current cycle's cost.

> [!WARNING]
> **Beta.** This is a pre-release (`0.1.0b5`). Expect rough edges, and check the numbers
> against a real bill before relying on them. Please report problems in
> [Issues](https://github.com/mpreissner/ha-check-my-bill/issues).

## Requirements

- Home Assistant 2026.8 or newer.
- Grid consumption configured on the Energy dashboard.
- Optional: the [Opower](https://www.home-assistant.io/integrations/opower/) integration
  for your utility. Without it, cycles are still costed, but there is nothing to check
  them against.

## Installation (HACS)

1. In HACS, open the ⋮ menu → **Custom repositories**.
2. Add `https://github.com/mpreissner/ha-check-my-bill` with the type **Integration**.
3. Open **Check My Bill** in HACS and download it. Only pre-releases exist so far: if HACS
   offers no version, or not the newest one, turn on **Show beta versions** for the
   repository (⋮ → **Redownload**) and pick it there.
4. Restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration → Check My Bill**.

To install by hand, copy `custom_components/check_my_bill` into your configuration's
`custom_components/` folder and restart.

## Setup

The setup steps are:

1. **Meter.** Electric or gas. The next page lists only the statistics that fit it.
2. **Grid statistics.** These are the import and export statistics, filled in from the
   Energy dashboard. Only energy statistics are listed.
3. **Utility account.** Pick the Opower account to compare against, or **Don't use utility
   data**. With Opower, the billing dates come from the utility.
4. **Billing cycle.** Asked only when no utility dates are available. Enter the day of the
   month your meter is read, or the end date of your last bill.
5. **Rates.** Supply and delivery per kWh, fixed charges, tax, and how exported energy is
   billed. Leave the rates empty to compare kWh only.

Under **Configure** on the integration you can change:

- the rates, including versions that start on a later date;
- tiers, seasonal rates and riders, edited as YAML;
- the tolerance;
- how exported energy is billed: not billed, net metering, or a separate credit rate.

Choose **Reconfigure** to pick different statistics. That rebuilds the cycle history.

### Gas

Add a second entry and choose **Gas** on the first page. The next page lists only statistics
in a volume (ft³, m³, CCF) or therms.

- **Unit.** Setup asks which unit your bill charges by: therms, CCF or m³. Quantities,
  rates and sensors all use that unit.
- **CCF and therms.** A CCF counts as one therm, as most US utilities bill. A meter in ft³
  or m³ works with a bill in therms through that rate.
- **Opower's units.** Opower labels all gas data CCF, even when the utility reports therms.
  Setup reads the real unit from your account and lets you correct it.
- **Comparison by day.** Gas is compared by whole day, because utilities report gas usage
  by day. The current cycle runs to the end of yesterday.
- **Defaults.** Gas has no export, and its default tolerance is 5%.

## What you get

### Card

The card is added to the card picker automatically, with no resource to register:

```yaml
type: custom:check-my-bill-card
mode: cycles          # cycles | follow_energy (follows the Energy dashboard's date picker)
cycles: 6             # past cycles to list, 1-24
title: Electric bill  # optional
entry_id: ...         # only needed with more than one Check My Bill entry
```

It shows:

- **Current cycle:** your kWh, the utility's kWh, the cost so far and the projected cost.
- **Past cycles:** a table with a status for each cycle. Click a row to see its cost
  breakdown and the kWh range the utility should be within.

### Sensors

| Sensor | |
|---|---|
| Cycle grid import | kWh imported this cycle |
| Cycle expected cost | Cost so far this cycle |
| Cycle projected cost | Projected cost for the whole cycle |
| Cycle status | `ok`, `warn`, `mismatch`, `no_data` or `in_progress` |
| Cycle start / Cycle end | The current cycle's dates |
| Last cycle expected cost | What the last closed bill should have been |

### Past cycles

- **Opower bill dates:** with Opower, past cycles use your actual bill dates and the
  billed kWh and cost, refreshed once a day.
- **Estimated cycles:** otherwise, cycles before installation are estimated by stepping
  back whole months, and are marked "est.".
- **Correcting a cycle:** fix an estimated cycle with the **Set cycle dates** action.
- **Late data:** if the utility imports data late, **Recompute** reads the statistics
  again.

### Repairs

You get a repair issue when:

- the utility starts recording export while export billing is set to "not billed";
- a configured statistic disappears, for example after Opower is re-authenticated;
- a configured statistic changes its unit.

## Known limitations

- Reading past bills from Opower uses the Opower integration's internals. If a Home
  Assistant update changes them, Check My Bill falls back to estimated dates and logs a
  warning.
- Opower's billed cost may leave out taxes and fees, depending on the utility.
- Electricity only, for now.
- No diagnostics download yet.

## Development

The design is in [docs/specs/](docs/specs/).

Unit tests:

```sh
pytest -q tests
```

Home Assistant tests (needs `pip install --group ha`):

```sh
pytest -q -o pythonpath=. tests_ha
```
