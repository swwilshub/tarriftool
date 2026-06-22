"""Command-line interface."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

from .config import HomeConfig
from .demand import build_demand
from .sandbox import DEFAULT_RANGES, DEFAULT_SWEEPS, sweep_parameter, tornado
from .seasonal import seasonal_switch_summary
from .simulator import annual_costs, monthly_costs, simulate_all, spend_breakdown
from .tariffs import builtin_tariffs


def _load_cfg(path: str) -> HomeConfig:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"Config not found at {p}. Edit config/home.yaml first.")
    return HomeConfig.load(p)


def _load_env_if_present():
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
    except Exception:
        pass


def cmd_demand(args):
    cfg = _load_cfg(args.config)
    df, ev_daily, temp = build_demand(cfg)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out)
    print(f"wrote half-hourly demand: {out} ({len(df):,} rows)")
    summary = pd.Series({
        "annual_base_kwh": df["base_load_kwh"].sum(),
        "annual_hp_kwh":   df["hp_kwh"].sum(),
        "annual_solar_kwh": df["solar_kwh"].sum(),
        "annual_ev_kwh":   ev_daily.sum(),
    }).round(0)
    print("\nAnnual totals (tariff-independent):")
    print(summary.to_string())


def cmd_compare(args):
    cfg = _load_cfg(args.config)
    results = simulate_all(cfg)
    breakdown = spend_breakdown(results)
    print("\n=== Annual cost per tariff (same demand profile) ===")
    print(breakdown[["annual_cost_gbp", "import_kwh", "export_kwh"]].round(2).to_string())
    print("\n=== Spend breakdown by category (£) ===")
    print(breakdown[["spend_base_gbp", "spend_hp_gbp", "spend_ev_gbp", "battery_loss_kwh"]].round(2).to_string())
    if args.monthly:
        m = monthly_costs(results).round(2)
        print("\n=== Monthly cost (£) ===")
        print(m.to_string())


def cmd_seasonal(args):
    cfg = _load_cfg(args.config)
    results = simulate_all(cfg)
    s = seasonal_switch_summary(results)
    print("\n=== Monthly winner table (£) ===")
    print(s["monthly_table"].round(2).to_string())
    print("\n=== Seasonal switch schedule ===")
    for name, months in s["schedule_compact"]:
        ms = ",".join(_month_names(months))
        print(f"  {ms:30s} -> {name}")
    print(f"\nBest single all-year tariff: {s['best_single_tariff']} (£{s['best_single_cost_gbp']:,.2f})")
    print(f"Seasonal-switch total:       £{s['switch_total_gbp']:,.2f}")
    print(f"Saving vs best single:       £{s['saving_gbp']:,.2f}/yr")


def cmd_sandbox(args):
    cfg = _load_cfg(args.config)
    if args.param:
        values = [float(v) for v in args.values] if args.values else DEFAULT_RANGES.get(args.param)
        if not values:
            raise SystemExit(f"No values supplied and no defaults for {args.param}")
        df = sweep_parameter(cfg, args.param, values)
        print(f"\n=== Sweep: {args.param} ===")
        cost_cols = [c for c in df.columns if c.startswith("cost_")]
        print(df[["value", "winner", "best_single_tariff", "seasonal_switch_total_gbp"] + cost_cols].round(2).to_string(index=False))
        return

    # Tornado / sensitivity across multiple params
    sweeps = DEFAULT_SWEEPS
    tdf = tornado(cfg, sweeps)
    print("\n=== Sensitivity (tornado) — ranked by £ swing ===")
    print(tdf.round(2).to_string(index=False))
    if args.plot:
        from .plotting import tornado_chart
        out = tornado_chart(tdf, args.plot)
        print(f"\nwrote tornado chart: {out}")


def cmd_fetch_rates(args):
    _load_env_if_present()
    from datetime import datetime, timedelta, timezone
    from .adapters.octopus import OctopusClient

    client = OctopusClient()
    to = datetime.now(timezone.utc)
    fr = to - timedelta(days=args.days)
    rates = client.standard_unit_rates(args.product, args.tariff, fr, to)
    if rates.empty:
        print("No rates returned.")
        return
    print(rates.head().to_string())
    if args.out:
        rates.to_csv(args.out, index=False)
        print(f"wrote {args.out}")


def cmd_fetch_consumption(args):
    _load_env_if_present()
    from datetime import datetime, timedelta, timezone
    from .adapters.octopus import OctopusClient

    client = OctopusClient()
    to = datetime.now(timezone.utc)
    fr = to - timedelta(days=args.days)
    df = client.consumption(fr, to, export=args.export)
    print(f"rows: {len(df)}")
    if not df.empty:
        print(df.head().to_string())
        print(f"total kWh: {df['consumption_kwh'].sum():.1f}")
        if args.out:
            df.to_csv(args.out)
            print(f"wrote {args.out}")


def _month_names(months: list[int]) -> list[str]:
    names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    return [names[m - 1] for m in months]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tarriftool", description="Home energy tariff optimiser & what-if sandbox")
    p.add_argument("--config", default="config/home.yaml", help="path to home.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demand", help="Build synthetic demand profile and print annual totals.")
    d.add_argument("--out", default="out/demand.csv")
    d.set_defaults(func=cmd_demand)

    c = sub.add_parser("compare", help="Run all tariffs against the same demand and print costs.")
    c.add_argument("--monthly", action="store_true")
    c.set_defaults(func=cmd_compare)

    s = sub.add_parser("seasonal", help="Show the optimal seasonal tariff-switch schedule.")
    s.set_defaults(func=cmd_seasonal)

    sb = sub.add_parser("sandbox", help="Sweep a parameter (or run default sensitivity tornado).")
    sb.add_argument("--param", help="dot-keyed: battery.usable_kwh, ev.weekly_miles, solar.kwp, heat_pump.gain_kwh_per_hdd")
    sb.add_argument("--values", nargs="*", help="sweep values (defaults to built-in range for the param)")
    sb.add_argument("--plot", help="optional path for a tornado chart PNG")
    sb.set_defaults(func=cmd_sandbox)

    fr = sub.add_parser("fetch-rates", help="Pull live Octopus unit rates for a product/tariff.")
    fr.add_argument("--product", required=True, help="e.g. INTELLI-VAR-22-10-14")
    fr.add_argument("--tariff", required=True, help="e.g. E-1R-INTELLI-VAR-22-10-14-A")
    fr.add_argument("--days", type=int, default=14)
    fr.add_argument("--out")
    fr.set_defaults(func=cmd_fetch_rates)

    fc = sub.add_parser("fetch-consumption", help="Pull historic half-hourly consumption (for calibration ONLY).")
    fc.add_argument("--days", type=int, default=30)
    fc.add_argument("--export", action="store_true", help="pull export meter instead of import")
    fc.add_argument("--out")
    fc.set_defaults(func=cmd_fetch_consumption)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
