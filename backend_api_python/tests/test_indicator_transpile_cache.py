"""Stored indicators are Pine, but the sandbox runs Python.

The chart executes Pine in the browser now, so the server-side path survives for
alerts and for API consumers. That path still has to translate the source, and
translation of a large script costs minutes, which is why it is cached.
"""

from app.services.indicator_params import looks_like_pine
from app.services.indicator_signal_alerts import _transpile_pine


PINE = """
//@version=6
indicator("Cache probe", overlay=true)
len = input.int(14, "Length")
plot(ta.ema(close, len))
"""

LEGACY_PYTHON = """
my_indicator_name = "Legacy"
df = df.copy()
output = {"name": my_indicator_name, "plots": [], "signals": [], "layers": []}
"""


def test_pine_source_is_translated_to_runnable_python():
    translated = _transpile_pine(PINE)
    assert translated != PINE
    # The result is Python: it parses as such, which is what the sandbox needs.
    compile(translated, "<indicator>", "exec")


def test_a_translation_is_reused_rather_than_recomputed(monkeypatch):
    from app.services import indicator_signal_alerts

    calls = {"count": 0}
    real = indicator_signal_alerts._transpile_pine

    source = PINE.replace("14", "21")
    first = real(source)

    # Counting through the cache is not enough -- the point is that the second
    # call does not enter the transpiler at all.
    import app.services.pine_transpiler.chart as chart_module

    def explode(*args, **kwargs):
        calls["count"] += 1
        raise AssertionError("the transpiler ran twice for the same source")

    monkeypatch.setattr(chart_module, "transpile_indicator", explode)
    second = _transpile_pine(source)
    assert first == second
    assert calls["count"] == 0


def test_uncompilable_pine_is_reported_not_swallowed():
    # The message has to name the reason; "execution failed" would send the
    # author looking at the wrong layer.
    bad = '//@version=6\nindicator("Broken")\nplot(close\n'
    try:
        _transpile_pine(bad)
    except ValueError as error:
        assert "could not be compiled" in str(error)
    else:
        raise AssertionError("uncompilable source should raise")


def test_python_rows_are_left_alone():
    # The detector is what keeps an unmigrated indicator's alerts working, so a
    # legacy row must not be sent through the Pine transpiler.
    assert looks_like_pine(PINE) is True
    assert looks_like_pine(LEGACY_PYTHON) is False
