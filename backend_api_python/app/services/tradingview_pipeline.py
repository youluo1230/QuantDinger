"""TradingView candidate assessment, conversion and marketplace promotion.

This is the orchestration layer that turns staged TradingView rows
(``qd_tv_scripts``) into executable Strategy API V2 sources
(``qd_script_sources``) and, when they clear the compile/backtest gate, into
marketplace listings.

Three stages, each individually callable so operators can run them by hand:

``assess_pending_scripts``
    Score how mechanically convertible each staged Pine script is. Cheap enough
    to run over the whole corpus.
``convert_pending_scripts``
    Transpile the highest-scoring candidates and persist them as private
    strategy sources. Compilation is a hard gate: a candidate that does not
    compile is recorded as ``compile_failed`` and never becomes a source.
``backtest_and_publish``
    Run a real backtest for converted sources, then publish the ones that pass
    to the marketplace.

Design note: stage 2 never trusts the transpiler. The transpiler's own
``ok=True`` is a hint; ``compile_strategy_v2`` is the authority.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Bump when the scoring rules or the transpiler mapping change, so previously
# assessed rows get re-assessed instead of being trusted forever.
MIGRATABILITY_VERSION = max(1, int(os.getenv("TRADINGVIEW_ASSESS_VERSION", "1")))

_DEFAULT_INSTRUMENT = os.getenv("TRADINGVIEW_DEFAULT_INSTRUMENT", "BTC/USDT")
_DEFAULT_FREQUENCY = os.getenv("TRADINGVIEW_DEFAULT_FREQUENCY", "1h")
# Only "strong" candidates are worth a backtest slot.
DEFAULT_MIN_SCORE = max(0, int(os.getenv("TRADINGVIEW_MIN_SCORE", "70")))
# A run that never opens a position does not exercise the translated strategy,
# so it is not evidence the conversion works. Anything below this bar is parked
# as ``no_trades`` instead of being published.
MIN_PUBLISH_TRADES = max(1, int(os.getenv("TRADINGVIEW_MIN_TRADES", "3")))


def _assess_module():
    """Import the transpiler lazily.

    ``pine_transpiler`` depends on the optional LGPL Pine parser, so importing
    it at module scope would make every worker pay for it. It also lets this
    module be imported before the transpiler package exists on disk.
    """
    from app.services import pine_transpiler  # noqa: PLC0415 - intentional lazy import

    return pine_transpiler


def _cheap_assessment(source: str) -> Dict[str, Any]:
    """Fallback scorer used when the Pine parser is unavailable.

    Deliberately conservative: it counts namespaces with plain string scanning
    and reports every drawing call as a blocker, so the pipeline degrades to
    "score only the obviously simple scripts" rather than to "score everything
    as fine".
    """
    text = source or ""
    lowered = text.lower()
    counts = {
        "table": lowered.count("table."),
        "line": lowered.count("line."),
        "label": lowered.count("label."),
        "box": lowered.count("box."),
        "polyline": lowered.count("polyline."),
        "request": lowered.count("request.security"),
        "array": lowered.count("array."),
        "matrix": lowered.count("matrix."),
        "map": lowered.count("map."),
    }
    has_strategy_entry = "strategy.entry" in lowered or "strategy.order" in lowered
    blockers = [name for name, count in counts.items() if count and name in ("polyline",)]
    score = 100
    if not has_strategy_entry:
        score -= 45
    if counts["request"]:
        score -= 10
    if counts["polyline"]:
        score -= 25
    if counts["table"]:
        score -= 5
    if len(text) > 40000:
        score -= 10
    return {
        "score": max(0, min(100, score)),
        "blocked": bool(blockers),
        "blockers": blockers,
        "stats": {
            "namespace_counts": counts,
            "source_chars": len(text),
            "has_strategy_entry": has_strategy_entry,
            "assessor": "cheap_fallback",
        },
    }


def assess_source(source: str) -> Dict[str, Any]:
    """Score one Pine script's mechanical convertibility.

    The ``assessor`` marker records which scorer produced the verdict. Without
    it a fallback score is indistinguishable from a parser-backed one after the
    fact, and the fallback is deliberately optimistic, so operators could no
    longer tell why a script ranked highly.
    """
    try:
        module = _assess_module()
        assessment = module.assess_migratability(source)
        assessor = "parser"
    except Exception as exc:  # parser missing or crashed on this script
        logger.debug("Falling back to the cheap assessor: %s", exc)
        assessment = _cheap_assessment(source)
        assessment.setdefault("stats", {})["assessor_error"] = str(exc)
        assessor = assessment.get("assessor") or "cheap_fallback"
    result = dict(assessment or {})
    result["score"] = max(0, min(100, int(result.get("score") or 0)))
    result["blocked"] = bool(result.get("blocked"))
    result["blockers"] = list(result.get("blockers") or [])
    result["assessor"] = assessor
    result.setdefault("stats", {})
    return result


def _fetch_source_rows(limit: int, *, only_unassessed: bool) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    where = "source_fetched_at IS NOT NULL AND source_code <> ''"
    params: list[Any] = []
    if only_unassessed:
        where += " AND migratability_version < ?"
        params.append(MIGRATABILITY_VERSION)
    params.append(limit)
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                f"""
                SELECT id, script_id_part, name, author, source_code, likes_count,
                       symbol_name, pine_version
                  FROM qd_tv_scripts
                 WHERE {where}
                 ORDER BY migratability_score DESC, likes_count DESC, id ASC
                 LIMIT ?
                """,
                tuple(params),
            )
            return [dict(row) for row in (cur.fetchall() or [])]
        finally:
            cur.close()


def _store_assessment(script_row_id: int, assessment: Dict[str, Any]) -> None:
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                """
                UPDATE qd_tv_scripts
                   SET migratability_score = ?,
                       migratability = ?::jsonb,
                       migratability_version = ?,
                       updated_at = NOW()
                 WHERE id = ?
                """,
                (
                    int(assessment.get("score") or 0),
                    json.dumps(assessment, default=str),
                    MIGRATABILITY_VERSION,
                    script_row_id,
                ),
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Could not store assessment for TradingView row %s", script_row_id)
        finally:
            cur.close()


def assess_pending_scripts(*, limit: int = 40) -> Dict[str, Any]:
    """Score the highest-value staged scripts that have no current score."""
    rows = _fetch_source_rows(limit, only_unassessed=True)
    assessed = 0
    blocked = 0
    errors = 0
    for row in rows:
        try:
            assessment = assess_source(row.get("source_code") or "")
        except Exception:
            errors += 1
            logger.exception("Assessment failed for %s", row.get("script_id_part"))
            continue
        _store_assessment(int(row["id"]), assessment)
        assessed += 1
        if assessment.get("blocked"):
            blocked += 1
    result = {"requested": limit, "processed": assessed, "blocked": blocked, "errors": errors}
    logger.info("TradingView assessment batch finished: %s", result)
    return result


_INSERT_CONVERSION_SQL = """
INSERT INTO qd_tv_conversions (
    script_id_part, source_id, transpiler, transpiler_version, status,
    blockers, warnings, diagnostics, code_hash
) VALUES (?, ?, ?, ?, ?, ?::jsonb, ?::jsonb, ?::jsonb, ?)
ON CONFLICT (script_id_part) DO UPDATE SET
    source_id = COALESCE(EXCLUDED.source_id, qd_tv_conversions.source_id),
    transpiler = EXCLUDED.transpiler,
    transpiler_version = EXCLUDED.transpiler_version,
    status = EXCLUDED.status,
    blockers = EXCLUDED.blockers,
    warnings = EXCLUDED.warnings,
    diagnostics = EXCLUDED.diagnostics,
    code_hash = EXCLUDED.code_hash,
    updated_at = NOW()
"""


def _record_conversion(
    *,
    script_id_part: str,
    source_id: Optional[int],
    status: str,
    blockers: List[str],
    warnings: List[str],
    diagnostics: Dict[str, Any],
    code_hash: str,
) -> None:
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                _INSERT_CONVERSION_SQL,
                (
                    script_id_part,
                    source_id,
                    "pine_transpiler",
                    MIGRATABILITY_VERSION,
                    status,
                    json.dumps(blockers, default=str),
                    json.dumps(warnings, default=str),
                    json.dumps(diagnostics, default=str),
                    code_hash,
                ),
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Could not record conversion for %s", script_id_part)
        finally:
            cur.close()


def _stored_run_result(run_id: int) -> Dict[str, Any]:
    """Read back the summary of an already completed backtest run.

    Used when publishing a strategy whose backtest already succeeded, so the
    publish pass does not have to re-run an identical experiment. Keys match the
    live ``StrategyV2BacktestService.run`` result so downstream code is agnostic
    about which path produced the numbers.
    """
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                """
                SELECT total_trades, total_return, sharpe_ratio, max_drawdown
                  FROM qd_backtest_runs
                 WHERE id = ?
                """,
                (int(run_id),),
            )
            row = dict(cur.fetchone() or {})
        finally:
            cur.close()
    return {
        "totalTrades": int(row.get("total_trades") or 0),
        "totalReturn": row.get("total_return"),
        "sharpeRatio": row.get("sharpe_ratio"),
        "maxDrawdown": row.get("max_drawdown"),
    }


def _set_conversion_status(
    script_id_part: str,
    *,
    status: str,
    backtest_run_id: int | None = None,
    code_hash: str = "",
    diagnostics: Dict[str, Any] | None = None,
) -> None:
    """Move a conversion to a terminal state.

    Every outcome after a backtest attempt — published, backtested, parked as a
    failure, or parked as a no-trade run — has to be recorded, otherwise the row
    stays selectable and the same script burns a slot on every later batch.
    """
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            try:
                cur.execute(
                    """
                    UPDATE qd_tv_conversions
                       SET status = ?,
                           backtest_run_id = COALESCE(?, backtest_run_id),
                           code_hash = CASE WHEN ? = '' THEN code_hash ELSE ? END,
                           diagnostics = COALESCE(diagnostics, '{}'::jsonb) || ?::jsonb,
                           updated_at = NOW()
                     WHERE script_id_part = ?
                    """,
                    (
                        status,
                        backtest_run_id,
                        code_hash,
                        code_hash,
                        json.dumps(diagnostics or {}, default=str),
                        script_id_part,
                    ),
                )
                db.commit()
            finally:
                cur.close()
    except Exception:
        logger.exception("Could not set conversion status %s for %s", status, script_id_part)


def _existing_source_id(script_id_part: str) -> int:
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                "SELECT source_id FROM qd_tv_conversions WHERE script_id_part = ?",
                (script_id_part,),
            )
            row = cur.fetchone()
            return int(row["source_id"]) if row and row["source_id"] else 0
        finally:
            cur.close()


def _candidate_rows(limit: int, *, min_score: int) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            # Skip rows already converted (or already failed for this version).
            cur.execute(
                """
                SELECT s.id, s.script_id_part, s.name, s.author, s.source_code,
                       s.migratability_score, s.likes_count
                  FROM qd_tv_scripts s
                  LEFT JOIN qd_tv_conversions c ON c.script_id_part = s.script_id_part
                 WHERE s.source_fetched_at IS NOT NULL
                   AND s.source_code <> ''
                   AND s.migratability_score >= ?
                   AND (c.id IS NULL
                        OR c.status IN ('pending', 'transpile_failed', 'compile_failed')
                        OR c.transpiler_version <> ?)
                 ORDER BY s.migratability_score DESC, s.likes_count DESC, s.id ASC
                 LIMIT ?
                """,
                (min_score, MIGRATABILITY_VERSION, limit),
            )
            return [dict(row) for row in (cur.fetchall() or [])]
        finally:
            cur.close()


def _source_name(row: Dict[str, Any]) -> str:
    name = str(row.get("name") or "").strip() or "TradingView strategy"
    author = str(row.get("author") or "").strip()
    # Keep the TradingView attribution visible in the library list; the
    # marketplace licence note also requires it.
    prefix = "[TV]"
    return f"{prefix} {name}"[:250] + (f" (by {author})" if author else "")


def _source_description(row: Dict[str, Any], assessment: Dict[str, Any]) -> str:
    parts = [
        f"Ported from TradingView strategy {row.get('script_id_part')}.",
        f"Original author: {row.get('author') or 'unknown'}.",
        f"Migratability score: {int(assessment.get('score') or 0)}/100.",
    ]
    warnings = assessment.get("stats", {}).get("unsupported_calls") or []
    if warnings:
        shown = ", ".join(str(item) for item in list(warnings)[:8])
        parts.append(f"Dropped TradingView-only calls: {shown}.")
    return " ".join(parts)


def convert_pending_scripts(
    *,
    limit: int = 8,
    user_id: int = 1,
    min_score: int = DEFAULT_MIN_SCORE,
) -> Dict[str, Any]:
    """Transpile candidates into private Strategy V2 sources.

    Compilation is the gate: ``compile_strategy_v2`` must accept the generated
    code before a ``qd_script_sources`` row is created.
    """
    from app.services.script_source import get_script_source_service
    from app.services.strategy_v2 import compile_strategy_v2, strategy_source_code_hash

    rows = _candidate_rows(limit, min_score=min_score)
    transpiled = 0
    compiled = 0
    created = 0
    failures: List[Dict[str, str]] = []
    module = None

    for row in rows:
        script_id_part = str(row.get("script_id_part") or "")
        source = str(row.get("source_code") or "")
        if not script_id_part or not source:
            continue
        try:
            module = module or _assess_module()
            result = module.transpile_pine(
                source,
                symbol=_DEFAULT_INSTRUMENT,
                timeframe=_DEFAULT_FREQUENCY,
                name=str(row.get("name") or ""),
            )
        except Exception as exc:
            logger.exception("Transpile raised for %s", script_id_part)
            _record_conversion(
                script_id_part=script_id_part,
                source_id=None,
                status="transpile_failed",
                blockers=[],
                warnings=[],
                diagnostics={"error": str(exc)},
                code_hash="",
            )
            failures.append({"script_id_part": script_id_part, "stage": "transpile", "error": str(exc)})
            continue

        blockers = list(getattr(result, "blockers", []) or [])
        warnings = list(getattr(result, "warnings", []) or [])
        code = getattr(result, "code", None)
        ok = bool(getattr(result, "ok", False)) and bool(code)
        stats = dict(getattr(result, "stats", {}) or {})

        if not ok:
            _record_conversion(
                script_id_part=script_id_part,
                source_id=None,
                status="transpile_failed",
                blockers=blockers,
                warnings=warnings,
                diagnostics={"stats": stats, "error": getattr(result, "error", None)},
                code_hash="",
            )
            failures.append(
                {
                    "script_id_part": script_id_part,
                    "stage": "transpile",
                    "error": getattr(result, "error", None) or ",".join(blockers) or "not convertible",
                }
            )
            continue
        transpiled += 1

        # The transpiler is not the authority on compilability; the strategy
        # contract is. A candidate that fails here must not enter the library.
        try:
            compile_strategy_v2(code)
        except Exception as exc:
            code_value = getattr(exc, "code", "") or ""
            _record_conversion(
                script_id_part=script_id_part,
                source_id=None,
                status="compile_failed",
                blockers=blockers,
                warnings=warnings,
                diagnostics={"stats": stats, "compile_error": str(exc), "compile_code": code_value},
                code_hash=strategy_source_code_hash(code),
            )
            failures.append(
                {
                    "script_id_part": script_id_part,
                    "stage": "compile",
                    "error": f"{code_value}: {exc}".strip(": "),
                }
            )
            continue
        compiled += 1

        # Re-converting the same script must update the existing source instead
        # of littering the library with duplicates.
        existing_source_id = _existing_source_id(script_id_part)
        service = get_script_source_service()
        params = dict(getattr(result, "params", {}) or {})
        try:
            if existing_source_id:
                service.update_source(
                    existing_source_id,
                    user_id,
                    {
                        "name": _source_name(row),
                        "description": _source_description(row, {"score": row.get("migratability_score"), "stats": stats}),
                        "code": code,
                        "param_schema": params,
                        "metadata": {
                            "tradingview": {
                                "script_id_part": script_id_part,
                                "author": row.get("author") or "",
                                "likes_count": int(row.get("likes_count") or 0),
                                "source_chars": len(source),
                            }
                        },
                    },
                )
                source_id = existing_source_id
            else:
                source_id = service.create_source(
                    {
                        "user_id": user_id,
                        "name": _source_name(row),
                        "description": _source_description(
                            row, {"score": row.get("migratability_score"), "stats": stats}
                        ),
                        "code": code,
                        "asset_type": "script",
                        "param_schema": params,
                        "visibility": "private",
                        "status": "draft",
                        "metadata": {
                            "tradingview": {
                                "script_id_part": script_id_part,
                                "author": row.get("author") or "",
                                "likes_count": int(row.get("likes_count") or 0),
                                "source_chars": len(source),
                            }
                        },
                    }
                )
                created += 1
        except Exception as exc:
            logger.exception("Could not persist converted source for %s", script_id_part)
            _record_conversion(
                script_id_part=script_id_part,
                source_id=existing_source_id or None,
                status="persist_failed",
                blockers=blockers,
                warnings=warnings,
                diagnostics={"stats": stats, "error": str(exc)},
                code_hash=strategy_source_code_hash(code),
            )
            failures.append({"script_id_part": script_id_part, "stage": "persist", "error": str(exc)})
            continue

        _record_conversion(
            script_id_part=script_id_part,
            source_id=source_id,
            status="ready",
            blockers=blockers,
            warnings=warnings,
            diagnostics={"stats": stats},
            code_hash=strategy_source_code_hash(code),
        )

    summary = {
        "requested": limit,
        "candidates": len(rows),
        "transpiled": transpiled,
        "compiled": compiled,
        "created": created,
        "updated": compiled - created,
        "failures": failures[:20],
    }
    logger.info("TradingView conversion batch finished: %s", summary)
    return summary


def _backtest_range(frequency: str) -> tuple[datetime, datetime]:
    """Return a legal backtest window for the configured frequency.

    ``app/services/backtest_limits.py`` rejects windows longer than the
    per-frequency cap, so the window is clamped instead of hard-coded.
    """
    from app.services.backtest_limits import validate_backtest_range

    end = datetime.now(tz=timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    for days in (365, 180, 90, 30):
        start = end - timedelta(days=days)
        error = validate_backtest_range(
            market="Crypto",
            symbol=_DEFAULT_INSTRUMENT,
            timeframe=frequency,
            start_date=start,
            end_date=end,
        )
        if not error:
            return start, end
    return end - timedelta(days=30), end


def backtest_and_publish(
    *,
    limit: int = 5,
    user_id: int = 1,
    min_score: int = DEFAULT_MIN_SCORE,
    initial_capital: float = 10000.0,
    publish: bool = True,
) -> Dict[str, Any]:
    """Backtest converted sources and publish the ones that clear the gate.

    Publishing reuses the same path as a manual marketplace publish
    (``publish_script_template_from_strategy``), including the
    ``BACKTEST_REQUIRED`` precondition, so the automated and manual flows cannot
    diverge.
    """
    from app.services.community_service import get_community_service
    from app.services.strategy_v2 import (
        StrategyV2BacktestService,
        strategy_source_code_hash,
    )
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            # ``backtested`` rows already hold a successful run, so a publish
            # pass must pick them up: re-running the backtest would waste minutes
            # and still never publish anything when only fresh rows are selected.
            wanted = ("ready", "backtested") if publish else ("ready",)
            cur.execute(
                """
                SELECT c.script_id_part, c.source_id, c.status, c.backtest_run_id,
                       s.name, s.description, s.code
                  FROM qd_tv_conversions c
                  JOIN qd_script_sources s ON s.id = c.source_id
                 WHERE c.status = ANY(?)
                   AND c.source_id IS NOT NULL
                   AND s.user_id = ?
                 ORDER BY c.updated_at DESC
                 LIMIT ?
                """,
                (list(wanted), user_id, max(1, int(limit))),
            )
            rows = [dict(row) for row in (cur.fetchall() or [])]
        finally:
            cur.close()

    service = StrategyV2BacktestService()
    # Resolved lazily: a backtest-only run has no business building the
    # marketplace service, and doing so eagerly made ``publish=False`` pay for
    # expensive eager state it never used.
    community = get_community_service() if publish else None
    start, end = _backtest_range(_DEFAULT_FREQUENCY)
    results: List[Dict[str, Any]] = []
    published = 0

    for row in rows:
        source_id = int(row["source_id"])
        code = str(row.get("code") or "")
        name = str(row.get("name") or "TradingView strategy")
        if publish and str(row.get("status") or "") == "backtested" and row.get("backtest_run_id"):
            # Already has a successful run: reuse it so a publish pass is cheap
            # and idempotent instead of re-backtesting every time.
            run_id = int(row["backtest_run_id"])
            result = _stored_run_result(run_id)
        else:
            try:
                run_id, result = service.run(
                    user_id=user_id,
                    code=code,
                    start_date=start,
                    end_date=end.replace(hour=23, minute=59, second=59),
                    initial_capital=float(initial_capital),
                    leverage_enabled=False,
                    leverage=1.0,
                    commission=0.001,
                    slippage=0.0,
                    params={},
                    strategy_id=None,
                    source_id=source_id,
                    strategy_name=name,
                )
            except Exception as exc:
                logger.exception("Backtest failed for source %s", source_id)
                results.append({"source_id": source_id, "script_id_part": row["script_id_part"], "error": str(exc)})
                # Park the row so a broken strategy does not monopolise every future
                # batch. Without this the row stays ``ready`` and the same failures
                # are retried forever, starving strategies that have never run.
                _set_conversion_status(
                    row["script_id_part"],
                    status="backtest_failed",
                    diagnostics={"error": str(exc)[:2000]},
                )
                continue

        trades = int(result.get("totalTrades") or 0)
        summary = {
            "source_id": source_id,
            "script_id_part": row["script_id_part"],
            "run_id": run_id,
            "totalTrades": trades,
            "totalReturn": result.get("totalReturn"),
            "sharpeRatio": result.get("sharpeRatio"),
            "maxDrawdown": result.get("maxDrawdown"),
        }

        # A backtest that never opens a position "succeeds" without exercising
        # the translated strategy at all: the entry condition simply never held.
        # Publishing that to the marketplace would advertise an unverified
        # strategy as if it had passed, so require real evidence of trading.
        if trades < MIN_PUBLISH_TRADES:
            summary["skipped"] = "no_trades"
            summary["minTrades"] = MIN_PUBLISH_TRADES
            results.append(summary)
            _set_conversion_status(
                row["script_id_part"],
                status="no_trades",
                backtest_run_id=run_id,
                diagnostics={"totalTrades": trades, "minTrades": MIN_PUBLISH_TRADES},
            )
            continue

        if publish:
            try:
                ok, message, payload = community.publish_script_template_from_strategy(
                    user_id=user_id,
                    strategy_id=0,
                    code=code,
                    name=name,
                    description=str(row.get("description") or ""),
                    pricing_type="free",
                    price=0.0,
                    vip_free=False,
                    code_hidden=False,
                    is_admin=True,
                    existing_indicator_id=0,
                    source_id=source_id,
                    param_schema={},
                )
                summary["published"] = bool(ok)
                if not ok:
                    summary["publish_error"] = message
                else:
                    published += 1
                    indicator_id = 0
                    if isinstance(payload, dict):
                        indicator_id = int(payload.get("indicator_id") or 0)
                    with get_db_connection() as db:
                        cur = db.cursor()
                        try:
                            cur.execute(
                                """
                                UPDATE qd_tv_conversions
                                   SET status = 'published', backtest_run_id = ?,
                                       code_hash = ?, updated_at = NOW()
                                 WHERE script_id_part = ?
                                """,
                                (run_id, strategy_source_code_hash(code), row["script_id_part"]),
                            )
                            db.commit()
                        finally:
                            cur.close()
                    summary["indicator_id"] = indicator_id
            except Exception as exc:
                logger.exception("Publish failed for source %s", source_id)
                summary["publish_error"] = str(exc)
        else:
            _set_conversion_status(
                row["script_id_part"],
                status="backtested",
                backtest_run_id=run_id,
                code_hash=strategy_source_code_hash(code),
            )

        results.append(summary)

    output = {
        "requested": limit,
        "backtested": len([item for item in results if item.get("run_id")]),
        "published": published,
        "skipped": len([item for item in results if item.get("skipped")]),
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "results": results,
    }
    logger.info("TradingView backtest/publish batch finished: %s", output)
    return output


def pipeline_status() -> Dict[str, Any]:
    """Aggregate counters for operators and the API route."""
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                """
                SELECT status, COUNT(*) AS count
                  FROM qd_tv_conversions
                 GROUP BY status
                """
            )
            conversions = {str(row["status"]): int(row["count"]) for row in (cur.fetchall() or [])}
            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE source_fetched_at IS NOT NULL) AS with_source,
                       COUNT(*) FILTER (WHERE migratability_score >= 70) AS strong,
                       COUNT(*) FILTER (WHERE migratability_version > 0) AS assessed
                  FROM qd_tv_scripts
                """
            )
            corpus = dict(cur.fetchone() or {})
        finally:
            cur.close()
    return {"corpus": corpus, "conversions": conversions, "assess_version": MIGRATABILITY_VERSION}


__all__ = [
    "DEFAULT_MIN_SCORE",
    "MIGRATABILITY_VERSION",
    "MIN_PUBLISH_TRADES",
    "assess_pending_scripts",
    "assess_source",
    "backtest_and_publish",
    "convert_pending_scripts",
    "pipeline_status",
]
