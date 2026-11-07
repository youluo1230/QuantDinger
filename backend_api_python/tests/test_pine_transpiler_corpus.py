"""Corpus regression test for the Pine Script transpiler.

The corpus lives outside the repository (it is a cached sample of public
TradingView strategies), so these tests only run when it is present. Parsing a
single large Pine script can take tens of seconds, therefore the suite works on
a small, deterministic subset and the compile gate is applied to whatever the
transpiler actually reported as converted.

Set ``PINE_TRANSPILER_CORPUS`` to a directory of ``*.pine`` files to point the
suite at a different sample, and ``PINE_TRANSPILER_CORPUS_LIMIT`` to change how
many files are exercised.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

import pytest

from app.services.pine_transpiler import assess_migratability, parser_available, transpile_pine
from app.services.strategy_v2.contract import compile_strategy_v2

#: Candidate corpus locations, most specific first. The environment override is
#: only considered when it is actually set: ``Path("")`` would resolve to the
#: current directory and silently match an empty corpus.
_CORPUS_CANDIDATES = tuple(
    path
    for path in (
        Path(os.environ["PINE_TRANSPILER_CORPUS"])
        if os.environ.get("PINE_TRANSPILER_CORPUS")
        else None,
        Path("/tv/corpus"),
        Path("/tmp/pine_corpus"),
    )
    if path is not None
)

#: Number of scripts the suite exercises. Parsing dominates the runtime, so the
#: default keeps the whole file well inside the global pytest timeout.
_DEFAULT_LIMIT = 3


def _corpus_directory() -> Path | None:
    """Return the first corpus directory that contains scripts, if any."""
    for candidate in _CORPUS_CANDIDATES:
        if candidate.is_dir() and any(candidate.glob("*.pine")):
            return candidate
    return None


def _corpus_files(limit: int) -> list[Path]:
    """Return up to ``limit`` corpus scripts, sorted for stable ordering."""
    directory = _corpus_directory()
    if directory is None:
        return []
    return sorted(directory.glob("*.pine"))[:limit]


_CORPUS = _corpus_directory()
_LIMIT = int(os.environ.get("PINE_TRANSPILER_CORPUS_LIMIT", _DEFAULT_LIMIT))

pytestmark = [
    pytest.mark.skipif(not parser_available(), reason="pynescript is not installed"),
    pytest.mark.skipif(_CORPUS is None, reason="Pine corpus is not available"),
]


def _files() -> list[Path]:
    """Return the corpus subset this module exercises."""
    return _corpus_files(_LIMIT)


@functools.lru_cache(maxsize=None)
def _transpile(path: Path):
    """Transpile one corpus script, cached because parsing is slow."""
    source = path.read_text(encoding="utf-8", errors="replace")
    return source, transpile_pine(source, symbol="BTC/USDT", timeframe="1h", name=path.stem)


@pytest.mark.parametrize("path", _files(), ids=lambda item: item.stem[:40])
def test_corpus_script_transpiles_to_compiling_source(path: Path):
    """Any script reported as converted must also pass the strategy compiler."""
    _, result = _transpile(path)

    if not result.ok:
        pytest.skip(f"not auto-transpilable: {result.error}")

    assert result.code, "ok result without code"
    assert result.blockers == []
    # Independent belt-and-braces check, exactly as the pipeline does it.
    compile_strategy_v2(result.code)


@pytest.mark.parametrize("path", _files(), ids=lambda item: item.stem[:40])
def test_corpus_assessment_reports_a_usable_score(path: Path):
    """The scorer must always answer with a bounded score and consistent verdict."""
    source, _ = _transpile(path)
    assessment = assess_migratability(source)

    assert isinstance(assessment["score"], int)
    assert 0 <= assessment["score"] <= 100
    assert isinstance(assessment["blocked"], bool)
    assert isinstance(assessment["blockers"], list)
    assert isinstance(assessment["stats"], dict)
    # A blocked verdict must name at least one reason.
    if assessment["blocked"]:
        assert assessment["blockers"]
