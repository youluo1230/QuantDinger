# TradingView Strategy Pipeline

QuantDinger cannot execute Pine Script. This pipeline closes that gap for one
narrow, high-value case: importing **public TradingView strategies** into the
strategy marketplace as native Strategy API V2 code.

It is deliberately not a Pine interpreter. Every candidate passes through the
same gates a hand-written strategy does — transpile, compile, backtest, publish
— and anything that fails a gate is recorded and dropped rather than shipped.

## What it does

The pipeline has four stages, each a separate service call, Celery task, and
admin-only HTTP endpoint:

1. **Harvest** — crawl public TradingView strategy listings, then fetch each
   script's Pine source.
2. **Assess** — score each fetched script for mechanical convertibility.
3. **Convert** — transpile the best candidates into Strategy API V2 source and
   refuse anything that does not compile.
4. **Backtest and publish** — run the converted strategy through the normal
   backtest service, then publish the result to the marketplace.

Nothing is published without a successful backtest, and no stage trusts the
previous one: `convert` re-compiles the generated code and `backtest` uses the
same `StrategyV2BacktestService` as the UI.

## Data model

Three tables back the pipeline. They are created by
`migrations/20261107_tradingview_scripts.sql` (registered in
`app/utils/db.py` as `tradingview-scripts-20261107`).

| Table | Purpose |
| --- | --- |
| `qd_tv_scripts` | Discovery corpus: listing metadata, cached Pine source, assessment score. |
| `qd_tv_harvest_runs` | One row per harvest attempt with per-stage counters and status. |
| `qd_tv_conversions` | Per-script conversion lifecycle, from staged to published. |

`qd_tv_scripts.script_id_part` is unique. Re-harvesting updates metadata and
never regresses engagement counters (`GREATEST(...)` on update), because listing
pages sometimes serve a slightly stale snapshot.

Pine source is only fetched for scripts whose `script_access` resolves to
`open`. TradingView serves invite-only code behind a login, and the trade-off
here is to skip it rather than add session handling.

## Endpoints

All routes live under `/api/tradingview` and require authentication; mutating
routes additionally require the `admin` role.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/status` | Pipeline and harvest counters. |
| `GET` | `/scripts` | Paginated candidates ordered by score. |
| `GET` | `/scripts/<id>` | One candidate including its Pine source. |
| `POST` | `/harvest` | Run one discovery + fetch round. |
| `POST` | `/assess` | Re-score unassessed scripts. |
| `POST` | `/convert` | Transpile and compile-gate candidates. |
| `POST` | `/backtest` | Backtest converted sources, optionally publish. |

## Configuration

The pipeline is **disabled by default**. Scheduled tasks return immediately
unless `TRADINGVIEW_HARVEST_ENABLED=true`, so an existing deployment starts
making outbound calls to `tradingview.com` only after an operator opts in.

See `.env.example` for the full variable list. The important ones:

| Variable | Default | Meaning |
| --- | --- | --- |
| `TRADINGVIEW_HARVEST_ENABLED` | `false` | Master switch for all scheduled stages. |
| `TRADINGVIEW_HARVEST_INTERVAL_SEC` | `86400` | Harvest cadence, clamped to a 3600s floor. |
| `TRADINGVIEW_HARVEST_MAX_SOURCES` | `60` | Source fetches per run. |
| `TRADINGVIEW_MIN_SCORE` | `70` | Candidates below this are staged but never converted. |
| `TRADINGVIEW_MIN_TRADES` | `3` | Minimum trades before a strategy may be listed. |
| `TRADINGVIEW_TRANSPILER_VERSION` | `5` | Bump to regenerate sources after transpiler changes. Version 5 translates onto the interval upstream published, so everything generated earlier sits on the wrong frequency grid. |
| `TRADINGVIEW_BACKTEST_TIMEOUT_SEC` | `300` | Per-strategy backtest ceiling; slow scripts become `timeout`. |
| `TRADINGVIEW_BACKTEST_DAYS` | `180` | Backtest lookback. Cost grows with the square of the window, so a longer window is rarely worth the queue time. |
| `TRADINGVIEW_DEFAULT_INSTRUMENT` | `BTC/USDT` | Instrument used for verification backtests. |
| `TRADINGVIEW_DEFAULT_FREQUENCY` | `1h` | Fallback only. Strategies are translated onto the interval TradingView published them on; this applies when upstream publishes no usable interval. |
| `TRADINGVIEW_CONVERT_USER_ID` | `1` | Owner of the generated strategy sources. |

## Operating notes

- **Upstream politeness.** Listing pages are 24 items each and the pipeline
  pauses between requests. Raising the per-run budgets shortens the time to
  fill a corpus but increases the chance of being throttled.
- **Pagination is path-based.** TradingView ignores `?page=`, so page *N* is
  `/scripts/<topic>/page-N/`. This is encoded in `_listing_url` and covered by
  tests; changing it silently truncates the corpus to page one.
- **The assessor degrades safely.** When the optional Pine parser is not
  installed, assessment falls back to a conservative string-counting scorer
  (`assessor: "cheap_fallback"`). The fallback deliberately marks scripts
  without `strategy.entry`/`strategy.order` as weak, so a missing parser yields
  fewer conversions rather than wrong ones.
- **A backtest is only evidence if it traded.** `StrategyV2BacktestService`
  reports success for a strategy whose entry condition never fired, with
  `total_trades = 0`. Such a run is parked as `no_trades` rather than published,
  because nothing about the translated strategy was exercised. The same is true
  of conversions whose translation raises at runtime: they are parked as
  `backtest_failed` so one broken script cannot consume a slot on every batch.
  Both states keep the row out of later batches; set it back to `ready` to retry
  after fixing the transpiler.
- **A finished backtest is not an endorsement.** Publishing is the marketplace's
  only quality signal — there is no editorial layer behind it — so the publish
  step applies floors to the measured result: `TRADINGVIEW_MIN_RETURN_PCT`
  (default `0`), `TRADINGVIEW_MIN_SHARPE` (default `0`) and
  `TRADINGVIEW_MAX_DRAWDOWN_PCT` (default `60`, on the magnitude of the
  engine's negative `maxDrawdown`). A strategy that misses any of them is
  parked as `below_threshold` with `diagnostics.metrics` and
  `diagnostics.failed` recorded, which keeps the decision auditable and lets a
  later threshold change be re-evaluated from stored numbers instead of a fresh
  backtest. These are floors, not quality: they reject "lost money" and "blew
  up", and say nothing about how good the survivors are. Raising them is the
  intended way to tighten the market; lowering them re-admits rejects on the
  next publish pass, since `below_threshold` rows stay out of the queue until
  their status is reset to `ready`.
- **The interval comes from upstream, not from the code.** TradingView reports
  the chart interval the author published each script on (`symbol.interval`, for
  example `60` or `1D`). That is the only evidence about the timeframe a
  strategy was designed for, so conversion translates onto it: a 15-minute
  scalper is measured on 15-minute bars instead of inheriting a global hourly
  default. Tokens outside the subscription grid snap to the nearest grid point
  (an 8-hour strategy becomes 4-hour, which is 2x off; inheriting the hourly
  default would be 8x off). Tokens with no duration at all — a monthly `1M`, or
  a value the feed does not normally emit — fall back to
  `TRADINGVIEW_DEFAULT_FREQUENCY`. Because the grid now varies per strategy, the
  backtest window does too: a 1m strategy can only be measured over weeks while
  a 1h strategy gets months, so **returns across listings are not directly
  comparable** and the marketplace score inherits that caveat.
- **Licensing.** TradingView code is readable by anyone but not freely
  redistributable. Generated strategies are derived works; keep the original
  script URL (stored in `chart_url`) with the listing so attribution is
  possible.
- **Pine parsing.** `pynescript` (LGPL-3.0-or-later) is pinned in
  `requirements.lock`, so the importer has a real parser in production. The
  import stays lazy and guarded anyway: a deployment that strips the package
  still runs, it only falls back to the conservative text scorer. The parser
  takes roughly a second per script, so it belongs on the conversion path and
  never in a request-time validation path.
- **The sample corpus is not vendored.** `backend_api_python/tests/test_pine_transpiler_corpus.py`
  reads real TradingView sources from `PINE_TRANSPILER_CORPUS` (for example the
  `tv_import/corpus/` directory that `harvest` populates) and skips when the
  directory is absent. TradingView scripts stay publicly readable but are not
  freely redistributable, so they are treated as a cache rather than a source
  file of this repository.

## Related

- [Strategy API V2 development](STRATEGY_DEV_GUIDE.md)
- [Backtest Center](../product/BACKTEST_CENTER.md)
- [Indicator development guide](INDICATOR_DEV_GUIDE.md) — why Pine is not
  executed for indicators
