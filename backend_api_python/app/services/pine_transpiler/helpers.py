"""Pandas helper sources embedded into generated Strategy API V2 strategies.

The transpiler does not import pandas itself: every indicator a converted
strategy needs is emitted as a plain module-level Python function inside the
generated source. Keeping the helpers here as text means the emitted strategy
stays self-contained and the sandbox never needs TA-Lib.

Helper names are prefixed ``_p_`` so they cannot collide with identifiers that
survive from the Pine source.
"""

from __future__ import annotations

from typing import Final


BASE_UTILS: Final[str] = '''def _p_div(numerator, denominator):
    """Divide the way Pine does: division by zero yields ``na``, not an error."""
    if hasattr(denominator, "replace"):
        return numerator / denominator.replace(0.0, float("nan"))
    try:
        value = float(denominator)
    except (TypeError, ValueError):
        return numerator / denominator
    if value == 0.0:
        return float("nan")
    return numerator / denominator


def _p_mod(numerator, denominator):
    """Remainder with Pine semantics for a zero divisor."""
    if hasattr(denominator, "replace"):
        return numerator % denominator.replace(0.0, float("nan"))
    try:
        value = float(denominator)
    except (TypeError, ValueError):
        return numerator % denominator
    if value == 0.0:
        return float("nan")
    return numerator % denominator


def _p_as_series(value):
    """Widen a Pine scalar operand to a per-bar series.

    Pine's ``ta.*`` functions accept a scalar source (an ``input`` value or a
    loop variable) and treat it as a constant across bars. Pandas has no such
    notion, so wrap the program's bar frame instead of failing on ``.rolling``.
    """
    if hasattr(value, "rolling"):
        return value
    frame = _FRAME_REF[0]
    index = frame.index if frame is not None and len(frame) else None
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float("nan")
    if index is None:
        return pd.Series([number], dtype="float64")
    # Mirror the bar frame's index so arithmetic against real series aligns
    # instead of raising "Can only compare identically-labeled Series objects".
    return pd.Series([number] * len(index), index=index, dtype="float64")


def _p_window(length):
    return max(1, int(float(length)))


def _p_shift(value, count):
    """Shift a series by ``count`` bars, where positive counts look backwards."""
    if hasattr(value, "shift"):
        return value.shift(int(float(count)))
    return value


def _p_last(value):
    if hasattr(value, "iloc"):
        return value.iloc[-1] if len(value) else float("nan")
    return value


def _p_truth(value):
    """Collapse a series or scalar to a single boolean for the current bar."""
    last = _p_last(value)
    try:
        number = float(last)
    except (TypeError, ValueError):
        return bool(last)
    if number != number:
        return False
    return number != 0.0


def _p_mask(condition):
    """Normalise a Pine boolean context to an elementwise mask."""
    if not hasattr(condition, "fillna"):
        return bool(condition)
    filled = condition.fillna(0)
    try:
        return filled.astype("bool")
    except (TypeError, ValueError):
        pass
    try:
        return filled.astype("float64") != 0.0
    except (TypeError, ValueError):
        return filled.map(bool)


def _p_where(condition, when_true, when_false):
    if not hasattr(condition, "fillna"):
        return when_true if bool(condition) else when_false
    mask = _p_mask(condition)
    if hasattr(when_false, "mask") and hasattr(when_false, "copy"):
        result = when_false.copy()
        try:
            return result.mask(mask, when_true)
        except (TypeError, ValueError):
            pass
    result = mask.astype("object").copy()
    result[:] = when_false
    return result.mask(mask, when_true)


def _p_isna(value=None):
    if hasattr(value, "isna"):
        return value.isna()
    number = _p_last(value)
    if number is None:
        return True
    try:
        return bool(float(number) != float(number))
    except (TypeError, ValueError):
        return False


def _p_at(value, position):
    """Read one field of a Pine tuple result."""
    if isinstance(value, (tuple, list)):
        index = int(position)
        if 0 <= index < len(value):
            return value[index]
        return float("nan")
    return value


def _p_nz(value, replacement=0.0):
    if hasattr(value, "fillna"):
        return value.fillna(replacement)
    number = _p_last(value)
    if number != number:
        return replacement
    return number


def _p_fixnan(value):
    if hasattr(value, "ffill"):
        return value.ffill()
    return value


def _p_series(frame, column):
    if column in frame.columns:
        return frame[column]
    return frame.iloc[:, 0] * 0.0


def _p_time_series(frame):
    index = frame.index
    try:
        stamps = index.astype("int64") // 1000000
        return pd.Series(stamps, index=index, dtype="int64")
    except (TypeError, ValueError):
        return pd.Series(range(len(index)), index=index, dtype="int64")


def _p_matrix_new(size=0, fill=0.0):
    """Minimal matrix stand-in: a list of rows, enough for generic calls."""
    count = max(0, int(size))
    return [[fill for _ in range(count)] for _ in range(count)]


def _p_map_new():
    """Minimal map stand-in: an insertion-ordered dictionary."""
    return {}


def _p_matrix_get(matrix, row, column=None):
    if not isinstance(matrix, (list, tuple)):
        return float("nan")
    try:
        target = matrix[int(float(row))]
    except (IndexError, TypeError, ValueError):
        return float("nan")
    if column is None:
        return target
    try:
        return target[int(float(column))]
    except (IndexError, TypeError, ValueError):
        return float("nan")


def _p_matrix_set(matrix, row, column, value):
    if not isinstance(matrix, list):
        return value
    try:
        matrix[int(float(row))][int(float(column))] = value
    except (IndexError, TypeError, ValueError):
        return value
    return value


def _p_matrix_rows(matrix):
    return float(len(matrix)) if isinstance(matrix, (list, tuple)) else 0.0


def _p_matrix_columns(matrix):
    if isinstance(matrix, (list, tuple)) and matrix:
        return float(len(matrix[0]))
    return 0.0


def _p_map_put(mapping, key, value):
    if isinstance(mapping, dict):
        mapping[key] = value
    return value


def _p_map_get(mapping, key, default=None):
    """Return a map value.

    The default is a literal ``None`` because strategy source may only use
    literal function defaults; ``None`` still means "missing" here.
    """
    if not isinstance(mapping, dict):
        return float("nan") if default is None else default
    return mapping.get(key, float("nan") if default is None else default)


def _p_map_contains(mapping, key):
    return bool(isinstance(mapping, dict) and key in mapping)


def _p_map_remove(mapping, key):
    if not isinstance(mapping, dict):
        return float("nan")
    return mapping.pop(key, float("nan"))


def _p_map_size(mapping):
    return float(len(mapping)) if isinstance(mapping, dict) else 0.0


def _p_session_flag(value):
    """Return a constant session flag as a per-bar series."""
    return _p_as_series(1.0 if value else 0.0)


def _p_timestamp(*parts):
    """Build a bar timestamp from calendar components, like Pine's timestamp()."""
    frame = _FRAME_REF[0]
    if frame is None:
        return float("nan")
    index = pd.DatetimeIndex(frame.index)
    if not parts:
        return int(index[-1].value // 1000000)
    try:
        numbers = [int(_p_last(part)) for part in parts]
    except (TypeError, ValueError):
        return float("nan")
    year = numbers[0] if len(numbers) > 0 else 1970
    month = numbers[1] if len(numbers) > 1 else 1
    day = numbers[2] if len(numbers) > 2 else 1
    hour = numbers[3] if len(numbers) > 3 else 0
    minute = numbers[4] if len(numbers) > 4 else 0
    try:
        stamp = pd.Timestamp(year=year, month=month, day=day, hour=hour, minute=minute)
    except (TypeError, ValueError):
        return float("nan")
    return int(stamp.value // 1000000)


def _p_time(*parts):
    """Return the bar open time in milliseconds, as Pine's time() does."""
    return _p_time_series_value()


def _p_time_series_value():
    frame = _FRAME_REF[0]
    if frame is None:
        return float("nan")
    index = pd.DatetimeIndex(frame.index)
    return int(index[-1].value // 1000000)


def _p_time_component(name):
    """Return a per-bar calendar component, matching Pine's time builtins."""
    frame = _FRAME_REF[0]
    if frame is None:
        return pd.Series([float("nan")], dtype="float64")
    index = pd.DatetimeIndex(frame.index)
    parts = {
        "year": index.year,
        "month": index.month,
        "dayofmonth": index.day,
        "dayofweek": index.dayofweek,
        "hour": index.hour,
        "minute": index.minute,
        "second": index.second,
        "weekofyear": index.isocalendar().week,
    }
    values = parts.get(name, index.year)
    return pd.Series(values, index=frame.index, dtype="float64")


def _p_bar_index_series(frame):
    return pd.Series(range(len(frame)), index=frame.index, dtype="int64")


def _p_hist(history, count):
    """Read a bar-offset value from a tracked script-variable history."""
    if not history:
        return float("nan")
    offset = int(float(count))
    if offset < 0 or offset >= len(history):
        return float("nan")
    return history[-1 - offset]


def _p_prev(value, count):
    """Return the value ``count`` bars back, matching Pine's series indexing."""
    if hasattr(value, "iloc"):
        offset = int(float(count))
        if offset < 0 or offset >= len(value):
            return float("nan")
        return float(value.iloc[-1 - offset])
    return value


def _p_arange(count):
    return np.arange(max(0, int(count)), dtype="float64")


def _p_num(value):
    """Coerce a Pine numeric value (possibly None or NaN) to a float."""
    if value is None:
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _p_flag(value):
    """Coerce a Pine bool value to a Python bool."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if hasattr(value, "iloc"):
        return _p_truth(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return bool(value)
    if number != number:
        return False
    return number != 0.0


def _p_str(value, pattern=None):
    """Coerce any Pine value to its string form."""
    if value is None:
        return "nan"
    if isinstance(value, str):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:
        return "nan"
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return str(number)


def _p_str_upper(value):
    return _p_str(value).upper()


def _p_str_lower(value):
    return _p_str(value).lower()


def _p_str_contains(value, fragment):
    return _p_str(fragment) in _p_str(value)


def _p_and(first, second):
    left = _p_mask(first)
    right = _p_mask(second)
    left_series = hasattr(left, "astype")
    right_series = hasattr(right, "astype")
    if left_series and right_series:
        return left & right
    if left_series:
        return left & bool(right)
    if right_series:
        return bool(left) & right
    return bool(left) and bool(right)


def _p_or(first, second):
    left = _p_mask(first)
    right = _p_mask(second)
    left_series = hasattr(left, "astype")
    right_series = hasattr(right, "astype")
    if left_series and right_series:
        return left | right
    if left_series:
        return left | bool(right)
    if right_series:
        return bool(left) | right
    return bool(left) or bool(right)


def _p_not(value):
    mask = _p_mask(value)
    if isinstance(mask, bool):
        return not mask
    return ~mask


def _p_round(value, precision=None):
    if precision is None:
        result = np.round(value)
    else:
        result = np.round(value, int(_p_last(precision)))
    if hasattr(result, "iloc"):
        return result
    return float(result)


def _p_avg(*values):
    numbers = [_p_num(_p_last(item)) for item in values]
    numbers = [item for item in numbers if item == item]
    if not numbers:
        return float("nan")
    return float(sum(numbers)) / float(len(numbers))


def _p_math_sum(first, second=None, third=None):
    total = _p_num(_p_last(first))
    for extra in (second, third):
        if extra is not None:
            total += _p_num(_p_last(extra))
    return total


def _p_fill(value, frame):
    """Broadcast a scalar to a series aligned with the history frame."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float("nan")
    return pd.Series(number, index=frame.index, dtype="float64")


def _p_loop_range(start, end, step=None):
    """Return the integer sequence a Pine ``for ... to`` loop would walk."""
    first = int(_p_num(_p_last(start)))
    last = int(_p_num(_p_last(end)))
    if step is None or step == 0:
        stride = 1 if last >= first else -1
    else:
        stride = int(_p_num(_p_last(step)))
        if stride == 0:
            stride = 1
    stride = abs(stride) if last >= first else -abs(stride)
    span = abs(last - first) + 1
    if span > 5000:
        span = 5000
    return [first + stride * offset for offset in range(span)]
'''

_PREFIX: Final[str] = '''def _p_sma(series, length):
    window = _p_window(length)
    return _p_as_series(series).rolling(window, min_periods=window).mean()


def _p_ema(series, length):
    window = _p_window(length)
    return _p_as_series(series).ewm(span=window, adjust=False, min_periods=window).mean()


def _p_rma(series, length):
    window = _p_window(length)
    return _p_as_series(series).ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


def _p_wma(series, length):
    window = _p_window(length)
    weights = np.arange(1, window + 1, dtype="float64")
    total = float(weights.sum())

    def _weighted(window_values):
        return float(np.dot(np.asarray(window_values, dtype="float64"), weights) / total)

    return _p_as_series(series).rolling(window, min_periods=window).apply(_weighted, raw=True)


def _p_hma(series, length):
    window = _p_window(length)
    half = _p_window((window + 1) / 2.0)
    root = _p_window(round(window ** 0.5))
    return _p_wma(_p_wma(series, half) * 2.0 - _p_wma(series, window), root)


def _p_swma(series):
    weights = np.asarray([1.0, 2.0, 2.0, 1.0], dtype="float64")

    def _weighted(window_values):
        return float(np.dot(np.asarray(window_values, dtype="float64"), weights) / 6.0)

    return _p_as_series(series).rolling(4, min_periods=4).apply(_weighted, raw=True)


def _p_alma(series, length, offset=0.85, sigma=6.0):
    window = _p_window(length)
    positions = np.arange(window, dtype="float64")
    spread = window * float(sigma)
    pivot = -(float(offset) * (window - 1) - (window - 1) / 2.0)
    weights = np.exp(-((positions - (window - 1) + pivot) ** 2) / (2.0 * spread * spread))
    total = float(weights.sum())
    if total == 0.0:
        return series.rolling(window, min_periods=window).mean()

    def _weighted(window_values):
        return float(np.dot(np.asarray(window_values, dtype="float64"), weights) / total)

    return series.rolling(window, min_periods=window).apply(_weighted, raw=True)


def _p_vwma(series, volume, length):
    window = _p_window(length)
    weighted = (series * volume).rolling(window, min_periods=window).sum()
    normalizer = volume.rolling(window, min_periods=window).sum()
    return weighted / normalizer.replace(0.0, np.nan)
'''

_PRICE_STATS: Final[str] = '''def _p_stdev(series, length):
    window = _p_window(length)
    return series.rolling(window, min_periods=window).std(ddof=0)


def _p_variance(series, length):
    window = _p_window(length)
    return series.rolling(window, min_periods=window).var(ddof=0)


def _p_correlation(series, other, length):
    window = _p_window(length)
    left = _p_as_series(series)
    right = _p_as_series(other)
    return left.rolling(window, min_periods=window).corr(right)


def _p_linreg(series, length, offset=0):
    window = _p_window(length)
    positions = np.arange(1, window + 1, dtype="float64")
    centered = positions - positions.mean()
    denominator = float((centered * centered).sum())

    def _value(window_values):
        values = np.asarray(window_values, dtype="float64")
        slope = float((centered * (values - values.mean())).sum() / denominator)
        intercept = float(values.mean() - slope * positions.mean())
        return intercept + slope * (window + int(offset))

    return series.rolling(window, min_periods=window).apply(_value, raw=True)


def _p_cum(series):
    return series.cumsum()


def _p_change(series, length=1):
    return _p_as_series(series).diff(_p_window(length))


def _p_roc(series, length):
    window = _p_window(length)
    return _p_as_series(series).pct_change(window) * 100.0


def _p_mom(series, length):
    return series.diff(_p_window(length))


def _p_highest(series, length):
    window = _p_window(length)
    return _p_as_series(series).rolling(window, min_periods=window).max()


def _p_lowest(series, length):
    window = _p_window(length)
    return series.rolling(window, min_periods=window).min()


def _p_highestbars(series, length):
    window = _p_window(length)
    positions = np.arange(window, dtype="float64")

    def _value(window_values):
        values = np.asarray(window_values, dtype="float64")
        if np.isnan(values).all():
            return np.nan
        return float(positions[int(np.nanargmax(values))] - (window - 1))

    return series.rolling(window, min_periods=window).apply(_value, raw=True)


def _p_lowestbars(series, length):
    window = _p_window(length)
    positions = np.arange(window, dtype="float64")

    def _value(window_values):
        values = np.asarray(window_values, dtype="float64")
        if np.isnan(values).all():
            return np.nan
        return float(positions[int(np.nanargmin(values))] - (window - 1))

    return series.rolling(window, min_periods=window).apply(_value, raw=True)


def _p_barssince(condition):
    flags = _p_mask(condition).to_numpy() if hasattr(condition, "to_numpy") else None
    if flags is None:
        return 0.0
    counter = np.full(len(flags), np.nan)
    distance = 0
    seen = False
    for position in range(len(flags)):
        if flags[position]:
            seen = True
            distance = 0
            counter[position] = 0.0
        elif seen:
            distance += 1
            counter[position] = float(distance)
    return pd.Series(counter, index=condition.index)


def _p_valuewhen(condition, source, occurrence=0):
    flags = _p_mask(condition).to_numpy() if hasattr(condition, "to_numpy") else None
    values = source.to_numpy() if hasattr(source, "to_numpy") else None
    if flags is None or values is None:
        return source
    wanted = int(occurrence)
    output = np.full(len(flags), np.nan)
    hits = []
    for position in range(len(flags)):
        if flags[position]:
            hits.append(float(values[position]))
        target = len(hits) - 1 - wanted
        if target >= 0:
            output[position] = hits[target]
    return pd.Series(output, index=condition.index)


def _p_pivothigh(series, leftbars, rightbars):
    left = _p_window(leftbars)
    right = _p_window(rightbars)
    span = left + right + 1
    values = series.to_numpy()

    def _value(window_values):
        candidate = window_values[left]
        if np.isnan(candidate):
            return np.nan
        if np.nanmax(window_values) > candidate:
            return np.nan
        return float(candidate)

    pivots = series.rolling(span, min_periods=span).apply(_value, raw=True)
    del values
    return pivots.shift(-right).fillna(np.nan)


def _p_pivotlow(series, leftbars, rightbars):
    left = _p_window(leftbars)
    right = _p_window(rightbars)
    span = left + right + 1

    def _value(window_values):
        candidate = window_values[left]
        if np.isnan(candidate):
            return np.nan
        if np.nanmin(window_values) < candidate:
            return np.nan
        return float(candidate)

    pivots = series.rolling(span, min_periods=span).apply(_value, raw=True)
    return pivots.shift(-right).fillna(np.nan)
'''

_CROSSES: Final[str] = '''def _p_crossover(first, second):
    left = first if hasattr(first, "shift") else first
    right = second if hasattr(second, "shift") else second
    if not hasattr(left, "shift") or not hasattr(right, "shift"):
        return _p_truth(left)
    previous_left = left.shift(1)
    previous_right = right.shift(1)
    return (left > right) & (previous_left <= previous_right)


def _p_crossunder(first, second):
    left = first if hasattr(first, "shift") else first
    right = second if hasattr(second, "shift") else second
    if not hasattr(left, "shift") or not hasattr(right, "shift"):
        return not _p_truth(left)
    previous_left = left.shift(1)
    previous_right = right.shift(1)
    return (left < right) & (previous_left >= previous_right)


def _p_cross(first, second):
    return _p_crossover(first, second) | _p_crossunder(first, second)


def _p_rising(series, length):
    if not hasattr(series, "shift"):
        return True
    return series > series.shift(_p_window(length))


def _p_falling(series, length):
    if not hasattr(series, "shift"):
        return False
    return series < series.shift(_p_window(length))
'''

_VOLATILITY: Final[str] = '''def _p_true_range(high, low, close):
    previous = close.shift(1)
    ranges = pd.concat([(high - low), (high - previous).abs(), (low - previous).abs()], axis=1)
    return ranges.max(axis=1)


def _p_atr(high, low, close, length):
    return _p_rma(_p_true_range(high, low, close), length)


def _p_rsi(series, length):
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    average_gain = _p_rma(gain, length)
    average_loss = _p_rma(loss, length)
    relative = average_gain / average_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + relative.replace(0.0, np.nan)))


def _p_stoch(close, high, low, length):
    window = _p_window(length)
    lowest = low.rolling(window, min_periods=window).min()
    highest = high.rolling(window, min_periods=window).max()
    span = (highest - lowest).replace(0.0, np.nan)
    return 100.0 * (close - lowest) / span


def _p_macd(series, fast, slow, signal):
    fast_line = _p_ema(series, fast)
    slow_line = _p_ema(series, slow)
    line = fast_line - slow_line
    return line, _p_ema(line, signal), line - _p_ema(line, signal)


def _p_bollinger(series, length, multiplier):
    basis = _p_sma(series, length)
    deviation = _p_stdev(series, length) * float(multiplier)
    return basis, basis + deviation, basis - deviation


def _p_bbw(series, length, multiplier):
    basis, upper, lower = _p_bollinger(series, length, multiplier)
    return (upper - lower) / basis.replace(0.0, np.nan)


def _p_cci(high, low, close, length):
    typical = (high + low + close) / 3.0
    average = _p_sma(typical, length)
    deviation = (typical - average).abs().rolling(_p_window(length), min_periods=_p_window(length)).mean()
    return (typical - average) / (0.015 * deviation.replace(0.0, np.nan))


def _p_wpr(close, high, low, length):
    window = _p_window(length)
    highest = high.rolling(window, min_periods=window).max()
    lowest = low.rolling(window, min_periods=window).min()
    span = (highest - lowest).replace(0.0, np.nan)
    return -100.0 * (highest - close) / span


def _p_mfi(high, low, close, volume, length):
    typical = (high + low + close) / 3.0
    flow = typical * volume
    delta = typical.diff()
    positive = flow.where(delta > 0.0, 0.0).rolling(_p_window(length), min_periods=_p_window(length)).sum()
    negative = flow.where(delta < 0.0, 0.0).rolling(_p_window(length), min_periods=_p_window(length)).sum()
    ratio = positive / negative.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + ratio))


def _p_vwap(high, low, close, volume):
    typical = (high + low + close) / 3.0
    cumulative_flow = (typical * volume).cumsum()
    cumulative_volume = volume.cumsum().replace(0.0, np.nan)
    return cumulative_flow / cumulative_volume


def _p_supertrend(high, low, close, factor, length):
    average_range = _p_atr(high, low, close, length)
    midpoint = (high + low) / 2.0
    upper = (midpoint + float(factor) * average_range).to_numpy()
    lower = (midpoint - float(factor) * average_range).to_numpy()
    closes = close.to_numpy()
    size = len(closes)
    line = np.full(size, np.nan)
    direction = np.full(size, np.nan)
    final_upper = np.nan
    final_lower = np.nan
    trend = 1.0
    for position in range(size):
        if np.isnan(upper[position]) or np.isnan(lower[position]):
            continue
        if position > 0 and not np.isnan(final_upper):
            if closes[position - 1] <= final_upper:
                final_upper = min(upper[position], final_upper)
            else:
                final_upper = upper[position]
            if closes[position - 1] >= final_lower:
                final_lower = max(lower[position], final_lower)
            else:
                final_lower = lower[position]
            if closes[position] > final_upper:
                trend = -1.0
            elif closes[position] < final_lower:
                trend = 1.0
        else:
            final_upper = upper[position]
            final_lower = lower[position]
            if position > 0 and not np.isnan(closes[position - 1]):
                trend = -1.0 if closes[position] > (final_upper + final_lower) / 2.0 else 1.0
        line[position] = final_lower if trend < 0 else final_upper
        direction[position] = trend
    return pd.Series(line, index=close.index), pd.Series(direction, index=close.index)


def _p_percentrank(series, length):
    window = _p_window(length)

    def _value(window_values):
        values = np.asarray(window_values, dtype="float64")
        current = values[-1]
        if np.isnan(current):
            return np.nan
        return 100.0 * float((values < current).sum()) / float(len(values))

    return series.rolling(window, min_periods=window).apply(_value, raw=True)


def _p_dmi(high, low, close, length, smoothing):
    upward = high.diff()
    downward = -low.diff()
    plus_move = pd.Series(np.where((upward > downward) & (upward > 0.0), upward, 0.0), index=high.index)
    minus_move = pd.Series(np.where((downward > upward) & (downward > 0.0), downward, 0.0), index=high.index)
    true_range = _p_rma(_p_true_range(high, low, close), length).replace(0.0, np.nan)
    plus_di = 100.0 * _p_rma(plus_move, length) / true_range
    minus_di = 100.0 * _p_rma(minus_move, length) / true_range
    spread = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / spread
    return plus_di, minus_di, _p_rma(dx, smoothing)
'''

STATE_SUPPORT: Final[str] = '''def _state_get(key, fallback):
    """Read a persistent Pine ``var`` slot, initialising it on first use."""
    if key not in _STATE:
        _STATE[key] = fallback
    return _STATE[key]


def _state_set(key, value):
    """Write a persistent Pine ``var`` slot, collapsing series to this bar."""
    _STATE[key] = _p_last(value)
    return _STATE[key]


def _limited(value):
    """Clamp a Pine loop bound so generated loops always terminate."""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 0
    if limit > 10000:
        return 10000
    if limit < -10000:
        return -10000
    return limit


def _sign(value):
    """Return the sign of a loop step, defaulting to 1."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1
    if number < 0:
        return -1
    return 1
'''


RUNTIME_SUPPORT: Final[str] = '''def _p_ternary(condition, when_true, when_false):
    """Resolve a Pine conditional expression without eager truth testing."""
    if hasattr(condition, "fillna"):
        return _p_where(condition, when_true, when_false)
    if isinstance(condition, bool):
        return when_true if condition else when_false
    return when_true if _p_truth(condition) else when_false


def _p_position_size():
    """Return the current position size in units, or ``0.0`` when flat."""
    position = get_position(_SYMBOL)
    if position is None:
        return 0.0
    try:
        return float(position.amount)
    except (TypeError, ValueError):
        return 0.0


def _p_position_price():
    """Return the average entry price of the open position, or ``nan``."""
    position = get_position(_SYMBOL)
    if position is None:
        return float("nan")
    try:
        return float(position.avg_cost)
    except (TypeError, ValueError):
        return float("nan")


def _p_open_trades():
    return 1 if float(get_position(_SYMBOL).amount) != 0.0 else 0


def _p_col(frame, name):
    """Pick a history column by name, tolerating alternate column casing."""
    if name in frame.columns:
        return frame[name]
    lowered = str(name).lower()
    for column in frame.columns:
        if str(column).lower() == lowered:
            return frame[column]
    return frame.iloc[:, 0] * 0.0
'''


RUNTIME_HELPER_NAMES: Final[tuple[str, ...]] = (
    "ternary",
    "position_size",
    "position_price",
    "open_trades",
    "col",
)


ARRAY_SUPPORT: Final[str] = '''def _p_array_new(size=0, fill=0.0):
    count = max(0, int(size))
    return [fill for _ in range(count)]


def _p_array_get(values, position):
    index = int(position)
    if index < 0:
        index += len(values)
    if index < 0 or index >= len(values):
        return float("nan")
    return values[index]


def _p_array_set(values, position, value):
    index = int(position)
    if index < 0:
        index += len(values)
    if 0 <= index < len(values):
        values[index] = value
    return values


def _p_array_remove(values, position):
    index = int(position)
    if 0 <= index < len(values):
        values.pop(index)
    return values


def _p_array_push(values, value):
    values.append(value)
    return values


def _p_array_pop(values):
    if not values:
        return float("nan")
    return values.pop()


def _p_array_unshift(values, value):
    values.insert(0, value)
    return values


def _p_array_clear(values):
    values.clear()
    return values


def _p_array_index(values, value):
    for position, item in enumerate(values):
        if item == value:
            return position
    return -1


def _p_array_lastindex(values, value):
    for position in range(len(values) - 1, -1, -1):
        if values[position] == value:
            return position
    return -1


def _p_array_includes(values, value):
    return _p_array_index(values, value) >= 0


def _p_array_shift(values):
    if not values:
        return float("nan")
    return values.pop(0)


def _p_array_copy(values):
    return [item for item in values]


def _p_array_sort(values, order="asc"):
    items = [item for item in values]
    descending = str(order).lower().startswith("desc")
    return sorted(items, reverse=descending)


def _p_array_reverse(values):
    return [item for item in reversed(values)]


def _p_array_last(values):
    if not values:
        return float("nan")
    return values[-1]


def _p_array_from(*values):
    return [item for item in values]


def _p_array_median(values):
    return _p_array_stats(values, "median")


def _p_array_range(values):
    numbers = [float(item) for item in values if item == item]
    if not numbers:
        return float("nan")
    return float(max(numbers) - min(numbers))


def _p_array_stats(values, operation):
    numbers = []
    for item in values:
        collapsed = _p_last(item)
        try:
            number = float(collapsed)
        except (TypeError, ValueError):
            continue
        if number == number:
            numbers.append(number)
    if not numbers:
        return float("nan")
    if operation == "sum":
        return float(sum(numbers))
    if operation == "avg":
        return float(sum(numbers)) / float(len(numbers))
    if operation == "max":
        return float(max(numbers))
    if operation == "min":
        return float(min(numbers))
    if operation == "median":
        ordered = sorted(numbers)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return float(ordered[middle])
        return float((ordered[middle - 1] + ordered[middle]) / 2.0)
    total = sum(numbers)
    mean = total / float(len(numbers))
    variance = sum((item - mean) ** 2 for item in numbers) / float(len(numbers))
    if operation == "stdev":
        return float(variance ** 0.5)
    return float(variance)
'''

_GROUPS: Final[dict[str, tuple[str, ...]]] = {
    "prefix": (
        "sma", "ema", "rma", "wma", "hma", "swma", "alma", "vwma",
    ),
    "price_stats": (
        "stdev", "variance", "correlation", "linreg", "cum", "change", "roc", "mom",
        "highest", "lowest", "highestbars", "lowestbars", "barssince", "valuewhen",
        "pivothigh", "pivotlow",
    ),
    "crosses": ("crossover", "crossunder", "cross", "rising", "falling"),
    "volatility": (
        "tr", "true_range", "atr", "rsi", "stoch", "macd", "bollinger", "bb",
        "bbw", "cci", "wpr", "mfi", "vwap", "supertrend", "percentrank", "dmi",
    ),
    "array": (
        "array_new", "array_get", "array_set", "array_push", "array_pop",
        "array_shift", "array_unshift", "array_clear", "array_remove",
        "array_index", "array_lastindex", "array_includes", "array_stats",
        "array_copy", "array_sort", "array_last", "array_from",
        "array_median", "array_range", "array_reverse",
    ),
    "runtime": RUNTIME_HELPER_NAMES,
    "state": (),
}

_GROUP_SOURCES: Final[dict[str, str]] = {
    "prefix": _PREFIX,
    "price_stats": _PRICE_STATS,
    "crosses": _CROSSES,
    "volatility": _VOLATILITY,
    "array": ARRAY_SUPPORT,
    "runtime": RUNTIME_SUPPORT,
    "state": STATE_SUPPORT,
}

_HELPER_GROUP: Final[dict[str, str]] = {
    name: group for group, names in _GROUPS.items() for name in names
}


#: Blocks every generated strategy needs, independent of the indicator mix.
ALWAYS_EMITTED: Final[tuple[str, ...]] = ("state", "runtime")

#: Groups whose helpers call into another group. Emitting an indicator without
#: its dependencies leaves the generated module calling undefined helpers, which
#: the strategy contract rejects with ``undefinedGlobal``.
_GROUP_DEPENDENCIES: Final[dict[str, tuple[str, ...]]] = {
    "prefix": (),
    "price_stats": (),
    "crosses": (),
    "volatility": ("prefix", "price_stats"),
    "array": (),
    "runtime": (),
    "state": (),
}

#: Emit order. Helpers are defined in this order so a group can rely on any
#: earlier group being present.
_GROUP_ORDER: Final[tuple[str, ...]] = (
    "state",
    "runtime",
    "prefix",
    "price_stats",
    "crosses",
    "volatility",
    "array",
)


def _resolve_groups(names: set[str]) -> set[str]:
    """Expand indicator names into the full set of groups they require."""
    groups: set[str] = set(ALWAYS_EMITTED)
    pending = [group for group in (_HELPER_GROUP.get(name) for name in names) if group]
    while pending:
        group = pending.pop()
        if group in groups:
            continue
        groups.add(group)
        pending.extend(_GROUP_DEPENDENCIES.get(group, ()))
    return groups


def helper_source(used: set[str]) -> str:
    """Return the helper block needed for the requested indicator names."""
    groups = _resolve_groups(used)
    parts: list[str] = [BASE_UTILS]
    for group in _GROUP_ORDER:
        if group in groups:
            parts.append(_GROUP_SOURCES[group])
    return "\n\n".join(part.strip("\n") for part in parts)


def is_helper(name: str) -> bool:
    """Report whether a ``ta.*`` name has a pandas helper implementation."""
    return name in _HELPER_GROUP
