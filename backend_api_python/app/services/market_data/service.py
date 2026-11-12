"""One place that answers "give me candles for this range".

Charts, backtests and the trading loop all ask the same question and used to
answer it three different ways, each with its own cache that the others could
not see. This service is the single answer: it reads what is stored, fetches
only what is missing, and writes what it fetched so the next caller does not
pay for it again.

The rules that keep it honest:

* Storage is consulted before any network call, and a range that is already
  covered costs nothing.
* Only the uncovered intervals are fetched. Asking for a year when a month is
  stored pulls eleven months, not twelve.
* Coverage is recorded from the bars that came back, never from the range that
  was requested, so a venue that keeps thirty days cannot make the ledger claim
  a year.
* Every upstream fetch is bounded and audited. A source that starts timing out
  shows up as failed audit rows rather than a chart that silently hangs.

Enablement is explicit. ``MARKET_DATA_LAYER_ENABLED`` defaults to off during
rollout; the existing code paths keep working until an operator turns it on.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.data_sources.base import timeframe_seconds
from app.data_sources.factory import DataSourceFactory
from app.services.market_data import (
    CandleBar,
    CandleStream,
    CoveredRange,
    MarketCandleRepository,
    covered_within,
    missing_intervals,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Bars requested per upstream call. Venues differ in what they will serve, so
#: this is the conservative floor that every source accepts; a range needing
#: more is split into contiguous chunks.
FETCH_CHUNK_BARS = 500

#: Ceiling on wall-clock time spent fetching for one request. A chart asking
#: for a year of one-minute bars on a cold store would otherwise hold the
#: request open for minutes.
FETCH_BUDGET_SECONDS = float(os.getenv("MARKET_DATA_FETCH_BUDGET_SECONDS", "90") or 90)

#: How many chunks one request may fetch before it answers with what it has.
MAX_CHUNKS_PER_REQUEST = int(os.getenv("MARKET_DATA_MAX_CHUNKS", "40") or 40)

#: Upper bound on bars returned for a single request, so a year of one-minute
#: data does not travel to the browser by accident.
MAX_BARS_PER_RESPONSE = int(os.getenv("MARKET_DATA_MAX_BARS", "20000") or 20000)


def is_market_data_layer_enabled() -> bool:
    """Whether requests should be answered by this layer.

    Off by default: the layer writes to the database, and a deployment that has
    not applied the migration must keep working exactly as before.
    """
    raw = str(os.getenv("MARKET_DATA_LAYER_ENABLED", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


@dataclass
class CandleResult:
    """The answer to a candle request, with how it was assembled."""

    bars: List[CandleBar] = field(default_factory=list)
    source: str = "none"
    fetched_bars: int = 0
    stored_bars: int = 0
    missing: List[tuple[int, int]] = field(default_factory=list)
    truncated: bool = False
    errors: List[str] = field(default_factory=list)

    def as_rows(self) -> List[Dict[str, Any]]:
        """Return the shape every existing consumer already understands."""
        return [
            {
                "time": bar.time,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in self.bars
        ]


class MarketDataService:
    """Reads, fills and stores candle ranges for every consumer."""

    def __init__(self, repository: Optional[MarketCandleRepository] = None):
        self._repository = repository or MarketCandleRepository()

    def get_candles(
        self,
        stream: CandleStream,
        *,
        start: Optional[int],
        end: Optional[int],
        limit: int = 0,
        allow_fetch: bool = True,
    ) -> CandleResult:
        """Return bars for ``[start, end]``, fetching what is not stored.

        ``start``/``end`` may be omitted to mean "the most recent ``limit``
        bars", which is what a chart asks for when it scrolls to the live edge.
        """
        if start is None or end is None:
            resolved = self._resolve_window(stream, start=start, end=end, limit=limit)
            if resolved is None:
                return CandleResult(bars=[], source="none")
            start, end = resolved

        start, end = int(start), int(end)
        if end < start:
            return CandleResult(bars=[], source="none")

        ranges = self._repository.read_ranges(stream)
        gaps = missing_intervals(ranges, start, end)
        fetched = 0
        errors: List[str] = []

        if gaps and allow_fetch:
            fetched, errors = self._fill_gaps(stream, gaps)
            if fetched:
                ranges = self._repository.read_ranges(stream)

        bars = self._repository.read_bars(stream, start, end, limit=limit)
        truncated = False
        if limit and len(bars) > limit:
            bars = bars[-limit:]
            truncated = True
        elif not limit and len(bars) > MAX_BARS_PER_RESPONSE:
            bars = bars[-MAX_BARS_PER_RESPONSE:]
            truncated = True

        if fetched and bars:
            source = "mixed"
        elif fetched:
            source = "upstream"
        elif bars:
            source = "storage"
        else:
            source = "none"

        return CandleResult(
            bars=bars,
            source=source,
            fetched_bars=fetched,
            stored_bars=len(bars),
            missing=gaps,
            truncated=truncated,
            errors=errors,
        )

    def prefetch(self, stream: CandleStream, *, start: int, end: int) -> Dict[str, Any]:
        """Warm a range without returning the bars.

        Used before a backtest, where the caller wants the data resident and
        does not want to carry a year of bars through the request.
        """
        start, end = int(start), int(end)
        ranges = self._repository.read_ranges(stream)
        gaps = missing_intervals(ranges, start, end)
        if not gaps:
            return {
                "requested": [start, end],
                "missing": [],
                "fetched": 0,
                "covered": True,
                "errors": [],
            }
        fetched, errors = self._fill_gaps(stream, gaps)
        remaining = missing_intervals(self._repository.read_ranges(stream), start, end)
        return {
            "requested": [start, end],
            "missing": gaps,
            "fetched": fetched,
            "covered": not remaining,
            "errors": errors,
        }

    def coverage(self, stream: CandleStream) -> Dict[str, Any]:
        """Report what is stored for a stream."""
        ranges = self._repository.read_ranges(stream)
        return {
            "stream": {
                "market": stream.market,
                "symbol": stream.symbol,
                "timeframe": stream.timeframe,
                "exchange_id": stream.exchange_id,
                "market_type": stream.market_type,
                "instrument_id": stream.instrument_id,
            },
            "ranges": [
                {
                    "from": item.covered_from,
                    "to": item.covered_to,
                    "bars": item.bar_count,
                }
                for item in ranges
            ],
            "counts": {
                "ranges": len(ranges),
                "bars": self._repository.count_bars(stream),
            },
        }

    def _resolve_window(
        self, stream: CandleStream, *, start: Optional[int], end: Optional[int], limit: int
    ) -> Optional[tuple[int, int]]:
        """Turn "the newest N bars" into a concrete interval.

        A chart asking for the live edge gives a bar count, not a date range.
        Anchoring on the newest stored bar keeps the answer stable when the
        store is warm; a cold store starts from the wall clock.
        """
        seconds = timeframe_seconds(stream.timeframe)
        wanted = max(1, int(limit or 300))
        ranges = self._repository.read_ranges(stream)
        if ranges:
            anchor = max(item.covered_to for item in ranges)
        else:
            anchor = int(time.time())
        return anchor - seconds * wanted, anchor

    def _fill_gaps(
        self, stream: CandleStream, gaps: Sequence[tuple[int, int]]
    ) -> tuple[int, List[str]]:
        """Fetch uncovered intervals, one bounded chunk at a time."""
        seconds = timeframe_seconds(stream.timeframe)
        deadline = time.monotonic() + FETCH_BUDGET_SECONDS
        fetched = 0
        errors: List[str] = []
        chunks = 0

        for gap_start, gap_end in gaps:
            cursor = int(gap_start)
            while cursor <= int(gap_end):
                if chunks >= MAX_CHUNKS_PER_REQUEST:
                    return fetched, errors
                if time.monotonic() >= deadline:
                    errors.append("fetch budget exhausted")
                    return fetched, errors

                chunk_end = min(cursor + seconds * FETCH_CHUNK_BARS, int(gap_end))
                inserted, error = self._fetch_chunk(stream, cursor, chunk_end, seconds=seconds)
                fetched += inserted
                if error:
                    errors.append(error)
                chunks += 1

                # Advance past what came back. A venue that returns nothing for
                # this window would otherwise be asked again forever, so the
                # cursor moves regardless.
                cursor = chunk_end + seconds

        return fetched, errors

    def _fetch_chunk(
        self, stream: CandleStream, start: int, end: int, *, seconds: int
    ) -> tuple[int, str]:
        """Fetch one contiguous window and store whatever it yields.

        Returns the number of bars stored and an error string when the upstream
        answered badly. Failure is reported rather than raised: a chart with a
        partial range is more useful than a chart with no range.
        """
        # Both bounds are bar boundaries, and they align in opposite directions.
        #
        # The lower bound rounds *up*. A gap begins one second after the last
        # covered bar, so rounding down would re-request that stored bar; the
        # fetch would then look like it missed and the gap would never close.
        #
        # The upper bound is the last bar that has already closed, and it is
        # handed to the venue as an *exclusive* cutoff because sources filter
        # with ``time < before_time``. Passing the boundary itself drops the bar
        # that starts there; passing the one after asks for a bar that is still
        # forming, and the source's coverage check then rejects the whole
        # window rather than trimming it.
        start = int(start)
        if start % seconds:
            start += seconds - start % seconds
        end = int(end) - int(end) % seconds
        last_closed = int(time.time()) // seconds * seconds
        if end > last_closed:
            end = last_closed
        if end < start:
            end = start
        cutoff = end + seconds
        count = max(1, int((end - start) // seconds) + 1)
        limit = min(count, FETCH_CHUNK_BARS)
        started = time.monotonic()
        try:
            rows = DataSourceFactory.get_kline(
                market=stream.market,
                symbol=stream.symbol,
                timeframe=stream.timeframe,
                limit=limit,
                before_time=int(cutoff),
                after_time=int(start),
                exchange_id=stream.exchange_id or None,
                market_type=stream.market_type or None,
                closed_only=True,
            )
        except Exception as exc:  # noqa: BLE001 - upstream failure must not break the read
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Market data fetch failed for %s %s %s: %s",
                stream.market,
                stream.symbol,
                stream.timeframe,
                detail,
            )
            self._audit(
                stream,
                start=start,
                end=end,
                bars=0,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                outcome="error",
                detail=detail,
            )
            return 0, detail

        bars = self._to_bars(rows)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if not bars:
            self._audit(
                stream,
                start=start,
                end=end,
                bars=0,
                elapsed_ms=elapsed_ms,
                outcome="empty",
                detail="",
            )
            return 0, ""

        # Coverage follows the bars, not the request. A venue holding thirty
        # days of one-minute history answers a year-long ask with thirty days,
        # and recording the requested bounds would make every later read skip
        # the ten months that were never stored.
        inserted = self._repository.write_bars(stream, bars)
        self._audit(
            stream,
            start=start,
            end=end,
            bars=len(bars),
            elapsed_ms=elapsed_ms,
            outcome="ok",
            detail="",
        )
        return inserted, ""

    def _audit(
        self,
        stream: CandleStream,
        *,
        start: int,
        end: int,
        bars: int,
        elapsed_ms: int,
        outcome: str,
        detail: str,
    ) -> None:
        try:
            self._repository.record_fetch(
                stream,
                requested_from=int(start),
                requested_to=int(end),
                bars_returned=int(bars),
                duration_ms=int(elapsed_ms),
                outcome=outcome,
                detail=detail,
            )
        except Exception as exc:  # noqa: BLE001 - auditing must never break a read
            logger.debug("Market data audit write failed: %s", exc)

    @staticmethod
    def _to_bars(rows: Sequence[Dict[str, Any]]) -> List[CandleBar]:
        """Normalise whatever a source returned into stored bars.

        Sources return epoch seconds or milliseconds depending on the venue;
        storing milliseconds would put the same bar in two different rows
        depending on which source answered.
        """
        bars: List[CandleBar] = []
        for row in rows or []:
            try:
                raw_time = int(row.get("time"))
            except (AttributeError, TypeError, ValueError):
                continue
            if raw_time > 10_000_000_000:
                raw_time //= 1000
            try:
                bars.append(
                    CandleBar(
                        time=raw_time,
                        open=float(row.get("open") or 0.0),
                        high=float(row.get("high") or 0.0),
                        low=float(row.get("low") or 0.0),
                        close=float(row.get("close") or 0.0),
                        volume=float(row.get("volume") or 0.0),
                    )
                )
            except (TypeError, ValueError):
                continue
        return bars


_service: Optional[MarketDataService] = None


def get_market_data_service() -> MarketDataService:
    """Return the process-wide service."""
    global _service
    if _service is None:
        _service = MarketDataService()
    return _service


def covered_or_none(stream: CandleStream, start: int, end: int) -> Optional[bool]:
    """Small helper for callers that only need the coverage answer."""
    try:
        ranges: List[CoveredRange] = MarketCandleRepository().read_ranges(stream)
    except Exception:  # noqa: BLE001 - a missing table must not break callers
        return None
    return covered_within(ranges, start, end)


def fetch_through_layer(
    *,
    market: str,
    symbol: str,
    timeframe: str,
    limit: int,
    before_time: Optional[int] = None,
    exchange_id: Optional[str] = None,
    market_type: Optional[str] = None,
    instrument_id: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Answer a legacy "latest N bars" request from the candle store.

    Returns ``None`` when the layer is off or cannot answer, which tells the
    caller to fall back to its own path. That fallback is what makes enabling
    the flag safe: an unapplied migration or an empty store degrades to the old
    behaviour instead of emptying the chart.
    """
    if not is_market_data_layer_enabled():
        return None
    try:
        seconds = timeframe_seconds(timeframe)
    except ValueError:
        return None

    stream = CandleStream(
        market=market,
        symbol=symbol,
        timeframe=timeframe,
        exchange_id=(exchange_id or "").strip(),
        market_type=(market_type or "").strip(),
        instrument_id=(instrument_id or "").strip(),
    )
    # ``before_time`` is exclusive on the upstream path, so the newest wanted
    # bar ends one second earlier.
    end = int(before_time) - 1 if before_time else None
    start = int(before_time) - seconds * int(limit) if before_time else None
    try:
        result = get_market_data_service().get_candles(
            stream, start=start, end=end, limit=int(limit)
        )
    except Exception as exc:  # noqa: BLE001 - a broken layer must not break the chart
        logger.warning("Market data layer unavailable, falling back: %s", exc)
        return None
    if not result.bars:
        return None
    return result.as_rows()


def fetch_range_through_layer(
    *,
    market: str,
    symbol: str,
    timeframe: str,
    start: int,
    end: int,
    exchange_id: Optional[str] = None,
    market_type: Optional[str] = None,
    instrument_id: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Answer an explicit range request for a caller that validates coverage.

    Unlike :func:`fetch_through_layer`, this returns rows only when the ledger
    claims the whole range is stored. A backtest rejects an incomplete window
    outright, so handing it a partial answer would turn "the store has most of
    it" into a failed run; returning ``None`` instead lets the caller take its
    own path and get the same quality of answer it got before.

    The range is inclusive and expressed in unix seconds, matching the store.
    """
    if not is_market_data_layer_enabled():
        return None
    if end <= start:
        return None
    try:
        timeframe_seconds(timeframe)
    except ValueError:
        return None

    stream = CandleStream(
        market=market,
        symbol=symbol,
        timeframe=timeframe,
        exchange_id=(exchange_id or "").strip(),
        market_type=(market_type or "").strip(),
        instrument_id=(instrument_id or "").strip(),
    )
    try:
        result = get_market_data_service().get_candles(
            stream, start=int(start), end=int(end), limit=0
        )
        if not result.bars:
            return None
        ranges = MarketCandleRepository().read_ranges(stream)
    except Exception as exc:  # noqa: BLE001 - fall back rather than fail a run
        logger.warning("Market data layer unavailable for a range request: %s", exc)
        return None
    if not covered_within(ranges, int(start), int(end)):
        return None
    return result.as_rows()
