"""Lazy access to the optional ``pynescript`` Pine Script parser.

``pynescript`` is LGPL-3.0-or-later while this repository is Apache-2.0, so the
dependency is optional: it is imported inside functions, and every consumer
degrades to a structured error when it is missing. Importing this module never
imports ``pynescript``.
"""

from __future__ import annotations

from typing import Any


PARSER_MODULE = "pynescript"
PARSER_REQUIREMENT = "pynescript==0.3.0"

# Node classes are resolved once on first use and cached. They are keyed by
# name because the parser's generated ASDL module is private to the library.
_NODE_CACHE: dict[str, type] = {}
_PARSER_READY: bool | None = None


class ParserUnavailable(RuntimeError):
    """Raised when the Pine parser cannot be imported or used."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PineParseError(ValueError):
    """Raised when the parser rejects a Pine Script source."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def parser_available() -> bool:
    """Report whether the Pine parser can be imported in this process."""
    global _PARSER_READY
    if _PARSER_READY is None:
        try:
            _ast_module()
        except ParserUnavailable:
            _PARSER_READY = False
        else:
            _PARSER_READY = True
    return bool(_PARSER_READY)


def _ast_module() -> Any:
    try:
        import pynescript.ast as pine_ast
    except ImportError as exc:  # pragma: no cover - exercised by the no-parser test
        raise ParserUnavailable(f"{PARSER_MODULE} is not installed: {exc}") from exc
    return pine_ast


def node_class(name: str) -> type:
    """Return a parser node class by name, raising when the parser is absent."""
    cached = _NODE_CACHE.get(name)
    if cached is not None:
        return cached
    module = _ast_module()
    for attribute in ("stmt", "expr"):
        namespace = getattr(module, attribute, None)
        candidate = getattr(namespace, name, None) if namespace is not None else None
        if isinstance(candidate, type) and candidate.__name__ == name:
            _NODE_CACHE[name] = candidate
            return candidate
    candidate = getattr(module, name, None)
    if isinstance(candidate, type):
        _NODE_CACHE[name] = candidate
        return candidate
    raise ParserUnavailable(f"pynescript node type is missing: {name}")


def parse(source: str) -> Any:
    """Parse Pine Script ``source`` into a ``Script`` node.

    Raises ``ParserUnavailable`` when the parser is missing and
    ``PineParseError`` when the source is not valid Pine Script.
    """
    module = _ast_module()
    text = str(source or "")
    if not text.strip():
        raise PineParseError("empty_source")
    try:
        return module.parse(text)
    except Exception as exc:  # The parser raises its own SyntaxError family.
        raise PineParseError(f"{type(exc).__name__}: {exc}".strip()) from exc


def node_name(node: Any) -> str:
    """Return the node class name without importing the parser."""
    return type(node).__name__ if node is not None else ""


def is_node(node: Any, name: str) -> bool:
    """Duck-typed node check that works without the parser being installed."""
    return node is not None and type(node).__name__ == name


def children(node: Any) -> list[Any]:
    """Return direct child nodes in field order, for walkers."""
    fields = getattr(node, "_fields", ()) or ()
    output: list[Any] = []
    for field in fields:
        value = getattr(node, field, None)
        if isinstance(value, list):
            output.extend(item for item in value if _is_node(item))
        elif _is_node(value):
            output.append(value)
    return output


def walk(node: Any) -> list[Any]:
    """Return every node in the subtree, parents before children."""
    output: list[Any] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if not _is_node(current):
            continue
        output.append(current)
        stack.extend(reversed(children(current)))
    return output


def _is_node(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool, list, tuple, dict)):
        return False
    return hasattr(value, "_fields") and hasattr(value, "lineno")


def line_of(node: Any) -> int:
    """Return the 1-based source line for a node, or 0 when unavailable."""
    try:
        return int(getattr(node, "lineno", 0) or 0)
    except (TypeError, ValueError):
        return 0
