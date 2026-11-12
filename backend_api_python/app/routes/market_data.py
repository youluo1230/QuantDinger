"""Unified market data API.

One router for the question every consumer asks -- "give me candles for this
range" -- so the chart, the backtester and any future feature share one store
instead of three private caches that disagree.

``/api/indicator/kline`` remains the address the current frontend calls; it
delegates here once ``MARKET_DATA_LAYER_ENABLED`` is on, which is what makes the
migration invisible to the browser.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from flask import jsonify, request

from app.data_sources.base import timeframe_seconds
from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.services.market.watchlist import validate_watchlist_pair
from app.services.market_context import SUPPORTED_CRYPTO_EXCHANGE_IDS, MarketContext
from app.services.market_data import CandleStream, MarketCandleRepository
from app.services.market_data.service import (
    MAX_BARS_PER_RESPONSE,
    get_market_data_service,
    is_market_data_layer_enabled,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

market_data_blp = Blueprint("market_data", __name__)


def _ok(data: Any = None, message: str = "common.success", meta: Optional[Dict[str, Any]] = None):
    payload: Dict[str, Any] = {"code": 1, "msg": message, "data": data}
    if meta is not None:
        payload["meta"] = meta
    return jsonify(payload)


def _error(message: str, status: int = 400):
    return jsonify({"code": 0, "msg": message, "data": None}), status


def _int_arg(name: str, default: Optional[int] = None, *, minimum: Optional[int] = None,
             maximum: Optional[int] = None) -> Optional[int]:
    raw = request.args.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _resolve_stream() -> tuple[Optional[CandleStream], Optional[MarketContext], Optional[str]]:
    """Build the stream identity from query parameters.

    Returns ``(stream, context, error)``. The context is returned so the caller
    can echo back what the request resolved to, which is what lets a client
    confirm that ``BTCUSDT`` with no suffix became ``BTC/USDT``.
    """
    market = (request.args.get("market", "USStock") or "").strip()
    symbol = (request.args.get("symbol", "") or "").strip()
    timeframe = (request.args.get("timeframe", "1D") or "").strip()
    exchange_id = (
        request.args.get("exchange_id") or request.args.get("exchangeId") or ""
    ).strip()
    market_type = (
        request.args.get("market_type") or request.args.get("marketType") or ""
    ).strip()
    instrument_id = (
        request.args.get("instrument_id") or request.args.get("instrumentId") or ""
    ).strip()

    if not symbol:
        return None, None, "marketData.symbolRequired"

    context = MarketContext.from_mapping(
        {
            "market": market,
            "symbol": symbol,
            "exchange_id": exchange_id or None,
            "market_type": market_type or None,
            "instrument_id": instrument_id,
            "timeframe": timeframe,
        },
        apply_crypto_defaults=False,
    )
    if market == "Crypto":
        if context.exchange_id and context.exchange_id not in SUPPORTED_CRYPTO_EXCHANGE_IDS:
            return None, None, "marketData.unsupportedCryptoExchange"
        symbol = context.symbol
        exchange_id = context.exchange_id or ""
        market_type = context.market_type or ""

    validation_err = validate_watchlist_pair(market, symbol)
    if validation_err:
        return None, None, validation_err

    try:
        timeframe_seconds(timeframe)
    except ValueError:
        return None, None, "marketData.unsupportedTimeframe"

    stream = CandleStream(
        market=market,
        symbol=symbol,
        timeframe=timeframe,
        exchange_id=exchange_id,
        market_type=market_type,
        instrument_id=instrument_id,
    )
    return stream, context, None


@market_data_blp.route("/candles", methods=["GET"])
def get_candles():
    """Return bars for a range, fetching what is not stored.

    Accepts either an explicit ``start``/``end`` (unix seconds) or a ``limit``
    meaning "the newest N bars". The response carries the same bar shape the
    chart already consumes plus a ``source`` field reporting whether the answer
    came from storage, upstream, or both, which is what makes the layer
    observable while it is being rolled out.
    """
    stream, context, error = _resolve_stream()
    if error:
        return _error(error, 400)
    assert stream is not None

    start = _int_arg("start")
    end = _int_arg("end")
    limit = _int_arg("limit", 300, minimum=1, maximum=MAX_BARS_PER_RESPONSE)
    allow_fetch = (request.args.get("fetch", "1") or "").strip().lower() not in {"0", "false", "no"}

    try:
        result = get_market_data_service().get_candles(
            stream, start=start, end=end, limit=int(limit or 300), allow_fetch=allow_fetch
        )
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001 - surface a readable message
        logger.error("Market data candles failed: %s", exc, exc_info=True)
        return _error(f"marketData.fetchFailed: {exc}", 500)

    rows = result.as_rows()
    if not rows:
        return _ok(
            [],
            "No data found",
            meta={
                "market_context": context.as_dict() if context else None,
                "bar_count": 0,
                "source": result.source,
                "missing": [list(item) for item in result.missing],
                "errors": result.errors,
            },
        )

    return _ok(
        rows,
        meta={
            "market_context": context.as_dict() if context else None,
            "bar_count": len(rows),
            "source": result.source,
            "fetched_bars": result.fetched_bars,
            "truncated": result.truncated,
            "missing": [list(item) for item in result.missing],
            "errors": result.errors,
        },
    )


@market_data_blp.route("/coverage", methods=["GET"])
def get_coverage():
    """Report which ranges are already stored for a stream."""
    stream, context, error = _resolve_stream()
    if error:
        return _error(error, 400)
    assert stream is not None

    try:
        report = get_market_data_service().coverage(stream)
    except Exception as exc:  # noqa: BLE001
        logger.error("Market data coverage failed: %s", exc, exc_info=True)
        return _error(f"marketData.coverageFailed: {exc}", 500)

    report["market_context"] = context.as_dict() if context else None
    return _ok(report)


@market_data_blp.route("/prefetch", methods=["POST"])
def post_prefetch():
    """Warm a range without returning its bars.

    Used before a backtest: the caller wants the data resident and does not want
    to carry a year of bars through the response.
    """
    stream, _context, error = _resolve_stream()
    if error:
        return _error(error, 400)
    assert stream is not None

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    start = body.get("start")
    end = body.get("end")
    if start is None and request.args.get("start") is not None:
        start = _int_arg("start")
    if end is None and request.args.get("end") is not None:
        end = _int_arg("end")
    if start is None or end is None:
        return _error("marketData.rangeRequired", 400)

    try:
        report = get_market_data_service().prefetch(
            stream, start=int(start), end=int(end)
        )
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        logger.error("Market data prefetch failed: %s", exc, exc_info=True)
        return _error(f"marketData.prefetchFailed: {exc}", 500)

    return _ok(report)


@market_data_blp.route("/status", methods=["GET"])
def get_status():
    """Storage size and per-market coverage, for operators."""
    try:
        repository = MarketCandleRepository()
        summary = repository.summary()
    except Exception as exc:  # noqa: BLE001
        logger.error("Market data status failed: %s", exc, exc_info=True)
        return _error(f"marketData.statusFailed: {exc}", 500)

    summary["enabled"] = is_market_data_layer_enabled()
    return _ok(summary)


def is_enabled() -> bool:
    """Whether requests should be routed through this layer."""
    return is_market_data_layer_enabled()
