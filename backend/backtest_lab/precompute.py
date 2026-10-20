"""Versioned, incremental feature precomputation for QuantWeave.

The materialized table deliberately reuses the Lego SQL compiler.  It is an
acceleration layer, never an alternative source of factor definitions.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import duckdb
import pandas as pd

from .config import FEATURE_DB, MARKET_DB, PRECOMPUTE_WORK_DIR
from .platform_lock import FileLock


FEATURE_SET = "core-stock-v1"
FEATURE_SCHEMA_VERSION = 2
FEATURE_CALCULATOR_VERSION = "core-v2"
MARKET_SCHEMA_VERSION = 1
MAX_CHANGE_VERSIONS = 400

CANONICAL_REQUIREMENTS: dict[str, Any] = {
    "momentum": [2, 5, 10, 20, 60, 120],
    "mas": [(5, 20), (10, 60)],
    "ma_periods": [5, 10, 14, 20, 60, 120],
    "rsis": [14],
    "vols": [20, 60],
    "liquidities": [20, 60],
    "drawdowns": [60],
    "turtles": [20, 60],
    "rolling": [5, 10, 14, 20, 60, 120],
    "ema_periods": [],
    "ma_lags": [(20, 1), (20, 5), (60, 1), (60, 5)],
    "obv_windows": [],
    "recursive_indicators": False,
    "singles": [],
    "longest": 120,
}

MATERIALIZED_COLUMNS = [
    "date", "instrument_id", "code", "name", "exchange",
    "open", "high", "low", "close", "volume", "amount", "first_date",
    "prev_close", "prev_volume", "daily_return", "volume_change",
    "typical_price", "true_range", "listing_days",
    "lag_close_2", "lag_close_5", "lag_close_10", "lag_close_20",
    "lag_close_60", "lag_close_120",
    "ma_5", "ma_10", "ma_20", "ma_60", "ma_120", "vma_20",
    "adv_20", "adv_60",
    "max_close_20", "min_close_20", "max_high_20", "min_low_20",
    "max_close_60", "min_close_60", "max_high_60", "min_low_60",
    "std_close_20", "vol_20", "vol_60", "atr_14", "atr_20", "rsi_14",
    "avg_range_20", "downside_vol_20", "path_length_20",
    "peak_60", "prior_high_20", "prior_high_60",
]


def _now() -> datetime:
    return datetime.now().astimezone().replace(tzinfo=None)


def _status_path() -> Path:
    return PRECOMPUTE_WORK_DIR / "status.json"


def _write_status(payload: dict[str, Any]) -> None:
    PRECOMPUTE_WORK_DIR.mkdir(parents=True, exist_ok=True)
    path = _status_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def ensure_market_metadata(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Create and return the transactionally maintained market version row."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS dataset_meta (
               id VARCHAR PRIMARY KEY,
               dataset_version BIGINT NOT NULL,
               schema_version INTEGER NOT NULL,
               max_trade_date DATE,
               row_count BIGINT NOT NULL,
               updated_at TIMESTAMP NOT NULL
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS dataset_change (
               dataset_version BIGINT NOT NULL,
               instrument_id VARCHAR NOT NULL,
               min_changed_date DATE NOT NULL,
               max_changed_date DATE NOT NULL,
               PRIMARY KEY (dataset_version, instrument_id)
           )"""
    )
    row = connection.execute(
        "SELECT dataset_version,schema_version,max_trade_date,row_count,updated_at "
        "FROM dataset_meta WHERE id='market'"
    ).fetchone()
    if row is None:
        maximum, count = connection.execute(
            "SELECT max(date),count(*) FROM daily_bar"
        ).fetchone()
        connection.execute(
            "INSERT INTO dataset_meta VALUES ('market',1,?,?,?,?)",
            [MARKET_SCHEMA_VERSION, maximum, int(count or 0), _now()],
        )
        row = (1, MARKET_SCHEMA_VERSION, maximum, int(count or 0), _now())
    return {
        "dataset_version": int(row[0]),
        "schema_version": int(row[1]),
        "max_trade_date": row[2],
        "row_count": int(row[3]),
        "updated_at": row[4],
    }


def record_market_change(
    connection: duckdb.DuckDBPyConnection,
    incoming: pd.DataFrame,
) -> dict[str, Any]:
    """Increment the market version inside the caller's write transaction."""
    current = ensure_market_metadata(connection)
    version = int(current["dataset_version"]) + 1
    changes = (
        incoming.groupby("instrument_id", as_index=False)["date"]
        .agg(min_changed_date="min", max_changed_date="max")
    )
    changes["dataset_version"] = version
    connection.register("incoming_dataset_change", changes)
    connection.execute(
        """INSERT INTO dataset_change
           SELECT dataset_version,instrument_id,min_changed_date,max_changed_date
           FROM incoming_dataset_change"""
    )
    connection.unregister("incoming_dataset_change")
    maximum, count = connection.execute(
        "SELECT max(date),count(*) FROM daily_bar"
    ).fetchone()
    connection.execute(
        """UPDATE dataset_meta
           SET dataset_version=?,schema_version=?,max_trade_date=?,row_count=?,updated_at=?
           WHERE id='market'""",
        [version, MARKET_SCHEMA_VERSION, maximum, int(count or 0), _now()],
    )
    connection.execute(
        "DELETE FROM dataset_change WHERE dataset_version <= ?",
        [max(0, version - MAX_CHANGE_VERSIONS)],
    )
    return {
        "dataset_version": version,
        "schema_version": MARKET_SCHEMA_VERSION,
        "max_trade_date": maximum,
        "row_count": int(count or 0),
    }


def market_snapshot(market_db: Path = MARKET_DB, initialize: bool = False) -> dict[str, Any]:
    if not market_db.is_file():
        return {"status": "missing", "dataset_version": 0, "max_trade_date": None}
    if initialize:
        with duckdb.connect(str(market_db)) as connection:
            return {"status": "ready", **ensure_market_metadata(connection)}
    with duckdb.connect(str(market_db), read_only=True) as connection:
        tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
        if "dataset_meta" not in tables:
            maximum, count = connection.execute(
                "SELECT max(date),count(*) FROM daily_bar"
            ).fetchone()
            return {
                "status": "unversioned",
                "dataset_version": 0,
                "schema_version": 0,
                "max_trade_date": maximum,
                "row_count": int(count or 0),
            }
        row = connection.execute(
            "SELECT dataset_version,schema_version,max_trade_date,row_count,updated_at "
            "FROM dataset_meta WHERE id='market'"
        ).fetchone()
        if row is None:
            return {"status": "unversioned", "dataset_version": 0, "max_trade_date": None}
        return {
            "status": "ready",
            "dataset_version": int(row[0]),
            "schema_version": int(row[1]),
            "max_trade_date": row[2],
            "row_count": int(row[3]),
            "updated_at": row[4],
        }


def _feature_manifest(feature_db: Path = FEATURE_DB) -> dict[str, Any] | None:
    if not feature_db.is_file():
        return None
    try:
        with duckdb.connect(str(feature_db), read_only=True) as connection:
            tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
            if "feature_manifest" not in tables:
                return None
            row = connection.execute(
                """SELECT feature_set,schema_version,calculator_version,dataset_version,
                          history_start,max_trade_date,row_count,status,build_id,updated_at,last_error
                   FROM feature_manifest WHERE feature_set=?""",
                [FEATURE_SET],
            ).fetchone()
    except Exception as exc:
        return {"status": "error", "last_error": str(exc)}
    if row is None:
        return None
    keys = [
        "feature_set", "schema_version", "calculator_version", "dataset_version",
        "history_start", "max_trade_date", "row_count", "status", "build_id",
        "updated_at", "last_error",
    ]
    return dict(zip(keys, row))


def precompute_status(
    market_db: Path = MARKET_DB,
    feature_db: Path = FEATURE_DB,
) -> dict[str, Any]:
    market = market_snapshot(market_db)
    feature = _feature_manifest(feature_db)
    state: dict[str, Any] = {}
    try:
        state = json.loads(_status_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    if feature is None:
        status = "missing"
    elif feature.get("status") == "error":
        status = "error"
    elif (
        int(feature.get("schema_version") or 0) != FEATURE_SCHEMA_VERSION
        or str(feature.get("calculator_version") or "") != FEATURE_CALCULATOR_VERSION
    ):
        status = "incompatible"
    elif int(feature.get("dataset_version") or -1) != int(market.get("dataset_version") or 0):
        status = "stale"
    else:
        status = str(feature.get("status") or "ready")
    return {
        "status": status,
        "dataset_version": int(market.get("dataset_version") or 0),
        "feature_dataset_version": int((feature or {}).get("dataset_version") or 0),
        "schema_version": int((feature or {}).get("schema_version") or FEATURE_SCHEMA_VERSION),
        "calculator_version": (feature or {}).get("calculator_version", FEATURE_CALCULATOR_VERSION),
        "history_start": (feature or {}).get("history_start"),
        "max_trade_date": (feature or {}).get("max_trade_date"),
        "row_count": int((feature or {}).get("row_count") or 0),
        "fallback_enabled": True,
        "last_started_at": state.get("last_started_at"),
        "last_finished_at": state.get("last_finished_at"),
        "last_error": state.get("last_error") or (feature or {}).get("last_error"),
        "feature_db": str(feature_db),
    }


def requirements_supported(requirements: dict[str, Any]) -> bool:
    if requirements.get("recursive_indicators") or requirements.get("singles"):
        return False
    for key in (
        "momentum", "ma_periods", "rsis", "vols", "liquidities", "drawdowns",
        "turtles", "rolling", "ema_periods", "ma_lags", "obv_windows",
    ):
        requested = {tuple(item) if isinstance(item, (list, tuple)) else item for item in requirements.get(key, [])}
        available = {tuple(item) if isinstance(item, (list, tuple)) else item for item in CANONICAL_REQUIREMENTS.get(key, [])}
        if not requested.issubset(available):
            return False
    return True


def attach_precomputed_features(
    connection: duckdb.DuckDBPyConnection,
    requirements: dict[str, Any],
    start: date,
    end: date,
    scope: str,
    asset_type: str,
    columns: Iterable[str] | None = None,
    feature_db: Path = FEATURE_DB,
) -> tuple[bool, str | None]:
    if os.environ.get("QUANTWEAVE_PRECOMPUTE_ENABLED", "1") == "0":
        return False, "disabled"
    if scope != "all" or asset_type != "stock":
        return False, "unsupported-universe"
    if not requirements_supported(requirements):
        return False, "unsupported-requirements"
    status = precompute_status(feature_db=feature_db)
    if status["status"] != "ready":
        return False, f"feature-store-{status['status']}"
    history_start = pd.Timestamp(status["history_start"]).date()
    maximum = pd.Timestamp(status["max_trade_date"]).date()
    if start < history_start or end > maximum:
        return False, "outside-feature-range"
    escaped = str(feature_db).replace("'", "''")
    try:
        connection.execute(f"ATTACH '{escaped}' AS feature_cache (READ_ONLY)")
        available = {
            str(row[0])
            for row in connection.execute(
                "DESCRIBE feature_cache.daily_core_features"
            ).fetchall()
        }
        requested = list(dict.fromkeys(columns or sorted(available)))
        missing = [column for column in requested if column not in available]
        if missing:
            return False, "missing-feature-columns:" + ",".join(missing[:10])
        projection = ",".join(f'"{column}"' for column in requested)
        connection.execute(
            f"""CREATE TEMP TABLE lego_features AS
                SELECT {projection} FROM feature_cache.daily_core_features
                WHERE date BETWEEN ? AND ?""",
            [start, end],
        )
        count = connection.execute("SELECT count(*) FROM lego_features").fetchone()[0]
        if not count:
            connection.execute("DROP TABLE lego_features")
            return False, "empty-feature-range"
        return True, None
    except Exception as exc:
        try:
            connection.execute("DROP TABLE IF EXISTS lego_features")
        except Exception:
            pass
        return False, f"feature-read-error:{exc}"


def _years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _setup_feature_connection(connection: duckdb.DuckDBPyConnection, market_db: Path) -> None:
    threads = max(1, min(8, int(os.environ.get("QUANTWEAVE_DUCKDB_THREADS", "2"))))
    connection.execute(f"SET threads={threads}")
    escaped = str(market_db).replace("'", "''")
    connection.execute(f"ATTACH '{escaped}' AS market (READ_ONLY)")
    connection.execute("CREATE TEMP VIEW daily_bar AS SELECT * FROM market.daily_bar")
    connection.execute("CREATE TEMP VIEW instrument_master AS SELECT * FROM market.instrument_master")


def _create_manifest_table(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """CREATE TABLE feature_manifest (
               feature_set VARCHAR PRIMARY KEY,
               schema_version INTEGER NOT NULL,
               calculator_version VARCHAR NOT NULL,
               dataset_version BIGINT NOT NULL,
               history_start DATE NOT NULL,
               max_trade_date DATE NOT NULL,
               row_count BIGINT NOT NULL,
               status VARCHAR NOT NULL,
               build_id VARCHAR NOT NULL,
               updated_at TIMESTAMP NOT NULL,
               last_error VARCHAR
           )"""
    )


def build_features(
    market_db: Path = MARKET_DB,
    feature_db: Path = FEATURE_DB,
    history_years: int | None = None,
) -> dict[str, Any]:
    """Atomically build the canonical stock feature store."""
    started = time.perf_counter()
    started_at = datetime.now().astimezone()
    years = int(history_years or os.environ.get("QUANTWEAVE_PRECOMPUTE_HISTORY_YEARS", "10"))
    snapshot = market_snapshot(market_db, initialize=True)
    maximum = snapshot.get("max_trade_date")
    if maximum is None:
        raise ValueError("行情库没有日线数据")
    history_start = _years_before(pd.Timestamp(maximum).date(), years)
    warmup_start = history_start - timedelta(days=max(400, CANONICAL_REQUIREMENTS["longest"] * 5))
    build_id = uuid.uuid4().hex
    feature_db.parent.mkdir(parents=True, exist_ok=True)
    temporary = feature_db.with_name(f"{feature_db.stem}.build-{build_id}.duckdb")
    lock = FileLock(PRECOMPUTE_WORK_DIR / "feature-build.lock")
    _write_status({"status": "building", "last_started_at": started_at.isoformat(timespec="seconds"), "last_error": None})
    try:
        with lock:
            from .lego_pipeline import _feature_sql

            with duckdb.connect(str(temporary)) as connection:
                _setup_feature_connection(connection, market_db)
                feature_sql = _feature_sql(
                    "m.exchange IN ('SH','SZ','BJ')",
                    CANONICAL_REQUIREMENTS,
                    "m.exchange IN ('SH','SZ','BJ')",
                    "stock",
                )
                projection = ",".join(f'"{column}"' for column in MATERIALIZED_COLUMNS)
                connection.execute(
                    "CREATE TABLE daily_core_features AS "
                    f"SELECT {projection} FROM ({feature_sql}) source WHERE date>=?",
                    [pd.Timestamp(warmup_start).date(), pd.Timestamp(maximum).date(), history_start],
                )
                connection.execute(
                    "CREATE UNIQUE INDEX daily_core_features_pk "
                    "ON daily_core_features(date,instrument_id)"
                )
                _create_manifest_table(connection)
                row_count, minimum, feature_max = connection.execute(
                    "SELECT count(*),min(date),max(date) FROM daily_core_features"
                ).fetchone()
                connection.execute(
                    "INSERT INTO feature_manifest VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        FEATURE_SET, FEATURE_SCHEMA_VERSION, FEATURE_CALCULATOR_VERSION,
                        snapshot["dataset_version"], minimum, feature_max, int(row_count),
                        "ready", build_id, _now(), None,
                    ],
                )
                connection.execute("CHECKPOINT")
            if feature_db.exists():
                backup = feature_db.with_suffix(".previous.duckdb")
                if backup.exists():
                    backup.unlink()
                feature_db.replace(backup)
            temporary.replace(feature_db)
            if "backup" in locals() and backup.exists():
                backup.unlink()
        payload = {
            "ok": True,
            "status": "ready",
            "mode": "build",
            "dataset_version": snapshot["dataset_version"],
            "history_start": history_start,
            "max_trade_date": maximum,
            "row_count": int(row_count),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "last_started_at": started_at.isoformat(timespec="seconds"),
            "last_finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "last_error": None,
        }
        _write_status(payload)
        return payload
    except Exception as exc:
        if temporary.exists():
            temporary.unlink()
        payload = {
            "ok": False,
            "status": "error",
            "mode": "build",
            "last_started_at": started_at.isoformat(timespec="seconds"),
            "last_finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "last_error": str(exc),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
        _write_status(payload)
        raise


def update_features(
    market_db: Path = MARKET_DB,
    feature_db: Path = FEATURE_DB,
) -> dict[str, Any]:
    """Incrementally rebuild changed instruments, or bootstrap with a full build."""
    started = time.perf_counter()
    started_at = datetime.now().astimezone()
    snapshot = market_snapshot(market_db, initialize=True)
    manifest = _feature_manifest(feature_db)
    if not manifest:
        return build_features(market_db, feature_db)
    if (
        int(manifest.get("schema_version") or 0) != FEATURE_SCHEMA_VERSION
        or str(manifest.get("calculator_version") or "") != FEATURE_CALCULATOR_VERSION
    ):
        return build_features(market_db, feature_db)
    feature_version = int(manifest.get("dataset_version") or 0)
    target_version = int(snapshot["dataset_version"])
    if feature_version == target_version:
        return {
            "ok": True, "status": "ready", "mode": "update", "updated_rows": 0,
            "dataset_version": target_version, "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    with duckdb.connect(str(market_db), read_only=True) as market:
        tables = {row[0] for row in market.execute("SHOW TABLES").fetchall()}
        if "dataset_change" not in tables:
            return build_features(market_db, feature_db)
        earliest = market.execute("SELECT min(dataset_version) FROM dataset_change").fetchone()[0]
        if earliest is None or feature_version < int(earliest) - 1:
            return build_features(market_db, feature_db)
        changes = market.execute(
            """SELECT instrument_id,min(min_changed_date) AS min_changed_date
               FROM dataset_change WHERE dataset_version>? AND dataset_version<=?
               GROUP BY instrument_id ORDER BY instrument_id""",
            [feature_version, target_version],
        ).fetchall()
    if not changes:
        return build_features(market_db, feature_db)
    identifiers = [str(row[0]) for row in changes]
    recompute_start = min(pd.Timestamp(row[1]).date() for row in changes)
    history_start = pd.Timestamp(manifest["history_start"]).date()
    recompute_start = max(recompute_start, history_start)
    warmup_start = recompute_start - timedelta(days=max(400, CANONICAL_REQUIREMENTS["longest"] * 5))
    maximum = pd.Timestamp(snapshot["max_trade_date"]).date()
    lock = FileLock(PRECOMPUTE_WORK_DIR / "feature-build.lock")
    _write_status({"status": "building", "last_started_at": started_at.isoformat(timespec="seconds"), "last_error": None})
    try:
        with lock, duckdb.connect(str(feature_db)) as connection:
            from .lego_pipeline import _feature_sql

            _setup_feature_connection(connection, market_db)
            sql = _feature_sql(
                "m.instrument_id IN (SELECT * FROM unnest(?))",
                CANONICAL_REQUIREMENTS,
                "m.exchange IN ('SH','SZ','BJ')",
                "stock",
            )
            connection.execute("BEGIN TRANSACTION")
            try:
                projection = ",".join(f'"{column}"' for column in MATERIALIZED_COLUMNS)
                connection.execute(
                    "CREATE TEMP TABLE rebuilt_features AS "
                    f"SELECT {projection} FROM ({sql}) source WHERE date>=?",
                    [identifiers, pd.Timestamp(warmup_start).date(), maximum, recompute_start],
                )
                connection.execute(
                    "DELETE FROM daily_core_features WHERE instrument_id IN (SELECT * FROM unnest(?)) AND date>=?",
                    [identifiers, recompute_start],
                )
                connection.execute("INSERT INTO daily_core_features SELECT * FROM rebuilt_features")
                row_count, minimum, feature_max = connection.execute(
                    "SELECT count(*),min(date),max(date) FROM daily_core_features"
                ).fetchone()
                connection.execute(
                    """UPDATE feature_manifest SET dataset_version=?,history_start=?,max_trade_date=?,
                              row_count=?,status='ready',build_id=?,updated_at=?,last_error=NULL
                       WHERE feature_set=?""",
                    [target_version, minimum, feature_max, int(row_count), uuid.uuid4().hex, _now(), FEATURE_SET],
                )
                updated_rows = connection.execute("SELECT count(*) FROM rebuilt_features").fetchone()[0]
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        payload = {
            "ok": True, "status": "ready", "mode": "update",
            "dataset_version": target_version, "updated_instruments": len(identifiers),
            "updated_rows": int(updated_rows), "recompute_start": recompute_start,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "last_started_at": started_at.isoformat(timespec="seconds"),
            "last_finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "last_error": None,
        }
        _write_status(payload)
        return payload
    except Exception as exc:
        payload = {
            "ok": False, "status": "error", "mode": "update",
            "last_started_at": started_at.isoformat(timespec="seconds"),
            "last_finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "last_error": str(exc), "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
        _write_status(payload)
        raise


def verify_features(
    market_db: Path = MARKET_DB,
    feature_db: Path = FEATURE_DB,
) -> dict[str, Any]:
    status = precompute_status(market_db, feature_db)
    checks: list[dict[str, Any]] = []
    ok = status["status"] == "ready"
    if feature_db.is_file():
        try:
            with duckdb.connect(str(feature_db), read_only=True) as connection:
                duplicates = connection.execute(
                    """SELECT count(*) FROM (
                           SELECT date,instrument_id,count(*) n FROM daily_core_features
                           GROUP BY date,instrument_id HAVING n>1
                       )"""
                ).fetchone()[0]
                null_keys = connection.execute(
                    "SELECT count(*) FROM daily_core_features WHERE date IS NULL OR instrument_id IS NULL"
                ).fetchone()[0]
            checks.extend([
                {"name": "unique_keys", "ok": duplicates == 0, "value": int(duplicates)},
                {"name": "non_null_keys", "ok": null_keys == 0, "value": int(null_keys)},
            ])
            ok = ok and all(item["ok"] for item in checks)
        except Exception as exc:
            checks.append({"name": "readable", "ok": False, "error": str(exc)})
            ok = False
    return {"ok": ok, "status": status, "checks": checks}
