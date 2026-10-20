#!/usr/bin/env bash
# 单容器内同时拉起 FastAPI(:8767) 与 Next.js(:3100)，任一退出则整体退出
set -euo pipefail

cd /app
export QW_DATA_DIR="${QW_DATA_DIR:-/app/data}"
export RUIPAN_BACKTEST_API_PORT="${RUIPAN_BACKTEST_API_PORT:-8767}"
export PORT="${PORT:-3100}"
export HOSTNAME="0.0.0.0"

echo "──────────────────────────────────────────────────"
echo " QuantWeave 全栈回测台（Docker 单镜像）"
echo " 数据目录: $QW_DATA_DIR"
echo " 后端接口: 127.0.0.1:$RUIPAN_BACKTEST_API_PORT（仅容器内，前端经 /api 代理）"
echo " 访问地址: http://localhost:$PORT/backtest"
echo "──────────────────────────────────────────────────"

# 防呆：数据文件必须在
if [[ ! -f "$QW_DATA_DIR/market_data.duckdb" ]]; then
  echo "✗ 找不到 $QW_DATA_DIR/market_data.duckdb" >&2
  echo "  镜像内已内置数据；若你用 -v 挂载覆盖了 /app/data，请确认宿主机目录里有 market_data.duckdb" >&2
  exit 1
fi

python -m uvicorn quant_service.app:app \
  --host 127.0.0.1 --port "$RUIPAN_BACKTEST_API_PORT" --workers 1 &
API_PID=$!

# 等后端就绪（用 python 探活，基础镜像里没有 curl）
for _ in $(seq 1 60); do
  if python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:${RUIPAN_BACKTEST_API_PORT}/health',timeout=2).read()" >/dev/null 2>&1
  then break; fi
  sleep 1
done
if ! python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:${RUIPAN_BACKTEST_API_PORT}/health',timeout=2).read()" >/dev/null 2>&1
then
  echo "✗ 后端启动失败" >&2
  kill "$API_PID" 2>/dev/null || true
  exit 1
fi
echo "✓ 后端就绪"

node server.js &
WEB_PID=$!
echo "✓ 前端启动中，稍候打开 http://localhost:$PORT/backtest"
echo "  前端源码: /app/frontend-src　后端源码: /app/quant_service /app/backtest_lab"
echo "  使用说明: /app/使用说明.md"

# 任一进程退出就收摊，避免半死状态
wait -n "$API_PID" "$WEB_PID"
echo "! 有进程退出，容器结束" >&2
kill "$API_PID" "$WEB_PID" 2>/dev/null || true
exit 1
