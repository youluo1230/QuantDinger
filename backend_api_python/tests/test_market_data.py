"""Coverage arithmetic and storage behaviour for the market candle store.

The range ledger is what turns "do we need to download" into arithmetic, so
most of these tests target the gap maths directly. The repository behaviour is
exercised against a fake cursor that records statements, which is enough to
pin down the two rules that matter: coverage is only claimed for bars that
exist, and overlapping writes merge instead of duplicating.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, List, Optional

import pytest

from app.services import market_data
from app.services.market_data import (
    CandleBar,
    CandleStream,
    CoveredRange,
    MarketCandleRepository,
    covered_within,
    merge_coverage,
    missing_intervals,
)


class _Cursor:
    """Cursor replaying canned rows and recording every statement."""

    def __init__(self, handler=None):
        self._handler = handler
        self._rows: List[dict[str, Any]] = []
        self.executed: List[tuple[str, tuple | None]] = []
        self.rowcount = 0

    def execute(self, query: str, params: tuple | None = None):
        self.executed.append((query, params))
        if self._handler is not None:
            self._rows = list(self._handler(query, params) or [])
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass

    def queries(self, marker: str):
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


def _install_db(monkeypatch, handler=None) -> _Cursor:
    cursor = _Cursor(handler)

    @contextmanager
    def _connect():
        yield _Connection(cursor)

    monkeypatch.setattr(market_data, "get_db_connection", _connect)
    return cursor


def _stream(**overrides) -> CandleStream:
    values = {"market": "Crypto", "symbol": "BTC/USDT", "timeframe": "1h"}
    values.update(overrides)
    return CandleStream(**values)


def test_a_fully_covered_interval_has_no_gaps():
    ranges = [CoveredRange(covered_from=0, covered_to=1000, bar_count=1001)]

    assert missing_intervals(ranges, 0, 1000) == []
    assert covered_within(ranges, 0, 1000) is True


def test_gaps_before_inside_and_after_are_all_reported():
    ranges = [
        CoveredRange(covered_from=200, covered_to=400, bar_count=201),
        CoveredRange(covered_from=600, covered_to=800, bar_count=201),
    ]

    gaps = missing_intervals(ranges, 0, 1000)

    assert gaps == [(0, 199), (401, 599), (801, 1000)]


def test_adjacent_ranges_are_joined_before_the_gap_is_computed():
    """Two ranges that touch leave no gap between them.

    The store keeps its own rows disjoint, but a merged read can hand us
    neighbours; treating ``[0,100]`` and ``[101,200]`` as separate would report a
    phantom one-second gap every time they meet.
    """
    ranges = [
        CoveredRange(covered_from=0, covered_to=100, bar_count=101),
        CoveredRange(covered_from=101, covered_to=200, bar_count=100),
    ]

    assert merge_coverage(ranges) == [CoveredRange(0, 200, 201)]
    assert missing_intervals(ranges, 0, 200) == []


def test_overlapping_ranges_do_not_double_count_their_span():
    ranges = [
        CoveredRange(covered_from=0, covered_to=500, bar_count=501),
        CoveredRange(covered_from=400, covered_to=900, bar_count=501),
    ]

    merged = merge_coverage(ranges)

    assert len(merged) == 1
    assert merged[0].covered_from == 0
    assert merged[0].covered_to == 900
    assert missing_intervals(ranges, 0, 900) == []


def test_coverage_outside_the_requested_window_is_ignored():
    ranges = [CoveredRange(covered_from=0, covered_to=10000, bar_count=10001)]

    assert missing_intervals(ranges, 500, 600) == []
    assert covered_within(ranges, 500, 600) is True


def test_an_empty_ledger_means_the_whole_window_is_missing():
    assert missing_intervals([], 100, 200) == [(100, 200)]
    assert covered_within([], 100, 200) is False


def test_a_reversed_window_has_no_gaps():
    assert missing_intervals([], 500, 100) == []


def test_coverage_is_recorded_only_for_the_bars_that_came_back(monkeypatch):
    """A short upstream answer must not be recorded as full coverage.

    Requesting a year and receiving thirty days happens constantly -- that is
    exactly what a venue with limited one-minute history does. Writing the
    requested bounds would mark the missing ten months as stored and every
    later read would return a gap it never refills.
    """
    cursor = _install_db(monkeypatch)
    repository = MarketCandleRepository()
    bars = [
        CandleBar(time=1000, open=1.0, high=2.0, low=0.5, close=1.5),
        CandleBar(time=3700, open=1.5, high=2.5, low=1.0, close=2.0),
    ]

    repository.write_bars(_stream(), bars)

    range_insert = cursor.queries("INSERT INTO qd_market_candle_ranges")[0]
    params = range_insert[1]
    # The bounds after the six identity columns are the ones actually stored.
    assert params[6] == 1000
    assert params[7] == 3700


def test_an_explicit_coverage_window_still_wins(monkeypatch):
    """Callers may widen coverage when they know the gaps are empty.

    A venue that answers a request with no bars at all (a market holiday) is
    the case where the caller knows the interval was checked but holds no bars
    to prove it.
    """
    cursor = _install_db(monkeypatch)
    repository = MarketCandleRepository()

    repository.write_bars(
        _stream(),
        [CandleBar(time=1000, open=1.0, high=2.0, low=0.5, close=1.5)],
        covered_from=1000,
        covered_to=9000,
    )

    range_insert = cursor.queries("INSERT INTO qd_market_candle_ranges")[0]
    assert range_insert[1][6] == 1000
    assert range_insert[1][7] == 9000


def test_writing_no_bars_touches_nothing(monkeypatch):
    cursor = _install_db(monkeypatch)

    assert MarketCandleRepository().write_bars(_stream(), []) == 0
    assert cursor.executed == []


def test_bars_are_written_in_one_statement(monkeypatch):
    """A year of one-minute candles must not become a million round trips."""
    cursor = _install_db(monkeypatch)
    bars = [
        CandleBar(time=1000 + index * 60, open=1.0, high=1.0, low=1.0, close=1.0)
        for index in range(25)
    ]

    MarketCandleRepository().write_bars(_stream(), bars)

    inserts = cursor.queries("INSERT INTO qd_market_candles")
    assert len(inserts) == 1
    assert inserts[0][0].count("?") == 25 * 12
    assert "ON CONFLICT" in inserts[0][0]
    assert "DO NOTHING" in inserts[0][0]


def test_a_stream_identity_includes_the_venue():
    """Spot and perpetual BTC/USDT are different price series."""
    spot = CandleStream(market="Crypto", symbol="BTC/USDT", timeframe="1h", market_type="spot")
    perp = CandleStream(market="Crypto", symbol="BTC/USDT", timeframe="1h", market_type="swap")

    assert spot.key != perp.key


def test_reading_bars_returns_oldest_first(monkeypatch):
    def handler(query, params):
        if "FROM qd_market_candles" in query and "DESC" in query:
            # A descending request must be flipped back before it leaves.
            return [
                {"bar_time": 300, "open": 3, "high": 3, "low": 3, "close": 3, "volume": 0},
                {"bar_time": 200, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 0},
            ]
        return []

    _install_db(monkeypatch, handler)

    bars = MarketCandleRepository().read_bars(_stream(), 0, 1000, limit=2)

    assert [bar.time for bar in bars] == [200, 300]


def test_reading_without_a_limit_asks_for_ascending_order(monkeypatch):
    cursor = _install_db(monkeypatch, lambda query, params: [])

    MarketCandleRepository().read_bars(_stream(), 0, 1000)

    sql = cursor.queries("FROM qd_market_candles")[0][0]
    assert "ORDER BY bar_time ASC" in sql


def test_a_failed_fetch_can_be_audited_without_touching_bars(monkeypatch):
    cursor = _install_db(monkeypatch)

    MarketCandleRepository().record_fetch(
        _stream(),
        requested_from=0,
        requested_to=1000,
        bars_returned=0,
        duration_ms=42,
        outcome="error",
        detail="upstream refused",
    )

    assert cursor.executed[0][0].startswith("INSERT INTO qd_market_candle_fetches")
    assert cursor.executed[0][1][-2] == "error"


def test_pruning_narrows_the_ledger_it_left_behind(monkeypatch):
    """Deleting bars without editing coverage would create an unfillable gap."""
    cursor = _install_db(monkeypatch)

    MarketCandleRepository().prune_before(5000)

    statements = [item[0] for item in cursor.executed]
    assert any("DELETE FROM qd_market_candles" in statement for statement in statements)
    assert any("UPDATE qd_market_candle_ranges" in statement for statement in statements)
    assert any(
        "DELETE FROM qd_market_candle_ranges" in statement for statement in statements
    )


def test_a_range_that_touches_a_stored_one_is_merged(monkeypatch):
    def handler(query, params):
        if "FROM qd_market_candle_ranges" in query and "SELECT id" in query:
            return [{"id": 7, "covered_from": 0, "covered_to": 999}]
        if "SELECT count(*)" in query:
            return [{"total": 1001}]
        return []

    cursor = _install_db(monkeypatch, handler)

    MarketCandleRepository().write_bars(
        _stream(), [CandleBar(time=1000, open=1.0, high=1.0, low=1.0, close=1.0)]
    )

    deletes = cursor.queries("DELETE FROM qd_market_candle_ranges")
    assert deletes and deletes[0][1] == (7,)
    inserts = cursor.queries("INSERT INTO qd_market_candle_ranges")
    assert inserts[0][1][6] == 0
    assert inserts[0][1][7] == 1000
    assert inserts[0][1][8] == 1001


def test_the_bar_count_is_measured_not_summed(monkeypatch):
    """Re-fetching a range must not inflate its bar count.

    Warming the same stream twice is normal -- two callers race, or a retry
    re-requests a range that partly landed. Adding the counts of the merged
    rows would report double the bars that exist, and any later "is this range
    dense enough" check would be wrong.
    """
    def handler(query, params):
        if "FROM qd_market_candle_ranges" in query and "SELECT id" in query:
            return [{"id": 3, "covered_from": 0, "covered_to": 999}]
        if "SELECT count(*)" in query:
            # The store holds 1000 rows, whatever the previous ledger claimed.
            return [{"total": 1000}]
        return []

    cursor = _install_db(monkeypatch, handler)

    MarketCandleRepository().write_bars(
        _stream(),
        [CandleBar(time=1000, open=1.0, high=1.0, low=1.0, close=1.0)],
    )

    inserts = cursor.queries("INSERT INTO qd_market_candle_ranges")
    assert inserts[0][1][8] == 1000
