"""Behaviour of the unified market-data HTTP surface.

These cover the parts a client can get wrong: which parameters are required,
what an unsupported pair reports, and -- most importantly -- that the legacy
kline endpoint keeps working when the layer is switched off or cannot answer.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.routes import market_data as routes_module
from app.services.market_data.service import CandleResult


class _Service:
    """Records calls and returns a caller-supplied result."""

    def __init__(self, result: Optional[CandleResult] = None, error: Optional[Exception] = None):
        self.result = result or CandleResult(bars=[], source="none")
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    def get_candles(self, stream, **kwargs):
        self.calls.append({"stream": stream, **kwargs})
        if self.error:
            raise self.error
        return self.result

    def coverage(self, stream):
        return {"stream": {"market": stream.market, "symbol": stream.symbol}, "ranges": []}

    def prefetch(self, stream, *, start, end):
        return {"requested": [start, end], "missing": [], "fetched": 0, "covered": True, "errors": []}


@pytest.fixture()
def client(monkeypatch):
    from flask import Flask

    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.register_blueprint(routes_module.market_data_blp, url_prefix="/api/market-data")
    with app.test_client() as test_client:
        yield test_client


def test_candles_requires_a_symbol(client, monkeypatch):
    monkeypatch.setattr(routes_module, "get_market_data_service", lambda: _Service())

    response = client.get("/api/market-data/candles?market=Crypto")

    assert response.status_code == 400
    assert response.get_json()["msg"] == "marketData.symbolRequired"


def test_candles_rejects_an_unknown_timeframe(client, monkeypatch):
    monkeypatch.setattr(routes_module, "get_market_data_service", lambda: _Service())

    response = client.get("/api/market-data/candles?market=Crypto&symbol=BTC/USDT&timeframe=7H")

    assert response.status_code == 400
    assert response.get_json()["msg"] == "marketData.unsupportedTimeframe"


def test_candles_returns_bars_and_reports_where_they_came_from(client, monkeypatch):
    from app.services.market_data import CandleBar

    result = CandleResult(
        bars=[CandleBar(time=1_700_000_000, open=1, high=2, low=0.5, close=1.5, volume=3)],
        source="storage",
        stored_bars=1,
    )
    service = _Service(result=result)
    monkeypatch.setattr(routes_module, "get_market_data_service", lambda: service)

    response = client.get("/api/market-data/candles?market=Crypto&symbol=BTC/USDT&timeframe=1H&limit=5")

    body = response.get_json()
    assert response.status_code == 200
    assert body["data"] == [
        {"time": 1_700_000_000, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 3.0}
    ]
    assert body["meta"]["source"] == "storage"
    assert body["meta"]["bar_count"] == 1
    assert service.calls[0]["limit"] == 5


def test_candles_can_be_run_without_touching_the_network(client, monkeypatch):
    service = _Service()
    monkeypatch.setattr(routes_module, "get_market_data_service", lambda: service)

    client.get("/api/market-data/candles?market=Crypto&symbol=BTC/USDT&timeframe=1H&fetch=0")

    assert service.calls[0]["allow_fetch"] is False


def test_a_failing_store_is_reported_rather_than_masked(client, monkeypatch):
    service = _Service(error=RuntimeError("store down"))
    monkeypatch.setattr(routes_module, "get_market_data_service", lambda: service)

    response = client.get("/api/market-data/candles?market=Crypto&symbol=BTC/USDT&timeframe=1H")

    assert response.status_code == 500
    assert "store down" in response.get_json()["msg"]


def test_prefetch_needs_a_range(client, monkeypatch):
    monkeypatch.setattr(routes_module, "get_market_data_service", lambda: _Service())

    response = client.post("/api/market-data/prefetch?market=Crypto&symbol=BTC/USDT&timeframe=1H")

    assert response.status_code == 400
    assert response.get_json()["msg"] == "marketData.rangeRequired"


def test_prefetch_accepts_the_range_in_the_body(client, monkeypatch):
    service = _Service()
    monkeypatch.setattr(routes_module, "get_market_data_service", lambda: service)

    response = client.post(
        "/api/market-data/prefetch?market=Crypto&symbol=BTC/USDT&timeframe=1H",
        json={"start": 1_700_000_000, "end": 1_700_360_000},
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["requested"] == [1_700_000_000, 1_700_360_000]


class _Repo:
    def summary(self):
        return {
            "markets": [{"market": "Crypto", "bars": 10, "streams": 1, "oldest": 1, "newest": 2}],
            "ranges": {"Crypto": 1},
            "fetches24h": {"ok": 3},
        }


def test_status_reports_storage_and_whether_the_layer_is_on(client, monkeypatch):
    monkeypatch.setattr(routes_module, "MarketCandleRepository", _Repo)
    monkeypatch.setenv("MARKET_DATA_LAYER_ENABLED", "true")

    body = client.get("/api/market-data/status").get_json()

    assert body["data"]["enabled"] is True
    assert body["data"]["markets"][0]["bars"] == 10


def test_the_bridge_stays_out_of_the_way_when_the_layer_is_off(monkeypatch):
    """A deployment without the flag must keep its existing code path."""
    from app.services.market_data import service as service_module

    monkeypatch.delenv("MARKET_DATA_LAYER_ENABLED", raising=False)

    assert (
        service_module.fetch_through_layer(market="Crypto", symbol="BTC/USDT", timeframe="1H", limit=10)
        is None
    )
    assert (
        service_module.fetch_range_through_layer(
            market="Crypto", symbol="BTC/USDT", timeframe="1H", start=0, end=3600
        )
        is None
    )


def test_the_bridge_never_raises_when_the_store_is_broken(monkeypatch):
    from app.services.market_data import service as service_module

    monkeypatch.setenv("MARKET_DATA_LAYER_ENABLED", "true")

    class _Broken:
        def read_ranges(self, stream):
            raise RuntimeError("no such table")

        def read_bars(self, *args, **kwargs):
            raise RuntimeError("no such table")

        def write_bars(self, *args, **kwargs):
            raise RuntimeError("no such table")

        def record_fetch(self, *args, **kwargs):
            pass

    monkeypatch.setattr(service_module, "MarketCandleRepository", _Broken)
    monkeypatch.setattr(service_module, "_service", None)

    assert (
        service_module.fetch_through_layer(market="Crypto", symbol="BTC/USDT", timeframe="1H", limit=10)
        is None
    )


def test_a_range_request_only_answers_when_the_window_is_fully_stored(monkeypatch):
    """A backtest rejects partial history, so a partial answer must not be given."""
    from app.services.market_data import CandleBar, CoveredRange
    from app.services.market_data import service as service_module

    monkeypatch.setenv("MARKET_DATA_LAYER_ENABLED", "true")

    class _PartialRepo:
        def read_ranges(self, stream):
            return [CoveredRange(0, 1000, 10)]

        def read_bars(self, stream, start, end, *, limit=0):
            return [CandleBar(time=0, open=1, high=1, low=1, close=1)]

        def write_bars(self, *args, **kwargs):
            return 0

        def record_fetch(self, *args, **kwargs):
            pass

    monkeypatch.setattr(service_module, "MarketCandleRepository", _PartialRepo)
    monkeypatch.setattr(service_module, "_service", None)

    # Inside the stored range: answered.
    assert service_module.fetch_range_through_layer(
        market="Crypto", symbol="BTC/USDT", timeframe="1H", start=0, end=1000
    ) is not None

    # Reaches past it: refused, so the caller takes its own path.
    assert service_module.fetch_range_through_layer(
        market="Crypto", symbol="BTC/USDT", timeframe="1H", start=0, end=9000
    ) is None
