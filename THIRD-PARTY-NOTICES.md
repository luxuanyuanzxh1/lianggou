# 第三方组件声明

本项目包含以下第三方代码/许可证，公开分发时需保留相应声明。

## KLineChart（前端 K 线图）

- 位置：`frontend/public/vendor/klinecharts.min.js`
- 许可证：Apache License 2.0
- 许可证全文：`frontend/public/vendor/KLineChart-LICENSE.txt`
- 版权：Copyright (c) 2019 lihu

## Zettaranc 规则（选股/策略）

- 位置：`backend/quant_service/zettaranc_rules.py`、
  `backend/quant_service/screeners/zettaranc_b*.py`
- 上游：https://github.com/lululu811/zettaranc-skill （v4.0.3）
- 许可证：MIT
- 说明：本项目仅按日线 OHLCV 重新实现可复现的规则，未引入上游的 Tushare / SQLite /
  通知 / LLM / 执行栈。文件头部已保留上游仓库与许可证信息。

## Python / Node 依赖

见 `requirements-runtime.txt`（运行期）与 `frontend/package.json`（前端）。
