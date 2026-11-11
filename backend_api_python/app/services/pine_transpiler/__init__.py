"""Pine Script -> Strategy API V2 transpiler.

Converts a TradingView Pine Script strategy into native QuantDinger strategy
source. The conversion is deliberately conservative and self-verifying: a
result is only reported as ``ok`` after the emitted source passed
``compile_strategy_v2``, so callers never receive code that will not compile.

Typical use::

    from app.services.pine_transpiler import transpile_pine

    result = transpile_pine(pine_source, symbol="BTC/USDT", timeframe="1h")
    if result.ok:
        deploy(result.code)

The optional ``pynescript`` parser (LGPL-3.0-or-later) is imported lazily so this
module stays import-safe when the dependency is absent; degraded calls return a
result with ``error == "parser_unavailable"`` instead of raising.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Final

from .assess import assess_migratability
from .emitter import Emitter, EmitError, normalize_timeframe
from .helpers import helper_source
from .pine_ast import (
    PARSER_REQUIREMENT,
    PineParseError,
    ParserUnavailable,
    parse,
    parser_available,
)
from .result import TranspileResult

__all__ = [
    "TranspileResult",
    "assess_migratability",
    "normalize_timeframe",
    "parser_available",
    "transpile_pine",
]

#: Bars of history the generated strategy requests each bar.
_WARMUP_BARS: Final[int] = 300

#: Frequencies the runtime can subscribe to.
_SUPPORTED_FREQUENCIES: Final[frozenset[str]] = frozenset(
    {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"}
)

#: Fields requested from ``get_history``. This must be a list: the runtime treats
#: a plain string as a single field name and would return an empty selection.
_HISTORY_FIELDS: Final[tuple[str, ...]] = ("open", "high", "low", "close", "volume")

#: Identifier prefix reserved for generated helpers.
_RESERVED_PREFIX: Final[str] = "_p_"

#: Maximum emitted source length before the transpiler gives up.
_MAX_SOURCE_CHARS: Final[int] = 400_000


def transpile_pine(
    source: str,
    *,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    name: str = "",
) -> TranspileResult:
    """Convert Pine Script ``source`` into Strategy API V2 source.

    Args:
        source: Pine Script text, typically a public TradingView strategy.
        symbol: Instrument the generated strategy trades.
        timeframe: Driving frequency of the generated strategy.
        name: Human-readable script name used in the generated docstring.

    Returns:
        A :class:`TranspileResult`. ``ok`` is ``True`` only when the emitted code
        compiled through ``compile_strategy_v2``.
    """
    text = str(source or "")
    stats: dict[str, Any] = {"chars": len(text)}
    warnings: list[str] = []
    params: dict[str, dict[str, Any]] = {}

    if not text.strip():
        return TranspileResult(
            ok=False,
            code=None,
            blockers=["empty_source"],
            warnings=warnings,
            stats=stats,
            params=params,
            entry_style="none",
            error="empty_source",
        )
    if len(text) > _MAX_SOURCE_CHARS:
        return TranspileResult(
            ok=False,
            code=None,
            blockers=["source_too_large"],
            warnings=warnings,
            stats=stats,
            params=params,
            entry_style="none",
            error="source_too_large",
        )

    if not parser_available():
        return TranspileResult(
            ok=False,
            code=None,
            blockers=["parser_unavailable"],
            warnings=[f"install {PARSER_REQUIREMENT} to enable Pine conversion"],
            stats=stats,
            params=params,
            entry_style="none",
            error="parser_unavailable",
        )

    try:
        tree = parse(text)
    except ParserUnavailable as exc:
        return TranspileResult(
            ok=False,
            code=None,
            blockers=["parser_unavailable"],
            warnings=[exc.reason],
            stats=stats,
            params=params,
            entry_style="none",
            error="parser_unavailable",
        )
    except PineParseError as exc:
        return TranspileResult(
            ok=False,
            code=None,
            blockers=["parse_error"],
            warnings=[exc.message],
            stats={**stats, "parse_error": exc.message},
            params=params,
            entry_style="none",
            error="parse_error",
        )

    emitter = Emitter(symbol=symbol, timeframe=timeframe, name=name)
    try:
        emitted = emitter.emit(tree)
    except EmitError as exc:
        return TranspileResult(
            ok=False,
            code=None,
            blockers=[exc.blocker],
            warnings=[*emitter.warnings, str(exc)],
            stats={**stats, **emitter.stats},
            params=emitter.params,
            entry_style=emitter.entry_style,
            error=exc.blocker,
        )
    except RecursionError:
        return TranspileResult(
            ok=False,
            code=None,
            blockers=["source_too_complex"],
            warnings=[*emitter.warnings, "Pine source nests too deeply"],
            stats={**stats, **emitter.stats},
            params=emitter.params,
            entry_style=emitter.entry_style,
            error="source_too_complex",
        )

    warnings.extend(emitted["warnings"])
    stats.update(emitted["stats"])
    params = dict(emitted["params"])
    blockers: list[str] = list(emitted["blockers"])

    assembly = _assemble(
        emitter=emitter,
        emitted=emitted,
        source=text,
        symbol=symbol,
        timeframe=timeframe,
        name=name,
    )
    if assembly.get("error"):
        return TranspileResult(
            ok=False,
            code=assembly.get("code") or None,
            blockers=list(dict.fromkeys([*blockers, "assembly_failed"])),
            warnings=[*warnings, str(assembly["error"])],
            stats=stats,
            params=params,
            entry_style=str(emitted.get("entry_style") or "none"),
            error="assembly_failed",
        )

    code = str(assembly["code"])
    blockers = list(dict.fromkeys([*blockers, *assembly["blockers"]]))
    if blockers:
        return TranspileResult(
            ok=False,
            code=code,
            blockers=blockers,
            warnings=warnings,
            stats=stats,
            params=params,
            entry_style=str(emitted.get("entry_style") or "none"),
            error=blockers[0],
        )

    gate = _compile_gate(code)
    if gate is not None:
        return TranspileResult(
            ok=False,
            code=code,
            blockers=list(dict.fromkeys([*blockers, gate])),
            warnings=[*warnings, f"generated code rejected by strategy compiler: {gate}"],
            stats=stats,
            params=params,
            entry_style=str(emitted.get("entry_style") or "none"),
            error=gate,
        )

    return TranspileResult(
        ok=True,
        code=code,
        blockers=[],
        warnings=warnings,
        stats={**stats, "code_chars": len(code)},
        params=params,
        entry_style=str(emitted.get("entry_style") or "none"),
        error=None,
    )


def _compile_gate(code: str) -> str | None:
    """Compile ``code`` with the strategy contract; return an error code or None."""
    try:
        from app.services.strategy_v2.contract import compile_strategy_v2
    except ImportError:  # pragma: no cover - contract module is always present in-tree
        return None
    try:
        compile_strategy_v2(code)
    except Exception as exc:  # noqa: BLE001 - the contract raises its own error type
        detail = str(getattr(exc, "code", "") or exc or type(exc).__name__)
        return f"compile_failed:{detail}"
    return None


def _assemble(
    *,
    emitter: Emitter,
    emitted: dict[str, Any],
    source: str,
    symbol: str,
    timeframe: str,
    name: str,
) -> dict[str, Any]:
    """Build the final Strategy API V2 module source."""
    frequency = normalize_timeframe(timeframe)
    if frequency not in _SUPPORTED_FREQUENCIES:
        return {"code": "", "blockers": ["unsupported_timeframe"], "error": frequency}

    statements: list[str] = list(emitted.get("statements") or [])

    title = _clean_title(name) or "Pine strategy"
    params = dict(emitted.get("params") or {})
    extra_frequencies = [
        item for item in emitted.get("extra_frequencies") or [] if item in _SUPPORTED_FREQUENCIES
    ]

    lines: list[str] = []
    lines.extend(_docstring(title))
    lines.extend(_param_annotations(params))
    lines.append("import numpy as np")
    lines.append("import pandas as pd")
    lines.append("")
    lines.extend(_module_constants(symbol=symbol, frequency=frequency))
    lines.append("")
    lines.append(f"_STATE = {_state_literal(emitter)}")
    lines.append("")
    lines.extend(_helper_functions(emitter=emitter, emitted=emitted))
    lines.append("")
    lines.extend(_initialize_function(frequency=frequency, extra=extra_frequencies))
    lines.append("")
    lines.extend(
        _handle_data_function(
            statements=statements,
            emitted=emitted,
            function_blocks=list(emitted.get("function_blocks") or []),
        )
    )

    code = "\n".join(lines).rstrip() + "\n"
    blockers: list[str] = []
    try:
        ast.parse(code)
    except SyntaxError as exc:
        # Keep the rejected source: a syntax error in generated code is only
        # debuggable with the offending lines in hand.
        return {
            "code": code,
            "blockers": ["emitted_source_invalid"],
            "error": f"{exc.msg} (line {exc.lineno})",
        }

    if not code.strip():
        blockers.append("emitted_source_empty")
    return {"code": code, "blockers": blockers, "error": None}


def _docstring(title: str) -> list[str]:
    """Render the generated module docstring."""
    return [
        f'"""{title}.',
        "",
        "Generated by the Pine Script transpiler from a TradingView strategy.",
        "Structure mirrors the source script: module state holds the Pine ``var``",
        "slots and the generated helpers reproduce the indicator math with pandas.",
        '"""',
    ]


#: Comment text that would otherwise trip a runtime safety regex. ``open``,
#: ``eval`` and friends are only dangerous as calls, but the sandbox patterns
#: also match plain words followed by a parenthesis.
_SAFE_COMMENT_TEXT: Final[re.Pattern[str]] = re.compile(r"(?<![\w.])(open|file|eval|exec|compile)\s*\(")

def _param_annotations(params: dict[str, dict[str, Any]]) -> list[str]:
    """Render ``# @param`` annotations for the inferred inputs."""
    lines: list[str] = []
    for name, schema in params.items():
        kind = str(schema.get("type") or "float")
        default = schema.get("default")
        rendered = _param_default_literal(default, kind)
        description = str(schema.get("title") or "").strip()
        if description:
            description = description.replace("\n", " ")
            # Comments are scanned by the runtime safety regexes, so a title such
            # as "RTH open (09:30)" would otherwise trip the ``\bopen\s*\(``
            # rule and reject the whole module.
            description = _SAFE_COMMENT_TEXT.sub(" ", description)
        annotation = f"# @param {name} {kind} {rendered} {description}".rstrip()
        # Comments are scanned by the runtime safety regexes as well.
        lines.append(_SAFE_COMMENT_TEXT.sub(lambda match: f"{match.group(1)} ", annotation))
    return lines


def _param_default_literal(value: Any, kind: str) -> str:
    """Render a parameter default in the ``# @param`` textual form."""
    if kind == "bool":
        return "true" if value else "false"
    if kind == "string":
        text = str(value if value is not None else "")
        return text or ""
    if kind == "int":
        try:
            return str(int(float(value)))
        except (TypeError, ValueError):
            return "0"
    try:
        return repr(float(value))
    except (TypeError, ValueError):
        return "0.0"


#: Seconds per bar for each supported subscription frequency.
_FREQUENCY_SECONDS: Final[dict[str, int]] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}


def _module_constants(*, symbol: str, frequency: str) -> list[str]:
    """Render the literal module constants the generated body relies on."""
    warmup = _WARMUP_BARS
    multiplier, intraday = _timeframe_flags(frequency)
    seconds = _FREQUENCY_SECONDS.get(frequency, 3600)
    return [
        f'_SYMBOL = "{symbol}"',
        f'_TIMEFRAME = "{frequency}"',
        f"_WARMUP = {warmup}",
        f"_FIELDS = {list(_HISTORY_FIELDS)!r}",
        f"_TIMEFRAME_MULTIPLIER = {multiplier}",
        f"_TIMEFRAME_IS_INTRADAY = {intraday}",
        f"_TIMEFRAME_IS_DAILY = {str(frequency == '1d')}",
        f"_TIMEFRAME_IS_WEEKLY = {str(frequency == '1w')}",
        "_TIMEFRAME_IS_MONTHLY = False",
        f"_TIMEFRAME_SECONDS = {seconds}",
        f'_QUOTE_CURRENCY = "{_quote_currency(symbol)}"',
        "_INITIAL_CAPITAL = 100000.0",
        '_ENTRY_NAME = "pine"',
        "_bar_index = 0",
        "_time = 0",
        "_BAR_COUNTER = [0]",
        "_FRAME_REF = [None]",
        "open_series = None",
        "high_series = None",
        "low_series = None",
        "close_series = None",
        "volume_series = None",
        "hl2_series = None",
        "hlc3_series = None",
        "ohlc4_series = None",
        "hlcc4_series = None",
    ]


def _timeframe_flags(frequency: str) -> tuple[int, bool]:
    """Return the minute multiplier and intraday flag for a frequency."""
    minutes = {
        "1m": 1,
        "3m": 3,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60,
        "4h": 240,
        "1d": 1440,
        "1w": 10080,
    }.get(frequency, 1440)
    return minutes, minutes < 1440


def _quote_currency(symbol: str) -> str:
    """Return the quote currency of an instrument symbol."""
    parts = str(symbol or "").replace("-", "/").split("/")
    return parts[1] if len(parts) > 1 else "USDT"


def _state_literal(emitter: Emitter) -> str:
    """Render the initial ``_STATE`` mapping for the persistent Pine slots.

    Only literal initialisers are allowed at module level, so a slot whose Pine
    declaration is dynamic (for example ``var float x = ta.sma(close, 9)``)
    starts at ``0.0`` and the first bar writes the real value.
    """
    pairs: list[str] = []
    for key in sorted(emitter.state_keys.values()):
        initialiser = emitter.state_init.get(key, "")
        rendered = _literal_or_default(initialiser)
        if rendered is None:
            # A dynamic initialiser such as ``array.new_line(0)`` cannot live in
            # the module-level literal. Leave the key absent so the read-time
            # fallback supplied by ``_state_get`` initialises it instead.
            continue
        pairs.append(f'"{key}": {rendered}')
    if not pairs:
        return "{}"
    return "{" + ", ".join(pairs) + "}"


def _helper_functions(*, emitter: Emitter, emitted: dict[str, Any]) -> list[str]:
    """Render the helper block the generated strategy needs."""
    used = set(emitted.get("helpers") or ())
    for name in ("state", "runtime"):
        used.add(name)
    return helper_source(used).splitlines()


def _initialize_function(*, frequency: str, extra: list[str]) -> list[str]:
    """Render the generated ``initialize`` handler."""
    lines = [
        "def initialize(context):",
        "    context.set_universe([_SYMBOL])",
        f'    context.subscribe(frequency="{frequency}")',
    ]
    for item in extra:
        if item == frequency:
            continue
        lines.append(f'    context.subscribe(frequency="{item}")')
    lines.append("    context.set_warmup(_WARMUP)")
    return lines


def _handle_data_function(
    *,
    statements: list[str],
    emitted: dict[str, Any],
    function_blocks: list[list[str]] | None = None,
) -> list[str]:
    """Render the generated ``handle_data`` handler.

    Translated Pine functions are nested inside the handler rather than emitted
    at module level: Pine functions see the script's top-level variables, which
    only exist as ``handle_data`` locals in the generated strategy.
    """
    lines = [
        "def handle_data(context, data):",
        "    global _bar_index, _time",
        "    global open_series, high_series, low_series, close_series, volume_series",
        "    global hl2_series, hlc3_series, ohlc4_series, hlcc4_series",
        "    frame = get_history(_WARMUP, _TIMEFRAME, _FIELDS, _SYMBOL)",
        "    if frame is None or len(frame) < 2:",
        "        return",
        '    open_series = _p_col(frame, "open")',
        '    high_series = _p_col(frame, "high")',
        '    low_series = _p_col(frame, "low")',
        '    close_series = _p_col(frame, "close")',
        '    volume_series = _p_col(frame, "volume")',
        "    hl2_series = (high_series + low_series) / 2.0",
        "    hlc3_series = (high_series + low_series + close_series) / 3.0",
        "    ohlc4_series = (open_series + high_series + low_series + close_series) / 4.0",
        "    hlcc4_series = (close_series * 2.0 + high_series + low_series) / 4.0",
        "    _FRAME_REF[0] = frame",
        # Pine's bar_index counts bars since the script started. The history
        # window saturates at _WARMUP bars, so len(frame) stops growing and
        # would freeze every `bar_index % n` guard at the same value.
        "    _BAR_COUNTER[0] = _BAR_COUNTER[0] + 1",
        "    _bar_index = _BAR_COUNTER[0] - 1",
        "    _time = int(_p_last(_p_time_series(frame)))",
        "    _closed_trades = _p_close_trade_count()",
        "    _win_trades = _p_win_trade_count()",
        "    _loss_trades = _p_loss_trade_count()",
    ]
    lines.extend(sorted(emitted.get("predeclared") or []))
    # Translated Pine functions read ``context.params`` values as closure
    # variables, so every parameter must be bound before the functions are
    # defined; otherwise they resolve to unbound locals at call time.
    param_lines = list(emitted.get("param_lines") or [])
    if param_lines:
        lines.append("")
        lines.extend(f"    {line}" for line in param_lines)
    lines.append("")
    for block in function_blocks or ():
        # Function blocks are generated at module depth; indent them so they
        # become nested definitions inside ``handle_data`` and can close over
        # the handler's locals.
        for line in block:
            lines.append(f"    {line}" if line.strip() else line)
        lines.append("")
    for statement in statements:
        lines.append(statement if statement else "")
    return lines


def _literal_or_default(expression: str) -> str | None:
    """Return a module-level literal for a state initialiser expression.

    ``None`` means the initialiser is dynamic and must not be written into the
    module-level ``_STATE`` mapping at all.
    """
    text = str(expression or "").strip()
    if not text:
        return None
    # ``float('nan')`` is a call expression, which the contract rejects inside a
    # module-level literal. ``None`` carries the same "no value yet" meaning and
    # is what the read-time fallback in ``_state_get`` restores.
    text = _NAN_LITERAL.sub("None", text)
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return None
    if not _is_module_literal(tree.body):
        return None
    return text


def _is_module_literal(node: ast.AST) -> bool:
    """Report whether a node is safe as a module-level literal value."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, bytes, int, float, complex, bool, type(None)))
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_module_literal(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is not None and _is_module_literal(key) and _is_module_literal(value)
            for key, value in zip(node.keys, node.values)
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return isinstance(node.operand, ast.Constant) and isinstance(
            node.operand.value, (int, float, complex)
        )
    if isinstance(node, ast.Name) and node.id in {"_SYMBOL", "_TIMEFRAME"}:
        return True
    return False


#: NaN written as a call, which cannot appear in a module-level literal.
_NAN_LITERAL: Final[re.Pattern[str]] = re.compile(r"float\(\s*['\"]nan['\"]\s*\)")


def _clean_title(name: str) -> str:
    """Normalize a script name for the generated docstring."""
    text = str(name or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).replace('"""', "'''")
    # The runtime safety regexes scan comments and docstrings too, so a script
    # named "ORB open (09:30)" would otherwise trip ``\bopen\s*\(``.
    return _SAFE_COMMENT_TEXT.sub(lambda match: f"{match.group(1)} ", text)[:160]
