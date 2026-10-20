from __future__ import annotations
import ast, importlib.util, re, sys
from pathlib import Path

SCREENER_DIR=Path(__file__).parent/"screeners"
ALLOWED_IMPORTS={"math","statistics","datetime","pandas","numpy"}
ARCHIVED_PERSONAL_SCREENERS={
    "zettaranc_b123_sequence",
    "zettaranc_b1_discipline",
    "zettaranc_b2_confirmation",
    "zettaranc_b3_continuation",
}

def _load(path:Path):
    spec=importlib.util.spec_from_file_location(f"ruipan_screener_{path.stem}",path)
    if not spec or not spec.loader:raise RuntimeError(f"无法加载 {path.name}")
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    fn=getattr(module,"screen",None);meta=getattr(module,"SCREENER_META",{})
    if not callable(fn):raise ValueError(f"{path.name} 必须导出 screen(bars, params)")
    return fn,{"id":path.stem,"name":meta.get("name",path.stem),"description":meta.get("description",""),"lookback":int(meta.get("lookback",120)),"params":meta.get("params",{}),"columns":meta.get("columns",[])}

def list_screeners():
    result=[];SCREENER_DIR.mkdir(exist_ok=True)
    for path in sorted(SCREENER_DIR.glob("*.py")):
        if path.name.startswith("_") or path.stem in ARCHIVED_PERSONAL_SCREENERS:continue
        try:_,meta=_load(path);result.append(meta)
        except Exception as exc:result.append({"id":path.stem,"name":path.stem,"description":f"加载失败: {exc}","lookback":120,"params":{},"error":str(exc)})
    return result

def get_screener(strategy_id:str):
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{1,48}",strategy_id):raise ValueError("选股策略ID格式无效")
    path=SCREENER_DIR/f"{strategy_id}.py"
    if not path.exists():raise FileNotFoundError(strategy_id)
    return _load(path)

def import_screener(strategy_id:str,code:str):
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{1,48}",strategy_id):raise ValueError("策略ID只能使用字母、数字和下划线")
    tree=ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node,(ast.Import,ast.ImportFrom)):
            names=[x.name.split('.')[0] for x in node.names] if isinstance(node,ast.Import) else [(node.module or '').split('.')[0]]
            if any(x not in ALLOWED_IMPORTS for x in names):raise ValueError("选股策略只能导入 pandas、numpy、math、statistics、datetime")
    SCREENER_DIR.mkdir(exist_ok=True);path=SCREENER_DIR/f"{strategy_id}.py";path.write_text(code,encoding="utf-8");_,meta=_load(path);return meta

def import_tdx_screener(strategy_id:str,name:str,formula:str,lookback:int=250):
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{1,48}",strategy_id):raise ValueError("策略ID只能使用字母、数字和下划线")
    if not formula.strip():raise ValueError("通达信公式不能为空")
    source=f'''from quant_service.tdx_formula import evaluate_formula\n\nTDX_FORMULA={formula!r}\nSCREENER_META={{"name":{name!r},"description":"通达信公式导入","lookback":{max(30,min(int(lookback),500))},"params":{{}},"columns":[]}}\n\ndef screen(bars,params):\n    result=evaluate_formula(bars,TDX_FORMULA)\n    if result["matched"]:\n        return {{"score":100.0,"signals":["通达信公式命中"],"details":result["details"]}}\n    return None\n'''
    SCREENER_DIR.mkdir(exist_ok=True);path=SCREENER_DIR/f"{strategy_id}.py";path.write_text(source,encoding="utf-8");_,meta=_load(path);return meta
