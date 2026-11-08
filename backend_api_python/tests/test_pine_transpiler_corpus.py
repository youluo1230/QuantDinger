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
import math
import os
from pathlib import Path
from typing import Any

import pytest

from app.services.pine_transpiler import assess_migratability, parser_available, transpile_pine
from app.services.strategy_v2 import StrategyV2BacktestRunner
from app.services.strategy_v2.contract import compile_strategy_v2
from app.services.strategy_v2.frequencies import SUPPORTED_SUBSCRIPTION_FREQUENCIES

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


def _sample_frame(frequency: str, bars: int = 420) -> Any:
    """Build a deterministic OHLCV frame with alternating candles.

    The prices oscillate so crossover-style entries actually fire: a monotonic
    series never crosses back and would make a correct strategy look inert.
    """
    import pandas as pd

    # pandas reads a bare ``m`` as month-end, so minute frequencies are spelled
    # out explicitly.
    offset = {
        "1m": "1min",
        "3m": "3min",
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "1h": "1h",
        "4h": "4h",
        "1d": "1D",
        "1w": "1W",
    }.get(frequency, "1h")

    prices = [100.0 + 12.0 * math.sin(index / 15.0) for index in range(bars)]
    opens = [prices[0]] + prices[:-1]
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(open_, close) * 1.004 for open_, close in zip(opens, prices)],
            "low": [min(open_, close) * 0.996 for open_, close in zip(opens, prices)],
            "close": prices,
            "volume": [1000.0 + (index % 17) * 10.0 for index in range(bars)],
        },
        index=pd.date_range("2026-01-01", periods=bars, freq=offset),
    )


@pytest.mark.parametrize("path", _files(), ids=lambda item: item.stem[:40])
def test_corpus_script_executes_without_crashing(path: Path):
    """Converted scripts must survive a real backtest run, not just compilation.

    Every subscribed frequency is supplied because the runtime drives the
    backtest from the shortest one subscribed (``driving_frequency``).
    """
    _, result = _transpile(path)

    if not result.ok:
        pytest.skip(f"not auto-transpilable: {result.error}")

    frames = {
        frequency: {"BTC/USDT": _sample_frame(frequency)}
        for frequency in SUPPORTED_SUBSCRIPTION_FREQUENCIES
    }
    runner = StrategyV2BacktestRunner(
        code=result.code,
        frames={"BTC/USDT": frames["1h"]["BTC/USDT"]},
        frequency_frames=frames,
        initial_capital=10000.0,
    )
    output = runner.run()

    # Trading is data-dependent, but a run must always produce an equity curve.
    assert output["finalEquity"] > 0.0
    assert isinstance(output["rawTrades"], list)
