"""Directed-graph contract for the visual Lego strategy workstation.

The canvas is not presentation-only.  Its directed edges are validated here
and compiled into the deterministic block order consumed by ``lego_pipeline``.
Only a connected DAG from the universe node to the execution node is accepted.
Branches and merges are allowed; cycles, backwards stage edges and orphaned
enabled nodes are rejected before a backtest can start.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any


EXECUTION_STAGES = {
    "universe": 0,
    "filter": 1,
    "strategy": 2,
    "factor": 3,
    "normalize": 4,
    "penalty": 5,
    "rank": 6,
    "portfolio": 7,
    "execution": 8,
}


def _position(node: dict[str, Any]) -> tuple[float, float]:
    pos = node.get("position") or {}
    try:
        return float(pos.get("x", 0)), float(pos.get("y", 0))
    except (TypeError, ValueError):
        return 0.0, 0.0


def default_workflow(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Create a truthful left-to-right graph for a legacy linear pipeline."""

    stage_rows: dict[str, int] = defaultdict(int)
    nodes: list[dict[str, Any]] = []
    for block in blocks:
        stage = str(block.get("category", "strategy"))
        stage_index = EXECUTION_STAGES.get(stage, 2)
        row = stage_rows[stage]
        stage_rows[stage] += 1
        nodes.append({
            "id": str(block["id"]),
            "position": {"x": 70 + stage_index * 260, "y": 70 + row * 128},
        })
    edges = []
    for source, target in zip(blocks, blocks[1:]):
        edges.append({
            "id": f"edge-{source['id']}-{target['id']}",
            "source": str(source["id"]),
            "target": str(target["id"]),
            "label": "数据流",
        })
    return {"version": 1, "nodes": nodes, "edges": edges}


def normalize_workflow(workflow: dict[str, Any], blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a small, JSON-safe workflow document and reject ghost objects."""

    raw = copy.deepcopy(workflow or {})
    known = {str(block["id"]) for block in blocks}
    nodes: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    for item in raw.get("nodes", []) or []:
        node_id = str(item.get("id", ""))
        if not node_id or node_id in seen_nodes:
            raise ValueError("工作站存在空节点或重复节点")
        if node_id not in known:
            raise ValueError(f"工作站含有未知模块节点：{node_id}")
        seen_nodes.add(node_id)
        x, y = _position(item)
        nodes.append({"id": node_id, "position": {"x": x, "y": y}})
    missing = known - seen_nodes
    if missing:
        raise ValueError(f"这些模块尚未放入工作站：{', '.join(sorted(missing))}")

    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str]] = set()
    for index, item in enumerate(raw.get("edges", []) or []):
        source, target = str(item.get("source", "")), str(item.get("target", ""))
        if source not in known or target not in known:
            raise ValueError("连线指向了不存在的模块")
        if source == target:
            raise ValueError(f"模块 {source} 不能连接到自己")
        pair = (source, target)
        if pair in seen_edges:
            raise ValueError(f"重复连线：{source} → {target}")
        seen_edges.add(pair)
        edges.append({
            "id": str(item.get("id") or f"edge-{index}-{source}-{target}"),
            "source": source,
            "target": target,
            "label": str(item.get("label") or "数据流")[:40],
        })
    return {"version": 1, "nodes": nodes, "edges": edges}


def compile_workflow(
    blocks: list[dict[str, Any]], workflow: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate the canvas DAG and compile it to the backend execution order."""

    normalized = normalize_workflow(workflow, blocks)
    by_id = {str(block["id"]): copy.deepcopy(block) for block in blocks}
    enabled = {node_id for node_id, block in by_id.items() if bool(block.get("enabled", True))}
    if not enabled:
        raise ValueError("工作站没有启用的模块")

    roots = [node_id for node_id in enabled if str(by_id[node_id].get("category")) == "universe"]
    sinks = [node_id for node_id in enabled if str(by_id[node_id].get("category")) == "execution"]
    if len(roots) != 1 or len(sinks) != 1:
        raise ValueError("工作站必须且只能有一个启用的股票池入口和一个启用的成交出口")
    root, sink = roots[0], sinks[0]

    outgoing: dict[str, set[str]] = {node_id: set() for node_id in enabled}
    incoming: dict[str, set[str]] = {node_id: set() for node_id in enabled}
    for edge in normalized["edges"]:
        source, target = edge["source"], edge["target"]
        if source not in enabled or target not in enabled:
            continue
        source_stage = EXECUTION_STAGES.get(str(by_id[source].get("category")), -1)
        target_stage = EXECUTION_STAGES.get(str(by_id[target].get("category")), -1)
        if source_stage < 0 or target_stage < 0:
            raise ValueError("工作站存在后端无法识别的模块阶段")
        if source_stage > target_stage:
            raise ValueError(
                f"连线方向错误：{by_id[source].get('name', source)} 不能回连到 "
                f"{by_id[target].get('name', target)}"
            )
        outgoing[source].add(target)
        incoming[target].add(source)

    for node_id in enabled:
        if node_id != root and not incoming[node_id]:
            raise ValueError(f"模块“{by_id[node_id].get('name', node_id)}”没有输入连线")
        if node_id != sink and not outgoing[node_id]:
            raise ValueError(f"模块“{by_id[node_id].get('name', node_id)}”没有输出连线")
    if incoming[root]:
        raise ValueError("股票池入口不能有输入连线")
    if outgoing[sink]:
        raise ValueError("成交出口不能再连接下游模块")

    reachable: set[str] = set()
    stack = [root]
    while stack:
        current = stack.pop()
        if current in reachable:
            continue
        reachable.add(current)
        stack.extend(outgoing[current])
    if reachable != enabled:
        missing = enabled - reachable
        raise ValueError(f"存在无法从股票池调用的模块：{', '.join(sorted(missing))}")

    can_reach_sink: set[str] = set()
    stack = [sink]
    while stack:
        current = stack.pop()
        if current in can_reach_sink:
            continue
        can_reach_sink.add(current)
        stack.extend(incoming[current])
    if can_reach_sink != enabled:
        missing = enabled - can_reach_sink
        raise ValueError(f"存在无法流向成交出口的模块：{', '.join(sorted(missing))}")

    positions = {node["id"]: _position(node) for node in normalized["nodes"]}
    indegree = {node_id: len(incoming[node_id]) for node_id in enabled}
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    ordered_ids: list[str] = []
    while ready:
        ready.sort(key=lambda node_id: (
            EXECUTION_STAGES.get(str(by_id[node_id].get("category")), 99),
            positions[node_id][0], positions[node_id][1], node_id,
        ))
        current = ready.pop(0)
        ordered_ids.append(current)
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if len(ordered_ids) != len(enabled):
        raise ValueError("工作站存在环路；回测流程必须是无环有向图")

    disabled_ids = sorted(
        set(by_id) - enabled,
        key=lambda node_id: (
            EXECUTION_STAGES.get(str(by_id[node_id].get("category")), 99),
            positions[node_id][0], positions[node_id][1], node_id,
        ),
    )
    return [by_id[node_id] for node_id in ordered_ids + disabled_ids], normalized


__all__ = ["EXECUTION_STAGES", "compile_workflow", "default_workflow", "normalize_workflow"]
