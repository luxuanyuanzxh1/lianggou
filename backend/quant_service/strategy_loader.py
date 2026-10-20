from __future__ import annotations
import ast, importlib.util, re, sys
from pathlib import Path

STRATEGY_DIR = Path(__file__).parent / "strategies"
ALLOWED_IMPORTS = {"backtrader", "math", "statistics", "datetime"}
ARCHIVED_PERSONAL_STRATEGIES = {
    "b1_multifactor",
    "b2_multifactor",
    "gated_multi_expert",
    "zettaranc_b1_discipline",
}

def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"ruipan_strategy_{path.stem}", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path.name}")
    module = importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    strategy = getattr(module, "Strategy", None);meta = getattr(module, "STRATEGY_META", {})
    if strategy is None and "portfolio" not in str(meta.get("engine", "")):
        raise ValueError(f"{path.name} must export class Strategy")
    return strategy, {
        "id": path.stem,
        "name": meta.get("name", path.stem),
        "description": meta.get("description", ""),
        "params": meta.get("params", {}),
        "source": meta.get("source", f"quant_service/strategies/{path.name}"),
        "category": meta.get("category", "本地策略"),
        "uses_sector_trend": bool(meta.get("uses_sector_trend", False)),
        "engine": meta.get("engine", "backtrader"),
        "read_only": bool(meta.get("read_only", False)),
        "portfolio_data": meta.get("portfolio_data"),
        "source_root": meta.get("source_root"),
        "source_files": meta.get("source_files", []),
    }

def list_strategies():
    result=[]
    for path in sorted(STRATEGY_DIR.glob("*.py")):
        if path.name.startswith("_") or path.stem in ARCHIVED_PERSONAL_STRATEGIES:continue
        try: _,meta=_load(path);result.append(meta)
        except Exception as exc:result.append({"id":path.stem,"name":path.stem,"description":f"加载失败: {exc}","params":{},"error":str(exc)})
    return result

def get_strategy(strategy_id: str):
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{1,48}", strategy_id):raise ValueError("invalid strategy id")
    path=STRATEGY_DIR/f"{strategy_id}.py"
    if not path.exists():raise FileNotFoundError(strategy_id)
    return _load(path)

def import_strategy(strategy_id: str, code: str):
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{1,48}", strategy_id):raise ValueError("策略ID只能使用字母、数字和下划线")
    tree=ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node,(ast.Import,ast.ImportFrom)):
            names=[x.name.split('.')[0] for x in node.names] if isinstance(node,ast.Import) else [(node.module or '').split('.')[0]]
            if any(x not in ALLOWED_IMPORTS for x in names):raise ValueError("策略只能导入 backtrader、math、statistics、datetime")
    path=STRATEGY_DIR/f"{strategy_id}.py";path.write_text(code,encoding="utf-8")
    _,meta=_load(path);return meta

def strategy_source_bundle(strategy_id: str):
    _,meta=get_strategy(strategy_id)
    if not meta.get("source_root"):
        path=STRATEGY_DIR/f"{strategy_id}.py"
        return {"id":strategy_id,"read_only":bool(meta.get("read_only",False)),"files":[{"path":path.name,"code":path.read_text(encoding="utf-8")}]}
    root=(STRATEGY_DIR.parents[1]/str(meta.get("source_root"))).resolve()
    allowed_root=STRATEGY_DIR.parents[1].resolve()
    if not root.is_relative_to(allowed_root):raise ValueError("invalid source root")
    files=[]
    for relative in meta.get("source_files",[]):
        path=(root/relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():continue
        files.append({"path":relative,"code":path.read_text(encoding="utf-8",errors="replace")})
    return {"id":strategy_id,"read_only":True,"files":files}
