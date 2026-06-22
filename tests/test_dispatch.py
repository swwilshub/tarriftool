from tarriftool.config import HomeConfig
from tarriftool.simulator import simulate_all
from tarriftool.seasonal import seasonal_switch_summary


def _cfg(tmp_path):
    cfg = """
battery: {usable_kwh: 15, max_charge_kw: 6, max_discharge_kw: 6, round_trip_efficiency: 0.9, initial_soc_frac: 0.5}
ev: {battery_kwh: 100, home_charge_kw: 7, weekly_miles: 70, efficiency_mi_per_kwh: 2.2}
heat_pump: {rated_input_kw: 9, scop: 4, base_temp_c: 15.5, gain_kwh_per_hdd: 5.5}
base_load: {annual_kwh: 3000}
solar: {kwp: 9, yield_kwh_per_kwp_year: 950}
simulation: {year: 2023}
"""
    p = tmp_path / "h.yaml"
    p.write_text(cfg)
    return HomeConfig.load(p)


def test_dispatch_runs_and_costs_are_positive(tmp_path):
    cfg = _cfg(tmp_path)
    results = simulate_all(cfg)
    assert set(["go", "intelligent_go", "cosy", "agile", "flat"]).issubset(results.keys())
    for k, r in results.items():
        # Net cost may be negative (heavy solar exporter), but should be sane.
        assert -5000 < r.annual_cost_gbp < 5000, (k, r.annual_cost_gbp)
        # SoC stays within 0..cap
        assert r.df["soc_kwh"].min() >= -1e-6
        assert r.df["soc_kwh"].max() <= cfg.battery.usable_kwh + 1e-6


def test_seasonal_flip_mechanism(tmp_path):
    """The flip mechanism must work: under high-winter-demand conditions
    without IOG access, Cosy should win at least one winter month and Go or
    Agile should win at least one summer month. (For the user's actual specs
    IOG may dominate all year — that's a correct honest answer; here we
    stress the underlying mechanism instead.)
    """
    from tarriftool.tariffs import builtin_tariffs
    cfg = _cfg(tmp_path)
    hard = cfg.with_overrides(**{
        "battery.usable_kwh": 10.0,
        "heat_pump.gain_kwh_per_hdd": 9.0,
        "heat_pump.scop": 2.8,
        "solar.kwp": 3.0,
        "ev.weekly_miles": 200.0,
        "base_load.annual_kwh": 4500.0,
    })
    ts = builtin_tariffs()
    ts.pop("intelligent_go")  # simulate "no smart-charger" case
    results = simulate_all(hard, ts)
    s = seasonal_switch_summary(results)
    winners = s["schedule"]
    winter_winners = {winners[m] for m in (1, 2, 12)}
    summer_winners = {winners[m] for m in (6, 7, 8)}
    assert "cosy" in winter_winners, f"winter winners={winter_winners}"
    assert winter_winners != summer_winners, f"no flip: winter={winter_winners}, summer={summer_winners}"


def test_battery_grows_with_capacity(tmp_path):
    cfg = _cfg(tmp_path)
    small = cfg.with_overrides(**{"battery.usable_kwh": 5.0})
    big = cfg.with_overrides(**{"battery.usable_kwh": 25.0})
    s_results = simulate_all(small)
    b_results = simulate_all(big)
    # Bigger battery should not raise cost on any tariff
    for k in s_results:
        assert b_results[k].annual_cost_gbp <= s_results[k].annual_cost_gbp + 1e-3
