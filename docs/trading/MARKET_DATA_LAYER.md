# Unified market data layer

Status: implemented behind `MARKET_DATA_LAYER_ENABLED`. Owner: lead. Last updated: 2026-11-13.

This document is the plan for turning the current ad-hoc candle access into one
market data layer that the chart, the backtester, and any future consumer all
share. It records the decisions and the order of work; as pieces land, the
"Progress" section at the bottom tracks what exists.

## Why this exists

Today the same question -- "what were BTC/USDT 1h candles last year" -- is
answered three different ways:

| Consumer | Path | Persistence |
| --- | --- | --- |
| Chart (`/api/indicator/kline`) | `KlineService.get_kline` -> `DataSourceFactory` | In-memory `CacheManager`, TTL 3-60s |
| Backtest (`load_strategy_frame`) | process-wide `_SHARED_FRAME_CACHE` (256 entries / 256 MB) | RAM only, lost on restart |
| Trading loop | `DataSourceFactory` directly | none |

Three consequences follow, and they are the reason for the work:

1. Every backend restart re-downloads everything. A one-year 1m backtest pulls
   hundreds of thousands of bars again.
2. The chart and the backtest can disagree. Different caches with different
   TTLs mean the chart may show a bar the backtest never sees.
3. There is no recorded answer to "do we already have this range?". Every
   caller guesses, and a miss costs a full upstream fetch.

## What we are building

One layer, one entry point, one storage location:

```
consumer -> MarketDataService -> [ persistent store | upstream fetch ]
```

with these properties:

- **Range-aware caching.** A request for a range is answered from storage when
  covered, otherwise the uncovered edges are fetched and stored, then the
  request is answered. This is the behaviour the user asked for: pick an
  instrument, ask for a year, and the layer downloads whatever is missing.
- **Coverage ledger.** Storage records which `(market, symbol, timeframe)`
  ranges exist, so "do we have it" is a lookup, not a scan.
- **Single entry for every consumer.** The chart, the backtester and any new
  feature call the same service. No consumer talks to `DataSourceFactory`
  directly any more.
- **Explicit upstream budget.** Fetching is bounded by bar counts and wall
  clock, with the limits recorded per market, because the sources differ by an
  order of magnitude in what they allow.

## Storage decision

PostgreSQL is already the durable source of truth for this deployment
(`AGENTS.md` calls this out), so candles go there rather than into a new
component.

Rejected alternatives, with the reason, so nobody re-litigates them later:

- **TimescaleDB.** The running database is the stock `postgres:18.3-alpine`
  image and `pg_available_extensions` has no `timescaledb`. Adding it means a
  different image and a migration window for no benefit at this volume.
- **Parquet on disk.** Fast to scan for bulk backtests, but it is a second
  durability story next to PostgreSQL, and range deletion / coverage queries
  become file bookkeeping. Not worth it before we are actually I/O bound.
- **ClickHouse / DuckDB sidecar.** Another service to run, back up, and
  monitor. The dataset here is millions of rows, not billions.

Tables (migration `20261113_market_candles.sql`, plus the same definitions in
`init.sql` for fresh installs):

- `qd_market_candles` -- the bars. Primary key `(market, symbol, timeframe,
  bar_time)`. Stored as `numeric` for OHLC to avoid float drift across reloads,
  `bigint` volume.
- `qd_market_candle_ranges` -- the coverage ledger. One row per contiguous
  stored range per stream, with `covered_from` / `covered_to` (unix seconds)
  and `bar_count`. Updated in the same transaction as the bars.
- `qd_market_candle_fetches` -- an audit row per upstream fetch (stream,
  requested range, bars, duration, outcome). This is what makes "why is this
  slow" answerable later.

Partitioning is deliberately deferred. At one year of 1m bars for a few hundred
instruments the table is tens of millions of rows, which a single index handles;
partitioning by month is a cheap later change if the table grows past that.

## Fetch budget and rate limits

The sources are not interchangeable, so the layer enforces per-market limits
rather than one global setting:

| Market | Per-request bars | Notes |
| --- | --- | --- |
| Crypto | 1000 | Venue pagination exists already; keep its 180s wall clock |
| USStock | 1000 | yfinance / finnhub |
| Forex | 1000 | Twelve Data -> Tiingo -> yfinance fallback chain |
| Futures | 1000 | same chain |
| CNStock / HKStock | 500 | Tencent -> yfinance -> AkShare |

A request for more bars than the limit is split into contiguous chunks and
fetched sequentially, which is what the existing crypto pagination does. The
layer reuses that behaviour instead of inventing a second one.

## API surface

The layer is reached through one router, `app/routes/market_data.py`, mounted
at `/api/market-data`. The existing `/api/indicator/kline` becomes a thin
alias so the current frontend keeps working while the chart is migrated.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/market-data/candles` | Bars for a range, filling gaps from upstream as needed |
| `GET /api/market-data/coverage` | What ranges are already stored for a stream |
| `POST /api/market-data/prefetch` | Warm a range without returning every bar |
| `GET /api/market-data/status` | Storage size and per-stream coverage summary |

`/candles` keeps the current response shape (`{time, open, high, low, close,
volume}`) so callers do not change. A `source` field reports whether the answer
came from storage, upstream, or both, which is what makes the layer observable
during rollout.

## Work order

Each step is independently verifiable and leaves the system working.

1. **Schema and storage primitives.** Migration, `init.sql` entry, registration
   in `app/utils/db.py`, and a `MarketCandleRepository` with `read_range`,
   `write_bars`, `read_ranges`, `write_range`. Tests against a fake cursor.
2. **MarketDataService.** Range resolution (what is covered, what is missing),
   chunked upstream fetch through the existing `DataSourceFactory`, transactional
   write-then-read. Behind a feature flag, defaulting off.
3. **Route and alias.** `/api/market-data/*`, `/api/indicator/kline` delegating
   to it when the flag is on.
4. **Backtest integration.** `load_strategy_frame` reads through the service so
   backtests and charts share one store. This is the step that removes the
   restart-redownload problem.
5. **Frontend unification.** The charting datafeed and the klinecharts path both
   call `/api/market-data/candles`.
6. **Operational surface.** Status endpoint, retention policy (how long to keep
   bars per timeframe), and a periodic job that prunes and reports.

## Progress

- [x] Facts confirmed: PostgreSQL 18.3 without timescaledb, no candle table
      exists, `CacheManager` is memory-first, backtest cache is RAM-only,
      `after_time` is supported by every data source, crypto pagination has a
      180s budget.
- [x] Step 1: schema and storage primitives
      (`migrations/20261113_market_candles.sql`, `app/services/market_data/`).
- [x] Step 2: service
      (`app/services/market_data/service.py`; coverage addressed from stored
      ranges, only gaps fetched, every fetch audited).
- [x] Step 3: routes (`/api/market-data/{candles,coverage,prefetch,status}`).
      The legacy `/api/indicator/kline` answers through the layer when it is on
      and falls back to its own path when it is off or cannot answer.
- [x] Step 4: backtest integration (`load_strategy_frame` reads a fully
      covered window from the store and otherwise takes its own path).
- [ ] Step 5: frontend unification. The chart still calls
      `/api/indicator/kline`, which now routes through the layer, so the
      browser is already served by it; the Charting Library datafeed in
      `QuantDinger-Vue` still needs to point at `/api/market-data/candles`
      directly before the legacy endpoint can be retired.
- [x] Step 6: operations: `/api/market-data/status`, and the scheduled
      `prune_market_candles` task (`MARKET_DATA_RETENTION_DAYS`,
      `MARKET_DATA_AUDIT_RETENTION_DAYS`).

## Measured on the real stack

Cold then warm, `Crypto:BTC/USDT 1H`, one week:

| Call | Result |
| --- | --- |
| First request, empty store | 3.3 s, 168 bars stored |
| Same request again | 0.65 s, zero upstream calls |
| Store read with the window covered | 0.03 s |
| Backtest `load_strategy_frame` on a covered window | 0.03 s, no network |

Two boundary rules were found by running it against the live venue rather than
by testing against mocks, and both are now covered by regression tests:

* the lower bound of a gap rounds **up** to the next bar boundary, because
  `missing_intervals` reports one second past the last covered bar and asking
  for that stored bar again would leave the gap permanently open;
* the upper bound is handed to the source as an **exclusive** cutoff one bar
  past the last wanted bar, because sources filter with `time < before_time`.
  Passing the boundary itself silently drops the final bar; passing the one
  after requests a bar that is still forming, and the source then rejects the
  whole window instead of trimming it.

## Configuration

All of these are read at call time, so changing one takes effect on the next
request without a rebuild.

| Variable | Default | Effect |
| --- | --- | --- |
| `MARKET_DATA_LAYER_ENABLED` | `false` | Routes the chart, the backtester and the new API through the store. Off means every caller keeps its old path. |
| `MARKET_DATA_FETCH_BUDGET_SECONDS` | `90` | Wall-clock ceiling on one request's downloads. |
| `MARKET_DATA_MAX_CHUNKS` | `40` | Upstream calls allowed per request. |
| `MARKET_DATA_MAX_BARS` | `20000` | Bars returned in one response. |
| `MARKET_DATA_RETENTION_DAYS` | `400` | Bars older than this are pruned. |
| `MARKET_DATA_AUDIT_RETENTION_DAYS` | `14` | Fetch audit rows older than this are pruned. |
| `MARKET_DATA_PRUNE_INTERVAL_SEC` | `86400` | How often the prune task runs. |

## Rolling it out

1. Apply `market-candles-20261113` (the migration runner does this on start).
2. Set `MARKET_DATA_LAYER_ENABLED=true` and restart the backend.
3. Watch `/api/market-data/status`: `markets[].bars` growing while `fetches24h`
   stays proportional to real demand is the healthy shape. A large `empty`
   count means a source is being asked for ranges it does not serve, which is
   the signal to narrow the request rather than to retry harder.
4. Turning it off is the rollback: every caller returns to its previous path
   and the stored rows simply stop being read.
