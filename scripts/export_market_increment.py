#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a small market-data increment without exposing the full database")
    parser.add_argument("--market-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--since", required=True)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.unlink(missing_ok=True)
    escaped_output = str(args.output).replace("'", "''")
    with duckdb.connect(str(args.market_db), read_only=True) as connection:
        connection.execute(
            "COPY (SELECT date,instrument_id,open,close,high,low,volume,amount "
            "FROM daily_bar WHERE date > ?) TO '" + escaped_output + "' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)",
            [args.since],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
