# QuantDinger Indicator Development Guide

> Applies to: the QuantDinger chart-indicator contract, written in Pine Script v5/v6
> Audience: first-time indicator authors, developers migrating a Python indicator or a TradingView script, and reviewers of AI-generated code

A QuantDinger chart indicator is a Pine Script study. The Indicator IDE runs the source in the user's browser with PineTS, a Pine runtime written in TypeScript, and the source is stored verbatim in the indicator row (<code>qd_indicator_codes.code</code>). What the chart draws is what the browser executed: no chart-indicator value is computed on the server.

The most important boundary is: **an indicator is for chart analysis, not trade execution.**

An indicator cannot place orders, backtest, run live trading, read an account, manage positions, enable leverage, or execute stop-loss/take-profit rules. To trade an idea, convert its visual signals into a Strategy API V2 strategy, then verify, backtest, and deploy that strategy separately.

Because the source is Pine, the language semantics are Pine's own: <code>ta.*</code> calculations, <code>[1]</code> history references, <code>var</code> persistence, <code>request.security()</code>, and <code>input.*</code> declarations behave the way they do on TradingView for the subset the browser runtime implements. What an indicator does not have here is an account, an order, or a position.

Supported versions are **2 through 6**. Version 5 and 6 scripts run as written; a
script declaring v2, v3 or v4 is translated on the way in, and the panel says so
rather than refusing it. The version directive must be the first line of the file:

~~~pine
//@version=6
~~~

Anything older than version 2 is refused before the script reaches the chart. A script that does not compile is refused at save time: <code>POST /api/indicator/saveIndicator</code> and the Agent Gateway save path answer HTTP 400 with <code>Invalid Pine Script: &lt;reason&gt;</code>. Saves are validated by the server's Pine compiler (<code>backend_api_python/app/services/pine_validation.py</code>, built on the <code>pynescript</code> parser), so a syntax error is reported where you can fix it rather than surfacing inside the chart.

---

## 1. Build a minimal indicator first

Paste this into the Indicator IDE and run it:

~~~pine
//@version=6
indicator("Close Line", overlay=true)

plot(close, color=#3B82F6)
~~~

This is the complete minimal contract:

1. Declare the Pine version on the first line.
2. Declare the study with <code>indicator("Close Line", overlay=true)</code>. The title becomes the saved indicator name, and <code>overlay=true</code> places it on the price chart instead of a separate pane.
3. Draw with <code>plot()</code>. There is no output dictionary and no return value: the drawing calls are the output.

For a new indicator, add one line first, then its <code>input.*</code> settings, then event markers, and only then drawing objects such as labels and boxes.

---

## 2. Indicator, strategy, and conversion boundaries

| Artifact | Owns | Does not own |
| --- | --- | --- |
| Chart Indicator (Pine study) | plots, fills, markers, labels, boxes, alert conditions | backtest, live trading, orders, positions, leverage, execution risk |
| Strategy API V2 | subscriptions, signals, order intents, positions, backtest, live execution, protections | chart rendering |
| Indicator-to-Strategy | translating visual signal meaning into executable code | mixing order behavior into the source indicator |

A marker is a drawing, not a trade. A downward marker titled “Death” can mean a long exit warning or simply weakening conditions; it does not open a short and does not reverse a position.

For a long-only conversion, the usual mapping is:

- explicit bullish entry event → <code>open_long</code>
- explicit bearish exit event → <code>close_long</code>
- generate <code>open_short</code> only when the user explicitly requests shorting and provides a distinct bearish entry rule

Do not create execution columns such as <code>open_long</code>, <code>close_long</code>, <code>open_short</code>, <code>close_short</code>, <code>add_long</code>, or <code>reduce_long</code> in an indicator: those belong to Strategy API V2. There is also no <code># @strategy</code> annotation to write — an indicator has no annotation layer at all.

A <code>strategy(...)</code> declaration is legal in the IDE, and the chart still draws its <code>plot()</code> output, but nothing is executed in the browser: a strategy's entries and exits only happen when the same source is backtested server-side. The save path reports that case as the <code>SCRIPT_IS_A_STRATEGY</code> hint, which is a note rather than an error.

---

## 3. Runtime: where the script runs and what it can read

The IDE executes the script in the browser. The server's role is to compile and validate the source on save, and to run the script separately for the two consumers that cannot use a browser.

| Path | Runs | Purpose |
| --- | --- | --- |
| Chart study | browser, PineTS | everything the chart draws |
| Save/validate | server, Pine compiler | accepts or refuses the source before it is stored |
| Alerts and API consumers | server, transpiled to Python | signal evaluation for notifications and non-browser callers |

The browser runtime reads bar data through Pine's built-in series: <code>open</code>, <code>high</code>, <code>low</code>, <code>close</code>, <code>volume</code>, <code>hl2</code>, <code>hlc3</code>, <code>ohlc4</code>, <code>time</code>, and <code>bar_index</code>. There is no <code>df</code> and no <code>params</code> dictionary: a series is a value indexed by bar, and a setting is the variable an <code>input.*</code> call returns.

Available namespaces: <code>ta</code>, <code>math</code>, <code>str</code>, <code>color</code>, <code>array</code>, <code>matrix</code>, <code>map</code>, <code>request</code>, <code>input</code>, <code>strategy</code>, <code>syminfo</code>, <code>timeframe</code>, and <code>chart</code>. One imported library is vendored, <code>TradingView/ta</code>, at any version; its <code>requestVolumeDelta</code> splits a bar into buy and sell volume. Do not import a library that is not vendored with the runtime: an unresolved import fails the whole script.

Two execution environments read the same source, so two rules follow from that:

- The chart is a browser replay over the bars the chart already holds. There is no order, account, or portfolio state to read.
- The server-side path used by alerts transpiles the Pine source to Python first, then evaluates it. That is why an alert evaluates the conditions the chart draws. The two paths are not byte-identical, though: the server replay collapses bar state, so a condition that depends on a live bar must be gated on <code>barstate.isconfirmed</code> for the alert to match what the chart shows.

History, <code>var</code>, and <code>na</code> follow Pine semantics. A series read from the future does not exist; <code>close[1]</code> is the previous bar, and reading past the first available bar yields <code>na</code>.

---

## 4. Script structure and metadata

A complete study has one shape:

~~~pine
//@version=6
// Dual EMA Viewer — chart-only study, no order logic.

indicator("Dual EMA Viewer", overlay=true)

// 1. Settings
// 2. Calculation
// 3. Drawing
// 4. Alerts
~~~

Rules that the compiler and the save path enforce:

- <code>//@version=6</code> is the first line.
- Exactly one <code>indicator(...)</code> or <code>strategy(...)</code> declaration, on one line, at the start of a line near the top.
- The declaration's first string argument is the study title. That title is the name the library listing shows; there is no separate name block.
- Every knob the user may tune is declared with <code>input.*</code>, not left as a literal.

There is no separate metadata block. The retired Python contract used a bare assignment (<code>my_indicator_name = "..."</code>) for the same job; a migrated script replaces it with the declaration title, which is also what the save path reads first.

Project source rules require English identifiers, comments, input titles, and default display labels. Localize display text only when a user explicitly requests a target language.

---

## 5. Settings: declaring input.* fields

The IDE derives the study's input metadata from the script's <code>input.*</code> declarations and hands it to the charting library, which renders the fields in its own **Settings** dialog (the gear icon on the study's legend). There is nothing to declare separately and nothing to register: an input that exists in the source becomes a field, and its title, group, tooltip, and bounds come from the arguments you wrote. A value changed there re-runs the script and redraws the study immediately; the value belongs to the chart workspace, not to a separate parameter form.

~~~pine
//@version=6
indicator("EMA Channel", overlay=true)

fastLen = input.int(12, "Fast EMA length", minval=1, maxval=200, step=1, group="Parameters", tooltip="Bars used by the fast EMA. Lower values react sooner and whipsaw more.")
slowLen = input.int(26, "Slow EMA length", minval=2, maxval=400, step=2, group="Parameters", tooltip="Bars used by the slow EMA. Must stay above the fast length.")
bandPct = input.float(1.5, "Channel width (%)", minval=0.1, maxval=10.0, step=0.1, group="Parameters", tooltip="Distance of the channel from the fast EMA, in percent.")
showMarks = input.bool(true, "Show crossover markers", group="Visuals")
lineColor = input.color(#3B82F6, "Line color", group="Visuals")
src = input.source(close, "Price source", group="Visuals")
htf = input.timeframe("1D", "Higher timeframe", group="Visuals")
~~~

What each argument becomes in the panel:

| Argument | Panel effect |
| --- | --- |
| the second argument, <code>title</code> | the field label, and the key the saved parameter value is stored under |
| <code>group</code> | the section the field is placed in |
| <code>tooltip</code> | the help text shown beside the field |
| <code>minval</code> / <code>maxval</code> | the accepted minimum and maximum of a numeric field |
| <code>options=[...]</code> | a dropdown instead of a free-text field |
| the first argument, <code>defval</code> | the value the panel starts at |

Input constructors that become form fields: <code>input.int</code>, <code>input.float</code>, <code>input.bool</code>, <code>input.string</code>, <code>input.color</code>, <code>input.source</code>, <code>input.timeframe</code>, <code>input.symbol</code>, <code>input.session</code>, <code>input.price</code>, <code>input.time</code>, <code>input.text_area</code>, and <code>input.enum</code>.

Rules:

- Use the input declaration's title as the label. A field with no title is not shown.
- A default must be a literal or a constant assigned a literal. The panel reads it out of the source before the script runs, so a computed default (<code>input.int(timeframe.period)</code>) has no value to display and the field reports none.
- Do not invent a second name for a setting in the code. The variable the declaration is assigned to is the value the script uses: <code>fastLen</code> above is the length, not a lookup key.
- A length or index argument must be an <code>int</code>: <code>ta.ema(close, 12)</code> is fine, <code>ta.ema(close, 12.5)</code> is refused by the compiler.
- Parameters control calculations and display only. Do not declare account, position, leverage, stop-loss, or take-profit settings in an indicator; those belong to the strategy.

Dropdown and constrained-value example:

~~~pine
mode = input.string("Trend", "Mode", options=["Trend", "Range", "Off"], group="Parameters", tooltip="Which branch of the study to draw.")
~~~

---

## 6. Plots, candles, fills, and colors

<code>plot()</code> draws a series on the chart. Its arguments are the value and, optionally, the legend title and style:

~~~pine
//@version=6
indicator("EMA Overlay", overlay=true)

emaFast = ta.ema(close, 12)
emaSlow = ta.ema(close, 26)

plot(emaFast, "EMA Fast", color=#22C55E, linewidth=2)
plot(emaSlow, "EMA Slow", color=#3B82F6, linewidth=2)
~~~

- <code>title</code> is what the legend shows. Give a title to anything the reader is meant to identify; leave helper series untitled or hide them with <code>display=display.none</code>.
- <code>style</code> selects the form: <code>plot.style_line</code>, <code>plot.style_linebr</code> (a gap where the value is <code>na</code>), <code>plot.style_stepline</code>, <code>plot.style_columns</code>, <code>plot.style_histogram</code>, <code>plot.style_circles</code>, <code>plot.style_cross</code>, <code>plot.style_area</code>, or <code>plot.style_areabr</code>.
- <code>color</code> takes a hex literal or <code>color.*</code>. A per-bar color is a series expression: <code>color = close >= open ? #22C55E : #EF4444</code>.
- A gap is <code>na</code>. Write <code>plot(condition ? value : na)</code>; never plot a sentinel such as <code>0</code> for a missing point, because the line then connects through zero and misrepresents the price.
- <code>offset</code> shifts the drawing by whole bars. Use it when the drawing is deliberately displaced; do not fold the shift into the value.

Candles and bars have their own call:

~~~pine
plotcandle(open, high, low, close, "Custom candles", color=close >= open ? #22C55E : #EF4444, wickcolor=#9CA3AF, bordercolor=#374151)
~~~

<code>plotbar()</code> draws OHLC bars from the same four values.

Levels and shaded regions:

~~~pine
hline(70, "Overbought", color=color.new(#EF4444, 60))
hline(30, "Oversold", color=color.new(#22C55E, 60))

upper = plot(emaFast * 1.005, "Channel upper", color=#22C55E, display=display.none)
lower = plot(emaFast * 0.995, "Channel lower", color=#22C55E, display=display.none)
fill(upper, lower, color=color.new(#22C55E, 85), title="Channel")
~~~

- <code>hline()</code> takes a constant price. It is a level, not a series.
- <code>fill()</code> takes the two <code>plot()</code> handles it should shade. Never hardcode a value where a handle belongs.
- <code>color.new(color, transparency)</code> builds a translucent color; 0 is opaque and 100 is invisible. Prefer a transparency above 60 for a band behind price.
- <code>bgcolor()</code> and <code>barcolor()</code> color the whole background or the candles, one color per bar. Use <code>na</code> to leave a bar untouched.

~~~pine
bgcolor(emaFast > emaSlow ? color.new(#22C55E, 92) : color.new(#EF4444, 92), title="Regime")
barcolor(close >= open ? #22C55E : #EF4444, title="Candle direction")
~~~

### Filled bands and clouds

A cloud is two <code>plot()</code> handles and one <code>fill()</code>. Ordering happens at draw time, so the band stays valid when the two lines cross:

~~~pine
//@version=6
indicator("Trend envelope", overlay=true)

fastLen = input.int(12, "Fast EMA length", minval=1, maxval=200, group="Parameters")
slowLen = input.int(26, "Slow EMA length", minval=2, maxval=400, group="Parameters")
cloudOpacity = input.int(85, "Cloud transparency", minval=60, maxval=98, group="Visuals")

emaFast = ta.ema(close, fastLen)
emaSlow = ta.ema(close, slowLen)

upper = plot(math.max(emaFast, emaSlow), "Upper edge", color=#22C55E)
lower = plot(math.min(emaFast, emaSlow), "Lower edge", color=#F43F5E)
fill(upper, lower, color=emaFast >= emaSlow ? color.new(#22C55E, cloudOpacity) : color.new(#F43F5E, cloudOpacity), title="Trend cloud")
~~~

Moving averages, Bollinger bands, and price channels normally overlay the price chart (<code>overlay=true</code>). RSI, MACD, and other oscillators normally use a separate pane (<code>overlay=false</code>); all plots from one study share that pane, so they do not split into several panes the way separate TradingView scripts would.

---

## 7. Markers: one-bar events

A marker belongs on the bar where something changed, not on every bar where a condition happens to be true. Pine gives the change event directly:

~~~pine
//@version=6
indicator("EMA Cross Markers", overlay=true)

fastLen = input.int(12, "Fast EMA length", minval=1, maxval=200, group="Parameters")
slowLen = input.int(26, "Slow EMA length", minval=2, maxval=400, group="Parameters")
showMarks = input.bool(true, "Show crossover markers", group="Visuals")

emaFast = ta.ema(close, fastLen)
emaSlow = ta.ema(close, slowLen)

crossUp = ta.crossover(emaFast, emaSlow)
crossDown = ta.crossunder(emaFast, emaSlow)

plotshape(showMarks and crossUp, "Golden cross", shape.triangleup, location.belowbar, #22C55E, size=size.small)
plotshape(showMarks and crossDown, "Death cross", shape.triangledown, location.abovebar, #EF4444, size=size.small)
~~~

Why the distinction matters:

- <code>ta.crossover(a, b)</code> is true on the single bar where <code>a</code> moved above <code>b</code>; <code>ta.crossunder</code> is the mirror. That is the event an author means by “a cross”.
- A continuous boolean such as <code>emaFast &gt; emaSlow</code> is true on every bar of the trend. Used as a marker condition it paints a shape on every one of those bars, and an <code>alertcondition</code> built on it fires on every bar for as long as the trend lasts. That is the most common authoring mistake in published indicators.
- A boolean with no condition, such as <code>plotshape(true, ...)</code>, marks every bar and is almost never what was meant.

Marker calls:

| Call | Use |
| --- | --- |
| <code>plotshape(cond, title, shape.triangleup, location.belowbar, color)</code> | a built-in shape at a fixed side of the bar |
| <code>plotchar(cond, title, "▲", location.belowbar, color)</code> | a single character instead of a shape |
| <code>plotarrow(up, "Delta")</code> | an up/down arrow sized by magnitude |

Marker arguments worth setting: <code>title</code> (the name the marker and its alert are listed under), <code>location.belowbar</code> / <code>location.abovebar</code>, <code>size</code>, <code>text</code> for a short label on the shape, and <code>color</code> per bar.

Markers remain drawings. A green triangle is not a long position; it is the bar where a long entry condition was true.

---

## 8. Alerts and other timeframes

<code>alertcondition()</code> declares a condition the user can attach an alert to. It places no order and sends nothing by itself:

~~~pine
alertcondition(ta.crossover(emaFast, emaSlow), "Golden cross", "EMA fast crossed above slow")
alertcondition(ta.crossunder(emaFast, emaSlow), "Death cross", "EMA fast crossed below slow")
~~~

- The first argument is the condition, the second is the alert's title in the UI, the third is the message text.
- Declare alerts on event conditions. A state condition notifies on every bar of the state.
- The chart draws the marker and the alert panel lists the condition. Configuration (channel, delivery) is the user's choice after saving; the script cannot send a notification itself.
- Signal alerts are evaluated server-side: the stored Pine source is transpiled to Python and replayed over the bars for the alert's symbol and timeframe. A marker on the last completed bar is delivered on the following bar, so an alert does not drift onto a still-forming candle.

Other symbols and timeframes come from <code>request.security()</code>. It is the only way to read data the chart is not already showing:

~~~pine
htfClose = request.security(syminfo.tickerid, "1D", close[1], lookahead=barmerge.lookahead_off)
~~~

- The second argument is the timeframe, the third the expression to evaluate there.
- Reading <code>close[1]</code> on the higher timeframe reads the last value that is already closed. Reading <code>close</code> there reads a bar that is still moving, so the value changes until that higher-timeframe bar closes.
- <code>lookahead=barmerge.lookahead_off</code> is the honest default; <code>lookahead_on</code> shows a value before it exists and is the usual cause of a backtest that cannot be reproduced live.
- A higher-timeframe read does not switch the chart's timeframe. The study still runs on the chart's bars.

### Waiting for a closed bar

Repainting is the author's responsibility, and the tool for it is <code>barstate.isconfirmed</code>:

~~~pine
confirmedUp = ta.crossover(emaFast, emaSlow) and barstate.isconfirmed

plotshape(confirmedUp, "Confirmed cross", shape.triangleup, location.belowbar, #22C55E)
alertcondition(confirmedUp, "Confirmed cross", "EMA fast crossed above slow on a closed bar")
~~~

While the rightmost bar is still forming, <code>barstate.isconfirmed</code> is false, so the marker and the alert condition wait for the bar to close. Any condition that reads a live value — the current close, a higher-timeframe read, a volume figure — must be gated this way if it drives an alert or a claim about the past.

---

## 9. Lines, labels, and boxes

Most studies need only plots and markers. Reach for Pine's drawing objects when the annotation is an object rather than a per-bar series: a support line, a demand zone, a one-off label on a structural event.

~~~pine
// A label on each confirmed crossover, deleted when the next one appears.
var label lastMark = na

if confirmedUp
    if not na(lastMark)
        label.delete(lastMark)
    lastMark := label.new(bar_index, low, "Long", style=label.style_label_up, color=#22C55E, textcolor=#FFFFFF)
~~~

~~~pine
// A horizontal level held for the last 100 bars.
var line support = na

if bar_index == last_bar_index - 100
    support := line.new(bar_index, low, last_bar_index, low, color=#F59E0B, width=1, style=line.style_dashed)
~~~

- <code>var</code> keeps one handle across bars instead of creating a new object on every bar. Without it the study makes a new drawing each bar until the object limit is reached and the oldest is dropped.
- Delete what is superseded: <code>label.delete()</code>, <code>line.delete()</code>, <code>box.delete()</code>. A stale marker left on the chart reads as a live level.
- The number of objects a script may keep is capped by the declaration: <code>indicator(..., max_labels_count=500, max_lines_count=500, max_boxes_count=500)</code>.
- <code>box.new()</code> draws a zone: top, bottom, left and right edges. <code>bgcolor()</code> is not a substitute for a zone that starts and ends on specific bars.
- Arrays of handles are the way to manage a set: <code>array.new_line()</code> plus <code>array.push()</code> and <code>line.delete()</code> when the set has to be pruned.

Drawing objects are still drawings. A box labelled “Support” is not an order, a stop, or a position.

---

## 10. Migrating a Python indicator to Pine

The Python indicator contract is retired. A stored indicator is Pine source; a row that still holds Python is refused at save time, because the source no longer compiles. The rewrite is mechanical, and this table is the whole of it:

| Python indicator (retired) | Pine study (current) |
| --- | --- |
| <code>my_indicator_name = "..."</code> | the title in <code>indicator("...")</code> |
| <code>my_indicator_description = "..."</code> | still read as the row's description if present; Pine has no description syntax, so otherwise it comes from the save payload |
| <code># @param fast_len int 12 Fast EMA period</code> | <code>fastLen = input.int(12, "Fast EMA period", minval=1)</code> |
| <code>params.get("fast_len", 12)</code> | the variable <code>fastLen</code> the declaration produced |
| the <code>df</code> working copy | the built-in series <code>open/high/low/close/volume</code> |
| pandas or numpy arithmetic | <code>ta.*</code>, <code>math.*</code>, and plain series arithmetic |
| the <code>output</code> dictionary | <code>plot()</code>, <code>plotshape()</code>, and the drawing calls |
| a cloud entry with <code>upper</code>/<code>lower</code> | two <code>plot()</code> handles plus <code>fill()</code> |
| a marker list with <code>None</code> gaps | <code>plotshape(condition, ...)</code> on a one-bar event |
| a zone, line, or label layer object | <code>box.new()</code>, <code>line.new()</code>, <code>label.new()</code> |
| <code>None</code> for a missing value | <code>na</code> |

Steps:

1. Retype the logic as Pine. There is no DataFrame to slice and no vectorised rewrite: <code>ta.ema(close, len)</code> replaces the hand-rolled <code>ewm</code> chain, and <code>close[1]</code> replaces a <code>shift</code>.
2. Delete the <code># @param</code> block and write <code>input.*</code> declarations with the same defaults, plus the range the old code assumed (<code>minval</code>, <code>maxval</code>, <code>step</code>).
3. Delete the <code>output</code> dictionary and call the drawing function that matches each entry: a plot entry becomes <code>plot()</code>, a marker entry becomes <code>plotshape()</code>, a cloud entry becomes two plots and a <code>fill()</code>.
4. Keep the calculations, not the plumbing. Sentinel values, index gymnastics, and the <code>df</code> copy step all disappear.
5. Save. The parameter panel is derived from the code, so there is nothing to keep in sync afterwards: editing an <code>input.*</code> line is editing the form.

The panel derives itself from the source, which also means a value the user should be able to change must not be a literal buried in the body — promote it to an input while migrating, or it cannot be tuned later.

---

## 11. Pine type and series traps

Several Python habits translate badly. These are the cases that produce a chart that looks plausible and is wrong:

- <code>na</code> is Pine's missing value, and it propagates. Arithmetic on <code>na</code> yields <code>na</code>, a comparison with <code>na</code> is neither true nor false, and a plot of <code>na</code> is a gap. Do not paper over it with <code>nz()</code> unless the study genuinely means zero: <code>nz(rsi)</code> turns a warm-up gap into a value of 0, which is a real RSI reading.
- Integer division truncates. <code>1/2</code> is <code>0</code>, not <code>0.5</code>; write <code>1.0/2</code> or use a float input when a ratio is meant.
- Lengths and indices must be integers. <code>ta.ema(close, len)</code> requires an <code>int</code>; a float length is a compile error, not a rounding.
- A history offset reads the past only. <code>close[1]</code> is the previous bar, and a read past the first bar in the chart's data is <code>na</code>. Guard a long lookback with <code>bar_index &gt; n</code>.
- <code>var</code> and <code>varip</code> persist state across bars. That is what makes a counter or a drawing handle work, and it is also what makes a script behave differently from a fresh evaluation: initialise with <code>na</code> or a literal and set the value on the bar it belongs to.
- A series cannot be used where a simple value is required, such as an input default or a timeframe string. <code>timeframe.period</code> is read once and is a simple value; <code>str.tostring(close)</code> is a series.
- Strings in a title or a message are literals. An alert message cannot be built from a series inside <code>alertcondition()</code>; the message is fixed text.
- Division by zero yields <code>na</code> or infinity. Guard the denominator (<code>den == 0 ? na : num/den</code>) instead of letting a spike reach the plot.

---

## 12. Avoid look-ahead and repainting

A study may use only the current and earlier bars. The ways to break that in Pine:

- reading a still-forming higher-timeframe bar with <code>request.security()</code> and treating it as final;
- <code>lookahead=barmerge.lookahead_on</code> on a higher-timeframe read, which shows a value before the bar that produced it existed;
- plotting a condition that depends on the live close and then describing the marker as historical;
- using <code>barstate.islast</code> to rewrite what earlier bars should have shown;
- moving a marker to an earlier bar because the signal “should have” been there.

The honest crossover reads the current and previous state, and gates the event on a closed bar:

~~~pine
confirmedUp = ta.crossover(emaFast, emaSlow) and barstate.isconfirmed
~~~

This delays the event until the bar closes; it never reads a later bar. If a signal requires the close to confirm, the strategy that trades it executes later too. Do not move the signal backward to improve the chart or a backtest.

---

## 13. Language limits and safety

A Pine study cannot do anything dangerous, and the constraints that remain are about what can be expressed rather than what is permitted:

- No filesystem, network, database, process, or clock access. Everything comes from the chart's bars and the script's inputs.
- Imports resolve only against vendored sources. An unresolved import fails the whole script.
- One <code>indicator(...)</code> or <code>strategy(...)</code> declaration per file.
- No account, order, position, or portfolio state. <code>strategy.*</code> calls are legal but have nothing to act on in the browser.
- The compiler is the gate. Source that does not compile is refused with HTTP 400 at save time and never reaches the chart.
- Keep the per-bar work bounded. A script that loops over a large array on every bar is correct but slow, and the preview and alert replay both pay for it.

---

## 14. Complete tutorial: dual EMA viewer

~~~pine
//@version=6
// Dual EMA Viewer — chart-only study. Visual markers, no order logic.

indicator("Dual EMA Viewer", overlay=true)

// ===== Settings =====
fastLen = input.int(12, "Fast EMA length", minval=5, maxval=30, step=1, group="Parameters", tooltip="Bars used by the fast EMA.")
slowLen = input.int(26, "Slow EMA length", minval=10, maxval=80, step=2, group="Parameters", tooltip="Bars used by the slow EMA.")
showMarks = input.bool(true, "Show crossover markers", group="Visuals")
showLabels = input.bool(false, "Label each crossover", group="Visuals")
fastColor = input.color(#22C55E, "Fast EMA color", group="Visuals")
slowColor = input.color(#3B82F6, "Slow EMA color", group="Visuals")

// ===== Calculation =====
emaFast = ta.ema(close, fastLen)
emaSlow = ta.ema(close, slowLen)

// A crossover is a one-bar event. A continuous condition would mark every bar
// of the trend, and an alert on it would fire on every one of those bars.
golden = ta.crossover(emaFast, emaSlow)
death = ta.crossunder(emaFast, emaSlow)

// Only count the event once the bar has closed, so the study does not repaint.
confirmedGolden = golden and barstate.isconfirmed
confirmedDeath = death and barstate.isconfirmed

// ===== Drawing =====
plot(emaFast, "EMA Fast", color=fastColor, linewidth=2)
plot(emaSlow, "EMA Slow", color=slowColor, linewidth=2)

plotshape(showMarks and confirmedGolden, "Long Entry", shape.triangleup, location.belowbar, #22C55E, size=size.small, text="L")
plotshape(showMarks and confirmedDeath, "Long Exit", shape.triangledown, location.abovebar, #EF4444, size=size.small, text="X")

var label lastLabel = na

if showLabels and confirmedGolden
    if not na(lastLabel)
        label.delete(lastLabel)
    lastLabel := label.new(bar_index, low, "Golden cross", style=label.style_label_up, color=#22C55E, textcolor=#FFFFFF)

// ===== Alerts =====
alertcondition(confirmedGolden, "Golden cross", "EMA fast crossed above slow on a closed bar")
alertcondition(confirmedDeath, "Death cross", "EMA fast crossed below slow on a closed bar")
~~~

How it works:

1. The declaration states the study's name and that it draws over price.
2. Every tunable value is an <code>input.*</code>, so the settings panel builds itself from the source with the ranges and help text written here.
3. The EMAs are persistent series, so they are plots.
4. The crossings are one-bar events, so they are markers, and both are gated on <code>barstate.isconfirmed</code>.
5. The markers are named “Long Entry” and “Long Exit”. The bearish marker is an exit, not an automatic short entry — that decision belongs to the conversion, not to the drawing.
6. The alert conditions are declarations. They notify; they do not trade.
7. No order, position, or backtest appears anywhere in the script.

Open the same study on another symbol and timeframe: the code does not name either, because the chart supplies them.

---

## 15. Validation, debugging, and common errors

Use this workflow after each meaningful change:

1. Paste or edit the source in the Indicator IDE.
2. Save. The server compiles the source; a syntax error comes back as HTTP 400 <code>Invalid Pine Script: &lt;reason&gt;</code> with the failing line.
3. Open the study on a chart and inspect the drawing.
4. Change an input and confirm the drawing responds.
5. Confirm markers appear only on event bars, not on every bar of a trend.
6. Attach an alert condition and confirm it fires on the bar you expect.

The IDE also reports non-blocking quality hints while you edit. They describe structure, not values:

| Hint | Severity | Meaning | How to clear it |
| --- | --- | --- | --- |
| <code>EMPTY_CODE</code> | error | the source is empty | provide a complete script |
| <code>MISSING_PINE_VERSION</code> | warn | no <code>//@version=</code> line | make <code>//@version=6</code> the first line |
| <code>UNSUPPORTED_PINE_VERSION</code> | error | the declared version is below 2 | rewrite the source for version 2 or later |
| <code>UNKNOWN_PINE_VERSION</code> | warn | the declared version is above 6 | target 6, which is what the runtime implements |
| <code>PINE_SYNTAX_ERROR</code> | error | the parser rejected the source | fix the reported line; the message carries the position |
| <code>MISSING_SCRIPT_DECLARATION</code> | warn | no <code>indicator(...)</code> or <code>strategy(...)</code> call | add exactly one declaration on its own line |
| <code>MISSING_SCRIPT_TITLE</code> | info | the declaration has no title | pass the study name as the first argument |
| <code>SCRIPT_IS_A_STRATEGY</code> | info | the source declares <code>strategy(...)</code> | expected for a strategy; for a chart study declare <code>indicator(...)</code> |
| <code>NO_PLOTS_DECLARED</code> | warn | the script draws nothing | add a <code>plot</code>, <code>plotshape</code>, <code>fill</code>, <code>hline</code>, <code>bgcolor</code>, or <code>barcolor</code> call |

Hints with severity <code>warn</code> and <code>info</code> do not block a save. An <code>error</code> does: the source is not stored and the response carries the reason.

Common failures that are not hints:

| Symptom | Cause | Fix |
| --- | --- | --- |
| a marker on every bar | the condition is a state, not an event | use <code>ta.crossover</code> / <code>ta.crossunder</code> |
| an alert every bar | the same state condition | build the alert on the one-bar event |
| “could not find declaration” | the version line is not first, or the declaration is indented or split | first line, column zero, one line |
| a field missing from the panel | the input has no title | give the input a title |
| a field shows no default | the default is an expression, not a literal | use a literal default |
| the line connects through zero | a gap was plotted as <code>0</code> | plot <code>na</code> for the gap |
| a value changes on the last bar | the script reads a live value | gate on <code>barstate.isconfirmed</code> |

---

## 16. Semantic checklist before strategy conversion

Answer these questions before converting:

- Which marker is the long entry?
- Which marker is the long exit?
- Is shorting genuinely required? A bearish long exit is not automatically a short entry.
- Is reversal required? If so, are closing and reverse entry separate actions?
- On which timeframe and completed bar is the signal confirmed?
- Are repeated entries, scale-in, or scale-out allowed?
- How should sizing, stop-loss, take-profit, and trailing protection work?
- Which instrument and market type will the strategy own?
- If it is an exchange equity, what exact catalog product, venue, currency, product type, and API family will execution use? Preserve an instrument such as <code>Crypto:00700/HKD@gate:spot</code> or a catalog-confirmed <code>Crypto:HK0700/USDT@binance:swap</code>; do not replace it with its underlying stock.
- Is the product a long-only spot equity or a Crypto swap derivative that requires explicit direction metadata and <code>position_side</code>?

The converted strategy drops the chart-only parts: colors, plot styles, offsets, markers, labels, and fills. Keep the signal algebra — the conditions and the bar they fire on — and declare instruments, frequency, sizing, and risk explicitly with Strategy API V2. If the original study already used <code>strategy(...)</code> and its conditions are written as strategy entries, the conversion is mostly a matter of moving that source onto the strategy side.

Underlying stock data can support chart history and point-in-time fundamentals only when the active catalog supplies a valid <code>USStock</code> or <code>HKStock</code> mapping. That data mapping never changes the execution instrument, and the converter must not guess a region from a display name.

Always verify and backtest generated strategy code again. Marketplace publication requires at least one successful backtest record.

---

## 17. Pre-publication checklist

- [ ] <code>//@version=6</code> is the first line (or <code>//@version=5</code> for a v5 target).
- [ ] Exactly one <code>indicator(...)</code> declaration, with a title that names the study.
- [ ] The study draws something: a plot, a marker, a fill, or a level.
- [ ] Every tunable value is an <code>input.*</code> with a title, and numeric inputs carry sensible <code>minval</code>/<code>maxval</code>/<code>step</code>.
- [ ] Comments, identifiers, input titles, and plot titles are English.
- [ ] Markers are one-bar events built on <code>ta.crossover</code> / <code>ta.crossunder</code>, not on a persistent state.
- [ ] Anything that reads a live value is gated on <code>barstate.isconfirmed</code>.
- [ ] Higher-timeframe reads use <code>request.security()</code> without <code>lookahead_on</code>.
- [ ] No order, position, leverage, or execution-risk logic is present.
- [ ] Missing values are <code>na</code>; no sentinel such as <code>0</code> stands in for a gap.
- [ ] No future data is read: no live higher-timeframe value, no rewritten past bar.
- [ ] Short datasets, warm-up periods, and unusual parameters do not crash the preview.
- [ ] The script saves without an <code>error</code>-severity hint, and the chart preview draws what the description claims.

---


## 18. Where the IDE ends and the library begins

The Indicator IDE is a chart workspace, not a drawing kit built beside one. The ownership split is deliberate, and it decides where a new control belongs:

| Concern | Owned by |
| --- | --- |
| Symbol (search, watchlist, venue) | the library's native header |
| Interval and chart type | the library's native header |
| Adding a study, moving it between panes, reading its legend values | the library |
| A study's inputs | the library's Settings dialog, backed by the script's <code>input.*</code> metadata |
| Saving the script, signal alerts, converting to a strategy | the IDE's own action bar |
| The Pine editor | the IDE, as a bottom drawer bound to one pane |
| The workspace archive (panes, symbols, intervals, studies, inputs) | the IDE, through <code>/api/users/chart-templates</code> |

Two rules follow:

- Do not add a second symbol picker, interval picker, chart-type picker, or parameter form to the IDE chrome. Every one of those was removed once the library rendered its own; a duplicate control is a second source of truth that drifts.
- Anything that changes what the chart shows must go through the library's API (<code>createStudy</code>, <code>removeEntity</code>, <code>getInputValues</code>, <code>setChartType</code>) or through the datafeed, so reloading the workspace reproduces the same screen.

The design record, including the ticker contract (<code>QD:&lt;market&gt;:&lt;marketType&gt;:&lt;exchangeId&gt;:&lt;instrumentId&gt;</code>) and the archive JSON shape, is [The chart-first Indicator IDE](INDICATOR_IDE_CHART_FIRST.md).
