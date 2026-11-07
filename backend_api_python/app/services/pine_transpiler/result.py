"""Result types shared by the Pine Script transpiler public API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TranspileResult:
    """Outcome of converting one Pine Script source into Strategy API V2 source.

    ``ok`` is only ``True`` when the emitted source was additionally verified by
    the Strategy API V2 compiler, so callers may treat ``ok`` as a compile gate
    instead of re-checking the manifest themselves.
    """

    ok: bool
    code: str | None = None
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    params: dict[str, dict[str, Any]] = field(default_factory=dict)
    entry_style: str = "none"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable view used by HTTP and worker callers."""
        return {
            "ok": self.ok,
            "code": self.code,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "stats": dict(self.stats),
            "params": {name: dict(value) for name, value in self.params.items()},
            "entry_style": self.entry_style,
            "error": self.error,
        }
