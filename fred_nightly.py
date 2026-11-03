#!/usr/bin/env python3
"""
Nightly rate pull for the Yield Curve Lab.

- Pulls Fed + Treasury constant-maturity series from FRED.
- Pulls the Treasury's own par yield curve (the primary source for the curve fit).
- Saves each run as its own dated snapshot (nothing earlier is overwritten).
- Rebuilds a master file from the snapshots, so any past day can be recreated.
- Logs every run and exits non-zero if something is missing, so a scheduler can alert you.

Setup:
    pip install fredapi pandas requests
    export FRED_API_KEY="your_key"      # free at fred.stlouisfed.org

Run manually:   python fred_nightly.py
Run on a date:  python fred_nightly.py --asof 2026-10-30
"""
import argparse
import io
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from fredapi import Fred

DATA_DIR = Path(os.environ.get("YCL_DATA_DIR", "data"))
SNAP_DIR = DATA_DIR / "snapshots"
LOG_FILE = DATA_DIR / "pipeline.log"

FRED_SERIES = {
    # policy rates
    "EFFR": "effective_fed_funds",
    "DFEDTARU": "fed_target_upper",
    "DFEDTARL": "fed_target_lower",
    "SOFR": "sofr",
    # constant-maturity Treasury yields (cross-check for the Treasury pull)
    "DGS1MO": "cmt_1m",
    "DGS3MO": "cmt_3m",
    "DGS6MO": "cmt_6m",
    "DGS1": "cmt_1y",
    "DGS2": "cmt_2y",
    "DGS5": "cmt_5y",
    "DGS10": "cmt_10y",
    "DGS30": "cmt_30y",
}

TREASURY_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
    "?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
)

LOOKBACK_DAYS = 30  # re-pull a window each night so late revisions/gaps get picked up


def setup_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
    )


def last_business_day(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def pull_fred(asof: date) -> pd.DataFrame:
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise RuntimeError("FRED_API_KEY is not set")
    fred = Fred(api_key=key)
    start = asof - timedelta(days=LOOKBACK_DAYS)
    cols = {}
    for sid, name in FRED_SERIES.items():
        s = fred.get_series(sid, observation_start=start, observation_end=asof)
        cols[name] = s
    df = pd.DataFrame(cols)
    df.index.name = "date"
    return df


def pull_treasury(asof: date) -> pd.DataFrame:
    resp = requests.get(TREASURY_URL.format(year=asof.year), timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()
    df.index.name = "date"
    df.columns = ["par_" + c.lower().replace(" ", "") for c in df.columns]
    return df[df.index >= pd.Timestamp(asof - timedelta(days=LOOKBACK_DAYS))]


def save_snapshot(asof: date, fred_df: pd.DataFrame, tsy_df: pd.DataFrame):
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    # same asof date -> same file, so re-running a day is safe (idempotent)
    fred_df.to_csv(SNAP_DIR / f"{asof.isoformat()}_fred.csv")
    tsy_df.to_csv(SNAP_DIR / f"{asof.isoformat()}_treasury.csv")


def rebuild_master():
    """Master = every snapshot stacked, newest snapshot wins for any given date."""
    for kind in ("fred", "treasury"):
        files = sorted(SNAP_DIR.glob(f"*_{kind}.csv"))
        if not files:
            continue
        frames = [pd.read_csv(f, parse_dates=["date"], index_col="date") for f in files]
        master = pd.concat(frames)
        master = master[~master.index.duplicated(keep="last")].sort_index()
        master.to_csv(DATA_DIR / f"master_{kind}.csv")


def check_missing(asof: date, fred_df: pd.DataFrame, tsy_df: pd.DataFrame) -> list:
    problems = []
    expected = pd.Timestamp(last_business_day(asof))
    if tsy_df.empty or tsy_df.index.max() < expected:
        problems.append(
            f"Treasury curve missing {expected.date()} "
            f"(latest: {tsy_df.index.max().date() if not tsy_df.empty else 'none'}). "
            "Could be a holiday or a late file."
        )
    # FRED posts Treasury yields about a day late, so only require the prior business day
    fred_expected = pd.Timestamp(last_business_day(asof - timedelta(days=1)))
    for col in fred_df.columns:
        s = fred_df[col].dropna()
        if s.empty or s.index.max() < fred_expected - timedelta(days=3):
            problems.append(f"FRED series {col} looks stale (latest: {s.index.max() if not s.empty else 'none'})")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", help="YYYY-MM-DD, defaults to today")
    args = ap.parse_args()
    asof = datetime.strptime(args.asof, "%Y-%m-%d").date() if args.asof else date.today()

    setup_logging()
    logging.info("Run started for %s", asof)
    try:
        fred_df = pull_fred(asof)
        tsy_df = pull_treasury(asof)
        save_snapshot(asof, fred_df, tsy_df)
        rebuild_master()
    except Exception:
        logging.exception("Pipeline failed")
        sys.exit(2)

    problems = check_missing(asof, fred_df, tsy_df)
    for p in problems:
        logging.warning(p)
    logging.info("Run finished: %d FRED rows, %d Treasury rows, %d warnings",
                 len(fred_df), len(tsy_df), len(problems))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
