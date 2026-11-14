# Real-time chart and Pine indicator plan

Status: agreed with the operator on 2026-11-14, implementation started.
Owner: backend chart/data path (`backend_api_python/`) and the Vue frontend fork.

This document exists because the previous months of work went into the
strategy funnel (harvest → transpile → backtest → publish) and left the two
things the operator actually wanted unfinished:

- **the chart does not update** — the TradingView datafeed we wrote has no
  source of live bars, and
- **Pine indicators are not on the chart** — the transpiler turns Pine scripts
  into *strategies*, and the chart only loads `asset_type='indicator'` assets,
  of which there are three built-in examples.

Everything below is scoped to fixing those two, and to the three defects found
while investigating why the chart looked dead.

## What was decided, and why

The operator's requirement is explicit: **real-time means a stream, not
polling.** "中转聚合都行但是不要轮询" (2026-11-14). So the transport is a
WebSocket for crypto. Non-crypto markets keep their existing behaviour and are
out of scope for this plan.

Rejected: **server-sent events from the Flask API**, which was my first
recommendation and was wrong. The deployment runs `GUNICORN_WORKERS=1` with
`GUNICORN_THREADS=8` and the `gthread` worker holds one thread per connection
for the request's whole lifetime, so eight open SSE connections would starve
every other API call, and `max_requests=800` would cut live streams on worker
recycle. SSE would have needed a worker-model change, a new kline stream, and
nginx configuration before it delivered a single frame.

Also rejected: **REST polling at 5–30 second intervals**, which is what
`KlineChart.vue` does today. It works, it is well tuned per timeframe, and it
is exactly what the operator does not want.

## Defects that must be fixed first

These are not features; they are bugs that make the chart wrong or dead.

1. **Bar time is sent in seconds, the library requires milliseconds.**
   `charting_library.d.ts:1823` states `Amount of **milliseconds** since Unix
   epoch start`. `src/charting/datafeed.js:116` returns seconds, so
   `1793808000` is read as 1970-01-21 instead of 2026-11-04. The time axis of
   every chart is wrong today.

2. **`getBars` sends `before_time`, which the endpoint ignores.**
   `src/charting/datafeed.js:219` passes it; `app/routes/market_data.py:138`
   only reads `start`, `end`, `limit` and `fetch`. Scrolling left therefore
   re-fetches the newest bars, the client-side filter drops them, and the chart
   locks its left edge (or declares end-of-data after repeated empty answers).

3. **`pushBar` discards every bar that has not closed.**
   `src/charting/datafeed.js:256` returns early when
   `Date.now() < (bar.time + seconds) * 1000`. The comment claims the library
   treats every callback as finished; `charting_library.d.ts:12750` says the
   opposite — "call the callback every time you want to update the most recent
   bar". As written, a live feed would be silently swallowed.

## Scope

### In scope

- Crypto real-time K-lines over a WebSocket, from the exchange to the chart.
- Pine indicators rendered as chart studies, loaded from the indicator list.
- The three defects above, with regression tests.
- An operator-facing page that exercises both.

### Out of scope

- Non-crypto real-time (US equities, forex, futures keep the REST path).
- Replacing `KlineChart.vue` (7152 lines, still serves the backtest result page).
- The strategy funnel. It keeps running; it is not extended here.
- Writing a TradingView study runtime. We replay pre-computed series (see
  "Study replay" below) and do not execute Pine in the browser.

## Design

### Crypto real-time transport

The browser connects **directly to the exchange's public WebSocket**, reusing
`src/utils/exchangeWs.js`, which already speaks binance, okx, bitget, bybit,
gate and htx, per-venue, with reconnect, backoff, a watchdog for silent
sockets, and client-side bar closure detection (`closedByTime`). It is the same
client `KlineChart.vue` has been using in production.

Direct connection, not a backend relay, because:

- the exchange endpoints are public and unauthenticated; a relay adds a hop
  and a process to babysit without adding data;
- the existing client already handles venue quirks (instrument ids, timeframe
  spellings, subscription frames) which would otherwise have to be re-encoded
  server-side;
- the backend's own `PublicMarketPriceFeed` is a **tick-only** singleton that
  lives in the trading worker process, is not reachable from the API process,
  and carries no OHLCV at all — using it would mean building a second stream
  anyway.

The consequence to accept: if the exchange is unreachable from the browser but
reachable from the server, the chart shows history and no ticks. The history
path still works, so the failure is degraded rather than blank.

### How bars reach the library

`createDatafeed` gains a `mount` step that owns one `ExchangeKlineWs` for the
current symbol/timeframe and forwards its ticks into `pushBar`. The bar object
handed to the library must carry **milliseconds** and the **bar's open time**,
never `Date.now()`: the library's `_putToCacheNewBar` replaces the last bar
only on an exact time match and logs a time-violation error otherwise, so a
per-tick timestamp would grow a new candle on every tick.

Ticks for the forming bar are forwarded continuously (the library updates the
last candle), and the closed bar is forwarded once with the same open time, so
the final tick of a period lands as a normal update. Indicators are re-seeded
on period close, not per tick.

### Study replay

The Charting Library does not accept a series of indicator values directly; a
custom study's `main()` is called once per bar and must **return** the values
for that bar. Our studies therefore replay a pre-computed array:

- `loadSeries` fetches the whole window from `POST /api/indicator/chart-preview`
  once, together with `candleTimes`;
- `main()` indexes that array **by bar time** (built into a map), not by call
  order, so a partially-filled window yields `NaN` at the tail instead of the
  line shifting one bar left — the failure mode of the call-order version
  already in `indicatorPlots.js`;
- the series cache key includes the indicator id, market, symbol, timeframe
  **and the window start/timeframe epoch**, so a new session cannot reuse a
  stale snapshot.

Real-time indicator values are not re-computed per tick. Recomputing would
mean one preview request per tick; instead the study's tail is left empty and
refilled when the bar closes. This is a deliberate trade: the price line moves
at tick rate, the indicator line advances at bar rate, which is also what a
TradingView-hosted Pine study does when its inputs reference `close`.

### Pine indicator conversion

The existing transpiler emits *strategy* code (`initialize`/`handle_data`, one
bar at a time). The chart wants *indicator* code (a vectorised script that
returns `plots`/`signals`). The two are not the same program, and the
transpiler cannot simply be pointed at a different template.

The conversion path reuses, in order of value:

- the Pine parser and AST helpers (`pine_ast.py`),
- the expression translator and the `ta.*` / `math.*` / `str.*` mappings
  (`emitter.py`, `mapping.py`) — these cover 80% of real scripts,
- the helper library (`helpers.py`), which is written against pandas Series
  and therefore already vectorised for the parts that matter.

What it cannot reuse is the per-bar state machine: `var` in a Pine indicator is
still a per-bar variable, and the vectorised emitter has to model it explicitly
(the same way `_p_scalar_hist` does for strategies). Scripts whose state cannot
be expressed as a column-wise operation are blocked, not silently mangled.

Corpus reality (measured 2026-11-14):

| filter | count |
|---|---|
| indicator scripts with source | 186 |
| …that call `plot`/`plotshape` | 124 |
| …and do not use `polyline` | 124 |
| …using `var` | 175 |
| …using `for` | 177 |
| …using `table` | 95 |
| …using `request.security` | 62 |
| …that reference `strategy.*` at all | 2 |

So the addressable first batch is the 124 that draw something and can be
represented with the existing plot vocabulary. `table` is already dropped by
the strategy emitter and dropping it here too is fine: a table is an
annotation, not a series.

## Steps

1. [x] Fix the three datafeed defects and pin them with tests. No new features
   in this step; it exists so the chart's time axis is correct before anything
   is built on top.
2. [x] Mount a crypto WebSocket into the datafeed and drive `pushBar` from it,
   with ms timestamps and open-time bar keys. Ship the real-time chart, verify
   with a live symbol.
3. [x] Write the indicator emitter: Pine indicator source in, `plots`/`signals`
   out, validated by running the output through the sandbox and asserting a
   non-empty plot. Publish converted indicators as free `asset_type='indicator'`
   assets, following the strategy funnel's publish gate.
4. [x] Point the chart's indicator picker at those assets and replay them by
   time. Verify on the ten highest-voted indicators first.
5. [x] Batch-convert the remaining corpus, publish what validates, and record
   the blockers for what does not.

## What the batch conversion actually found

Run on 2026-11-15 against the 124 addressable scripts:

| outcome | count | meaning |
|---|---|---|
| published | 66 | runs in the chart sandbox and draws at least 10 points |
| blank | 28 | runs, but every plot is `None` on the sample frame |
| transpile_failed | 22 | no chart output, or the parser refused the source |
| sandbox_failed | 4 | crashed or exceeded the 20 s chart budget |
| ready | 6 | converted and drawn, waiting on a later publishing pass |

The gap between the earlier "102 transpiled" measurement and the 66 that
publish is the whole point of this gate. Translation success was never the
question: the first implementation compiled every script and produced empty
plots, because the per-bar statements had been nested one level too deep and
never executed. A module that runs but draws nothing is worth nothing to a
reader, so it is parked as `blank` rather than shipped as an empty pane.

The 28 blanks are mostly scripts whose first visible output is conditional on
an input that defaults to off (`plotBands = input.bool(false)`), or whose
plotted value is itself NaN on a synthetic frame. They are not defects in the
conversion; they are scripts that draw nothing until a reader turns something
on. Exposing those inputs is the open question below.

## Acceptance

- The crypto chart updates without a page reload, at the exchange's tick rate,
  with correct timestamps (no 1970 axis, no duplicate candles per tick).
- Scrolling left loads older bars until the data source runs out, and the
  chart says so rather than locking.
- At least ten converted Pine indicators render visibly on the chart.
- No polling timer exists on the crypto chart path.

## Open questions

- **Indicator parameters.** `chart-preview` accepts `params`; the converted
  indicators declare `# @param` lines. Whether the workbench exposes them as
  editable inputs in step 4 or ships fixed defaults first is undecided.
- **Which symbols get a live stream.** All crypto pairs the venue lists, or a
  curated watchlist? Streaming every pair the user scrolls through costs one
  socket per chart, which is fine, but a symbol the venue does not carry fails
  at subscribe time with no history fallback.
