# Chart-first Indicator IDE

Status: decided 2026-11-18. Planning map: [youluo1230/QuantDinger#1](https://github.com/youluo1230/QuantDinger/issues/1).
Migration order: [#8](https://github.com/youluo1230/QuantDinger/issues/8).

This document records what the indicator IDE is becoming, which layer owns which
control, and the contracts the frontend and backend have to meet. It replaces the
implicit "toolbar plus ad-hoc chart calls" design in
`QuantDinger-Vue/src/views/indicator-ide/index.vue`.

## Problem

The chart surface already runs TradingView Charting Library v31 in the browser,
with Pine studies executed by PineTS. On top of it the IDE still draws a second
control surface: its own symbol/watchlist picker, exchange and market-type
selectors, chart-type menu, timeframe radios, indicator multi-select, parameter
drawer, and fullscreen button. Several of these are now inert or conflict with
the library:

- chart type: the toolbar writes `candleType`, but the widget never reads it
  (no `chartStyle` is passed), so the menu does nothing.
- timeframe: the toolbar drives a widget remount; changing the interval in the
  native header does not flow back to the pane state, and live bars are gated on
  the toolbar's value.
- indicators: studies are added through `custom_indicators_getter` and then also
  through the native dialog, which produces unmanaged duplicates; any set change
  remounts the whole widget instead of adding or removing one study.
- persistence: five separate `localStorage` keys, one per control, no server-side
  layout.
- symbol search: the native search is disabled even though no `searchSymbols`
  implementation exists, so it cannot be enabled until the datafeed implements it.

## Target surface

```
+--------------------------------------------------------------+
| TV native header: symbol . 1m 5m 15m 1h 4h 1d | candles . |  |
| indicators . compare . undo redo screenshot fullscreen . save |
+--------------------------------------------------------------+
| QD actions: save script . signal alerts . convert to strategy  |
|             . editor toggle                                    |
+--------------------------------------------------------------+
| layout 1 2 4 | active pane P1 BTC/USDT 1h                      |
+----------------------------+----------------+----------------+
| chart pane P1              | chart pane P2  |  rail 48px      |
| (own native header)        | (own header)   |  indicator lib  |
+----------------------------+----------------+  studies list   |
| chart pane P3              | chart pane P4  |  editor         |
+----------------------------+----------------+  layouts        |
| Pine editor drawer (A): bound to the pane it was opened for   |
+--------------------------------------------------------------+
```

Each pane is an independent `ChartingLibraryWidget` and therefore keeps its own
native header. The IDE does not wrap them in a synthetic global header; that was
prototyped, rejected, and is recorded on issue #2.

## Ownership

| Concern | Owner | Notes |
|---|---|---|
| Symbol search, interval, chart type, fullscreen | TV native | Toolbar controls removed |
| Indicator add/remove | TV native dialog plus QD rail list (read-only) | `custom_indicators_getter` only supplies metainfo |
| Indicator inputs | TV native settings dialog | Inputs come from Pine `input.*`; values flow back to `resolveCustomIndicators` |
| Save/load layout | QD workspace archive | Native Save/Load stays hidden; no `save_load_adapter` |
| Save Pine script | QD editor -> `POST /api/indicator/saveIndicator` | Server still validates with pynescript |
| Signal alerts | QD action bar bell -> existing modal | Backend worker unchanged |
| Convert to strategy | QD action bar | Existing sessionStorage handoff |
| Multi-pane layout | QD | 1/2/4 widgets; toolbar and rail act on the active pane only |

## Contracts

### Symbol ticker

`QD:<market>:<marketType>:<exchangeId>:<instrumentId>`, for example
`QD:crypto:swap:binance:BTC/USDT`. The encoding is produced by `searchSymbols`
and `resolveSymbol`, and parsed by `decodeTicker`. Panes store the encoded
ticker. `resolveSymbol` falls back to a bare symbol plus the pane's current
venue when decoding fails, so callers that still pass `BTC/USDT` keep working.

### Datafeed

`src/charting/datafeed.js` must implement:

- `onReady`: capability fields only; `exchanges` and `symbols_types` are empty.
- `searchSymbols(query, userInput, exchange, symbolType, onResult)`: calls
  `GET /api/market/symbols/search`, returns `{symbol, full_name, description,
  exchange, ticker, type}` where `ticker` is the encoded form.
- `resolveSymbol`: accepts encoded ticker and bare symbol; returns
  `{name, ticker, description, type, session, timezone, minmov, pricescale,
  has_intraday, has_daily, has_weekly_and_monthly, supported_resolutions}`.
- `subscribeBars`: reports symbol/interval changes back to the pane state.

### Studies

- `metainfo.inputs` is generated from the parsed Pine inputs. Type mapping:
  `int -> integer`, `float -> float`, `bool -> bool`, `color -> color`,
  `source -> source`, `symbol -> symbol`, `timeframe -> resolution`,
  `session -> session`, `string -> text`, multi-line string -> `text_area`.
  Input ids are stable (`pineInputId`).
- Studies are added and removed in place (`chart.createStudy`,
  `chart.removeEntity`); the widget is not remounted for a study change.
- `study_properties_changed` reads `getStudyById(id).getInputValues()` and feeds
  them back into the Pine execution, debounced, dropping stale responses.

### Workspace archive

`GET/POST/DELETE /api/users/chart-templates` keeps its route and storage
(`qd_users.chart_templates`), but the payload becomes a whole-chart snapshot:

```json
{
  "version": 1,
  "name": "BTC 4h + ETH 1h",
  "layoutMode": 2,
  "activePane": 0,
  "editorOpen": false,
  "panes": [
    {
      "symbol": "QD:crypto:swap:binance:BTC/USDT",
      "timeframe": "4H",
      "chartType": "candles",
      "indicators": [
        {"id": 12, "name": "EMA cross", "params": {"fast": 12}, "visible": true}
      ]
    }
  ]
}
```

Old rows (`{name, indicators:[...]}`) are converted on read into a single-pane
document; rows that cannot be converted are listed as read-only legacy entries.
No upgrade-time rewrite of stored rows.

## Migration

Four phases, each independently mergeable and revertible; details and gates on
issue #8.

1. **P0 wiring** — datafeed search + ticker codec, study inputs and in-place
   add/remove, `chartStyle` passthrough. No visible change.
2. **P1 native surface** — delete the duplicated toolbar controls, enable the
   native symbol search, keep the QD action bar and multi-pane.
3. **P2 editor and archive** — bottom drawer editor, workspace archive against
   `chart-templates`, drop the five `localStorage` keys, delete `/tv-workbench`.
4. **P3 cleanup** — stale comments, dead canvas-era code, docs.

## Verification

- Frontend unit: ticker round-trip, datafeed search/resolve, inputs metainfo,
  archive shape conversion, active-pane binding.
- Manual smoke per phase: 1/2/4 panes; change symbol from the native header;
  add and remove a QD study; edit an input and see the plot change; save a
  layout and reload it; open the alert modal and confirm symbol, timeframe and
  indicator default to the active pane; delete a pane's study and confirm no
  widget remount.
- Backend: chart-template shape test (legacy row -> new document -> read back),
  existing indicator validation and signal alert suites unchanged.

## Out of scope

- TradingView native alerts and `alertcondition` instances.
- `save_load_adapter` / `charts_storage_*` (native Save/Load stays hidden).
- Persisting drawings and line tools.
- Storing script revisions inside a layout; a layout references indicator ids,
  and restoring it runs the current script version, not the one that was saved.
- Rewriting the alert worker or the Pine execution runtime.
