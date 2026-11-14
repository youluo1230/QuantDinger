"""Unit tests for the Pine indicator transpiler.

The strategy transpiler proves its output by compiling it; an indicator has no
compiler, so the load-bearing assertion here is that the emitted module runs in
the real indicator sandbox and returns the drawings the source asked for. A
conversion that merely produces parseable Python proves nothing: the earlier
implementation did exactly that and silently collected zero plots, because the
per-bar statements were nested one level too deep inside a translated function
and never ran.
"""

from __future__ import annotations

import ast

import pytest

from app.services.pine_transpiler import parser_available
from app.services.pine_transpiler.chart import transpile_indicator

pytestmark = pytest.mark.skipif(
    not parser_available(),
    reason="pynescript is not installed in this image",
)


EMA_CROSS = """
//@version=5
indicator("EMA Cross Viewer", overlay=true)
fast = input.int(12, "Fast")
slow = input.int(26, "Slow")
ef = ta.ema(close, fast)
es = ta.ema(close, slow)
plot(ef, "EMA Fast", color=color.green)
plot(es, "EMA Slow", color=color.blue)
plotshape(ta.crossover(ef, es), "Golden", shape.triangleup, location.belowbar, color.green)
"""


def _frame(length: int = 180):
    import numpy as np
    import pandas as pd

    index = pd.date_range("2026-01-01", periods=length, freq="h", tz="UTC")
    rng = np.random.default_rng(11)
    close = 60000 + np.cumsum(rng.normal(0, 150, length))
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 80,
            "low": close - 80,
            "close": close,
            "volume": rng.integers(1, 200, length).astype(float),
        },
        index=index,
    )


def _run(source: str, *, params: dict | None = None):
    """Transpile ``source`` and execute it in the indicator sandbox."""
    from app.utils.safe_exec import safe_exec_indicator_isolated

    result = transpile_indicator(source)
    assert result.ok, (result.error, result.blockers)
    assert result.code
    ast.parse(result.code)
    outcome = safe_exec_indicator_isolated(result.code, _frame(), params or {}, timeout=60)
    assert outcome.get("success"), outcome.get("error")
    return result, (outcome.get("result") or {}).get("output") or {}


def test_a_plot_becomes_a_series_the_chart_can_draw():
    """A ``plot`` call must land as a numeric plot of the frame's length."""
    result, output = _run(EMA_CROSS)

    plots = output.get("plots") or []
    names = [plot.get("name") for plot in plots]
    assert names == ["EMA Fast", "EMA Slow"]
    for plot in plots:
        data = plot.get("data") or []
        assert len(data) == 180, "the contract requires one value per K-line row"
        # An EMA of a 180-bar frame is defined for the last 168 bars; a plot of
        # all ``None`` would mean the buffer was collected but never filled.
        assert sum(1 for value in data if value is not None) > 100
    assert [item.get("title") for item in result.stats["plots"]] == ["EMA Fast", "EMA Slow"]


def test_plotshape_becomes_a_signal_and_never_an_order():
    """Drawing conditions are markers, not trades: the module must not trade."""
    result, output = _run(EMA_CROSS)

    assert "order_target_percent(_SYMBOL" not in result.code.split('"""')[2]
    signals = output.get("signals") or []
    assert [signal.get("text") for signal in signals] == ["Golden"]
    data = (signals[0].get("data") or [])
    assert len(data) == 180
    assert any(value for value in data), "an EMA cross has to fire somewhere"


def test_the_module_reads_its_inputs_from_a_plain_params_mapping():
    """The chart sandbox has no ``context``; an input read must not name one."""
    result = transpile_indicator(EMA_CROSS)

    assert "context.params" not in result.code
    assert 'params.get("fast"' in result.code


def test_the_module_declares_the_constants_the_timeframe_builtins_use():
    """``timeframe.period`` and friends render as module constants.

    Those names are defined by the strategy assembler. A chart module that only
    defined ``_QD_TIMEFRAME`` died with ``NameError: name '_TIMEFRAME'`` the
    moment a script so much as read ``timeframe.period``.
    """
    source = """
//@version=5
indicator("TF probe")
plot(timeframe.period == "60" ? close : close, "close")
"""
    result, output = _run(source)

    for name in ("_TIMEFRAME", "_SYMBOL", "_TIMEFRAME_MULTIPLIER"):
        assert f"\n{name} = " in result.code, name
    assert (output.get("plots") or [{}])[0].get("data")


def test_a_script_that_draws_nothing_is_reported_as_a_failure():
    """An empty pane looks identical to a broken conversion, so it fails."""
    source = """
//@version=5
indicator("Silent")
value = ta.sma(close, 10)
"""
    result = transpile_indicator(source)

    assert result.ok is False
    assert "no_chart_output" in (result.blockers or [result.error])


def test_an_inline_input_stays_out_of_the_strategy_entry_point():
    """The chart assembler must not emit ``initialize``/``handle_data``."""
    result = transpile_indicator(EMA_CROSS)

    assert "def initialize(" not in result.code
    assert "def handle_data(" not in result.code
    assert "_qd_replay(df, _qd_params)" in result.code
