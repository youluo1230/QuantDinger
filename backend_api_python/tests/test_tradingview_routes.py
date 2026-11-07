"""TradingView pipeline HTTP route tests.

The routes are thin wrappers over the pipeline services, so these tests assert
the things that can actually regress: the auth boundary (mutating routes are
admin-only), the request clamping, and the response envelope.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from app import create_app


@pytest.fixture()
def app():
    application = create_app()
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


def _as_role(monkeypatch, role: str | None):
    """Make ``login_required`` resolve to a fixed role.

    ``verify_token`` is looked up on ``app.utils.auth`` at call time, so
    replacing it here changes the decorator's behaviour without touching the
    route module's already-applied decorators.
    """
    from app.utils import auth as auth_module

    def _verify(_token):
        if role is None:
            return None
        return {
            "user_id": 1,
            "_verified_user_role": role,
            "_verified_username": "tester",
        }

    monkeypatch.setattr(auth_module, "verify_token", _verify)


def _auth():
    return {"Authorization": "Bearer test-token"}


def test_routes_reject_anonymous_requests(client):
    assert client.get("/api/tradingview/status").status_code == 401
    assert client.get("/api/tradingview/scripts").status_code == 401
    assert client.post("/api/tradingview/harvest").status_code == 401


def test_mutating_routes_reject_non_admin(client, monkeypatch):
    _as_role(monkeypatch, "user")

    for path, payload in (
        ("/api/tradingview/harvest", {"max_pages": 1, "max_sources": 0}),
        ("/api/tradingview/assess", {"limit": 1}),
        ("/api/tradingview/convert", {"limit": 1}),
        ("/api/tradingview/backtest", {"limit": 1}),
    ):
        response = client.post(path, headers=_auth(), json=payload)
        assert response.status_code == 403, path
        assert response.get_json()["code"] == 403


def test_read_routes_are_available_to_ordinary_users(client, monkeypatch):
    _as_role(monkeypatch, "user")
    monkeypatch.setattr(
        "app.services.tradingview_pipeline.pipeline_status",
        lambda: {"corpus": {"total": 0}, "conversions": {}, "assess_version": 1},
    )
    monkeypatch.setattr(
        "app.services.tradingview_harvest.harvest_status",
        lambda: {"runs": [], "corpus": {}},
    )

    response = client.get("/api/tradingview/status", headers=_auth())

    assert response.status_code == 200
    body = response.get_json()
    assert body["code"] == 1
    assert body["data"]["pipeline"]["corpus"]["total"] == 0


def test_harvest_route_clamps_request_budget(client, monkeypatch):
    """An interactive request must not ask for thousands of pages."""
    _as_role(monkeypatch, "admin")
    captured: Dict[str, Any] = {}

    def _harvest(**kwargs):
        captured.update(kwargs)
        return {"discovered": 0, "written": 0}

    monkeypatch.setattr("app.services.tradingview_harvest.harvest", _harvest)

    response = client.post(
        "/api/tradingview/harvest",
        headers=_auth(),
        json={"max_pages": 99, "max_sources": 9999, "topics": ["trend", " "]},
    )

    assert response.status_code == 200
    assert captured["max_pages"] == 10
    assert captured["max_sources"] == 200
    assert captured["topics"] == ["trend"]
    assert captured["trigger"] == "api"


def test_convert_route_passes_operator_budget(client, monkeypatch):
    _as_role(monkeypatch, "admin")
    captured: Dict[str, Any] = {}

    def _convert(**kwargs):
        captured.update(kwargs)
        return {"candidates": 0}

    monkeypatch.setattr("app.services.tradingview_pipeline.convert_pending_scripts", _convert)

    response = client.post(
        "/api/tradingview/convert",
        headers=_auth(),
        json={"limit": 4, "min_score": 85},
    )

    assert response.status_code == 200
    assert captured["limit"] == 4
    assert captured["min_score"] == 85
    assert captured["user_id"] == 1


def test_backtest_route_defaults_to_publishing(client, monkeypatch):
    _as_role(monkeypatch, "admin")
    captured: Dict[str, Any] = {}

    def _backtest(**kwargs):
        captured.update(kwargs)
        return {"backtested": 0, "published": 0}

    monkeypatch.setattr("app.services.tradingview_pipeline.backtest_and_publish", _backtest)

    response = client.post("/api/tradingview/backtest", headers=_auth(), json={})

    assert response.status_code == 200
    assert captured["publish"] is True
    assert captured["limit"] == 3


def test_backtest_route_can_disable_publishing(client, monkeypatch):
    _as_role(monkeypatch, "admin")
    captured: Dict[str, Any] = {}

    def _backtest(**kwargs):
        captured.update(kwargs)
        return {"backtested": 0, "published": 0}

    monkeypatch.setattr("app.services.tradingview_pipeline.backtest_and_publish", _backtest)

    client.post(
        "/api/tradingview/backtest",
        headers=_auth(),
        json={"publish": False, "limit": 1},
    )

    assert captured["publish"] is False


def test_script_detail_returns_404_for_unknown_id(client, monkeypatch):
    _as_role(monkeypatch, "user")

    class _Cur:
        def execute(self, *_args, **_kwargs):
            pass

        def fetchone(self):
            return None

        def close(self):
            pass

    class _Db:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr("app.routes.tradingview.get_db_connection", lambda: _Db())

    response = client.get("/api/tradingview/scripts/999", headers=_auth())

    assert response.status_code == 404
    assert response.get_json()["msg"] == "script_not_found"


def test_status_route_reports_errors_instead_of_raising(client, monkeypatch):
    _as_role(monkeypatch, "user")

    def _boom():
        raise RuntimeError("database is on fire")

    monkeypatch.setattr("app.services.tradingview_pipeline.pipeline_status", _boom)
    monkeypatch.setattr(
        "app.services.tradingview_harvest.harvest_status", lambda: {"runs": []}
    )

    response = client.get("/api/tradingview/status", headers=_auth())

    assert response.status_code == 500
    body = response.get_json()
    assert body["code"] == 0
    assert "on fire" in body["msg"]
