-- TradingView strategy harvesting staging tables.
--
-- Harvested public TradingView strategies land in qd_tv_scripts instead of
-- qd_script_sources so a daily crawl cannot flood the user's strategy list.
-- Promotion into qd_script_sources happens only after a candidate compiles and
-- backtests successfully (see app/services/tradingview_pipeline.py).
--
-- This file must stay idempotent: the bootstrap ledger re-runs a component
-- whenever its checksum changes.

CREATE TABLE IF NOT EXISTS qd_tv_scripts (
    id BIGSERIAL PRIMARY KEY,
    script_id_part VARCHAR(120) NOT NULL,
    name VARCHAR(300) NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    author VARCHAR(120) NOT NULL DEFAULT '',
    topic VARCHAR(64) NOT NULL DEFAULT '',
    tv_script_type VARCHAR(32) NOT NULL DEFAULT '',
    script_access VARCHAR(32) NOT NULL DEFAULT '',
    chart_url TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    symbol_name VARCHAR(120) NOT NULL DEFAULT '',
    pine_version INTEGER,
    source_code TEXT NOT NULL DEFAULT '',
    source_chars INTEGER NOT NULL DEFAULT 0,
    source_hash VARCHAR(64) NOT NULL DEFAULT '',
    likes_count INTEGER NOT NULL DEFAULT 0,
    views_count INTEGER NOT NULL DEFAULT 0,
    comments_count INTEGER NOT NULL DEFAULT 0,
    is_picked BOOLEAN NOT NULL DEFAULT FALSE,
    tv_created_at TIMESTAMPTZ,
    tv_updated_at TIMESTAMPTZ,
    source_fetched_at TIMESTAMPTZ,
    source_fetch_error TEXT NOT NULL DEFAULT '',
    -- Cached migratability assessment so listing does not re-parse Pine.
    migratability_score INTEGER NOT NULL DEFAULT 0,
    migratability JSONB NOT NULL DEFAULT '{}'::jsonb,
    migratability_version INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tv_scripts_script_id_part
  ON qd_tv_scripts(script_id_part);
CREATE INDEX IF NOT EXISTS idx_tv_scripts_topic_likes
  ON qd_tv_scripts(topic, likes_count DESC);
CREATE INDEX IF NOT EXISTS idx_tv_scripts_score
  ON qd_tv_scripts(migratability_score DESC, likes_count DESC);
CREATE INDEX IF NOT EXISTS idx_tv_scripts_pending_source
  ON qd_tv_scripts(source_fetched_at) WHERE source_fetched_at IS NULL;

CREATE TABLE IF NOT EXISTS qd_tv_harvest_runs (
    id BIGSERIAL PRIMARY KEY,
    trigger_type VARCHAR(20) NOT NULL DEFAULT 'manual',
    status VARCHAR(20) NOT NULL DEFAULT 'running',
    topics TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    result JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_tv_harvest_runs_started
  ON qd_tv_harvest_runs(started_at DESC);
-- Only one harvest may be in flight; a stuck run is reaped by the service
-- before a new one is claimed.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tv_harvest_runs_running
  ON qd_tv_harvest_runs ((status)) WHERE status = 'running';

-- Conversion bookkeeping: which stripped-down candidate maps to which strategy
-- source, and whether it cleared the compile/backtest gate.
CREATE TABLE IF NOT EXISTS qd_tv_conversions (
    id BIGSERIAL PRIMARY KEY,
    script_id_part VARCHAR(120) NOT NULL,
    source_id BIGINT REFERENCES qd_script_sources(id) ON DELETE SET NULL,
    transpiler VARCHAR(40) NOT NULL DEFAULT 'pine_transpiler',
    transpiler_version INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    diagnostics JSONB NOT NULL DEFAULT '{}'::jsonb,
    backtest_run_id BIGINT,
    code_hash VARCHAR(64) NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tv_conversions_script
  ON qd_tv_conversions(script_id_part);
CREATE INDEX IF NOT EXISTS idx_tv_conversions_status
  ON qd_tv_conversions(status, updated_at DESC);
