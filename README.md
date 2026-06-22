# tarriftool

A home energy tariff optimiser and what-if sandbox for Octopus customers
with solar, a home battery, an EV and an air-source heat pump.

**Interactive browser version**: `docs/index.html` — a self-contained JS
port of the model that runs entirely client-side. Push to GitHub and
enable Pages (**Settings → Pages → Branch: main, Folder: /docs**) for a
hosted version with sliders for every lever.

It answers two questions:

1. **Scenario sandbox** — *"if I add 5 kWh of battery, does that let me stay
   on Intelligent Go all year and skip the Cosy switch?"* For each lever
   (battery kWh, heat-pump demand, EV mileage, solar kWp), sweep a range
   and see how the annual bill and the winning tariff move.
2. **Seasonal swap recommender** — *"should I be on Cosy in winter and
   Intelligent Go in summer, or stick with one tariff all year?"* Tells
   you the per-month winner, the recommended switch schedule, and the £
   saved versus the best fixed-all-year tariff.

---

## The whole design in one paragraph (read this first)

Your historic smart-meter consumption is **not** your demand. It's the
*result* of your battery + EV shifting load into whichever cheap windows
your current tariff already has. Feeding it straight into a simulation of
a different tariff would compare the new tariff against a profile already
pre-optimised for the old one, which is meaningless.

So the model is split into two layers:

* **Tariff-independent demand** (`demand.py`) — what the home physically
  needs: base appliance load, heat-pump electrical demand from heating
  degree-days, EV energy from weekly miles, solar generation from a
  clear-sky model. **The same demand profile is used for every tariff
  comparison.**
* **Tariff-dependent dispatch** (`dispatch.py`) — the engine that decides
  *when* to take energy from the grid: charges the EV in the cheapest
  slots, cycles the battery into cheap windows and out during expensive
  ones, prefers solar over grid. **This is the only thing that varies by
  tariff.**

This decoupling is what makes the tariff comparison honest. The
**validation gate** (below) is what tells you the demand reconstruction
is right.

---

## Quick start

```bash
python -m pip install -e .

# Edit your assets / demand parameters first
$EDITOR config/home.yaml

# Half-hourly synthetic demand profile (zero API access needed)
python -m tarriftool.cli demand

# Annual cost on each tariff, same demand profile
python -m tarriftool.cli compare --monthly

# Seasonal switch recommendation
python -m tarriftool.cli seasonal

# Scenario sandbox: sweep a single parameter
python -m tarriftool.cli sandbox --param battery.usable_kwh

# Tornado sensitivity across battery / HP / EV / solar
python -m tarriftool.cli sandbox --plot out/tornado.png
```

---

## What the model assumes

* **Time resolution**: 30-minute settlement periods (UK half-hourly), one
  full year.
* **Heat pump**: electrical demand = `HDD × gain ÷ SCOP`, distributed over
  the day on a morning/evening shape, rate-capped by `rated_input_kw`.
  Heat-pump pre-heating into cheap windows is **not** modelled as a
  separate optimisation lever in v1; the battery absorbs cheap energy on
  its behalf, which is the dominant effect for most UK homes. Heat demand
  itself is fixed across all tariff comparisons.
* **Base load**: split between an overnight floor + morning + evening
  peaks (UK residential shape), scaled to `annual_kwh`.
* **EV**: each day's required kWh = `weekly_miles ÷ mi/kWh ÷ 7`. **When**
  that energy is taken is decided by dispatch (cheapest slots first).
* **Solar**: a UK-shaped clear-sky cosine envelope normalised to
  `kwp × yield_kwh_per_kwp_year`. Adequate for sweeps; replace with
  Enphase or PVGIS data for higher accuracy.
* **Battery**: usable kWh + max charge/discharge kW + round-trip
  efficiency (split √rte each way). Charge to capacity in any
  cheap-tier slot; discharge to cover residual load in any non-cheap-tier
  slot. Solar surplus charges battery before grid; battery never
  discharges to grid.
* **Tariffs**: built-in approximations for Intelligent Go, Octopus Go,
  Cosy Octopus, Octopus Agile (synthetic) and a flat baseline. **For
  accurate £ figures, pull live rates from the Octopus API**
  (`tarriftool fetch-rates`).

---

## Why the seasons can flip

* **Winter**: heat-pump demand pushes daily kWh above what a single
  overnight cheap window + battery cycle can store. Go's standard rate
  kicks in for many daytime kWh. Cosy's three cheap windows let the
  battery cycle 2–3× per day, soaking up enough cheap energy to cover
  the day — Cosy wins.
* **Summer**: no space heating, EV is the dominant load, fits entirely
  in one overnight Go window. Cosy's higher base rate then drags ahead.

The dispatch engine reproduces this *only* if battery state-of-charge
genuinely runs out and forces expensive imports on high-demand days. If
your config doesn't flip, that's an honest "for your specs, one tariff
dominates" — not a bug.

> For the specs supplied in this repo (15 kWh battery, SCOP 4, 9 kWp
> solar, 70 mi/wk EV) Intelligent Go wins every month — the battery is
> never starved because the home's underlying demand is modest relative
> to its assets. To see the flip mechanism, try
> `tarriftool sandbox --param heat_pump.gain_kwh_per_hdd` or override
> conditions in the sandbox.

---

## Module map

```
src/tarriftool/
  config.py          Typed loader for config/home.yaml
  demand.py          Tariff-INDEPENDENT half-hourly profile generator
  dispatch.py        Tariff-DEPENDENT greedy dispatch engine
  tariffs.py         Tariff definitions (built-in + LiveTariff wrapper)
  simulator.py       Glue: build demand once, dispatch under each tariff
  seasonal.py        Per-month winner table + switch recommender
  sandbox.py         Parameter sweeps + tornado sensitivity
  calibrate.py       Back out demand parameters from real meter data
  plotting.py        Optional matplotlib helpers
  adapters/
    octopus.py       Octopus API client (rates + consumption)
  cli.py             Argparse entry point
```

Tariffs and assets live in YAML / Python data classes so adding a new
asset or tariff is a single-file change. Secrets stay in `.env` (see
`.env.example`); nothing is hard-coded.

---

## Calibration & validation (the contamination-free path)

`tarriftool fetch-consumption` and `tarriftool fetch-rates` pull historic
half-hourly consumption and live unit rates from the Octopus API.

The historic consumption is **not fed into dispatch**. It's used by
`calibrate.estimate_demand_params()` to back-fit the synthetic demand
model's parameters (`annual_base_kwh`, `hp_gain_kwh_per_hdd`) to your
home, by regressing daily kWh against heating degree-days. The fit
intercept reveals `(base + EV)`, the slope reveals `HP gain × 1/SCOP`.

The **validation gate** (`calibrate.validation_gate`) is the moment of
truth: simulate the year using the calibrated demand + the dispatch
engine + **your old tariff's rates**, and compare to your actual bill.
If they don't match within ~5%, the demand reconstruction is wrong —
don't trust any cross-tariff comparison until it does.

Workflow:

```bash
cp .env.example .env  # fill in OCTOPUS_API_KEY, MPAN, serial
python -m tarriftool.cli fetch-consumption --days 365 --out data/consumption.csv
python -m tarriftool.cli fetch-rates --product GO-VAR-22-10-14 \
    --tariff E-1R-GO-VAR-22-10-14-A --days 365 --out data/go_rates.csv
# Update gain_kwh_per_hdd and base_load.annual_kwh in config/home.yaml
# Re-run `tarriftool compare` under your old tariff and confirm £ matches bill.
```

---

## Environment variables

Set in `.env` (never commit it):

```
OCTOPUS_API_KEY=
OCTOPUS_ACCOUNT_NUMBER=
OCTOPUS_MPAN=
OCTOPUS_SERIAL=
OCTOPUS_EXPORT_MPAN=
OCTOPUS_EXPORT_SERIAL=
```

Enphase support is stubbed for v2.

---

## Adding a tariff

Edit `tariffs.builtin_tariffs()`:

```python
tariffs["my_tariff"] = StaticTariff(
    name="My TOU",
    standard_price_p=24.0,
    windows=[
        TariffWindow(0.5, 5.5, 9.0),    # cheap overnight
        TariffWindow(16.0, 19.0, 35.0), # peak
    ],
    standing_p_per_day=52.0,
    export_p_per_kwh=15.0,
)
```

Or use `LiveTariff(prices_p, index, ...)` with a half-hourly array
fetched from the API.

---

## Adding an asset

All asset specs are dataclasses in `config.py`. Add a new dataclass +
section to `home.yaml`, then teach `demand.py` / `dispatch.py` how to
model it. The dispatch's contract is "consume kWh per slot" or "produce
kWh per slot"; everything else slots in.

---

## Tests

```bash
python -m pytest -q
```

The dispatch test asserts (i) SoC stays within bounds, (ii) the
seasonal-flip mechanism appears under high-demand conditions, and (iii)
adding battery never raises cost.
