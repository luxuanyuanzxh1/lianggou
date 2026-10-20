"""整套系统冒烟测试：把后端每条关键链路都打一遍，输出通过/失败表。

用法::

    .venv/bin/python scripts/smoke_test.py                 # 打 8767 上的服务
    .venv/bin/python scripts/smoke_test.py --port 8768     # 打另一个实例（换数据目录后验证）

判定口径
--------
- PASS   : HTTP 200 且返回体符合预期结构
- WARN   : HTTP 200 但数据为空 / 缺依赖（能跑但功能降级）
- FAIL   : 非 200 或结构不符
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

ROOT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(base: str, method: str, path: str, payload: dict | None = None, timeout: int = 600):
    # 中文查询参数必须转义，否则 urllib 直接抛 UnicodeEncodeError
    safe_path = urllib.parse.quote(path, safe="/?&=%:#")
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(
        base + safe_path,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    t0 = time.time()
    try:
        with ROOT_OPENER.open(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, body, time.time() - t0
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), time.time() - t0
    except Exception as exc:  # 连接失败等
        return 0, str(exc).encode(), time.time() - t0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--web-port", type=int, default=3100)
    parser.add_argument("--skip-slow", action="store_true", help="跳过 3 年全市场回测")
    parser.add_argument("--api-only", action="store_true", help="只测后端，不测前端页面")
    args = parser.parse_args()
    api = f"http://127.0.0.1:{args.port}"
    web = f"http://127.0.0.1:{args.web_port}"

    rows: list[tuple[str, str, str]] = []

    def add(name: str, verdict: str, detail: str) -> None:
        rows.append((name, verdict, detail))

    # 1. 健康
    st, body, dt = call(api, "GET", "/health")
    ok = st == 200 and b'"ok":true' in body
    add("后端 /health", "PASS" if ok else "FAIL", f"{st} · {dt*1000:.0f}ms")

    # 2. 策略目录
    st, body, dt = call(api, "GET", "/api/v1/lego/catalog")
    cat = json.loads(body) if st == 200 else {}
    n_blocks = len(cat.get("catalog", {}))
    presets = list((cat.get("presets") or {}).keys())
    add("策略目录 /lego/catalog", "PASS" if st == 200 and n_blocks else "FAIL",
        f"{st} · {n_blocks} 个积木块 · 预设 {presets}")

    pipeline = dict((cat.get("presets") or {}).get("standard_multifactor") or {})

    # 3. 管线校验
    if pipeline:
        st, body, dt = call(api, "POST", "/api/v1/lego/validate", {"pipeline": pipeline})
        add("管线校验 /lego/validate", "PASS" if st == 200 else "FAIL", f"{st} · {dt*1000:.0f}ms")
    else:
        add("管线校验 /lego/validate", "FAIL", "拿不到预设，无法校验")

    # 4. 已保存管线
    st, body, dt = call(api, "GET", "/api/v1/lego/pipelines")
    n = len(json.loads(body).get("pipelines", [])) if st == 200 else 0
    add("已存管线 /lego/pipelines", "PASS" if st == 200 else "FAIL", f"{st} · {n} 条")

    # 5. 历史回测记录
    st, body, dt = call(api, "GET", "/api/v1/lego/runs")
    runs = json.loads(body).get("runs", []) if st == 200 else []
    add("回测记录 /lego/runs", "PASS" if st == 200 and runs else ("WARN" if st == 200 else "FAIL"),
        f"{st} · {len(runs)} 条")

    # 6. 回测（短区间，快速验证引擎可用）
    if pipeline:
        short = dict(pipeline, start=str(date.today() - timedelta(days=365)), end="2026-10-09")
        st, body, dt = call(api, "POST", "/api/v1/lego/backtests", {"pipeline": short}, timeout=900)
        ok = st == 200
        detail = f"{st} · {dt:.1f}s · {len(body)/1e6:.1f}MB"
        rid = None
        if ok:
            res = json.loads(body)
            rid = res["run_id"]
            detail += f" · universe {res['coverage']['universe_size']} · 成交 {res['metrics']['trades']}"
        add("单年回测 /lego/backtests", "PASS" if ok else "FAIL", detail)

        # 7. 回测结果复查
        if rid:
            st2, body2, dt2 = call(api, "GET", f"/api/v1/lego/runs/{rid}")
            add("回测落盘复查 /lego/runs/{id}", "PASS" if st2 == 200 else "FAIL", f"{st2} · {dt2*1000:.0f}ms")

    # 8. 因子实验室
    st, body, dt = call(api, "GET", "/api/v1/factor-lab/catalog")
    add("因子实验室目录", "PASS" if st == 200 else "FAIL", f"{st} · {len(body)}B")

    # 9. 策略库 / 选股器
    st, body, dt = call(api, "GET", "/api/v1/strategies")
    n = len(json.loads(body).get("strategies", [])) if st == 200 else 0
    add("策略库 /strategies", "PASS" if st == 200 else "FAIL", f"{st} · {n} 个")

    st, body, dt = call(api, "GET", "/api/v1/screeners")
    n = len(json.loads(body).get("screeners", [])) if st == 200 else 0
    add("选股器 /screeners", "PASS" if st == 200 else "FAIL", f"{st} · {n} 个")

    # 10. K 线（日/周/月 + 不复权/前复权）
    for period in ("日线", "周线", "月线"):
        st, body, dt = call(api, "GET", f"/api/v1/klines?market=1&code=600519&period={period}")
        n = len(json.loads(body).get("candles", [])) if st == 200 else 0
        add(f"K线 600519 · {period}", "PASS" if st == 200 and n else ("WARN" if st == 200 else "FAIL"),
            f"{st} · {n} 根")
    st, body, dt = call(api, "GET", "/api/v1/klines?market=1&code=600519&period=日线&adjustment=前复权")
    n = len(json.loads(body).get("candles", [])) if st == 200 else 0
    add("K线 600519 · 前复权", "PASS" if st == 200 and n else ("WARN" if st == 200 else "FAIL"),
        f"{st} · {n} 根" + ("" if n else " · 复权缓存缺失"))

    # 11. 数据调度 / 工作区
    st, body, dt = call(api, "GET", "/api/v1/data/scheduler")
    add("数据调度状态", "PASS" if st == 200 else "WARN", f"{st} · {len(body)}B")
    st, body, dt = call(api, "GET", "/api/v1/workspace/check")
    add("工作区检查", "PASS" if st == 200 else "WARN", f"{st} · {len(body)}B")

    # 12. AI 状态（只读，不发请求）
    st, body, dt = call(api, "GET", "/api/v1/ai/status")
    add("AI 网关状态", "PASS" if st == 200 else "WARN", f"{st} · {len(body)}B")

    # 13. 前端页面（/factor-lab 不是独立路由，因子实验室是 /backtest 里的一个页签）
    if not args.api_only:
        for path in ("/", "/backtest"):
            st, body, dt = call(web, "GET", path)
            add(f"前端 {path}", "PASS" if st in (200, 307) else "FAIL", f"{st} · {len(body)/1024:.0f}KB")

    # 输出
    width = max(len(r[0]) for r in rows) + 2
    print("\n" + "=" * 76)
    print(f" 冒烟测试 · API :{args.port} · Web :{args.web_port}")
    print("=" * 76)
    for name, verdict, detail in rows:
        mark = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}[verdict]
        print(f" {mark} {name:<{width}} {verdict:<5} {detail}")
    counts = {k: sum(1 for r in rows if r[1] == k) for k in ("PASS", "WARN", "FAIL")}
    print("-" * 76)
    print(f" 合计 {len(rows)} 项：通过 {counts['PASS']} · 降级 {counts['WARN']} · 失败 {counts['FAIL']}")
    print("=" * 76 + "\n")
    raise SystemExit(0 if counts["FAIL"] == 0 else 1)


if __name__ == "__main__":
    main()
