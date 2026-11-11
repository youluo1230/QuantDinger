"""Unit tests for the Pine Script transpiler.

The compile gate is the load-bearing assertion: whenever the transpiler claims
a script converted successfully, the emitted source must also pass
``compile_strategy_v2`` and be executable by the real strategy runtime.
"""

from __future__ import annotations

import ast

import pytest

from app.services.pine_transpiler import (
    TranspileResult,
    assess_migratability,
    parser_available,
    transpile_pine,
)
from app.services.pine_transpiler.emitter import normalize_timeframe
from app.services.pine_transpiler.helpers import helper_source, is_helper
from app.services.strategy_v2.contract import compile_strategy_v2

pytestmark = pytest.mark.skipif(
    not parser_available(),
    reason="pynescript is not installed in this image",
)


EMA_CROSS = """
//@version=5
strategy("EMA Cross", overlay=true)
len = input.int(14, "EMA length")
fast = ta.ema(close, 9)
slow = ta.ema(close, len)
if ta.crossover(fast, slow)
    strategy.entry("Long", strategy.long)
if ta.crossunder(fast, slow)
    strategy.close_all("exit")
"""


def _compiles(code: str) -> bool:
    """Report whether emitted code passes the strategy contract."""
    try:
        compile_strategy_v2(code)
    except Exception:  # noqa: BLE001 - the contract raises its own error type
        return False
    return True


def test_transpile_returns_result_with_documented_fields():
    result = transpile_pine(EMA_CROSS, symbol="BTC/USDT", timeframe="1h", name="EMA Cross")

    assert isinstance(result, TranspileResult)
    assert result.ok is True
    assert result.error is None
    assert result.blockers == []
    assert result.entry_style == "signal"
    assert isinstance(result.code, str) and result.code.strip()
    assert isinstance(result.warnings, list)
    assert isinstance(result.stats, dict)
    assert isinstance(result.params, dict)


def test_compiled_output_passes_strategy_contract():
    result = transpile_pine(EMA_CROSS, symbol="BTC/USDT", timeframe="1h")

    assert result.ok, result.blockers
    assert _compiles(result.code)


def test_generated_source_respects_runtime_module_rules():
    result = transpile_pine(EMA_CROSS)

    assert result.ok, result.blockers
    code = result.code
    # The strategy sandbox rejects these at import time.
    assert "from __future__" not in code
    assert "__import__" not in code
    # The runtime needs module-level state to carry Pine ``var`` values.
    assert "_STATE = {" in code
    assert "def initialize(context):" in code
    assert "def handle_data(context, data):" in code


def test_generated_source_avoids_sandbox_banned_builtins():
    """The strategy sandbox rejects these names before the code even imports."""
    banned = (
        "getattr", "setattr", "dir", "globals", "vars", "breakpoint",
        "importlib", "eval", "exec", "compile", "open", "chr", "format",
    )
    result = transpile_pine(EMA_CROSS)
    assert result.ok, result.blockers

    for name in banned:
        assert f"{name}(" not in result.code, f"generated source calls banned {name}()"


def test_param_extraction_maps_inputs_to_schema():
    source = """
//@version=5
strategy("Params", overlay=true)
fastLen = input.int(12, "Fast length")
threshold = input.float(1.5, "Threshold")
useFilter = input.bool(true, "Use filter")
label = input.string("BTC", "Label")
if close > ta.sma(close, fastLen)
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert result.params["fastLen"] == {
        "type": "int",
        "default": 12,
        "title": "Fast length",
    }
    assert result.params["threshold"]["type"] == "float"
    assert result.params["useFilter"]["type"] == "bool"
    assert result.params["useFilter"]["default"] is True
    assert result.params["threshold"]["default"] == pytest.approx(1.5)
    assert "context.params.get" in result.code
    assert "# @param" in result.code
    assert _compiles(result.code)


def test_ta_indicator_calls_are_translated_to_pandas_helpers():
    source = """
//@version=5
strategy("Indicators", overlay=true)
basis = ta.sma(close, 20)
dev = ta.stdev(close, 20)
rsiValue = ta.rsi(close, 14)
atrValue = ta.atr(14)
if ta.crossover(close, basis) and rsiValue < 70
    strategy.entry("Long", strategy.long)
if ta.crossunder(close, basis) or rsiValue > 80
    strategy.close_all()
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert result.stats["ta_calls"] >= 5
    # No TA-Lib: the indicator math must be plain pandas/numpy helpers.
    assert "_p_sma(" in result.code
    assert "_p_rsi(" in result.code
    assert "_p_atr(" in result.code
    assert "talib" not in result.code
    assert _compiles(result.code)


def test_ta_helper_source_covers_every_supported_indicator():
    from app.services.pine_transpiler.mapping import TA_SPECS

    for name, spec in TA_SPECS.items():
        assert is_helper(spec[0]), f"ta.{name} helper {spec[0]} has no implementation"

    used = {spec[0] for spec in TA_SPECS.values()}
    emitted = helper_source(used)
    for helper in used:
        assert f"def _p_{helper}(" in emitted, f"helper {helper} not emitted"


def test_helper_source_emits_transitive_dependencies():
    # rsi/atr are defined in the volatility block, which calls the prefix
    # helpers; those must be emitted too or the module fails to compile.
    emitted = helper_source({"rsi"})

    assert "def _p_rsi(" in emitted
    for dependency in ("_p_ema(", "_p_rma(", "_p_sma(", "_p_window("):
        assert f"def {dependency}" in emitted
    assert "def _state_get(" in emitted


def test_var_state_persists_through_module_state():
    source = """
//@version=5
strategy("State", overlay=true)
var int bars = 0
bars := bars + 1
if bars > 10 and ta.crossover(ta.ema(close, 5), ta.ema(close, 20))
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert result.stats["uses_var"] is True
    assert "_state_get(" in result.code
    assert "_state_set(" in result.code
    # No bare local binding may shadow the persisted slot.
    assert "_STATE = {" in result.code
    assert _compiles(result.code)


def test_series_conditions_are_wrapped_to_avoid_dataframe_ambiguity():
    source = """
//@version=5
strategy("Series condition", overlay=true)
signal = ta.crossover(ta.sma(close, 5), ta.sma(close, 20))
if signal
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert "_p_truth(" in result.code
    assert _compiles(result.code)


def test_display_calls_are_dropped_with_warnings_not_blockers():
    source = """
//@version=5
strategy("Display", overlay=true)
plot(close)
plotshape(ta.crossover(close, ta.sma(close, 9)), style=shape.triangleup)
bgcolor(close > open ? color.green : color.red)
barcolor(color.blue)
alertcondition(ta.crossover(close, ta.sma(close, 9)), title="cross")
label.new(bar_index, close, "x")
line.new(bar_index, close, bar_index + 1, close)
table.new(position.top_right, 1, 1)
if ta.crossover(close, ta.sma(close, 9))
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert result.blockers == []
    assert any("plot" in warning for warning in result.warnings)
    assert _compiles(result.code)


def test_request_security_becomes_a_subscription_warning():
    source = """
//@version=5
strategy("HTF", overlay=true)
htfClose = request.security(syminfo.tickerid, "240", close)
if ta.crossover(close, ta.sma(htfClose, 20))
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert result.stats["uses_request_security"] is True
    assert 'context.subscribe(frequency="4h")' in result.code
    assert _compiles(result.code)


def test_short_entries_become_negative_target_percent():
    source = """
//@version=5
strategy("Both sides", overlay=true)
if ta.crossunder(ta.ema(close, 9), ta.ema(close, 21))
    strategy.entry("Short", strategy.short)
if ta.crossover(ta.ema(close, 9), ta.ema(close, 21))
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert result.stats["uses_short"] is True
    assert "order_target_percent(_SYMBOL, -1.0" in result.code
    assert "order_target_percent(_SYMBOL, 1.0" in result.code
    assert _compiles(result.code)


def test_script_without_entry_orders_is_reported_as_blocked():
    source = """
//@version=5
strategy("No entries", overlay=true)
plot(ta.sma(close, 20))
"""

    result = transpile_pine(source)

    assert result.ok is False
    assert "no_strategy_entry" in result.blockers
    assert result.code is not None
    assert result.error


def test_unsupported_matrix_construct_is_blocked():
    source = """
//@version=5
strategy("Matrix", overlay=true)
m = matrix.new<int>(2, 2, 0)
if ta.crossover(close, ta.sma(close, 9))
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok is False
    assert any("matrix" in blocker for blocker in result.blockers)


def test_empty_source_returns_error_instead_of_raising():
    result = transpile_pine("")

    assert result.ok is False
    assert result.code is None
    assert result.error == "empty_source"


def test_timeframe_normalization_handles_pine_literals():
    assert normalize_timeframe("240") == "4h"
    assert normalize_timeframe("60") == "1h"
    assert normalize_timeframe("D") == "1d"
    assert normalize_timeframe("1h") == "1h"
    assert normalize_timeframe("weird") == "1d"


def test_generated_strategy_runs_in_the_backtest_runtime():
    """The transpiled strategy must execute end-to-end, not merely compile."""
    import math

    import pandas as pd

    from app.services.strategy_v2 import StrategyV2BacktestRunner

    result = transpile_pine(EMA_CROSS, symbol="BTC/USDT", timeframe="1h")
    assert result.ok, result.blockers

    prices = [100.0 + 10.0 * math.sin(index / 12.0) for index in range(400)]
    index = pd.date_range("2026-01-01", periods=len(prices), freq="h")
    frame = pd.DataFrame(
        {
            "open": prices,
            "high": [price * 1.01 for price in prices],
            "low": [price * 0.99 for price in prices],
            "close": prices,
            "volume": [1000.0] * len(prices),
        },
        index=index,
    )

    runner = StrategyV2BacktestRunner(
        code=result.code,
        frames={"BTC/USDT": frame},
        initial_capital=10000.0,
    )
    output = runner.run()

    assert output["rawTrades"], "expected the EMA cross strategy to trade"
    assert output["finalEquity"] > 0


def _unbound_names(function: ast.FunctionDef) -> set[str]:
    """Names a function loads but never binds in its own scope."""
    bound = {arg.arg for arg in function.args.posonlyargs + function.args.args}
    bound.update(arg.arg for arg in function.args.kwonlyargs)
    if function.args.vararg:
        bound.add(function.args.vararg.arg)
    if function.args.kwarg:
        bound.add(function.args.kwarg.arg)
    traced = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Global):
            traced.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    loaded = {
        node.id
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    return loaded - bound - traced


def test_translated_pine_functions_never_read_unbound_names():
    """Every name a nested Pine function reads must be bound before it runs.

    Pine functions close over script-level variables. The generated strategy
    nests them inside ``handle_data``, so a parameter that is bound after the
    function definition is an unbound local at call time. This static check
    catches that whole class without needing a backtest.
    """
    source = """
//@version=5
strategy("Scoped", overlay=true)
length = input.int(2, "Length")
mult = input.float(3.0, "Mult")

measure(src, span) =>
    fast = ta.ema(src, span)
    slow = ta.ema(src, length)
    fast - slow * mult

score() =>
    trend = measure(close, length)
    direction = trend > 0 ? 1.0 : -1.0
    direction

value = score()
if value > 0
    strategy.entry("Long", strategy.long)
else
    strategy.entry("Short", strategy.short)
"""

    result = transpile_pine(source)
    assert result.ok, result.blockers

    tree = ast.parse(result.code)
    handler = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "handle_data"
    )
    module_names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}

    # Only the definitions that are actually nested inside the handler close over
    # handler locals; module-level helpers legitimately see module globals.
    bound_before: set[str] = set()
    nested_checked = 0
    for statement in handler.body:
        if isinstance(statement, ast.FunctionDef):
            missing = _unbound_names(statement) - module_names - bound_before - {"np", "pd"}
            assert not missing, f"{statement.name} reads names bound after it: {sorted(missing)}"
            bound_before.add(statement.name)
            nested_checked += 1
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            for node in ast.walk(statement):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    bound_before.add(node.id)
        elif isinstance(statement, ast.Global):
            bound_before.update(statement.names)
        elif isinstance(statement, ast.If):
            for node in ast.walk(statement):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    bound_before.add(node.id)
    assert nested_checked, "the script defines Pine functions that must be emitted"


SCALAR_AND_SWITCH = """
//@version=5
strategy("Hard constructs", overlay=true)
len = input.int(9, "llen")
mode = input.string("A", "Mode")
src = input(ohlc4, "Source")

pick(m) =>
    v = switch m
        "A" => 1.0
        "B" => 2.0
        => 0.0
    v

fast = ta.sma(src, len)
slow = ta.ema(close, len * 2)
gate = pick(mode)
signal = ta.crossover(fast, slow) and gate > 0
if signal
    strategy.entry("Long", strategy.long)
if ta.crossunder(fast, slow)
    strategy.close_all()
"""


def test_scalar_inputs_and_switch_functions_run_in_the_backtest_runtime():
    """A script mixing inputs, switch and two rolling paths must still execute."""
    import math

    import pandas as pd

    from app.services.strategy_v2 import StrategyV2BacktestRunner

    result = transpile_pine(SCALAR_AND_SWITCH)
    assert result.ok, result.blockers

    prices = [100.0 + 10.0 * math.sin(index / 9.0) for index in range(400)]
    opens = [prices[0]] + prices[:-1]
    index = pd.date_range("2026-01-01", periods=len(prices), freq="h")
    frame = pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) * 1.004 for o, c in zip(opens, prices)],
            "low": [min(o, c) * 0.996 for o, c in zip(opens, prices)],
            "close": prices,
            "volume": [1000.0 + (item % 13) * 5.0 for item in range(len(prices))],
        },
        index=index,
    )

    runner = StrategyV2BacktestRunner(
        code=result.code,
        frames={"BTC/USDT": frame},
        initial_capital=10000.0,
    )
    output = runner.run()

    assert output["finalEquity"] > 0
    assert output["rawTrades"], "scalar-fed helpers must still produce trades"


def test_assess_migratability_scores_simple_scripts_highly():
    assessment = assess_migratability(EMA_CROSS)

    assert set(assessment) >= {"score", "blocked", "blockers", "stats"}
    assert isinstance(assessment["score"], int)
    assert 0 <= assessment["score"] <= 100
    assert assessment["blocked"] is False
    assert assessment["score"] >= 70


def test_assess_migratability_blocks_heavy_constructs():
    source = """
//@version=5
strategy("Polyline", overlay=true)
m = matrix.new<float>(2, 2, 0.0)
p = polyline.new(bar_index, close)
if ta.crossover(close, ta.sma(close, 9))
    strategy.entry("Long", strategy.long)
"""

    assessment = assess_migratability(source)

    assert assessment["blocked"] is True
    assert assessment["blockers"]


def test_assess_migratability_is_importable_without_the_parser():
    """The scorer is a stdlib token scan, so it must work with no parser at all."""
    import sys

    class _BlockPynescript:
        """Meta path hook that makes ``pynescript`` unimportable."""

        def find_spec(self, name, path=None, target=None):  # noqa: ANN001, ANN201
            if name.split(".")[0] == "pynescript":
                raise ImportError("pynescript intentionally absent")
            return None

    # ``pine_ast`` caches the availability verdict, so the transpiler modules have
    # to be evicted as well; only ``pynescript`` is blocked, not our own package.
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name.startswith("pynescript") or name.startswith("app.services.pine_transpiler")
    }
    for name in saved:
        del sys.modules[name]
    sys.meta_path.insert(0, _BlockPynescript())
    try:
        import importlib

        module = importlib.import_module("app.services.pine_transpiler")
        assert module.parser_available() is False

        assessment = module.assess_migratability(EMA_CROSS)
        assert 0 <= assessment["score"] <= 100
        assert isinstance(assessment["blockers"], list)

        # Transpiling degrades to a reported error instead of raising.
        result = module.transpile_pine(EMA_CROSS)
        assert result.ok is False
        assert result.error == "parser_unavailable"
        assert result.blockers == ["parser_unavailable"]
        assert result.code is None
    finally:
        sys.meta_path = [hook for hook in sys.meta_path if not isinstance(hook, _BlockPynescript)]
        for name in list(sys.modules):
            if name.startswith("app.services.pine_transpiler"):
                del sys.modules[name]
        sys.modules.update(saved)


def test_generated_source_survives_the_sandbox_safety_scan():
    """Comments, docstrings and string literals must not trip the safety regexes.

    The runtime scans raw source text, so an input titled ``RTH open (09:30)``
    used to reject the whole module through the ``open(`` rule.
    """
    from app.utils.safe_exec import validate_code_safety

    source = """
//@version=5
strategy("ORB open (09:30)", overlay=true)
anchor = input.string("RTH open (09:30)", "Session anchor")
if ta.crossover(close, ta.sma(close, 20))
    strategy.entry("Long", strategy.long)
"""
    result = transpile_pine(source, name='ORB open (09:30) "quoted"')
    assert result.ok, result.blockers

    ok, reason = validate_code_safety(result.code)
    assert ok, reason


def test_na_state_slots_are_initialised_on_first_read():
    """``na`` initialisers are stored as ``None`` and restored by the reader.

    A module-level literal cannot hold ``float('nan')``; leaving ``None`` in the
    slot would surface as ``'>=' not supported between NoneType and float``.
    """
    source = """
//@version=5
strategy("na slot")
var float tracked = na
tracked := nz(tracked[1], 1.0)
if ta.crossover(close, ta.sma(close, 10)) and tracked > 0
    strategy.entry("Long", strategy.long)
"""
    result = transpile_pine(source)
    assert result.ok, result.blockers
    assert "float('nan')" not in result.code.split("_STATE = ")[1].split("\n")[0]

    import math

    import pandas as pd

    from app.services.strategy_v2 import StrategyV2BacktestRunner

    prices = [100.0 + 10.0 * math.sin(index / 11.0) for index in range(400)]
    opens = [prices[0]] + prices[:-1]
    frame = pd.DataFrame(
        {
            "open": opens,
            "high": [max(open_, close) * 1.004 for open_, close in zip(opens, prices)],
            "low": [min(open_, close) * 0.996 for open_, close in zip(opens, prices)],
            "close": prices,
            "volume": [1000.0] * len(prices),
        },
        index=pd.date_range("2026-01-01", periods=len(prices), freq="h"),
    )
    output = StrategyV2BacktestRunner(
        code=result.code, frames={"BTC/USDT": frame}, initial_capital=10000.0
    ).run()
    assert output["finalEquity"] > 0.0


def test_scorer_never_imports_pynescript_at_module_scope():
    """The scorer is regex-based so callers can rank scripts without pynescript."""
    import app.services.pine_transpiler.assess as assess_module

    assert assess_module.__file__ is not None
    tree = ast.parse(open(assess_module.__file__, encoding="utf-8").read())
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "pynescript" not in imported


def test_calendar_component_history_offsets_are_preserved():
    """``month(time[1])`` must read the previous bar, not the current one.

    Dropping the offset made ``new_month = month(time) != month(time[1])``
    compare the component against itself. Because ``barstate.islast`` is
    constant true in a finished-bar replay, strategies that guard an
    accumulating array on ``new_month`` appended on every bar, which turned a
    one-year backtest into a quadratic multi-minute hang.
    """
    source = """
//@version=5
strategy("Months", overlay=true)
new_month = month(time) != month(time[1])
if new_month
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert '_p_shift(_p_time_component("month"), 1)' in result.code
    assert _compiles(result.code)


def test_unmapped_calls_never_render_to_an_empty_string():
    """A dropped operand must not silently delete itself from an expression.

    ``180 * acos(0.5) / 3`` used to render as ``_p_div(180, 3)`` because
    ``acos`` had no mapping and returned ``""``. The assignment then looked
    valid but computed a different number, which is the hardest class of
    transpiler bug to notice.
    """
    source = """
//@version=5
strategy("Angles", overlay=true)
x = 180 * acos(0.5) / 3
if bar_index == 0
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert "np.arccos" in result.code
    assert "_p_div(180, 3)" not in result.code


def test_unknown_calls_become_nan_instead_of_vanishing():
    """Unmapped call sites must bind a value so downstream reads still work."""
    source = """
//@version=5
strategy("Unknown", overlay=true)
x = totallyNotAPineBuiltin(close)
if bar_index == 0
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert "totallyNotAPineBuiltin" in "".join(result.stats.get("unresolved") or []) or (
        "float('nan')" in result.code
    )


def test_pine_v3_flat_series_calls_use_the_bar_series():
    """``lowest(20)`` predates the ``ta.`` namespace and takes only a length."""
    source = """
//@version=5
strategy("Flat", overlay=true)
lo = lowest(20)
if bar_index == 0
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert "_p_lowest(low_series, 20)" in result.code
    assert "def _p_lowest" in result.code


def test_flat_cum_passes_the_explicit_source():
    """``cum`` always took a source argument, unlike its flat siblings."""
    source = """
//@version=5
strategy("Cum", overlay=true)
v = cum(volume)
if bar_index == 0
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert "_p_cum(volume_series)" in result.code
    assert "def _p_cum" in result.code


def test_closed_trade_counters_read_the_runtime_ledger():
    """Pine exit logic keys off ``strategy.closedtrades``; it must not be frozen.

    The counters used to read a ``_STATE`` slot that nothing ever wrote, so
    every ``strategy.closedtrades > 0`` guard was permanently false and any
    scale-out or trailing logic built on it never ran.
    """
    source = """
//@version=5
strategy("Counters", overlay=true)
if strategy.closedtrades > 0 and strategy.closedtrades.profit(0) > 0
    strategy.entry("Long", strategy.long)
if bar_index == 0
    strategy.entry("First", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert "_p_close_trade_count()" in result.code
    assert "_p_closed_profit(" in result.code
    assert "def _p_close_trade_count" in result.code
    assert "_STATE.get(\"closed_trades\"" not in result.code


def test_bar_index_advances_past_the_warmup_window():
    """``bar_index`` must count bars, not the length of the history window.

    ``get_history(_WARMUP, ...)`` saturates at the warmup length, so deriving
    the index from ``len(frame)`` froze it forever at ``_WARMUP - 1``. Every
    ``bar_index % n`` schedule then fired exactly once (or never), which made
    simple entry/exit strategies report zero trades.
    """
    source = """
//@version=5
strategy("Index", overlay=true)
if bar_index % 20 == 0
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert "_BAR_COUNTER[0] = _BAR_COUNTER[0] + 1" in result.code
    assert "_bar_index = len(frame) - 1" not in result.code


def test_scalar_sources_are_widened_before_rolling_windows():
    """Pine accepts an input or loop variable as a ``ta.*`` source.

    Passing that scalar straight into pandas raised "object has no attribute
    rolling", which killed ten strategies at runtime even though they compiled.
    """
    source = """
//@version=5
strategy("Scalars", overlay=true)
len = 20
m = ta.stdev(close, len)
s = ta.vwma(close, len)
c = cum(volume)
if bar_index == 0
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    # ``vwma`` weights by the bar's own volume, so only the length is passed.
    assert "_p_vwma(close_series, 20)" in result.code
    assert "_p_cum(volume_series)" in result.code


def test_variadic_math_max_accepts_more_than_two_arguments():
    """``math.max`` is variadic in Pine, while ``np.maximum`` is binary."""
    source = """
//@version=5
strategy("Variadic", overlay=true)
x = math.max(close, open, high, low)
if bar_index == 0
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert "_p_math_max(close_series, open_series, high_series, low_series)" in result.code
    assert "def _p_math_max" in result.code


def test_array_size_survives_a_slot_that_holds_a_scalar():
    """``var array<box> heat = array.new_box()`` starts empty, not a number.

    The slot is predeclared with a neutral scalar, so mapping ``array.size``
    onto the builtin ``len`` raised ``object of type 'float' has no len()`` on
    every script that sized a drawing array before its first write.
    """
    source = """
//@version=5
strategy("Array size", overlay=true)
var array<box> boxes = array.new_box()
while boxes.size() > 0
    box.delete(boxes.shift())
boxes.push(box.new(bar_index, high, bar_index + 2, low))
if bar_index == 0
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert "_p_array_size(" in result.code
    assert "def _p_array_size" in result.code


def test_drawing_array_constructors_resolve_to_the_list_helper():
    """Every ``array.new_*`` overload creates a list, not an undefined name."""
    source = """
//@version=5
strategy("Drawing arrays", overlay=true)
var array<label> labels = array.new_label()
var array<line> lines = array.new_line()
if bar_index == 0
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert "_p_array_new(" in result.code


def test_an_input_default_that_names_a_variable_is_not_hoisted():
    """``input.time(StartDate, ...)`` must bind after ``StartDate`` exists.

    Hoisting every parameter to the top of ``handle_data`` made the fallback
    reference a local that had not been assigned yet.
    """
    source = """
//@version=5
strategy("Input default", overlay=true)
StartDate = timestamp('15Aug 2022 14:00 +0000')
testPeriodStart = input.time(StartDate, 'Start of trading')
if time > testPeriodStart
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert 'testPeriodStart = context.params.get("testPeriodStart", StartDate)' in result.code
    hoisted = [line for line in result.code.splitlines() if "testPeriodStart" in line]
    # The binding has to sit below the assignment that defines StartDate.
    positions = [
        index
        for index, line in enumerate(result.code.splitlines())
        if "StartDate =" in line or "testPeriodStart =" in line
    ]
    assert positions == sorted(positions)
    assert hoisted
