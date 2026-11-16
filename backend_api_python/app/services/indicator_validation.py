from datetime import datetime, timedelta
from typing import Any, Dict

import numpy as np
import pandas as pd

from app.services.indicator_params import IndicatorParamsParser


def generate_mock_df(length: int = 200) -> pd.DataFrame:
    dates = [datetime.now() - timedelta(minutes=i) for i in range(length)]
    dates.reverse()

    returns = np.random.normal(0, 0.002, length)
    price_path = 10000 * np.exp(np.cumsum(returns))

    close = price_path
    high = close * (1 + np.abs(np.random.normal(0, 0.001, length)))
    low = close * (1 - np.abs(np.random.normal(0, 0.001, length)))
    open_price = close * (1 + np.random.normal(0, 0.001, length))
    high = np.maximum(high, np.maximum(open_price, close))
    low = np.minimum(low, np.minimum(open_price, close))
    volume = np.abs(np.random.normal(100, 50, length)) * 1000

    return pd.DataFrame({
        "time": [int(item.timestamp() * 1000) for item in dates],
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


def merge_indicator_params(code: str, user_params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    declared_params = IndicatorParamsParser.parse_params(code or "")
    return IndicatorParamsParser.merge_params(declared_params, user_params or {})


def validate_indicator_code(code: str, user_params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Validate a Pine indicator script.

    This used to run the script in the sandbox and inspect the ``output`` dict it
    produced. Indicators now run in the browser, so the backend can no longer see
    what they draw -- and inspecting a transpiled copy would only check code the
    user never sees. What it can still check is that the script compiles.

    The return shape is deliberately unchanged (``success``/``msg``/``error_type``/
    ``details``/``plots_count``/``signals_count``/``hints``) so every caller, the
    Agent API and the IDE's quality panel included, keeps working.

    ``user_params`` is accepted and ignored: Pine reads its settings from
    ``input.*`` declarations inside the script, so there is nothing to merge in.
    """
    from app.services.pine_validation import analyze_pine_quality, validate_pine_source

    raw = (code or "").strip()
    if not raw:
        return {
            "success": False,
            "msg": "Code is empty",
            "error_type": "EmptyCode",
            "details": None,
            "plots_count": 0,
            "signals_count": 0,
            "hints": [{"severity": "error", "code": "EMPTY_CODE", "params": {}}],
        }

    verdict = validate_pine_source(raw)
    if not verdict.get("success"):
        return {
            "success": False,
            "msg": verdict.get("details") or "The script does not compile.",
            "error_type": verdict.get("error_type") or "PineParseError",
            "details": verdict.get("details"),
            "plots_count": 0,
            "signals_count": 0,
            "hints": list(verdict.get("hints") or []),
        }

    # A compile that succeeded cannot tell us how many series the script draws
    # without running it, and running it is the browser's job.
    return {
        "success": True,
        "msg": "Pine Script compiles.",
        "error_type": None,
        "details": None,
        "plots_count": 0,
        "signals_count": 0,
        "hints": analyze_pine_quality(raw),
    }


def indicator_debug_summary(validation: Dict[str, Any] | None = None) -> Dict[str, Any]:
    validation = validation or {}
    hints = validation.get("hints") or []
    return {
        "success": bool(validation.get("success")),
        "message": validation.get("msg"),
        "error_type": validation.get("error_type"),
        "hint_codes": [hint.get("code") for hint in hints if hint.get("code")],
        "hint_count": len(hints),
        "plots_count": validation.get("plots_count", 0),
        "signals_count": validation.get("signals_count", 0),
    }
