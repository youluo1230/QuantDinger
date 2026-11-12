-- Unified market data layer: candle storage and coverage tracking.
--
-- The platform answered "what were these candles" three different ways before
-- this table existed: a short-lived in-memory cache for the chart, a RAM-only
-- frame cache for backtests, and a direct upstream call for the trading loop.
-- Every backend restart re-downloaded everything, and the chart and the
-- backtest could disagree about what a bar contained.
--
-- Storing bars here makes the answer durable and shared. The ranges table is
-- the ledger that turns "do we already have this range" into one indexed
-- lookup instead of a scan over the bars themselves.
--
-- Idempotent by construction: this file runs on fresh installs through
-- init.sql and again on upgrades through db.py.

CREATE TABLE IF NOT EXISTS qd_market_candles (
    id bigserial PRIMARY KEY,
    market varchar(32) NOT NULL,
    symbol varchar(64) NOT NULL,
    timeframe varchar(8) NOT NULL,
    exchange_id varchar(32) NOT NULL DEFAULT '',
    market_type varchar(16) NOT NULL DEFAULT '',
    instrument_id varchar(64) NOT NULL DEFAULT '',
    -- Unix seconds. Stored as bigint rather than timestamptz because every
    -- upstream speaks unix seconds and the repeated second<->timestamp
    -- conversions were a source of off-by-one bar boundaries.
    bar_time bigint NOT NULL,
    -- numeric, not double precision: OHLC values are compared and re-aggregated
    -- across reloads, and float drift would make a stored bar differ from the
    -- same bar fetched again.
    open numeric(24, 10) NOT NULL,
    high numeric(24, 10) NOT NULL,
    low numeric(24, 10) NOT NULL,
    close numeric(24, 10) NOT NULL,
    volume numeric(28, 10) NOT NULL DEFAULT 0,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_market_candles_bar UNIQUE (market, symbol, timeframe, exchange_id, market_type, instrument_id, bar_time)
);

-- The read path always filters by stream first and then by time range, so the
-- index leads with the full stream identity.
CREATE INDEX IF NOT EXISTS idx_market_candles_stream_time
    ON qd_market_candles (market, symbol, timeframe, exchange_id, market_type, instrument_id, bar_time);

-- Retention sweeps delete by age across all streams, which the stream index
-- cannot serve.
CREATE INDEX IF NOT EXISTS idx_market_candles_time
    ON qd_market_candles (bar_time);

CREATE TABLE IF NOT EXISTS qd_market_candle_ranges (
    id bigserial PRIMARY KEY,
    market varchar(32) NOT NULL,
    symbol varchar(64) NOT NULL,
    timeframe varchar(8) NOT NULL,
    exchange_id varchar(32) NOT NULL DEFAULT '',
    market_type varchar(16) NOT NULL DEFAULT '',
    instrument_id varchar(64) NOT NULL DEFAULT '',
    -- Inclusive bounds in unix seconds. A stored range means "every bar the
    -- venue has in this interval is present", not "we asked for this interval":
    -- the fetch code narrows the range to what actually came back.
    covered_from bigint NOT NULL,
    covered_to bigint NOT NULL,
    bar_count integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_market_candle_ranges_stream UNIQUE (market, symbol, timeframe, exchange_id, market_type, instrument_id, covered_from)
);

CREATE INDEX IF NOT EXISTS idx_market_candle_ranges_stream
    ON qd_market_candle_ranges (market, symbol, timeframe, exchange_id, market_type, instrument_id, covered_from, covered_to);

CREATE TABLE IF NOT EXISTS qd_market_candle_fetches (
    id bigserial PRIMARY KEY,
    market varchar(32) NOT NULL,
    symbol varchar(64) NOT NULL,
    timeframe varchar(8) NOT NULL,
    exchange_id varchar(32) NOT NULL DEFAULT '',
    market_type varchar(16) NOT NULL DEFAULT '',
    instrument_id varchar(64) NOT NULL DEFAULT '',
    requested_from bigint NOT NULL DEFAULT 0,
    requested_to bigint NOT NULL DEFAULT 0,
    bars_returned integer NOT NULL DEFAULT 0,
    duration_ms integer NOT NULL DEFAULT 0,
    outcome varchar(32) NOT NULL DEFAULT 'unknown',
    detail text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_market_candle_fetches_stream
    ON qd_market_candle_fetches (market, symbol, timeframe, created_at DESC);
