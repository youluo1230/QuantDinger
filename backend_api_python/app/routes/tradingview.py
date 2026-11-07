"""TradingView supply pipeline API routes.

Operator-facing surface for the three pipeline stages:

* ``GET  /api/tradingview/status``      staged corpus and conversion counters
* ``GET  /api/tradingview/scripts``     paginated candidate list
* ``GET  /api/tradingview/scripts/<id>`` one candidate with its Pine source
* ``POST /api/tradingview/harvest``     run discovery + source fetch now
* ``POST /api/tradingview/assess``      re-score staged scripts
* ``POST /api/tradingview/convert``     transpile candidates into sources
* ``POST /api/tradingview/backtest``    backtest converted sources, optionally publish

Every mutating endpoint is admin-only: the pipeline writes strategy sources on
behalf of a single user, so it should not be reachable by ordinary accounts.
"""

from __future__ import annotations

import json
from typing import Any, Dict

from flask import g, jsonify, request

from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.utils.auth import admin_required, login_required
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

tradingview_blp = Blueprint("tradingview", __name__)


def _ok(data: Any = None, message: str = "common.success"):
    return jsonify({"code": 1, "msg": message, "data": data})


def _error(message: str, status: int = 400, data: Any = None):
    return jsonify({"code": 0, "msg": message, "data": data}), status


def _payload() -> Dict[str, Any]:
    body = request.get_json(silent=True) or {}
    return body if isinstance(body, dict) else {}


def _bool_arg(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _int_arg(raw: Any, default: int, *, minimum: int = 1, maximum: int = 1000) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


@tradingview_blp.route("/status", methods=["GET"])
@login_required
def tradingview_status():
    """Aggregate pipeline counters for operators."""
    from app.services.tradingview_harvest import harvest_status
    from app.services.tradingview_pipeline import pipeline_status

    try:
        return _ok({"pipeline": pipeline_status(), "harvest": harvest_status()})
    except Exception as exc:
        logger.exception("TradingView status failed")
        return _error(str(exc), 500)


@tradingview_blp.route("/scripts", methods=["GET"])
@login_required
def tradingview_scripts():
    """Paginated staged candidate list, ordered by migratability score."""
    raw_score = request.args.get("min_score")
    min_score = _int_arg(raw_score, 0, minimum=0, maximum=100)
    limit = _int_arg(request.args.get("limit"), 50, maximum=200)
    offset = _int_arg(request.args.get("offset"), 0, minimum=0, maximum=100000)
    topic = str(request.args.get("topic") or "").strip()
    with_source = _bool_arg(request.args.get("with_source"), False)

    where = ["migratability_score >= ?"]
    params: list[Any] = [min_score]
    if topic:
        where.append("topic = ?")
        params.append(topic)
    if with_source:
        where.append("source_fetched_at IS NOT NULL")

    clause = " AND ".join(where)
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    f"SELECT COUNT(*) AS total FROM qd_tv_scripts WHERE {clause}",
                    tuple(params),
                )
                total = int((cur.fetchone() or {}).get("total") or 0)
                cur.execute(
                    f"""
                    SELECT id, script_id_part, name, author, topic, symbol_name,
                           pine_version, likes_count, views_count, is_picked,
                           source_chars, source_fetched_at, source_fetch_error,
                           migratability_score, migratability, chart_url
                      FROM qd_tv_scripts
                     WHERE {clause}
                     ORDER BY migratability_score DESC, likes_count DESC, id ASC
                     LIMIT ? OFFSET ?
                    """,
                    tuple(params + [limit, offset]),
                )
                items = []
                for row in cur.fetchall() or []:
                    record = dict(row)
                    score_detail = record.get("migratability")
                    if isinstance(score_detail, str):
                        try:
                            record["migratability"] = json.loads(score_detail)
                        except (TypeError, ValueError):
                            record["migratability"] = {}
                    items.append(record)
            finally:
                cur.close()
    except Exception as exc:
        logger.exception("TradingView script listing failed")
        return _error(str(exc), 500)
    return _ok({"total": total, "limit": limit, "offset": offset, "items": items})


@tradingview_blp.route("/scripts/<int:script_row_id>", methods=["GET"])
@login_required
def tradingview_script_detail(script_row_id: int):
    """One staged candidate including its cached Pine source."""
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute("SELECT * FROM qd_tv_scripts WHERE id = ?", (int(script_row_id),))
                row = cur.fetchone()
            finally:
                cur.close()
    except Exception as exc:
        logger.exception("TradingView script detail failed")
        return _error(str(exc), 500)
    if not row:
        return _error("script_not_found", 404)
    record = dict(row)
    for field in ("migratability",):
        value = record.get(field)
        if isinstance(value, str):
            try:
                record[field] = json.loads(value)
            except (TypeError, ValueError):
                record[field] = {}
    return _ok(record)


@tradingview_blp.route("/harvest", methods=["POST"])
@login_required
@admin_required
def tradingview_harvest_now():
    """Run one discovery + source-fetch round synchronously.

    Bounded by ``max_pages``/``max_sources`` so an interactive request cannot
    run for minutes; the scheduled Celery task has no such bound.
    """
    from app.services.tradingview_harvest import harvest

    body = _payload()
    topics = body.get("topics")
    topic_list = [str(item) for item in topics if str(item).strip()] if isinstance(topics, list) else None
    try:
        result = harvest(
            trigger="api",
            topics=topic_list,
            max_pages=_int_arg(body.get("max_pages"), 2, maximum=10),
            max_sources=_int_arg(body.get("max_sources"), 20, minimum=0, maximum=200),
        )
    except Exception as exc:
        logger.exception("TradingView harvest endpoint failed")
        return _error(str(exc), 500)
    if result.get("skipped"):
        return _ok(result, "tradingview.harvestSkipped")
    return _ok(result)


@tradingview_blp.route("/assess", methods=["POST"])
@login_required
@admin_required
def tradingview_assess_now():
    """Score staged scripts that have no current assessment."""
    from app.services.tradingview_pipeline import assess_pending_scripts

    body = _payload()
    try:
        result = assess_pending_scripts(limit=_int_arg(body.get("limit"), 20, maximum=200))
    except Exception as exc:
        logger.exception("TradingView assess endpoint failed")
        return _error(str(exc), 500)
    return _ok(result)


@tradingview_blp.route("/convert", methods=["POST"])
@login_required
@admin_required
def tradingview_convert_now():
    """Transpile and compile-gate the best candidates into strategy sources."""
    from app.services.tradingview_pipeline import convert_pending_scripts

    body = _payload()
    user_id = int(getattr(g, "user_id", 1) or 1)
    try:
        result = convert_pending_scripts(
            limit=_int_arg(body.get("limit"), 5, maximum=50),
            user_id=user_id,
            min_score=_int_arg(body.get("min_score"), 70, minimum=0, maximum=100),
        )
    except Exception as exc:
        logger.exception("TradingView convert endpoint failed")
        return _error(str(exc), 500)
    return _ok(result)


@tradingview_blp.route("/backtest", methods=["POST"])
@login_required
@admin_required
def tradingview_backtest_now():
    """Backtest converted sources and optionally publish them to the market."""
    from app.services.tradingview_pipeline import backtest_and_publish

    body = _payload()
    user_id = int(getattr(g, "user_id", 1) or 1)
    try:
        result = backtest_and_publish(
            limit=_int_arg(body.get("limit"), 3, maximum=20),
            user_id=user_id,
            min_score=_int_arg(body.get("min_score"), 70, minimum=0, maximum=100),
            publish=_bool_arg(body.get("publish"), True),
        )
    except Exception as exc:
        logger.exception("TradingView backtest endpoint failed")
        return _error(str(exc), 500)
    return _ok(result)


__all__ = ["tradingview_blp"]
