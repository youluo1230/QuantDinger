from __future__ import annotations

import json

from app.services import user_preferences


def _pane(symbol: str, indicators: list | None = None) -> dict:
    return {
        "symbol": symbol,
        "timeframe": "1H",
        "chartType": "candle_solid",
        "indicators": indicators
        if indicators is not None
        else [{"id": 7, "name": "EMA", "visible": True, "params": {"Length": 21}}],
    }


def _workspace(**overrides) -> dict:
    workspace = {
        "version": 3,
        "layoutMode": 4,
        "activePane": 1,
        "editorOpen": True,
        "panes": [
            _pane("QD:crypto:swap:binance:BTC/USDT"),
            _pane("QD:USStock:stock::AAPL"),
        ],
    }
    workspace.update(overrides)
    return workspace


def _memory_store(monkeypatch) -> list:
    stored: list = []

    def load(user_id: int) -> list:
        return [dict(item) for item in stored]

    def store(user_id: int, templates: list) -> None:
        stored[:] = templates

    monkeypatch.setattr(user_preferences, "_load_chart_templates", load)
    monkeypatch.setattr(user_preferences, "_store_chart_templates", store)
    return stored


def test_workspace_is_clamped_to_what_the_ide_can_render():
    workspace, error = user_preferences.normalize_workspace(_workspace(activePane=9, layoutMode=3))

    assert error == ""
    assert workspace["version"] == 1
    assert workspace["layoutMode"] == 1
    assert workspace["activePane"] == 1
    assert len(workspace["panes"]) == 2
    assert workspace["panes"][0]["symbol"] == "QD:crypto:swap:binance:BTC/USDT"
    assert workspace["panes"][0]["indicators"][0] == {
        "id": "7",
        "name": "EMA",
        "visible": True,
        "params": {"Length": 21},
    }


def test_workspace_rejects_a_shape_the_ide_could_not_restore():
    assert user_preferences.normalize_workspace(None)[1] == "Workspace must be an object"
    assert user_preferences.normalize_workspace({"layoutMode": 1})[1] == "Workspace panes are required"
    too_many = _workspace(panes=[_pane(str(index)) for index in range(5)])
    assert "at most 4 panes" in user_preferences.normalize_workspace(too_many)[1]
    oversized = _workspace(panes=[_pane("X" * 64, [{"id": index, "params": {"v": "y" * 200}} for index in range(20)])] * 4)
    oversized["panes"][0]["symbol"] = "S" * 64
    oversized["panes"][0]["indicators"] = [
        {"id": str(index), "params": {f"k{index}": "z" * 200}} for index in range(20)
    ]
    oversized["padding"] = "p" * (70 * 1024)
    assert user_preferences.normalize_workspace(oversized)[1] == "Workspace is too large"


def test_workspace_snapshot_round_trips_through_a_saved_template(monkeypatch):
    stored = _memory_store(monkeypatch)

    ok, message, saved = user_preferences.save_chart_template(
        4, {"name": "Desk", "workspace": _workspace()}
    )

    assert ok, message
    assert saved["name"] == "Desk"
    assert saved["workspace"]["panes"][1]["symbol"] == "QD:USStock:stock::AAPL"
    # Older clients only understand a flat list; it is derived from the active pane.
    assert saved["indicators"][0]["id"] == "7"
    assert saved["indicators"][0]["type"] == "pine"
    assert stored[0]["workspace"]["panes"][0]["timeframe"] == "1H"


def test_legacy_indicator_template_is_converted_to_a_single_pane(monkeypatch):
    _memory_store(monkeypatch)

    ok, message, saved = user_preferences.save_chart_template(
        4,
        {
            "name": "Legacy",
            "indicators": [
                {
                    "id": 11,
                    "instanceId": "abc",
                    "type": "pine",
                    "name": "MACD",
                    "visible": True,
                    "params": {"fast": 12},
                }
            ],
        },
    )

    assert ok, message
    workspace = saved["workspace"]
    assert len(workspace["panes"]) == 1
    assert workspace["panes"][0]["symbol"] == ""
    assert workspace["panes"][0]["indicators"] == [
        {"id": "11", "name": "MACD", "visible": True, "params": {"fast": 12}}
    ]


def test_saving_an_existing_template_keeps_its_identity(monkeypatch):
    monkeypatch.setattr(
        user_preferences,
        "_load_chart_templates",
        lambda user_id: [
            {
                "id": "tpl_1",
                "name": "Old",
                "created_at": "2026-11-01T00:00:00Z",
                "updated_at": "2026-11-01T00:00:00Z",
                "indicators": [],
            }
        ],
    )
    written: list = []
    monkeypatch.setattr(
        user_preferences, "_store_chart_templates", lambda user_id, templates: written.extend(templates)
    )

    ok, message, saved = user_preferences.save_chart_template(
        4, {"template_id": "tpl_1", "name": "Renamed", "workspace": _workspace()}
    )

    assert ok, message
    assert saved["id"] == "tpl_1"
    assert saved["name"] == "Renamed"
    assert saved["created_at"] == "2026-11-01T00:00:00Z"
    assert len(written) == 1


def test_saved_templates_are_still_json_serialisable(monkeypatch):
    _memory_store(monkeypatch)
    ok, _, saved = user_preferences.save_chart_template(4, {"name": "Ser", "workspace": _workspace()})
    assert ok
    assert json.loads(json.dumps(saved))["workspace"]["layoutMode"] == 4
