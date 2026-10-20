"""把本地 QuantData 主库（fact/master schema）桥接成 QuantWeave 后端期望的 market_data schema。

背景
----
QuantWeave 后端（quant_service + backtest_lab）固定读取 ``data/market_data.duckdb``
里的 ``daily_bar`` / ``instrument_master`` 等表。本地 QuantData 主库是另一套命名
（``fact.price_bar`` / ``master.instrument``），两者不能直接互换。

本脚本生成 ``data_local/market_data.duckdb``：把主库的 raw 日线重映射成后端 schema。
源库以 READ ONLY 方式 ATTACH，绝不写入、不修改。

口径映射（重要，均已在 README 中记录）
--------------------------------------
1. ``bar_date`` -> ``date``；``adjustment='raw'`` 只取原始价（后端 full_market 也标注
   ``price_adjustment: unadjusted``，口径一致）。
2. ``volume``：主库单位是 **share（股）**，后端沿用服务器库口径 **手（lot=100股）**，
   故除以 100。
3. ``amount``：主库 price_bar **没有成交额字段**，用典型价近似
   ``amount = volume(股) × (high+low+close)/3``。与真实 VWAP 的偏差通常在 1% 以内，
   只影响流动性/换手类排序，不影响价格与收益计算。这是唯一一处派生字段。
4. ``asset_type`` / ``code`` / ``exchange`` / ``name`` 直接取自主库 master.instrument；
   ``market`` 后端实际未参与查询，按 1=SH / 0=SZ / 2=BJ 填充占位。
5. 周/月/季/年线、板块、概念、数据集元信息等后端有引用但主库没有的表，
   用服务器库的 DDL 建空表（``SELECT * ... LIMIT 0``），保证不会因缺表报错。

用法
----
.. code-block:: bash

    .venv/bin/python scripts/build_local_dataset.py            # 默认源与输出
    .venv/bin/python scripts/build_local_dataset.py --start 2015-01-01
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
# 源库（个人主数据底座）是私有数据，路径不写死在仓库里。
# 用环境变量 QW_SOURCE_DB 或命令行 --source 传入。
_env_source = os.environ.get("QW_SOURCE_DB", "").strip()
DEFAULT_SOURCE = Path(_env_source) if _env_source else None
DEFAULT_SERVER = ROOT / "data" / "market_data.duckdb"
DEFAULT_OUT = ROOT / "data_local"

# 主库没有、后端会引用的表：用服务器库 DDL 建空表
EMPTY_TABLES = [
    "weekly_bar",
    "monthly_bar",
    "quarterly_bar",
    "yearly_bar",
    "sector_master",
    "sector_member",
    "concept_daily_bar",
    "concept_period_bar",
    "dataset_meta",
    "dataset_change",
    "sync_state",
]


def build(
    source: Path,
    server: Path,
    out_dir: Path,
    start: str | None,
    end: str | None,
    with_valuation: bool = False,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "market_data.duckdb"
    if target.exists():
        target.unlink()

    con = duckdb.connect(str(target))
    con.execute(f"ATTACH '{source}' AS localdb (READ_ONLY)")
    server_attached = server.exists()
    if server_attached:
        con.execute(f"ATTACH '{server}' AS serverdb (READ_ONLY)")

    where = ["adjustment = 'raw'", "bar_kind = 'daily'"]
    params: list[str] = []
    if start:
        where.append("bar_date >= ?")
        params.append(start)
    if end:
        where.append("bar_date <= ?")
        params.append(end)
    clause = " AND ".join(where)

    t0 = time.time()
    con.execute(
        f"""
        CREATE TABLE daily_bar AS
        SELECT
            bar_date                       AS date,
            instrument_id,
            open,
            high,
            low,
            close,
            volume / 100.0                 AS volume,   -- 股 -> 手
            volume * ((high + low + close) / 3.0) AS amount  -- 派生：典型价近似
        FROM localdb.fact.price_bar
        WHERE {clause}
        """,
        params,
    )
    print(f"[daily_bar] 写入完成 {time.time() - t0:.1f}s")

    con.execute(
        """
        CREATE TABLE instrument_master AS
        SELECT
            instrument_id,
            CASE upper(coalesce(exchange, ''))
                WHEN 'SH' THEN 1
                WHEN 'SZ' THEN 0
                WHEN 'BJ' THEN 2
                ELSE -1
            END                                    AS market,
            code,
            name,
            coalesce(asset_type, 'stock')          AS asset_type,
            upper(coalesce(exchange, ''))          AS exchange,
            'local-quantdata'                      AS source,
            now()                                  AS updated_at
        FROM localdb.master.instrument
        """
    )
    print("[instrument_master] 写入完成")

    made_empty: list[str] = []
    if server_attached:
        server_tables = {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE database_name = 'serverdb'"
            ).fetchall()
        }
        for table in EMPTY_TABLES:
            if table in server_tables:
                con.execute(f"CREATE TABLE {table} AS SELECT * FROM serverdb.{table} LIMIT 0")
                made_empty.append(table)
    print(f"[空表] {len(made_empty)} 张：{', '.join(made_empty)}")

    # 周/月/季/年线：主库没有，直接从 daily_bar 聚合派生（后端 K 线周期切换要用）
    for name, unit in (("weekly_bar", "week"), ("monthly_bar", "month"), ("quarterly_bar", "quarter"), ("yearly_bar", "year")):
        t1 = time.time()
        con.execute(
            f"""
            CREATE TABLE {name} AS
            SELECT
                min(date)                        AS period_start,
                instrument_id,
                arg_min(open, date)              AS open,
                arg_max(close, date)             AS close,
                max(high)                        AS high,
                min(low)                         AS low,
                sum(volume)                      AS volume,
                sum(amount)                      AS amount,
                count(*)                         AS trading_days,
                max(date)                        AS period_end
            FROM daily_bar
            GROUP BY instrument_id, date_trunc('{unit}', date)
            """
        )
        print(f"[{name}] 聚合完成 {time.time() - t1:.1f}s")

    valuation_stats: dict | None = None
    if with_valuation:
        t2 = time.time()
        # 源库是长表（一行一个指标），转成「一行一天四列」的宽表，方便因子直接取
        # 注意：后端现有 54 个因子块全部是价量类，这张表暂时没有消费方，
        # 入库目的是让「估值 + 反转」这类新因子有数据可用。
        mv_where = []
        if start:
            mv_where.append(f"asof_date >= DATE '{start}'")
        if end:
            mv_where.append(f"asof_date <= DATE '{end}'")
        mv_clause = ("WHERE " + " AND ".join(mv_where)) if mv_where else ""
        con.execute(
            f"""
            CREATE TABLE market_value_daily AS
            SELECT
                instrument_id,
                asof_date                                        AS date,
                max(CASE WHEN value_type = 'total_mv' THEN value END) AS total_mv,
                max(CASE WHEN value_type = 'float_mv' THEN value END) AS float_mv,
                max(CASE WHEN value_type = 'pb'       THEN value END) AS pb,
                max(CASE WHEN value_type = 'pe_ttm'   THEN value END) AS pe_ttm
            FROM localdb.fact.market_value
            {mv_clause}
            GROUP BY 1, 2
            """
        )
        n = con.execute("SELECT count(*) FROM market_value_daily").fetchone()[0]
        valuation_stats = {"rows": n, "seconds": round(time.time() - t2, 1)}
        print(f"[market_value_daily] 估值宽表 {n:,} 行 {time.time() - t2:.1f}s")

    con.execute("CREATE INDEX IF NOT EXISTS daily_bar_pk ON daily_bar (instrument_id, date)")
    con.execute("CREATE INDEX IF NOT EXISTS instrument_master_pk ON instrument_master (instrument_id)")

    con.execute("DETACH serverdb") if server_attached else None
    con.execute("DETACH localdb")

    stats = con.execute(
        """
        SELECT count(*) AS rows,
               count(DISTINCT instrument_id) AS instruments,
               min(date) AS first_date,
               max(date) AS last_date
        FROM daily_bar
        """
    ).fetchone()
    con.close()

    return {
        "target": str(target),
        "target_mb": round(target.stat().st_size / 1024 / 1024, 1),
        "daily_bar_rows": stats[0],
        "instruments": stats[1],
        "first_date": str(stats[2]),
        "last_date": str(stats[3]),
        "empty_tables": made_empty,
        "valuation": valuation_stats,
        "source": str(source),
        "derived_fields": {"volume": "share/100 -> lot", "amount": "volume*typical_price"},
        "build_seconds": round(time.time() - t0, 1),
    }


def link_companions(server_dir: Path, out_dir: Path) -> list[str]:
    """分钟线/复权缓存/特征库主库没有，从服务器拷贝取用。

    小文件（<200MB）直接复制，避免本地库目录里的写入污染服务器副本；
    大文件（feature_store 2.5G）保持软链，只读使用。
    """
    linked: list[str] = []
    for name in ("minute_bars.duckdb", "adjusted_kline_cache.duckdb", "feature_store.duckdb"):
        src = server_dir / name
        dst = out_dir / name
        if not src.exists():
            continue
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        if src.stat().st_size < 200 * 1024 * 1024:
            shutil.copy2(src, dst)
            linked.append(f"{name}(copy)")
        else:
            dst.symlink_to(src)
            linked.append(f"{name}(symlink)")
    return linked


def main() -> None:
    parser = argparse.ArgumentParser(description="构建本地主库 -> QuantWeave market_data 桥接库")
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        required=DEFAULT_SOURCE is None,
        help="源库路径（个人主数据底座）。也可用环境变量 QW_SOURCE_DB 指定",
    )
    parser.add_argument("--server", type=Path, default=DEFAULT_SERVER)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--start", default=None, help="起始日期，默认全量")
    parser.add_argument("--end", default=None)
    parser.add_argument("--no-companions", action="store_true", help="只生成 market_data.duckdb，不复制分钟线/复权缓存")
    parser.add_argument("--with-valuation", action="store_true", help="额外把源库估值长表转成 market_value_daily 宽表")
    args = parser.parse_args()

    if not args.source.exists():
        raise SystemExit(f"找不到本地主库：{args.source}")

    report = build(
        args.source, args.server, args.out, args.start, args.end, with_valuation=args.with_valuation
    )
    report["linked"] = [] if args.no_companions else link_companions(args.server.parent, args.out)
    (args.out / "build_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
