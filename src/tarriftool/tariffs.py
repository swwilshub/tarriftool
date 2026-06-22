"""
Tariff definitions and pricing.

Each tariff exposes:
    name                   str
    import_price_series(idx) -> ndarray of p/kWh
    standing_p_per_day     float
    export_p_per_kwh       float

Two flavours:
    StaticTariff   fixed standard rate + time-of-use windows (Go, Cosy, IOG).
    AgileTariff    half-hourly time-varying rate (synthetic UK Agile proxy).

Builtin numbers are reasonable mid-2024 fallbacks. The Octopus API adapter
(adapters/octopus.py) can override these with live rates for the relevant
GSP region; see `tarriftool fetch-rates`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Iterable

import numpy as np
import pandas as pd


class Tariff(Protocol):
    name: str
    standing_p_per_day: float
    export_p_per_kwh: float
    def import_price_series(self, idx: pd.DatetimeIndex) -> np.ndarray: ...


# ---------------------------------------------------------------------------

@dataclass
class TariffWindow:
    """A time-of-day price override window."""
    start_h: float  # 0..24, inclusive
    end_h: float    # 0..24, exclusive (may be < start_h to wrap midnight)
    price_p: float
    # If non-None, restrict to these weekdays (0=Mon).
    weekdays: tuple[int, ...] | None = None


@dataclass
class StaticTariff:
    name: str
    standard_price_p: float
    windows: list[TariffWindow] = field(default_factory=list)
    standing_p_per_day: float = 50.0
    export_p_per_kwh: float = 15.0

    def import_price_series(self, idx: pd.DatetimeIndex) -> np.ndarray:
        prices = np.full(len(idx), self.standard_price_p, dtype=float)
        hod = idx.hour.to_numpy() + idx.minute.to_numpy() / 60.0
        dow = idx.dayofweek.to_numpy()
        for w in self.windows:
            if w.start_h <= w.end_h:
                mask = (hod >= w.start_h) & (hod < w.end_h)
            else:
                mask = (hod >= w.start_h) | (hod < w.end_h)
            if w.weekdays is not None:
                mask = mask & np.isin(dow, list(w.weekdays))
            prices = np.where(mask, w.price_p, prices)
        return prices


@dataclass
class AgileTariff:
    """Synthetic Agile proxy: sinusoidal half-hourly variation around a mean.

    The Octopus Agile adapter can drop a real half-hourly array in via
    `LiveTariff` below.  This is the offline fallback so the model runs with
    no network access.
    """
    name: str = "Octopus Agile (synthetic)"
    mean_p: float = 22.0
    peak_amp_p: float = 14.0
    winter_premium_p: float = 4.0
    standing_p_per_day: float = 50.0
    export_p_per_kwh: float = 15.0

    def import_price_series(self, idx: pd.DatetimeIndex) -> np.ndarray:
        hod = idx.hour.to_numpy() + idx.minute.to_numpy() / 60.0
        doy = idx.dayofyear.to_numpy()
        base = self.mean_p + self.peak_amp_p * np.cos(2 * np.pi * (hod - 17.5) / 24.0)
        seasonal = self.winter_premium_p * np.cos(2 * np.pi * (doy - 15) / 365.0)
        return base + seasonal


@dataclass
class LiveTariff:
    """A tariff whose half-hourly prices are pre-computed (e.g. fetched from
    Octopus). Wraps an array aligned to a known index.
    """
    name: str
    prices_p: np.ndarray
    index: pd.DatetimeIndex
    standing_p_per_day: float = 50.0
    export_p_per_kwh: float = 15.0

    def import_price_series(self, idx: pd.DatetimeIndex) -> np.ndarray:
        # Reindex to match incoming idx (forward-fill within the day).
        s = pd.Series(self.prices_p, index=self.index)
        return s.reindex(idx, method="ffill").to_numpy()


# ---------------------------------------------------------------------------
# Builtin Octopus tariffs (approximate June 2024 South-East rates).
# Replace with live values via the API adapter for accurate £ figures.

def builtin_tariffs() -> dict[str, Tariff]:
    tariffs: dict[str, Tariff] = {}

    # Intelligent Octopus Go — 7p 23:30..05:30, rest ~25p.
    tariffs["intelligent_go"] = StaticTariff(
        name="Intelligent Octopus Go",
        standard_price_p=25.0,
        windows=[TariffWindow(23.5, 5.5, 7.0)],
        standing_p_per_day=50.0,
        export_p_per_kwh=15.0,
    )

    # Octopus Go — 8.5p 00:30..05:30, rest ~28p.
    tariffs["go"] = StaticTariff(
        name="Octopus Go",
        standard_price_p=28.0,
        windows=[TariffWindow(0.5, 5.5, 8.5)],
        standing_p_per_day=50.0,
        export_p_per_kwh=15.0,
    )

    # Cosy Octopus — cheap 12p in three windows, peak 39p 16-19, else ~25p.
    tariffs["cosy"] = StaticTariff(
        name="Cosy Octopus",
        standard_price_p=25.0,
        windows=[
            TariffWindow(4.0, 7.0, 12.0),
            TariffWindow(13.0, 16.0, 12.0),
            TariffWindow(22.0, 24.0, 12.0),
            TariffWindow(16.0, 19.0, 39.0),  # peak
        ],
        standing_p_per_day=50.0,
        export_p_per_kwh=15.0,
    )

    # Octopus Agile synthetic.
    tariffs["agile"] = AgileTariff()

    # A flat-rate baseline for comparison.
    tariffs["flat"] = StaticTariff(
        name="Flat 24p",
        standard_price_p=24.0,
        windows=[],
        standing_p_per_day=50.0,
        export_p_per_kwh=15.0,
    )

    return tariffs


# ---------------------------------------------------------------------------
# Seasonal helpers

UK_SEASONS = {
    "winter": {12, 1, 2},
    "spring": {3, 4, 5},
    "summer": {6, 7, 8},
    "autumn": {9, 10, 11},
}


def season_of(month: int) -> str:
    for s, ms in UK_SEASONS.items():
        if month in ms:
            return s
    return "unknown"


def tariff_summary(tariffs: Iterable[Tariff]) -> pd.DataFrame:
    rows = []
    for t in tariffs:
        rows.append({
            "name": t.name,
            "standing_p_per_day": t.standing_p_per_day,
            "export_p_per_kwh": t.export_p_per_kwh,
        })
    return pd.DataFrame(rows)
