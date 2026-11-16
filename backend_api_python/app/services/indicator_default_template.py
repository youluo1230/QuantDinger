"""Canonical default chart indicator template for QuantDinger.

The IDE authors Pine Script and the browser runs it, so the starter must be a
Pine study. It is also the fallback when the LLM is unavailable, which means a
user who never configured a model still gets something that draws.
"""

from __future__ import annotations


def build_default_indicator_template(
    *,
    name: str = "EMA Chart Indicator Template",
    description: str = "EMA crossover chart indicator with visual markers only.",
) -> str:
    """EMA crossover Pine study used as the starter and the offline fallback."""
    safe_name = (name or "EMA Chart Indicator Template").replace("\\", "\\\\").replace('"', '\\"')
    safe_desc = (description or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'''//@version=6
// ============================================================
// QuantDinger default chart indicator template
// ------------------------------------------------------------
// Indicators are chart-only. They do not open, close, size, or manage trades.
// Convert an indicator to a strategy before backtesting or live trading.
// ============================================================
indicator("{safe_name}", overlay = true)

// {safe_desc}

// ===== Tunable settings; every one appears in the indicator settings dialog =====
fastPeriod = input.int(10, "Fast EMA period", minval = 1, group = "Periods")
slowPeriod = input.int(30, "Slow EMA period", minval = 2, group = "Periods")
showMarkers = input.bool(true, "Show crossover markers", group = "Visuals")

emaFast = ta.ema(close, fastPeriod)
emaSlow = ta.ema(close, slowPeriod)

plot(emaFast, "EMA Fast", color = #FF9800, linewidth = 2)
plot(emaSlow, "EMA Slow", color = #3F51B5, linewidth = 2)

// A crossover is a one-bar event: mark the bar the state changed, not every bar
// the state persists, so notifications do not repeat.
golden = ta.crossover(emaFast, emaSlow)
death = ta.crossunder(emaFast, emaSlow)

plotshape(showMarkers and golden, "Golden cross", shape.triangleup, location.belowbar, #00E676, size = size.small)
plotshape(showMarkers and death, "Death cross", shape.triangledown, location.abovebar, #FF5252, size = size.small)

alertcondition(golden, "Golden cross", "EMA fast crossed above slow")
alertcondition(death, "Death cross", "EMA fast crossed below slow")
'''
