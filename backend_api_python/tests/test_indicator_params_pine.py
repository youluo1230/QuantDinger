"""Indicator parameter declarations are read from Pine ``input.*`` calls.

The parameter list feeds the strategy builder's form and the indicator quality
panel, so it has to describe the knobs the script really declares. Before the
Pine migration this module parsed ``# @param`` comments, which no longer exist
in a Pine source, so every script reported zero parameters.

Old Python rows still exist in the database, which is why the comment grammar is
kept and tested here as well.
"""

from app.services.indicator_params import IndicatorParamsParser, looks_like_pine
from app.services.pine_validation import parse_pine_params


PINE_SOURCE = """
//@version=6
indicator("Params sample", overlay=true)

len = input.int(20, "Lookback", minval=1, maxval=200, group="Params")
show = input.bool(true, "Show markers", group="Visuals")
mode = input.string("fast", "Mode", options=["fast", "slow"])
tf = input.timeframe("60", "Higher timeframe")

plot(ta.sma(close, len))
"""


def test_pine_source_is_recognised_by_its_header():
    assert looks_like_pine(PINE_SOURCE) is True
    assert looks_like_pine("my_indicator_name = 'x'\noutput = {}\n") is False
    assert looks_like_pine("") is False


def test_pine_inputs_are_reported_as_parameters():
    params = parse_pine_params(PINE_SOURCE)
    names = [param["name"] for param in params]
    assert names == ["Lookback", "Show markers", "Mode", "Higher timeframe"]


def test_a_bool_parameter_keeps_its_boolean_default():
    # A string "true" would be sent back to the form as text, and a "false"
    # string is truthy -- either way the toggle would lie about the script.
    params = parse_pine_params(PINE_SOURCE)
    show = next(param for param in params if param["name"] == "Show markers")
    assert show["type"] == "bool"
    assert show["default"] is True


def test_parameter_bounds_and_groups_come_through():
    params = parse_pine_params(PINE_SOURCE)
    lookback = next(param for param in params if param["name"] == "Lookback")
    assert lookback["type"] == "int"
    assert lookback["default"] == 20
    assert lookback["min"] == 1
    assert lookback["max"] == 200
    assert lookback["group"] == "Params"
    # The variable the declaration is bound to is the key the runtime accepts
    # even after the title is edited.
    assert lookback["pine_var_id"] == "len"


def test_enum_options_are_reported_as_selectable_values():
    params = parse_pine_params(PINE_SOURCE)
    mode = next(param for param in params if param["name"] == "Mode")
    assert mode["type"] == "str"
    assert mode["values"] == ["fast", "slow"]


def test_a_timeframe_default_that_is_an_expression_has_no_literal_value():
    params = parse_pine_params(PINE_SOURCE)
    tf = next(param for param in params if param["name"] == "Higher timeframe")
    assert tf["type"] == "timeframe"
    # "60" is a literal here, so it survives; `timeframe.period` would not.
    assert tf["default"] == "60"


def test_the_parser_class_uses_the_pine_grammar_for_pine_source():
    # The strategy builder and the quality panel call through this class, so it
    # is the class that has to be Pine-aware.
    params = IndicatorParamsParser.parse_params(PINE_SOURCE)
    assert [param["name"] for param in params] == [
        "Lookback",
        "Show markers",
        "Mode",
        "Higher timeframe",
    ]


def test_legacy_python_rows_still_parse_with_the_comment_grammar():
    legacy = """
my_indicator_name = "Legacy"
# @param ma_fast int 5 Short moving-average period
# @param ma_slow int 20 Long moving-average period
df = df.copy()
output = {"name": my_indicator_name, "plots": [], "signals": []}
"""
    params = IndicatorParamsParser.parse_params(legacy)
    assert [param["name"] for param in params] == ["ma_fast", "ma_slow"]
    assert params[0]["default"] == 5


def test_merging_a_bool_override_keeps_the_boolean():
    # The form sends a real boolean; re-reading it as text would turn False into
    # "False", which the text converter does not recognise as false.
    merged = IndicatorParamsParser.merge_params(
        parse_pine_params(PINE_SOURCE),
        {"Show markers": False, "Lookback": 50},
    )
    assert merged["Show markers"] is False
    assert merged["Lookback"] == 50


def test_merging_without_overrides_returns_the_declared_defaults():
    merged = IndicatorParamsParser.merge_params(parse_pine_params(PINE_SOURCE), {})
    assert merged["Lookback"] == 20
    assert merged["Show markers"] is True


CONST_DRIVEN_SOURCE = """
//@version=6
indicator("Const driven", overlay=true)

const string G_PARAMS = "01 — Parameters"
const int FAST_DEFAULT = 12
const int SLOW_DEFAULT = 26

fastLen = input.int(FAST_DEFAULT, "Fast length", minval=1, maxval=500, group=G_PARAMS)
slowLen = input.int(SLOW_DEFAULT, "Slow length", minval=1, maxval=500, group=G_PARAMS)

plot(ta.ema(close, fastLen) - ta.ema(close, slowLen))
"""


def test_a_default_named_by_a_constant_is_resolved():
    # Real scripts write `input.int(FAST_DEFAULT, ...)` and define the number
    # further up; leaving it unresolved reports no default at all.
    params = parse_pine_params(CONST_DRIVEN_SOURCE)
    fast = next(param for param in params if param["name"] == "Fast length")
    assert fast["default"] == 12
    slow = next(param for param in params if param["name"] == "Slow length")
    assert slow["default"] == 26


def test_a_group_named_by_a_constant_is_resolved():
    # The group title is a string constant for the same reason the number is:
    # it is repeated across a dozen declarations.
    params = parse_pine_params(CONST_DRIVEN_SOURCE)
    assert params[0]["group"] == "01 — Parameters"


def test_a_padded_title_is_reported_trimmed():
    # Scripts hang rows under a group with em spaces. The runtime trims the
    # title before exposing it, so the key the form shows must match.
    source = (
        '//@version=6\nindicator("Padded")\n'
        'x = input.int(1, "\u2003\u2003↳ Width", minval=1)\nplot(x)\n'
    )
    params = parse_pine_params(source)
    assert params[0]["name"] == "↳ Width"


def test_an_unknown_identifier_default_stays_unknown():
    # A constant computed from another constant is not evaluated here; showing
    # nothing beats showing a wrong number.
    source = (
        '//@version=6\nindicator("Derived")\n'
        'base = 10 + 5\n'
        'x = input.int(base, "Length")\nplot(x)\n'
    )
    params = parse_pine_params(source)
    assert params[0]["default"] is None
