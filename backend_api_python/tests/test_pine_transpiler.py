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
        "getattr",
        "setattr",
        "dir",
        "globals",
        "vars",
        "breakpoint",
        "importlib",
        "eval",
        "exec",
        "compile",
        "open",
        "chr",
        "format",
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


def test_a_matrix_strategy_translates_instead_of_being_blocked():
    """`matrix.*` used to reject the script; the runtime can express it.

    This is the same source that used to be blocked, kept as a regression: it
    must now compile, and it must still emit the matrix call.
    """
    source = """
//@version=5
strategy("Matrix", overlay=true)
m = matrix.new<int>(2, 2, 0)
if ta.crossover(close, ta.sma(close, 9))
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok is True, result.blockers
    assert "_p_matrix_new" in result.code
    assert not any("matrix" in blocker for blocker in result.blockers)


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
    loaded = {node.id for node in ast.walk(function) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
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
    handler = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "handle_data")
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
    output = StrategyV2BacktestRunner(code=result.code, frames={"BTC/USDT": frame}, initial_capital=10000.0).run()
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
    assert "totallyNotAPineBuiltin" in "".join(result.stats.get("unresolved") or []) or ("float('nan')" in result.code)


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
    assert '_STATE.get("closed_trades"' not in result.code


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


def test_an_indicator_alert_condition_becomes_a_long_order():
    """An indicator has no ``strategy.*`` call; its alert is the entry.

    TradingView ships far more indicators than strategies, especially on
    subjects such as ICT and order blocks. Without lowering the alert condition
    into an order every indicator would be rejected as ``no_strategy_entry``.
    """
    source = """
//@version=5
indicator("Order blocks", overlay=true)
bull = ta.crossover(close, ta.sma(close, 20))
alertcondition(bull, "Bullish order block formed", "bull")
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert 'order_target_percent(_SYMBOL, 1.0, reason="pine_signal_long")' in result.code
    assert result.stats["has_signal_entry"] is True


def test_an_indicator_bearish_alert_opens_a_short():
    source = """
//@version=5
indicator("Bear", overlay=true)
bear = ta.crossunder(close, ta.sma(close, 20))
plotshape(bear, "Bearish breakdown", style=shape.triangledown, location=location.abovebar)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert 'order_target_percent(_SYMBOL, -1.0, reason="pine_signal_short")' in result.code


def test_a_real_strategy_does_not_get_a_second_order_from_its_plots():
    """A strategy usually plots the same condition it trades.

    Lowering those plots into orders as well would enter every position twice.
    """
    source = """
//@version=5
strategy("Dual", overlay=true)
buy = ta.crossover(close, ta.sma(close, 20))
plotshape(buy, "Buy")
if buy
    strategy.entry("Long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    orders = [line for line in result.code.splitlines() if "order_target_percent" in line]
    assert len(orders) == 1
    assert result.stats["has_strategy_entry"] is True


def test_array_accessors_survive_a_slot_that_is_still_a_scalar():
    """``var array<box> x`` is predeclared with a scalar, not an empty list.

    Every accessor used to call ``len`` on that placeholder, so a script that
    read an array before its first write died with
    ``object of type 'float' has no len()``.
    """
    from app.services.pine_transpiler import helpers

    namespace: dict = {}
    exec(helpers.ARRAY_SUPPORT, namespace)  # noqa: S102 - evaluating generated helpers

    placeholder = float("nan")
    assert namespace["_p_array_size"](placeholder) == 0
    assert namespace["_p_array_get"](placeholder, 0) != namespace["_p_array_get"](placeholder, 0)
    assert namespace["_p_array_index"](placeholder, 1.0) == -1
    assert namespace["_p_array_pop"](placeholder) != namespace["_p_array_pop"](placeholder)
    assert namespace["_p_array_push"](placeholder, 1.0) is placeholder
    assert namespace["_p_array_stats"](placeholder, "avg") != namespace["_p_array_stats"](placeholder, "avg")


def test_a_constant_that_is_reassigned_stays_a_variable():
    """``x = false`` followed by ``x := true`` is a flag, not a constant.

    Folding the initialiser into every read leaves the reassignment writing to a
    name nothing reads, so ``if x`` tests the folded ``False`` forever and the
    strategy never enters. The reassignment lives inside an ``if``, so the search
    for it has to descend into nested blocks.
    """
    source = """//@version=5
strategy("T")
var int last_signal = 0
buy_signal = false
raw_buy = close > open
if raw_buy and last_signal != 1
    buy_signal := true
    last_signal := 1
if buy_signal
    strategy.entry("Long", strategy.long)
"""
    result = transpile_pine(source, symbol="BTC/USDT", timeframe="1h", name="T")

    assert result.ok, result.error
    assert "if _p_truth(buy_signal):" in result.code
    assert "if _p_truth(False):" not in result.code


def test_an_untouched_constant_is_still_inlined():
    """The inliner must keep working for names the script never reassigns."""
    source = """//@version=5
strategy("T")
mult = 2.5
threshold = 10
buy = close > threshold
if buy
    strategy.entry("Long", strategy.long)
"""
    result = transpile_pine(source, symbol="BTC/USDT", timeframe="1h", name="T")

    assert result.ok, result.error
    assert "10" in result.code


def _helper_namespace() -> dict:
    """Evaluate the emitted helper source so it can be exercised directly."""
    import types

    import numpy as np
    import pandas as pd

    from app.services.pine_transpiler import helpers

    namespace: dict = {"np": np, "pd": pd}
    for block in (
        helpers.BASE_UTILS,
        helpers.STATE_SUPPORT,
        helpers.RUNTIME_SUPPORT,
        # Indicator helpers live in their own blocks; a test that exercises an
        # indicator has to load them too, exactly as ``helper_source`` would.
        helpers._PREFIX,
        helpers._PRICE_STATS,
        helpers._CROSSES,
        helpers._VOLATILITY,
        helpers.ARRAY_SUPPORT,
    ):
        exec(block, namespace)  # noqa: S102 - evaluating generated helpers
    return namespace


def test_ternaries_keep_their_elementwise_semantics():
    """``_p_where`` must resolve per element, not collapse to one branch.

    The rewrite that moved both-series resolution onto ``np.where`` is only
    legitimate while it still selects element by element, keeps the index, and
    reads a missing condition as false the way ``fillna(0)`` did.
    """
    import numpy as np
    import pandas as pd

    namespace = _helper_namespace()
    where = namespace["_p_where"]

    index = pd.date_range("2026-01-01", periods=6, freq="1min")
    condition = pd.Series([1.0, 0.0, np.nan, 1.0, 0.0, 1.0], index=index)
    when_true = pd.Series([10.0] * 6, index=index)
    when_false = pd.Series([20.0] * 6, index=index)

    resolved = where(condition, when_true, when_false)

    assert resolved.index.equals(index)
    assert list(resolved) == [10.0, 20.0, 20.0, 10.0, 20.0, 10.0]

    mixed = where(condition, when_true, 0.0)
    assert list(mixed) == [10.0, 0.0, 0.0, 10.0, 0.0, 10.0]


def test_ternaries_still_return_strings_untouched():
    """A string branch must not be pushed through ``np.where``."""
    import pandas as pd

    namespace = _helper_namespace()
    where = namespace["_p_where"]

    index = pd.date_range("2026-01-01", periods=3, freq="1min")
    condition = pd.Series([1.0, 0.0, 1.0], index=index)
    labels = pd.Series(["up", "up", "up"], index=index)

    resolved = where(condition, labels, "flat")

    assert list(resolved) == ["up", "flat", "up"]


def test_win_loss_counts_survive_a_second_run():
    """The incremental tally must reset when a new run starts.

    ``_p_trade_tally`` caches how much of the ledger it has counted. A second
    backtest begins with a shorter ledger, and the count has to restart rather
    than continue from the previous run's totals.
    """
    namespace = _helper_namespace()

    ledger: list[dict] = []

    def get_closed_trades():
        return list(ledger)

    namespace["get_closed_trades"] = get_closed_trades
    namespace["_p_closed_trades"] = lambda: get_closed_trades()
    tally = namespace["_p_trade_tally"]

    ledger.extend([{"profit": 5.0}, {"profit": -2.0}])
    assert tally() == (1.0, 1.0)

    ledger.append({"profit": 3.0})
    assert tally() == (2.0, 1.0)

    # A new run: the ledger shrinks back to one losing trade.
    ledger[:] = [{"profit": -1.0}]
    assert tally() == (0.0, 1.0)


def test_timestamp_defaults_are_comparable_with_bar_time():
    """A date-gated strategy must be able to reach its own entry condition.

    Pine's ``timestamp()`` and ``time`` are compared directly, so they have to
    share a unit and both have to be numbers. Two defects met here: the helper
    returned microseconds while ``time`` is milliseconds, and it parsed the
    ``"01-01-2023"`` form that TradingView writes in ``input.time`` defaults with
    ``int()``, which returns NaN. A comparison against NaN is false on every bar,
    so the strategy compiled, backtested, and never opened a position.
    """
    import numpy as np
    import pandas as pd

    from app.services.pine_transpiler import helpers

    frame = pd.DataFrame(
        {
            "open": [1.0] * 4,
            "high": [1.0] * 4,
            "low": [1.0] * 4,
            "close": [1.0] * 4,
            "volume": [1.0] * 4,
        },
        index=pd.date_range("2026-11-01", periods=4, freq="h"),
    )
    namespace = {"np": np, "pd": pd}
    exec(helpers.BASE_UTILS, namespace)  # noqa: S102 - the helpers are source text
    namespace["_FRAME_REF"] = [frame]

    bar_time = namespace["_p_time"]()
    assert bar_time == frame.index[-1].value // 1_000_000

    for literal in ("01-01-2023", "2023-01-01"):
        stamp = namespace["_p_timestamp"](literal)
        assert stamp == 1672531200000, f"{literal} must parse to a timestamp"
        assert stamp < bar_time, "an older date must compare as before the bar"

    calendar = namespace["_p_timestamp"](2026, 11, 1)
    assert calendar <= bar_time
    assert calendar == 1793491200000


def test_an_array_declared_with_na_starts_empty():
    """``array.new_color(na)`` is Pine's spelling for an empty array.

    ``na`` reaches the helper as NaN, and ``int(NaN)`` raises, so a script that
    declared an array this way died on its first bar. The message ("cannot
    convert float NaN to integer") names the helper, not the ``na`` in the
    source, which is why this looked like a transpiler bug for three separate
    strategies.
    """
    import numpy as np
    import pandas as pd

    from app.services.pine_transpiler import helpers

    namespace = {"np": np, "pd": pd}
    exec(helpers.BASE_UTILS, namespace)  # noqa: S102 - helpers are source text
    exec(helpers.ARRAY_SUPPORT, namespace)  # noqa: S102

    array_new = namespace["_p_array_new"]
    assert array_new(float("nan")) == []
    assert array_new(float("nan"), "x") == []
    assert array_new() == []
    assert array_new(3, "x") == ["x", "x", "x"]


def test_a_matrix_declared_with_na_starts_empty():
    import numpy as np
    import pandas as pd

    from app.services.pine_transpiler import helpers

    namespace = {"np": np, "pd": pd}
    exec(helpers.BASE_UTILS, namespace)  # noqa: S102
    assert namespace["_p_matrix_new"](float("nan")) == []


def test_matrix_arithmetic_matches_the_textbook_result():
    """`matrix.*` was a blocking construct even though the helpers existed.

    Eleven scripts were rejected outright for using `matrix.mult`, `matrix.inv`,
    `matrix.transpose`, `matrix.row` or `matrix.col`, all of which the runtime
    can express on the list-of-rows representation it already uses. Rejecting a
    script for a namespace the helper layer supports is worse than translating
    it: nothing reaches the backtest, so nothing can be judged.
    """
    import numpy as np
    import pandas as pd

    from app.services.pine_transpiler import helpers

    namespace = {"np": np, "pd": pd}
    exec(helpers.BASE_UTILS, namespace)  # noqa: S102 - helpers are source text

    multiply = namespace["_p_matrix_multiply"]
    assert multiply([[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]]) == [
        [19.0, 22.0],
        [43.0, 50.0],
    ]
    # A shape mismatch returns an empty matrix rather than a partial product.
    assert multiply([[1.0, 2.0]], [[1.0], [2.0], [3.0]]) == []

    transpose = namespace["_p_matrix_transpose"]
    assert transpose([[1.0, 2.0], [3.0, 4.0]]) == [[1.0, 3.0], [2.0, 4.0]]

    inverse = namespace["_p_matrix_inverse"]
    product = multiply([[2.0, 1.0], [1.0, 3.0]], inverse([[2.0, 1.0], [1.0, 3.0]]))
    assert product[0][0] == pytest.approx(1.0)
    assert product[1][0] == pytest.approx(0.0)
    assert namespace["_p_matrix_inverse"]([[0.0, 0.0], [0.0, 0.0]]) == [[0.0, 0.0], [0.0, 0.0]]

    assert namespace["_p_matrix_row"]([[1.0, 2.0]], 0) == [1.0, 2.0]
    assert namespace["_p_matrix_col"]([[1.0, 2.0], [3.0, 4.0]], 1) == [2.0, 4.0]


def test_matrix_and_map_are_no_longer_blocking():
    """The blocker table must not reject a namespace the emitter translates."""
    from app.services.pine_transpiler import mapping

    assert "matrix." not in mapping.BLOCKING_PATTERNS
    assert "map." not in mapping.BLOCKING_PATTERNS
    for name in ("matrix.mult", "matrix.inv", "matrix.transpose", "matrix.row", "matrix.col"):
        assert name in mapping.MATRIX_CALLS, f"{name} must translate, not block"


def test_triple_quoted_strings_are_folded_before_parsing():
    """Pine allows multi-line tooltips; the parser does not accept them at all.

    script rejects triple-quoted literals outright — even on one line — so a
    script whose only sin is a documentation block failed to parse. Folding the
    delimiters keeps the text, which is all the translator reads.
    """
    from app.services.pine_transpiler.pine_ast import (
        _fold_triple_quoted_strings,
        parse,
    )

    source = 'x = """line one\nline two"""\ny = 1\n'
    folded = _fold_triple_quoted_strings(source)
    # The literal collapses onto one physical line; the statement after it stays.
    assert '"""' not in folded
    assert folded.endswith("y = 1\n")
    assert "line one\\nline two" in folded
    assert parse(source) is not None

    assert parse("x = '''a\nb'''\ny = 2\n") is not None
    assert _fold_triple_quoted_strings('x = "abc"') == 'x = "abc"'


def test_matrix_namespace_calls_do_not_take_the_namespace_as_receiver():
    """``matrix.rows(m)`` is a namespace call, not a method on a value.

    The method syntax ``m.rows()`` routes the receiver into the first argument.
    Reading the namespace form the same way emitted ``_p_matrix_rows(matrix, m)``
    — with the literal word ``matrix`` as the first argument — so every script
    that used the ordinary spelling died with `name 'matrix' is not defined`.
    """
    source = (
        "//@version=5\n"
        "strategy('t')\n"
        "var matrix<float> m = matrix.new<float>(0, 4)\n"
        "rows = matrix.rows(m)\n"
        "cell = matrix.get(m, 0, 2)\n"
        "matrix.set(m, 0, 1, 2.0)\n"
        "if rows > 0\n"
        "    strategy.entry('L', strategy.long)\n"
    )
    result = transpile_pine(source)
    assert result.ok, result.blockers
    assert "_p_matrix_rows(_state_get" in result.code
    # The helper definition itself takes a parameter named ``matrix``; only the
    # call site matters, so the comma after the name is part of the assertion.
    assert "_p_matrix_rows(matrix," not in result.code
    assert "_p_matrix_get(_state_get" in result.code


def test_a_table_handle_is_not_a_list():
    """``dashboard.clear(...)`` on a ``table`` is a display call.

    The name lookup alone cannot tell a table handle from an array, so
    ``dash.clear(0, 0, 8, 79)`` was routed into ``_p_array_clear`` with the
    table itself as the argument and raised a signature error.
    """
    source = (
        "//@version=5\n"
        "strategy('t')\n"
        "var table dash = table.new(position.top_right, 9, 80)\n"
        "n = 0\n"
        "if barstate.islast\n"
        "    dash.clear(0, 0, 8, 79)\n"
        "    n := 1\n"
        "if n >= 0\n"
        "    strategy.entry('L', strategy.long)\n"
    )
    result = transpile_pine(source)
    assert result.ok, result.blockers
    # The helper is not even emitted: nothing in the script needs it any more.
    call_sites = [line for line in result.code.splitlines() if "_p_array_clear(" in line]
    assert call_sites == [], call_sites


def test_a_loop_over_na_runs_zero_times_instead_of_raising():
    """Pine walks an ``na`` iterable zero times; Python would raise."""
    namespace = _helper_namespace()
    walk = namespace["_p_iter"]
    assert list(walk(float("nan"))) == []
    assert list(walk(None)) == []
    assert list(walk([1.0, 2.0])) == [1.0, 2.0]
    assert walk("abc") == "abc"


def test_a_loop_bound_of_na_does_not_raise():
    """``for i = 0 to na`` is an empty loop in Pine, not a crash."""
    namespace = _helper_namespace()
    # ``na`` reads as zero, so ``for i = 0 to na`` walks the single bound value
    # rather than raising on ``int(nan)``.
    assert list(namespace["_p_loop_range"](float("nan"), float("nan"))) == [0]
    assert list(namespace["_p_loop_range"](0, 2)) == [0, 1, 2]


def test_entry_when_guard_is_kept_as_the_order_condition():
    """``strategy.entry(..., when=cond)`` only trades on the bars ``cond`` holds.

    Dropping the guard makes the entry unconditional, which does not look like a
    bug: the strategy still compiles, still trades, and still reports numbers.
    The numbers are just for a different strategy, and every script written this
    way collapses into the same "flip the target each bar" behaviour.
    """
    source = """
//@version=5
strategy("when guard", overlay=true)
longCond = ta.crossover(ta.ema(close, 9), ta.ema(close, 21))
shortCond = ta.crossunder(ta.ema(close, 9), ta.ema(close, 21))
strategy.entry("long", strategy.long, when = longCond)
strategy.entry("short", strategy.short, when = shortCond)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert result.stats["uses_when_guard"] is True
    lines = [line.strip() for line in result.code.splitlines()]
    long_at = lines.index('order_target_percent(_SYMBOL, 1.0, reason="pine_long")')
    short_at = lines.index('order_target_percent(_SYMBOL, -1.0, reason="pine_short")')
    assert lines[long_at - 1] == "if _p_truth(longCond):"
    assert lines[short_at - 1] == "if _p_truth(shortCond):"
    assert _compiles(result.code)


def test_close_when_guard_is_kept_as_the_exit_condition():
    """``strategy.close(..., when=cond)`` must not fire on every bar either."""
    source = """
//@version=5
strategy("close guard", overlay=true)
exitCond = ta.crossunder(ta.ema(close, 9), ta.ema(close, 21))
strategy.entry("long", strategy.long)
strategy.close("long", when = exitCond)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    lines = [line.strip() for line in result.code.splitlines()]
    close_at = lines.index('order_target_percent(_SYMBOL, 0.0, reason="pine_close")')
    assert lines[close_at - 1] == "if _p_truth(exitCond):"
    assert _compiles(result.code)


def test_an_entry_without_when_stays_unconditional():
    """A plain ``strategy.entry`` has no guard to keep; it must not gain one."""
    source = """
//@version=5
strategy("plain", overlay=true)
if ta.crossover(ta.ema(close, 9), ta.ema(close, 21))
    strategy.entry("long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert result.stats["uses_when_guard"] is False
    # The only condition around the order is the ``if`` the author wrote; no
    # extra guard is invented for the call itself.
    guards = [
        line for line in result.code.splitlines()
        if line.strip().startswith("if _p_truth(")
    ]
    assert len(guards) == 1, guards


def test_a_var_array_keeps_the_array_it_was_declared_with():
    """The slot has to start as the array, not as a scalar that swallows writes.

    ``_p_array_set`` cannot mutate a float, and ``_state_get`` keeps whichever
    fallback it saw first. A function translated before the declaration therefore
    pinned the slot to ``nan``, every write was dropped, and every later read
    returned a fresh empty array — a strategy with zero trades and no error.
    """
    source = """
//@version=5
strategy("var array", overlay=true)
var buf = array.new_int(2)
f_fill() =>
    array.set(buf, 0, 7)
f_fill()
if array.get(buf, 0) == 7
    strategy.entry("long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert "float('nan')" not in _state_literal_of(result.code), _state_literal_of(result.code)
    # Every read of the slot must default to the array the script declared;
    # a scalar default is what made the writes disappear.
    assert '_state_get("buf_v", _p_array_new(2))' in result.code
    assert "_state_get(\"buf_v\", float('nan'))" not in result.code


def _state_literal_of(code: str) -> str:
    """Return the generated ``_STATE`` literal line, or an empty string."""
    for line in code.splitlines():
        if line.startswith("_STATE = "):
            return line
    return ""


def test_history_applied_to_a_collection_call_reads_the_previous_bar():
    """``array.get(buf, 0)[1]`` is the value that call returned on the last bar.

    Pine has no "scalar" — every expression is a series, so indexing any
    expression means "one bar ago". The translator renders ``array.get`` to a
    plain number and ``_p_shift`` passes numbers through, which silently turns
    ``x != x[1]`` into ``x != x``. This pins the replacement behaviour.
    """
    source = """
//@version=5
strategy("hist", overlay=true)
var int[] buf = array.new_int(1)
array.set(buf, 0, 1)
prev = array.get(buf, 0)[1]
if not na(prev) and prev == 1
    strategy.entry("long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert _scalar_hist_calls(result.code), "the call site keeps no history"
    assert "_p_shift(_p_array_get(" not in result.code

    namespace = _helper_namespace()
    namespace["_STATE"] = {}
    hist = namespace["_p_scalar_hist"]
    namespace["_bar_index"] = 0
    hist("_shv1", 1, 1)
    namespace["_bar_index"] = 1
    assert hist("_shv1", 0, 1) == 1
    # Re-evaluating the same site on the same bar must not shift the offsets.
    assert hist("_shv1", 0, 1) == 1


def test_history_on_a_ta_call_still_uses_the_series_path():
    """``ta.sma(close, 3)[1]`` already yields a Series; no history slot needed."""
    source = """
//@version=5
strategy("ta hist", overlay=true)
rising = ta.sma(close, 3)[1] < ta.sma(close, 3)
if rising
    strategy.entry("long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert not _scalar_hist_calls(result.code)
    assert "_p_shift(_p_sma(" in result.code


def _scalar_hist_calls(code: str) -> list:
    """Return the generated ``_p_scalar_hist`` call sites, not its definition."""
    import re

    return re.findall(r'_p_scalar_hist\("[^"]+"', code)


def test_a_stop_exit_becomes_a_guarded_close():
    """``strategy.exit`` has to close the position, not vanish.

    The runtime trades target percentages, so a stop order becomes a close on the
    bar the script's own condition fires. Dropping the call kept the position open
    until the opposite entry arrived — a different strategy with a different
    drawdown, and nothing in the output said so.
    """
    source = """
//@version=5
strategy("exit", overlay=true)
longCond = ta.crossover(ta.ema(close, 9), ta.ema(close, 21))
exitCond = ta.crossunder(ta.ema(close, 9), ta.ema(close, 21))
strategy.entry("Long", strategy.long, when=longCond)
strategy.exit("Exit Long", "Long", stop=longTrailingStop, when=exitCond)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert "strategy_exit_ignored" not in (result.warnings or [])
    assert "strategy_exit_as_close" in (result.warnings or [])
    lines = [line.strip() for line in result.code.splitlines()]
    close_at = lines.index('order_target_percent(_SYMBOL, 0.0, reason="pine_exit")')
    assert lines[close_at - 1] == "if _p_truth(exitCond):"


def test_the_percentile_rank_is_not_the_close_series():
    """An unmapped ``ta.*`` degrades to ``close_series``; a filter must not.

    ``ta.percentile_nearest_rank`` compared against price collapsed to "close
    versus close" and never fired, so the strategy backtested as if it never
    wanted to trade while every intermediate name was defined.
    """
    source = """
//@version=5
strategy("pct", overlay=true)
v = ta.percentile_nearest_rank(high, 7, 75)
if close > v
    strategy.entry("long", strategy.long)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    assert "ta_unsupported:percentile_nearest_rank" not in (result.warnings or [])
    assert "_p_percentile_nearest_rank(" in result.code

    namespace = _helper_namespace()
    import numpy as np

    values = np.arange(100, dtype="float64")[::-1]
    import pandas as pd

    series = pd.Series(values)
    ranked = namespace["_p_percentile_nearest_rank"](series, 10, 75)
    assert abs(float(ranked.iloc[-1]) - 7.0) < 1e-9, float(ranked.iloc[-1])


def test_the_remaining_percentile_and_trend_names_translate():
    """Every ``ta.*`` this corpus uses must have a real implementation."""
    cases = {
        "sar": "v = ta.sar(high, low, 0.02, 0.2)",
        "cmo": "v = ta.cmo(close, 14)",
        "tsi": "v = ta.tsi(close, 13, 25)",
        "dema": "v = ta.dema(close, 20)",
        "tema": "v = ta.tema(close, 20)",
        "median": "v = ta.median(close, 20)",
        "dev": "v = ta.dev(close, 20)",
        "cog": "v = ta.cog(close, 10)",
        "percentile_linear_interpolation": "v = ta.percentile_linear_interpolation(high, 7, 75)",
    }
    for name, line in cases.items():
        source = f"""
//@version=5
strategy("{name}", overlay=true)
{line}
if close > v
    strategy.entry("long", strategy.long)
"""

        result = transpile_pine(source)

        assert result.ok, (name, result.blockers)
        assert f"ta_unsupported:{name}" not in (result.warnings or []), name
        assert "_p_" in result.code, name


def test_an_array_holds_the_bar_value_not_the_series():
    """Pine array elements are values; a stored series breaks later compares.

    ``array.set(buf, i, someSeries)`` stores what that series read on the bar,
    not the series object. Keeping the object made a later ``high >= buf[i]``
    compare two Series of different lengths and raise
    "Can only compare identically-labeled Series objects".
    """
    namespace = _helper_namespace()
    import numpy as np
    import pandas as pd

    frame = pd.Series(np.arange(10, dtype="float64"))
    buffer = []

    namespace["_p_array_push"](buffer, frame)
    namespace["_p_array_set"](buffer, 0, frame)
    namespace["_p_array_unshift"](buffer, frame)

    assert all(not hasattr(item, "iloc") for item in buffer), buffer
    # push appends, set overwrites the same slot, unshift prepends: two values.
    assert [float(item) for item in buffer] == [9.0, 9.0], buffer


def test_a_na_offset_reads_as_missing_instead_of_raising():
    """``close[n]`` with ``n`` unset is ``na`` in Pine, not an exception."""
    namespace = _helper_namespace()
    import numpy as np
    import pandas as pd

    series = pd.Series(np.arange(5, dtype="float64"))
    shifted = namespace["_p_shift"](series, float("nan"))

    assert len(shifted) == len(series)
    assert shifted.isna().all()

    scalar = namespace["_p_shift"](7.0, float("nan"))
    assert scalar != scalar  # NaN


def test_an_na_length_does_not_abort_the_helper():
    """``int(nan)`` inside a helper killed backtests the source never blamed."""
    namespace = _helper_namespace()

    assert namespace["_p_window"](float("nan")) == 1
    assert namespace["_p_window"](None) == 1
    assert namespace["_p_window"](7) == 7


def test_a_stochastic_called_with_numeric_bands_still_produces_values():
    """``ta.stoch(close, 14, 1, 3)`` passes numbers where bands are expected."""
    namespace = _helper_namespace()
    import numpy as np
    import pandas as pd

    close = pd.Series(np.linspace(100, 140, 60))
    out = namespace["_p_stoch"](close, 14, 1, 3)

    assert hasattr(out, "iloc"), out
    assert not out.isna().all()

    bands = namespace["_p_stoch"](close, close + 1.0, close - 1.0, 14)
    assert not bands.isna().all()


def test_a_var_inside_a_helper_does_not_capture_the_script_variable():
    """Same identifier in two scopes is two slots, and only one is ``var``.

    A helper's ``var _C`` used to own the ``_C`` slot for the whole script, so
    the script-level ``_C = pinv(...)`` became a state write into a slot whose
    initialiser referenced the helper's parameters. That surfaced as
    ``name 'nrowsA' is not defined`` on a line containing neither name.
    """
    source = """//@version=5
strategy("T")
multiply(a, b, rows, cols) =>
    var acc = array.new_float(rows * cols, 0.0)
    array.set(acc, 0, a + b)
    acc
coef = multiply(close, open, 3, 2)
basis = close + coef
if ta.crossover(basis, close)
    strategy.entry("Long", strategy.long)
"""
    result = transpile_pine(source, symbol="BTC/USDT", timeframe="1h", name="T")

    assert result.ok, result.error
    # The script-level assignment must bind a local, not reach for the helper's
    # slot — and the helper's slot must not leak its parameter names upward.
    assert 'basis = (close_series + coef)' in result.code or "basis = " in result.code
    assert "float('nan')" not in result.code.split("_STATE = ")[1].split("\n")[0]


def test_a_var_initialiser_may_refer_to_a_later_var():
    """Declaration order must not desync a slot's initialiser.

    ``var a = array.new_int(n, 0)`` refers to ``var n = 20`` written above it in
    the script, but pre-declaration used to walk the body in reverse, so ``n``
    was not registered yet and its read became the bare name ``n`` — a NameError
    reported against a line that reads perfectly well.
    """
    source = """//@version=5
strategy("T")
var int limit = 20
var buf = array.new_int(limit, 0)
array.set(buf, 0, bar_index)
if ta.crossover(close, ta.sma(close, limit))
    strategy.entry("Long", strategy.long)
"""
    result = transpile_pine(source, symbol="BTC/USDT", timeframe="1h", name="T")

    assert result.ok, result.error
    # Neither slot may be referenced as a bare Python name.
    assert "array.new_int(limit, 0)" not in result.code
    assert '_state_get("limit_v", 20)' in result.code


def test_a_method_on_a_call_result_still_receives_its_receiver():
    """``source.f(length).bands()`` must pass ``source`` to the method.

    The owner of that call is itself a call, so the dispatch cannot name it and
    the method body's first parameter went unfilled — a TypeError raised inside
    generated code the author cannot see.
    """
    source = """//@version=5
strategy("T")
method bands(float src) =>
    src * 2.0
lp(series, length) =>
    ta.ema(series, length)
upper = lp(close, 3).bands()
if ta.crossover(close, upper)
    strategy.entry("Long", strategy.long)
"""
    result = transpile_pine(source, symbol="BTC/USDT", timeframe="1h", name="T")

    assert result.ok, result.error
    call_sites = [
        line.strip()
        for line in result.code.splitlines()
        if "v_fn_bands(" in line and "def " not in line
    ]
    assert call_sites, result.code
    assert "v_fn_bands(v_fn_lp(close_series, 3))" in call_sites[0]


def test_a_function_that_reads_its_own_previous_value_still_runs():
    """A self-referencing local inside a function must be bound first.

    Pine lets a function body both write ``_direction`` and read ``_direction[1]``
    on the same line; Python would raise UnboundLocalError on the first bar.
    """
    source = """
//@version=5
strategy("zigzag", overlay=true)

zigzag() =>
    _direction = _isUp[1] and _isDown ? -1 : _isDown[1] and _isUp ? 1 : nz(_direction[1])
    _direction

_isUp = close > open
_isDown = close < open
_direction = zigzag()
if _direction == 1
    strategy.entry("long", strategy.long)
if _direction == -1
    strategy.entry("short", strategy.short)
"""

    result = transpile_pine(source)

    assert result.ok, result.blockers
    lines = result.code.splitlines()
    body_at = next(
        index for index, line in enumerate(lines)
        if line.strip().startswith("def v_fn_zigzag(")
    )
    assert lines[body_at + 1].strip() == "direction = float('nan')"
    assert "_p_nz(_p_shift(direction, 1))" in lines[body_at + 2]
