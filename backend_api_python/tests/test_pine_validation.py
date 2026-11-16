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


def test_a_legacy_pine_version_is_refused_with_the_reason():
    # PineTS runs v5 and v6 only. Letting v4 through would fail inside the
    # browser runtime, far from where the author can fix it.
    verdict = validate_pine_source('//@version=4\nstudy("Old")\nplot(close)')
    assert verdict["success"] is False
    assert verdict["error_type"] == "UnsupportedVersion"
    assert "version 4" in verdict["details"]


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
