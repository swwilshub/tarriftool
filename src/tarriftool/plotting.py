"""Optional plotting helpers. matplotlib is imported lazily so headless
environments without a display can still use the rest of the package."""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def tornado_chart(tornado_df: pd.DataFrame, out_path: str | Path) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    df = tornado_df.copy().sort_values("abs_swing_gbp")
    fig, ax = plt.subplots(figsize=(8, 0.6 * len(df) + 1.5))
    ax.barh(df["param"], df["swing_gbp"], color=["tab:red" if v < 0 else "tab:green" for v in df["swing_gbp"]])
    ax.axvline(0, color="black", lw=0.8)
    ax.set_xlabel("£ swing (high − low) using best tariff per scenario")
    ax.set_title("Annual-bill sensitivity by parameter")
    for i, (_, row) in enumerate(df.iterrows()):
        ax.text(row["swing_gbp"], i, f"  {row['low_value']}→{row['high_value']}  "
                                     f"({row['low_winner']}→{row['high_winner']})",
                va="center", fontsize=8)
    plt.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return str(out_path)


def monthly_costs_chart(monthly_df: pd.DataFrame, out_path: str | Path) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 5))
    for col in monthly_df.columns:
        ax.plot(monthly_df.index, monthly_df[col], marker="o", label=col)
    ax.set_ylabel("£ / month")
    ax.set_title("Monthly cost per tariff (same demand profile)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return str(out_path)
