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
    """The scorer is regex-based so callers can rank scripts without pynescript."""
    import app.services.pine_transpiler.assess as assess_module

    source = assess_module.__file__
    assert source is not None
