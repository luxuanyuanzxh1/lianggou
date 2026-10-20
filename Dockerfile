# QuantWeave 全栈单镜像
#   前端：Next.js（完整源码在 /app/frontend-src，构建产物在 /app/.next + server.js）
#   后端：FastAPI（/app/quant_service）+ Backtrader 引擎（/app/backtest_lab）
#   数据库：DuckDB（/app/data，日线 1631 万行 + 复权缓存）
#
# 构建：docker build -t quantweave:20261020 .
#   国内默认源：npmmirror + 清华 pypi
#   海外构建：docker build --build-arg NPM_SOURCE=npmjs --build-arg PIP_INDEX=https://pypi.org/simple -t quantweave:20261020 .

# ---------- 阶段 1：前端构建 ----------
FROM node:24.14.1-slim AS web
WORKDIR /build
ENV NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production

# 依赖源可切换：npmmirror（默认，国内快） / npmjs（海外）
ARG NPM_SOURCE=npmmirror

COPY frontend/package.json frontend/.npmrc ./
COPY frontend/package-lock.json ./package-lock.json
COPY frontend/package-lock.npmjs.json ./package-lock.npmjs.json

# 海外构建时换成 npmjs 官方锁文件与 registry
RUN if [ "$NPM_SOURCE" = "npmjs" ]; then \
      cp package-lock.npmjs.json package-lock.json && \
      printf 'registry=https://registry.npmjs.org/\n' > .npmrc ; \
    fi && \
    npm ci --no-audit --no-fund

COPY frontend/next.config.ts frontend/tsconfig.json \
     frontend/postcss.config.mjs frontend/next-env.d.ts ./
COPY frontend/app ./app
COPY frontend/public ./public

# 直接调 next build，绕开依赖 zsh 的 npm script
RUN npx next build

# ---------- 阶段 2：运行镜像 ----------
FROM python:3.13-slim AS runtime

LABEL org.opencontainers.image.title="QuantWeave 全栈回测台" \
      org.opencontainers.image.description="Next.js + FastAPI + DuckDB + Backtrader，开箱即用" \
      org.opencontainers.image.version="2026.10.20"

ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QW_DATA_DIR=/app/data \
    RUIPAN_BACKTEST_API_PORT=8767 \
    PORT=3100 \
    HOSTNAME=0.0.0.0 \
    NODE_ENV=production \
    TZ=Asia/Shanghai

WORKDIR /app

# Node 运行时：只需二进制（python:3.13-slim 已带 libstdc++ / libgcc）
COPY --from=web /usr/local/bin/node /usr/local/bin/node

# Python 依赖：只装回测真正用到的（不装 pytdx / scikit-learn）
COPY requirements-runtime.txt ./requirements-runtime.txt
RUN pip install --no-cache-dir -i "$PIP_INDEX" -r requirements-runtime.txt

# ---------- 后端：完整源码 ----------
COPY backend/quant_service ./quant_service
COPY backend/backtest_lab ./backtest_lab
COPY config ./config
COPY scripts/*.py ./scripts/

# ---------- 前端：运行产物 + 完整源码 ----------
COPY --from=web /build/.next/standalone ./
COPY --from=web /build/.next/static ./.next/static
COPY --from=web /build/public ./public

COPY --from=web /build/app ./frontend-src/app
COPY --from=web /build/public ./frontend-src/public
COPY --from=web /build/package.json /build/package-lock.json /build/next.config.ts \
     /build/tsconfig.json /build/postcss.config.mjs /build/next-env.d.ts \
     ./frontend-src/

# ---------- 数据库：最小执行单元 ----------
# market_data 含日线/周月季年线/估值宽表；adjusted_kline_cache 让前复权 K 线可用
COPY data/market_data.duckdb ./data/market_data.duckdb
COPY data/adjusted_kline_cache.duckdb ./data/adjusted_kline_cache.duckdb

COPY entrypoint.sh ./entrypoint.sh
COPY README.md ./使用说明.md
RUN chmod +x ./entrypoint.sh && mkdir -p /app/work/quantweave/runs

EXPOSE 3100

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3100/backtest').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

ENTRYPOINT ["/app/entrypoint.sh"]
