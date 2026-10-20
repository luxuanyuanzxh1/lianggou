# 睿盘选股策略接口

把策略保存为本目录下的 `strategy_id.py`，网页会自动发现。策略文件需要导出：

```python
SCREENER_META = {
    "name": "策略显示名称",
    "description": "策略说明",
    "lookback": 120,
    "params": {"threshold": 5.0},
    "columns": ["自定义结果列"],
}

def screen(bars, params):
    # bars 按日期升序，列为 date/open/close/high/low/volume/amount
    # 不命中返回 None；命中返回以下字典。
    return {
        "score": 88.0,
        "signals": ["触发条件一", "触发条件二"],
        "details": {"自定义结果列": 12.3},
    }
```

Cursor/IDE 也可以调用 `POST http://127.0.0.1:8766/api/v1/screeners/import`，请求体为：

```json
{"strategy_id":"my_screen","code":"完整 Python 策略源码"}
```

运行历史选股：`POST /api/v1/screens`。请求体包含 `strategy_id`、`date`、`universe`、`limit`、`params`。
