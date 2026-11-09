-- Record the chart interval a harvested TradingView strategy was published on.
--
-- TradingView exposes its canonical timeframe in the listing payload as
-- ``symbol.interval`` (for example ``60`` for one hour, ``1D`` for daily).
-- Without it every converted strategy was backtested on whatever
-- TRADINGVIEW_DEFAULT_FREQUENCY happened to be, so a 15-minute scalper was
-- judged on hourly bars. The column records what upstream actually published,
-- which is the best available evidence for the interval a strategy was designed
-- for; an empty value means upstream did not publish one and the configured
-- default is used.
--
-- This file must stay idempotent: the bootstrap ledger re-runs a component
-- whenever its checksum changes.

ALTER TABLE qd_tv_scripts
    ADD COLUMN IF NOT EXISTS chart_interval VARCHAR(16) NOT NULL DEFAULT '';

COMMENT ON COLUMN qd_tv_scripts.chart_interval IS
    'Interval the author published the script on, as TradingView reports it (raw upstream token, e.g. 60 or 1D).';
