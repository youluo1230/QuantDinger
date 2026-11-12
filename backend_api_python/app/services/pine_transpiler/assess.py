"""Stdlib-only migratability scoring for Pine Script sources.

The assessment never imports ``pynescript``: it is a token scan that produces a
stable, explainable score so the harvest pipeline can rank TradingView scripts
before it pays the cost of a real parse. ``transpile_pine`` performs the
authoritative check.
"""

from __future__ import annotations

import re
from typing import Any, Final

from .mapping import BLOCKING_PATTERNS, TA_SPECS



_CALL_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(")
_BARE_CALL_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\(")
_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"//\s*@version\s*=\s*(\d+)")

#: Namespaces whose calls add real trading semantics.
_TRADING_NAMESPACES: Final[frozenset[str]] = frozenset(
    {"ta", "strategy", "request", "array", "input"}
)

#: Display-only namespaces that are dropped with a warning, not a blocker.
_DISPLAY_NAMESPACES: Final[frozenset[str]] = frozenset(
    {"plot", "plotshape", "plotchar", "plotcandle", "plotarrow", "plotbar",
     "bgcolor", "barcolor", "fill", "hline", "label", "line", "box", "table",
     "alertcondition", "alert", "color", "str", "vis"}
)

#: Sources above this size are unlikely to convert cleanly in one pass.
_SIZE_HEAVY = 60_000
_SIZE_LARGE = 25_000
_SIZE_TINY = 400

#: Calls that carry a per-bar condition an indicator can be traded on. A Pine
#: indicator has no ``strategy.*`` namespace: its only executable statement is a
#: drawing or alert call, and the condition inside one of these is exactly the
#: signal a strategy would have acted on.
_SIGNAL_CALLS: Final[frozenset[str]] = frozenset(
    {"plotshape", "plotchar", "alertcondition"}
)


def assess_migratability(source: str) -> dict[str, Any]:
    """Score how likely a Pine Script source is to transpile automatically.

    Returns ``{"score": int, "blocked": bool, "blockers": [...], "stats": {...}}``.
    The function never raises and never imports the optional Pine parser.
    """
    text = str(source or "")
    stats: dict[str, Any] = {
        "chars": len(text),
        "pine_version": 0,
        "ta_calls": 0,
        "unsupported_calls": [],
        "uses_var": False,
        "uses_for": False,
        "uses_while": False,
        "uses_request_security": False,
        "uses_table": False,
        "has_strategy_entry": False,
        "has_strategy_close": False,
        "has_signal_entry": False,
        "signal_calls": 0,
        "uses_short": False,
        "input_calls": 0,
        "display_calls": 0,
    }
    blockers: list[str] = []

    if not text.strip():
        return {
            "score": 0,
            "blocked": True,
            "blockers": ["empty_source"],
            "stats": stats,
        }

    version_match = _VERSION_PATTERN.search(text)
    stats["pine_version"] = int(version_match.group(1)) if version_match else 0

    calls: dict[str, int] = {}
    for namespace, name in _CALL_PATTERN.findall(text):
        key = f"{namespace}.{name}"
        calls[key] = calls.get(key, 0) + 1

    ta_calls = {key: count for key, count in calls.items() if key.startswith("ta.")}
    stats["ta_calls"] = sum(ta_calls.values())
    unsupported_ta = sorted(
        key.split(".", 1)[1] for key in ta_calls if key.split(".", 1)[1] not in TA_SPECS
    )
    stats["unsupported_calls"] = unsupported_ta

    stats["uses_var"] = bool(re.search(r"(?m)^\s*var(?:ip|p)?\s+\w", text))
    stats["uses_for"] = bool(re.search(r"(?m)^\s*for\b", text))
    stats["uses_while"] = bool(re.search(r"(?m)^\s*while\b", text))
    stats["uses_request_security"] = "request.security(" in text
    stats["uses_table"] = "table." in text
    stats["has_strategy_entry"] = "strategy.entry(" in text
    stats["has_strategy_close"] = bool(
        re.search(r"strategy\.(close_all|close|exit)\s*\(", text)
    )
    stats["uses_short"] = "strategy.short" in text
    # Indicators carry no ``strategy.*`` call at all. Their tradeable content is
    # the condition behind each ``plotshape``/``alertcondition``: the runtime can
    # only trade an indicator if one of those exists, so count them as the entry
    # signal rather than rejecting the whole script. These calls are bare, so
    # they are not part of ``calls`` (which only records ``ns.name(`` pairs).
    bare_calls = _BARE_CALL_PATTERN.findall(text)
    stats["signal_calls"] = sum(1 for name in bare_calls if name in _SIGNAL_CALLS)
    stats["has_signal_entry"] = stats["signal_calls"] > 0
    stats["input_calls"] = sum(
        count for key, count in calls.items() if key.split(".", 1)[0] == "input"
    )
    stats["display_calls"] = sum(
        count
        for key, count in calls.items()
        if key.split(".", 1)[0] in _DISPLAY_NAMESPACES
    )

    for pattern, blocker in BLOCKING_PATTERNS.items():
        if pattern in text:
            blockers.append(blocker)

    if not stats["has_strategy_entry"] and not stats["has_signal_entry"]:
        blockers.append("no_strategy_entry")

    score = 40
    score += 20 if (stats["has_strategy_entry"] or stats["has_signal_entry"]) else 0
    score += 10 if stats["has_strategy_close"] or stats["uses_short"] else 0
    score += 10 if stats["pine_version"] >= 5 else 4 if stats["pine_version"] == 4 else 0
    coverage = _ta_coverage(ta_calls)
    score += int(round(coverage * 15.0))

    if stats["chars"] > _SIZE_HEAVY:
        score -= 20
    elif stats["chars"] > _SIZE_LARGE:
        score -= 10
    elif stats["chars"] < _SIZE_TINY:
        score -= 5
    if stats["uses_request_security"]:
        score -= 3
    if stats["uses_while"]:
        score -= 4
    if not stats["uses_var"]:
        score -= 2
    if unsupported_ta:
        score -= min(15, 3 * len(unsupported_ta))
    if stats["uses_table"]:
        score -= 2
    if blockers:
        score -= min(30, 10 * len(blockers))

    bounded = max(0, min(100, score))
    return {
        "score": bounded,
        "blocked": bool(blockers),
        "blockers": sorted(set(blockers)),
        "stats": stats,
    }


def _ta_coverage(ta_calls: dict[str, int]) -> float:
    total = sum(ta_calls.values())
    if total <= 0:
        return 1.0
    supported = sum(
        count
        for key, count in ta_calls.items()
        if key.split(".", 1)[1] in TA_SPECS
    )
    return supported / float(total)
