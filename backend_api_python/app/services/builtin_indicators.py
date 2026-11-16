"""
Seed the built-in sample indicators for new users (freely editable, deletable).

Idempotent on the first sample's name: a user who already has it is skipped, so
repeated calls to `create_user` and the like cannot insert it twice.

Indicators are Pine Script and nothing else. The samples this module seeded
before the switch were Python, so `upgrade_builtin_indicator_samples` also
rewrites any stored sample that is still Python -- otherwise every user would
keep a document the compiler now refuses to save.

The samples double as reference material: each one demonstrates a different part
of the drawing vocabulary (bands and markers, fills, histograms and levels), so
an author opening the IDE has a working example of whatever they want to draw.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from app.utils.logger import get_logger

logger = get_logger(__name__)


# Used for idempotency on registration. Keep in sync with _builtin_specs()[0]["name"].
_BUILTIN_PACK_ANCHOR_NAME = "[Sample] SuperTrend Trend-Following"

# The names the samples were seeded under before the Pine switch, mapped to the
# sample that now belongs to them. A row is rewritten only while it still
# carries a machine-written sample body (see `_is_untouched_sample`), so an
# author who edited their copy keeps their work.
_LEGACY_SAMPLE_NAMES: Dict[str, str] = {
    "SuperTrend Trend-Following": "[Sample] SuperTrend Trend-Following",
    "[Sample] SuperTrend Trend-Following": "[Sample] SuperTrend Trend-Following",
    "Dual EMA Viewer (官方示例)": "[Sample] Dual EMA Viewer",
    "[Sample] Dual EMA Viewer": "[Sample] Dual EMA Viewer",
    "Multi-Indicator Composite (官方示例)": "[Sample] Multi-Indicator Composite",
    "[Sample] Multi-Indicator Composite": "[Sample] Multi-Indicator Composite",
    # Truly old Python samples with no Pine counterpart yet; they become the
    # anchor sample rather than disappearing from a user's library.
    "[示例] 布林带触及": "[Sample] SuperTrend Trend-Following",
    "[示例] MACD 柱穿零轴": "[Sample] SuperTrend Trend-Following",
    "[示例] 双均线策略": "[Sample] SuperTrend Trend-Following",
    "[示例] RSI 超买超卖": "[Sample] SuperTrend Trend-Following",
    "Dual Moving Average Strategy": "[Sample] SuperTrend Trend-Following",
}


# Sample 1: a complete Pine study with a declaration, declared inputs, plots and
# markers. It is chart analysis only -- no strategy(), no orders -- because that
# is the boundary the Indicator IDE documents.
_SUPERTREND_CODE = r'''//@version=6
// SuperTrend Trend-Following -- classic ATR channel flip.
//
// Chart analysis only: this study draws bands and flip markers and never
// places an order. Convert it to a strategy to backtest the same rules.
//
// The bands may only tighten in the direction the trend already travels, so
// each one is carried forward until price forces a reset. That makes the
// series path-dependent, which is what `var` is for.
indicator("[Sample] SuperTrend Trend-Following", overlay = true)

atrPeriod = input.int(10, "ATR period", minval = 1, maxval = 200, group = "Settings")
multiplier = input.float(3.0, "ATR multiplier", minval = 0.5, maxval = 10.0, step = 0.1, group = "Settings")
showMarkers = input.bool(true, "Show flip markers", group = "Settings")

// Wilder-smoothed ATR (RMA), the recurrence professional charts use.
atr = ta.atr(atrPeriod)
hl2 = (high + low) / 2.0
upperBasic = hl2 + multiplier * atr
lowerBasic = hl2 - multiplier * atr

var float finalUpper = na
var float finalLower = na
var int direction = 0

if na(atr) or na(atr[1])
    // Warmup: no band has formed yet, so no direction exists either.
    direction := 0
else
    finalUpper := na(finalUpper) or upperBasic < finalUpper or close[1] > finalUpper ? upperBasic : finalUpper
    finalLower := na(finalLower) or lowerBasic > finalLower or close[1] < finalLower ? lowerBasic : finalLower
    // Compare against the previous bands only: no look-ahead.
    direction := close > finalUpper ? 1 : close < finalLower ? -1 : direction

up = direction == 1 and not na(finalLower) ? finalLower : na
down = direction == -1 and not na(finalUpper) ? finalUpper : na

plot(up, "SuperTrend up", color = #00e676, linewidth = 2)
plot(down, "SuperTrend down", color = #ff5252, linewidth = 2)

flippedUp = direction == 1 and direction[1] == -1
flippedDown = direction == -1 and direction[1] == 1
plotshape(showMarkers and flippedUp, title = "Long flip", style = shape.labelup, location = location.belowbar, color = #00e676, text = "L")
plotshape(showMarkers and flippedDown, title = "Short flip", style = shape.labeldown, location = location.abovebar, color = #ff5252, text = "S")
'''


# Sample 2: two averages, the gap between them shaded, and the crossovers marked.
# This is the sample to read when you want a fill or a marker.
_DUAL_EMA_CODE = r'''//@version=6
// Dual EMA Viewer -- two averages, the gap shaded, crossovers marked.
//
// Chart analysis only. The fill follows the two plots it names, so it is drawn
// exactly where the averages are and nowhere else.
indicator("[Sample] Dual EMA Viewer", overlay = true)

fastLength = input.int(12, "Fast EMA length", minval = 1, maxval = 500, group = "Settings")
slowLength = input.int(26, "Slow EMA length", minval = 1, maxval = 500, group = "Settings")
showFill = input.bool(true, "Shade the gap between the averages", group = "Settings")
showMarkers = input.bool(true, "Mark crossovers", group = "Settings")

fastEma = ta.ema(close, fastLength)
slowEma = ta.ema(close, slowLength)

// A fill needs the plot objects, not their values: name them so the two lines
// and the band between them stay one drawing.
fastLine = plot(fastEma, "Fast EMA", color = #2962FF, linewidth = 2)
slowLine = plot(slowEma, "Slow EMA", color = #B71C1C, linewidth = 2)
fill(fastLine, slowLine, color = showFill ? #2962FF1A : na, title = "Gap")

// The crossover is a one-bar event, which is what a marker can express.
plotshape(showMarkers and ta.crossover(fastEma, slowEma), title = "Bull cross", style = shape.triangleup, location = location.belowbar, color = #00e676, size = size.tiny)
plotshape(showMarkers and ta.crossunder(fastEma, slowEma), title = "Bear cross", style = shape.triangledown, location = location.abovebar, color = #ff5252, size = size.tiny)
'''


# Sample 3: a lower-pane study -- oscillator, fixed levels and a histogram.
# This is the sample to read when you want hlines or column styling.
_COMPOSITE_CODE = r'''//@version=6
// Multi-Indicator Composite -- oscillator, levels and a histogram in a lower pane.
//
// Chart analysis only. Declared without overlay so it draws in its own pane
// instead of on top of the candles.
indicator("[Sample] Multi-Indicator Composite", overlay = false)

rsiLength = input.int(14, "RSI length", minval = 2, maxval = 100, group = "RSI")
overbought = input.float(70.0, "Overbought level", minval = 50.0, maxval = 100.0, group = "RSI")
oversold = input.float(30.0, "Oversold level", minval = 0.0, maxval = 50.0, group = "RSI")
fastLength = input.int(12, "MACD fast length", minval = 1, maxval = 200, group = "MACD")
slowLength = input.int(26, "MACD slow length", minval = 2, maxval = 400, group = "MACD")
signalLength = input.int(9, "MACD signal length", minval = 1, maxval = 100, group = "MACD")

rsi = ta.rsi(close, rsiLength)
[macdLine, signalLine, histogram] = ta.macd(close, fastLength, slowLength, signalLength)

plot(rsi, "RSI", color = #7E57C2, linewidth = 2)

// Fixed levels are hlines: they span the whole pane and are not per-bar data.
hline(overbought, "Overbought", color = #ff5252, linestyle = hline.style_dashed)
hline(oversold, "Oversold", color = #00e676, linestyle = hline.style_dashed)

// A column plot colours each bar by its own sign.
plot(histogram, "MACD histogram", color = histogram >= 0 ? #26A69A : #EF5350, style = plot.style_columns)
plot(signalLine, "Signal", color = #FFB74D)
'''


def _builtin_specs() -> List[Dict[str, str]]:
    """The built-in samples: name / description / code, as the IDE stores them.

    Each sample is a complete Pine study that the save path's compiler accepts,
    and each one demonstrates a different drawing vocabulary. The first is the
    anchor for idempotency -- keep its name last or first, but do not rename it
    without also updating `_BUILTIN_PACK_ANCHOR_NAME` and the legacy name map.
    """
    return [
        {
            "name": _BUILTIN_PACK_ANCHOR_NAME,
            "description": (
                "Classic SuperTrend (ATR-channel direction flip): Wilder-smoothed ATR "
                "drives adaptive upper / lower bands that tighten with the trend, and a "
                "close beyond the previous band flips the drawn direction. Shows how to "
                "carry state across bars and mark a one-bar event."
            ),
            "code": _SUPERTREND_CODE,
        },
        {
            "name": "[Sample] Dual EMA Viewer",
            "description": (
                "Two EMAs with the gap between them shaded and crossovers marked. The "
                "sample to read when you want a fill between two plots or a marker on a "
                "one-bar event."
            ),
            "code": _DUAL_EMA_CODE,
        },
        {
            "name": "[Sample] Multi-Indicator Composite",
            "description": (
                "RSI with fixed overbought / oversold levels plus a MACD histogram, drawn "
                "in a lower pane. The sample to read when you want hlines, column styling, "
                "or a study that does not overlay the candles."
            ),
            "code": _COMPOSITE_CODE,
        },
    ]


def _spec_by_name() -> Dict[str, Dict[str, str]]:
    return {spec["name"]: spec for spec in _builtin_specs()}


def seed_builtin_indicators_for_new_user(db: Any, user_id: int) -> int:
    """
    Write the sample indicator pack after a successful registration.

    Skipped when the user already has a row carrying the anchor name, which makes
    the call idempotent. Returns how many rows this call inserted.
    """
    if not user_id:
        return 0
    now = int(time.time())
    cur = db.cursor()
    try:
        cur.execute(
            """
            SELECT 1 AS x
            FROM qd_indicator_codes
            WHERE user_id = ? AND name = ?
            LIMIT 1
            """,
            (user_id, _BUILTIN_PACK_ANCHOR_NAME),
        )
        if cur.fetchone():
            return 0

        inserted = 0
        for spec in _builtin_specs():
            cur.execute(
                """
                INSERT INTO qd_indicator_codes
                  (user_id, is_buy, end_time, name, code, description,
                   publish_to_community, pricing_type, price, preview_image, vip_free, review_status,
                   source_language, createtime, updatetime, created_at, updated_at)
                VALUES (?, 0, 1, ?, ?, ?, 0, 'free', 0, '', FALSE, NULL, 'pine', ?, ?, NOW(), NOW())
                """,
                (
                    user_id,
                    spec["name"],
                    spec["code"],
                    spec["description"],
                    now,
                    now,
                ),
            )
            inserted += 1
        db.commit()
        if inserted:
            logger.info("Seeded %s builtin indicator(s) for new user_id=%s", inserted, user_id)
        return inserted
    except Exception as e:
        logger.warning("seed_builtin_indicators_for_new_user failed user_id=%s: %s", user_id, e)
        try:
            db.rollback()
        except Exception:
            pass
        return 0
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _is_pine_source(code: str) -> bool:
    """Whether the stored text is Pine Script rather than a legacy Python sample.

    Delegates to the same detector the parameter parser uses, so a row cannot be
    classified one way for editing and another way here.
    """
    from app.services.indicator_params import looks_like_pine

    return bool(looks_like_pine(code or ""))


def _is_untouched_sample(code: str) -> bool:
    """Whether a stored body is exactly one of the sample bodies this module writes.

    A byte-identical body means the module wrote it, so rewriting it destroys
    nothing. A body that differs was edited by its author and is left alone -- no
    matter what the row is called.
    """
    text = str(code or "")
    return any(text == spec["code"] for spec in _builtin_specs())


def plan_duplicate_repair(rows: List[Dict[str, Any]], specs: List[Dict[str, str]]) -> List[tuple]:
    """Decide which duplicate sample rows should become a different sample.

    A pack of one sample per lesson can only be seeded once, but an earlier
    version of this module rewrote every legacy sample row to the same name. The
    rows left behind hold byte-identical machine bodies and nothing of the
    author's, so they can carry the samples the library is still missing.

    Only a row that is a byte-identical duplicate of a lower-id row with the same
    name is a candidate, and only a pack name the user has no row for at all is a
    destination. A row with unique content is never touched, and neither is a
    user's copy of a sample they edited.

    Returns `[(row_id, spec_name)]`.
    """
    order = [spec["name"] for spec in specs]
    by_spec = {spec["name"]: spec for spec in specs}

    per_user: Dict[Any, List[Dict[str, Any]]] = {}
    for row in rows:
        per_user.setdefault(row.get("user_id"), []).append(row)

    plan: List[tuple] = []
    for _user_id, user_rows in per_user.items():
        ordered = sorted(user_rows, key=lambda row: row.get("id") or 0)
        present = {str(row.get("name") or "") for row in ordered}
        seen: Dict[str, str] = {}
        for row in ordered:
            name = str(row.get("name") or "")
            body = str(row.get("code") or "")
            if name not in by_spec:
                continue
            if name not in seen:
                seen[name] = body
                continue
            # A second row with this name: only a machine copy can be spared.
            if body != seen[name]:
                continue
            spare = next((candidate for candidate in order if candidate not in present), None)
            if spare is None:
                continue
            plan.append((row.get("id"), spare))
            present.add(spare)
    return plan


def upgrade_builtin_indicator_samples() -> int:
    """
    Bring persisted official samples up to the current Pine pack.

    Two cases are rewritten: a row still holding one of the legacy Python
    samples, and a row holding a sample body that now belongs to a different
    entry of the pack (which happens when the pack is reordered or renamed). A
    row whose body no longer matches any sample was edited by its author and is
    skipped, as is a row outside the known sample names.
    """
    from app.utils.db import get_db_connection

    specs = _spec_by_name()
    if not specs:
        return 0

    # `specs` is keyed by name; the pack order is what the repair walks.
    order_names = [spec["name"] for spec in _builtin_specs()]
    legacy_names = tuple(_LEGACY_SAMPLE_NAMES.keys())
    placeholders = ",".join(["?"] * len(legacy_names))
    now = int(time.time())
    updated = 0

    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(
                f"""
                SELECT id, name, code
                FROM qd_indicator_codes
                WHERE (is_buy IS NULL OR is_buy = 0)
                  AND name IN ({placeholders})
                """,
                legacy_names,
            )
            rows = cur.fetchall() or []
            for row in rows:
                row_id = row.get("id") if isinstance(row, dict) else row[0]
                name = row.get("name") if isinstance(row, dict) else row[1]
                code = row.get("code") if isinstance(row, dict) else row[2]

                target_name = _LEGACY_SAMPLE_NAMES.get(str(name or ""))
                target = specs.get(target_name or "")
                if target is None:
                    continue

                # Already the right sample, word for word: nothing to do.
                if str(name or "") == target["name"] and str(code or "") == target["code"]:
                    continue

                # A body nobody wrote but this module: the author's work.
                if _is_pine_source(code or "") and not _is_untouched_sample(code or ""):
                    continue

                cur.execute(
                    """
                    UPDATE qd_indicator_codes
                    SET name = ?, code = ?, description = ?,
                        source_language = 'pine',
                        updatetime = ?, updated_at = NOW()
                    WHERE id = ?
                    """,
                    (
                        target["name"],
                        target["code"],
                        target["description"],
                        now,
                        row_id,
                    ),
                )
                updated += int(cur.rowcount or 0)

            # Phase two: rows an earlier version collapsed onto one sample name
            # are handed the samples the library is still missing.
            pack_slots = ",".join(["?"] * len(order_names))
            cur.execute(
                f"SELECT id, user_id, name, code FROM qd_indicator_codes WHERE name IN ({pack_slots})",
                tuple(order_names),
            )
            pack_rows = [dict(row) if not isinstance(row, dict) else row for row in (cur.fetchall() or [])]
            for row_id, spec_name in plan_duplicate_repair(pack_rows, _builtin_specs()):
                spec = specs[spec_name]
                cur.execute(
                    """
                    UPDATE qd_indicator_codes
                    SET name = ?, code = ?, description = ?,
                        source_language = 'pine',
                        updatetime = ?, updated_at = NOW()
                    WHERE id = ?
                    """,
                    (
                        spec["name"],
                        spec["code"],
                        spec["description"],
                        now,
                        row_id,
                    ),
                )
                updated += int(cur.rowcount or 0)
            db.commit()
            if updated:
                logger.info("Upgraded %s builtin indicator sample(s) to Pine", updated)
            return updated
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning("upgrade_builtin_indicator_samples failed: %s", e)
            return 0
        finally:
            try:
                cur.close()
            except Exception:
                pass
