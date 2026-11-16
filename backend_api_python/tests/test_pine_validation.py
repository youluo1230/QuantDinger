"""Tests for the Pine validation that replaced the Python deny-list.

The point of these is that the IDE used to accept anything: ``validate_code_safety``
rejects Python idioms, and a Pine script contains none of them, so every script
passed. What is asserted here is that broken Pine is now refused and valid Pine is
still accepted -- including the near-misses that a naive check would get wrong.
"""

import pytest

from app.services.pine_validation import (
    analyze_pine_quality,
    declaration_of,
    extract_indicator_meta,
    parse_pine_params,
    pine_version,
    validate_pine_source,
)


VALID = '//@version=5\nindicator("My Study")\nplot(close)\n'


def test_a_compiling_script_is_accepted():
    verdict = validate_pine_source(VALID)
    assert verdict["success"] is True
    assert verdict["hints"] == []


def test_an_unbalanced_call_is_refused():
    verdict = validate_pine_source('//@version=5\nindicator("B")\nplot(close')
    assert verdict["success"] is False
    assert verdict["error_type"] == "PineParseError"
    # The author needs the position, not "syntax error somewhere".
    assert "line 3" in verdict["details"]


def test_an_empty_script_is_refused_rather_than_treated_as_a_draft():
    verdict = validate_pine_source("   \n\n  ")
    assert verdict["success"] is False
    assert verdict["error_type"] == "EmptySource"


def test_a_legacy_pine_version_is_accepted_because_the_chart_translates_it():
    # The browser runtime runs v5 and v6, but the frontend translates an older
    # script before running it. Refusing it here would block a script the chart
    # can actually draw.
    verdict = validate_pine_source('//@version=4\nstudy("Old")\nplot(close)')
    assert verdict["success"] is True
    assert not verdict["error_type"]


def test_a_prehistoric_pine_version_is_still_refused():
    # v1 has no `//@version` convention at all and no `study`/`indicator`
    # declaration the panel can name the script from.
    verdict = validate_pine_source('//@version=1\nplot(close)')
    assert verdict["success"] is False
    assert verdict["error_type"] == "UnsupportedVersion"
    assert "version 1" in verdict["details"]


def test_a_future_version_is_accepted_with_a_warning():
    verdict = validate_pine_source('//@version=7\nindicator("New")\nplot(close)')
    assert verdict["success"] is True
    assert "UNKNOWN_PINE_VERSION" in {hint["code"] for hint in verdict["hints"]}


def test_a_strategy_is_accepted_but_flagged():
    verdict = validate_pine_source('//@version=5\nstrategy("S")\nplot(close)')
    assert verdict["success"] is True
    assert "SCRIPT_IS_A_STRATEGY" in {hint["code"] for hint in verdict["hints"]}


def test_a_script_that_draws_nothing_is_flagged():
    hints = analyze_pine_quality('//@version=5\nindicator("Quiet")\nx = close + 1')
    assert "NO_PLOTS_DECLARED" in {hint["code"] for hint in hints}


@pytest.mark.parametrize(
    "line",
    ['plot(close)', 'plotshape(x)', 'bgcolor(close > open ? color.red : na)', 'hline(70)'],
)
def test_drawing_calls_are_recognised(line):
    source = '//@version=5\nindicator("D")\n' + line + "\n"
    assert "NO_PLOTS_DECLARED" not in {hint["code"] for hint in analyze_pine_quality(source)}


def test_the_declared_title_names_the_indicator():
    # The name TradingView shows is the one the author typed into the
    # declaration; the library listing must not invent a different one.
    assert extract_indicator_meta(VALID)["name"] == "My Study"


def test_a_title_keyword_is_read_like_a_positional_title():
    source = '//@version=5\nindicator(title="Named", overlay=true)\nplot(close)'
    assert extract_indicator_meta(source)["name"] == "Named"


def test_the_legacy_assignment_still_names_a_script_without_a_declaration():
    source = '//@version=5\nmy_indicator_name = "Legacy"\nmy_indicator_description = "d"\nplot(close)'
    meta = extract_indicator_meta(source)
    assert meta["name"] == "Legacy"
    assert meta["description"] == "d"
    # The declaration wins when both are present, because it is what runs.
    both = '//@version=5\nindicator("Current")\nmy_indicator_name = "Stale"\nplot(close)'
    assert extract_indicator_meta(both)["name"] == "Current"


def test_a_missing_declaration_is_reported_but_not_fatal():
    verdict = validate_pine_source('//@version=5\nplot(close)')
    assert verdict["success"] is True
    assert {"severity": "warn", "code": "MISSING_SCRIPT_DECLARATION", "params": {}} in verdict["hints"]


def test_the_version_and_declaration_helpers_read_what_they_claim():
    assert pine_version(VALID) == 5
    assert pine_version("plot(close)") is None
    assert declaration_of(VALID) == {"kind": "indicator", "title": "My Study"}
    assert declaration_of("plot(close)") == {"kind": "", "title": ""}


def test_an_indented_declaration_still_counts():
    assert declaration_of('  indicator("Spaced")')["title"] == "Spaced"


def test_python_source_is_now_refused():
    # The regression this change is about: legacy Python rows must not be
    # quietly accepted as "valid Pine".
    verdict = validate_pine_source('my_indicator_name = "Old Python"\noutput = {"plots": []}\n')
    assert verdict["success"] is False
    assert verdict["error_type"] == "PineParseError"


def test_a_legacy_study_declaration_names_the_indicator():
    # `study(` is how a pre-v5 script declares an indicator. Reporting it as
    # missing a declaration would nag the author about a script that is fine.
    verdict = validate_pine_source('//@version=4\nstudy("Bands", overlay=true)\nplot(close)')
    codes = {hint["code"] for hint in verdict["hints"]}
    assert "MISSING_SCRIPT_DECLARATION" not in codes
    assert "MISSING_SCRIPT_TITLE" not in codes


def test_a_legacy_study_without_a_title_is_told_so():
    verdict = validate_pine_source('//@version=4\nstudy(overlay=true)\nplot(close)')
    codes = {hint["code"] for hint in verdict["hints"]}
    assert "MISSING_SCRIPT_TITLE" in codes


def test_a_legacy_input_takes_its_type_from_the_default():
    # `input(1.8, ...)` is the pre-v5 spelling. Reading it as a string would
    # render a decimal knob as a text box in the settings panel.
    params = parse_pine_params(
        '//@version=4\nstudy("x")\nsigma = input(1.8, step=0.1, title="Sigma")\n'
    )
    assert params[0]["type"] == "float"
    assert params[0]["default"] == 1.8


def test_a_legacy_integer_input_reads_as_an_integer():
    params = parse_pine_params('//@version=4\nstudy("x")\nlen = input(30, title="Length")\n')
    assert params[0]["type"] == "int"
    assert params[0]["default"] == 30


def test_a_legacy_string_input_stays_a_string():
    params = parse_pine_params('//@version=4\nstudy("x")\ns = input("0700-1600", title="Session")\n')
    assert params[0]["type"] == "str"
    assert params[0]["default"] == "0700-1600"


def test_a_typed_input_still_reports_its_own_type():
    # The modern spelling names the type; inference must not override it.
    params = parse_pine_params('//@version=6\nindicator("x")\nlen = input.int(30, title="Length")\n')
    assert params[0]["type"] == "int"
