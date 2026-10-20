from __future__ import annotations


def enrich_order_pnl(orders: list[dict]) -> list[dict]:
    """Attach realized P&L to sell orders using per-symbol average cost."""
    positions: dict[str, dict] = {}
    for order in orders:
        code = str(order.get("code") or "")
        side = str(order.get("side") or "")
        shares = abs(int(order.get("shares") or 0))
        price = float(order.get("price") or 0)
        commission = float(order.get("commission") or 0)
        order["pnl"] = None
        order["pnl_rate"] = None
        if not code or shares <= 0 or price <= 0:
            continue
        state = positions.setdefault(code, {"shares": 0, "cost": 0.0, "entry_date": ""})
        if side == "买入":
            if state["shares"] <= 0:
                state["entry_date"] = str(order.get("date") or "")
            state["cost"] += price * shares + commission
            state["shares"] += shares
            continue
        if side != "卖出" or state["shares"] <= 0:
            continue
        matched = min(shares, int(state["shares"]))
        cost = float(state["cost"]) * matched / int(state["shares"])
        sell_fee = commission * matched / shares
        proceeds = price * matched - sell_fee
        pnl = proceeds - cost
        order["entry_date"] = state["entry_date"]
        order["entry_cost"] = round(cost, 2)
        order["pnl"] = round(pnl, 2)
        order["pnl_rate"] = round(pnl / cost, 6) if cost else 0.0
        state["shares"] -= matched
        state["cost"] -= cost
        if state["shares"] <= 0:
            positions.pop(code, None)
    return orders
