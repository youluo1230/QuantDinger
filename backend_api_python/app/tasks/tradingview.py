"""Scheduled TradingView strategy harvesting on the maintenance queue."""

from __future__ import annotations

import os

from app.celery_app import celery_app


def _enabled(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@celery_app.task(
    bind=True,
    name="quantdinger.tasks.tradingview_harvest",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=2,
    soft_time_limit=1500,
    time_limit=1800,
)
def run_tradingview_harvest(self):
    """Discover public TradingView strategies and cache their Pine sources.

    Disabled by default: enabling it makes the backend reach out to
    tradingview.com on a schedule, which operators should opt into explicitly.
    """
    del self
    if not _enabled("TRADINGVIEW_HARVEST_ENABLED"):
        return {"skipped": True, "reason": "disabled"}
    from app.services.tradingview_harvest import harvest

    # Indicators vastly outnumber strategies on subjects such as ICT and order
    # blocks, so the scheduled run pulls both categories by default.
    categories = [
        item.strip()
        for item in os.getenv("TRADINGVIEW_SCRIPT_TYPES", "strategies,indicators").split(",")
        if item.strip()
    ]
    return harvest(
        trigger="celery-beat",
        max_pages=max(1, int(os.getenv("TRADINGVIEW_HARVEST_MAX_PAGES", "3"))),
        max_sources=max(0, int(os.getenv("TRADINGVIEW_HARVEST_MAX_SOURCES", "60"))),
        script_types=categories,
    )


@celery_app.task(
    bind=True,
    name="quantdinger.tasks.tradingview_assess",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=2,
    soft_time_limit=1200,
    time_limit=1500,
)
def run_tradingview_assess(self):
    """Refresh migratability scores for harvested scripts without a score.

    Kept separate from the harvest task so a slow Pine parse never delays
    discovery, and so operators can re-score the corpus after a transpiler
    upgrade without re-crawling TradingView.
    """
    del self
    if not _enabled("TRADINGVIEW_HARVEST_ENABLED"):
        return {"skipped": True, "reason": "disabled"}
    from app.services.tradingview_pipeline import assess_pending_scripts

    return assess_pending_scripts(
        limit=max(1, int(os.getenv("TRADINGVIEW_ASSESS_BATCH", "40"))),
    )


@celery_app.task(
    bind=True,
    name="quantdinger.tasks.tradingview_convert",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=1,
    soft_time_limit=2700,
    time_limit=3000,
)
def run_tradingview_convert(self):
    """Transpile the best-scoring candidates into Strategy V2 sources."""
    del self
    if not _enabled("TRADINGVIEW_HARVEST_ENABLED"):
        return {"skipped": True, "reason": "disabled"}
    from app.services.tradingview_pipeline import convert_pending_scripts

    return convert_pending_scripts(
        limit=max(1, int(os.getenv("TRADINGVIEW_CONVERT_BATCH", "120"))),
        user_id=int(os.getenv("TRADINGVIEW_CONVERT_USER_ID", "1")),
    )


@celery_app.task(
    bind=True,
    name="quantdinger.tasks.tradingview_verify",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=1,
    soft_time_limit=3300,
    time_limit=3600,
)
def run_tradingview_verify(self):
    """Backtest converted strategies and publish the ones that clear the gate.

    The final stage of the daily loop. Publishing goes through
    ``publish_script_template_from_strategy``, which enforces the same
    ``BACKTEST_REQUIRED`` precondition as a manual publish, so an automated
    listing can never bypass the backtest gate.
    """
    del self
    if not _enabled("TRADINGVIEW_HARVEST_ENABLED"):
        return {"skipped": True, "reason": "disabled"}
    from app.services.tradingview_pipeline import backtest_and_publish

    return backtest_and_publish(
        limit=max(1, int(os.getenv("TRADINGVIEW_VERIFY_BATCH", "10"))),
        user_id=int(os.getenv("TRADINGVIEW_CONVERT_USER_ID", "1")),
        publish=_enabled("TRADINGVIEW_AUTO_PUBLISH", default="true"),
    )


__all__ = [
    "run_tradingview_assess",
    "run_tradingview_convert",
    "run_tradingview_harvest",
    "run_tradingview_verify",
]
