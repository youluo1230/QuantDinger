"""Behaviour of the layer that decides what to fetch and what to store.

The repository tests cover storage; these cover the decisions on top of it:
which gaps get fetched, what happens when an upstream answers badly, and the
rule that a bar count is turned into a range without inventing coverage.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.services import market_data
from app.services.market_data import CandleBar, CandleStream, CoveredRange
from app.services.market_data import service as service_module
from app.services.market_data.service import CandleResult, MarketDataService


class _Repository:
    """In-memory stand-in for the SQL repository."""

    def __init__(self, bars: Optional[List[CandleBar]] = None, ranges: Optional[List[CoveredRange]] = None):
        self.bars = list(bars or [])
        self.ranges = list(ranges or [])
        self.writes: List[List[CandleBar]] = []
        self.audits: List[Dict[str, Any]] = []

    def read_bars(self, stream, start, end, *, limit=0):
        picked = [bar for bar in self.bars if start <= bar.time <= end]
        picked.sort(key=lambda bar: bar.time)
        if limit and len(picked) > limit:
            picked = picked[-limit:]
        return picked

    def read_ranges(self, stream):
        return list(self.ranges)

    def write_bars(self, stream, bars, *, covered_from=None, covered_to=None):
        self.writes.append(list(bars))
        existing = {bar.time for bar in self.bars}
        new = [bar for bar in bars if bar.time not in existing]
        self.bars.extend(new)
        self.bars.sort(key=lambda bar: bar.time)
        if new:
            lowest = min(bar.time for bar in new)
            highest = max(bar.time for bar in new)
            self.ranges = [CoveredRange(lowest, highest, len(new))]
        return len(new)

    def record_fetch(self, stream, **kwargs):
        self.audits.append(kwargs)

    def count_bars(self, stream):
        return len(self.bars)


def _stream(**overrides) -> CandleStream:
    values = {"market": "Crypto", "symbol": "BTC/USDT", "timeframe": "1H"}
    values.update(overrides)
    return CandleStream(**values)


def _bar_row(timestamp: int, close: float = 100.0) -> Dict[str, Any]:
    return {
        "time": timestamp,
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 5.0,
    }


def test_a_covered_request_never_calls_upstream(monkeypatch):
    """Storage first is the whole point: a warm store costs no network."""
    repository = _Repository(
        bars=[CandleBar(time=3600, open=1, high=1, low=1, close=1)],
        ranges=[CoveredRange(3600, 3600, 1)],
    )
    service = MarketDataService(repository=repository)
    called: List[Any] = []

    def _get_kline(**kwargs):
        called.append(kwargs)
        return []

    monkeypatch.setattr(service_module.DataSourceFactory, "get_kline", staticmethod(_get_kline))

    result = service.get_candles(_stream(), start=3600, end=3600)

    assert called == []
    assert result.source == "storage"
    assert [bar.time for bar in result.bars] == [3600]


def test_only_the_uncovered_part_is_fetched(monkeypatch):
    """A stored month inside a year request must not be downloaded again."""
    repository = _Repository(
        bars=[CandleBar(time=index * 3600, open=1, high=1, low=1, close=1) for index in range(1, 25)],
        ranges=[CoveredRange(3600, 86400, 24)],
    )
    service = MarketDataService(repository=repository)
    requested: List[tuple[int, int]] = []

    def _get_kline(**kwargs):
        requested.append((kwargs["after_time"], kwargs["before_time"]))
        after = kwargs["after_time"]
        return [_bar_row(after + 3600)]

    monkeypatch.setattr(service_module.DataSourceFactory, "get_kline", staticmethod(_get_kline))
    monkeypatch.setattr(service_module, "FETCH_CHUNK_BARS", 500)

    service.get_candles(_stream(), start=0, end=86400 + 3600 * 2)

    # The stored window is untouched; only the edges are requested.
    assert all(not (after >= 3600 and before <= 86400) for after, before in requested)


def test_coverage_is_recorded_from_returned_bars(monkeypatch):
    """A short upstream answer must not claim the requested range."""
    repository = _Repository()
    service = MarketDataService(repository=repository)

    def _get_kline(**kwargs):
        return [_bar_row(10_000), _bar_row(13_600)]

    monkeypatch.setattr(service_module.DataSourceFactory, "get_kline", staticmethod(_get_kline))

    service.get_candles(_stream(), start=10_000, end=500_000)

    written = repository.writes[0]
    assert min(bar.time for bar in written) == 10_000
    assert max(bar.time for bar in written) == 13_600
    assert repository.ranges[0].covered_to == 13_600


def test_an_upstream_error_does_not_break_the_read(monkeypatch):
    """A chart with stored bars beats an exception."""
    repository = _Repository(
        bars=[CandleBar(time=3600, open=1, high=1, low=1, close=1)],
        ranges=[CoveredRange(3600, 3600, 1)],
    )
    service = MarketDataService(repository=repository)

    def _get_kline(**kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(service_module.DataSourceFactory, "get_kline", staticmethod(_get_kline))

    result = service.get_candles(_stream(), start=3600, end=7200)

    assert [bar.time for bar in result.bars] == [3600]
    assert result.errors and "upstream down" in result.errors[0]
    assert repository.audits and repository.audits[-1]["outcome"] == "error"


def test_fetching_is_bounded_by_the_configured_budget(monkeypatch):
    """A cold store must not hold a request open indefinitely."""
    repository = _Repository()
    service = MarketDataService(repository=repository)
    monkeypatch.setattr(service_module, "FETCH_BUDGET_SECONDS", 0)
    monkeypatch.setattr(service_module.DataSourceFactory, "get_kline", staticmethod(lambda **k: []))

    result = service.get_candles(_stream(), start=0, end=3600 * 5000)

    assert "fetch budget exhausted" in result.errors


def test_the_chunk_ceilings_stop_a_runaway_fetch(monkeypatch):
    repository = _Repository()
    service = MarketDataService(repository=repository)
    monkeypatch.setattr(service_module, "MAX_CHUNKS_PER_REQUEST", 2)
    monkeypatch.setattr(service_module, "FETCH_CHUNK_BARS", 10)
    monkeypatch.setattr(service_module.DataSourceFactory, "get_kline", staticmethod(lambda **k: []))

    service.get_candles(_stream(), start=0, end=3600 * 1000)

    # Only the two allowed chunks were attempted.
    assert len(repository.audits) == 2


def test_millisecond_timestamps_are_normalised(monkeypatch):
    """Venues disagree, and storing both units would duplicate every bar."""
    repository = _Repository()
    service = MarketDataService(repository=repository)
    monkeypatch.setattr(
        service_module.DataSourceFactory,
        "get_kline",
        staticmethod(lambda **k: [_bar_row(1_700_000_000_000)]),
    )

    service.get_candles(_stream(), start=1_699_999_000, end=1_700_001_000)

    assert repository.writes[0][0].time == 1_700_000_000


def test_prefetch_reports_what_is_still_missing(monkeypatch):
    repository = _Repository()
    service = MarketDataService(repository=repository)
    monkeypatch.setattr(service_module.DataSourceFactory, "get_kline", staticmethod(lambda **k: []))

    report = service.prefetch(_stream(), start=0, end=7200)

    assert report["covered"] is False
    assert report["missing"] == [(0, 7200)]


def test_an_unsupported_timeframe_is_rejected_before_any_fetch(monkeypatch):
    repository = _Repository()
    service = MarketDataService(repository=repository)
    called: List[Any] = []
    monkeypatch.setattr(
        service_module.DataSourceFactory,
        "get_kline",
        staticmethod(lambda **k: called.append(k) or []),
    )

    with pytest.raises(ValueError):
        service.get_candles(_stream(timeframe="7H"), start=0, end=3600)

    assert called == []


def test_the_layer_stays_off_until_it_is_turned_on(monkeypatch):
    monkeypatch.delenv("MARKET_DATA_LAYER_ENABLED", raising=False)
    assert service_module.is_market_data_layer_enabled() is False

    monkeypatch.setenv("MARKET_DATA_LAYER_ENABLED", "true")
    assert service_module.is_market_data_layer_enabled() is True

    monkeypatch.setenv("MARKET_DATA_LAYER_ENABLED", "0")
    assert service_module.is_market_data_layer_enabled() is False


def test_a_chart_bar_count_becomes_a_window_anchored_on_stored_data(monkeypatch):
    """A live-edge request must not depend on the wall clock when data exists."""
    stored = [
        CandleBar(time=1_700_100_000 - index * 3600, open=1, high=1, low=1, close=1)
        for index in range(10)
    ]
    repository = _Repository(
        bars=stored,
        ranges=[CoveredRange(1_700_000_000, 1_700_100_000, 100)],
    )
    service = MarketDataService(repository=repository)
    monkeypatch.setattr(service_module.DataSourceFactory, "get_kline", staticmethod(lambda **k: []))

    result = service.get_candles(_stream(), start=None, end=None, limit=10, allow_fetch=False)

    assert result.source == "storage"
    assert len(result.bars) == 10
    # Anchored on the newest stored second, not on now.
    assert result.bars[-1].time == 1_700_100_000


def test_a_gap_starting_mid_bar_does_not_rerequest_the_stored_bar(monkeypatch):
    """The lower bound rounds up, or the gap can never close.

    ``missing_intervals`` returns one second past the last covered bar. Feeding
    that to the venue as-is asks for a bar that is already stored; the fetch
    then "finds" it, records the same coverage, and the next read computes the
    identical gap -- which is exactly the loop this guards.
    """
    repository = _Repository(
        bars=[CandleBar(time=3600, open=1, high=1, low=1, close=1)],
        ranges=[CoveredRange(3600, 3600, 1)],
    )
    service = MarketDataService(repository=repository)
    requested: List[tuple[int, int]] = []

    def _get_kline(**kwargs):
        requested.append((kwargs["after_time"], kwargs["before_time"]))
        return [_bar_row(kwargs["after_time"])]

    monkeypatch.setattr(service_module.DataSourceFactory, "get_kline", staticmethod(_get_kline))

    service.get_candles(_stream(), start=3600, end=3600 * 5)

    assert requested
    after = requested[0][0]
    assert after % 3600 == 0, f"lower bound must land on a bar boundary, got {after}"
    assert after > 3600, "the stored bar must not be requested again"


def test_the_venue_cutoff_is_past_the_last_wanted_bar(monkeypatch):
    """The upper bound is exclusive upstream, so it points one bar later.

    Sources filter with ``time < before_time``. Handing them the boundary of the
    last wanted bar silently drops it, and asking for the boundary itself
    instead pulls a bar that is still forming, which the source's own coverage
    check then rejects together with the whole window.
    """
    repository = _Repository()
    service = MarketDataService(repository=repository)
    requested: List[tuple[int, int]] = []

    def _get_kline(**kwargs):
        requested.append((kwargs["after_time"], kwargs["before_time"]))
        return []

    monkeypatch.setattr(service_module.DataSourceFactory, "get_kline", staticmethod(_get_kline))

    last_wanted = 3600 * 10
    service.get_candles(_stream(), start=0, end=last_wanted)

    assert requested
    after, before = requested[0]
    assert before == last_wanted + 3600, f"cutoff must be exclusive, got {before}"
    assert before - after >= 3600 * 10


def test_a_window_reaching_into_the_future_stops_at_the_last_closed_bar(monkeypatch):
    """A live chart asks for bars that do not exist yet; do not ask the venue."""
    repository = _Repository()
    service = MarketDataService(repository=repository)
    requested: List[tuple[int, int]] = []

    def _get_kline(**kwargs):
        requested.append((kwargs["after_time"], kwargs["before_time"]))
        return []

    monkeypatch.setattr(service_module.DataSourceFactory, "get_kline", staticmethod(_get_kline))

    now = int(service_module.time.time())
    boundary = now // 3600 * 3600
    service.get_candles(_stream(), start=boundary - 3600 * 3, end=boundary + 3600 * 50)

    assert requested
    _after, before = requested[0]
    assert before <= boundary + 3600, "must not ask for a bar that is still forming"
