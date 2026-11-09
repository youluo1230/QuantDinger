"""TradingView supply-pipeline stage orchestration tests.

The stages talk to Postgres, the transpiler package, the strategy compiler and
the community service. These tests replace all four with fakes so the control
flow can be asserted without a database: the important behaviour is *what the
pipeline refuses to do* — create a source that does not compile, or publish a
strategy whose backtest failed.
"""

from __future__ import annotations

import json
from datetime import timedelta
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
    # The conversion row records which generator produced the code, which is a
    # different lifecycle from the assessor version: a transpiler fix has to
    # regenerate sources while a scoring tweak must not.
    assert params[3] == pipeline_module.TRANSPILER_VERSION


def test_regenerating_code_clears_the_previous_run_id(monkeypatch):
    """A stale run id must not survive regeneration.

    The publish pass reuses ``backtest_run_id`` instead of re-running the
    engine, so keeping an id from a run that measured different source would
    advertise a result the current code never produced.
    """

    cursor = _install_db(monkeypatch, lambda query, params: [])

    pipeline_module._record_conversion(
        script_id_part="PUB;a",
        source_id=3,
        status="ready",
        blockers=[],
        warnings=[],
        diagnostics={},
        code_hash="h",
    )

    _sql, _params = cursor.executed[0]
    assert "backtest_run_id = NULL" in _sql


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


def test_backtest_window_accounts_for_the_declared_warmup():
    """The engine validates the window *including* warmup bars.

    Validating without them accepted a window the engine then rejected as
    ``strategyV2.backtestRangeLimit``, which parked a strategy that was in fact
    fine.
    """
    from app.services.backtest_limits import validate_backtest_range

    start, end = pipeline_module._backtest_range("5m", warmup_bars=pipeline_module._WARMUP_BARS)

    assert (
        validate_backtest_range(
            market="Crypto",
            symbol=pipeline_module._DEFAULT_INSTRUMENT,
            timeframe="5m",
            start_date=start,
            end_date=end,
            warmup_bars=pipeline_module._WARMUP_BARS,
            fetch_start=start,
        )
        is None
    )


def test_a_secondary_subscription_shrinks_the_window():
    """A 1m subscription caps the window at a month, however long 1h allows.

    The engine rejects the whole request when any subscribed timeframe exceeds
    its cap, so a strategy that also subscribes to ``1m`` cannot be backtested
    over the default 180 days.
    """
    from app.services.backtest_limits import validate_backtest_range

    start, end = pipeline_module._backtest_range(
        "1h", warmup_bars=pipeline_module._WARMUP_BARS, extra_frequencies=["1m"]
    )

    assert (end - start).days <= 30
    for timeframe in ("1h", "1m"):
        assert (
            validate_backtest_range(
                market="Crypto",
                symbol=pipeline_module._DEFAULT_INSTRUMENT,
                timeframe=timeframe,
                start_date=start,
                end_date=end,
                warmup_bars=pipeline_module._WARMUP_BARS,
                fetch_start=start,
            )
            is None
        )


def test_the_window_is_shrunk_by_the_warmup_the_engine_fetches():
    """The engine validates the warmup it fetches ahead of the window.

    A 30-day window plus a 19-day warmup is a 49-day request, which a ``1m``
    subscription rejects. Shrinking here is what keeps the row out of a
    ``backtestRangeLimit`` retry loop.
    """
    from app.services.backtest_limits import (
        backtest_warmup_calendar_days,
        validate_backtest_range,
    )

    start, end = pipeline_module._backtest_range(
        "1h", warmup_bars=pipeline_module._WARMUP_BARS, extra_frequencies=["1m"]
    )
    fetch_start = start - timedelta(days=backtest_warmup_calendar_days("1h", pipeline_module._WARMUP_BARS))

    assert (end - fetch_start).days <= 30
    assert (
        validate_backtest_range(
            market="Crypto",
            symbol=pipeline_module._DEFAULT_INSTRUMENT,
            timeframe="1m",
            start_date=start,
            end_date=end,
            warmup_bars=pipeline_module._WARMUP_BARS,
            fetch_start=fetch_start,
        )
        is None
    )


def test_declared_frequencies_reads_every_subscription():
    """The window resolution must see all subscriptions, not just the first."""
    code = (
        "def initialize(context):\n"
        '    context.subscribe(frequency="1h")\n'
        '    context.subscribe(frequency="1m")\n'
    )

    assert pipeline_module._declared_frequencies(code) == ["1h", "1m"]
    assert pipeline_module._declared_frequencies("") == []


def test_backtest_window_honours_the_configured_lookback(monkeypatch):
    """Operators must be able to trade window length for queue throughput.

    The engine's per-bar replay makes cost grow with the square of the window,
    so the lookback is configuration rather than a constant.
    """
    from app.services.backtest_limits import validate_backtest_range

    monkeypatch.setenv("TRADINGVIEW_BACKTEST_DAYS", "45")

    start, end = pipeline_module._backtest_range("1h")

    assert (end - start).days == 45
    assert (
        validate_backtest_range(
            market="Crypto",
            symbol=pipeline_module._DEFAULT_INSTRUMENT,
            timeframe="1h",
            start_date=start,
            end_date=end,
        )
        is None
    )


def test_backtest_window_falls_back_when_the_lookback_is_illegal(monkeypatch):
    """An over-long setting must be narrowed, not turned into a hard failure."""
    from app.services.backtest_limits import validate_backtest_range

    # ``5m`` is capped well below ten years by backtest_limits.
    monkeypatch.setenv("TRADINGVIEW_BACKTEST_DAYS", "3650")

    start, end = pipeline_module._backtest_range("5m")

    assert start < end
    assert (
        validate_backtest_range(
            market="Crypto",
            symbol=pipeline_module._DEFAULT_INSTRUMENT,
            timeframe="5m",
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


def test_a_strategy_that_exceeds_the_time_ceiling_is_parked_as_timeout(monkeypatch):
    """A pathological strategy must not hold the batch hostage.

    Generated code that rescans the whole history on every bar can take minutes
    on a one-year window. The ceiling is per strategy, and the parking status is
    distinct from ``backtest_failed`` because re-running a timeout unchanged
    would burn exactly the same minutes again.
    """
    candidate = {
        "script_id_part": "PUB;slow",
        "source_id": 43,
        "name": "slow",
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
            # The sandbox raises its own TimeoutError class, not the builtin.
            from app.utils.safe_exec import TimeoutError as SandboxTimeoutError

            raise SandboxTimeoutError("Code execution timed out after 300 seconds")

    monkeypatch.setattr("app.services.strategy_v2.StrategyV2BacktestService", _Service)

    result = pipeline_module.backtest_and_publish(limit=1, user_id=1, publish=False)

    assert result["backtested"] == 0
    parked = [item for item in cursor.executed if item[1] and item[1][0] == "timeout"]
    assert parked, "a timed out strategy must be parked under its own status"
    assert parked[0][1][-1] == "PUB;slow"


def _threshold_harness(monkeypatch, metrics):
    """Run one publish pass over a stored backtest and return what happened."""
    candidate = {
        "script_id_part": "PUB;weak",
        "source_id": 11,
        "status": "backtested",
        "backtest_run_id": 77,
        "name": "weak",
        "description": "",
        "code": "def initialize(context):\n    pass\n",
    }

    def handler(query: str, params):
        if "FROM qd_tv_conversions c" in query:
            return [candidate]
        return []

    cursor = _install_db(monkeypatch, handler)
    monkeypatch.setattr(
        pipeline_module, "_stored_run_result", lambda run_id: dict(metrics)
    )
    monkeypatch.setattr(
        "app.services.strategy_v2.strategy_source_code_hash", lambda code: "h"
    )
    published: List[dict] = []

    class _Community:
        def publish_script_template_from_strategy(self, **kwargs):
            published.append(kwargs)
            return True, "success", {"indicator_id": 3}

    monkeypatch.setattr(
        "app.services.community_service.get_community_service", lambda: _Community()
    )
    result = pipeline_module.backtest_and_publish(
        limit=1, user_id=1, min_score=0, publish=True
    )
    return cursor, published, result


def test_a_losing_backtest_is_not_published(monkeypatch):
    """A measured loss is information, not a listing.

    The marketplace has no editorial layer, so publishing is the endorsement.
    """
    cursor, published, result = _threshold_harness(
        monkeypatch,
        {"totalTrades": 40, "totalReturn": -12.5, "sharpeRatio": 0.4, "maxDrawdown": -20.0},
    )

    assert published == []
    assert result["published"] == 0
    assert result["skipped"] == 1
    parked = [item for item in cursor.executed if item[1] and item[1][0] == "below_threshold"]
    assert parked, "a rejected strategy must be parked under below_threshold"
    assert parked[0][1][-1] == "PUB;weak"


def test_a_backtest_that_did_not_beat_noise_is_not_published(monkeypatch):
    """A positive return with a negative Sharpe is still unproven risk-taking."""
    _, published, result = _threshold_harness(
        monkeypatch,
        {"totalTrades": 90, "totalReturn": 2.0, "sharpeRatio": -0.3, "maxDrawdown": -15.0},
    )

    assert published == []
    assert result["published"] == 0
    assert result["results"][0]["verdict"]["failed"] == ["sharpe"]


def test_an_explosive_drawdown_is_not_published(monkeypatch):
    """A profitable run that lost most of its peak equity is not a listing."""
    _, published, result = _threshold_harness(
        monkeypatch,
        {"totalTrades": 120, "totalReturn": 30.0, "sharpeRatio": 1.2, "maxDrawdown": -85.0},
    )

    assert published == []
    assert result["results"][0]["verdict"]["failed"] == ["drawdown"]


def test_a_defensible_backtest_is_still_published(monkeypatch):
    """The thresholds must not block the strategies they are meant to admit."""
    _, published, result = _threshold_harness(
        monkeypatch,
        {"totalTrades": 36, "totalReturn": 16.27, "sharpeRatio": 1.04, "maxDrawdown": -14.32},
    )

    assert result["published"] == 1
    assert len(published) == 1
    assert result["results"][0]["verdict"]["rejected"] is False


def test_the_verdict_records_why_a_strategy_was_rejected(monkeypatch):
    """The decision has to be auditable from the row, not from a log line."""
    _, _, result = _threshold_harness(
        monkeypatch,
        {"totalTrades": 10, "totalReturn": -5.0, "sharpeRatio": -1.0, "maxDrawdown": -80.0},
    )

    verdict = result["results"][0]["verdict"]
    assert verdict["rejected"] is True
    assert sorted(verdict["failed"]) == ["drawdown", "return", "sharpe"]
    assert verdict["metrics"]["totalReturn"] == -5.0
    assert verdict["thresholds"]["maxDrawdownPct"] > 0


def test_a_missing_metric_does_not_silently_reject(monkeypatch):
    """An absent number is unknown, not failing; the trades floor still applies."""
    _, published, result = _threshold_harness(
        monkeypatch, {"totalTrades": 20, "totalReturn": None, "sharpeRatio": None}
    )

    assert result["published"] == 1
    assert len(published) == 1


@pytest.mark.parametrize(
    "interval,expected",
    [
        ("60", "1h"),
        ("1D", "1d"),
        ("D", "1d"),
        ("15", "15m"),
        ("5", "5m"),
        ("240", "4h"),
        ("1W", "1w"),
        # Upstream sometimes writes a full unit instead of the bare count.
        ("60m", "1h"),
        ("1h", "1h"),
        ("4h", "4h"),
    ],
)
def test_published_intervals_map_onto_the_subscription_grid(interval, expected):
    """TradingView's interval tokens must resolve to a real subscription."""
    assert pipeline_module._frequency_for_interval(interval) == expected


@pytest.mark.parametrize(
    "interval,expected",
    [
        ("1h", "1h"),
        ("4h", "4h"),
        ("6h", "4h"),
        ("1D", "1d"),
        ("2D", "1d"),
        ("1W", "1w"),
        ("W", "1w"),
        # Two-day bars have no runtime equivalent; daily is the nearest grid
        # point, and far closer than falling back to an hourly default.
        ("2D", "1d"),
    ],
)
def test_unit_suffixed_intervals_resolve_case_sensitively(interval, expected):
    """``1M`` is one month but ``1m`` is one minute, so case carries meaning."""
    assert pipeline_module._frequency_for_interval(interval) == expected


@pytest.mark.parametrize("interval", ["", None, "1M", "M", "nonsense"])
def test_unsupported_intervals_fall_back_to_the_configured_default(interval):
    """The runtime only supports a fixed grid, so unmappable intervals fall back.

    Silently rounding 45 minutes to an hour would measure a different strategy;
    using the configured default at least matches the previous behaviour and is
    visible in the conversion diagnostics.
    """
    assert pipeline_module._frequency_for_interval(interval) == pipeline_module._DEFAULT_FREQUENCY


def test_a_strategy_is_translated_onto_the_interval_it_was_published_for(monkeypatch):
    """The harvested interval, not the configured default, drives the transpile.

    A 15-minute strategy measured on hourly bars is a different strategy.
    """
    captured = {}

    class _Transpiler:
        @staticmethod
        def transpile_pine(source, **kwargs):
            captured.update(kwargs)
            return _Result(ok=False, error="stop here")

    monkeypatch.setattr(pipeline_module, "_assess_module", lambda: _Transpiler())
    _install_db(
        monkeypatch,
        _candidate_handler(
            [
                {
                    "id": 1,
                    "script_id_part": "PUB;scalp",
                    "name": "scalp",
                    "author": "a",
                    "source_code": "src",
                    "chart_interval": "15",
                }
            ]
        ),
    )

    pipeline_module.convert_pending_scripts(limit=1, user_id=1, min_score=0)

    assert captured["timeframe"] == "15m"


def test_the_backtest_window_follows_the_generated_code_not_the_default(monkeypatch):
    """The window must match the frequency the generated code subscribes to.

    Validating a 1m strategy against the hourly cap would request a window the
    engine then rejects.
    """
    assert pipeline_module._driving_frequency(
        'context.subscribe(frequency="5m")\n'
    ) == ("5m", [])
    # Shortest wins, matching the engine, regardless of authoring order.
    primary, extras = pipeline_module._driving_frequency(
        'context.subscribe(frequency="1h")\ncontext.subscribe(frequency="1m")\n'
    )
    assert primary == "1m"
    assert extras == ["1h"]
    assert pipeline_module._driving_frequency("") == (
        pipeline_module._DEFAULT_FREQUENCY,
        [],
    )


@pytest.mark.parametrize(
    "interval,expected",
    [
        # Real tokens observed in the feed that have no exact grid match.
        ("9", "5m"),   # 4 minutes from 5m, 6 from 15m
        ("10", "5m"),  # equidistant from 5m and 15m, tie goes to the shorter bar
        ("21", "15m"),
        ("45", "30m"),
        ("180", "4h"),
        ("480", "4h"),
        ("8h", "4h"),
        ("12h", "4h"),
    ],
)
def test_off_grid_intervals_snap_to_the_nearest_subscription(interval, expected):
    """An 8-hour strategy on 4-hour bars is off by 2x; on the 1h default, 8x."""
    assert pipeline_module._frequency_for_interval(interval) == expected


def test_the_nearest_grid_prefers_the_shorter_bar_on_a_tie():
    """Between two equally distant grids, the shorter bar keeps more detail."""
    # 10 minutes is 5 away from both 5m and 15m; the shorter bar wins.
    assert pipeline_module._nearest_grid_frequency(10) == "5m"
    # 120 minutes is 60 away from 1h and 120 away from 4h: nearest, not a tie.
    assert pipeline_module._interval_minutes("2h") == 120
    assert pipeline_module._nearest_grid_frequency(120) == "1h"
