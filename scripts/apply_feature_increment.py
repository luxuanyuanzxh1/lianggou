#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a verified feature-store increment transactionally")
    parser.add_argument("--feature-db", type=Path, required=True)
    parser.add_argument("--increment", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--backup-manifest", type=Path)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    expected_count = int(manifest["row_count"])
    with duckdb.connect(str(args.feature_db)) as connection:
        if args.backup_manifest:
            columns = [
                "feature_set", "schema_version", "calculator_version", "dataset_version",
                "history_start", "max_trade_date", "row_count", "status", "build_id",
                "updated_at", "last_error",
            ]
            current = connection.execute(
                "SELECT " + ",".join(columns) + " FROM feature_manifest WHERE feature_set='core-stock-v1'"
            ).fetchone()
            args.backup_manifest.parent.mkdir(parents=True, exist_ok=True)
            args.backup_manifest.write_text(
                json.dumps(dict(zip(columns, current)), ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        connection.execute("CREATE TEMP TABLE incoming_features AS SELECT * FROM read_parquet(?)", [str(args.increment)])
        target_columns = [row[0] for row in connection.execute("DESCRIBE daily_core_features").fetchall()]
        incoming_columns = [row[0] for row in connection.execute("DESCRIBE incoming_features").fetchall()]
        if incoming_columns != target_columns:
            raise ValueError("特征增量列与目标表不一致")
        incoming_count, duplicate_keys, null_keys = connection.execute(
            """SELECT count(*),
                      (SELECT count(*) FROM (
                           SELECT date,instrument_id,count(*) n FROM incoming_features
                           GROUP BY date,instrument_id HAVING n>1
                       )),
                      (SELECT count(*) FROM incoming_features
                       WHERE date IS NULL OR instrument_id IS NULL)
               FROM incoming_features"""
        ).fetchone()
        if incoming_count <= 0 or duplicate_keys or null_keys:
            raise ValueError("特征增量为空或包含无效键")

        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute("INSERT OR REPLACE INTO daily_core_features SELECT * FROM incoming_features")
            actual_count, minimum, maximum = connection.execute(
                "SELECT count(*),min(date),max(date) FROM daily_core_features"
            ).fetchone()
            if int(actual_count) != expected_count:
                raise ValueError(f"特征总行数不匹配: expected={expected_count}, actual={actual_count}")
            connection.execute(
                """UPDATE feature_manifest
                   SET schema_version=?,calculator_version=?,dataset_version=?,history_start=?,
                       max_trade_date=?,row_count=?,status=?,build_id=?,updated_at=?,last_error=?
                   WHERE feature_set='core-stock-v1'""",
                [
                    int(manifest["schema_version"]),
                    manifest["calculator_version"],
                    int(manifest["dataset_version"]),
                    manifest["history_start"],
                    manifest["max_trade_date"],
                    expected_count,
                    manifest["status"],
                    manifest["build_id"],
                    manifest["updated_at"],
                    manifest.get("last_error"),
                ],
            )
            connection.execute("COMMIT")
            connection.execute("CHECKPOINT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    print(json.dumps({
        "ok": True,
        "increment_rows": int(incoming_count),
        "row_count": int(actual_count),
        "history_start": str(minimum),
        "max_trade_date": str(maximum),
        "dataset_version": int(manifest["dataset_version"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
