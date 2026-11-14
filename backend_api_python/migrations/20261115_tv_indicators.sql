-- TradingView indicator conversions.
--
-- A strategy conversion ends in a backtest, which is why it lives in
-- ``qd_tv_conversions`` next to a run id. An indicator has no backtest: it runs
-- once in the chart sandbox and the rendered plots are the whole verdict. So it
-- needs its own table rather than another status value on a row whose other
-- columns all describe a backtest that will never happen.

CREATE TABLE IF NOT EXISTS qd_tv_indicators (
    script_id_part      VARCHAR(128) PRIMARY KEY,
    indicator_id        INTEGER,
    transpiler_version  INTEGER     NOT NULL DEFAULT 0,
    status              VARCHAR(32) NOT NULL DEFAULT 'ready',
    plots               INTEGER     NOT NULL DEFAULT 0,
    signals             INTEGER     NOT NULL DEFAULT 0,
    warnings            JSONB       NOT NULL DEFAULT '[]'::jsonb,
    diagnostics         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    code_hash           VARCHAR(64) NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tv_indicators_status
    ON qd_tv_indicators (status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_tv_indicators_indicator
    ON qd_tv_indicators (indicator_id)
    WHERE indicator_id IS NOT NULL;
