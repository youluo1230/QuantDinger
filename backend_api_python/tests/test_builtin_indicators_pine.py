"""
The built-in sample indicators.

A new user's first indicator is the clearest statement of what this product
considers a valid indicator, so every sample has to be Pine that the same
compiler the save path uses will accept. These cases pin that, and pin that
upgrading legacy samples leaves an author's own Pine work alone.
"""

import re

import pytest

from app.services import builtin_indicators
from app.services.pine_validation import (
    declaration_of,
    extract_indicator_meta,
    parse_pine_params,
    validate_pine_source,
)


def _samples():
    return builtin_indicators._builtin_specs()


def _sample_code() -> str:
    return _samples()[0]["code"]


@pytest.fixture(params=[0, 1, 2], ids=lambda i: _samples()[i]["name"])
def sample(request):
    return _samples()[request.param]


def test_every_sample_is_pine_that_compiles(sample):
    result = validate_pine_source(sample["code"])
    assert result["success"] is True, result
    assert result["error_type"] == ""


def test_every_sample_is_a_chart_study_not_a_strategy(sample):
    # The Indicator IDE is a chart tool; a strategy declaration there would put
    # backtest-only vocabulary in front of someone drawing a line.
    assert re.search(r"^indicator\(", sample["code"], re.MULTILINE), sample["name"]
    assert not re.search(r"^strategy\(", sample["code"], re.MULTILINE), sample["name"]
    assert declaration_of(sample["code"])["kind"] == "indicator"


def test_every_sample_declares_version_six(sample):
    assert sample["code"].startswith("//@version=6")


def test_every_sample_title_matches_the_name_it_is_seeded_under(sample):
    # The declaration is the name Pine itself gives the script; a sample whose
    # title disagreed with its stored name would show two different names in the
    # IDE, one of which the user cannot find again.
    assert declaration_of(sample["code"])["title"] == sample["name"]
    assert extract_indicator_meta(sample["code"])["name"] == sample["name"]


def test_the_sample_names_are_distinct():
    names = [spec["name"] for spec in _samples()]
    assert len(names) == len(set(names))
    # The first entry is the registration idempotency anchor.
    assert names[0] == builtin_indicators._BUILTIN_PACK_ANCHOR_NAME


def test_every_sample_has_no_python_leftovers(sample):
    code = sample["code"]
    for token in ("pd.", "np.", "df = ", "params.get", "output = ", "my_indicator_name", "# @param"):
        assert token not in code, f"{token!r} survived the Pine rewrite in {sample['name']}"


def test_every_sample_declares_its_settings_as_inputs(sample):
    # Every sample is a reference for authors; a hard-coded constant teaches the
    # wrong habit, and the parameter panel has nothing to render.
    specs = parse_pine_params(sample["code"])
    assert specs, f"{sample['name']} declares no inputs"
    for spec in specs:
        assert spec["type"] in ("int", "float", "bool", "color", "string", "str")
        assert spec["name"]


def test_the_supertrend_sample_exposes_settings_and_draws_bands_and_flips():
    code = _sample_code()
    specs = {spec["name"]: spec for spec in parse_pine_params(code)}
    assert "ATR period" in specs
    assert "ATR multiplier" in specs
    # The panel reads min/max; a period of zero would divide by zero in ta.atr.
    assert specs["ATR period"]["type"] == "int"
    assert specs["ATR period"]["min"] == 1
    assert code.count("plot(") >= 2
    # A SuperTrend without markers is half the drawing: the flip is the event.
    assert "plotshape(" in code
    assert "direction[1] ==" in code


def test_the_dual_ema_sample_shows_a_fill_and_crossover_markers():
    code = _samples()[1]["code"]
    # A fill needs the plot objects, so the sample must show that form.
    assert re.search(r"^fill\(", code, re.MULTILINE)
    assert "ta.crossover(" in code
    assert code.count("plotshape(") >= 2


def test_the_composite_sample_shows_hlines_and_column_style():
    code = _samples()[2]["code"]
    # A lower-pane study, which is the only sample that does not overlay price.
    assert "overlay = false" in code
    assert re.search(r"^hline\(", code, re.MULTILINE)
    assert "plot.style_columns" in code


def test_every_sample_avoids_look_ahead(sample):
    # `close` on the current bar is fine; a band carried forward must be built
    # from the previous bars or the study repaints.
    code = sample["code"]
    if "finalUpper" in code:
        assert "close[1] > finalUpper" in code
        assert "var float finalUpper" in code
    # No sample may index forward.
    assert not re.search(r"\[\s*-\d+\s*\]", code)


def test_it_reads_a_python_sample_as_not_pine():
    legacy = 'my_indicator_name = "Dual EMA Viewer"\n# @param fast_len int 12 Fast EMA\n'
    assert builtin_indicators._is_pine_source(legacy) is False
    assert builtin_indicators._is_pine_source(_sample_code()) is True


def test_legacy_name_map_covers_every_sample_it_claims():
    mapping = builtin_indicators._LEGACY_SAMPLE_NAMES
    names = {spec["name"] for spec in _samples()}
    # Every mapped target must be a sample that exists, or the upgrade silently
    # does nothing for the rows it selects.
    for legacy, target in mapping.items():
        assert target in names, f"{legacy!r} maps to unknown sample {target!r}"
    # And every sample must be reachable from at least one name.
    assert names <= set(mapping.values())
    for name in ("[Sample] SuperTrend Trend-Following", "Dual EMA Viewer (官方示例)", "[示例] 双均线策略"):
        assert name in mapping


def test_an_untouched_body_is_recognised_and_an_edited_one_is_not():
    code = _sample_code()
    assert builtin_indicators._is_untouched_sample(code) is True
    assert builtin_indicators._is_untouched_sample(code + "\n// my note\n") is False
    assert builtin_indicators._is_untouched_sample("") is False


def test_a_duplicate_machine_copy_is_handed_a_sample_the_library_lacks():
    # An earlier version of this module rewrote every legacy sample row to the
    # anchor name, leaving identical copies. They hold nothing of the author's,
    # so they can carry the lessons the library is missing instead of being
    # three rows called the same thing.
    specs = _samples()
    anchor = specs[0]
    rows = [
        {"id": 1, "user_id": 1, "name": anchor["name"], "code": anchor["code"]},
        {"id": 2, "user_id": 1, "name": anchor["name"], "code": anchor["code"]},
        {"id": 3, "user_id": 1, "name": anchor["name"], "code": anchor["code"]},
    ]
    plan = builtin_indicators.plan_duplicate_repair(rows, specs)
    assert plan == [(2, specs[1]["name"]), (3, specs[2]["name"])]


def test_an_edited_row_is_never_repointed_at_another_sample():
    specs = _samples()
    anchor = specs[0]
    rows = [
        {"id": 1, "user_id": 1, "name": anchor["name"], "code": anchor["code"]},
        # Same name, different body: this one belongs to its author.
        {"id": 2, "user_id": 1, "name": anchor["name"], "code": anchor["code"] + "\n// mine\n"},
    ]
    assert builtin_indicators.plan_duplicate_repair(rows, specs) == []


def test_a_user_who_already_has_every_sample_gets_no_duplicate():
    specs = _samples()
    rows = [
        {"id": index + 1, "user_id": 1, "name": spec["name"], "code": spec["code"]}
        for index, spec in enumerate(specs)
    ]
    rows.append({"id": 9, "user_id": 1, "name": specs[0]["name"], "code": specs[0]["code"]})
    # Every sample name is taken, so the extra copy has nothing to become.
    assert builtin_indicators.plan_duplicate_repair(rows, specs) == []


def test_duplicates_are_resolved_per_user_not_across_users():
    specs = _samples()
    anchor = specs[0]
    rows = [
        {"id": 1, "user_id": 1, "name": anchor["name"], "code": anchor["code"]},
        {"id": 2, "user_id": 2, "name": anchor["name"], "code": anchor["code"]},
    ]
    # Both users hold exactly one copy, so neither row moves.
    assert builtin_indicators.plan_duplicate_repair(rows, specs) == []


def test_registering_twice_seeds_once():
    class FakeCursor:
        def __init__(self):
            self.calls = []
            self._has = False

        def execute(self, sql, params=()):
            self.calls.append((" ".join(sql.split()), params))
            if sql.strip().upper().startswith("SELECT"):
                return
            self._has = True

        def fetchone(self):
            return {"x": 1} if self._has else None

        def close(self):
            pass

    class FakeDb:
        def __init__(self):
            self.cursor_obj = FakeCursor()

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            pass

        def rollback(self):
            pass

    db = FakeDb()
    assert builtin_indicators.seed_builtin_indicators_for_new_user(db, 7) == len(_samples())
    assert builtin_indicators.seed_builtin_indicators_for_new_user(db, 7) == 0
