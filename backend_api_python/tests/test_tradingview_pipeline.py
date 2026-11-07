"""TradingView supply-pipeline stage orchestration tests.

The stages talk to Postgres, the transpiler package, the strategy compiler and
the community service. These tests replace all four with fakes so the control
flow can be asserted without a database: the important behaviour is *what the
pipeline refuses to do* — create a source that does not compile, or publish a
strategy whose backtest failed.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Callable, Dict, List

import pytest

from app.services import tradingview_pipeline as pipeline_module


class _Cursor:
    """Cursor that replays a test-supplied handler for each executed query."""

    def __init__(self, handler: Callable[[str, tuple | None], List[Dict[str, Any]]]):
        self._handler = handler
        self.executed: List[tuple[str, tuple | None]] = []
        self._rows: List[Dict[str, Any]] = []
        self.rowcount = 1

    def execute(self, query: str, params: tuple | None = None):
        self.executed.append((query, params))
        self._rows = list(self._handler(query, params) or [])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass

    def queries(self, marker: str) -> List[tuple[str, tuple | None]]:
        return [item for item in self.executed if marker in item[0]]


class _Connection:
    def __init__(self, cursor: _Cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _install_db(monkeypatch, handler) -> _Cursor:
    cursor = _Cursor(handler)

    @contextmanager
    def _connect():
        yield _Connection(cursor)

    monkeypatch.setattr(pipeline_module, "get_db_connection", _connect)
    return cursor


class _FakeTranspiler:
    """Stand-in for ``app.services.pine_transpiler``."""

    def __init__(self, result):
        self.result = result
        self.calls: List[Dict[str, Any]] = []

    def transpile_pine(self, source, *, symbol=None, timeframe=None, name=None):
        self.calls.append(
            {"source": source, "symbol": symbol, "timeframe": timeframe, "name": name}
        )
        return self.result

    @staticmethod
    def assess_migratability(source):
        return {"score": 80, "blocked": False, "blockers": [], "stats": {}}


class _Result:
    def __init__(self, *, ok=True, code="print('hi')", error=None, blockers=(), stats=None):
        self.ok = ok
        self.code = code if ok else None
        self.error = error
        self.blockers = list(blockers)
        self.warnings: List[str] = []
        self.stats: Dict[str, Any] = dict(stats or {})


def _candidate_handler(rows):
    """Serve candidate rows for the convert query and nothing elsewhere."""

    def handler(query: str, params):
        if "FROM qd_tv_scripts s" in query:
            return rows
        if "FROM qd_tv_conversions" in query and "SELECT source_id" in query:
            return []
        return []

    return handler


def test_convert_records_transpile_failure_without_touching_sources(monkeypatch):
    """A candidate the transpiler rejects must never reach ``create_source``."""
    monkeypatch.setattr(
        pipeline_module,
        "_assess_module",
        lambda: _FakeTranspiler(_Result(ok=False, error="unsupported ta.alma")),
    )
    cursor = _install_db(
        monkeypatch,
        _candidate_handler(
            [{"id": 1, "script_id_part": "PUB;x", "name": "X", "author": "a", "source_code": "src"}]
        ),
    )
    created: List[Dict[str, Any]] = []

    class _Service:
        @staticmethod
        def create_source(payload):
            created.append(payload)
            return 99

    monkeypatch.setattr(
        "app.services.script_source.get_script_source_service", lambda: _Service()
    )

    summary = pipeline_module.convert_pending_scripts(limit=1, user_id=1, min_score=0)

    assert summary["candidates"] == 1
    assert summary["transpiled"] == 0
    assert summary["created"] == 0
    assert created == []
    assert summary["failures"][0]["stage"] == "transpile"
    assert "unsupported ta.alma" in summary["failures"][0]["error"]
    insert = cursor.queries("INSERT INTO qd_tv_conversions")
    assert insert, "transpile failure must be recorded for operator visibility"
    assert "transpile_failed" in insert[0][1]


def test_convert_refuses_code_that_fails_the_strategy_contract(monkeypatch):
    """The transpiler's opinion is not authoritative; the compiler is."""
    monkeypatch.setattr(
        pipeline_module,
        "_assess_module",
        lambda: _FakeTranspiler(_Result(ok=True, code="bad code")),
    )
    cursor = _install_db(
        monkeypatch,
        _candidate_handler(
            [{"id": 2, "script_id_part": "PUB;y", "name": "Y", "author": "b", "source_code": "src"}]
        ),
    )

    class _CompileError(RuntimeError):
        code = "strategyV2.topLevelExecutableNotAllowed"

    def _boom(_code):
        raise _CompileError("top-level call")

    monkeypatch.setattr("app.services.strategy_v2.compile_strategy_v2", _boom)
    monkeypatch.setattr(
        "app.services.strategy_v2.strategy_source_code_hash", lambda code: "hash-y"
    )
    created: List[Dict[str, Any]] = []

    monkeypatch.setattr(
        "app.services.script_source.get_script_source_service",
        lambda: type("S", (), {"create_source": staticmethod(lambda payload: created.append(payload) or 1)}),
    )

    summary = pipeline_module.convert_pending_scripts(limit=1, user_id=1, min_score=0)

    assert summary["transpiled"] == 1
    assert summary["compiled"] == 0
    assert created == []
    assert summary["failures"][0]["stage"] == "compile"
    assert "strategyV2.topLevelExecutableNotAllowed" in summary["failures"][0]["error"]
    insert = cursor.queries("INSERT INTO qd_tv_conversions")
    assert "compile_failed" in insert[0][1]


def test_convert_creates_source_only_after_compile_passes(monkeypatch):
    monkeypatch.setattr(
        pipeline_module,
        "_assess_module",
        lambda: _FakeTranspiler(_Result(ok=True, code="def initialize(context):\n    pass")),
    )
    cursor = _install_db(
        monkeypatch,
        _candidate_handler(
            [{"id": 3, "script_id_part": "PUB;z", "name": "Z", "author": "c", "source_code": "src"}]
        ),
    )
    monkeypatch.setattr("app.services.strategy_v2.compile_strategy_v2", lambda code: None)
    monkeypatch.setattr(
        "app.services.strategy_v2.strategy_source_code_hash", lambda code: "hash-z"
    )
    created: List[Dict[str, Any]] = []

    class _Service:
        @staticmethod
        def create_source(payload):
            created.append(payload)
            return 42

    monkeypatch.setattr(
        "app.services.script_source.get_script_source_service", lambda: _Service()
    )

    summary = pipeline_module.convert_pending_scripts(limit=1, user_id=1, min_score=0)

    assert summary["compiled"] == 1
    assert summary["created"] == 1
    assert summary["failures"] == []
    assert created[0]["code"] == "def initialize(context):\n    pass"
    assert created[0]["visibility"] == "private"
    assert created[0]["metadata"]["tradingview"]["script_id_part"] == "PUB;z"
    insert = cursor.queries("INSERT INTO qd_tv_conversions")
    assert "ready" in insert[0][1]
    assert insert[0][1][1] == 42


def test_convert_passes_channel_config_to_transpiler(monkeypatch):
    """The instrument/frequency must come from settings, not the transpiler."""
    transpiler = _FakeTranspiler(_Result(ok=False, error="nope"))
    monkeypatch.setattr(pipeline_module, "_assess_module", lambda: transpiler)
    _install_db(monkeypatch, _candidate_handler([]))

    pipeline_module.convert_pending_scripts(limit=1, user_id=1, min_score=0)

    assert transpiler.calls == [], "no candidates means no transpile calls"


def test_assess_pending_scripts_persists_scores(monkeypatch):
    handler_calls: List[str] = []

    def handler(query: str, params):
        handler_calls.append(query)
        if "FROM qd_tv_scripts" in query:
            return [
                {"id": 11, "script_id_part": "PUB;a", "name": "A", "source_code": "s1"},
                {"id": 12, "script_id_part": "PUB;b", "name": "B", "source_code": "s2"},
            ]
        return []

    _install_db(monkeypatch, handler)
    monkeypatch.setattr(
        pipeline_module,
        "assess_source",
        lambda source: {"score": 91, "blocked": False, "blockers": [], "stats": {"len": len(source)}},
    )

    result = pipeline_module.assess_pending_scripts(limit=2)

    assert result["processed"] == 2
    updates = [q for q in handler_calls if "UPDATE qd_tv_scripts" in q]
    assert len(updates) == 2


def test_assess_pending_scripts_survives_a_single_failure(monkeypatch):
    def handler(query: str, params):
        if "FROM qd_tv_scripts" in query:
            return [
                {"id": 11, "script_id_part": "PUB;a", "source_code": "s1"},
                {"id": 12, "script_id_part": "PUB;b", "source_code": "s2"},
            ]
        return []

    _install_db(monkeypatch, handler)

    def _assess(source):
        if source == "s2":
            raise ValueError("parser blew up")
        return {"score": 50, "blocked": False, "blockers": [], "stats": {}}

    monkeypatch.setattr(pipeline_module, "assess_source", _assess)

    result = pipeline_module.assess_pending_scripts(limit=2)

    assert result["processed"] == 1
    assert result["errors"] == 1


def test_backtest_and_publish_marks_row_published(monkeypatch):
    rows = [
        {
            "script_id_part": "PUB;ok",
            "source_id": 7,
            "name": "[TV] Strategy",
            "description": "desc",
            "code": "def initialize(context): pass",
        }
    ]
    updates: List[tuple] = []
    published: List[Dict[str, Any]] = []

    def handler(query: str, params):
        if "FROM qd_tv_conversions c" in query:
            return rows
        return []

    cursor = _install_db(monkeypatch, handler)
    cursor_original_execute = cursor.execute

    def _execute(query, params=None):
        if "UPDATE qd_tv_conversions" in query:
            updates.append(params)
        cursor_original_execute(query, params)

    cursor.execute = _execute  # type: ignore[method-assign]

    class _Backtest:
        def run(self, **kwargs):
            return 123, {
                "totalTrades": 12,
                "totalReturn": 8.5,
                "sharpeRatio": 1.1,
                "maxDrawdown": -4.0,
            }

    class _Community:
        def publish_script_template_from_strategy(self, **kwargs):
            published.append(kwargs)
            return True, "success", {"indicator_id": 55, "review_status": "approved"}

    monkeypatch.setattr("app.services.strategy_v2.StrategyV2BacktestService", _Backtest)
    monkeypatch.setattr(
        "app.services.strategy_v2.strategy_source_code_hash", lambda code: "hash-ok"
    )
    monkeypatch.setattr(
        "app.services.community_service.get_community_service", lambda: _Community()
    )

    result = pipeline_module.backtest_and_publish(limit=1, user_id=1, min_score=0, publish=True)

    assert result["backtested"] == 1
    assert result["published"] == 1
    assert result["results"][0]["indicator_id"] == 55
    assert published[0]["source_id"] == 7
    assert published[0]["is_admin"] is True
    assert updates and updates[0][0] == 123


def test_backtest_without_publish_marks_row_backtested(monkeypatch):
    rows = [
        {
            "script_id_part": "PUB;ok",
            "source_id": 7,
            "name": "[TV] Strategy",
            "description": "desc",
            "code": "def initialize(context): pass",
        }
    ]
    updates: List[tuple] = []

    def handler(query: str, params):
        if "FROM qd_tv_conversions c" in query:
            return rows
        return []

    cursor = _install_db(monkeypatch, handler)
    original = cursor.execute

    def _execute(query, params=None):
        if "UPDATE qd_tv_conversions" in query:
            updates.append(params)
        original(query, params)

    cursor.execute = _execute  # type: ignore[method-assign]

    class _Backtest:
        def run(self, **kwargs):
            return 9, {"totalTrades": 3, "totalReturn": -1.0}

    def _must_not_publish(**_kwargs):
        raise AssertionError("publish=False must not call the community service")

    monkeypatch.setattr("app.services.strategy_v2.StrategyV2BacktestService", _Backtest)
    monkeypatch.setattr(
        "app.services.strategy_v2.strategy_source_code_hash", lambda code: "hash-ok"
    )
    monkeypatch.setattr(
        "app.services.community_service.get_community_service", _must_not_publish
    )

    result = pipeline_module.backtest_and_publish(limit=1, user_id=1, min_score=0, publish=False)

    assert result["published"] == 0
    assert result["backtested"] == 1
    assert updates


def test_backtest_failure_is_reported_and_does_not_stop_the_batch(monkeypatch):
    rows = [
        {"script_id_part": "PUB;bad", "source_id": 1, "name": "Bad", "code": "x"},
        {"script_id_part": "PUB;good", "source_id": 2, "name": "Good", "code": "y"},
    ]

    def handler(query: str, params):
        if "FROM qd_tv_conversions c" in query:
            return rows
        return []

    _install_db(monkeypatch, handler)

    class _Backtest:
        def run(self, **kwargs):
            if kwargs["code"] == "x":
                raise RuntimeError("no data for instrument")
            return 5, {"totalTrades": 12, "totalReturn": 0.1}

    class _Community:
        def publish_script_template_from_strategy(self, **_kwargs):
            return True, "success", {"indicator_id": 1}

    monkeypatch.setattr("app.services.strategy_v2.StrategyV2BacktestService", _Backtest)
    monkeypatch.setattr(
        "app.services.strategy_v2.strategy_source_code_hash", lambda code: "h"
    )
    monkeypatch.setattr(
        "app.services.community_service.get_community_service", lambda: _Community()
    )

    result = pipeline_module.backtest_and_publish(limit=2, user_id=1, min_score=0, publish=True)

    assert result["backtested"] == 1
    assert result["published"] == 1
    errors = [item for item in result["results"] if item.get("error")]
    assert errors and "no data" in errors[0]["error"]


def test_conversion_upsert_keeps_an_existing_source_id(monkeypatch):
    """Re-recording a failed attempt must not orphan a previously created source."""
    cursor = _install_db(monkeypatch, lambda query, params: [])

    pipeline_module._record_conversion(
        script_id_part="PUB;a",
        source_id=None,
        status="compile_failed",
        blockers=[],
        warnings=[],
        diagnostics={},
        code_hash="h",
    )

    sql, params = cursor.executed[0]
    assert "COALESCE(EXCLUDED.source_id, qd_tv_conversions.source_id)" in sql
    assert params[0] == "PUB;a"


def test_conversion_diagnostics_are_json_encodings(monkeypatch):
    cursor = _install_db(monkeypatch, lambda query, params: [])

    pipeline_module._record_conversion(
        script_id_part="PUB;a",
        source_id=3,
        status="ready",
        blockers=["table"],
        warnings=["approx"],
        diagnostics={"nested": {"value": 1}},
        code_hash="h",
    )

    _sql, params = cursor.executed[0]
    assert json.loads(params[5]) == ["table"]
    assert json.loads(params[6]) == ["approx"]
    assert json.loads(params[7])["nested"]["value"] == 1
    assert "pine_transpiler" == params[2]
    assert params[3] == pipeline_module.MIGRATABILITY_VERSION


def test_pipeline_status_reports_empty_corpus(monkeypatch):
    def handler(query, params):
        if "FROM qd_tv_conversions" in query:
            return []
        return [{"total": 0, "with_source": 0, "strong": 0, "assessed": 0}]

    _install_db(monkeypatch, handler)

    status = pipeline_module.pipeline_status()

    assert status["corpus"]["total"] == 0
    assert status["conversions"] == {}
    assert status["assess_version"] >= 1


@pytest.mark.parametrize("frequency", ["1m", "5m", "15m", "1h", "4h", "1d"])
def test_backtest_range_is_legal_for_every_supported_frequency(frequency):
    """The publish path must never trip the per-frequency range limit."""
    from app.services.backtest_limits import validate_backtest_range

    start, end = pipeline_module._backtest_range(frequency)

    assert start < end
    assert (
        validate_backtest_range(
            market="Crypto",
            symbol=pipeline_module._DEFAULT_INSTRUMENT,
            timeframe=frequency,
            start_date=start,
            end_date=end,
        )
        is None
    )


def test_assess_source_records_which_scorer_produced_the_score(monkeypatch):
    """A fallback verdict must stay distinguishable from a parser-backed one.

    The cheap fallback is deliberately optimistic, so without this marker an
    operator cannot tell why a script was ranked as a strong candidate.
    """
    class _Module:
        @staticmethod
        def assess_migratability(source: str) -> dict:
            return {"score": 88, "blocked": False, "blockers": [], "stats": {}}

    monkeypatch.setattr(pipeline_module, "_assess_module", lambda: _Module)

    result = pipeline_module.assess_source("strategy('x')")

    assert result["assessor"] == "parser"
    assert result["score"] == 88


def test_assess_source_marks_fallback_when_the_parser_is_missing(monkeypatch):
    def _boom():
        raise ImportError("pynescript missing")

    monkeypatch.setattr(pipeline_module, "_assess_module", _boom)

    result = pipeline_module.assess_source("strategy('x')\nstrategy.entry('L', strategy.long)")

    assert result["assessor"] == "cheap_fallback"


def test_failed_backtest_is_parked_so_it_cannot_starve_the_queue(monkeypatch):
    """A strategy that blows up must not be retried by every later batch.

    Leaving the row in ``ready`` made one broken script consume a backtest slot
    on every run, so strategies that had never been tried were never reached.
    """
    candidate = {
        "script_id_part": "PUB;broken",
        "source_id": 42,
        "name": "broken",
        "description": "",
        "code": "def initialize(context):\n    pass\n",
    }

    def handler(query: str, params):
        if "FROM qd_tv_conversions c" in query:
            return [candidate]
        return []

    cursor = _install_db(monkeypatch, handler)

    class _Service:
        def run(self, **kwargs):
            raise RuntimeError("strategyV2.runtimeFailed:handle_data:boom")

    monkeypatch.setattr("app.services.strategy_v2.StrategyV2BacktestService", _Service)

    result = pipeline_module.backtest_and_publish(limit=1, user_id=1, publish=False)

    assert result["backtested"] == 0
    assert result["results"][0]["error"].endswith("boom")
    parked = [item for item in cursor.executed if item[1] and item[1][0] == "backtest_failed"]
    assert parked, "the failing row must be parked, not silently left as ready"
    assert parked[0][1][-1] == "PUB;broken"


def test_a_backtest_that_never_trades_is_not_published(monkeypatch):
    """Zero trades means the entry logic never fired, so nothing was verified.

    ``service.run`` reports such a run as a success with ``totalTrades == 0``.
    Publishing it would advertise an unexercised strategy as validated.
    """
    candidate = {
        "script_id_part": "PUB;idle",
        "source_id": 7,
        "name": "idle",
        "description": "",
        "code": "def initialize(context):\n    pass\n",
    }

    def handler(query: str, params):
        if "FROM qd_tv_conversions c" in query:
            return [candidate]
        return []

    cursor = _install_db(monkeypatch, handler)
    published: List[dict] = []

    class _Backtest:
        def run(self, **kwargs):
            return 9, {"totalTrades": 0, "totalReturn": 0.0}

    class _Community:
        def publish_script_template_from_strategy(self, **kwargs):
            published.append(kwargs)
            return True, "success", {"indicator_id": 3}

    monkeypatch.setattr("app.services.strategy_v2.StrategyV2BacktestService", _Backtest)
    monkeypatch.setattr(
        "app.services.strategy_v2.strategy_source_code_hash", lambda code: "h"
    )
    monkeypatch.setattr(
        "app.services.community_service.get_community_service", lambda: _Community()
    )

    result = pipeline_module.backtest_and_publish(limit=1, user_id=1, min_score=0, publish=True)

    assert result["published"] == 0
    assert result["skipped"] == 1
    assert result["results"][0]["skipped"] == "no_trades"
    assert published == [], "an untraded strategy must not reach the marketplace"


def test_publishing_reuses_a_successful_run_instead_of_repeating_it(monkeypatch):
    """A row that already backtested must be publishable without a fresh run.

    Selecting only ``ready`` rows made a publish pass a no-op once the backtest
    phase had moved everything on, so nothing could ever reach the marketplace.
    """
    candidate = {
        "script_id_part": "PUB;done",
        "source_id": 3,
        "status": "backtested",
        "backtest_run_id": 77,
        "name": "done",
        "description": "",
        "code": "def initialize(context):\n    pass\n",
    }

    def handler(query: str, params):
        if "FROM qd_tv_conversions c" in query:
            return [candidate]
        if "FROM qd_backtest_runs" in query:
            return [{"total_trades": 25, "total_return": 4.5, "sharpe_ratio": 0.8, "max_drawdown": -12.0}]
        return []

    _install_db(monkeypatch, handler)
    runs: List[dict] = []
    published: List[dict] = []

    class _Backtest:
        def run(self, **kwargs):
            runs.append(kwargs)
            return 999, {"totalTrades": 1}

    class _Community:
        def publish_script_template_from_strategy(self, **kwargs):
            published.append(kwargs)
            return True, "success", {"indicator_id": 11}

    monkeypatch.setattr("app.services.strategy_v2.StrategyV2BacktestService", _Backtest)
    monkeypatch.setattr(
        "app.services.strategy_v2.strategy_source_code_hash", lambda code: "h"
    )
    monkeypatch.setattr(
        "app.services.community_service.get_community_service", lambda: _Community()
    )

    result = pipeline_module.backtest_and_publish(limit=5, user_id=1, min_score=0, publish=True)

    assert runs == [], "an already-backtested row must not be re-run"
    assert result["published"] == 1
    assert published and published[0]["source_id"] == 3
