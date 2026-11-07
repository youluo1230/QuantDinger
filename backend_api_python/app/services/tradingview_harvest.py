"""TradingView public strategy harvesting.

TradingView exposes two unauthenticated JSON endpoints that this module relies
on:

* the public script listing feed
  ``https://www.tradingview.com/scripts/<topic>/`` with
  ``?component-data-only=1&script_type=strategies&script_access=open``
* the Pine source facade
  ``https://pine-facade.tradingview.com/pine-facade/get/<script_id_part>/last``

Both return machine-readable JSON without cookies, so harvesting needs no API
key and no Node runtime. Pagination for the listing feed is done with a path
segment (``/scripts/<topic>/page-2/``); ``?page=2`` is silently ignored by the
upstream site.

Harvested rows are staged in ``qd_tv_scripts`` rather than written straight to
``qd_script_sources`` so a scheduled crawl cannot flood the user's strategy
list. Promotion happens later, after a candidate compiles and backtests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import requests

from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

TV_WEB_BASE = os.getenv("TV_WEB_BASE", "https://www.tradingview.com").rstrip("/")
TV_FACADE_BASE = os.getenv("TV_FACADE_BASE", "https://pine-facade.tradingview.com").rstrip("/")

# Topics mirror the curated TradingView strategy collections. Order matters only
# for log readability; harvest rounds use it to spend the request budget.
DEFAULT_TOPICS: Tuple[str, ...] = (
    "editors-picks",
    "trend",
    "breakout",
    "ema",
    "sma",
    "atr",
    "supertrend",
    "vwap",
    "mean-reversion",
    "liquidity",
    "scalping",
    "momentum",
    "rsi",
    "macd",
    "bollinger",
    "pivot",
    "grid",
    "dca",
    "range",
    "volatility",
)

_ITEMS_PER_PAGE = 24
_HTTP_TIMEOUT = float(os.getenv("TV_HARVEST_HTTP_TIMEOUT_SEC", "25"))
_REQUEST_PAUSE_SEC = float(os.getenv("TV_HARVEST_REQUEST_PAUSE_SEC", "1.2"))
_DEFAULT_MAX_PAGES = max(1, int(os.getenv("TV_HARVEST_MAX_PAGES", "3")))
_DEFAULT_MAX_SOURCES = max(0, int(os.getenv("TV_HARVEST_MAX_SOURCES", "60")))

_USER_AGENT = os.getenv(
    "TV_HARVEST_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)

_harvest_lock = threading.Lock()


class TradingViewHarvestError(RuntimeError):
    """Raised when the upstream listing feed cannot be read at all."""


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def _json_get(session: requests.Session, url: str, **kwargs: Any) -> Any:
    response = session.get(url, timeout=_HTTP_TIMEOUT, **kwargs)
    response.raise_for_status()
    return response.json()


def _listing_url(topic: str, page: int) -> str:
    topic_slug = quote(topic.strip().lower() or "trend", safe="")
    parts = [f"{TV_WEB_BASE}/scripts/{topic_slug}/"]
    if page > 1:
        parts.append(f"page-{page}/")
    query = "component-data-only=1&script_type=strategies&script_access=open"
    return "".join(parts) + "?" + query


def _source_url(script_id_part: str) -> str:
    return f"{TV_FACADE_BASE}/pine-facade/get/{quote(script_id_part, safe='')}/last?no_4xx=true"


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """TradingView returns unix seconds for created/updated markers."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return datetime.fromtimestamp(number, tz=timezone.utc)


def _timestamp_sql(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.astimezone(timezone.utc)


def _access_label(raw: Any) -> str:
    """Normalise ``script_access`` which is an int in the feed and a string in the facade."""
    if isinstance(raw, bool):
        return "open" if raw else "closed"
    if isinstance(raw, (int, float)):
        return "open" if int(raw) == 1 else "invite_only"
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    return "open" if text.startswith("open") else text


def _pine_version_of(source: str) -> Optional[int]:
    match = re.search(r"//\s*@version\s*=\s*(\d+)", source or "", re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _item_to_row(topic: str, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    script_id_part = str(item.get("script_id_part") or "").strip()
    if not script_id_part:
        return None
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    symbol = item.get("symbol") if isinstance(item.get("symbol"), dict) else {}
    return {
        "script_id_part": script_id_part[:120],
        "name": str(item.get("name") or "")[:300],
        # The feed ships a short blurb; full Pine source arrives from the facade.
        "description": str(item.get("description") or ""),
        "author": str(user.get("username") or "")[:120],
        "topic": str(topic)[:64],
        "tv_script_type": str(item.get("script_type") or "")[:32],
        "script_access": _access_label(item.get("script_access"))[:32],
        "chart_url": str(item.get("chart_url") or ""),
        "source_url": str(item.get("chart_url") or ""),
        "symbol_name": str(symbol.get("name") or "")[:120],
        "likes_count": max(0, int(item.get("likes_count") or 0)),
        "views_count": max(0, int(item.get("views_count") or 0)),
        "comments_count": max(0, int(item.get("comments_count") or 0)),
        "is_picked": bool(item.get("is_picked")),
        "tv_created_at": _timestamp_sql(_parse_timestamp(item.get("date_timestamp"))),
        "tv_updated_at": _timestamp_sql(_parse_timestamp(item.get("updated_date_timestamp"))),
    }


def fetch_listing(
    topic: str,
    *,
    page: int = 1,
    session: Optional[requests.Session] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Read one listing page. Returns (rows, upstream total count)."""
    client = session or _session()
    payload = _json_get(client, _listing_url(topic, page))
    ideas = (payload or {}).get("data", {}).get("ideas", {}) or {}
    data = ideas.get("data") or {}
    items = data.get("items")
    if not isinstance(items, list):
        raise TradingViewHarvestError(
            f"unexpected listing payload for topic {topic!r} page {page}"
        )
    rows = [row for row in (_item_to_row(topic, item) for item in items if isinstance(item, dict)) if row]
    return rows, max(0, int(data.get("total") or 0))


def fetch_source(
    script_id_part: str,
    *,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Fetch the Pine source for one script id.

    Returns a dict with ``source``/``script_name``/``script_access`` plus
    ``error`` when the facade rejected the id (deleted, invite-only, ...).
    """
    client = session or _session()
    try:
        payload = _json_get(
            client,
            _source_url(script_id_part),
            headers={"Origin": TV_WEB_BASE, "Referer": TV_WEB_BASE + "/"},
        )
    except Exception as exc:  # network/HTTP/JSON: record and move on
        logger.warning("TradingView source fetch failed for %s: %s", script_id_part, exc)
        return {"source": "", "script_name": "", "script_access": "", "error": str(exc)}
    source = str((payload or {}).get("source") or "")
    return {
        "source": source,
        "script_name": str((payload or {}).get("scriptName") or ""),
        "script_access": _access_label((payload or {}).get("scriptAccess")),
        "error": "" if source else "empty_source",
    }


def _claim_run(trigger: str, topics: Iterable[str]) -> Optional[int]:
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            # Reap runs orphaned by a crash so the unique index cannot block us.
            cur.execute(
                """
                UPDATE qd_tv_harvest_runs
                   SET status = 'failed', finished_at = NOW(),
                       result = '{"error":"interrupted"}'::jsonb
                 WHERE status = 'running'
                   AND started_at < NOW() - INTERVAL '2 hours'
                """
            )
            cur.execute(
                """
                INSERT INTO qd_tv_harvest_runs (trigger_type, status, topics)
                VALUES (?, 'running', ?)
                RETURNING id
                """,
                (trigger, ",".join(topics)),
            )
            row = cur.fetchone()
            db.commit()
            return int(row["id"])
        except Exception:
            db.rollback()
            logger.exception("Could not claim a TradingView harvest run")
            return None
        finally:
            cur.close()


def _finish_run(run_id: int, status: str, result: Dict[str, Any]) -> None:
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                """
                UPDATE qd_tv_harvest_runs
                   SET status = ?, finished_at = NOW(), result = ?::jsonb
                 WHERE id = ?
                """,
                (status, json.dumps(result, default=str), run_id),
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Could not finish TradingView harvest run %s", run_id)
        finally:
            cur.close()


_UPSERT_DISCOVERY_SQL = """
INSERT INTO qd_tv_scripts (
    script_id_part, name, description, author, topic, tv_script_type,
    script_access, chart_url, source_url, symbol_name, likes_count,
    views_count, comments_count, is_picked, tv_created_at, tv_updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (script_id_part) DO UPDATE SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    author = EXCLUDED.author,
    topic = EXCLUDED.topic,
    tv_script_type = EXCLUDED.tv_script_type,
    script_access = EXCLUDED.script_access,
    chart_url = EXCLUDED.chart_url,
    source_url = EXCLUDED.source_url,
    symbol_name = EXCLUDED.symbol_name,
    likes_count = GREATEST(qd_tv_scripts.likes_count, EXCLUDED.likes_count),
    views_count = GREATEST(qd_tv_scripts.views_count, EXCLUDED.views_count),
    comments_count = GREATEST(qd_tv_scripts.comments_count, EXCLUDED.comments_count),
    is_picked = qd_tv_scripts.is_picked OR EXCLUDED.is_picked,
    tv_created_at = COALESCE(EXCLUDED.tv_created_at, qd_tv_scripts.tv_created_at),
    tv_updated_at = COALESCE(EXCLUDED.tv_updated_at, qd_tv_scripts.tv_updated_at),
    updated_at = NOW()
"""


def upsert_discovered(rows: List[Dict[str, Any]]) -> int:
    """Insert/update listing rows. Returns how many rows were upserted."""
    if not rows:
        return 0
    written = 0
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            for row in rows:
                # A script can be listed under several topics; keep the first
                # topic that found it so the column stays a discovery hint.
                cur.execute(
                    _UPSERT_DISCOVERY_SQL,
                    (
                        row["script_id_part"],
                        row["name"],
                        row["description"],
                        row["author"],
                        row["topic"],
                        row["tv_script_type"],
                        row["script_access"],
                        row["chart_url"],
                        row["source_url"],
                        row["symbol_name"],
                        row["likes_count"],
                        row["views_count"],
                        row["comments_count"],
                        row["is_picked"],
                        row["tv_created_at"],
                        row["tv_updated_at"],
                    ),
                )
                written += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            cur.close()
    return written


def pending_source_targets(limit: int) -> List[Dict[str, Any]]:
    """Pick scripts whose Pine source has not been retrieved yet.

    Ordered by likes so the most interesting candidates are converted first
    when the per-run budget is small.
    """
    if limit <= 0:
        return []
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                """
                SELECT id, script_id_part, name, likes_count
                  FROM qd_tv_scripts
                 WHERE source_fetched_at IS NULL
                   AND source_fetch_error = ''
                   AND script_access IN ('open', '')
                 ORDER BY likes_count DESC, id ASC
                 LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in (cur.fetchall() or [])]
        finally:
            cur.close()


def store_source(
    script_id_part: str,
    *,
    source: str,
    script_name: str = "",
    script_access: str = "",
    error: str = "",
) -> None:
    """Persist one fetched Pine source (or the failure reason)."""
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            if source:
                cur.execute(
                    """
                    UPDATE qd_tv_scripts
                       SET source_code = ?,
                           source_chars = ?,
                           source_hash = ?,
                           source_fetched_at = NOW(),
                           source_fetch_error = '',
                           pine_version = ?,
                           script_access = CASE WHEN ? = '' THEN script_access ELSE ? END,
                           name = CASE
                                    WHEN ? = '' OR name <> '' THEN name
                                    ELSE ?
                                  END,
                           updated_at = NOW()
                     WHERE script_id_part = ?
                    """,
                    (
                        source,
                        len(source),
                        _content_hash(source),
                        _pine_version_of(source),
                        script_access or "",
                        script_access or "",
                        script_name,
                        script_name,
                        script_id_part,
                    ),
                )
            else:
                cur.execute(
                    """
                    UPDATE qd_tv_scripts
                       SET source_fetch_error = ?, updated_at = NOW()
                     WHERE script_id_part = ?
                    """,
                    (error or "unknown_error", script_id_part),
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            cur.close()


def harvest(
    *,
    trigger: str = "manual",
    topics: Optional[Iterable[str]] = None,
    max_pages: Optional[int] = None,
    max_sources: Optional[int] = None,
) -> Dict[str, Any]:
    """Run one full harvest: discover from listings, then fetch Pine sources.

    ``max_pages`` and ``max_sources`` bound the network budget so a scheduled
    run stays well inside the Celery soft time limit.
    """
    if not _harvest_lock.acquire(blocking=False):
        logger.info("TradingView harvest already running; skipping this trigger")
        return {"skipped": True, "reason": "already_running"}
    try:
        topic_list = [t for t in (topics or DEFAULT_TOPICS) if str(t).strip()]
        page_budget = max(1, int(max_pages if max_pages is not None else _DEFAULT_MAX_PAGES))
        source_budget = max(0, int(max_sources if max_sources is not None else _DEFAULT_MAX_SOURCES))
        run_id = _claim_run(trigger, topic_list)
        started = datetime.now(tz=timezone.utc)
        discovered = 0
        written = 0
        sources_ok = 0
        sources_failed = 0
        topic_errors: Dict[str, str] = {}

        session = _session()
        try:
            for topic in topic_list:
                for page in range(1, page_budget + 1):
                    try:
                        rows, _total = fetch_listing(topic, page=page, session=session)
                    except Exception as exc:
                        topic_errors[topic] = str(exc)
                        logger.warning("TradingView listing failed for %s page %s: %s", topic, page, exc)
                        break
                    if not rows:
                        break
                    discovered += len(rows)
                    try:
                        written += upsert_discovered(rows)
                    except Exception:
                        logger.exception("Could not persist TradingView listing rows for %s", topic)
                    if len(rows) < _ITEMS_PER_PAGE:
                        break
                    if _REQUEST_PAUSE_SEC > 0:
                        threading.Event().wait(_REQUEST_PAUSE_SEC)

            for target in pending_source_targets(source_budget):
                result = fetch_source(target["script_id_part"], session=session)
                if result.get("source"):
                    store_source(
                        target["script_id_part"],
                        source=result["source"],
                        script_name=result.get("script_name", ""),
                        script_access=result.get("script_access", ""),
                    )
                    sources_ok += 1
                else:
                    store_source(
                        target["script_id_part"],
                        source="",
                        error=result.get("error") or "empty_source",
                    )
                    sources_failed += 1
                if _REQUEST_PAUSE_SEC > 0:
                    threading.Event().wait(_REQUEST_PAUSE_SEC)
        finally:
            session.close()

        summary = {
            "trigger": trigger,
            "topics": topic_list,
            "pages_per_topic": page_budget,
            "source_budget": source_budget,
            "discovered": discovered,
            "written": written,
            "sources_ok": sources_ok,
            "sources_failed": sources_failed,
            "topic_errors": topic_errors,
            "duration_sec": round((datetime.now(tz=timezone.utc) - started).total_seconds(), 2),
        }
        if run_id is not None:
            _finish_run(run_id, "success", summary)
        logger.info("TradingView harvest complete: %s", summary)
        return summary
    except Exception as exc:
        logger.exception("TradingView harvest failed")
        return {"skipped": False, "error": str(exc)}
    finally:
        _harvest_lock.release()


def harvest_status() -> Dict[str, Any]:
    """Operator-facing snapshot of the last runs and the staging corpus."""
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                """
                SELECT id, trigger_type, status, topics, started_at, finished_at, result
                  FROM qd_tv_harvest_runs
                 ORDER BY id DESC
                 LIMIT 5
                """
            )
            runs = []
            for row in cur.fetchall() or []:
                record = dict(row)
                result = record.get("result")
                if isinstance(result, str):
                    try:
                        record["result"] = json.loads(result)
                    except (TypeError, ValueError):
                        record["result"] = {}
                runs.append(record)
            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE source_fetched_at IS NOT NULL) AS with_source,
                       COUNT(*) FILTER (WHERE migratability_score >= 70) AS strong,
                       COUNT(*) FILTER (WHERE migratability_version > 0) AS assessed
                  FROM qd_tv_scripts
                """
            )
            totals = dict(cur.fetchone() or {})
            return {"runs": runs, "corpus": totals}
        finally:
            cur.close()


__all__ = [
    "DEFAULT_TOPICS",
    "TradingViewHarvestError",
    "fetch_listing",
    "fetch_source",
    "harvest",
    "harvest_status",
    "pending_source_targets",
    "store_source",
    "upsert_discovered",
]
