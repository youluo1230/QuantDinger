"""Pine Script name mappings for the Strategy API V2 transpiler.

Three kinds of mapping live here:

``TA_SPECS``
    ``ta.*`` names that the emitted strategy implements through a pandas
    helper from :mod:`app.services.pine_transpiler.helpers`. Each entry binds
    Pine's own parameter order onto the helper's positional signature.
``DROPPED_NAMESPACES``
    Pine drawing and display APIs that carry no trading meaning, so the
    transpiler removes the statement and records a warning.
``BLOCKING_PATTERNS``
    Constructs that cannot be expressed safely in Strategy API V2 and therefore
    must stop the translation instead of silently changing behaviour.
"""

from __future__ import annotations

from typing import Final


#: A helper binding: ``(pine_parameter_indexes, default_python_expression)``.
#: The index may be a tuple of candidate positions, tried in order, because
#: several Pine indicators changed parameter order between versions. ``None``
#: means "never bound from source"; the default always applies.
Binding = tuple[int | tuple[int, ...] | None, str]

#: ``ta.*`` specification: ``(helper name, helper bindings, output count)``.
#: The binding list is ordered exactly like the helper's Python signature.
TASpec = tuple[str, tuple[Binding, ...], int]

TA_SPECS: Final[dict[str, TASpec]] = {
    "sma": ("sma", ((0, "close_series"), (1, "14")), 1),
    "ema": ("ema", ((0, "close_series"), (1, "14")), 1),
    "rma": ("rma", ((0, "close_series"), (1, "14")), 1),
    "wma": ("wma", ((0, "close_series"), (1, "14")), 1),
    "hma": ("hma", ((0, "close_series"), (1, "14")), 1),
    "swma": ("swma", ((0, "close_series"),), 1),
    "alma": (
        "alma",
        ((0, "close_series"), (1, "20"), (2, "0.85"), (3, "6.0")),
        1,
    ),
    "vwma": ("vwma", ((0, "close_series"), (None, "volume_series"), (1, "20")), 1),
    "stdev": ("stdev", ((0, "close_series"), (1, "20")), 1),
    "variance": ("variance", ((0, "close_series"), (1, "20")), 1),
    "correlation": (
        "correlation",
        ((0, "close_series"), (1, "close_series"), (2, "20")),
        1,
    ),
    "linreg": ("linreg", ((0, "close_series"), (1, "14"), (2, "0")), 1),
    "cum": ("cum", ((0, "close_series"),), 1),
    "change": ("change", ((0, "close_series"), (1, "1")), 1),
    "roc": ("roc", ((0, "close_series"), (1, "9")), 1),
    "mom": ("mom", ((0, "close_series"), (1, "10")), 1),
    "highest": ("highest", ((0, "close_series"), (1, "14")), 1),
    "lowest": ("lowest", ((0, "close_series"), (1, "14")), 1),
    "highestbars": ("highestbars", ((0, "close_series"), (1, "14")), 1),
    "lowestbars": ("lowestbars", ((0, "close_series"), (1, "14")), 1),
    "barssince": ("barssince", ((0, "close_series > 0.0"),), 1),
    "valuewhen": (
        "valuewhen",
        ((0, "close_series > 0.0"), (1, "close_series"), (2, "0")),
        1,
    ),
    "pivothigh": (
        "pivothigh",
        ((0, "high_series"), (1, "10"), (2, "10")),
        1,
    ),
    "pivotlow": (
        "pivotlow",
        ((0, "low_series"), (1, "10"), (2, "10")),
        1,
    ),
    "crossover": ("crossover", ((0, "close_series"), (1, "close_series")), 1),
    "crossunder": ("crossunder", ((0, "close_series"), (1, "close_series")), 1),
    "cross": ("cross", ((0, "close_series"), (1, "close_series")), 1),
    "rising": ("rising", ((0, "close_series"), (1, "1")), 1),
    "falling": ("falling", ((0, "close_series"), (1, "1")), 1),
    "tr": ("true_range", ((None, "high_series"), (None, "low_series"), (None, "close_series")), 1),
    "atr": (
        "atr",
        ((None, "high_series"), (None, "low_series"), (None, "close_series"), (0, "14")),
        1,
    ),
    "rsi": ("rsi", ((0, "close_series"), (1, "14")), 1),
    "stoch": (
        "stoch",
        (
            (0, "close_series"),
            (1, "high_series"),
            (2, "low_series"),
            (3, "14"),
        ),
        1,
    ),
    "macd": (
        "macd",
        ((0, "close_series"), (1, "12"), (2, "26"), (3, "9")),
        3,
    ),
    "bb": ("bollinger", ((0, "close_series"), (1, "20"), (2, "2.0")), 3),
    "bbw": ("bbw", ((0, "close_series"), (1, "20"), (2, "2.0")), 1),
    "cci": (
        "cci",
        ((None, "high_series"), (None, "low_series"), (None, "close_series"), (1, "20")),
        1,
    ),
    "wpr": (
        "wpr",
        ((None, "close_series"), (None, "high_series"), (None, "low_series"), (0, "14")),
        1,
    ),
    "mfi": (
        "mfi",
        (
            (None, "high_series"),
            (None, "low_series"),
            (None, "close_series"),
            (None, "volume_series"),
            ((1, 0), "14"),
        ),
        1,
    ),
    "vwap": (
        "vwap",
        (
            (None, "high_series"),
            (None, "low_series"),
            (None, "close_series"),
            (None, "volume_series"),
        ),
        1,
    ),
    "supertrend": (
        "supertrend",
        (
            (None, "high_series"),
            (None, "low_series"),
            (None, "close_series"),
            (0, "3.0"),
            (1, "10"),
        ),
        2,
    ),
    "percentrank": ("percentrank", ((0, "close_series"), (1, "20")), 1),
    "dmi": (
        "dmi",
        (
            (None, "high_series"),
            (None, "low_series"),
            (None, "close_series"),
            (0, "14"),
            (1, "14"),
        ),
        3,
    ),
}

#: Tuple component names for indicators that return several series.
TA_OUTPUT_NAMES: Final[dict[str, tuple[str, ...]]] = {
    "supertrend": ("value", "direction"),
    "macd": ("line", "signal", "hist"),
    "bb": ("basis", "upper", "lower"),
    "dmi": ("plus", "minus", "adx"),
}

#: Pine series variables available on every bar and the emitted frame column.
SERIES_NAMES: Final[dict[str, str]] = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "hl2": "hl2",
    "hlc3": "hlc3",
    "ohlc4": "ohlc4",
    "hlcc4": "hlcc4",
}

#: Derived series the emitted preamble builds from the history frame.
SERIES_PREAMBLE: Final[dict[str, str]] = {
    "hl2": "(high_series + low_series) / 2.0",
    "hlc3": "(high_series + low_series + close_series) / 3.0",
    "ohlc4": "(open_series + high_series + low_series + close_series) / 4.0",
    "hlcc4": "(close_series * 2.0 + high_series + low_series) / 4.0",
}

#: Pine scalars and ``strategy.*`` properties with a direct runtime equivalent.
STATIC_CONSTANTS: Final[dict[str, str]] = {
    "na": "float('nan')",
    "true": "True",
    "false": "False",
    "syminfo.tickerid": "_SYMBOL",
    "syminfo.ticker": "_SYMBOL",
    "syminfo.mintick": "0.00000001",
    "syminfo.currency": "_QUOTE_CURRENCY",
    "syminfo.pointvalue": "1.0",
    "syminfo.description": "_SYMBOL",
    "syminfo.root": "_SYMBOL",
    "syminfo.prefix": "_SYMBOL",
    "timeframe.period": "_TIMEFRAME",
    "timeframe.multiplier": "_TIMEFRAME_MULTIPLIER",
    "timeframe.isintraday": "_TIMEFRAME_IS_INTRADAY",
    "timeframe.isdaily": "_TIMEFRAME_IS_DAILY",
    "timeframe.isweekly": "_TIMEFRAME_IS_WEEKLY",
    "timeframe.ismonthly": "_TIMEFRAME_IS_MONTHLY",
    "bar_index": "_bar_index",
    "time": "_time",
    "last_bar_index": "_bar_index",
    "strategy.position_size": "_p_position_size()",
    "strategy.position_avg_price": "_p_position_price()",
    "strategy.opentrades": "_p_open_trades()",
    "strategy.closedtrades": "_closed_trades",
    "strategy.wintrades": "_win_trades",
    "strategy.losstrades": "_loss_trades",
    "strategy.long": "1.0",
    "strategy.short": "-1.0",
    "strategy.initial_capital": "_INITIAL_CAPITAL",
    "strategy.equity": "_INITIAL_CAPITAL",
    "strategy.position_entry_name": "_ENTRY_NAME",
}

#: Pine ``math``/``strategy`` constants that map directly to Python values.
STATIC_CONSTANTS.update(
    {
        "math.pi": "np.pi",
        "math.e": "np.e",
        "ta.tr": "_p_true_range(high_series, low_series, close_series)",
    }
)

#: Pine helpers that build or filter timestamps. ``session_start_time`` and
#: friends derive bar-time values the runtime already knows.
STATIC_CONSTANTS.update(
    {
        "session.isfirstbar": "_p_session_flag(True)",
        "session.isfirstbar_regular": "_p_session_flag(True)",
        "session.islastbar": "_p_session_flag(True)",
        "session.islastbar_regular": "_p_session_flag(True)",
        "session.ismarket": "_p_session_flag(True)",
        "session.ispremarket": "_p_session_flag(False)",
        "session.ispostmarket": "_p_session_flag(False)",
        "dayofweek.monday": "0",
        "dayofweek.tuesday": "1",
        "dayofweek.wednesday": "2",
        "dayofweek.thursday": "3",
        "dayofweek.friday": "4",
        "dayofweek.saturday": "5",
        "dayofweek.sunday": "6",
    }
)

#: Pine calendar builtins that scripts also call as functions
#: (``dayofweek(time)``). The frame index already carries the timestamp, so the
#: argument is ignored and the bar's own component is returned.
TIME_COMPONENT_CALLS: Final[frozenset[str]] = frozenset(
    {
        "hour",
        "minute",
        "second",
        "year",
        "month",
        "dayofmonth",
        "dayofweek",
        "weekofyear",
    }
)

#: Builtins that are only constants when the script does not shadow them.
SOFT_CONSTANTS: Final[dict[str, str]] = {
    "tr": "_p_true_range(high_series, low_series, close_series)",
}

#: ``barstate`` flags describe the replay loop itself. The runtime only ever calls
#: ``handle_data`` on a completed bar, so every one of them is constantly true.
STATIC_CONSTANTS.update(
    {
        "barstate.isfirst": "(_bar_index == 0)",
        "barstate.islast": "True",
        "barstate.isconfirmed": "True",
        "barstate.isnew": "True",
        "barstate.isrealtime": "False",
        "barstate.ishistory": "True",
    }
)

#: Pine calendar builtins are per-bar series, not scalars, so they read the bar
#: frame's index rather than a timestamp value.
STATIC_CONSTANTS.update(
    {
        f"{name}": f'_p_time_component("{name}")'
        for name in (
            "hour",
            "minute",
            "second",
            "year",
            "month",
            "dayofmonth",
            "dayofweek",
            "weekofyear",
        )
    }
)

#: ``math.*`` calls translated to numpy or helper expressions.
MATH_CALLS: Final[dict[str, str]] = {
    "math.abs": "np.abs",
    "math.max": "np.maximum",
    "math.min": "np.minimum",
    "math.round": "_p_round",
    "math.floor": "np.floor",
    "math.ceil": "np.ceil",
    "math.sqrt": "np.sqrt",
    "math.pow": "np.power",
    "math.log": "np.log",
    "math.log10": "np.log10",
    "math.exp": "np.exp",
    "math.sin": "np.sin",
    "math.cos": "np.cos",
    "math.tan": "np.tan",
    "math.atan": "np.arctan",
    "math.asin": "np.arcsin",
    "math.acos": "np.arccos",
    "math.sign": "np.sign",
    "math.avg": "_p_avg",
    "math.sum": "_p_math_sum",
    "math.round_to_mintick": "_p_round",
    "math.todegrees": "np.degrees",
    "math.toradians": "np.radians",
}

#: Bare Pine global functions with a runtime or helper implementation.
GLOBAL_FUNCTIONS: Final[dict[str, str]] = {
    "timestamp": "_p_timestamp",
    "time": "_p_time",
    "nz": "_p_nz",
    "na": "_p_isna",
    "fixnan": "_p_fixnan",
    "max": "np.maximum",
    "min": "np.minimum",
    "abs": "np.abs",
    "round": "_p_round",
    "sum": "_p_math_sum",
    "avg": "_p_avg",
    "sign": "np.sign",
    "sqrt": "np.sqrt",
    "log": "np.log",
    "pow": "np.power",
    "exp": "np.exp",
    "floor": "np.floor",
    "ceil": "np.ceil",
}

#: Pine string helpers rewritten to Python or helper equivalents.
STRING_CALLS: Final[dict[str, str]] = {
    "str.tostring": "_p_str",
    "str.format": "_p_str",
    "str.length": "len",
    "str.upper": "_p_str_upper",
    "str.lower": "_p_str_lower",
    "str.contains": "_p_str_contains",
}

#: ``array.*`` calls mapped onto the emitted list helpers.
ARRAY_CALLS: Final[dict[str, str]] = {
    "array.new_float": "_p_array_new",
    "array.new_int": "_p_array_new",
    "array.new_bool": "_p_array_new",
    "array.new_string": "_p_array_new",
    "array.new": "_p_array_new",
    "array.get": "_p_array_get",
    "array.set": "_p_array_set",
    "array.push": "_p_array_push",
    "array.pop": "_p_array_pop",
    "array.shift": "_p_array_shift",
    "array.unshift": "_p_array_unshift",
    "array.clear": "_p_array_clear",
    "array.remove": "_p_array_remove",
    "array.indexof": "_p_array_index",
    "array.lastindexof": "_p_array_lastindex",
    "array.includes": "_p_array_includes",
    "array.size": "len",
}

ARRAY_INTRINSICS: Final[dict[str, str]] = {
    "array.copy": "_p_array_copy",
    "array.sort": "_p_array_sort",
    "array.last": "_p_array_last",
    "array.from": "_p_array_from",
    "array.median": "_p_array_median",
    "array.range": "_p_array_range",
    "array.reverse": "_p_array_reverse",
    "array.new_line": "_p_array_new",
}

ARRAY_CALLS.update(ARRAY_INTRINSICS)

#: ``matrix.*`` calls (Pine matrices are lists of lists here).
MATRIX_CALLS: Final[dict[str, str]] = {
    "matrix.new": "_p_matrix_new",
    "matrix.get": "_p_matrix_get",
    "matrix.set": "_p_matrix_set",
    "matrix.rows": "_p_matrix_rows",
    "matrix.columns": "_p_matrix_columns",
}

#: ``map.*`` calls (Pine maps are plain dicts here).
MAP_CALLS: Final[dict[str, str]] = {
    "map.new": "_p_map_new",
    "map.put": "_p_map_put",
    "map.get": "_p_map_get",
    "map.contains": "_p_map_contains",
    "map.remove": "_p_map_remove",
    "map.size": "_p_map_size",
}

#: ``array.*`` statistics that route through the generic reducer.
ARRAY_STATS: Final[dict[str, str]] = {
    "array.sum": "sum",
    "array.avg": "avg",
    "array.max": "max",
    "array.min": "min",
    "array.stdev": "stdev",
    "array.variance": "variance",
}

#: Display-only namespaces whose calls carry no trading semantics.
DROPPED_NAMESPACES: Final[tuple[str, ...]] = (
    "plot",
    "plotshape",
    "plotchar",
    "plotcandle",
    "plotarrow",
    "plotbar",
    "bgcolor",
    "barcolor",
    "fill",
    "hline",
    "label",
    "line",
    "box",
    "table",
    "alertcondition",
    "alert",
    "color",
    "log",
    "runtime",
)

#: ``strategy.*`` calls that place, flip, or flatten a position.
STRATEGY_ENTRY_CALLS: Final[frozenset[str]] = frozenset({"entry", "order", "long", "short"})
STRATEGY_CLOSE_CALLS: Final[frozenset[str]] = frozenset({"close", "close_all", "cancel", "cancel_all"})
STRATEGY_EXIT_CALLS: Final[frozenset[str]] = frozenset({"exit"})
STRATEGY_IGNORED_CALLS: Final[frozenset[str]] = frozenset(
    {
        "percent_of_equity",
        "fixed",
        "cash",
        "contracts",
        "commission_type",
        "commission_cash_per_contract",
        "commission_cash_per_order",
        "commission_percent",
        "slippage",
        "margin_long",
        "margin_short",
        "pyramiding",
        "process_orders_on_close",
        "calc_on_order_fills",
        "calc_on_every_tick",
    }
)

#: Pine declaration calls that describe the script rather than trade it.
DECLARATION_CALLS: Final[frozenset[str]] = frozenset({"strategy", "indicator", "study"})

#: ``request.*`` calls the runtime can approximate or must ignore.
REQUEST_SECURITY_CALLS: Final[frozenset[str]] = frozenset(
    {"request.security", "request.security_lower_tf"}
)

#: Blocking constructs: substring pattern -> machine-readable blocker identifier.
BLOCKING_PATTERNS: Final[dict[str, str]] = {
    "matrix.": "matrix_unsupported",
    "map.": "map_unsupported",
    "polyline.": "polyline_unsupported",
    "request.financial": "request_financial_unsupported",
    "request.dividends": "request_dividends_unsupported",
    "request.splits": "request_splits_unsupported",
    "request.economic": "request_economic_unsupported",
    "request.quandl": "request_quandl_unsupported",
    "request.currency_rate": "request_currency_rate_unsupported",
}

#: ``request.*`` calls removed with a warning because they are informational.
WARNED_REQUEST_CALLS: Final[dict[str, str]] = {
    "request.financial": "request_financial_ignored",
    "request.dividends": "request_dividends_ignored",
    "request.splits": "request_splits_ignored",
    "request.economic": "request_economic_ignored",
}

#: ``strategy.risk.*`` rules: the runtime applies protection separately.
STRATEGY_RISK_PREFIX: Final[str] = "strategy.risk."

#: Names the emitted strategy body must never reference.
FORBIDDEN_IDENTIFIERS: Final[frozenset[str]] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "globals",
        "vars",
        "dir",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "importlib",
    }
)

#: Pine type keywords stripped while renaming identifiers.
PINE_TYPE_WORDS: Final[frozenset[str]] = frozenset(
    {
        "float",
        "int",
        "bool",
        "string",
        "color",
        "line",
        "label",
        "box",
        "table",
        "array",
        "matrix",
        "map",
        "series",
        "simple",
        "const",
        "input",
        "varip",
        "var",
    }
)
