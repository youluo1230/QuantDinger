"""Tests for running indicator code through the chart preview.

The preview is what the editor draws against, so these tests are about the two
things that decide whether an author can iterate: that unsaved code is executed
as written, and that a failure comes back as an error rather than as a silently
empty chart.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services import indicator_signal_alerts as module


class _Service(module.IndicatorSignalAlertService):
    """Preview with the database and the market data replaced by fixtures."""

    def __init__(self, output=None, error=None, bars=None):
        self._output = output or {"plots": [], "signals": [], "layers": []}
        self._error = error
        self._bars = bars if bars is not None else [{"time": 1, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}]
        self.loaded_ids = []
        self.executed = []

    def _load_indicator_for_user(self, user_id, indicator_id):
        self.loaded_ids.append(indicator_id)
        return {"id": indicator_id, "name": "Saved", "runtime_code": "output = {'plots': []}", "code": ""}

    def _bars_to_df(self, bars):
        import pandas as pd

        if not bars:
            return pd.DataFrame()
        frame = pd.DataFrame(bars)
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        return frame.set_index("time")

    def _execute_indicator(self, code, df, params):
        self.executed.append((code, params))
        if self._error:
            raise RuntimeError(self._error)
        return self._output


@pytest.fixture()
def kline(monkeypatch):
    class _Kline:
        def get_kline(self, **_kwargs):
            return [
                {"time": 1700000000, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 10.0},
                {"time": 1700003600, "open": 1.05, "high": 1.2, "low": 1.0, "close": 1.15, "volume": 12.0},
            ]

    return _Kline()


def test_unsaved_code_is_executed_instead_of_the_stored_row(kline):
    """An author editing text has no saved row to preview."""
    service = _Service(output={"plots": [{"name": "Basis", "data": [1.0, 2.0]}], "signals": [], "layers": []})
    service.kline = kline

    result = service.preview_chart(1, {
        "code": "my_indicator_name = 'draft'",
        "name": "draft",
        "market": "Crypto",
        "symbol": "BTC/USDT",
        "timeframe": "1H",
    })

    # The stored row is never consulted, because it describes a different program.
    assert service.loaded_ids == []
    assert service.executed[0][0] == "my_indicator_name = 'draft'"
    assert result["indicator"]["name"] == "draft"
    assert result["plots"][0]["data"] == [1.0, 2.0]


def test_inline_code_wins_over_an_id_that_is_also_present(kline):
    """A draft on top of a saved indicator must preview the draft."""
    service = _Service(output={"plots": [], "signals": [], "layers": []})
    service.kline = kline

    service.preview_chart(1, {
        "indicator_id": 42,
        "code": "output = {'plots': []}",
        "market": "Crypto",
        "symbol": "BTC/USDT",
        "timeframe": "1H",
    })

    assert service.loaded_ids == []
    assert service.executed[0][0] == "output = {'plots': []}"


def test_a_saved_indicator_still_loads_by_id(kline):
    service = _Service(output={"plots": [], "signals": [], "layers": []})
    service.kline = kline

    service.preview_chart(1, {"indicator_id": 42, "market": "Crypto", "symbol": "BTC/USDT", "timeframe": "1H"})

    assert service.loaded_ids == [42]
    assert service.executed[0][0] == "output = {'plots': []}"


def test_neither_an_id_nor_code_is_an_error(kline):
    service = _Service()
    service.kline = kline

    with pytest.raises(ValueError, match="indicator_id is required"):
        service.preview_chart(1, {"market": "Crypto", "symbol": "BTC/USDT", "timeframe": "1H"})


def test_a_runtime_failure_surfaces_instead_of_returning_an_empty_chart(kline):
    """Silence is the worst outcome: it looks like a working, flat indicator."""
    service = _Service(error="NameError: name 'undefined_thing' is not defined")
    service.kline = kline

    with pytest.raises(RuntimeError, match="undefined_thing"):
        service.preview_chart(1, {
            "code": "plot(undefined_thing)",
            "market": "Crypto",
            "symbol": "BTC/USDT",
            "timeframe": "1H",
        })


def test_the_preview_still_echoes_the_symbol_it_ran_on(kline):
    """The chart needs to know which instrument these numbers belong to."""
    service = _Service(output={"plots": [], "signals": [], "layers": []})
    service.kline = kline

    result = service.preview_chart(1, {
        "code": "output = {'plots': []}",
        "market": "Crypto",
        "symbol": "ETH/USDT",
        "timeframe": "4H",
    })

    assert result["symbol"] == "ETH/USDT"
    assert result["timeframe"] == "4H"
