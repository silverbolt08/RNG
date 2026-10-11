# Data card (Annex D)

## 1. Name and version

| | Primary | Holdout |
|---|---|---|
| Dataset ID | `primary-s7-r150` | `holdout-s4201-r220` |
| Location | `data/primary/` | `data/holdout/` |
| Generated | 2026-10-11 08:48 UTC | 2026-10-11 08:48 UTC |
| Generator | `rng/data/generator.py` | same script, different profile |
| Role | Development, demo, evaluation | Held back; evaluation only |

The team generated both datasets. There is no organiser data pack and no real
company data anywhere in this repository.

## 2. How it was made

Rows are produced by a seeded simulation script, not by asking a language
model for data.

```
python -m rng.data.generator --profile primary --out data/primary
python -m rng.data.generator --profile holdout --out data/holdout
```

| Setting | Primary | Holdout |
|---|---|---|
| Seed | 7 | 4201 |
| Target rise in the bill (`--target-rise`) | 15% | 22% |
| Rise achieved | 15.00% | 22.00% |

The script simulates a factory from physical drivers, at 15-minute resolution:

- **Furnace:** units produced x energy intensity per product (1.0, 2.3 and
  6.4 kWh per unit for the three product families), plus standby.
- **Production lines:** energy per unit, plus standby, following the shift
  pattern.
- **HVAC:** a piecewise response to temperature, with a steeper slope above
  27 C. This is deliberately something a linear baseline cannot represent.
- **Chillers, compressors, lighting:** shift occupancy and temperature, plus a
  leak that starts on a known date.
- **Main meter:** measured independently of the sub-meters, with its own
  noise and 18 kW of unmetered base load.

Production volume is the free variable: the script solves it by bisection so
the total rise lands on the target. The target is a parameter, so other
scenarios (8%, 22%) come from the same script.

**Ground truth.** `ground_truth.json` holds the true split of the rise. It is
the same 16-subset Shapley counterfactual the analyser runs, evaluated on the
simulation's physical model instead of a fitted one. The analyser never reads
it and never imports the generator; `tests/test_no_generator_import.py` fails
the build if it does. Only `rng/eval` may read it.

**Prompts.** The generator script was written with an AI coding assistant.
_The prompts used are to be added here by the script's author before
submission._

## 3. Contents

Both datasets cover 1 April to 30 September 2026 (2026-Q2 and 2026-Q3), one
site, in UTC.

| File | Rows (primary / holdout) | Columns |
|---|---|---|
| `meter_15min.csv` | 157,968 / 158,052 | `meter_id`, `ts` (UTC), `kwh` |
| `tariff.csv` | 6 / 6 | `valid_from`, `valid_to`, `period`, `price_per_kwh`, plus `peak_start_hour`, `peak_end_hour` |
| `production.csv` | 549 / 549 | `date`, `product`, `units`, `shift_pattern`, `is_holiday` |
| `weather.csv` | 183 / 183 | `date`, `avg_temp_c`, `cooling_degree_days`, `heating_degree_days` |
| `dataset_meta.json` | - | Emission factor, currency, peak hours, tolerance. The analyser may read this. |
| `ground_truth.json` | - | True split and anomaly locations. Evaluation only. |

- **Meters:** one main meter and eight sub-meters (furnace, three lines, HVAC,
  chillers, compressors, lighting): 17,568 intervals each before gaps.
- **Products:** three families.
- **Tariff:** peak and off-peak prices in INR per kWh, three validity windows.
  Annex B names the tariff period but not which hours are peak, so the
  convention (09:00 to 21:00 UTC, weekdays) is published as two extra columns
  and in `dataset_meta.json`.
- **Emission factor:** 0.71 kg CO2e per kWh, stated in `dataset_meta.json`.

True split of the rise, in percentage points of the reference bill:

| Cause | Primary | Holdout |
|---|---|---|
| Tariff | 8.72 | 6.69 |
| Volume | 0.27 | 16.97 |
| Product mix | 2.87 | -5.83 |
| Weather | 1.61 | 1.74 |
| Controllable waste | 1.52 | 2.44 |
| **Total** | **15.00** | **22.00** |

## 4. Traps injected on purpose

The six edge cases of guide section 4.4, plus the hidden waste.

| Trap | Primary | Holdout |
|---|---|---|
| Sub-meters do not sum to main | 18 kW unmetered load plus per-meter noise; gap 1.81% | same mechanism; gap 1.38% |
| Missing intervals | compressors 12 May 02:00-08:00 (24); hvac 21 Aug 12:00-18:00 (24); line_3 all of 29 Jun (96) | furnace 3 May 18:00 to 4 May 06:00 (48); main 19 Sep 09:00-12:00 (12) |
| Meter reset | line_2, 18 Jul 03:00, one negative reading | lighting, 27 Aug 23:15, one negative reading |
| Tariff change mid-quarter | 16 Aug | 3 Sep |
| Mix shift with no waste behind it | energy-intense product rises from 12% to 15.5% of units | mix moves the other way (27% to 21%), so the mix term is negative |
| Hot period | +3.5 C, 5 to 13 Aug | +5.2 C, 20 to 29 Jul |
| Holiday logged as a normal shift | 7 Sep, logged `2-shift`, not a holiday | 2 Sep, logged `3-shift`, not a holiday |
| Hidden waste | 15 kW leak on compressors from 3 Aug | 21 kW leak on chillers from 11 Jul |

The holdout differs in seed, target rise, leaking asset, leak size and date,
direction of the mix shift, tariff prices and gap locations, so a method tuned
to the primary set is not rewarded on it.

## 5. Checks run

`python -m rng.data.validate data/primary` runs six checks on the raw files.
Full output: `docs/validation_output.txt` (primary) and
`docs/validation_output_holdout.txt` (holdout).

| Check | Primary | Holdout |
|---|---|---|
| Sub-meters within tolerance of main (6%) | PASS, gap 1.81% | PASS, gap 1.38% |
| Negative readings and resets | WARN, 1 reset found | WARN, 1 reset found |
| Timestamps regular, 15-minute grid, one time zone | WARN, 144 intervals missing in 3 gaps | WARN, 60 intervals missing in 2 gaps |
| Tariff windows contiguous and covering | PASS | PASS |
| Production days against the shift calendar | WARN, 7 Sep flagged | WARN, 2 Sep flagged |
| Row counts and Annex B columns | PASS | PASS |

A WARN is a trap from section 4 being found, which is the intended result. The
validator works from the files alone; `tests/test_validate.py` then confirms
that what it reports matches `ground_truth.json` exactly, on both datasets.

`tests/test_generator.py` also checks that the true split adds up to the total
change, that every declared trap is really in the data, and that the holdout
is not a rerun of the primary.

## 6. Known limits

- One site, two quarters, three product families.
- The cooling response is piecewise in temperature and the analyser's baseline
  is linear, so part of the weather effect lands in the residual. A small
  error on weather and waste is expected and is reported, not hidden.
- Sub-meters are measured independently of the main meter and do not
  reconcile exactly.
- All clock hours (shifts, peak window, night load) are UTC; no site-local
  time is modelled.
- Fix costs and the share of waste that is recoverable are not in the data.
  They are the team's assumptions, held in `rng/config.py` and stated in every
  response.
- The truth is defined by the same counterfactual construction the analyser
  uses. It has to be, or the comparison would mean nothing; what differs is
  the model inside it.
- `ground_truth.json` was corrected once, on 11 October, after comparison with
  the analyser exposed a fault in the generator: its counterfactuals priced a
  whole quarter at one tariff window, so the mid-quarter price rise was
  counted under volume instead of tariff. The four CSV files did not change
  and no measured value moved; only the true split between tariff and volume
  did. The figures in section 3 are the corrected ones.

## 7. Privacy

Entirely synthetic. No real company data, no personal data, and nothing
derived from either.
