# QuantWeave 全栈回测台 · Docker 交付包

一个文件夹 = 前端完整源码 + 后端完整源码 + DuckDB 数据库（最小执行单元）+ Docker 构建文件。
底层回测引擎是 **Backtrader**，拉起来就能在网页上搭积木式策略、跑全市场回测。

---

## 1. 最快上手

```bash
cd QuantWeave-Docker-20261020
docker compose up -d --build
```

等 2~5 分钟（首次要装依赖 + 编译前端），然后打开：

**http://localhost:3100/backtest**

> 只需要 3100 一个端口。Next.js 的 `rewrites` 把 `/api/v1/*` 反向代理到容器内的 FastAPI(:8767)，
> 所以后端不用单独暴露。

常用命令：

```bash
docker compose logs -f          # 看日志
docker compose down             # 停掉
docker compose ps               # 看状态
```

---

## 2. 目录结构

```
QuantWeave-Docker-20261020/
├── docker-compose.yml          ← 一条命令起全套
├── Dockerfile                  ← 多阶段构建：node 编译前端 → python 运行时
├── entrypoint.sh               ← 容器内同时拉起 FastAPI + Next.js
├── requirements-runtime.txt    ← 后端运行期依赖（Backtrader / DuckDB / FastAPI …）
├── .dockerignore
├── README.md                   ← 本文件（镜像内 /app/使用说明.md）
│
├── frontend/                   ← ★ 前端完整源码（Next.js 16）
│   ├── app/                    #   页面与路由：/ 、/backtest（含因子实验室页签）、/api 代理
│   ├── public/
│   ├── package.json
│   ├── package-lock.json       #   默认 npmmirror 源（国内）
│   ├── package-lock.npmjs.json #   官方源备用（海外构建用）
│   ├── next.config.ts          #   output: standalone + /api 反代规则
│   ├── tsconfig.json / postcss.config.mjs / next-env.d.ts / eslint.config.mjs
│   └── .npmrc
│
├── backend/                    ← ★ 后端完整源码
│   ├── quant_service/          #   FastAPI 接口层 + 回测编排 + 积木块引擎
│   └── backtest_lab/           #   Backtrader 引擎、因子、指标、绩效
│
├── config/                     ← 配置文件
├── scripts/                    ← 建库 / 冒烟 / 策略测试脚本
├── docs/                       ← 数据清单、数据库接入说明
│
└── data/                       ← ★ 数据库（最小执行单元，543 MB）
    ├── market_data.duckdb      #   日线 + 周月季年线 + 估值宽表
    └── adjusted_kline_cache.duckdb   # 前复权 K 线缓存
```

---

## 3. 里面装了什么数据

`data/market_data.duckdb`（506 MB，DuckDB）

| 表 | 行数 | 说明 |
|---|---|---|
| `daily_bar` | 6,978,037 | 日线，**2021-01-04 ~ 2026-10-09**，1,408 个交易日 |
| `weekly_bar` / `monthly_bar` / `quarterly_bar` / `yearly_bar` | 由日线聚合 | 周 / 月 / 季 / 年 |
| `market_value_daily` | 6,978,037 | 估值宽表：总市值 / 流通市值 / PB / PE_TTM |
| `instrument_master` | 5,536 | 标的主表 |

标的范围 **5,534 只**：深 2,905 / 沪 2,322 / 京 309。

**为什么从 2021 年开始**：完整历史（2000 年起）的库是 1.14 GB，超过 GitHub 单文件
100 MB 上限后必须走 Git LFS，而 LFS 免费额度只有 1 GB 存储 + 1 GB/月带宽。
裁剪到 2021 年起是 506 MB，压在额度内。
想要完整历史，用 `scripts/build_local_dataset.py` 从你自己的源库重建（见下节）。

**主动剔除的**（原库里有，但对跑通流程不必要）：

- `feature_store` —— 2.5 GB，只有预计算脚本用，回测是实时算因子
- `minute_bars` —— 25 MB，分钟线只覆盖 155 只标的

---

## 4. 改代码之后怎么办

前端源码在 `frontend/`，后端在 `backend/`，镜像里分别位于 `/app/frontend-src` 与 `/app/quant_service`、`/app/backtest_lab`。

**改前端**（改 `frontend/app/**` 后）：

```bash
docker compose up -d --build     # 重新 build，前端会重新编译
```

**改后端**（Python 是源码挂载进镜像的，有两种做法）：

- 省事：直接改 `backend/`，然后 `docker compose up -d --build`
- 调试：进容器改 `/app/quant_service/...`，重启容器即可，不用重新 build

数据库挂载在 `./data:/app/data`，**换库只要替换 `data/` 里的 duckdb 文件，不用重新 build 镜像**。

---

## 5. 只用 docker run（不用 compose）

```bash
docker build -t quantweave:20261020 .
docker run -d --name qw -p 3100:3100 -v "$(pwd)/data:/app/data" quantweave:20261020
```

镜像里已经内置了同一份数据，所以 `-v` 可以省略，只是省略后换库就得重新 build。

---

## 6. 功能范围

- **90 个策略积木块，83 个可用**（54 个因子块 + 选股域 + 择时 + 风控 + 交易执行）
- 可用选股域 8 个：沪深京 / 沪深 / 沪 / 深 / 京 / 主板 / 科创 / 创业
- 预设管线：`standard_multifactor`（多因子）
- 因子实验室可算 RankIC / ICIR / 胜率，验证因子有效性

**7 个积木块置灰（不可用）及原因**：

| 块 | 原因 |
|---|---|
| 5 个指数成分选股域（沪深300/中证500/上证50/科创50/创业板指） | 缺历史成分股变更表，会有幸存者偏差 |
| 14:45 定时执行 | 缺全市场分钟价（分钟线仅 155 只） |
| 本地已存策略 | V1 设计限制 |

**估值数据的现状**：`market_value_daily` 已经建好，但现有 54 个因子块全是价量因子，
没有一处读 PE / 市值。要用估值做因子，需要新写一个估值积木块（属功能开发，不是开关）。

---

## 7. 已知结论（重要）

`standard_multifactor` 预设在两个窗口表现完全相反：

| 区间 | 收益 | CAGR | Sharpe | 最大回撤 | 超额 |
|---|---|---|---|---|---|
| 2023-10 ~ 2026-10 | +136.10% | 33.15% | 0.94 | -43.75% | +67.94% |
| 2019-01 ~ 2023-10 | **-53.49%** | -14.84% | -0.57 | -65.25% | — |

根因：该预设里「区间动量」和「双均线趋势」的 RankIC(20) 在**两个窗口都是负的**
（-0.087 / -0.077），却配了正权重 0.6，等于把因子反着用；三个因子里只有「平均成交额」有效。
2025 年一年贡献了 +122.7%。

**结论：这套预设目前只能当流程演示，不能当可交易策略。** 要用它，至少要先把动量/趋势因子的权重改成负的，再重测。

---

## 8. 常见问题

**Q：构建时 npm / pip 很慢或超时？**
默认走国内源（npmmirror + 清华 pypi）。在海外构建请指定官方源：

```bash
docker build \
  --build-arg NPM_SOURCE=npmjs \
  --build-arg PIP_INDEX=https://pypi.org/simple \
  -t quantweave:20261020 .
```

**Q：镜像是什么架构？**
在你构建它的机器上是什么架构就是什么架构（Apple Silicon 是 `linux/arm64`）。
要打 x86 镜像：

```bash
docker buildx build --platform linux/amd64 -t quantweave:20261020-amd64 .
```

**Q：端口冲突？**
改 `docker-compose.yml` 里的 `3100:3100`，例如 `"3200:3100"`，容器内的 3100 不用动。

**Q：回测结果存在哪？**
`work/quantweave/runs/`，compose 已把它挂载到宿主机 `./work`，容器删了结果还在。

**Q：前端只有两个页面？**
对，只有 `/` 和 `/backtest`。「因子实验室」是 `/backtest` 里的一个页签，不是独立路由。

---

## 9. 校验记录

本包在本机做过完整回归：19 条链路冒烟测试 **19/19 通过**，包含
健康检查、90 个积木块目录、管线校验、单年全市场回测（universe 5534 / 成交 300 笔）、
回测落盘复查、因子实验室、策略库、选股器、600519 日线 6017 根 / 周线 1267 / 月线 302 / 前复权 5975、
数据调度、工作区、AI 网关、前端 `/` 与 `/backtest` 均返回 200。
