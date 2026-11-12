"""Periodic maintenance tasks managed by Celery Beat."""

from __future__ import annotations

import os
import socket

from app.celery_app import celery_app


def _enabled(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@celery_app.task(name="quantdinger.tasks.worker_heartbeat")
def record_worker_heartbeat() -> None:
    from app.services.strategy_command_repository import StrategyCommandRepository

    StrategyCommandRepository().record_worker_heartbeat(
        worker_id=f"celery:{socket.gethostname()}",
        role="celery",
        metadata={},
    )


@celery_app.task(name="quantdinger.tasks.cleanup_runtime_metadata")
def cleanup_runtime_metadata() -> dict:
    from app.services.strategy_command_repository import StrategyCommandRepository

    return StrategyCommandRepository().cleanup_runtime_metadata(
        command_retention_days=max(1, int(os.getenv("STRATEGY_COMMAND_RETENTION_DAYS", "30"))),
        heartbeat_retention_days=max(1, int(os.getenv("WORKER_HEARTBEAT_RETENTION_DAYS", "7"))),
    )


@celery_app.task(
    bind=True,
    name="quantdinger.tasks.prune_market_candles",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def prune_market_candles(self) -> dict:
    """Trim the candle store to its retention window.

    The store exists so a backtest and a chart share one download, which means
    it grows without bound by design. Retention is per-bar-age rather than
    per-stream: the oldest data is the least likely to be asked for again, and
    a range that still claims coverage for deleted bars would make every read
    compute a gap it never refills.
    """
    del self
    if not _enabled("MARKET_DATA_LAYER_ENABLED", default="false"):
        return {"skipped": True, "reason": "disabled"}
    import time

    from app.services.market_data import MarketCandleRepository

    retention_days = max(1, int(os.getenv("MARKET_DATA_RETENTION_DAYS", "400")))
    audit_days = max(1, int(os.getenv("MARKET_DATA_AUDIT_RETENTION_DAYS", "14")))
    now = int(time.time())
    repository = MarketCandleRepository()
    bars = repository.prune_before(now - retention_days * 86400)
    audits = repository.prune_audit_before(now - audit_days * 86400)
    return {
        "retention_days": retention_days,
        "audit_retention_days": audit_days,
        "deleted_bars": bars,
        "deleted_audits": audits,
    }


@celery_app.task(
    bind=True,
    name="quantdinger.tasks.reflection",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def run_reflection(self):
    del self
    if not _enabled("ENABLE_REFLECTION_WORKER", default="false"):
        return {"skipped": True}
    from app.services.reflection import ReflectionService

    return ReflectionService().run_verification_cycle()


@celery_app.task(
    bind=True,
    name="quantdinger.tasks.ai_calibration",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def run_ai_calibration(self):
    del self
    from app.services.ai_calibration import confidence_calibration_enabled
    if not confidence_calibration_enabled():
        return {"skipped": True}
    from app.services.ai_calibration import AICalibrationService

    service = AICalibrationService()
    results = []
    markets = os.getenv("AI_CALIBRATION_MARKETS", "Crypto").split(",")
    for market in markets:
        market = market.strip()
        if not market:
            continue
        result = service.calibrate_market(
            market=market,
            lookback_days=int(os.getenv("AI_CALIBRATION_LOOKBACK_DAYS", "30")),
            min_samples=int(os.getenv("AI_CALIBRATION_MIN_SAMPLES", "80")),
        )
        if result is not None:
            results.append(result.__dict__)
    return {"markets": results}


@celery_app.task(
    bind=True,
    name="quantdinger.tasks.market_catalog_sync",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def run_market_catalog_sync(self):
    del self
    if not _enabled("MARKET_CATALOG_AUTO_SYNC"):
        return {"skipped": True}
    from app.services.market_catalog_sync import run_market_catalog_sync_inline

    return run_market_catalog_sync_inline("celery-beat")
