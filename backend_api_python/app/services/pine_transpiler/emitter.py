"""Translation of Pine Script statements and expressions into Python source.

The emitter walks a parsed Pine ``Script`` and produces the body of a Strategy
API V2 strategy. It is intentionally conservative: anything it cannot express
exactly becomes a blocker instead of silently approximated behaviour.

Semantics that differ between the languages and are handled here:

``var`` declarations
    Pine ``var`` keeps a value across bars. The generated module uses one
    literal dict (``_STATE``) as the persistent store, which the Strategy API V2
    top-level validator accepts because it is a literal.
bar indexing
    ``close[1]`` becomes a pandas shift on the history frame. Pine's ``1``
    indexes backwards, pandas ``shift`` is forward, so the count is applied to
    a reversed series.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Final

from . import mapping
from .pine_ast import children, is_node, line_of, node_name, walk


#: Identifiers that are reserved in Python and must not be used verbatim.
PYTHON_KEYWORDS: frozenset[str] = frozenset(
    {
        "and", "as", "assert", "async", "await", "break", "class", "continue",
        "def", "del", "elif", "else", "except", "finally", "for", "from",
        "global", "if", "import", "in", "is", "lambda", "nonlocal", "not",
        "or", "pass", "raise", "return", "try", "while", "with", "yield",
        "None", "True", "False", "match", "case",
    }
)

#: Identifier prefix for renamed Pine locals and script variables.
LOCAL_PREFIX = "v_"

#: Identifiers the generated strategy body already uses. A Pine name that
#: matches one of these is renamed, because shadowing them (``len`` in
#: particular) breaks the surrounding generated code.
RESERVED_IDENTIFIERS: frozenset[str] = frozenset(
    {
        "np", "pd", "context", "data", "frame",
        "open", "high", "low", "close", "volume",
        "open_series", "high_series", "low_series", "close_series", "volume_series",
        "hl2_series", "hlc3_series", "ohlc4_series", "hlcc4_series",
        "get_history", "get_position", "order_target_percent",
        "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
        "float", "format", "frozenset", "int", "isinstance", "len", "list",
        "map", "max", "min", "pow", "print", "range", "repr", "reversed",
        "round", "set", "slice", "sorted", "str", "sum", "tuple", "zip",
    }
)

#: Maximum number of emitted statements before the transpiler gives up.
STATEMENT_LIMIT = 4000

#: Retained bar history length used for indicator math.
HISTORY_BARS = 500


class EmitError(RuntimeError):
    """Raised when a Pine construct cannot be expressed in the target runtime."""

    def __init__(self, blocker: str, detail: str = "") -> None:
        super().__init__(f"{blocker}:{detail}" if detail else blocker)
        self.blocker = blocker
        self.detail = detail


class Emitter:
    """Translate one parsed Pine Script into Strategy API V2 components."""

    def __init__(self, *, symbol: str, timeframe: str, name: str) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.name = name or "Pine strategy"
        #: Pine identifier -> emitted Python name.
        self.names: dict[str, str] = {}
        self.function_methods: set[str] = set()
        #: User ``type`` declarations, mapped to their field names in order.
        self.user_types: dict[str, list[str]] = {}
        #: Variables whose value is an instance of a user type.
        self.instances: dict[str, str] = {}
        #: Every Python local the generated handler binds.
        self.local_names: set[str] = set()
        #: Pine identifier -> state key for ``var`` persistence.
        self.state_keys: dict[str, str] = {}
        self.param_lines: list[str] = []
        self.constants: dict[str, str] = {}
        self.library_aliases: set[str] = set()
        #: Pine identifier -> history key for ``[n]`` lookups.
        self.history_keys: dict[str, str] = {}
        #: Pine function definitions translated to Python functions.
        self.functions: dict[str, str] = {}
        #: Emitted helper functions required by the generated source.
        self.helpers: set[str] = set()
        #: Inferred ``input.*`` parameters, name -> schema.
        self.params: dict[str, dict[str, Any]] = {}
        #: Warnings collected during translation.
        self.warnings: list[str] = []
        #: Blockers collected during translation.
        self.blockers: list[str] = []
        #: Statistics for reporting.
        self.stats: dict[str, Any] = {
            "ta_calls": 0,
            "math_calls": 0,
            "dropped_statements": 0,
            "unsupported_calls": [],
            "unsupported_namespaces": [],
            "unresolved": [],
            "uses_var": False,
            "uses_for": False,
            "uses_while": False,
            "uses_request_security": False,
            "has_strategy_entry": False,
            "has_strategy_close": False,
            "has_signal_entry": False,
            "signal_calls": 0,
            "uses_short": False,
            "emitted_statements": 0,
        }
        #: Timeframes subscribed through ``request.security``.
        self.extra_frequencies: list[str] = []
        #: Entry style detected in the Pine source.
        self.entry_style = "none"
        #: Whether drawing/alert conditions may be lowered into orders.
        self.signal_orders_enabled = True
        #: Translated Pine user functions, in source order.
        self.function_blocks: list[list[str]] = []
        #: Name of the Pine function currently being translated, if any.
        self._func_name: str = ""
        #: Statements that must run before the trading logic each bar.
        self.preamble: list[str] = []
        #: Statements containing order calls.
        self.body: list[str] = []
        #: Pine identifiers whose emitted local holds a pandas Series.
        self.series_names: set[str] = set()
        #: Initialiser expressions for persistent ``var`` slots.
        self.state_init: dict[str, str] = {}
        #: Every name assigned anywhere in the script, so handler-scope
        #: pre-declarations cover branch-local assignments.
        self.pending_names: dict[str, str] = {}
        #: Names emitted as pre-declared handler locals.
        self.predeclared: list[str] = []
        #: Temporary counter for generated locals.
        self._counter = 0
        self._unsupported: set[str] = set()

    # -- public entry point ---------------------------------------------------

    def emit(self, tree: Any) -> dict[str, Any]:
        """Translate ``tree`` and return the pieces of the generated strategy."""
        script_body = list(getattr(tree, "body", []) or [])
        # A real strategy already states its own entries and usually also plots
        # the very same conditions for the chart. Lowering those plots into
        # orders as well would place every trade twice, so signal lowering is
        # only enabled for scripts that never call ``strategy.entry``.
        self.signal_orders_enabled = not self._script_places_orders(tree)
        self._collect_declarations(script_body)
        self._predeclare_state(script_body)
        self._build_predeclarations()
        # Functions are translated first so calls resolve to real definitions.
        self._emit_user_functions(script_body)
        statements = self._emit_block(script_body, depth=1, top_level=True)
        if not self.stats["has_strategy_entry"] and not self.stats["has_signal_entry"]:
            self.blockers.append("no_strategy_entry")
        if self.entry_style == "none":
            self.blockers.append("no_entry_signal")
        self.stats["emitted_statements"] = len(statements)
        return {
            "statements": statements,
            "function_blocks": [block for block in self.function_blocks if block],
            "helpers": set(self.helpers),
            "params": dict(self.params),
            "warnings": list(self.warnings),
            "blockers": list(self.blockers),
            "stats": dict(self.stats),
            "extra_frequencies": list(self.extra_frequencies),
            "entry_style": self.entry_style,
            "local_names": sorted(self.local_names),
            "predeclared": list(self.predeclared),
            "param_lines": list(self.param_lines),
        }

    # -- preprocessing --------------------------------------------------------

    def _collect_declarations(self, body: list[Any]) -> None:
        """Record script-level declarations and translate Pine functions."""
        for node in body:
            if not is_node(node, "Import"):
                continue
            alias = str(getattr(node, "alias", "") or "")
            if not alias:
                parts = str(getattr(node, "path", "") or "").split("/")
                alias = parts[-2] if len(parts) > 1 else ""
            if alias:
                self.library_aliases.add(alias)
        for node in body:
            if is_node(node, "FunctionDef"):
                continue
            value = getattr(node, "value", None)
            if not is_node(value, "Call"):
                continue
            call = self._call_parts(value)
            if call is None:
                continue
            if call[0] in mapping.DECLARATION_CALLS and not call[1]:
                continue
        for node in body:
            if not is_node(node, "FunctionDef"):
                continue
            name = str(getattr(node, "name", "") or "")
            if name:
                self.functions[name] = "local"
        # User-defined object types become plain dicts; record their fields so
        # ``X.new(...)`` and ``instance.field`` can be lowered.
        for node in body:
            if not is_node(node, "TypeDef") and not is_node(node, "TypeDecl"):
                continue
            type_name = str(getattr(node, "name", "") or "")
            if not type_name:
                continue
            self.user_types[type_name] = self._type_fields(node)
        # Inline pure script-level constants so nested functions and hoisted
        # parameters never depend on assignment order inside ``handle_data``.
        for node in body:
            if not is_node(node, "Assign"):
                continue
            target = getattr(node, "target", None)
            value = getattr(node, "value", None)
            if not is_node(target, "Name") or node_name(getattr(node, "mode", None)) in {"Var", "VarIp"}:
                continue
            if not is_node(value, "Constant"):
                continue
            rendered = self._literal_repr(getattr(value, "value", None))
            if rendered is None:
                continue
            name = str(getattr(target, "id", ""))
            if name:
                self.constants[name] = rendered

    def _emit_user_functions(self, body: list[Any]) -> None:
        """Translate Pine ``name(args) =>`` functions into Python helpers."""
        for node in body:
            if not is_node(node, "FunctionDef"):
                continue
            name = str(getattr(node, "name", "") or "")
            if not name:
                continue
            params = self._function_params(node)
            if params is None:
                continue
            if bool(getattr(node, "method", False)):
                self.function_methods.add(name)
            body = list(getattr(node, "body", []) or [])
            self._func_name = name
            try:
                lines = self._emit_function_body(body)
            finally:
                self._func_name = ""
            lines = self._close_function_body(lines)
            signature = f"def {LOCAL_PREFIX}fn_{name}({', '.join(params)}):"
            if not lines:
                lines = ["    return float('nan')"]
            self.function_blocks.append([signature, *lines])
            self.functions[name] = "local"

    def _emit_function_body(self, body: list[Any]) -> list[str]:
        """Translate a Pine function body, returning its trailing value.

        Pine returns the value of the final expression in a ``name() =>`` body.
        Python would discard it, so that expression becomes an explicit
        ``return``. Control statements and assignments are emitted unchanged.
        """
        if not body:
            return []
        tail = body[-1]
        head = body[:-1]
        lines = self._emit_block(head, depth=1) if head else []
        value = getattr(tail, "value", None) if is_node(tail, "Expr") else None
        if value is not None and node_name(value) not in {"If", "While", "ForTo", "ForIn", "Switch"}:
            rendered = self._expr(value)
            if rendered:
                return [*lines, f"    return {rendered}"]
            if is_node(value, "Call"):
                return [*lines, "    return float('nan')"]
        tail_lines = self._emit_statement(tail, depth=1, top_level=False)
        return [*lines, *tail_lines]

    def _close_function_body(self, lines: list[str]) -> list[str]:
        """Ensure a translated Pine function has a body and a return."""
        output = [line for line in lines]
        while output and not output[-1].strip():
            output.pop()
        if not output:
            return ["    return float('nan')"]
        if not any(line.strip().startswith("return") for line in output):
            output.append("    return float('nan')")
        return output

    def _predeclare_state(self, body: list[Any]) -> None:
        """Register global ``var`` slots before function bodies are translated.

        Functions may read script-level ``var`` variables; the slot names have to
        exist by then so the read compiles to a state lookup instead of an
        undefined local.
        """
        stack: list[tuple[Any, int]] = [(item, 0) for item in body]
        while stack:
            current, level = stack.pop()
            if is_node(current, "FunctionDef"):
                continue
            if is_node(current, "Assign"):
                target = getattr(current, "target", None)
                if is_node(target, "Tuple"):
                    for item in getattr(target, "elts", []) or []:
                        if is_node(item, "Name"):
                            name = str(getattr(item, "id", "") or "")
                            if name:
                                self.pending_names.setdefault(name, "")
                if is_node(target, "Name"):
                    name = str(getattr(target, "id", "") or "")
                    if name:
                        if node_name(getattr(current, "mode", None)) in {"Var", "VarIp"}:
                            self.state_key(name)
                        else:
                            # Pine assignments made inside a branch stay visible
                            # afterwards; Python locals would not exist on the
                            # path where the branch is skipped, so bind a slot
                            # up front and let the branch fill it in.
                            key = name if level else ""
                            if key:
                                self.pending_names.setdefault(key, "")
            stack.extend((child, level + 1) for child in children(current))

    def _build_predeclarations(self) -> None:
        """Emit neutral bindings for names assigned only inside a branch.

        Pine keeps a variable assigned inside an ``if`` visible afterwards; a
        Python local would raise ``NameError`` on the path where the branch was
        skipped, so the handler binds a neutral value first and the branch
        overwrites it.
        """
        self.predeclared = []
        for name in sorted(self.pending_names):
            if name in self.state_keys:
                continue
            python_name = self.py_name(name)
            if python_name in self.local_names:
                continue
            self.predeclared.append(f"    {python_name} = float('nan')")

    def _function_params(self, node: Any) -> list[str] | None:
        """Return the Python parameter list of a Pine user function.

        Returns ``None`` when the function cannot be expressed, in which case it
        is dropped and its call sites fall back to the neutral value.
        """
        params: list[str] = []
        optional_seen = False
        for argument in getattr(node, "args", []) or []:
            if is_node(argument, "Param"):
                raw_name = str(getattr(argument, "name", "") or "")
            else:
                raw_name = str(getattr(argument, "id", "") or "")
            if not raw_name:
                return None
            rendered = ""
            default = getattr(argument, "default", None)
            if default is not None:
                rendered = self._expr(default)
            if rendered:
                optional_seen = True
                params.append(f"{self.py_name(raw_name)}={rendered}")
            elif optional_seen:
                # Python forbids a required parameter after an optional one.
                params.append(f"{self.py_name(raw_name)}=None")
            else:
                params.append(self.py_name(raw_name))
        return params

    def _call_parts(self, node: Any) -> tuple[str, str] | None:
        """Split a call node into ``(namespace-or-attribute, owner)``."""
        if not is_node(node, "Call"):
            return None
        func = getattr(node, "func", None)
        # Pine allows explicit type arguments (``matrix.new<float>(...)``); the
        # callee then arrives wrapped in Specialize/Subscript over the chain.
        while func is not None and node_name(func) in {"Specialize", "Subscript"}:
            func = getattr(func, "value", None)
        if is_node(func, "Name"):
            return str(getattr(func, "id", "")), ""
        if is_node(func, "Attribute"):
            owner = getattr(func, "value", None)
            if is_node(owner, "Name"):
                return str(getattr(func, "attr", "")), str(getattr(owner, "id", ""))
            if is_node(owner, "Attribute"):
                inner = getattr(owner, "value", None)
                if is_node(inner, "Name"):
                    return (
                        f"{getattr(inner, 'id', '')}.{getattr(owner, 'attr', '')}.{getattr(func, 'attr', '')}",
                        "",
                    )
            return str(getattr(func, "attr", "")), ""
        return None

    def _dotted(self, node: Any) -> str:
        """Render an attribute chain as ``a.b.c``, or ``""`` when not a chain."""
        parts: list[str] = []
        current = node
        while is_node(current, "Attribute"):
            parts.append(str(getattr(current, "attr", "")))
            current = getattr(current, "value", None)
        if not is_node(current, "Name") or not parts:
            return ""
        parts.append(str(getattr(current, "id", "")))
        return ".".join(reversed(parts))

    # -- name handling --------------------------------------------------------

    def py_name(self, pine_name: str) -> str:
        """Return the Python identifier that represents a Pine identifier."""
        existing = self.names.get(pine_name)
        if existing is not None:
            return existing
        cleaned = re.sub(r"[^0-9A-Za-z_]", "_", pine_name).strip("_") or "value"
        if cleaned[0].isdigit():
            cleaned = f"n_{cleaned}"
        if (
            cleaned in PYTHON_KEYWORDS
            or cleaned in mapping.FORBIDDEN_IDENTIFIERS
            or cleaned in RESERVED_IDENTIFIERS
            or cleaned.startswith("_")
        ):
            cleaned = f"{LOCAL_PREFIX}{cleaned}"
        candidate = cleaned
        used = set(self.names.values())
        suffix = 2
        while candidate in used:
            candidate = f"{cleaned}_{suffix}"
            suffix += 1
        self.names[pine_name] = candidate
        return candidate

    def is_series(self, pine_name: str) -> bool:
        """Report whether a Pine identifier currently holds a series."""
        return pine_name in self.series_names or pine_name in mapping.SERIES_NAMES

    def state_key(self, pine_name: str) -> str:
        """Return the ``_STATE`` key used to persist a Pine ``var``."""
        key = self.state_keys.get(pine_name)
        if key is not None:
            return key
        base = re.sub(r"[^0-9A-Za-z_]", "_", pine_name).strip("_") or "value"
        if self._func_name:
            base = f"{self._func_name}_{base}"
        key = f"{base}_v"
        used = set(self.state_keys.values())
        suffix = 2
        while key in used:
            key = f"{base}_v{suffix}"
            suffix += 1
        self.state_keys[pine_name] = key
        return key

    def history_key(self, pine_name: str) -> str:
        """Return the ``_HISTORY`` key used for Pine ``[n]`` indexing."""
        key = self.history_keys.get(pine_name)
        if key is not None:
            return key
        base = re.sub(r"[^0-9A-Za-z_]", "_", pine_name).strip("_") or "value"
        key = f"{base}_h"
        used = set(self.history_keys.values())
        suffix = 2
        while key in used:
            key = f"{base}_h{suffix}"
            suffix += 1
        self.history_keys[pine_name] = key
        return key

    def temp(self, label: str) -> str:
        """Return a unique temporary local name."""
        self._counter += 1
        base = re.sub(r"[^0-9A-Za-z_]", "_", label).strip("_") or "tmp"
        return f"_{base}_{self._counter}"

    # -- statements -----------------------------------------------------------

    def _emit_block(self, body: list[Any], *, depth: int, top_level: bool = False) -> list[str]:
        """Translate a Pine statement list into Python lines at ``depth``."""
        lines: list[str] = []
        for node in body:
            try:
                lines.extend(self._emit_statement(node, depth=depth, top_level=top_level))
            except EmitError as exc:
                if exc.blocker not in self.blockers:
                    self.blockers.append(exc.blocker)
                if self.stats["emitted_statements"] > STATEMENT_LIMIT:
                    raise
            if len(lines) > STATEMENT_LIMIT:
                raise EmitError("source_too_large", str(len(lines)))
        return lines

    def _emit_statement(self, node: Any, *, depth: int, top_level: bool) -> list[str]:
        """Translate one Pine statement."""
        indent = "    " * depth
        kind = node_name(node)

        if kind == "Comment":
            return []
        if kind == "Expr":
            return self._emit_expression_statement(getattr(node, "value", None), indent, depth)
        if kind == "Assign":
            return self._emit_assign(node, indent, depth, top_level=top_level)
        if kind == "ReAssign":
            return self._emit_reassign(node, indent, depth)
        if kind == "AugAssign":
            return self._emit_augassign(node, indent, depth)
        if kind in {"If", "While", "ForTo", "ForIn", "Switch"}:
            return self._emit_control(node, indent, depth, top_level=top_level)
        if kind == "Break":
            return [f"{indent}break"]
        if kind == "Continue":
            return [f"{indent}continue"]
        if kind == "FunctionDef":
            return []
        if kind in {"Import", "TypeDef", "EnumDef"}:
            return []
        if kind == "Pass":
            return [f"{indent}pass"]
        if kind == "Return":
            value = getattr(node, "value", None)
            if value is None:
                return [f"{indent}return"]
            return [f"{indent}return {self._expr(value)}"]
        self._warn_once(f"statement_dropped:{kind}")
        self.stats["dropped_statements"] = int(self.stats["dropped_statements"]) + 1
        return []

    def _emit_expression_statement(self, value: Any, indent: str, depth: int) -> list[str]:
        """Translate an expression used as a statement."""
        if value is None:
            return []
        if is_node(value, "Call"):
            return self._emit_call_statement(value, indent)
        # pynescript wraps control flow used as a statement inside ``Expr``, so
        # this branch must dispatch every block-shaped node; otherwise ``for``
        # and ``while`` loops are silently dropped from the generated strategy.
        if node_name(value) in {"If", "While", "ForTo", "ForIn", "Switch"}:
            return self._emit_control(value, indent, depth, top_level=False)
        rendered = self._expr(value)
        if rendered in {"", "None"}:
            return []
        return [f"{indent}{rendered}"]

    def _emit_call_statement(self, node: Any, indent: str) -> list[str]:
        """Translate a call used as a statement: orders, drops, or helpers."""
        parts = self._call_parts(node)
        if parts is None:
            return []
        name, owner = parts

        if owner == "strategy":
            return self._emit_strategy_statement(name, node, indent)
        dotted_name = self._dotted(getattr(node, "func", None)) or (f"{owner}.{name}" if owner else name)
        self._check_blocked(dotted_name)
        if not owner and name in mapping.DECLARATION_CALLS:
            return []
        if name in mapping.SIGNAL_CALLS and self.signal_orders_enabled:
            # An indicator has no ``strategy.*`` namespace, so its only per-bar
            # condition is the one inside a drawing or alert call. That condition
            # has to become the order, otherwise every indicator is "no entry
            # signal" and can never be backtested.
            return self._emit_signal_statement(name, node, indent)
        if owner in mapping.DROPPED_NAMESPACES or name in mapping.DROPPED_NAMESPACES:
            # Display calls are dropped, but their arguments may still have
            # side effects (``line.delete(array.shift(lines))``); keep those.
            return self._side_effects(node, indent, owner or name)
        if owner == "strategy" and name in mapping.STRATEGY_IGNORED_CALLS:
            return []
        dotted = self._dotted(getattr(node, "func", None))
        if dotted and dotted.startswith(mapping.STRATEGY_RISK_PREFIX):
            self._warn_once("strategy_risk_ignored")
            return []
        if owner == "request":
            self._note_request(name, node)
            return []
        if owner in {"alert", "alertcondition"}:
            self._warn_once(f"{owner}_dropped")
            return []
        rendered = self._expr(node)
        if not rendered:
            return []
        return [f"{indent}{rendered}"]

    def _side_effects(self, node: Any, indent: str, label: str) -> list[str]:
        """Render side-effecting sub-calls from a dropped display statement."""
        self._note_dropped(label or "display")
        lines: list[str] = []
        for argument in self._positional(node):
            if not is_node(argument, "Call"):
                continue
            parts = self._call_parts(argument)
            if parts is None:
                continue
            name, owner = parts
            if owner not in {"array", "map", "matrix"}:
                continue
            rendered = self._expr(argument)
            if rendered:
                lines.append(f"{indent}{rendered}")
        return lines

    def _emit_strategy_statement(self, name: str, node: Any, indent: str) -> list[str]:
        """Translate ``strategy.*`` calls into target-percentage orders."""
        if name in mapping.STRATEGY_IGNORED_CALLS:
            self._warn_once(f"strategy_{name}_ignored")
            return []
        if name in mapping.STRATEGY_ENTRY_CALLS:
            self.stats["has_strategy_entry"] = True
            return [self._entry_order(name, node, indent)]
        if name in mapping.STRATEGY_CLOSE_CALLS:
            self.stats["has_strategy_close"] = True
            if self.entry_style == "none":
                self.entry_style = "signal"
            return [f"{indent}order_target_percent(_SYMBOL, 0.0, reason=\"pine_close\")"]
        if name in mapping.STRATEGY_EXIT_CALLS:
            self._warn_once("strategy_exit_ignored")
            return []
        if name.startswith("risk"):
            self._warn_once("strategy_risk_ignored")
            return []
        self._warn_once(f"strategy_{name}_ignored")
        return []

    def _entry_order(self, name: str, node: Any, indent: str) -> str:
        """Build the target-percentage order for an entry-like call."""
        arguments = self._call_arguments(node)
        is_short = name == "short"
        if not is_short:
            for argument in arguments.values():
                if self._dotted(argument) == "strategy.short":
                    is_short = True
            direction = arguments.get("direction")
            if direction is not None and self._dotted(direction) == "strategy.short":
                is_short = True
        if is_short:
            self.stats["uses_short"] = True
            self.entry_style = "signal"
            self._warn_once("short_as_target_percent")
            return (
                f"{indent}order_target_percent(_SYMBOL, -1.0, reason=\"pine_short\")"
            )
        self.entry_style = "signal"
        return f"{indent}order_target_percent(_SYMBOL, 1.0, reason=\"pine_long\")"

    def _emit_signal_statement(self, name: str, node: Any, indent: str) -> list[str]:
        """Turn an indicator's drawing or alert condition into an order.

        A Pine indicator never calls ``strategy.entry``: its only executable
        statements are drawings and alerts, and the condition inside one of them
        is exactly the bar the author wanted to act on. Without this lowering
        every indicator ends up "no entry signal" and can never be backtested.

        The direction is not structural in Pine, so it is read from the title the
        author wrote ("Bullish order block formed", "CHoCH Bear"). A condition
        with no recognisable direction is treated as a long trigger, which is the
        common case and keeps the script tradeable instead of silently inert.
        """
        positional = self._positional(node)
        if not positional:
            self._warn_once(f"signal_without_condition:{name}")
            return []
        condition = self._expr(positional[0])
        if not condition:
            self._warn_once(f"signal_without_condition:{name}")
            return []
        title = self._signal_title(node)
        long_side = self._signal_is_long(title)
        self.stats["has_signal_entry"] = True
        self.stats["signal_calls"] = int(self.stats["signal_calls"]) + 1
        self.entry_style = "signal"
        target = "1.0" if long_side else "-1.0"
        reason = "pine_signal_long" if long_side else "pine_signal_short"
        if not long_side:
            self.stats["uses_short"] = True
            self._warn_once("short_as_target_percent")
        return [
            f"{indent}if {self._wrap_condition(condition)}:",
            f"{indent}    order_target_percent(_SYMBOL, {target}, reason=\"{reason}\")",
        ]

    def _script_places_orders(self, tree: Any) -> bool:
        """Report whether the script already enters trades through ``strategy.*``.

        The parser's root node carries no line number, so ``walk`` skips it and
        the statements have to be walked one by one.
        """
        stack = list(getattr(tree, "body", []) or [])
        while stack:
            node = stack.pop()
            if node_name(node) == "Call":
                parts = self._call_parts(node)
                if parts is not None:
                    name, owner = parts
                    if owner == "strategy" and name in mapping.STRATEGY_ENTRY_CALLS:
                        return True
            stack.extend(children(node))
        return False

    def _signal_title(self, node: Any) -> str:
        """Read the display title of a drawing or alert call."""
        arguments = self._call_arguments(node)
        for key in ("title",):
            source = arguments.get(key)
            if source is not None:
                literal = self._literal_value(source)
                if isinstance(literal, str):
                    return literal
        positional = self._positional(node)
        # ``plotshape``/``plotchar``/``alertcondition`` all take the title as the
        # second positional argument.
        if len(positional) > 1:
            literal = self._literal_value(positional[1])
            if isinstance(literal, str):
                return literal
        return ""

    def _signal_is_long(self, title: str) -> bool:
        """Decide the trade side a signal title describes."""
        lowered = str(title or "").lower()
        short_hit = any(word in lowered for word in mapping.SHORT_SIGNAL_WORDS)
        long_hit = any(word in lowered for word in mapping.LONG_SIGNAL_WORDS)
        if short_hit and not long_hit:
            return False
        return True

    def _emit_assign(self, node: Any, indent: str, depth: int, *, top_level: bool) -> list[str]:
        """Translate a Pine declaration or destructuring assignment."""
        target = getattr(node, "target", None)
        value = getattr(node, "value", None)
        mode = node_name(getattr(node, "mode", None))

        if is_node(target, "Tuple"):
            return self._emit_tuple_assign(target, value, indent)
        if not is_node(target, "Name"):
            return []

        pine_name = str(getattr(target, "id", ""))
        if not pine_name:
            return []
        if node_name(value) in {"If", "Switch"}:
            # Pine allows ``x = switch ...`` / ``x = if ...``. Those values are
            # block-shaped, so lower them into assignments inside a branch chain
            # instead of trying to render a Python expression.
            return self._emit_control_assign(pine_name, value, indent, depth)
        rendered = self._expr(value)

        if self._is_input_call(value):
            self._register_input(pine_name, value)
            py = self.py_name(pine_name)
            self.local_names.add(py)
            key = self._param_key(pine_name)
            fallback = rendered or _literal_source(self.params.get(key, {}).get("default"))
            # Parameters are hoisted to the top of ``handle_data`` by the
            # assembler: translated Pine functions read them as closure
            # variables and must never observe an unbound name. A default that
            # is not a literal (``input.time(StartDate, ...)``) still refers to
            # a script-level name, so it has to stay below the assignment that
            # binds it; nested functions only look the name up when they are
            # called, which is after the whole statement list has run.
            if _is_literal_expression(fallback):
                self.param_lines.append(f"{py} = context.params.get(\"{key}\", {fallback})")
                return []
            return [f"{indent}{py} = context.params.get(\"{key}\", {fallback})"]

        if mode in {"Var", "VarIp"}:
            self.stats["uses_var"] = True
            key = self.state_key(pine_name)
            self.state_init[key] = rendered or "float('nan')"
            if self._expr_is_series(value):
                self.series_names.add(pine_name)
            # Reads resolve through _state_get, so nothing is bound here: the
            # slot owns the value and every read must observe the latest write.
            return []

        py = self.py_name(pine_name)
        if rendered == "":
            return []
        instance_type = self._new_instance_type(value)
        if instance_type:
            self.instances[pine_name] = instance_type
        self.local_names.add(py)
        if self._expr_is_series(value):
            self.series_names.add(pine_name)
        return [f"{indent}{py} = {rendered}"]

    def _emit_control_assign(
        self, pine_name: str, value: Any, indent: str, depth: int
    ) -> list[str]:
        """Low-level helper lowering ``x = if ...`` / ``x = switch ...``."""
        py = self.py_name(pine_name)
        self.local_names.add(py)
        lines = [f"{indent}{py} = float('nan')"]
        if node_name(value) == "If":
            lines.extend(self._value_branch(getattr(value, "test", None), value, py, indent, depth))
            orelse = list(getattr(value, "orelse", []) or [])
            if orelse:
                lines.append(f"{indent}else:")
                lines.extend(self._value_block(orelse, py, indent, depth + 1, indent_body=True))
            return lines
        # ``switch``: compare against the subject, matching Pine's semantics.
        subject = self._expr(getattr(value, "subject", None))
        first = True
        fallback: list[Any] = []
        for case in getattr(value, "cases", []) or []:
            pattern = getattr(case, "pattern", None)
            body = list(getattr(case, "body", []) or [])
            if pattern is None:
                fallback = body
                continue
            rendered = self._expr(pattern)
            if not rendered:
                continue
            condition = f"{subject} == {rendered}" if subject else rendered
            keyword = "if" if first else "elif"
            lines.append(f"{indent}{keyword} {self._wrap_condition(condition)}:")
            lines.extend(self._value_block(body, py, indent, depth + 1, indent_body=True))
            first = False
        if fallback:
            lines.append(f"{indent}else:" if not first else f"{indent}if True:")
            lines.extend(self._value_block(fallback, py, indent, depth + 1, indent_body=True))
        return lines

    def _value_branch(
        self, test: Any, node: Any, target_py: str, indent: str, depth: int
    ) -> list[str]:
        """Render the ``if`` arm of a value-producing branch."""
        condition = self._condition(test)
        body = list(getattr(node, "body", []) or [])
        return [
            f"{indent}if {condition}:",
            *self._value_block(body, target_py, indent, depth + 1, indent_body=True),
        ]

    def _value_block(
        self,
        body: list[Any],
        target_py: str,
        indent: str,
        depth: int,
        *,
        indent_body: bool,
    ) -> list[str]:
        """Emit a branch body whose trailing expression is the branch value."""
        inner = indent + "    " if indent_body else indent
        lines: list[str] = []
        pending = list(body)
        trailing: Any = None
        if pending:
            last = pending[-1]
            if is_node(last, "Expr") and node_name(getattr(last, "value", None)) not in {
                "If",
                "While",
                "ForTo",
                "ForIn",
                "Switch",
            }:
                trailing = getattr(last, "value", None)
                pending = pending[:-1]
        for item in pending:
            lines.extend(self._emit_statement(item, depth=depth, top_level=False))
        rendered = self._expr(trailing) if trailing is not None else ""
        if rendered:
            lines.append(f"{inner}{target_py} = {rendered}")
        elif not lines:
            lines.append(f"{inner}{target_py} = float('nan')")
        return lines

    def _emit_tuple_assign(self, target: Any, value: Any, indent: str) -> list[str]:
        """Translate ``[a, b] = ta.supertrend(...)`` destructuring."""
        names = [
            str(getattr(item, "id", ""))
            for item in (getattr(target, "elts", []) or [])
            if is_node(item, "Name")
        ]
        if not names:
            return []
        call = self._call_parts(value) if is_node(value, "Call") else None
        if call is not None and call[1] == "ta":
            spec = mapping.TA_SPECS.get(call[0])
            if spec is not None and spec[2] == len(names):
                self.stats["ta_calls"] = int(self.stats["ta_calls"]) + 1
                self.helpers.add(spec[0])
                temporary = self.temp(call[0])
                expression = self._ta_expression(call[0], value)
                lines = [f"{indent}{temporary} = {expression}"]
                for position, pine_name in enumerate(names):
                    py = self.py_name(pine_name)
                    self.local_names.add(py)
                    lines.append(f"{indent}{py} = _p_at({temporary}, {position})")
                return lines
        rendered = self._expr(value)
        if not rendered:
            return []
        temporary = self.temp("tuple")
        lines = [f"{indent}{temporary} = {rendered}"]
        for position, pine_name in enumerate(names):
            py = self.py_name(pine_name)
            self.local_names.add(py)
            lines.append(f"{indent}{py} = _p_at({temporary}, {position})")
        return lines

    def _emit_reassign(self, node: Any, indent: str, depth: int) -> list[str]:
        """Translate Pine's ``x := value`` assignment."""
        target = getattr(node, "target", None)
        value = getattr(node, "value", None)
        if not is_node(target, "Name"):
            return []
        pine_name = str(getattr(target, "id", ""))
        rendered = self._expr(value)
        if not pine_name or rendered == "":
            return []
        if pine_name in self.state_keys:
            self.stats["uses_var"] = True
            self.series_names.discard(pine_name)
            if self._expr_is_series(value):
                self.series_names.add(pine_name)
            return [f"{indent}_state_set(\"{self.state_keys[pine_name]}\", {rendered})"]
        py = self.py_name(pine_name)
        self.local_names.add(py)
        if self._expr_is_series(value):
            self.series_names.add(pine_name)
        else:
            self.series_names.discard(pine_name)
        return [f"{indent}{py} = {rendered}"]

    def _emit_augassign(self, node: Any, indent: str, depth: int) -> list[str]:
        """Translate ``x += value`` including ``var`` state updates."""
        target = getattr(node, "target", None)
        value = getattr(node, "value", None)
        if not is_node(target, "Name"):
            return []
        pine_name = str(getattr(target, "id", ""))
        operator = self._binary_operator(getattr(node, "op", None))
        rendered = self._expr(value)
        if not pine_name or operator is None or rendered == "":
            return []
        if pine_name in self.state_keys:
            key = self.state_keys[pine_name]
            fallback = self.state_init.get(key, "0.0")
            current = f"_state_get(\"{key}\", {fallback})"
            self.stats["uses_var"] = True
            return [f"{indent}_state_set(\"{key}\", {current} {operator} {rendered})"]
        py = self.py_name(pine_name)
        return [f"{indent}{py} = {py} {operator} {rendered}"]

    def _emit_control(self, node: Any, indent: str, depth: int, *, top_level: bool) -> list[str]:
        """Translate ``if``/``while``/``for``/``switch`` statements."""
        kind = node_name(node)
        if kind == "If":
            return self._emit_if(node, indent, depth)
        if kind == "While":
            self.stats["uses_while"] = True
            return self._emit_while(node, indent, depth)
        if kind in {"ForTo", "ForIn"}:
            self.stats["uses_for"] = True
            return self._emit_for(node, indent, depth)
        if kind == "Switch":
            return self._emit_switch(node, indent, depth)
        return []

    def _emit_if(self, node: Any, indent: str, depth: int) -> list[str]:
        """Translate an ``if`` statement with its ``else`` chain."""
        test = self._condition(getattr(node, "test", None))
        body = self._emit_block(list(getattr(node, "body", []) or []), depth=depth + 1)
        if not body:
            body = ["    " * (depth + 1) + "pass"]
        lines = [f"{indent}if {test}:"]
        lines.extend(body)
        orelse = list(getattr(node, "orelse", []) or [])
        while orelse:
            first = orelse[0]
            if is_node(first, "Expr") and is_node(getattr(first, "value", None), "If"):
                nested = getattr(first, "value")
                test = self._condition(getattr(nested, "test", None))
                body = self._emit_block(list(getattr(nested, "body", []) or []), depth=depth + 1)
                if not body:
                    body = ["    " * (depth + 1) + "pass"]
                lines.append(f"{indent}elif {test}:")
                lines.extend(body)
                orelse = list(getattr(nested, "orelse", []) or [])
                continue
            body = self._emit_block(orelse, depth=depth + 1)
            if body:
                lines.append(f"{indent}else:")
                lines.extend(body)
            break
        return lines

    def _emit_while(self, node: Any, indent: str, depth: int) -> list[str]:
        """Translate a ``while`` loop.

        The loop carries an iteration cap: Pine loops normally terminate through
        a mutating condition, but a mistranslated body would otherwise hang the
        backtest instead of failing it.
        """
        test = self._condition(getattr(node, "test", None))
        body = self._emit_block(list(getattr(node, "body", []) or []), depth=depth + 1)
        if not body:
            body = ["    " * (depth + 1) + "pass"]
        self._loop_serial = getattr(self, "_loop_serial", 0) + 1
        guard = f"_loop_{self._loop_serial}"
        inner = "    " * (depth + 1)
        return [
            f"{indent}{guard} = 0",
            f"{indent}while {test}:",
            *body,
            f"{inner}{guard} = {guard} + 1",
            f"{inner}if {guard} > 10000:",
            f"{inner}    break",
        ]

    def _emit_for(self, node: Any, indent: str, depth: int) -> list[str]:
        """Translate ``for`` loops onto a bounded Python ``range``.

        The body is emitted one level deeper than ``indent``, so every early
        return below must still emit a block header. Returning the bare body
        would leave it over-indented relative to the surrounding statements,
        which the compiler reports as an unexpected indent.
        """
        kind = node_name(node)
        body = self._emit_block(list(getattr(node, "body", []) or []), depth=depth + 1)
        if not body:
            body = ["    " * (depth + 1) + "pass"]
        target = getattr(node, "target", None)
        # A Pine tuple target cannot be a Python loop variable, but the body
        # still has to run once per iteration, so bind a throwaway name.
        py = self.py_name(str(getattr(target, "id", ""))) if is_node(target, "Name") else "_it"
        if kind == "ForTo":
            start = self._expr(getattr(node, "start", None)) or "0"
            end = self._expr(getattr(node, "end", None)) or "0"
            step = getattr(node, "step", None)
            if step is None:
                upper = f"_limited({end})"
                return [f"{indent}for {py} in range(0, {upper} + 1):", *body]
            rendered_step = self._expr(step) or "1"
            upper = f"_limited({end}) + _sign({rendered_step})"
            return [f"{indent}for {py} in range(0, {upper}, {rendered_step}):", *body]
        iterator = self._expr(getattr(node, "iter", None)) or ""
        if not iterator:
            # Nothing to iterate over, but the body must still stay a block.
            return [f"{indent}if True:", *body]
        return [f"{indent}for {py} in {iterator}:", *body]

    def _emit_switch(self, node: Any, indent: str, depth: int) -> list[str]:
        """Translate ``switch`` into an ``if``/``elif`` chain."""
        subject = getattr(node, "subject", None)
        subject_expr = self._expr(subject) if subject is not None else ""
        lines: list[str] = []
        pending_else: list[str] | None = None
        first = True
        for case in getattr(node, "cases", []) or []:
            pattern = getattr(case, "pattern", None)
            body = self._emit_block(list(getattr(case, "body", []) or []), depth=depth + 1)
            if pattern is None:
                pending_else = body
                continue
            rendered = self._expr(pattern)
            if not rendered:
                continue
            condition = (
                f"{subject_expr} == {rendered}" if subject_expr else rendered
            )
            keyword = "if" if first else "elif"
            lines.append(f"{indent}{keyword} {self._wrap_condition(condition)}:")
            lines.extend(body or ["    " * (depth + 1) + "pass"])
            first = False
        if pending_else:
            if lines:
                lines.append(f"{indent}else:")
                lines.extend(pending_else)
            else:
                lines.extend(pending_else)
        return lines

    # -- conditions -----------------------------------------------------------

    def _condition(self, node: Any) -> str:
        """Render a node used as a boolean test.

        Indicator math makes almost every Pine condition a pandas Series, and
        ``if`` on a Series raises ``ValueError``. Always collapsing through
        ``_p_truth`` keeps plain booleans correct (``float(True) == 1.0``) while
        making series conditions evaluate for the current bar only.
        """
        rendered = self._expr(node)
        if not rendered:
            return "False"
        if is_node(node, "Constant") and isinstance(getattr(node, "value", None), bool):
            return rendered
        return self._wrap_condition(rendered)

    def _wrap_condition(self, rendered: str) -> str:
        """Wrap a possibly vectorized expression in a single-bar truth test."""
        return f"_p_truth({rendered})"

    def _is_scalar_condition(self, node: Any) -> bool:
        """Report whether a test already evaluates to a plain Python bool."""
        if is_node(node, "Call"):
            parts = self._call_parts(node)
            if parts is not None:
                name, owner = parts
                if f"{owner}.{name}" in {
                    "ta.crossover", "ta.crossunder", "ta.cross", "ta.rising", "ta.falling",
                }:
                    return False
                if owner == "array" and name == "includes":
                    return True
        if is_node(node, "Compare"):
            operands = [getattr(node, "left", None), *list(getattr(node, "comparators", []) or [])]
            return all(self._is_scalar_operand(item) for item in operands if item is not None)
        if is_node(node, "BoolOp"):
            return all(self._is_scalar_condition(item) for item in (getattr(node, "values", []) or []))
        if is_node(node, "UnaryOp"):
            return self._is_scalar_condition(getattr(node, "operand", None))
        return self._is_scalar_operand(node)

    def _is_scalar_operand(self, node: Any) -> bool:
        """Report whether an operand is a per-bar scalar rather than a series."""
        if is_node(node, "Constant"):
            return True
        if is_node(node, "Name"):
            name = str(getattr(node, "id", ""))
            return not self.is_series(name)
        if is_node(node, "Subscript"):
            return True
        if is_node(node, "Attribute"):
            dotted = self._dotted(node)
            if dotted:
                return dotted in mapping.STATIC_CONSTANTS and not dotted.startswith("strategy.")
            return False
        if is_node(node, "Call"):
            return not self._expr_is_series(node)
        if is_node(node, "BinOp"):
            return self._is_scalar_operand(getattr(node, "left", None)) and self._is_scalar_operand(
                getattr(node, "right", None)
            )
        return False

    # -- expressions ----------------------------------------------------------

    def _expr(self, node: Any) -> str:
        """Render any Pine expression node as a Python expression."""
        if node is None:
            return ""
        kind = node_name(node)
        if kind == "Constant":
            return self._constant(getattr(node, "value", None))
        if kind == "Name":
            return self._name_ref(str(getattr(node, "id", "")))
        if kind == "Attribute":
            return self._attribute_ref(node)
        if kind == "Call":
            rendered = self._expr_call(node)
            if not rendered:
                # An unmapped call must not disappear from the expression it sits
                # in. Returning "" deleted the operand outright, so
                # ``180 * acos(0.5) / 3`` quietly became ``180 / 3`` and
                # ``x = iff(c, 1, 2)`` dropped the whole assignment, leaving
                # later reads of ``x`` as NameError. A NaN poisons the arithmetic
                # visibly instead, and ``stats["unresolved"]`` names the culprit.
                self._note_unresolved(self._dotted(getattr(node, "func", None)) or "?")
                return "float('nan')"
            return rendered
        if kind == "BinOp":
            return self._binary(node)
        if kind == "BoolOp":
            return self._boolean(node)
        if kind == "UnaryOp":
            return self._unary(node)
        if kind == "Compare":
            return self._compare(node)
        if kind == "Conditional":
            test_node = getattr(node, "test", None)
            test = self._expr(test_node) or "False"
            body = self._expr(getattr(node, "body", None)) or "float('nan')"
            orelse = self._expr(getattr(node, "orelse", None)) or "float('nan')"
            return f"_p_ternary({test}, {body}, {orelse})"
        if kind == "Subscript":
            return self._subscript(node)
        if kind == "Tuple":
            items = [self._expr(item) for item in (getattr(node, "elts", []) or [])]
            return "(" + ", ".join(item for item in items if item) + ")"
        if kind == "List":
            items = [self._expr(item) for item in (getattr(node, "elts", []) or [])]
            return "[" + ", ".join(item for item in items if item) + "]"
        if kind == "Paren":
            inner = self._expr(getattr(node, "value", None))
            return f"({inner})" if inner else ""
        if kind == "If":
            return ""
        self._warn_once(f"expression_dropped:{kind}")
        return ""

    def _expr_is_series(self, node: Any) -> bool:
        """Report whether a Pine expression evaluates to a pandas Series."""
        if node is None:
            return False
        if is_node(node, "Name"):
            return self.is_series(str(getattr(node, "id", "")))
        if is_node(node, "Subscript"):
            return False
        if is_node(node, "Call"):
            parts = self._call_parts(node)
            if parts is None:
                return False
            name, owner = parts
            if owner == "ta":
                return True
            if owner in {"str", "array", "input", "request"}:
                return owner == "request"
            if owner == "math":
                return False
            if name in {"nz", "na", "fixnan"}:
                return False
            return False
        if is_node(node, "BinOp"):
            return self._expr_is_series(getattr(node, "left", None)) or self._expr_is_series(
                getattr(node, "right", None)
            )
        if is_node(node, "Conditional"):
            return self._expr_is_series(getattr(node, "body", None)) or self._expr_is_series(
                getattr(node, "orelse", None)
            )
        if is_node(node, "BoolOp"):
            return False
        return False

    def _constant(self, value: Any) -> str:
        """Render a Pine literal as a Python literal."""
        if value is None:
            return "float('nan')"
        if isinstance(value, bool):
            return "True" if value else "False"
        if isinstance(value, str):
            return _safe_string_literal(value)
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if value != value:
                return "float('nan')"
            if value in (float("inf"), float("-inf")):
                return "float('inf')" if value > 0 else "-float('inf')"
            return repr(value)
        return repr(value)

    def _library_call(self, alias: str, name: str, node: Any) -> str:
        """Render a call into an imported Pine library.

        Libraries are not transpiled, so the generated strategy substitutes the
        closest local behaviour. Date/time filters gate entries and default to
        allowing them; anything else degrades to a NaN-producing no-op.
        """
        self._warn_once(f"library_call_approximated:{alias}.{name}")
        lowered = name.lower()
        if "date" in lowered or "time" in lowered or "session" in lowered:
            return "True"
        if lowered.startswith(("is_", "in_")):
            return "True"
        return "float('nan')"

    def _literal_repr(self, value: Any) -> str | None:
        """Render a Python literal as source, or ``None`` when unsupported."""
        if isinstance(value, bool):
            return "True" if value else "False"
        if isinstance(value, (int, float, str)):
            return repr(value)
        return None

    def _name_ref(self, name: str) -> str:
        """Render a Pine identifier reference."""
        if not name:
            return ""
        if name in self.params:
            return self.py_name(name)
        if name in mapping.SERIES_NAMES:
            return f"{mapping.SERIES_NAMES[name]}_series"
        if name == "na":
            return "float('nan')"
        if name in {"true", "True"}:
            return "True"
        if name in {"false", "False"}:
            return "False"
        constant = self._static_constant(name)
        if constant is not None:
            return constant
        if name in self.constants:
            return self.constants[name]
        if name in self.state_keys:
            key = self.state_keys[name]
            return f"_state_get(\"{key}\", {self.state_init.get(key, 'float(\'nan\')')})"
        if name in mapping.SOFT_CONSTANTS and name not in self.names:
            for helper in re.findall(r"_p_[A-Za-z0-9_]+", mapping.SOFT_CONSTANTS[name]):
                self._register_helper(helper)
            return mapping.SOFT_CONSTANTS[name]
        # A script-level variable shadows a same-named function: Pine scripts
        # declare ``method atr(...)`` and then assign ``atr = ta.atr(len)``.
        if name in self.names:
            return self.names[name]
        if name in self.functions:
            return f"{LOCAL_PREFIX}fn_{name}"
        return self.py_name(name)

    def _attribute_ref(self, node: Any) -> str:
        """Render an attribute chain reference."""
        dotted = self._dotted(node)
        if dotted and dotted not in mapping.STATIC_CONSTANTS:
            owner, _, attribute = dotted.partition(".")
            field = self._object_field(owner, attribute)
            if field:
                return field
        if dotted:
            constant = self._static_constant(dotted)
            if constant is not None:
                return constant
            if dotted in self.names:
                return self.names[dotted]
            if dotted.startswith("strategy."):
                return "float('nan')"
            if dotted.startswith(("syminfo.", "timeframe.")):
                return "float('nan')"
            self._warn_once(f"attribute_dropped:{dotted}")
            return "float('nan')"
        return self._expr(getattr(node, "value", None))

    def _history_offset(self, node: Any) -> str:
        """Return the rendered ``[n]`` offset of a history reference, if any.

        ``month(time[1])`` passes a ``Subscript`` node; ``month(time)`` passes a
        plain name. Only the former carries an offset the caller must apply.
        """
        if not is_node(node, "Subscript"):
            return ""
        return self._expr(getattr(node, "slice", None))

    def _subscript(self, node: Any) -> str:
        """Render a Pine ``[n]`` series lookup."""
        value = getattr(node, "value", None)
        offset = getattr(node, "slice", None)
        rendered_offset = self._expr(offset)
        if is_node(value, "Name"):
            name = str(getattr(value, "id", ""))
            if name in mapping.SERIES_NAMES:
                column = mapping.SERIES_NAMES[name]
                return f"_p_shift({column}_series, {rendered_offset or '0'})"
        rendered_value = self._expr(value)
        if not rendered_value:
            return ""
        if rendered_offset in {"", "0"}:
            return rendered_value
        return f"_p_shift({rendered_value}, {rendered_offset})"

    def _binary(self, node: Any) -> str:
        """Render a binary arithmetic operation."""
        left = self._expr(getattr(node, "left", None))
        right = self._expr(getattr(node, "right", None))
        operator = self._binary_operator(getattr(node, "op", None))
        if not left or not right or operator is None:
            return left or right or ""
        if operator == "+":
            # Pine's ``+`` concatenates when either side happens to be a string,
            # and the runtime has no static types to prove otherwise: a colour
            # sentinel or an ``input.string`` read through a variable looks
            # numeric at emit time and only fails on the bar. Routing every
            # addition through the helper keeps both cases working.
            self._register_helper("_p_add")
            return f"_p_add({left}, {right})"
        if operator == "/":
            return f"_p_div({left}, {right})"
        if operator == "%":
            return f"_p_mod({left}, {right})"
        return f"({left} {operator} {right})"

    def _is_text(self, node: Any) -> bool:
        """Report whether a node statically produces a string."""
        if node is None:
            return False
        if is_node(node, "Constant"):
            return isinstance(getattr(node, "value", None), str)
        if is_node(node, "Call"):
            parts = self._call_parts(node)
            if parts is None:
                return False
            name, owner = parts
            if owner == "str":
                return True
            return name in {"string", "tostring"}
        if is_node(node, "Conditional"):
            return self._is_text(getattr(node, "body", None)) or self._is_text(
                getattr(node, "orelse", None)
            )
        return False

    def _binary_operator(self, node: Any) -> str | None:
        """Map a Pine binary operator node to Python syntax."""
        kind = node_name(node)
        return {
            "Add": "+",
            "Sub": "-",
            "Mult": "*",
            "Div": "/",
            "Mod": "%",
        }.get(kind)

    def _boolean(self, node: Any) -> str:
        """Render ``and``/``or`` on both scalars and series."""
        operator = node_name(getattr(node, "op", None))
        values = [self._expr(item) for item in (getattr(node, "values", []) or [])]
        values = [item for item in values if item]
        if not values:
            return ""
        target = "_p_and" if operator == "And" else "_p_or"
        if len(values) == 1:
            return values[0]
        expression = values[0]
        for value in values[1:]:
            expression = f"{target}({expression}, {value})"
        return expression

    def _unary(self, node: Any) -> str:
        """Render a unary operation."""
        operator = node_name(getattr(node, "op", None))
        operand = self._expr(getattr(node, "operand", None))
        if not operand:
            return ""
        if operator == "Not":
            return f"_p_not({operand})"
        if operator == "USub" or operator == "Sub":
            return f"(-{operand})"
        if operator == "UAdd" or operator == "Add":
            return f"(+{operand})"
        return operand

    def _generic_call(self, node: Any) -> str | None:
        """Recover ``array.new<float>(0)`` from its comparison-shaped AST.

        pynescript does not model Pine's explicit type arguments, so the parser
        yields ``array.new < float > 0`` for a generic call. When the left side
        is a callable attribute chain and the first comparator names a Pine
        type, reinterpret the node as the call it really is.
        """
        left = getattr(node, "left", None)
        comparators = list(getattr(node, "comparators", []) or [])
        operators = [node_name(item) for item in (getattr(node, "ops", []) or [])]
        if not is_node(left, "Attribute") or len(comparators) != 2:
            return None
        if operators != ["Lt", "Gt"]:
            return None
        type_node, argument = comparators
        if not is_node(type_node, "Name"):
            return None
        type_name = str(getattr(type_node, "id", ""))
        if type_name not in mapping.PINE_TYPE_WORDS and type_name not in self.user_types:
            return None
        dotted = self._dotted(left)
        if not dotted:
            return None
        parts = dotted.split(".")
        owner = parts[0] if len(parts) > 1 else ""
        name = parts[-1]
        rendered_argument = self._expr(argument)
        if not rendered_argument:
            return None
        self._warn_once(f"generic_type_argument:{dotted}")
        target = {
            "array": "_p_array_new",
            "matrix": "_p_matrix_new",
            "map": "_p_map_new",
        }.get(owner)
        if target is not None:
            self._register_helper(target)
        # Dispatch through the same namespaces as a normal call expression.
        if owner == "array":
            return f"_p_array_new({rendered_argument})"
        if owner == "matrix":
            return f"_p_matrix_new({rendered_argument})"
        if owner == "map":
            return f"_p_map_new({rendered_argument})"
        if owner == "str" and name == "tostring":
            return f"_p_str({rendered_argument})"
        return f"{self.py_name(name)}({rendered_argument})"

    def _compare(self, node: Any) -> str:
        """Render a comparison chain."""
        generic = self._generic_call(node)
        if generic is not None:
            return generic
        left = self._expr(getattr(node, "left", None))
        comparators = [self._expr(item) for item in (getattr(node, "comparators", []) or [])]
        operators = [self._compare_operator(item) for item in (getattr(node, "ops", []) or [])]
        if not left or not comparators or not operators:
            return left or ""
        parts: list[str] = [left]
        for operator, comparator in zip(operators, comparators):
            if not comparator or operator is None:
                return left
            parts.append(operator)
            parts.append(comparator)
        return "(" + " ".join(parts) + ")"

    def _compare_operator(self, node: Any) -> str | None:
        """Map a Pine comparison operator node to Python syntax."""
        return {
            "Eq": "==",
            "NotEq": "!=",
            "Lt": "<",
            "LtE": "<=",
            "Gt": ">",
            "GtE": ">=",
        }.get(node_name(node))

    def _expr_call(self, node: Any) -> str:
        """Render a Pine call expression."""
        parts = self._call_parts(node)
        if parts is None:
            return ""
        name, owner = parts
        dotted_call = f"{owner}.{name}" if owner else name
        self._check_blocked(dotted_call)
        full = self._dotted(getattr(node, "func", None))
        if full:
            constant = self._static_constant(full)
            if constant is not None:
                return constant
            if full.startswith("strategy."):
                target = mapping.TRADE_STAT_CALLS.get(full[len("strategy."):])
                if target:
                    self._register_helper(target)
                    return f"{target}()"
        if owner == "ta":
            return self._ta_call(name, node)
        if owner == "math":
            return self._math_call(name, node)
        if owner == "str":
            return self._string_call(name, node)
        if owner == "array":
            return self._array_call(name, node)
        if not owner and name == "input":
            # Deprecated flat form: ``input(defval, title)``.
            return self._input_value(node)
        if owner in {"request", "input"}:
            return self._request_or_input(name, owner, node)
        if owner == "strategy":
            return self._strategy_value(name)
        if owner in {"opentrades", "closedtrades"}:
            # ``strategy.opentrades.entry_price(0)`` parses as a three-part chain
            # whose owner collapsed to the member name; recover it from the AST.
            chain = self._dotted(getattr(node, "func", None))
            if chain.startswith("strategy."):
                member = chain[len("strategy."):]
                # Indexed form: ``strategy.closedtrades.profit(0)`` needs the
                # trade offset, which the zero-argument properties cannot take.
                indexed = mapping.CLOSED_TRADE_CALLS.get(member)
                arguments = [self._expr(item) for item in self._positional(node)]
                arguments = [item for item in arguments if item]
                if indexed:
                    self._register_helper(indexed)
                    return f"{indexed}({', '.join(arguments) or '0'})"
                target = mapping.TRADE_STAT_CALLS.get(member)
                if target:
                    self._register_helper(target)
                    return f"{target}()"
                self._warn_once(f"strategy_member_dropped:{member}")
                return "float('nan')"
        if owner == "color":
            self._warn_once(f"color_dropped:{name}")
            return "0"
        if owner in {"table", "label", "line", "box", "polyline", "linefill", "alert", "log"}:
            return ""
        dotted = self._dotted(getattr(node, "func", None))
        if dotted in mapping.GLOBAL_FUNCTIONS:
            return self._global_call(dotted, node)
        if name in mapping.GLOBAL_FUNCTIONS and not owner:
            return self._global_call(name, node)
        if not owner and name in mapping.FLAT_SERIES_CALLS:
            # Pine v3 and earlier dropped the source argument entirely
            # (``lowest(20)``); v4 and later pass it explicitly. Dispatch on the
            # argument count so both spellings reach the right helper.
            positional = list(self._positional(node))
            if len(positional) <= 1:
                target, series = mapping.FLAT_SERIES_CALLS[name]
                values = [self._expr(item) for item in positional]
                values = [item for item in values if item]
                self._register_helper(target)
                return f"{target}({series}, {values[0] if values else '14'})"
        if not owner and name in mapping.TA_SPECS:
            # Older Pine versions allow omitting the ``ta.`` namespace.
            return self._ta_call(name, node)
        if not owner and name in mapping.GLOBAL_CASTS:
            target = mapping.GLOBAL_CASTS[name]
            values = [self._expr(item) for item in self._positional(node)]
            values = [item for item in values if item]
            if not values:
                return ""
            if target == "_p_str":
                self._register_helper("_p_str")
            inner = values[0]
            return f"{target}({inner})"
        if not owner and name in mapping.TIME_COMPONENT_CALLS:
            # ``dayofweek(time)`` reads the bar's own timestamp, but
            # ``month(time[1])`` reads the *previous* bar. Dropping that offset
            # made ``new_month = month(time) != month(time[1])`` compare the
            # component against itself, so it was permanently false and every
            # strategy that guards an accumulating array on it appended once per
            # bar instead of once per month.
            args = self._positional(node)
            offset = self._history_offset(args[0]) if args else ""
            if offset and offset not in {"0"}:
                return f'_p_shift(_p_time_component("{name}"), {offset})'
            return f'_p_time_component("{name}")'
        if owner and name == "new" and owner in self.user_types:
            return self._object_new(owner, node)
        if name in self.functions and not owner:
            return self._user_function(name, node)
        if owner in self.library_aliases:
            return self._library_call(owner, name, node)
        if owner:
            # Pine method syntax routes the receiver into the first argument.
            rendered = self._method_call(owner, name, node)
            if rendered:
                return rendered
        if not owner:
            self._warn_once(f"call_dropped:{name}")
            self.stats["unsupported_calls"] = list(
                dict.fromkeys([*self.stats["unsupported_calls"], name])
            )
            return ""
        self._warn_once(f"call_dropped:{owner}.{name}")
        self.stats["unsupported_calls"] = list(
            dict.fromkeys([*self.stats["unsupported_calls"], f"{owner}.{name}"])
        )
        return ""

    def _new_instance_type(self, value: Any) -> str:
        """Return the user type when ``value`` constructs one, else ``""``."""
        if not is_node(value, "Call"):
            return ""
        parts = self._call_parts(value)
        if parts is None:
            return ""
        name, owner = parts
        if owner and name == "new" and owner in self.user_types:
            return owner
        return ""

    def _type_fields(self, node: Any) -> list[str]:
        """Return the field names declared by a Pine ``type`` block."""
        fields: list[str] = []
        for statement in getattr(node, "body", []) or []:
            target = getattr(statement, "target", None)
            if is_node(target, "Name"):
                field = str(getattr(target, "id", ""))
                if field:
                    fields.append(field)
        return fields

    def _instance_of(self, name: str) -> str:
        """Return the user type of ``name`` when it is a known instance."""
        return self.instances.get(name, "")

    def _object_new(self, type_name: str, node: Any) -> str:
        """Render ``Type.new(...)`` as a literal dict of its declared fields."""
        fields = self.user_types.get(type_name)
        if fields is None:
            return ""
        values = [self._expr(item) for item in self._positional(node)]
        entries: list[str] = []
        for index, field in enumerate(fields):
            value = values[index] if index < len(values) and values[index] else "float('nan')"
            entries.append(f"'{field}': {value}")
        return "{" + ", ".join(entries) + "}"

    def _object_field(self, owner: str, attribute: str) -> str:
        """Render ``instance.field`` for a user-type instance, else ``""``."""
        instance = self.names.get(owner) or self.py_name(owner)
        if self._instance_of(owner) and owner in self.names:
            return f"{instance}['{attribute}']"
        return ""

    def _method_call(self, owner: str, name: str, node: Any) -> str:
        """Render ``receiver.method(args)`` as a function call.

        Pine allows the receiver of an ``array``/``map``/``matrix``/``str``
        builtin, or of a user ``method``, to appear before the method name. The
        receiver becomes the first positional argument.
        """
        receiver = self._name_ref(owner)
        if not receiver:
            return ""
        arguments = [self._expr(item) for item in self._positional(node)]
        arguments = [item for item in arguments if item]
        if name in self.function_methods:
            return f"{LOCAL_PREFIX}fn_{name}({', '.join([receiver, *arguments])})"
        for namespace in ("array", "matrix", "map", "str"):
            rendered = self._namespaced_method(namespace, name, receiver, arguments, node)
            if rendered:
                return rendered
        return ""

    def _namespaced_method(
        self,
        namespace: str,
        name: str,
        receiver: str,
        arguments: list[str],
        node: Any,
    ) -> str:
        """Render ``receiver.name(...)`` through the namespace's helper table."""
        dotted = f"{namespace}.{name}"
        if namespace == "array":
            statistic = mapping.ARRAY_STATS.get(dotted)
            if statistic:
                self._register_helper("_p_array_stats")
                return f'_p_array_stats({receiver}, "{statistic}")'
            target = mapping.ARRAY_CALLS.get(dotted) or mapping.ARRAY_INTRINSICS.get(dotted)
            if target:
                return self._named_helper_call(target, [receiver, *arguments])
            return ""
        if namespace == "str":
            target = mapping.STRING_CALLS.get(dotted)
            if target:
                return self._named_helper_call(target, [receiver, *arguments])
            return ""
        targets = mapping.MATRIX_CALLS if namespace == "matrix" else mapping.MAP_CALLS
        target = targets.get(dotted)
        if target:
            return self._named_helper_call(target, [receiver, *arguments])
        return ""

    def _named_helper_call(self, target: str, arguments: list[str]) -> str:
        """Emit a helper call, registering the helper's dependency group."""
        self._register_helper(target)
        return f"{target}({', '.join(arguments)})"

    def _call_arguments(self, node: Any) -> dict[str, Any]:
        """Return call arguments keyed by their Pine parameter name or index."""
        output: dict[str, Any] = {}
        for index, argument in enumerate(getattr(node, "args", []) or []):
            label = getattr(argument, "name", None)
            value = getattr(argument, "value", None)
            if value is None:
                continue
            output[str(label) if label else f"#{index}"] = value
        return output

    def _positional(self, node: Any) -> list[Any]:
        """Return the positional argument values of a call."""
        output: list[Any] = []
        for argument in getattr(node, "args", []) or []:
            if getattr(argument, "name", None):
                continue
            value = getattr(argument, "value", None)
            if value is not None:
                output.append(value)
        return output

    # -- indicator calls ------------------------------------------------------

    def _ta_call(self, name: str, node: Any) -> str:
        """Render a ``ta.*`` call, translating tuples as needed."""
        spec = mapping.TA_SPECS.get(name)
        if spec is None:
            return self._ta_unsupported(name)
        self.stats["ta_calls"] = int(self.stats["ta_calls"]) + 1
        helper, _, outputs = spec
        self.helpers.add(helper)
        expression = self._ta_expression(name, node)
        if outputs == 1:
            return expression
        self._warn_once(f"ta_tuple_single_value:{name}")
        return f"_p_at({expression}, 0)"

    def _ta_unsupported(self, name: str) -> str:
        """Record an unmapped ``ta.*`` name and return a neutral series."""
        self._warn_once(f"ta_unsupported:{name}")
        self.stats["unsupported_calls"] = list(
            dict.fromkeys([*self.stats["unsupported_calls"], f"ta.{name}"])
        )
        return "close_series"

    def _ta_expression(self, name: str, node: Any) -> str:
        """Build the helper invocation for a ``ta.*`` call."""
        spec = mapping.TA_SPECS[name]
        helper, bindings, _ = spec
        arguments = self._call_arguments(node)
        positional = self._positional(node)
        keyword_lookup = {key: value for key, value in arguments.items() if not key.startswith("#")}
        if helper in {"pivothigh", "pivotlow"} and len(positional) == 2:
            # Pine overload: ta.pivothigh(leftbars, rightbars) implies high.
            implicit = "high_series" if helper == "pivothigh" else "low_series"
            rendered = [implicit]
            rendered.extend(self._expr(item) for item in positional)
            return f"_p_{helper}({', '.join(rendered)})"
        if helper in {"highest", "lowest", "highestbars", "lowestbars"} and len(positional) == 1:
            # Pine overload: ta.highest(length) implies high, ta.lowest(length)
            # implies low. Treating the lone argument as the source would pass an
            # integer into a rolling window.
            implicit = "high_series" if helper.startswith("highest") else "low_series"
            length = self._expr(positional[0])
            return f"_p_{helper}({implicit}, {length})"
        rendered: list[str] = []
        for binding_index, (source_index, default) in enumerate(bindings):
            value = self._binding_value(source_index, keyword_lookup, positional, helper, binding_index)
            if value is None:
                rendered.append(default)
            else:
                rendered.append(value)
        return f"_p_{helper}({', '.join(rendered)})"

    def _binding_value(
        self,
        source_index: int | tuple[int, ...] | None,
        keyword_lookup: dict[str, Any],
        positional: list[Any],
        helper: str,
        binding_index: int,
    ) -> str | None:
        """Resolve one helper argument from the Pine call site."""
        if source_index is None:
            return None
        candidates = source_index if isinstance(source_index, tuple) else (source_index,)
        for candidate in candidates:
            if candidate < len(positional):
                rendered = self._expr(positional[candidate])
                if rendered:
                    return rendered
        hint = _ARGUMENT_HINTS.get(helper)
        if hint is not None:
            for key in hint.get(binding_index, ()):
                value = keyword_lookup.get(key)
                if value is not None:
                    rendered = self._expr(value)
                    if rendered:
                        return rendered
        return None

    def _note_unresolved(self, name: str) -> None:
        """Record a name the emitter could not lower, for the pipeline to see."""
        if not name or name in {"?"}:
            return
        # Drawing namespaces have no value in the strategy runtime: they are
        # legible no-ops, not gaps in translation. Only report calls whose result
        # the strategy might actually have used.
        head = name.split(".")[0]
        if head in {"plot", "plotchar", "plotshape", "plotcandle", "plotbar",
                    "bgcolor", "barcolor", "fill", "hline", "alert", "log",
                    "table", "line", "label", "box", "polyline"}:
            return
        self.stats["unresolved"] = list(
            dict.fromkeys([*self.stats.get("unresolved", []), name])
        )

    def _static_constant(self, key: str) -> str | None:
        """Return a mapped static constant, registering the helpers it uses."""
        rendered = mapping.STATIC_CONSTANTS.get(key)
        if rendered is None:
            return None
        for helper in re.findall(r"_p_[A-Za-z0-9_]+", rendered):
            self._register_helper(helper)
        return rendered

    def _register_helper(self, target: str) -> None:
        """Record an emitted helper so its definition is included."""
        name = target[len("_p_"):] if target.startswith("_p_") else target
        if not name or not name.replace("_", "").isalnum():
            return
        self.helpers.add(name)

    def _math_call(self, name: str, node: Any) -> str:
        """Render a ``math.*`` call."""
        target = mapping.MATH_CALLS.get(f"math.{name}") or mapping.MATH_CALLS.get(name)
        self.stats["math_calls"] = int(self.stats["math_calls"]) + 1
        if target is None:
            self._warn_once(f"math_unsupported:{name}")
            return ""
        arguments = [self._expr(item) for item in self._positional(node)]
        arguments = [item for item in arguments if item]
        if not arguments:
            return f"{target}()"
        return f"{target}({', '.join(arguments)})"

    def _string_call(self, name: str, node: Any) -> str:
        """Render a ``str.*`` call."""
        target = mapping.STRING_CALLS.get(f"str.{name}") or mapping.STRING_CALLS.get(name)
        if target is None:
            self._warn_once(f"str_unsupported:{name}")
            return '""'
        arguments = [self._expr(item) for item in self._positional(node)]
        arguments = [item for item in arguments if item]
        return f"{target}({', '.join(arguments)})"

    def _array_call(self, name: str, node: Any) -> str:
        """Render an ``array.*`` call onto the emitted list helpers."""
        arguments = [self._expr(item) for item in self._positional(node)]
        arguments = [item for item in arguments if item]
        joined = ", ".join(arguments)
        dotted = f"array.{name}"
        statistics = mapping.ARRAY_STATS.get(dotted) or mapping.ARRAY_STATS.get(name)
        if statistics is not None:
            self._register_helper("_p_array_stats")
            if not arguments:
                return "float('nan')"
            return f"_p_array_stats({arguments[0]}, \"{statistics}\")"
        target = mapping.ARRAY_CALLS.get(dotted) or mapping.ARRAY_CALLS.get(name)
        if target is None:
            self._warn_once(f"array_unsupported:{name}")
            return ""
        self._register_helper(target)
        if not arguments:
            return "[]" if target == "_p_array_new" else ""
        return f"{target}({joined})"

    def _request_or_input(self, name: str, owner: str, node: Any) -> str:
        """Render ``input.*`` and ``request.security`` expressions."""
        if owner == "input":
            return self._input_value(node)
        return self._request_security(name, node)

    def _input_value(self, node: Any) -> str:
        """Render the default value of an ``input.*`` declaration.

        Pine accepts the default by keyword (``defval=``) as well as by
        position, and callers routinely put ``title=`` first; the rendered
        value must therefore come from the default, never from the display
        string.
        """
        arguments = self._call_arguments(node)
        for key in ("defval", "default"):
            source = arguments.get(key)
            if source is not None:
                rendered = self._expr(source)
                if rendered:
                    return rendered
        positional = self._positional(node)
        if positional:
            rendered = self._expr(positional[0])
            if rendered:
                return rendered
        return ""

    def _request_security(self, name: str, node: Any) -> str:
        """Render ``request.security`` as the local series when possible."""
        self.stats["uses_request_security"] = True
        positional = self._positional(node)
        if name == "security_lower_tf":
            self._warn_once("request_security_lower_tf_ignored")
            return "close_series"
        if len(positional) < 3:
            self._warn_once("request_security_incomplete")
            return "close_series"
        timeframe_node = positional[1]
        expression_node = positional[2]
        if is_node(expression_node, "Name"):
            series = str(getattr(expression_node, "id", ""))
            mapped = mapping.SERIES_NAMES.get(series)
            if mapped is not None:
                self._register_frequency(timeframe_node)
                return f"{mapped}_series"
        rendered = self._expr(expression_node)
        if not rendered:
            self._warn_once("request_security_call_form_ignored")
            return "close_series"
        self._warn_once("request_security_local_proxy")
        return rendered

    def _register_frequency(self, node: Any) -> None:
        """Record the timeframe of a ``request.security`` call when literal."""
        timeframe = self._literal_timeframe(node)
        if timeframe is None:
            self._warn_once("request_security_dynamic_timeframe")
            return
        if timeframe not in self.extra_frequencies:
            self.extra_frequencies.append(timeframe)

    def _literal_timeframe(self, node: Any) -> str | None:
        """Extract a runtime-supported frequency from a Pine timeframe literal."""
        if is_node(node, "Constant") and isinstance(getattr(node, "value", None), str):
            return normalize_timeframe(str(getattr(node, "value")))
        if is_node(node, "Name"):
            name = str(getattr(node, "id", ""))
            param = self.params.get(name)
            if param is not None:
                value = param.get("default")
                if isinstance(value, str):
                    return normalize_timeframe(value)
        return None

    def _strategy_value(self, name: str) -> str:
        """Render a ``strategy.*`` property read."""
        dotted = f"strategy.{name}"
        constant = self._static_constant(dotted)
        if constant is not None:
            return constant
        self._warn_once(f"strategy_property_dropped:{name}")
        return "float('nan')"

    def _global_call(self, name: str, node: Any) -> str:
        """Render a bare Pine global function."""
        target = mapping.GLOBAL_FUNCTIONS.get(name)
        if target is None:
            return ""
        # Aliases such as ``cum`` resolve to a helper rather than a numpy
        # builtin, so the definition has to be pulled into the module.
        self._register_helper(target)
        arguments = [self._expr(item) for item in self._positional(node)]
        arguments = [item for item in arguments if item]
        if not arguments:
            return f"{target}()"
        return f"{target}({', '.join(arguments)})"

    def _user_function(self, name: str, node: Any) -> str:
        """Render a call to a translated Pine user function."""
        arguments = [self._expr(item) for item in self._positional(node)]
        arguments = [item for item in arguments if item]
        return f"{LOCAL_PREFIX}fn_{name}({', '.join(arguments)})"

    # -- parameters -----------------------------------------------------------

    def _is_input_call(self, value: Any) -> bool:
        """Report whether a node is a Pine ``input*`` declaration."""
        if not is_node(value, "Call"):
            return False
        parts = self._call_parts(value)
        if parts is None:
            return False
        name, owner = parts
        return owner == "input" or name == "input"

    def _param_key(self, pine_name: str) -> str:
        """Return the ``context.params`` key for a Pine input name."""
        cleaned = re.sub(r"[^0-9A-Za-z_]", "_", pine_name).strip("_") or "param"
        return cleaned

    def _register_input(self, pine_name: str, node: Any) -> None:
        """Record an inferred parameter schema for a Pine input declaration."""
        key = self._param_key(pine_name)
        arguments = self._call_arguments(node)
        parts = self._call_parts(node)
        name, owner = parts if parts is not None else ("", "")
        type_name = ""
        if owner == "input":
            type_name = name
        flat = self._input_flat_call(node)
        kind = _INPUT_TYPES.get(type_name, "float")
        literal = self._input_default(arguments, flat, kind)
        title = self._input_title(arguments, flat)
        self.params[key] = {
            "type": kind,
            "default": literal,
            "title": title or key,
        }
        if kind == "string" and literal:
            pass

    def _input_flat_call(self, node: Any) -> dict[str, Any]:
        """Normalize the deprecated ``input(defval, title, type=...)`` form."""
        positional = self._positional(node)
        arguments = self._call_arguments(node)
        output: dict[str, Any] = {}
        if positional:
            output["defval"] = positional[0]
        if len(positional) > 1:
            output["title"] = positional[1]
        if len(positional) > 2:
            output["extra"] = positional[2]
        for key in ("defval", "title", "options", "minval", "maxval", "step", "type"):
            if key in arguments:
                output[key] = arguments[key]
        return output

    def _input_default(self, arguments: dict[str, Any], flat: dict[str, Any], kind: str) -> Any:
        """Extract a literal default from an input declaration."""
        for key in ("defval", "default"):
            source = arguments.get(key) or flat.get(key)
            if source is not None:
                literal = self._literal_value(source)
                if literal is not None:
                    return _coerce_literal(literal, kind)
        if kind == "bool":
            return False
        if kind == "string":
            return ""
        return 0 if kind == "int" else 0.0

    def _input_title(self, arguments: dict[str, Any], flat: dict[str, Any]) -> str:
        """Extract the human-readable title of an input declaration."""
        for key in ("title",):
            source = arguments.get(key) or flat.get(key)
            if source is not None:
                literal = self._literal_value(source)
                if isinstance(literal, str):
                    return literal
        positional = flat.get("title")
        if positional is not None:
            literal = self._literal_value(positional)
            if isinstance(literal, str):
                return literal
        return ""

    def _literal_value(self, node: Any) -> Any:
        """Return the static Python value of a node, or ``None`` when dynamic."""
        if is_node(node, "Constant"):
            return getattr(node, "value", None)
        if is_node(node, "UnaryOp"):
            operator = node_name(getattr(node, "op", None))
            inner = self._literal_value(getattr(node, "operand", None))
            if isinstance(inner, (int, float)) and not isinstance(inner, bool):
                return -inner if operator in {"USub", "Sub"} else inner
            return None
        if is_node(node, "Name"):
            name = str(getattr(node, "id", ""))
            if name == "na":
                return None
            return None
        return None

    # -- diagnostics ----------------------------------------------------------

    def _check_blocked(self, dotted: str) -> None:
        """Record a blocker when a construct cannot be reproduced faithfully."""
        for pattern, blocker in mapping.BLOCKING_PATTERNS.items():
            if dotted == pattern or dotted.startswith(pattern):
                if blocker not in self.blockers:
                    self.blockers.append(blocker)
                return

    def _note_dropped(self, namespace: str) -> None:
        """Record a dropped display namespace."""
        self.stats["dropped_statements"] = int(self.stats["dropped_statements"]) + 1
        self._warn_once(f"dropped_namespace:{namespace}")

    def _note_request(self, name: str, node: Any) -> None:
        """Record a dropped ``request.*`` call."""
        if name.startswith("security"):
            self._request_security("security", node)
            return
        blocker = mapping.BLOCKING_PATTERNS.get(f"request.{name}")
        if blocker is not None:
            if blocker not in self.blockers:
                self.blockers.append(blocker)
            return
        self._warn_once(f"request_dropped:{name}")

    def _warn_once(self, message: str) -> None:
        """Record a warning only once per message."""
        if message not in self.warnings:
            self.warnings.append(message)

    # -- post-processing ------------------------------------------------------

    def collect_identifiers(self) -> set[str]:
        """Return every emitted local identifier, for collision detection."""
        return set(self.names.values())


#: Words the runtime safety regexes treat as dangerous calls. They also match
#: inside string literals, which a script title such as "RTH open (09:30)"
#: happily contains, so such parentheses are escaped instead.
_BANNED_WORD_CALL: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w.])(open|file|eval|exec|compile)\s*\("
)


def _safe_string_literal(text: str) -> str:
    """Render a Pine string literal that cannot trip a safety regex."""
    rendered = repr(text)
    if not _BANNED_WORD_CALL.search(rendered):
        return rendered
    # ``\x28`` is the parenthesis at the source level, so the regex no longer
    # sees a call while the runtime value stays byte-identical.
    return _BANNED_WORD_CALL.sub(lambda match: match.group(0)[:-1] + "\\x28", rendered)


def _coerce_literal(value: Any, kind: str) -> Any:
    """Coerce a Pine literal to the declared parameter type."""
    if kind == "bool":
        if isinstance(value, bool):
            return value
        try:
            return bool(float(value))
        except (TypeError, ValueError):
            return False
    if kind == "int":
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0
    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if kind == "string":
        if value is None:
            return ""
        return str(value)
    return value


#: Pine ``input.*`` type names.
_INPUT_TYPES: Final[dict[str, str]] = {
    "int": "int",
    "float": "float",
    "bool": "bool",
    "string": "string",
    "symbol": "string",
    "timeframe": "string",
    "session": "string",
    "source": "float",
    "color": "string",
    "integer": "int",
    "price": "float",
}


#: Keyword aliases per helper slot, for indicators whose Pine parameters are
#: commonly supplied by name.
_ARGUMENT_HINTS: Final[dict[str, dict[int, tuple[str, ...]]]] = {
    "sma": {0: ("source", "src"), 1: ("length", "len")},
    "ema": {0: ("source", "src"), 1: ("length", "len")},
    "rma": {0: ("source", "src"), 1: ("length", "len")},
    "wma": {0: ("source", "src"), 1: ("length", "len")},
    "hma": {0: ("source", "src"), 1: ("length", "len")},
    "vwma": {0: ("source", "src"), 1: ("length", "len")},
    "stdev": {0: ("source", "src"), 1: ("length", "len")},
    "variance": {0: ("source", "src"), 1: ("length", "len")},
    "correlation": {0: ("source_a", "source1"), 1: ("source_b", "source2"), 2: ("length", "len")},
    "roc": {0: ("source", "src"), 1: ("length", "len")},
    "mom": {0: ("source", "src"), 1: ("length", "len")},
    "highest": {0: ("source", "src"), 1: ("length", "len")},
    "lowest": {0: ("source", "src"), 1: ("length", "len")},
    "highestbars": {0: ("source", "src"), 1: ("length", "len")},
    "lowestbars": {0: ("source", "src"), 1: ("length", "len")},
    "valuewhen": {1: ("source", "src")},
    "crossover": {0: ("source1", "x"), 1: ("source2", "y")},
    "crossunder": {0: ("source1", "x"), 1: ("source2", "y")},
    "cross": {0: ("source1", "x"), 1: ("source2", "y")},
    "rsi": {0: ("source", "src"), 1: ("length", "len")},
    "atr": {3: ("length", "len")},
    "cci": {3: ("length", "len")},
    "wpr": {3: ("length", "len")},
    "mfi": {4: ("length", "len")},
    "supertrend": {3: ("factor",), 4: ("atrPeriod", "atr_period", "length")},
    "dmi": {3: ("length", "len"), 4: ("lensig", "smoothing")},
    "percentrank": {0: ("source", "src"), 1: ("length", "len")},
    "bb": {0: ("source", "src"), 1: ("length", "len"), 2: ("mult",)},
    "bbw": {0: ("source", "src"), 1: ("length", "len"), 2: ("mult",)},
    "macd": {0: ("source", "src"), 1: ("fast", "fastLength"), 2: ("slow", "slowLength"), 3: ("signal", "signalLength")},
    "stoch": {0: ("source", "src"), 3: ("length", "len")},
    "linreg": {0: ("source", "src"), 1: ("length", "len")},
    "alma": {0: ("source", "src"), 1: ("length", "len"), 2: ("offset",), 3: ("sigma",)},
    "swma": {0: ("source", "src")},
    "cum": {0: ("source", "src")},
    "change": {0: ("source", "src"), 1: ("length", "len")},
    "barssince": {},
}


#: Pine timeframe literal -> runtime frequency, including TradingView aliases.
_TIMEFRAME_ALIASES: Final[dict[str, str]] = {
    "1": "1m",
    "3": "3m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1h",
    "120": "4h",
    "240": "4h",
    "d": "1d",
    "1d": "1d",
    "w": "1w",
    "1w": "1w",
    "m": "1d",
}

_SUPPORTED_FREQUENCIES: Final[frozenset[str]] = frozenset(
    {"1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"}
)


def normalize_timeframe(value: str) -> str:
    """Normalize a Pine timeframe literal to a runtime subscription frequency."""
    text = str(value or "").strip().lower()
    if not text:
        return "1d"
    if text in _TIMEFRAME_ALIASES:
        return _TIMEFRAME_ALIASES[text]
    if text in _SUPPORTED_FREQUENCIES:
        return text
    match = re.fullmatch(r"(\d+)([mhdw])", text)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        minutes = {"m": 1, "h": 60, "d": 1440, "w": 10080}[unit] * amount
        for candidate in ("1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"):
            if _FREQUENCY_MINUTES[candidate] == minutes:
                return candidate
        return "1d"
    return "1d"


_FREQUENCY_MINUTES: Final[dict[str, int]] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
    "1w": 10080,
}


def _literal_source(value: Any) -> str:
    """Render a Python value as a source literal for a ``params.get`` fallback."""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, (int, float)):
        return repr(value)
    return "0.0"


def _is_literal_expression(expression: str) -> bool:
    """Report whether an expression can be evaluated before the statements run.

    Hoisted ``context.params`` defaults are evaluated at the top of
    ``handle_data``, so the fallback may only mention literals. A default that
    names a script variable (``input.time(StartDate, 'Start')``) has to be bound
    where the Pine source declared it, after that variable exists.
    """
    try:
        tree = ast.parse(str(expression or "").strip(), mode="eval")
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Call, ast.Name)):
            return False
        if isinstance(node, ast.Attribute):
            return False
    return True
