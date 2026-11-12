"""TradingView harvesting and supply-pipeline tests.

These cover the parts that do not need the network or a database: URL building,
payload normalisation, and the assessment fallback used when the optional Pine
parser is missing. The stage orchestration is exercised with a fake cursor.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from app.services import tradingview_harvest as harvest_module
from app.services import tradingview_pipeline as pipeline_module


class _FakeCursor:
    """Minimal cursor recording executed SQL and replaying canned rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None):
        self.rows = list(rows or [])
        self.executed: list[tuple[str, tuple | None]] = []
        self.rowcount = 0

    def execute(self, query: str, params: tuple | None = None):
        self.executed.append((query, params))
        self.rowcount = 1

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


@contextmanager
def _fake_connection(cursor: _FakeCursor):
    yield _FakeConnection(cursor)


def test_listing_url_uses_path_pagination():
    """``?page=2`` is ignored upstream; pagination must be a path segment."""
    first = harvest_module._listing_url("trend", 1)
    second = harvest_module._listing_url("trend", 2)

    assert first == (
        "https://www.tradingview.com/scripts/trend/"
        "?component-data-only=1&script_type=strategies&script_access=open"
    )
    assert "/scripts/trend/page-2/" in second
    assert "page=" not in second


def test_listing_url_normalises_topic():
    assert "/scripts/mean-reversion/" in harvest_module._listing_url(" Mean-Reversion ", 1)


def test_listing_url_switches_the_script_type():
    """Indicators carry signals the transpiler recovers; strategies state theirs."""
    indicators = harvest_module._listing_url("ict", 1, "indicators")

    assert "script_type=indicators" in indicators
    assert "script_type=strategies" not in indicators
    assert harvest_module._listing_url("ict", 1, "nonsense").endswith(
        "script_type=strategies&script_access=open"
    )


def test_source_url_percent_encodes_script_id():
    """Script ids contain a ``;`` which must not break the path."""
    url = harvest_module._source_url("PUB;abc123")

    assert "PUB%3Babc123" in url
    assert url.endswith("/last?no_4xx=true")


def test_pine_version_of_reads_version_directive():
    assert harvest_module._pine_version_of("//@version=5\nstrategy('x')") == 5
    assert harvest_module._pine_version_of("// @version = 6\nx = 1") == 6
    assert harvest_module._pine_version_of("no directive here") is None


def test_access_label_normalises_int_and_string_forms():
    assert harvest_module._access_label(1) == "open"
    assert harvest_module._access_label(0) == "invite_only"
    assert harvest_module._access_label("open_no_auth") == "open"
    assert harvest_module._access_label("") == ""


def test_item_to_row_extracts_listing_fields():
    item = {
        "script_id_part": "PUB;deadbeef",
        "name": "Test Strategy",
        "description": "desc",
        "script_type": "strategy",
        "script_access": 1,
        "chart_url": "https://www.tradingview.com/script/abc/",
        "likes_count": 42,
        "views_count": 7,
        "comments_count": 3,
        "is_picked": True,
        "date_timestamp": 1793855097,
        "user": {"username": "someone"},
        "symbol": {"name": "BTCUSD"},
    }

    row = harvest_module._item_to_row("trend", item)

    assert row is not None
    assert row["script_id_part"] == "PUB;deadbeef"
    assert row["author"] == "someone"
    assert row["script_access"] == "open"
    assert row["likes_count"] == 42
    assert row["symbol_name"] == "BTCUSD"
    assert row["topic"] == "trend"
    assert row["tv_created_at"] is not None
    assert row["tv_created_at"].tzinfo == timezone.utc


def test_item_to_row_ignores_items_without_script_id():
    assert harvest_module._item_to_row("trend", {"name": "no id"}) is None


def test_fetch_listing_rejects_unexpected_payload(monkeypatch):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"ideas": {"data": {"items": "nope"}}}}

    class _Session:
        def get(self, url, timeout=None, **kwargs):
            return _Response()

    with pytest.raises(harvest_module.TradingViewHarvestError):
        harvest_module.fetch_listing("trend", session=_Session())


def test_fetch_listing_returns_rows_and_total():
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {
                    "ideas": {
                        "data": {
                            "total": 149,
                            "items": [
                                {"script_id_part": "PUB;a", "name": "A", "likes_count": 5},
                                {"script_id_part": "PUB;b", "name": "B", "likes_count": 9},
                                {"name": "no id"},
                            ],
                        }
                    }
                }
            }

    class _Session:
        def get(self, url, timeout=None, **kwargs):
            return _Response()

    rows, total = harvest_module.fetch_listing("trend", session=_Session())

    assert total == 149
    assert [row["script_id_part"] for row in rows] == ["PUB;a", "PUB;b"]


def test_fetch_source_records_error_instead_of_raising():
    class _Session:
        def get(self, url, timeout=None, **kwargs):
            raise RuntimeError("network down")

    result = harvest_module.fetch_source("PUB;x", session=_Session())

    assert result["source"] == ""
    assert "network down" in result["error"]


def test_upsert_discovered_uses_conflict_update(monkeypatch):
    cursor = _FakeCursor()
    monkeypatch.setattr(
        harvest_module, "get_db_connection", lambda: _fake_connection(cursor)
    )
    row = harvest_module._item_to_row(
        "trend", {"script_id_part": "PUB;a", "name": "A", "likes_count": 1}
    )

    written = harvest_module.upsert_discovered([row])

    assert written == 1
    sql = cursor.executed[0][0]
    assert "ON CONFLICT (script_id_part) DO UPDATE" in sql
    # Likes must never regress when a later crawl sees a stale count.
    assert "GREATEST(qd_tv_scripts.likes_count" in sql


def test_cheap_assessment_penalises_missing_strategy_entry():
    """The fallback scorer must not call a plain indicator convertible."""
    result = pipeline_module._cheap_assessment("x = ta.ema(close, 20)\nplot(x)")

    assert result["score"] < 100
    assert result["stats"]["has_strategy_entry"] is False
    assert result["stats"]["assessor"] == "cheap_fallback"


def test_cheap_assessment_scores_plain_strategy_highly():
    source = (
        "//@version=5\n"
        "strategy('x')\n"
        "fast = ta.ema(close, 9)\n"
        "slow = ta.ema(close, 21)\n"
        "if ta.crossover(fast, slow)\n"
        "    strategy.entry('L', strategy.long)\n"
    )

    result = pipeline_module._cheap_assessment(source)

    assert result["score"] >= 90
    assert result["blocked"] is False


def test_assess_source_falls_back_when_transpiler_missing(monkeypatch):
    def _boom():
        raise ImportError("pynescript missing")

    monkeypatch.setattr(pipeline_module, "_assess_module", _boom)

    result = pipeline_module.assess_source("strategy('x')\nstrategy.entry('L', strategy.long)")

    assert result["stats"]["assessor"] == "cheap_fallback"
    assert "pynescript missing" in result["stats"]["assessor_error"]
    assert 0 <= result["score"] <= 100


def test_assess_source_clamps_score(monkeypatch):
    class _Module:
        @staticmethod
        def assess_migratability(source):
            return {"score": 250, "blocked": False, "blockers": [], "stats": {}}

    monkeypatch.setattr(pipeline_module, "_assess_module", lambda: _Module())

    result = pipeline_module.assess_source("strategy('x')")

    assert result["score"] == 100


def test_backtest_range_respects_frequency_limits():
    """The window must satisfy ``validate_backtest_range`` for the frequency."""
    from app.services.backtest_limits import validate_backtest_range

    start, end = pipeline_module._backtest_range("5m")

    assert start < end
    error = validate_backtest_range(
        market="Crypto",
        symbol=pipeline_module._DEFAULT_INSTRUMENT,
        timeframe="5m",
        start_date=start,
        end_date=end,
    )
    assert error is None


def test_assess_version_is_positive():
    """The staging table records this so stale assessments get refreshed."""
    assert pipeline_module.MIGRATABILITY_VERSION >= 1


def test_harvest_disabled_by_flag(monkeypatch):
    from app.tasks import tradingview as task_module

    monkeypatch.delenv("TRADINGVIEW_HARVEST_ENABLED", raising=False)

    assert task_module.run_tradingview_harvest() == {"skipped": True, "reason": "disabled"}
    assert task_module.run_tradingview_assess() == {"skipped": True, "reason": "disabled"}
    assert task_module.run_tradingview_convert() == {"skipped": True, "reason": "disabled"}


def test_harvest_tasks_are_routed_to_maintenance_queue():
    from app.celery_app import celery_app

    for task in (
        "quantdinger.tasks.tradingview_harvest",
        "quantdinger.tasks.tradingview_assess",
        "quantdinger.tasks.tradingview_convert",
    ):
        assert celery_app.conf.task_routes[task]["queue"] == "maintenance"


def test_harvest_beat_entries_are_scheduled():
    from app.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    for key, task in (
        ("tradingview-harvest", "quantdinger.tasks.tradingview_harvest"),
        ("tradingview-assess", "quantdinger.tasks.tradingview_assess"),
        ("tradingview-convert", "quantdinger.tasks.tradingview_convert"),
    ):
        assert schedule[key]["task"] == task
        assert schedule[key]["schedule"] >= 3600
