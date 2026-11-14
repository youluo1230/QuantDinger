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
    # ``ta.vwma(source, length)`` weights by the bar's own volume; the runtime
    # helper takes that volume as an explicit argument, so index 1 is the length
    # and volume is supplied positionally by the helper signature below.
    "vwma": ("vwma", ((0, "close_series"), (1, "20")), 1),
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
    "percentile_nearest_rank": (
        "percentile_nearest_rank",
        ((0, "close_series"), (1, "100"), (2, "50")),
        1,
    ),
    "percentile_linear_interpolation": (
        "percentile_linear_interpolation",
        ((0, "close_series"), (1, "100"), (2, "50")),
        1,
    ),
    "sar": (
        "sar",
        (
            (None, "high_series"),
            (None, "low_series"),
            (0, "0.02"),
            (1, "0.2"),
        ),
        1,
    ),
    "cmo": ("cmo", ((0, "close_series"), (1, "14")), 1),
    "tsi": ("tsi", ((0, "close_series"), (1, "13"), (2, "25")), 1),
    "dema": ("dema", ((0, "close_series"), (1, "20")), 1),
    "tema": ("tema", ((0, "close_series"), (1, "20")), 1),
    "median": ("median", ((0, "close_series"), (1, "20")), 1),
    "dev": ("dev", ((0, "close_series"), (1, "20")), 1),
    "cog": ("cog", ((0, "close_series"), (1, "10")), 1),
    "kc": (
        "kc",
        (
            (0, "close_series"),
            (None, "high_series"),
            (None, "low_series"),
            (1, "20"),
            (2, "2.0"),
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
    "kc": ("basis", "upper", "lower"),
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
    "timeframe.in_seconds": "_TIMEFRAME_SECONDS",
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
    # ``vwap`` is ``ta.vwap`` in v4+, but v1-v3 also expose it bare, acting on the
    # bar's own high/low/close/volume. Soft rather than static so a script that
    # assigns its own ``vwap`` shadowing this is still honoured.
    "vwap": "_p_vwap(high_series, low_series, close_series, volume_series)",
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
#: Pine casts and object-style strategy counters. ``strategy.opentrades`` is an
#: object in Pine, so ``strategy.opentrades.entry_price(n)`` arrives as a
#: three-part call chain rather than a two-part one.
GLOBAL_CASTS: Final[dict[str, str]] = {
    "float": "_p_float",
    "int": "_p_int",
    "bool": "_p_bool",
    "string": "_p_str",
    "color": "_p_str",
}

#: ``strategy.opentrades.*`` and ``strategy.closedtrades.*`` member calls.
TRADE_STAT_CALLS: Final[dict[str, str]] = {
    "opentrades.entry_price": "_p_position_price",
    "opentrades.size": "_p_position_size",
    "opentrades.profit": "_p_position_profit",
    "opentrades.entry_bar_index": "_p_position_entry_bar",
    # A bare ``strategy.closedtrades.profit`` is realised PnL; the indexed form
    # is handled by CLOSED_TRADE_CALLS before this table is consulted.
    "closedtrades.profit": "_p_closed_profit",
    "closedtrades.win_count": "_p_win_trade_count",
    "closedtrades.loss_count": "_p_loss_trade_count",
    "closedtrades.size": "_p_closed_size",
    "closedtrades.entry_price": "_p_closed_entry_price",
    "closedtrades.exit_price": "_p_closed_exit_price",
    "closedtrades.entry_time": "_p_closed_entry_time",
    "closedtrades.exit_time": "_p_closed_exit_time",
}

#: Indexed ``strategy.closedtrades.*(index)`` accessors. Unlike the plain
#: properties above these take the trade offset as their only argument.
CLOSED_TRADE_CALLS: Final[dict[str, str]] = {
    "closedtrades.profit": "_p_closed_profit",
    "closedtrades.size": "_p_closed_size",
    "closedtrades.entry_price": "_p_closed_entry_price",
    "closedtrades.exit_price": "_p_closed_exit_price",
    "closedtrades.entry_time": "_p_closed_entry_time",
    "closedtrades.exit_time": "_p_closed_exit_time",
}

#: Scalar ``strategy.*`` counters.
TRADE_STAT_VALUES: Final[dict[str, str]] = {
    "opentrades": "_p_open_trade_count()",
    "closedtrades": "_p_close_trade_count()",
}

MATH_CALLS: Final[dict[str, str]] = {
    "math.abs": "np.abs",
    "math.max": "_p_math_max",
    "math.min": "_p_math_min",
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
    "max": "_p_math_max",
    "min": "_p_math_min",
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
    # v1-v3 flat math builtins. These are the pre-namespace spellings of
    # ``math.*``; the corpus still uses them heavily (``acos``/``atan`` appear in
    # every angle-of-trend script), and without them the call renders to an empty
    # string, which silently deletes the operand from the surrounding arithmetic.
    "acos": "np.arccos",
    "asin": "np.arcsin",
    "atan": "np.arctan",
    "cos": "np.cos",
    "sin": "np.sin",
    "tan": "np.tan",
    "atan2": "np.arctan2",
    "cosh": "np.cosh",
    "sinh": "np.sinh",
    "tanh": "np.tanh",
    "log10": "np.log10",
    "todegrees": "np.degrees",
    "toradians": "np.radians",
    "iff": "_p_ternary",
    "cum": "_p_cum",
    # ``highest``/``lowest`` live in FLAT_SERIES_CALLS instead: their arity
    # differs between Pine v3 and v4+, so one fixed target cannot serve both and
    # a GLOBAL_FUNCTIONS entry here would shadow that dispatch.
}

#: v1-v3 ``highest``/``lowest``/``cum`` act on the bar's own high/low/close and
#: take only a length, unlike their ``ta.`` namespaced forms which take a series
#: first. They are rewritten to the same helpers with the series supplied.
FLAT_SERIES_CALLS: Final[dict[str, tuple[str, str]]] = {
    "highest": ("_p_highest", "high_series"),
    "lowest": ("_p_lowest", "low_series"),
    # ``cum`` is the exception: Pine has always passed the source explicitly
    # (``cum(volume)``), so it is a plain one-argument helper instead.
}

#: Colours are palette constants in Pine, not series. The runtime has no colour
#: model, so every one collapses to a sentinel; keeping the name bound stops it
#: from being read as an undefined Python identifier.
STATIC_CONSTANTS.update(
    {
        f"color.{name}": f"'{name}'"
        for name in (
            "aqua", "black", "blue", "fuchsia", "gray", "green", "lime",
            "maroon", "navy", "olive", "orange", "purple", "red", "silver",
            "teal", "white", "yellow",
        )
    }
)
#: Bare colour names are v1-v3 spellings that predate the ``color.`` namespace.
STATIC_CONSTANTS.update(
    {
        name: f"'{name}'"
        for name in (
            "aqua", "black", "blue", "fuchsia", "gray", "green", "lime",
            "maroon", "navy", "olive", "orange", "purple", "red", "silver",
            "teal", "white", "yellow",
        )
    }
)

#: Pine bar-state and session builtins that have no per-bar Python equivalent but
#: must still resolve to *something* the strategy can compare.
STATIC_CONSTANTS.update(
    {
        "time_close": "_time",
        "time_tradingday": "_time",
        "timenow": "_time",
        "last_bar_time": "_time",
        "last_bar_index": "_bar_index",
        "tickerid": "_SYMBOL",
        "syminfo.tickerid": "_SYMBOL",
        # ``time(...)``/``security``-adjacent aliases that appear bare in v3
        # sources; the runtime has no session model, so the bar time stands in.
        "period": "_TIMEFRAME",
    }
)

#: Pine string helpers rewritten to Python or helper equivalents.
STRING_CALLS: Final[dict[str, str]] = {
    "str.tostring": "_p_str",
    "str.format": "_p_str_fill",
    "str.length": "_p_str_length",
    "str.upper": "_p_str_upper",
    "str.lower": "_p_str_lower",
    "str.contains": "_p_str_contains",
    "str.tonumber": "_p_str_tonumber",
    "str.split": "_p_str_split",
    "str.replace": "_p_str_replace",
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
    "array.size": "_p_array_size",
    "array.slice": "_p_array_slice",
    "array.join": "_p_array_join",
    "array.concat": "_p_array_concat",
    "array.insert": "_p_array_insert",
    "array.fill": "_p_array_fill",
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
    "array.new_label": "_p_array_new",
    "array.new_box": "_p_array_new",
    "array.new_color": "_p_array_new",
    "array.new_table": "_p_array_new",
    "array.new_polyline": "_p_array_new",
    "array.new_linefill": "_p_array_new",
}

ARRAY_CALLS.update(ARRAY_INTRINSICS)

#: ``matrix.*`` calls (Pine matrices are lists of lists here).
MATRIX_CALLS: Final[dict[str, str]] = {
    "matrix.new": "_p_matrix_new",
    "matrix.get": "_p_matrix_get",
    "matrix.set": "_p_matrix_set",
    "matrix.rows": "_p_matrix_rows",
    "matrix.columns": "_p_matrix_columns",
    "matrix.row": "_p_matrix_row",
    "matrix.col": "_p_matrix_col",
    "matrix.transpose": "_p_matrix_transpose",
    "matrix.mult": "_p_matrix_multiply",
    "matrix.inv": "_p_matrix_inverse",
}

#: ``map.*`` calls (Pine maps are plain dicts here).
MAP_CALLS: Final[dict[str, str]] = {
    "map.new": "_p_map_new",
    "map.put": "_p_map_put",
    "map.get": "_p_map_get",
    "map.contains": "_p_map_contains",
    "map.remove": "_p_map_remove",
    "map.size": "_p_map_size",
    "map.keys": "_p_map_keys",
    "map.values": "_p_map_values",
    # ``object.keys()`` and ``object.values()`` on a map reach the same helpers.
    "keys": "_p_map_keys",
    "values": "_p_map_values",
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

#: Drawing calls whose first argument is a per-bar condition. An indicator has
#: no ``strategy.*`` namespace, so these conditions are the only thing it can be
#: traded on; ``SIGNAL_CALLS`` marks the ones the emitter turns into orders.
SIGNAL_CALLS: Final[frozenset[str]] = frozenset(
    {"plotshape", "plotchar", "alertcondition"}
)

#: Words in a signal title that mark it as a long or a short trigger. TradingView
#: authors label alert conditions ("Bullish order block formed", "CHoCH Bear")
#: rather than marking them structurally, so the title is the only direction
#: information the source carries.
LONG_SIGNAL_WORDS: Final[tuple[str, ...]] = (
    "bull", "long", "buy", "up", "support", "demand", "ascending", "green",
)
SHORT_SIGNAL_WORDS: Final[tuple[str, ...]] = (
    "bear", "short", "sell", "down", "resistance", "supply", "descending", "red",
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
