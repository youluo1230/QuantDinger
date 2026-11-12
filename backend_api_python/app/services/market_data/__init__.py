"""Persistence for stored market candles and the ranges that cover them.

The chart, the backtester and any future consumer read bars through this
repository so that one durable answer exists instead of several process-local
caches that disagree and vanish on restart.

Two things live here and they must stay consistent with each other:

* the bars themselves, and
* the coverage ledger that answers "is this range already stored" without
  scanning the bars.

A write therefore records both inside one transaction. A range row is only
written for bars that actually came back, never for the range that was
requested: a venue that keeps thirty days of one-minute data answers a
year-long question with thirty days, and claiming a year would make every later
call silently skip the missing ten months.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.utils.db import get_db_connection


@dataclass(frozen=True)
class CandleStream:
    """Identity of one candle series.

    The venue columns are part of the identity, not decoration: BTC/USDT on
    spot and BTC/USDT on perpetual are different series, and so is the same
    ticker quoted through a different venue. Treating them as one stream would
    splice two price series together.
    """

    market: str
    symbol: str
    timeframe: str
    exchange_id: str = ""
    market_type: str = ""
    instrument_id: str = ""

    def as_params(self) -> Tuple[str, str, str, str, str, str]:
        return (
            str(self.market or ""),
            str(self.symbol or ""),
            str(self.timeframe or ""),
            str(self.exchange_id or ""),
            str(self.market_type or ""),
            str(self.instrument_id or ""),
        )

    @property
    def key(self) -> str:
        """Stable cache key that includes every column of the identity."""
        return "|".join(self.as_params())


@dataclass(frozen=True)
class CoveredRange:
    """An interval whose bars are all present in the store."""

    covered_from: int
    covered_to: int
    bar_count: int


@dataclass(frozen=True)
class CandleBar:
    """One OHLCV bar as it is written to and read from storage."""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


_STREAM_WHERE = (
    "market = ? AND symbol = ? AND timeframe = ? "
    "AND exchange_id = ? AND market_type = ? AND instrument_id = ?"
)


class MarketCandleRepository:
    """Reads and writes stored candles together with their coverage ledger."""

    def read_bars(self, stream: CandleStream, start: int, end: int, *, limit: int = 0) -> List[CandleBar]:
        """Return stored bars inside ``[start, end]``, oldest first.

        ``limit`` keeps the newest bars when the range holds more than the caller
        asked for, which is what a chart wants when it scrolls to the live edge.
        """
        where = [_STREAM_WHERE, "bar_time >= ?", "bar_time <= ?"]
        params: List[Any] = list(stream.as_params()) + [int(start), int(end)]
        if limit and limit > 0:
            # Order descending so the limit takes the newest, then flip back.
            sql = (
                f"SELECT bar_time, open, high, low, close, volume FROM qd_market_candles "
                f"WHERE {' AND '.join(where)} ORDER BY bar_time DESC LIMIT ?"
            )
            params.append(int(limit))
            rows = self._fetch(sql, params)
            rows.reverse()
        else:
            sql = (
                f"SELECT bar_time, open, high, low, close, volume FROM qd_market_candles "
                f"WHERE {' AND '.join(where)} ORDER BY bar_time ASC"
            )
            rows = self._fetch(sql, params)
        return [self._to_bar(row) for row in rows]

    def read_ranges(self, stream: CandleStream) -> List[CoveredRange]:
        """Return the stored coverage for one stream, oldest first."""
        rows = self._fetch(
            f"SELECT covered_from, covered_to, bar_count FROM qd_market_candle_ranges "
            f"WHERE {_STREAM_WHERE} ORDER BY covered_from ASC",
            list(stream.as_params()),
        )
        return [
            CoveredRange(
                covered_from=int(row["covered_from"]),
                covered_to=int(row["covered_to"]),
                bar_count=int(row.get("bar_count") or 0),
            )
            for row in rows
        ]

    def write_bars(
        self,
        stream: CandleStream,
        bars: Sequence[CandleBar],
        *,
        covered_from: Optional[int] = None,
        covered_to: Optional[int] = None,
    ) -> int:
        """Store bars and record their coverage in one transaction.

        Bars are written with ``ON CONFLICT DO NOTHING`` on the bar identity so
        that overlapping fetches -- which happen whenever two callers warm the
        same stream at once -- cannot fail the write or duplicate a bar.

        The coverage row is written only when the caller states a range, and the
        range must be the bounds of the bars that actually exist, not the bounds
        that were asked for. This is what stops a short upstream answer from
        being recorded as full coverage.
        """
        if not bars:
            return 0
        ordered = sorted(bars, key=lambda bar: int(bar.time))
        lowest = int(covered_from if covered_from is not None else ordered[0].time)
        highest = int(covered_to if covered_to is not None else ordered[-1].time)

        with get_db_connection() as db:
            cur = db.cursor()
            try:
                # A single multi-row insert keeps a long range to one statement;
                # one statement per bar made a year of one-minute candles take
                # minutes rather than seconds.
                values_sql = ", ".join(["(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"] * len(ordered))
                params: List[Any] = []
                for bar in ordered:
                    params.extend(
                        list(stream.as_params())
                        + [
                            int(bar.time),
                            float(bar.open),
                            float(bar.high),
                            float(bar.low),
                            float(bar.close),
                            float(bar.volume or 0.0),
                        ]
                    )
                cur.execute(
                    "INSERT INTO qd_market_candles "
                    "(market, symbol, timeframe, exchange_id, market_type, instrument_id, "
                    " bar_time, open, high, low, close, volume) VALUES "
                    + values_sql
                    + " ON CONFLICT (market, symbol, timeframe, exchange_id, market_type, "
                    "instrument_id, bar_time) DO NOTHING",
                    tuple(params),
                )
                inserted = int(cur.rowcount or 0)
                self._merge_range(cur, stream, lowest, highest, len(ordered))
                db.commit()
                return inserted
            finally:
                cur.close()

    def record_fetch(
        self,
        stream: CandleStream,
        *,
        requested_from: int,
        requested_to: int,
        bars_returned: int,
        duration_ms: int,
        outcome: str,
        detail: str = "",
    ) -> None:
        """Append one audit row for an upstream fetch.

        Written on its own connection after the data transaction so a failed
        audit insert can never roll back stored bars.
        """
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    "INSERT INTO qd_market_candle_fetches "
                    "(market, symbol, timeframe, exchange_id, market_type, instrument_id, "
                    " requested_from, requested_to, bars_returned, duration_ms, outcome, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(stream.as_params())
                    + (
                        int(requested_from),
                        int(requested_to),
                        int(bars_returned),
                        int(duration_ms),
                        str(outcome or "unknown")[:32],
                        str(detail or "")[:2000],
                    ),
                )
                db.commit()
            finally:
                cur.close()

    def count_bars(self, stream: CandleStream) -> int:
        rows = self._fetch(
            f"SELECT count(*) AS total FROM qd_market_candles WHERE {_STREAM_WHERE}",
            list(stream.as_params()),
        )
        return int(rows[0]["total"]) if rows else 0

    def summary(self) -> Dict[str, Any]:
        """Storage totals grouped by market, for the operator status endpoint.

        Deliberately coarse: one row per market rather than per stream, because
        a deployment holds thousands of streams and the question an operator
        asks is "is this growing", not "what is every series".
        """
        by_market = self._fetch(
            "SELECT market, count(*) AS bars, "
            "  count(DISTINCT symbol || '|' || timeframe) AS streams, "
            "  min(bar_time) AS oldest, max(bar_time) AS newest "
            "FROM qd_market_candles GROUP BY market ORDER BY market ASC",
            [],
        )
        range_rows = self._fetch(
            "SELECT market, count(*) AS ranges FROM qd_market_candle_ranges "
            "GROUP BY market ORDER BY market ASC",
            [],
        )
        recent = self._fetch(
            "SELECT outcome, count(*) AS total FROM qd_market_candle_fetches "
            "WHERE created_at >= NOW() - INTERVAL '24 hours' GROUP BY outcome",
            [],
        )
        return {
            "markets": [
                {
                    "market": str(row.get("market") or ""),
                    "bars": int(row.get("bars") or 0),
                    "streams": int(row.get("streams") or 0),
                    "oldest": int(row["oldest"]) if row.get("oldest") is not None else None,
                    "newest": int(row["newest"]) if row.get("newest") is not None else None,
                }
                for row in by_market
            ],
            "ranges": {
                str(row.get("market") or ""): int(row.get("ranges") or 0) for row in range_rows
            },
            "fetches24h": {
                str(row.get("outcome") or ""): int(row.get("total") or 0) for row in recent
            },
        }

    def prune_before(self, cutoff: int, *, batch_size: int = 50000) -> int:
        """Delete bars older than ``cutoff`` and narrow the affected ranges.

        Deleting bars without touching the ledger would leave a range claiming
        coverage for bars that no longer exist, and every later read would
        return a gap it never refills.
        """
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    "DELETE FROM qd_market_candles WHERE bar_time < ?", (int(cutoff),)
                )
                deleted = int(cur.rowcount or 0)
                cur.execute(
                    "UPDATE qd_market_candle_ranges SET covered_from = ?, updated_at = NOW() "
                    "WHERE covered_from < ? AND covered_to >= ?",
                    (int(cutoff), int(cutoff), int(cutoff)),
                )
                cur.execute(
                    "DELETE FROM qd_market_candle_ranges WHERE covered_to < ?", (int(cutoff),)
                )
                db.commit()
                return deleted
            finally:
                cur.close()

    def prune_audit_before(self, cutoff: int) -> int:
        """Delete fetch audit rows older than ``cutoff``.

        The audit answers "why was that slow" for a recent window; keeping every
        row forever would eventually cost more than the candles it describes.
        """
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    "DELETE FROM qd_market_candle_fetches WHERE created_at < to_timestamp(?)",
                    (int(cutoff),),
                )
                deleted = int(cur.rowcount or 0)
                db.commit()
                return deleted
            finally:
                cur.close()

    def _merge_range(
        self, cur, stream: CandleStream, covered_from: int, covered_to: int, bar_count: int
    ) -> None:
        """Extend coverage, joining any stored range this one touches or overlaps.

        Coverage is kept as disjoint intervals so a later read can compute gaps
        arithmetically. Adjacent fetches must therefore merge: storing
        ``[100, 200]`` and then ``[201, 300]`` as two rows is correct but makes
        every gap computation walk a list, and fetching bar 200 twice would
        create two overlapping rows that both claim it.

        The count is measured from the stored bars rather than added up. Summing
        the counts of the ranges being merged double-counts every bar that was
        fetched twice, which is the normal case when two callers warm the same
        stream or a retry re-requests a range that partly landed.
        """
        cur.execute(
            f"SELECT id, covered_from, covered_to FROM qd_market_candle_ranges "
            f"WHERE {_STREAM_WHERE} AND covered_to >= ? AND covered_from <= ? "
            f"ORDER BY covered_from ASC",
            list(stream.as_params()) + [int(covered_from) - 1, int(covered_to) + 1],
        )
        touching = list(cur.fetchall() or [])

        lowest = int(covered_from)
        highest = int(covered_to)
        if touching:
            lowest = min(lowest, *(int(row["covered_from"]) for row in touching))
            highest = max(highest, *(int(row["covered_to"]) for row in touching))
            cur.execute(
                f"DELETE FROM qd_market_candle_ranges WHERE id IN ({', '.join(['?'] * len(touching))})",
                tuple(int(row["id"]) for row in touching),
            )

        cur.execute(
            f"SELECT count(*) AS total FROM qd_market_candles "
            f"WHERE {_STREAM_WHERE} AND bar_time >= ? AND bar_time <= ?",
            list(stream.as_params()) + [lowest, highest],
        )
        counted = cur.fetchone()
        merged_count = int((counted or {}).get("total") or bar_count)

        cur.execute(
            "INSERT INTO qd_market_candle_ranges "
            "(market, symbol, timeframe, exchange_id, market_type, instrument_id, "
            " covered_from, covered_to, bar_count, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NOW()) "
            "ON CONFLICT (market, symbol, timeframe, exchange_id, market_type, instrument_id, "
            "covered_from) DO UPDATE SET covered_to = EXCLUDED.covered_to, "
            "bar_count = EXCLUDED.bar_count, updated_at = NOW()",
            tuple(stream.as_params())
            + (int(lowest), int(highest), int(merged_count)),
        )

    def _fetch(self, sql: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(sql, tuple(params))
                return [dict(row) for row in (cur.fetchall() or [])]
            finally:
                cur.close()

    @staticmethod
    def _to_bar(row: Dict[str, Any]) -> CandleBar:
        return CandleBar(
            time=int(row["bar_time"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume") or 0.0),
        )


def merge_coverage(ranges: Iterable[CoveredRange]) -> List[CoveredRange]:
    """Collapse overlapping or adjacent ranges into disjoint intervals.

    The store keeps ranges disjoint, but a caller combining results across
    streams or a ledger edited by hand can hand us overlapping intervals, and
    gap arithmetic is only correct on disjoint input.
    """
    ordered = sorted(ranges, key=lambda item: (item.covered_from, item.covered_to))
    merged: List[CoveredRange] = []
    for item in ordered:
        if merged and item.covered_from <= merged[-1].covered_to + 1:
            previous = merged[-1]
            merged[-1] = CoveredRange(
                covered_from=previous.covered_from,
                covered_to=max(previous.covered_to, item.covered_to),
                bar_count=previous.bar_count + item.bar_count,
            )
        else:
            merged.append(item)
    return merged


def missing_intervals(
    ranges: Iterable[CoveredRange], start: int, end: int
) -> List[Tuple[int, int]]:
    """Return the sub-intervals of ``[start, end]`` that are not covered.

    This is the whole point of the ledger: it turns "do we need to download" into
    arithmetic on a handful of rows instead of a scan over stored bars.
    """
    lowest = int(start)
    highest = int(end)
    if highest < lowest:
        return []
    gaps: List[Tuple[int, int]] = []
    cursor = lowest
    for item in merge_coverage(ranges):
        if item.covered_to < cursor:
            continue
        if item.covered_from > highest:
            break
        if item.covered_from > cursor:
            gaps.append((cursor, min(item.covered_from - 1, highest)))
        cursor = max(cursor, item.covered_to + 1)
        if cursor > highest:
            break
    if cursor <= highest:
        gaps.append((cursor, highest))
    return gaps


def covered_within(ranges: Iterable[CoveredRange], start: int, end: int) -> bool:
    """True when every second of ``[start, end]`` sits inside stored coverage."""
    return not missing_intervals(ranges, start, end)
