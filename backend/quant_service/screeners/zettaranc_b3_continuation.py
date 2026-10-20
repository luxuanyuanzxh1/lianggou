"""Standalone Zettaranc B3 continuation screener."""

from quant_service.screeners.zettaranc_b123_sequence import SCREENER_META as BASE_META
from quant_service.zettaranc_rules import evaluate_b3


SCREENER_META = {
    **BASE_META,
    "name": "Z·B3·缩量中继",
    "description": "只输出建立在此前1—2日真实标准B2之上的B3小阳、窄幅、缩量中继；同花顺门控不可绕过。",
}


def screen(bars, params):
    return evaluate_b3(bars, params)
