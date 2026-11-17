"""Indicator workspace helpers shared by human routes and Agent Gateway."""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from app.services.indicator_default_template import build_default_indicator_template
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

_NON_INDICATOR_ASSET_TYPES = frozenset({"script_template"})


def is_indicator_ide_listable(*, code: str = "", asset_type: Optional[str] = "indicator") -> bool:
    """Whether a ``qd_indicator_codes`` row belongs in the indicator IDE sidebar."""
    del code
    at = (asset_type or "indicator").strip().lower()
    return at not in _NON_INDICATOR_ASSET_TYPES


def extract_indicator_meta_from_code(code: str) -> Dict[str, str]:
    """Parse ``my_indicator_name`` / ``my_indicator_description`` assignments."""
    if not code or not isinstance(code, str):
        return {"name": "", "description": ""}
    name_match = re.search(
        r'^\s*my_indicator_name\s*=\s*([\'"])(.*?)\1\s*$', code, re.MULTILINE,
    )
    desc_match = re.search(
        r'^\s*my_indicator_description\s*=\s*([\'"])(.*?)\1\s*$', code, re.MULTILINE,
    )
    name = (name_match.group(2).strip() if name_match else "")[:100]
    description = (desc_match.group(2).strip() if desc_match else "")[:500]
    return {"name": name, "description": description}


def get_indicator_authoring_contract() -> Dict[str, Any]:
    """Machine-readable contract + starter template for external AI agents."""
    template = build_default_indicator_template()
    return {
        "version": "indicator-contract-v3-pine-chart-only",
        "doc": "docs/trading/INDICATOR_DEV_GUIDE_CN.md",
        "workflow": [
            "1. Call this contract (or MCP get_indicator_authoring_contract) before writing code.",
            "2. Write a complete Pine Script v5/v6 chart study (not natural language, not Python) following required_fields.",
            "3. POST /api/agent/v1/indicators/validate to check that it compiles.",
            "4. POST /api/agent/v1/indicators to save into the user's indicator library (scope W).",
            "5. If the user wants backtest/live trading, use the separate Indicator-to-Strategy workflow. Do not add execution behavior to indicator code.",
        ],
        "required_fields": {
            "header": "Start with //@version=6. Version 5 also runs; anything older is refused.",
            "declaration": "Exactly one indicator(...) or strategy(...) call, on one line, near the top. Its first string argument is the study title, which becomes the saved indicator name.",
            "inputs": "Declare every knob with input.int/input.float/input.bool/input.string/input.color/input.source/input.timeframe and a title. The settings panel is built from these declarations.",
            "plots": "Draw with plot, plotshape, plotchar, plotcandle, bgcolor, barcolor, hline or fill. Series must be a Pine series of bar length; na leaves a gap.",
            "signals": "Chart markers only. Prefer one-bar events such as ta.crossover over continuous state, so notifications do not repeat every bar.",
            "alerts": "Optional alertcondition(...) calls; they do not execute trades.",
            "series": "Use the built-in series open/high/low/close/volume/time plus request.security for higher timeframes. There is no DataFrame and no params dict.",
        },
        "forbidden": [
            "Python source, pandas/numpy, df or params: indicators are Pine Script only",
            "Execution/order columns: open_long, close_long, open_short, close_short, add_long, add_short, reduce_long, reduce_short",
            "Strategy lifecycle or live-trading handlers",
            "Backtest or broker/account configuration inside indicator code",
            "Imports (import TradingView/ta/7) unless the bundled library resolver supplies them",
            "More than one indicator()/strategy() declaration",
            "Version below 2, or study(...) instead of indicator(...)",
            "Natural language instead of Pine source",
        ],
        "starter_template": template,
        "minimal_indicator_snippet": (
            "//@version=6\n"
            "indicator(\"Agent EMA Viewer\", overlay = true)\n"
            "fastLen = input.int(12, \"Fast EMA period\", minval = 1)\n"
            "slowLen = input.int(26, \"Slow EMA period\", minval = 2)\n"
            "emaFast = ta.ema(close, fastLen)\n"
            "emaSlow = ta.ema(close, slowLen)\n"
            "plot(emaFast, \"EMA Fast\", color = #FF9800)\n"
            "plot(emaSlow, \"EMA Slow\", color = #2D8CFF)\n"
            "plotshape(ta.crossover(emaFast, emaSlow), \"Cross Up\", shape.triangleup, location.belowbar, #22C55E)\n"
            "plotshape(ta.crossunder(emaFast, emaSlow), \"Cross Down\", shape.triangledown, location.abovebar, #EF4444)\n"
        ),
    }


def validate_indicator_code(code: str, indicator_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run the same validation pipeline as ``/api/indicator/verifyCode``."""
    from app.routes.indicator import _validate_indicator_code_internal

    return _validate_indicator_code_internal(code, indicator_params)


def save_user_indicator(
    *,
    user_id: int,
    code: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    indicator_id: int = 0,
) -> int:
    """Create or update a private (non-market) indicator row for the tenant."""
    raw = (code or "").strip()
    if not raw:
        raise ValueError("code is required")

    asset_type = "indicator"
    # ``validate_code_safety`` exists to fence the Python sandbox, and it starts
    # by ``ast.parse``-ing the source -- which rejects every Pine script. The
    # sandbox no longer executes indicators, so the compile check is the gate
    # that matters here; it is the same one the IDE's save path applies.
    from app.services.pine_validation import validate_pine_source

    verdict = validate_pine_source(raw)
    if not verdict.get("success"):
        raise ValueError(f"Invalid Pine Script: {verdict.get('details') or verdict.get('error_type')}")

    meta = extract_indicator_meta_from_code(raw)
    name = (name or meta.get("name") or "").strip() or "Custom Indicator"
    description = (description or meta.get("description") or "").strip()
    now = int(time.time())
    iid = int(indicator_id or 0)

    with get_db_connection() as db:
        cur = db.cursor()
        if iid > 0:
            cur.execute(
                """
                UPDATE qd_indicator_codes
                SET name = ?, code = ?, description = ?, asset_type = ?,
                    updatetime = ?, updated_at = NOW()
                WHERE id = ? AND user_id = ? AND (is_buy IS NULL OR is_buy = 0)
                """,
                (name, raw, description, asset_type, now, iid, int(user_id)),
            )
            if cur.rowcount == 0:
                cur.close()
                raise ValueError(f"Indicator {iid} not found or not editable")
        else:
            cur.execute(
                """
                INSERT INTO qd_indicator_codes
                  (user_id, is_buy, end_time, name, code, description,
                   publish_to_community, pricing_type, price, preview_image, vip_free, asset_type,
                   createtime, updatetime, created_at, updated_at)
                VALUES (?, 0, 1, ?, ?, ?, 0, 'free', 0, '', FALSE, ?, ?, ?, NOW(), NOW())
                """,
                (int(user_id), name, raw, description, asset_type, now, now),
            )
            iid = int(cur.lastrowid or 0)
        db.commit()
        cur.close()
    if iid <= 0:
        raise RuntimeError("Failed to persist indicator")
    return iid


def list_user_indicators(user_id: int, *, limit: int = 50) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 200))
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT id, user_id, is_buy, name, description, code,
                   COALESCE(asset_type, 'indicator') as asset_type,
                   createtime, updatetime
            FROM qd_indicator_codes
            WHERE user_id = ?
            ORDER BY id DESC
            """,
            (int(user_id),),
        )
        rows = cur.fetchall() or []
        cur.close()
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not is_indicator_ide_listable(
            code=r.get("code") or "",
            asset_type=r.get("asset_type") or "indicator",
        ):
            continue
        out.append(
            {
                "id": r.get("id"),
                "name": r.get("name") or "",
                "description": r.get("description") or "",
                "is_buy": int(r.get("is_buy") or 0),
                "createtime": r.get("createtime"),
                "updatetime": r.get("updatetime"),
            }
        )
        if len(out) >= limit:
            break
    return out


def get_user_indicator(user_id: int, indicator_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT id, user_id, is_buy, name, code, description, createtime, updatetime
            FROM qd_indicator_codes
            WHERE id = ? AND user_id = ?
            """,
            (int(indicator_id), int(user_id)),
        )
        row = cur.fetchone()
        cur.close()
    if not row:
        return None
    return {
        "id": row.get("id"),
        "name": row.get("name") or "",
        "description": row.get("description") or "",
        "code": row.get("code") or "",
        "is_buy": int(row.get("is_buy") or 0),
        "createtime": row.get("createtime"),
        "updatetime": row.get("updatetime"),
    }


def link_indicator_config(
    user_id: int,
    indicator_config: Optional[Dict[str, Any]],
    *,
    auto_save: bool = True,
) -> Dict[str, Any]:
    """Ensure an embedded indicator source has a persisted indicator ID."""
    ic = dict(indicator_config or {})
    code = (ic.get("indicator_code") or ic.get("code") or "").strip()
    if not code or not auto_save:
        return ic
    existing_id = ic.get("indicator_id")
    try:
        existing_id = int(existing_id) if existing_id not in (None, "", 0) else 0
    except (TypeError, ValueError):
        existing_id = 0

    if existing_id > 0:
        owned = get_user_indicator(user_id, existing_id)
        if owned:
            ic.setdefault("indicator_name", owned.get("name") or "")
            ic["indicator_code"] = code
            return ic

    meta = extract_indicator_meta_from_code(code)
    name = (ic.get("indicator_name") or meta.get("name") or "Agent Indicator").strip()
    description = (ic.get("indicator_description") or meta.get("description") or "").strip()
    try:
        new_id = save_user_indicator(
            user_id=user_id,
            code=code,
            name=name,
            description=description,
        )
    except Exception as exc:
        logger.warning(f"link_indicator_config: auto-save skipped: {exc}")
        return ic

    ic["indicator_id"] = new_id
    ic["indicator_name"] = name
    ic["indicator_description"] = description
    ic["indicator_code"] = code
    return ic
