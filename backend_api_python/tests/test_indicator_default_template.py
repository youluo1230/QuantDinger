"""The starter template is the first thing a new author sees.

It is also what the IDE writes when the model is unavailable, so it has to be
valid Pine on its own: a template that does not compile would make a fresh
indicator fail before its author typed anything.
"""

from app.services.indicator_default_template import build_default_indicator_template
from app.services.pine_validation import (
    analyze_pine_quality,
    extract_indicator_meta,
    validate_pine_source,
)


def test_default_template_is_valid_pine():
    code = build_default_indicator_template()
    verdict = validate_pine_source(code)
    assert verdict["success"], verdict["details"]


def test_default_template_declares_the_study_and_its_settings():
    code = build_default_indicator_template()
    meta = extract_indicator_meta(code)
    assert meta["name"]

    codes = {hint["code"] for hint in analyze_pine_quality(code)}
    # The template is the reference the author copies, so it must not trip any
    # of the warnings a hand-written script would be flagged for.
    assert "MISSING_PINE_VERSION" not in codes
    assert "MISSING_SCRIPT_DECLARATION" not in codes
    assert "NO_PLOTS_DECLARED" not in codes
    assert "SCRIPT_IS_A_STRATEGY" not in codes


def test_default_template_has_no_python_runtime_left():
    code = build_default_indicator_template()
    for token in ("df[", "params.get", "output =", "my_indicator_name", "# @param"):
        assert token not in code
    # The execution columns belong to strategies; a chart study that mentions
    # them would suggest the indicator trades.
    for column in ("open_long", "close_long", "open_short", "close_short"):
        assert column not in code


def test_default_template_uses_inputs_rather_than_hardcoded_periods():
    code = build_default_indicator_template()
    # Every period in the template must be adjustable from the settings dialog.
    assert "input.int(" in code
    assert "ta.ema(close, fastPeriod)" in code
    assert "ta.ema(close, slowPeriod)" in code


def test_default_template_marks_one_bar_events():
    code = build_default_indicator_template()
    # A continuous state would light up every bar and repeat the alert.
    assert "ta.crossover(" in code
    assert "ta.crossunder(" in code
