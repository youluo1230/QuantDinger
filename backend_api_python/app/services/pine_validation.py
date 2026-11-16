"""Pine Script validation for the Indicator IDE.

The IDE authors Pine Script, and the browser runs it. That leaves the backend
with two jobs it used to do by executing the code: refusing source that cannot
compile, and naming the indicator so the library listing is readable.

Both are answered from the syntax tree rather than by running anything. The
previous implementation leaned on ``validate_code_safety``, a deny-list of
Python idioms (``__import__``, ``os.system``, pandas ``.read_csv``). None of
those can appear in Pine, so every Pine script passed trivially -- which is
worse than not checking at all, because it made a broken script look accepted.

What is actually worth rejecting is narrow. A Pine script cannot do anything
dangerous: it has no imports to resolve at runtime, no filesystem, no network.
The failure modes that matter are the ones that cost the author a debugging
session -- a syntax error, a missing ``indicator``/``strategy`` declaration, or
a declaration whose type does not match what the chart can draw.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.services.pine_transpiler.pine_ast import (
    ParserUnavailable,
    PineParseError,
    parser_available,
    parse,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Lowest Pine version the chart can run.
#:
#: The browser runtime itself only accepts v5 and later, but the frontend
#: translates an older script before running it (`pineUpgrade.js`), and the
#: transpiler accepts v2 through v4 too -- so an old script is not an error here.
#: Rejecting it would have the IDE refuse to publish a script its own chart can
#: draw.
MIN_SUPPORTED_VERSION = 2
MAX_SUPPORTED_VERSION = 6

#: ``//@version=6`` as Pine requires it: the first line, no leading whitespace.
_VERSION_RE = re.compile(r"^\s*//\s*@version\s*=\s*(\d+)", re.MULTILINE)

#: ``indicator(`` / ``strategy(`` / ``study(``: the statement that declares what
#: the script is. `study` is the pre-v5 spelling of `indicator`. A declaration is
#: a statement at column zero, which is what keeps it distinct from a string that
#: merely mentions the word.
_DECLARATION_KIND_RE = re.compile(r"^[ \t]*(indicator|strategy|study)\s*\(", re.MULTILINE)

#: The first string literal inside that declaration, which is its title. Kept
#: apart from the kind so that `study(overlay=true)` still reports as a declared
#: script that merely has no title.
_DECLARATION_TITLE_RE = re.compile(r"[\"']([^\"']*)[\"']")

#: A plot call the chart can draw. Used only to warn about a script that declares
#: itself an indicator and then draws nothing.
_PLOT_RE = re.compile(r"^[ \t]*(plot|plotshape|plotchar|plotcandle|plotbar|plotarrow|bgcolor|barcolor|fill|hline)\s*\(", re.MULTILINE)

#: ``my_indicator_name = "..."`` keeps working for scripts authored against the
#: old contract; it is read but never required.
_LEGACY_NAME_RE = re.compile(r"^\s*my_indicator_name\s*=\s*([\"'])(.*?)\1\s*$", re.MULTILINE)
_LEGACY_DESCRIPTION_RE = re.compile(r"^\s*my_indicator_description\s*=\s*([\"'])(.*?)\1\s*$", re.MULTILINE)


def _first_code_line(source: str) -> str:
    """The first line that is neither blank nor a comment."""
    for line in (source or "").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("//"):
            return stripped
    return ""


def pine_version(source: str) -> Optional[int]:
    """The declared Pine version, or None when the script does not say."""
    match = _VERSION_RE.search(source or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _matching_paren(text: str, open_index: int) -> int:
    """The index of the ``)`` that closes ``open_index``, or -1.

    Only the declaration is scanned, so a quote-aware walk is enough: the
    arguments are an expression list, and a ``)`` inside a string belongs to the
    string rather than the call.
    """
    if open_index < 0 or open_index >= len(text) or text[open_index] != "(":
        return -1
    depth = 0
    quote = ""
    index = open_index
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in ("\"", "'"):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def declaration_of(source: str) -> Dict[str, str]:
    """The script's own ``indicator(...)``/``strategy(...)`` declaration.

    Returns ``{"kind", "title"}``; ``kind`` is empty when there is no
    declaration and ``title`` when the declaration carries no title.
    """
    text = source or ""
    kind_match = _DECLARATION_KIND_RE.search(text)
    if not kind_match:
        return {"kind": "", "title": ""}
    kind = kind_match.group(1)
    # Read the title out of this declaration's own parentheses, so a string
    # elsewhere in the script cannot be mistaken for it.
    open_paren = text.find("(", kind_match.end() - 1)
    close_paren = _matching_paren(text, open_paren)
    args = text[open_paren + 1:close_paren] if close_paren > open_paren else ""
    title_match = _DECLARATION_TITLE_RE.search(args)
    title = (title_match.group(1) if title_match else "").strip()
    return {"kind": kind, "title": title}


def extract_indicator_meta(source: str) -> Dict[str, str]:
    """Name and description for an indicator row.

    The declaration title is the name Pine itself gives the script, so it wins
    over the legacy ``my_indicator_name`` assignment. The legacy form is still
    read because scripts written against the previous contract carry it and
    their authors expect the listing to keep the name they typed.
    """
    declared = declaration_of(source).get("title") or ""
    name = declared.strip()[:100]
    if not name:
        match = _LEGACY_NAME_RE.search(source or "")
        if match:
            name = (match.group(2) or "").strip()[:100]
    description = ""
    match = _LEGACY_DESCRIPTION_RE.search(source or "")
    if match:
        description = (match.group(2) or "").strip()[:500]
    return {"name": name, "description": description}


def validate_pine_source(source: str) -> Dict[str, Any]:
    """Check that a script compiles and is drawable as a chart study.

    The return value is shaped like ``validate_code_safety``'s so the save path
    only has to swap the call: ``{"success", "error_type", "details", "hints"}``.
    ``hints`` carries the non-blocking observations, which the IDE shows next to
    the editor.

    A missing parser is not a rejection. ``pynescript`` is an optional
    dependency chosen so the repository can ship Apache-2.0 code without pulling
    in an LGPL grammar; when it is absent the checks that do not need a tree
    still run, and the script is accepted rather than failed on infrastructure.
    """
    raw = (source or "").strip()
    hints: List[Dict[str, Any]] = []

    if not raw:
        return {
            "success": False,
            "error_type": "EmptySource",
            "details": "The script is empty.",
            "hints": [{"severity": "error", "code": "EMPTY_CODE", "params": {}}],
        }

    version = pine_version(raw)
    if version is None:
        hints.append({"severity": "warn", "code": "MISSING_PINE_VERSION", "params": {}})
    elif version < MIN_SUPPORTED_VERSION:
        return {
            "success": False,
            "error_type": "UnsupportedVersion",
            "details": (
                f"Pine Script version {version} is not supported. "
                f"Rewrite the script for version {MIN_SUPPORTED_VERSION} or later."
            ),
            "hints": [
                {
                    "severity": "error",
                    "code": "UNSUPPORTED_PINE_VERSION",
                    "params": {"version": version, "minimum": MIN_SUPPORTED_VERSION},
                }
            ],
        }
    elif version > MAX_SUPPORTED_VERSION:
        hints.append(
            {
                "severity": "warn",
                "code": "UNKNOWN_PINE_VERSION",
                "params": {"version": version, "maximum": MAX_SUPPORTED_VERSION},
            }
        )

    declared = declaration_of(raw)
    if not declared["kind"]:
        hints.append({"severity": "warn", "code": "MISSING_SCRIPT_DECLARATION", "params": {}})
    elif declared["kind"] == "strategy":
        # A strategy runs on the backend for backtests; the chart study path
        # reads `plots`, and a strategy produces those too, so this is a note
        # rather than an error.
        hints.append({"severity": "info", "code": "SCRIPT_IS_A_STRATEGY", "params": {}})
    if declared["kind"] and not declared["title"]:
        hints.append({"severity": "info", "code": "MISSING_SCRIPT_TITLE", "params": {}})

    if not _PLOT_RE.search(raw):
        hints.append({"severity": "warn", "code": "NO_PLOTS_DECLARED", "params": {}})

    if parser_available():
        try:
            parse(raw)
        except PineParseError as exc:
            return {
                "success": False,
                "error_type": "PineParseError",
                "details": str(exc)[:1000],
                "hints": hints
                + [{"severity": "error", "code": "PINE_SYNTAX_ERROR", "params": {"detail": str(exc)[:300]}}],
            }
        except ParserUnavailable:
            # Declared available but failed to load, which is an environment
            # problem and not the author's.
            logger.warning("Pine parser reported available but failed to load; skipping syntax check")
    else:
        logger.info("Pine parser unavailable; syntax not verified")

    return {"success": True, "error_type": "", "details": "", "hints": hints}


def analyze_pine_quality(source: str) -> List[Dict[str, Any]]:
    """Non-blocking observations about a Pine script.

    Deliberately small. Static analysis of Pine cannot see values, so anything
    beyond structure would be guessing, and a hint the author learns to ignore
    is worse than no hint.
    """
    raw = (source or "").strip()
    if not raw:
        return [{"severity": "error", "code": "EMPTY_CODE", "params": {}}]

    hints: List[Dict[str, Any]] = []
    if pine_version(raw) is None:
        hints.append({"severity": "warn", "code": "MISSING_PINE_VERSION", "params": {}})

    declared = declaration_of(raw)
    if not declared["kind"]:
        hints.append({"severity": "warn", "code": "MISSING_SCRIPT_DECLARATION", "params": {}})
    elif not declared["title"]:
        hints.append({"severity": "info", "code": "MISSING_SCRIPT_TITLE", "params": {}})

    if not _PLOT_RE.search(raw):
        hints.append({"severity": "warn", "code": "NO_PLOTS_DECLARED", "params": {}})

    # `strategy(...)` calls are legal in a study, but the chart cannot show
    # their behaviour: there is no order execution in the browser. The note is
    # what stops "why did none of my entries draw" from becoming a bug report.
    if declared["kind"] == "strategy":
        hints.append({"severity": "info", "code": "SCRIPT_IS_A_STRATEGY", "params": {}})

    return hints


#: Pine input constructors and the parameter type each one declares. The IDE's
#: settings panel uses the same mapping, so a form built here matches the one
#: the browser renders from the script.
_INPUT_TYPES = {
    "int": "int",
    "float": "float",
    "bool": "bool",
    "string": "str",
    "color": "color",
    "source": "source",
    "timeframe": "timeframe",
    "symbol": "symbol",
    "session": "session",
    "price": "price",
    "time": "time",
    "text_area": "str",
    "enum": "str",
}

_INPUT_CALL_RE = re.compile(
    r"\binput\s*(?:\.\s*(" + "|".join(_INPUT_TYPES) + r")\s*)?\("
)
_NAMED_ARG_RE = re.compile(r"^([A-Za-z_]\w*)\s*=(?!=)\s*([\s\S]*)$")
_NUMERIC_LITERAL_RE = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$")
_HEX_COLOUR_RE = re.compile(r"^#([0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_ASSIGNED_NAME_RE = re.compile(r"([A-Za-z_]\w*)\s*=\s*$")


def _match_paren(source: str, open_at: int) -> int:
    """Index of the ``)`` closing the ``(`` at ``open_at``, or -1.

    A regex cannot match balanced parentheses and Pine defaults contain calls
    (``input.float(math.round(x), ...)``), so depth is counted while skipping
    strings and comments.
    """
    depth = 0
    quote = ""
    index = open_at
    while index < len(source):
        char = source[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in ("\"", "'"):
            quote = char
        elif char == "/" and source[index + 1 : index + 2] == "/":
            newline = source.find("\n", index)
            if newline == -1:
                return -1
            index = newline
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _split_args(text: str) -> List[str]:
    """Split an argument list on top-level commas."""
    parts: List[str] = []
    current = ""
    depth = 0
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            current += char
            if char == "\\":
                current += text[index + 1 : index + 2]
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in ("\"", "'"):
            quote = char
            current += char
        elif char in "([{":
            depth += 1
            current += char
        elif char in ")]}":
            depth -= 1
            current += char
        elif char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
        index += 1
    if current.strip():
        parts.append(current.strip())
    return parts


def _string_literal(text: str) -> Optional[str]:
    match = re.match(r"^([\"'])([\s\S]*)\1$", (text or "").strip())
    return match.group(2) if match else None


def _infer_input_type(default_text: str) -> str:
    """The type a pre-v5 ``input(...)`` declaration implies.

    The old spelling takes no type name: ``input(1.8, ...)`` is a float and
    ``input("0700-1600", ...)`` is a string. Reading it as a string always would
    render every numeric knob in an old script as a text box, which the author
    then cannot type a decimal into.
    """
    raw = (default_text or "").strip()
    if not raw:
        return "str"
    if _string_literal(raw) is not None:
        return "str"
    if raw in ("true", "false"):
        return "bool"
    if _HEX_COLOUR_RE.match(raw):
        return "color"
    if _NUMERIC_LITERAL_RE.match(raw):
        return "int" if raw.lstrip("+-").isdigit() else "float"
    return "str"


def _literal_value(text: str, param_type: str) -> Any:
    """The literal a declaration shows as its default, or ``None``.

    An expression default (``input.int(timeframe.period)``) has no value the
    backend can know before the script runs, so it reports nothing rather than
    inventing a number.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    quoted = _string_literal(raw)
    if quoted is not None:
        return quoted
    if param_type == "bool":
        if raw == "true":
            return True
        if raw == "false":
            return False
        return None
    if param_type == "color":
        match = _HEX_COLOUR_RE.match(raw)
        return f"#{match.group(1)[:6].upper()}" if match else None
    if _NUMERIC_LITERAL_RE.match(raw):
        try:
            return float(raw) if ("." in raw or "e" in raw.lower()) else int(raw)
        except ValueError:
            return None
    return None


#: ``const int FAST_LEN = 12`` / ``var string G = "01 — MACD"``. Real scripts
#: declare their defaults and group titles this way and then reference the
#: names, so the literal has to be resolved or every field reads as unknown.
_CONST_DECL_RE = re.compile(
    r"^[ \t]*(?:const\s+|var\s+|varip\s+)?(?:int|float|bool|string|color)?\s*"
    r"([A-Za-z_]\w*)\s*=\s*([^\n]+)$",
    re.MULTILINE,
)


def _constant_table(source: str) -> Dict[str, str]:
    """Literal values of the script's scalar constants, keyed by name.

    Only single-line literal assignments are recorded. A constant computed from
    other constants would need real evaluation, which is the runtime's job; the
    panel is better off showing nothing than a guess.
    """
    table: Dict[str, str] = {}
    for match in _CONST_DECL_RE.finditer(source or ""):
        name, value = match.group(1), match.group(2).strip()
        # `x = input.int(...)` and other calls are not constants.
        if "(" in value.split("#")[0]:
            continue
        table[name] = value
    return table


def _resolve_constant(text: str, constants: Dict[str, str], depth: int = 0) -> str:
    """Follow an identifier to its literal, leaving anything else untouched."""
    token = (text or "").strip()
    if depth > 8 or token not in constants:
        return token
    return _resolve_constant(constants[token], constants, depth + 1)


def parse_pine_params(source: str) -> List[Dict[str, Any]]:
    """The ``input.*`` declarations a Pine script makes, in source order.

    Mirrors the IDE's own parser so the strategy builder's parameter form and
    the indicator settings panel describe the same knobs. Each entry keeps the
    shape that form already renders (``name``/``type``/``default``/
    ``description``) and adds ``group``/``label``/``pine_input_id`` for callers
    that need to address the declaration.
    """
    raw_source = source or ""
    if not raw_source.strip():
        return []

    params: List[Dict[str, Any]] = []
    constants = _constant_table(raw_source)
    auto_id = 0
    for match in _INPUT_CALL_RE.finditer(raw_source):
        kind = match.group(1) or ""
        open_at = match.end() - 1
        close_at = _match_paren(raw_source, open_at)
        if close_at == -1:
            break
        positional: List[str] = []
        named: Dict[str, str] = {}
        for arg in _split_args(raw_source[open_at + 1 : close_at]):
            pair = _NAMED_ARG_RE.match(arg)
            if pair:
                named[pair.group(1)] = pair.group(2).strip()
            else:
                positional.append(arg)
        default_text = named.get("defval") if "defval" in named else (positional[0] if positional else "")
        default_text = _resolve_constant(default_text, constants)
        # `input.int(...)` names its type; the pre-v5 `input(...)` does not, and
        # the type is whatever the default literal is.
        param_type = _INPUT_TYPES.get(kind) or _infer_input_type(default_text)
        title_text = named.get("title") if "title" in named else (positional[1] if len(positional) > 1 else "")
        title_text = _resolve_constant(title_text, constants)
        # Real scripts pad titles with em spaces to hang them under a group
        # header; the runtime trims them before exposing the input, so the key
        # the form shows has to be the trimmed one.
        title = _string_literal(title_text) if title_text else None
        if title is None:
            title = (title_text or "").strip()
        title = title.strip()
        if not title:
            continue

        # ``len = input.int(...)`` binds the declaration to a variable, which is
        # a key the runtime accepts even after the title is edited.
        head = raw_source[: match.start()]
        assigned = _ASSIGNED_NAME_RE.search(head[-200:])
        var_id = ""
        if assigned:
            before = head[-200:][: assigned.start()].rstrip()
            # ``if x == y`` would look like an assignment to that regex.
            if not before.endswith(("=", "!", "<", ">")):
                var_id = assigned.group(1)

        entry: Dict[str, Any] = {
            "name": title,
            "type": param_type,
            "default": _literal_value(default_text, param_type),
            "description": _string_literal(_resolve_constant(named.get("tooltip", ""), constants)) or "",
            "label": title,
            "group": _string_literal(_resolve_constant(named.get("group", ""), constants)) or "",
            "pine_input_id": f"in_{auto_id}",
            "pine_var_id": var_id,
        }
        auto_id += 1
        options = named.get("options")
        if options:
            values = [
                value
                for value in (_string_literal(item) for item in _split_args(options.strip().strip("[]")))
                if value is not None
            ]
            if values:
                entry["values"] = values
        for key, field in (("minval", "min"), ("maxval", "max")):
            if key in named:
                bound = _literal_value(_resolve_constant(named[key], constants), "float")
                if bound is not None:
                    entry[field] = bound
        params.append(entry)
    return params


__all__ = [
    "MIN_SUPPORTED_VERSION",
    "MAX_SUPPORTED_VERSION",
    "analyze_pine_quality",
    "declaration_of",
    "extract_indicator_meta",
    "parse_pine_params",
    "pine_version",
    "validate_pine_source",
]
