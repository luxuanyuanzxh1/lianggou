"""Standalone Zettaranc B2 confirmation screener."""

from quant_service.screeners.zettaranc_b123_sequence import SCREENER_META as BASE_META
from quant_service.zettaranc_rules import evaluate_b2


SCREENER_META = {
    **BASE_META,
    "name": "Z·B2·放量确认",
    "description": "只输出建立在此前1—3日真实标准B1之上的B2放量确认；同花顺活跃市值门控不可绕过。",
}


def screen(bars, params):
    return evaluate_b2(bars, params)
