"""Pine Script -> chart indicator transpiler.

A Pine *indicator* is a different program from a Pine strategy: it has no
``strategy.*`` namespace, draws its findings with ``plot``/``plotshape`` instead
of placing orders, and is judged by whether its lines look like the author's
rather than by whether a backtest made money. The strategy transpiler therefore
rejects every one of them ("no_strategy_entry"), and this module is the second
path: it reuses the same expression translator and per-bar runtime, but collects
what the script draws instead of what it trades.

The emitted program is a normal chart indicator in the Indicator IDE contract:
it reads ``df``/``params``, appends one value per bar to pre-declared buffers, and
finally assembles them into ``output``. Nothing is drawn inside the per-bar loop;
the loop only records numbers, so the cost is the same as any hand-written
indicator and the result is plain JSON the API can return.

Typical use::

    from app.services.pine_transpiler.chart import transpile_indicator

    result = transpile_indicator(pine_source, name="Bands")
    if result.ok:
        save(result.code)
"""

from __future__ import annotations

import ast
import re
from typing import Any, Final

from . import _clean_title, _module_constants, _param_annotations, _WARMUP_BARS
from .emitter import Emitter, EmitError, normalize_timeframe
from .helpers import helper_source
from .pine_ast import PineParseError, ParserUnavailable, parse
from .result import TranspileResult

__all__ = ["transpile_indicator"]

#: Frequencies the runtime can subscribe to, reused for the chart contract.
_SUPPORTED_FREQUENCIES: Final[frozenset[str]] = frozenset(
    {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"}
)

#: Fields the generated code reads from the frame.
_HISTORY_FIELDS: Final[tuple[str, ...]] = ("open", "high", "low", "close", "volume")

#: Identifier prefix reserved for generated buffers.
_RESERVED_PREFIX: Final[str] = "_qd_"

#: Statements allowed at module level by the indicator sandbox.
_SAFE_MODULE_LEVEL: Final[tuple[str, ...]] = (
    "docstring",
    "import",
    "literal",
    "function",
)

#: The chart contract's plot types, as documented in INDICATOR_DEV_GUIDE.md.
_PLOT_TYPES: Final[frozenset[str]] = frozenset({"line", "bar", "circle", "cloud"})


def transpile_indicator(
    source: str,
    *,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    name: str = "",
) -> TranspileResult:
    """Translate one Pine indicator into Indicator IDE source.

    ``ok`` is set only when the emitted module also passed ``ast.parse`` and its
    plot buffers are non-empty; the sandbox itself is exercised by the pipeline,
    not here, so a caller can convert a whole corpus without a database.
    """
    text = str(source or "")
    if not text.strip():
        return TranspileResult(ok=False, error="empty_source")

    frequency = normalize_timeframe(timeframe)
    if frequency not in _SUPPORTED_FREQUENCIES:
        return TranspileResult(ok=False, error=f"unsupported_timeframe:{frequency}")

    try:
        tree = parse(text)
    except ParserUnavailable as exc:
        return TranspileResult(ok=False, error=f"parser_unavailable:{exc}")
    except PineParseError as exc:
        return TranspileResult(ok=False, error=f"parse_error:{exc}")

    emitter = Emitter(symbol=symbol, timeframe=frequency, name=name)
    # Collect the drawing calls instead of dropping them, and stop lowering
    # drawing conditions into orders: an indicator never trades.
    emitter.chart_mode = True
    emitter.signal_orders_enabled = False

    try:
        emitted = emitter.emit(tree)
    except EmitError as exc:
        return TranspileResult(ok=False, error=f"emit_failed:{exc}")

    code, build_error = _assemble(
        emitter=emitter,
        emitted=emitted,
        symbol=symbol,
        frequency=frequency,
        name=name,
    )
    if build_error:
        return TranspileResult(ok=False, error=build_error)

    blockers = [
        str(item)
        for item in (emitted.get("blockers") or [])
        if str(item)
    ]
    # ``no_chart_output`` is fatal: an indicator that draws nothing renders as an
    # empty pane, which a reader cannot tell apart from a failed conversion.
    if "no_chart_output" in blockers:
        return TranspileResult(
            ok=False,
            code=code,
            blockers=blockers,
            warnings=list(emitted.get("warnings") or []),
            stats=dict(emitted.get("stats") or {}),
            params=dict(emitted.get("params") or {}),
            error="no_chart_output",
        )

    try:
        ast.parse(code)
    except SyntaxError as exc:
        return TranspileResult(ok=False, code=code, error=f"compile_failed:{exc.msg}")

    stats = dict(emitted.get("stats") or {})
    # Callers need the drawing inventory to report on a corpus and to render a
    # legend, and the stats mapping is the one field ``TranspileResult`` exposes
    # for free-form numbers like this.
    stats["plots"] = [dict(item) for item in (emitted.get("chart_plots") or [])]
    stats["signals"] = [dict(item) for item in (emitted.get("chart_signals") or [])]
    return TranspileResult(
        ok=True,
        code=code,
        blockers=blockers,
        warnings=list(emitted.get("warnings") or []),
        stats=stats,
        params=dict(emitted.get("params") or {}),
        entry_style="chart",
    )


def _assemble(
    *,
    emitter: Emitter,
    emitted: dict[str, Any],
    symbol: str,
    frequency: str,
    name: str,
) -> tuple[str, str | None]:
    """Render the Indicator IDE module, or an error string."""
    plots = list(emitted.get("chart_plots") or [])
    signals = list(emitted.get("chart_signals") or [])
    if not plots and not signals:
        return "", "no_chart_output"

    title = _clean_title(name) or "Pine indicator"
    params = dict(emitted.get("params") or {})

    lines: list[str] = []
    lines.extend(_docstring(title))
    lines.extend(_param_annotations(params))
    lines.append("import numpy as np")
    lines.append("import pandas as pd")
    lines.append("")
    lines.append(f'_QD_SYMBOL = "{symbol}"')
    lines.append(f'_QD_TIMEFRAME = "{frequency}"')
    # The translation layer renders ``timeframe.*`` and order calls as the
    # strategy-side module constants (``_TIMEFRAME``, ``_SYMBOL`` and the rest),
    # which the strategy assembler defines. A chart module needs the same names
    # present, otherwise a script that so much as reads ``timeframe.period``
    # dies on NameError before it draws anything.
    lines.extend(_module_constants(symbol=symbol, frequency=frequency))
    # The chart contract forbids executable statements at module level, so the
    # plot descriptors are literals and the buffers are created inside the
    # replay loop below.
    lines.append(f"_QD_PLOTS = {_plot_descriptors(plots)!r}")
    lines.append(f"_QD_SIGNALS = {_signal_descriptors(signals)!r}")
    lines.append("")
    lines.extend(_helper_functions(emitter=emitter, emitted=emitted))
    lines.append("")
    lines.extend(_render_body(emitter=emitter, emitted=emitted, plots=plots, signals=signals))
    return "\n".join(lines).rstrip() + "\n", None


def _docstring(title: str) -> list[str]:
    """Render the module docstring, escaping anything the sandbox dislikes."""
    safe = str(title or "Pine indicator").replace("\\", " ").replace('"""', "'''")
    # The sandbox scans string literals for ``__x__`` patterns and for calls to
    # banned builtins, so the title must not contain either.
    safe = re.sub(r"__[A-Za-z0-9_]+__", " ", safe)
    return [f'"""{safe}."""', ""]


def _plot_descriptors(plots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render the literal descriptions of the collected plots."""
    output: list[dict[str, Any]] = []
    for plot in plots:
        style = str(plot.get("style") or "line")
        if style not in _PLOT_TYPES:
            style = "line"
        output.append({
            "key": str(plot.get("key") or ""),
            "name": str(plot.get("title") or "Plot"),
            "type": style,
            "color": str(plot.get("color") or ""),
            "overlay": bool(plot.get("overlay")),
        })
    return output


def _signal_descriptors(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render the literal descriptions of the collected markers."""
    output: list[dict[str, Any]] = []
    for signal in signals:
        output.append({
            "key": str(signal.get("key") or ""),
            "name": str(signal.get("title") or "Signal"),
            "color": str(signal.get("color") or ""),
            "type": "buy" if signal.get("long") else "sell",
        })
    return output


def _helper_functions(*, emitter: Emitter, emitted: dict[str, Any]) -> list[str]:
    """Render exactly the helpers the translated statements call."""
    used = set(emitted.get("helpers") or ())
    for name in ("state", "runtime"):
        used.add(name)
    return helper_source(used).splitlines()


def _render_body(
    *,
    emitter: Emitter,
    emitted: dict[str, Any],
    plots: list[dict[str, Any]],
    signals: list[dict[str, Any]],
) -> list[str]:
    """Render the per-bar replay and the final ``output`` assignment.

    The replay walks the frame one bar at a time and re-runs the translated
    statements with the frame truncated at that bar. That is what Pine itself
    does, so ``var`` persistence, ``if`` branches and history lookups inside the
    script keep their meaning; a vectorised rewrite would change the semantics of
    the 112 of 122 scripts in the sample that reassign variables per bar.

    Nothing is drawn inside the loop: each ``plot`` statement appends one number
    to a buffer, and ``output`` is assembled afterwards from those buffers.
    """
    # The strategy assembler reads inputs from ``context.params``; the chart
    # contract hands them in a plain dict called ``params``, and there is no
    # context object at all, so every reference has to be renamed. Inputs that
    # are read inline (not hoisted into ``param_lines``) end up in the statements
    # themselves, which is why the whole emitted body is rewritten rather than
    # just the parameter prologue.
    def _chart_scope(text: str) -> str:
        return text.replace("context.params.get(", "params.get(")

    statements = [_chart_scope(line) for line in (emitted.get("statements") or [])]
    function_blocks = [
        [_chart_scope(line) for line in block]
        for block in (emitted.get("function_blocks") or [])
    ]
    parameter_lines = [_chart_scope(line) for line in (emitted.get("param_lines") or [])]
    predeclared = list(emitted.get("predeclared") or [])
    state = _state_literal(emitter)

    lines: list[str] = []
    # The sandbox injects ``df`` and ``params`` before the module runs, and the
    # chart contract forbids executable module-level statements, so the title has
    # to be a plain literal here rather than a computed value.
    lines.append(f"_QD_NAME = {_attr_safe_string(emitter.name)!r}")
    lines.append(f"_QD_STATE = {state}")
    lines.append(f"_QD_FIELDS = {list(_HISTORY_FIELDS)!r}")
    lines.append(f"_QD_WARMUP = {_WARMUP_BARS}")
    lines.append("_QD_CHART = {}")
    lines.append("_FRAME_REF = [None]")
    lines.append("_STATE = _QD_STATE")
    lines.append("_BAR_COUNTER = [0]")
    lines.append("_bar_index = 0")
    lines.append("_time = 0")
    # Trade counters are strategy vocabulary, but translated scripts reference
    # them; an indicator has no trades, and a NameError here would be fatal.
    lines.append("_closed_trades = 0.0")
    lines.append("_win_trades = 0.0")
    lines.append("_loss_trades = 0.0")
    lines.append("open_series = None")
    lines.append("high_series = None")
    lines.append("low_series = None")
    lines.append("close_series = None")
    lines.append("volume_series = None")
    lines.append("hl2_series = None")
    lines.append("hlc3_series = None")
    lines.append("ohlc4_series = None")
    lines.append("hlcc4_series = None")
    lines.append("")
    lines.append("")
    # A handful of published "indicators" still call ``strategy.entry`` out of
    # habit. Order helpers do not exist in the chart sandbox, so they are stubbed
    # here: the drawing is what the reader asked for, and the call is not wrong
    # enough to reject the whole script over.
    lines.append("def order_target_percent(symbol=None, target=None, **options):")
    lines.append('    """Absorb an order call: a chart draws, it does not trade."""')
    lines.append("    return None")
    lines.append("")
    lines.append("")
    lines.append("def get_position(symbol=None):")
    lines.append('    """Report a flat position, which is all an indicator can be."""')
    lines.append("    return None")
    lines.append("")
    lines.append("")
    lines.append("def _qd_series(frame, column):")
    lines.append('    """Column lookup, tolerant of the frame not carrying the name."""')
    lines.append("    if column in frame.columns:")
    lines.append("        return frame[column]")
    lines.append("    return pd.Series([float('nan')] * len(frame), index=frame.index, dtype='float64')")
    lines.append("")
    lines.append("")
    lines.append("def _qd_replay(frame, params):")
    lines.append('    """Run the translated script over every bar and collect its drawing."""')
    lines.append("    global _bar_index, _time")
    lines.append("    global open_series, high_series, low_series, close_series, volume_series")
    lines.append("    global hl2_series, hlc3_series, ohlc4_series, hlcc4_series")
    lines.append("    total = len(frame)")
    lines.append("    for position in range(total):")
    lines.append("        window = frame.iloc[: position + 1]")
    lines.append("        _FRAME_REF[0] = window")
    lines.append("        _BAR_COUNTER[0] = position + 1")
    lines.append("        _bar_index = position")
    lines.append("        _time = int(pd.DatetimeIndex(window.index).asi8[-1] // 1000000)")
    lines.append("        open_series = _qd_series(window, 'open')")
    lines.append("        high_series = _qd_series(window, 'high')")
    lines.append("        low_series = _qd_series(window, 'low')")
    lines.append("        close_series = _qd_series(window, 'close')")
    lines.append("        volume_series = _qd_series(window, 'volume')")
    lines.append("        hl2_series = (high_series + low_series) / 2.0")
    lines.append("        hlc3_series = (high_series + low_series + close_series) / 3.0")
    lines.append("        ohlc4_series = (open_series + high_series + low_series + close_series) / 4.0")
    lines.append("        hlcc4_series = (close_series * 2.0 + high_series + low_series) / 4.0")
    if predeclared:
        for line in predeclared:
            lines.append(f"        {line.strip()}")
    if parameter_lines:
        lines.append("")
        for line in parameter_lines:
            lines.append(f"        {line}")
    if function_blocks:
        lines.append("")
        for block in function_blocks:
            for block_line in block:
                lines.append(f"        {block_line}" if block_line.strip() else block_line)
    lines.append("")
    for statement in statements:
        # Statements were emitted at ``depth=1``, so they already carry one
        # level of indentation; the loop body is one level deeper still. Adding
        # a full loop indent here instead would nest them inside the last
        # translated function, where they never run.
        lines.append(f"    {statement}" if statement.strip() else statement)
    lines.append("")
    lines.append("")
    lines.append("def _qd_pad(values, total):")
    lines.append('    """Align a collected buffer to the frame, filling gaps with None."""')
    lines.append("    while len(values) < total:")
    lines.append("        values.append(None)")
    lines.append("    return values[:total]")
    lines.append("")
    lines.append("")
    # The sandbox injects ``df`` and ``params`` into the module namespace, so
    # both are ordinary globals here; ``dir()`` is not needed and is not part of
    # the builtins whitelist.
    lines.append("_qd_params = params if isinstance(params, dict) else {}")
    lines.append("_qd_replay(df, _qd_params)")
    lines.append("")
    lines.append("_qd_plots = []")
    lines.append("for _qd_spec in _QD_PLOTS:")
    lines.append("    _qd_values = _qd_pad(_QD_CHART.get(_qd_spec['key']) or [], len(df))")
    lines.append("    _qd_plots.append({")
    lines.append("        'name': _qd_spec['name'],")
    lines.append("        'data': _qd_values,")
    lines.append("        'color': _qd_spec['color'],")
    lines.append("        'type': _qd_spec['type'],")
    lines.append("        'overlay': _qd_spec['overlay'],")
    lines.append("    })")
    lines.append("_qd_signals = []")
    lines.append("for _qd_spec in _QD_SIGNALS:")
    lines.append("    _qd_marks = _QD_CHART.get(_qd_spec['key']) or []")
    lines.append("    _qd_flags = [1.0 if item else None for item in _qd_pad(list(_qd_marks), len(df))]")
    lines.append("    _qd_signals.append({")
    lines.append("        'type': _qd_spec['type'],")
    lines.append("        'text': _qd_spec['name'],")
    lines.append("        'color': _qd_spec['color'],")
    lines.append("        'data': _qd_flags,")
    lines.append("    })")
    lines.append("")
    lines.append("output = {")
    lines.append("    'name': _QD_NAME,")
    lines.append("    'plots': _qd_plots,")
    lines.append("    'signals': _qd_signals,")
    lines.append("    'layers': [],")
    lines.append("}")
    return lines


def _attr_safe_string(text: str) -> str:
    """Strip anything from a title that would break the generated module."""
    clean = str(text or "Pine indicator").replace("\\", " ")
    clean = clean.replace("\"", "'").replace("\n", " ")
    return re.sub(r"__[A-Za-z0-9_]+__", " ", clean)[:120]


def _state_literal(emitter: Emitter) -> str:
    """Render the state slots as a plain literal dictionary."""
    from . import _literal_or_default

    pairs: list[str] = []
    for key in sorted(emitter.state_keys.values()):
        rendered = _literal_or_default(emitter.state_init.get(key, ""))
        if rendered is None:
            continue
        pairs.append(f'"{key}": {rendered}')
    return "{" + ", ".join(pairs) + "}" if pairs else "{}"
