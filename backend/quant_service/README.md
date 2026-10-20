# 睿测本地回测 API

在 Cursor 中直接编辑 `quant_service/strategies/*.py`。每个文件导出 `class Strategy(bt.Strategy)` 和可选的 `STRATEGY_META`，保存后网页会自动发现，无需重启。

启动：`scripts/run_qmt_service.sh`（默认监听 `127.0.0.1:8766`）

接口文档：`http://127.0.0.1:8766/docs`

- `GET /api/v1/strategies`：列出 Cursor 中保存的策略
- `POST /api/v1/strategies/import`：通过 API 导入策略源码
- `POST /api/v1/backtests`：读取本地 DuckDB 并运行 Backtrader
- `GET /api/v1/backtests/{run_id}`：读取已完成结果

服务只监听本机 `127.0.0.1`，仅用于本地历史回测，不发送真实委托。策略代码会被 Python 执行，只导入自己信任的策略。
