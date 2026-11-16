"""Indicator validation is a compile check on the Pine source.

Indicators run in the browser now, so the backend cannot inspect the plots they
draw. What it can still do is refuse a script that will not compile, which is
what these cases pin: the shape of the result object every caller depends on,
and the specific reasons a script is turned away.
"""

from __future__ import annotations

from app.routes.indicator import _validate_indicator_code_internal


PLOT_ONLY = """
//@version=6
indicator("Sample", overlay=true)
plot(ta.sma(close, 14), color=#12B76A)
"""


def test_validation_accepts_a_script_that_compiles():
    result = _validate_indicator_code_internal(PLOT_ONLY)
    assert result["success"] is True
    assert not result["error_type"]
    # Nothing was executed, so the counts are the honest zero rather than a
    # number guessed from the source.
    assert result["plots_count"] == 0
    assert result["signals_count"] == 0


def test_validation_rejects_source_that_does_not_compile():
    # An unbalanced call is the most common editor mistake, and the parser
    # reports the position so the IDE can put the cursor there.
    result = _validate_indicator_code_internal(
        "//@version=6\nindicator(\"Broken\")\nplot(close\n"
    )
    assert result["success"] is False
    assert result["error_type"] == "PineParseError"
    assert result["details"]


def test_validation_accepts_a_legacy_version_the_chart_can_translate():
    # The runtime executes v5 and v6, but the frontend upgrades an older script
    # before running it, so the save path must not refuse what the chart draws.
    result = _validate_indicator_code_internal(
        "//@version=4\nstudy(\"Legacy\")\nplot(close)\n"
    )
    assert result["success"] is True
    assert not result["error_type"]


def test_validation_rejects_empty_source():
    result = _validate_indicator_code_internal("   \n\t ")
    assert result["success"] is False
    assert result["error_type"] == "EmptyCode"


def test_validation_keeps_the_result_shape_its_callers_read():
    # The Agent API and the IDE's quality panel both read these keys, so a
    # rename here would break them silently.
    result = _validate_indicator_code_internal(PLOT_ONLY)
    assert set(result) == {
        "success",
        "msg",
        "error_type",
        "details",
        "plots_count",
        "signals_count",
        "hints",
    }


def test_validation_warns_without_rejecting_when_the_declaration_is_missing():
    # A bare plot still draws; the declaration only supplies the study a name.
    result = _validate_indicator_code_internal("//@version=6\nplot(close)\n")
    assert result["success"] is True
    codes = {hint.get("code") for hint in result["hints"]}
    assert "MISSING_SCRIPT_DECLARATION" in codes


def test_validation_flags_a_strategy_rather_than_refusing_it():
    # Strategies are backtested server-side; the same source is still a valid
    # study to preview, so this is advice, not an error.
    result = _validate_indicator_code_internal(
        "//@version=6\nstrategy(\"S\")\nplot(close)\n"
    )
    assert result["success"] is True
    codes = {hint.get("code") for hint in result["hints"]}
    assert "SCRIPT_IS_A_STRATEGY" in codes
