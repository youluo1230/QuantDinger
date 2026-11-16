"""Tests for the TradingView indicator publishing pipeline.

The interesting failure modes here are not "does it compile". The earlier
implementation compiled all 124 indicator scripts and published nothing usable,
because the per-bar statements were nested one level too deep and never ran: the
modules were valid Python that produced empty plots. So these tests concentrate
on the gate — a module that runs but draws nothing must not reach the chart.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services import tradingview_indicators as pipeline


class _Cursor:
    def __init__(self, handler):
        self._handler = handler
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._rows = self._handler(sql, params) or []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass

    @property
    def lastrowid(self):
        return 99

    def queries(self, marker):
        return [(sql, params) for sql, params in self.executed if marker in sql]


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _install_db(monkeypatch, handler):
    cursor = _Cursor(handler)

    def _connect():
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            yield _Connection(cursor)

        return _ctx()

    monkeypatch.setattr(pipeline, "get_db_connection", _connect, raising=False)
    monkeypatch.setitem(sys.modules, "app.utils.db", _stub_db_module(_connect))
    return cursor


def _stub_db_module(connect):
    import types

    module = types.ModuleType("app.utils.db")
    module.get_db_connection = connect
    return module


class _Result:
    def __init__(self, ok, code="output = {'name': 'x', 'plots': [], 'signals': []}", error=None):
        self.ok = ok
        self.code = code if ok else None
        self.error = error
        self.blockers = [] if ok else [str(error or "blocked")]
        self.warnings = []
        self.stats = {}


@pytest.fixture()
def chart_stub(monkeypatch):
    """Replace the translator and the sandbox with scripted answers."""
    calls = {"transpile": 0, "sandbox": []}

    def _transpile(source, *, symbol="BTC/USDT", timeframe="1h", name=""):
        calls["transpile"] += 1
        return calls["result"]

    module = type(sys)("app.services.pine_transpiler.chart")
    module.transpile_indicator = _transpile
    monkeypatch.setitem(sys.modules, "app.services.pine_transpiler.chart", module)
    monkeypatch.setitem(
        sys.modules, "app.services.pine_transpiler",
        type(sys)("app.services.pine_transpiler"),
    )

    def _sandbox(code):
        calls["sandbox"].append(code)
        return calls["sandbox_result"]

    monkeypatch.setattr(pipeline, "_run_in_sandbox", _sandbox)
    return calls


def _candidate_row(script_id_part="PUB;demo", name="Demo", source="//@version=5\nplot(close)"):
    return {"script_id_part": script_id_part, "name": name, "source_code": source}


def _handler_for(rows):
    def _handle(sql, params):
        if "FROM qd_tv_scripts" in sql:
            return rows
        return []

    return _handle


def test_a_translation_that_draws_nothing_is_not_published(monkeypatch, chart_stub):
    """A module can run cleanly and still leave the pane empty."""
    _install_db(monkeypatch, _handler_for([_candidate_row()]))
    chart_stub["result"] = _Result(True)
    chart_stub["sandbox_result"] = {
        "success": True,
        "result": {"output": {"plots": [{"name": "Basis", "data": [None] * 50}], "signals": []}},
    }

    report = pipeline.convert_pending_indicators(limit=1, publish=True)

    assert report["drawn"] == 0
    assert report["published"] == 0
    assert report["skipped"] == 1
    assert report["results"][0]["status"] == "blank"


def test_a_translation_that_fails_the_sandbox_is_recorded_not_published(monkeypatch, chart_stub):
    _install_db(monkeypatch, _handler_for([_candidate_row()]))
    chart_stub["result"] = _Result(True)
    chart_stub["sandbox_result"] = {"success": False, "error": "Code execution timed out after 20 seconds"}

    report = pipeline.convert_pending_indicators(limit=1, publish=True)

    assert report["published"] == 0
    assert report["failed"] == 1
    assert report["results"][0]["status"] == "sandbox_failed"
    assert "timed out" in report["results"][0]["error"]


def test_a_blocked_translation_never_reaches_the_sandbox(monkeypatch, chart_stub):
    _install_db(monkeypatch, _handler_for([_candidate_row()]))
    chart_stub["result"] = _Result(False, error="no_chart_output")
    chart_stub["sandbox_result"] = {"success": True, "result": {"output": {}}}

    report = pipeline.convert_pending_indicators(limit=1, publish=True)

    assert chart_stub["sandbox"] == []
    assert report["transpiled"] == 0
    assert report["results"][0]["status"] == "transpile_failed"


def test_publishing_creates_the_row_the_chart_picker_reads(monkeypatch, chart_stub):
    cursor = _install_db(monkeypatch, _handler_for([_candidate_row(name="Basis Breakout")]))
    chart_stub["result"] = _Result(True, code="output = {}")
    chart_stub["sandbox_result"] = {
        "success": True,
        "result": {"output": {"plots": [{"name": "Basis", "data": [1.0] * 40}], "signals": []}},
    }

    report = pipeline.convert_pending_indicators(limit=1, user_id=1, publish=True)

    assert report["published"] == 1
    inserts = cursor.queries("INSERT INTO qd_indicator_codes")
    assert inserts, cursor.executed
    assert "'indicator'" in inserts[0][0]
    assert "'pine'" in inserts[0][0]
    assert inserts[0][1][1] == "[TV] Basis Breakout"


def test_the_published_row_holds_the_pine_source_not_the_translation(monkeypatch, chart_stub):
    """The browser runs Pine, so Python in that column is a broken indicator.

    ``_publish`` used to receive ``result.code`` -- the translated Python -- and
    that is what the IDE loaded. The chart handed it to the Pine runtime, and
    every later conversion would have overwritten the author-readable source
    with a machine translation of it.
    """
    source = "//@version=6\nindicator(\"Basis Breakout\", overlay = true)\nplot(close)"
    cursor = _install_db(monkeypatch, _handler_for([_candidate_row(name="Basis Breakout", source=source)]))
    chart_stub["result"] = _Result(True, code="output = {'name': 'x'}")
    chart_stub["sandbox_result"] = {
        "success": True,
        "result": {"output": {"plots": [{"name": "Basis", "data": [1.0] * 40}], "signals": []}},
    }

    pipeline.convert_pending_indicators(limit=1, user_id=1, publish=True)

    inserts = cursor.queries("INSERT INTO qd_indicator_codes")
    assert inserts, cursor.executed
    assert inserts[0][1][2] == source
    assert "output = " not in inserts[0][1][2]


def test_a_dry_run_publishes_nothing_but_still_scores(monkeypatch, chart_stub):
    cursor = _install_db(monkeypatch, _handler_for([_candidate_row()]))
    chart_stub["result"] = _Result(True, code="output = {}")
    chart_stub["sandbox_result"] = {
        "success": True,
        "result": {"output": {"plots": [{"name": "Basis", "data": [2.0] * 30}], "signals": []}},
    }

    report = pipeline.convert_pending_indicators(limit=1, publish=False)

    assert report["drawn"] == 1
    assert report["published"] == 0
    assert cursor.queries("INSERT INTO qd_indicator_codes") == []


def test_regenerating_different_code_drops_the_previous_listing(monkeypatch):
    """A listing must not survive a change to the code behind it."""
    cursor = _install_db(monkeypatch, _handler_for([]))

    pipeline._record("PUB;demo", status="published", version=2, code_hash="bbb")

    sql = cursor.queries("INSERT INTO qd_tv_indicators")[0][0]
    assert "qd_tv_indicators.code_hash = EXCLUDED.code_hash" in sql
    assert "indicator_id = CASE" in sql


def test_the_candidate_query_ignores_scripts_that_never_plot(monkeypatch):
    cursor = _install_db(monkeypatch, _handler_for([]))

    pipeline.convert_pending_indicators(limit=5, publish=False)

    sql = cursor.queries("FROM qd_tv_scripts")[0][0]
    assert "tv_script_type = 'indicator'" in sql
    assert "NOT LIKE '%%polyline.%%'" in sql
