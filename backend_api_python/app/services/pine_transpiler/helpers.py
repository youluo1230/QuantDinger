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


BASE_UTILS: Final[str] = '''def _p_add(left, right):
    """Pine ``+`` which also concatenates when either side is a string."""
    if isinstance(left, str) or isinstance(right, str):
        return _p_str(left) + _p_str(right)
    return left + right


def _p_div(numerator, denominator):
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
    """Read a bar count, falling back to a single bar when it is ``na``.

    ``int(nan)`` raises, and a script that passes an unset ``input`` or a
    loop variable that never got a value would abort the backtest from inside a
    helper it never mentioned.
    """
    try:
        number = float(length)
    except (TypeError, ValueError):
        return 1
    if number != number or number in (float("inf"), float("-inf")):
        return 1
    return max(1, int(number))


def _p_shift(value, count):
    """Shift a series by ``count`` bars, where positive counts look backwards.

    Pine indexes with an ``na`` offset are ``na``: ``close[length]`` where
    ``length`` was never assigned gives a missing value, not an exception and not
    the current bar. Scripts reach this by indexing an array slot that is still
    empty and subtracting it from ``bar_index``, so converting the offset with
    ``int()`` used to abort the whole backtest on the first such bar.
    """
    try:
        offset = float(count)
    except (TypeError, ValueError):
        offset = float("nan")
    if offset != offset or offset in (float("inf"), float("-inf")):
        if hasattr(value, "shift"):
            return pd.Series(np.full(len(value), np.nan), index=value.index)
        return float("nan")
    if hasattr(value, "shift"):
        return value.shift(int(offset))
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


def _p_bool(value):
    """Pine ``bool()`` cast that never raises on a series."""
    last = _p_last(value)
    try:
        return bool(float(last))
    except (TypeError, ValueError):
        return bool(last)


def _p_mask(condition):
    """Normalise a Pine boolean context to an elementwise mask."""
    if not hasattr(condition, "fillna"):
        return bool(condition)
    # ``fillna(0).astype("bool")`` rebuilt the whole block through pandas'
    # generic machinery: 42us per call on a 300-bar frame, and a translator that
    # resolves a ternary per bar paid it millions of times. Going through numpy
    # costs 10us for the same answer, and ``nan != 0`` keeps the incumbent
    # reading of a missing condition as false.
    try:
        raw = condition.to_numpy(dtype="float64", na_value=0.0)
        return pd.Series(np.nan_to_num(raw) != 0.0, index=condition.index, copy=False)
    except (TypeError, ValueError):
        pass
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
    true_series = hasattr(when_true, "fillna")
    false_series = hasattr(when_false, "fillna")
    # Resolve through numpy whenever both branches can be handed to ``np.where``
    # as raw arrays. ``Series.mask`` copies the whole frame and re-enters pandas'
    # block manager (94us on a 300-bar frame); ``np.where`` costs 10us and yields
    # the same values. Strings and objects fall through to the mask path, which
    # preserves their dtype semantics.
    try:
        left = when_true.to_numpy() if true_series else when_true
        right = when_false.to_numpy() if false_series else when_false
        if not isinstance(left, str) and not isinstance(right, str):
            flags = mask.to_numpy() if hasattr(mask, "to_numpy") else bool(mask)
            return pd.Series(
                np.where(flags, left, right),
                index=condition.index,
                copy=False,
            )
    except (TypeError, ValueError):
        pass
    if false_series and hasattr(when_false, "mask"):
        try:
            return when_false.copy().mask(mask, when_true)
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
        index = _p_index(position)
        if index is not None and 0 <= index < len(value):
            return value[index]
        return float("nan")
    return value


def _p_index(value, default=None):
    """Read a Pine index as an integer, treating ``na`` as "no element".

    ``array.get(buf, na)`` and ``matrix.get(m, na, na)`` are legal Pine and
    yield ``na``; ``int(nan)`` raises instead, which aborted whole backtests
    from inside a helper the script never mentioned.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return int(number)


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
        stamps = index.asi8 // 1000
        return pd.Series(stamps, index=index, dtype="int64")
    except (TypeError, ValueError):
        return pd.Series(range(len(index)), index=index, dtype="int64")


def _p_matrix_new(rows=0, columns=None, fill=0.0):
    """Minimal matrix stand-in: a list of rows, enough for generic calls.

    Pine's ``matrix.new<float>(rows, columns, initial)`` takes three arguments,
    and scripts that fill a matrix do call it that way. The one-argument form is
    what the translator emits for a bare ``matrix.new<float>()``; it means an
    empty matrix, and a single number there used to build a square matrix the
    caller never asked for.
    """
    try:
        row_count = max(0, int(rows))
    except (TypeError, ValueError):
        row_count = 0
    if columns is None:
        column_count = 0 if row_count == 0 else row_count
    else:
        try:
            column_count = max(0, int(columns))
        except (TypeError, ValueError):
            column_count = 0
    if row_count == 0 or column_count == 0:
        return []
    return [[fill for _ in range(column_count)] for _ in range(row_count)]


def _p_map_new():
    """Minimal map stand-in: an insertion-ordered dictionary."""
    return {}


def _p_matrix_get(matrix, row, column=None):
    if not isinstance(matrix, (list, tuple)):
        return float("nan")
    row_index = _p_index(row)
    if row_index is None or not -len(matrix) <= row_index < len(matrix):
        return float("nan")
    target = matrix[row_index]
    if column is None:
        return target
    column_index = _p_index(column)
    if column_index is None or not isinstance(target, (list, tuple)):
        return float("nan")
    if not -len(target) <= column_index < len(target):
        return float("nan")
    return target[column_index]


def _p_matrix_set(matrix, row, column, value):
    if not isinstance(matrix, list):
        return value
    row_index = _p_index(row)
    column_index = _p_index(column)
    if row_index is None or column_index is None:
        return value
    try:
        matrix[row_index][column_index] = _p_last(value)
    except (IndexError, TypeError):
        return value
    return value


def _p_matrix_rows(matrix):
    return float(len(matrix)) if isinstance(matrix, (list, tuple)) else 0.0


def _p_matrix_columns(matrix):
    if isinstance(matrix, (list, tuple)) and matrix:
        return float(len(matrix[0]))
    return 0.0


def _p_matrix_row(matrix, index):
    """Pine ``matrix.row``: one row of the implied matrix."""
    if not isinstance(matrix, (list, tuple)):
        return []
    position = _p_index(index)
    if position is None or not -len(matrix) <= position < len(matrix):
        return []
    return list(matrix[position])


def _p_matrix_col(matrix, index):
    """Pine ``matrix.col``: one column, gathered across the rows."""
    if not isinstance(matrix, (list, tuple)):
        return []
    column = _p_index(index)
    if column is None:
        return []
    output = []
    for row in matrix:
        if isinstance(row, (list, tuple)) and 0 <= column < len(row):
            output.append(row[column])
    return output


def _p_matrix_transpose(matrix):
    """Pine ``matrix.transpose``: rows become columns."""
    if not isinstance(matrix, (list, tuple)) or not matrix:
        return []
    width = max((len(row) for row in matrix if isinstance(row, (list, tuple))), default=0)
    return [[row[column] if isinstance(row, (list, tuple)) and column < len(row) else 0.0
             for row in matrix]
            for column in range(width)]


def _p_matrix_multiply(left, right):
    """Pine ``matrix.mult``: the ordinary matrix product.

    The helper works on the list-of-rows representation the rest of this module
    uses, so a strategy that multiplies matrices gets the same numbers it would
    on TradingView rather than a dropped statement.
    """
    if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
        return []
    if not left or not right:
        return []
    inner = len(right)
    if any(len(row) != inner for row in right if isinstance(row, (list, tuple))):
        return []
    width = len(right[0]) if right and isinstance(right[0], (list, tuple)) else 0
    output = []
    for row in left:
        if not isinstance(row, (list, tuple)) or len(row) != inner:
            return []
        output.append([sum(float(row[k]) * float(right[k][column]) for k in range(inner))
                       for column in range(width)])
    return output


def _p_matrix_id(size):
    """Pine ``matrix.new`` with ones on the diagonal is how scripts write an id."""
    try:
        count = int(float(size))
    except (TypeError, ValueError):
        count = 0
    return [[1.0 if row == column else 0.0 for column in range(count)] for row in range(count)]


def _p_matrix_inverse(matrix):
    """Pine ``matrix.inv``: Gauss-Jordan on the list-of-rows representation.

    A singular matrix yields zeros rather than raising, because a Pine script
    that inverts a degenerate matrix and reads the result must still run bar
    after bar; TradingView returns NaN entries there.
    """
    if not isinstance(matrix, (list, tuple)) or not matrix:
        return []
    size = len(matrix)
    if any(not isinstance(row, (list, tuple)) or len(row) != size for row in matrix):
        return []
    augmented = [
        [float(value) for value in row]
        + [1.0 if index == column else 0.0 for column in range(size)]
        for index, row in enumerate(matrix)
    ]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            return [[0.0 for _ in range(size)] for _ in range(size)]
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor:
                augmented[row] = [value - factor * other
                                  for value, other in zip(augmented[row], augmented[column])]
    return [row[size:] for row in augmented]


def _p_field(record, name):
    """Read a field of a user-type record (Pine ``point.field``).

    Returns ``na`` when the record is missing or the field is not declared, so a
    typo in the field list surfaces as a missing value rather than an exception
    in the middle of a backtest.
    """
    if isinstance(record, dict):
        return record.get(name, float("nan"))
    return float("nan")


def _p_unpack(item, count):
    """Unpack a Pine tuple loop target, padding short records with ``na``."""
    values = item if isinstance(item, (tuple, list)) else [item]
    output = list(values[:count])
    while len(output) < count:
        output.append(float("nan"))
    return output


def _p_map_key(key):
    """Collapse a Pine map key to a hashable scalar.

    ``map.put(m, y * 100 + month, ...)`` builds its key from series, so the key
    arrives as a Series and a dict refuses it as an unhashable key. Pine keys
    are values of the current bar, same as the values stored against them.
    """
    if key is None or isinstance(key, (str, int, float, bool)):
        return key
    return _p_last(key)


def _p_map_put(mapping, key, value):
    """Store a map entry, collapsing a series to this bar."""
    if isinstance(mapping, dict):
        mapping[_p_map_key(key)] = _p_last(value)
    return value


def _p_map_get(mapping, key, default=None):
    """Return a map value.

    The default is a literal ``None`` because strategy source may only use
    literal function defaults; ``None`` still means "missing" here.
    """
    if not isinstance(mapping, dict):
        return float("nan") if default is None else default
    return mapping.get(_p_map_key(key), float("nan") if default is None else default)


def _p_map_contains(mapping, key):
    return bool(isinstance(mapping, dict) and _p_map_key(key) in mapping)


def _p_map_remove(mapping, key):
    if not isinstance(mapping, dict):
        return float("nan")
    return mapping.pop(_p_map_key(key), float("nan"))


def _p_map_keys(mapping):
    """Pine ``map.keys``: the keys as an array, empty when the map is ``na``."""
    if not isinstance(mapping, dict):
        return []
    return list(mapping.keys())


def _p_map_values(mapping):
    """Pine ``map.values``: the values as an array, empty when ``na``."""
    if not isinstance(mapping, dict):
        return []
    return list(mapping.values())


def _p_map_size(mapping):
    return float(len(mapping)) if isinstance(mapping, dict) else 0.0


def _p_session_flag(value):
    """Return a constant session flag as a per-bar series."""
    return _p_as_series(1.0 if value else 0.0)


def _p_timestamp(*parts):
    """Build a bar timestamp in milliseconds, like Pine's timestamp().

    Pine's ``timestamp()`` accepts calendar numbers *or* a date string, and every
    consumer compares the result against ``time``. Two things have to line up for
    that comparison to mean anything, and both were wrong here:

    * the unit. ``time`` is exposed in milliseconds, so returning microseconds
      made ``time >= timestamp(...)`` false on every bar, which silently turned
      a date-gated strategy into one that never enters;
    * the string form. ``timestamp("01-01-2023")`` is the shape TradingView
      writes in an ``input.time`` default, and ``int()`` cannot parse it, so the
      helper fell back to NaN — and a comparison against NaN is also always
      false. Strategies gated that way never traded even when the unit was right.
    """
    frame = _FRAME_REF[0]
    if frame is None:
        return float("nan")
    index = pd.DatetimeIndex(frame.index)
    if not parts:
        return int(index.asi8[-1] // 1000)
    last = parts[0]
    # A single string argument is a Pine date literal, not a calendar number.
    if len(parts) == 1 and isinstance(_p_last(last), str):
        text = str(_p_last(last)).strip()
        for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y"):
            try:
                stamp = pd.Timestamp(pd.to_datetime(text, format=fmt))
            except (TypeError, ValueError):
                continue
            return int(stamp.timestamp() * 1000)
        try:
            stamp = pd.Timestamp(pd.to_datetime(text))
        except (TypeError, ValueError):
            return float("nan")
        return int(stamp.timestamp() * 1000)
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
    return int(stamp.timestamp() * 1000)


def _p_time(*parts):
    """Return the bar open time in milliseconds, as Pine's time() does."""
    return _p_time_series_value()


def _p_time_series_value():
    frame = _FRAME_REF[0]
    if frame is None:
        return float("nan")
    index = pd.DatetimeIndex(frame.index)
    return int(index.asi8[-1] // 1000)


_TIME_COMPONENT_CACHE = {}


def _p_time_component(name):
    """Return a per-bar calendar component, matching Pine's time builtins.

    Cached because generated strategies call this once per bar inside loops, and
    rebuilding the field for the whole history on every call turned a single
    backtest into a multi-minute hang. The key uses the window bounds rather
    than ``id(frame)``, and no ``getattr`` is used, because the strategy
    contract rejects any call whose name is not bound in the module.
    """
    frame = _FRAME_REF[0]
    if frame is None:
        return pd.Series([float("nan")], dtype="float64")
    index = frame.index
    count = len(frame)
    key = (count, index[0], index[-1], name) if count else (0, None, None, name)
    cached = _TIME_COMPONENT_CACHE.get(key)
    if cached is not None:
        return cached
    dates = pd.DatetimeIndex(index)
    if name == "weekofyear":
        values = dates.isocalendar().week
    elif name == "dayofweek":
        values = dates.dayofweek
    elif name == "month":
        values = dates.month
    elif name == "dayofmonth":
        values = dates.day
    elif name == "hour":
        values = dates.hour
    elif name == "minute":
        values = dates.minute
    elif name == "second":
        values = dates.second
    else:
        values = dates.year
    series = pd.Series(values, index=index, dtype="float64")
    if len(_TIME_COMPONENT_CACHE) > 64:
        _TIME_COMPONENT_CACHE.clear()
    _TIME_COMPONENT_CACHE[key] = series
    return series


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


def _p_scalar_hist(key, value, count):
    """Return what a per-bar *expression* evaluated to ``count`` bars back.

    Pine treats every expression as a series, so ``array.get(buf, 1)[1]`` reads
    the value that same call returned on the previous bar. The translator renders
    such a call as a plain number, and ``_p_shift`` passes numbers through
    unchanged — which quietly turns ``x != x[1]`` into ``x != x`` and makes the
    condition permanently false. Remembering the recent results of that call site
    is the only way to honour the indexing.

    The key is generated per call site, so two identical-looking calls keep
    separate histories, exactly as two separate Pine series would. A site reached
    more than once in a bar records one sample per bar, which keeps the offsets
    aligned with bars rather than with evaluations.
    """
    slot = _STATE.get(key)
    if not isinstance(slot, dict):
        slot = {"bar": -1, "vals": []}
        _STATE[key] = slot
    try:
        bar = int(_bar_index)
    except NameError:
        bar = 0
    if slot["bar"] != bar:
        slot["vals"].append(_p_last(value))
        slot["bar"] = bar
        if len(slot["vals"]) > 64:
            del slot["vals"][: len(slot["vals"]) - 64]
    offset = int(float(count))
    history = slot["vals"]
    if offset < 0 or offset >= len(history):
        return float("nan")
    return history[-1 - offset]


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


def _p_str_fill(pattern, *values):
    """Pine's ``str.format``: a template with ``{0}``/``{1}`` placeholders.

    ``str.format`` itself is unavailable: the sandbox bans it because dotted
    format fields resolve attributes implicitly, bypassing the source-level
    dunder checks. The placeholder grammar Pine uses is small enough to
    substitute directly.
    """
    template = _p_str(pattern)
    rendered = [_p_str(value) for value in values]
    output = ""
    index = 0
    while index < len(template):
        character = template[index]
        if character != "{":
            output += character
            index += 1
            continue
        close = template.find("}", index + 1)
        if close < 0:
            output += template[index:]
            break
        slot = template[index + 1:close]
        if slot.isdigit() and int(slot) < len(rendered):
            output += rendered[int(slot)]
        else:
            output += template[index:close + 1]
        index = close + 1
    return output


def _p_str_length(value):
    """Pine ``str.length``; ``na`` counts as an empty string."""
    return len(_p_str(value))


def _p_str_tonumber(value):
    """Pine ``str.tonumber``: parse a numeric prefix, ``na`` when unparsable.

    The generated module may not import ``re`` (the sandbox allow-list is
    numpy/pandas/math/json), so the numeric prefix is collected by hand. Pine
    returns ``na`` rather than raising, and a timeframe input is a plain string
    like ``"15"`` or ``"1D"`` that callers test with ``na()``.
    """
    text = _p_str(value).strip()
    if not text:
        return float("nan")
    try:
        return float(text)
    except (TypeError, ValueError):
        pass
    allowed = set("0123456789+-.eE")
    prefix = ""
    for index, character in enumerate(text):
        if character not in allowed:
            break
        if index and character in "+-" and text[index - 1] not in "eE":
            break
        prefix += character
    try:
        return float(prefix)
    except (TypeError, ValueError):
        return float("nan")


def _p_str_split(value, separator=None):
    """Pine ``str.split``: ``na`` separator splits on whitespace."""
    text = _p_str(value)
    if separator is None or separator == "":
        return text.split()
    return text.split(_p_str(separator))


def _p_str_replace(value, needle, replacement):
    """Pine ``str.replace``: ``na`` needle or replacement is a no-op."""
    text = _p_str(value)
    target = _p_str(needle)
    if not target:
        return text
    return text.replace(target, _p_str(replacement))


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


def _p_nary(reducer, values):
    """Fold a binary numpy reducer over Pine's variadic argument list."""
    items = [item for item in values if item is not None]
    if not items:
        return float("nan")
    result = items[0]
    for item in items[1:]:
        result = reducer(result, item)
    return result


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


def _p_math_max(*values):
    """Pine ``math.max`` accepts any number of arguments; numpy caps at three."""
    return _p_nary(np.maximum, values)


def _p_math_min(*values):
    """Pine ``math.min`` accepts any number of arguments; numpy caps at three."""
    return _p_nary(np.minimum, values)


def _p_fill(value, frame):
    """Broadcast a scalar to a series aligned with the history frame."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float("nan")
    return pd.Series(number, index=frame.index, dtype="float64")


def _p_loop_count(value):
    """Coerce a loop bound to an int, reading ``na`` as zero.

    Pine lets a loop bound be ``na`` — a series that has not started yet, or a
    length an input left unset — and treats it as zero rather than refusing.
    ``int(nan)`` raises, which turned a legitimate empty loop into a runtime
    failure that said nothing about the script.
    """
    number = _p_num(_p_last(value))
    if number != number or number in (float("inf"), float("-inf")):
        return 0
    return int(number)


def _p_loop_range(start, end, step=None):
    """Return the integer sequence a Pine ``for ... to`` loop would walk."""
    first = _p_loop_count(start)
    last = _p_loop_count(end)
    if step is None or step == 0:
        stride = 1 if last >= first else -1
    else:
        stride = _p_loop_count(step)
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
        return _p_as_series(series).rolling(window, min_periods=window).mean()

    def _weighted(window_values):
        return float(np.dot(np.asarray(window_values, dtype="float64"), weights) / total)

    return _p_as_series(series).rolling(window, min_periods=window).apply(_weighted, raw=True)


def _p_vwma(series, length):
    """Pine's ``ta.vwma`` always weights by the bar's own volume."""
    window = _p_window(length)
    source = _p_as_series(series)
    weights = _p_as_series(volume_series)
    weighted = (source * weights).rolling(window, min_periods=window).sum()
    normalizer = weights.rolling(window, min_periods=window).sum()
    return weighted / normalizer.replace(0.0, np.nan)
'''

_PRICE_STATS: Final[str] = """def _p_stdev(series, length):
    window = _p_window(length)
    return _p_as_series(series).rolling(window, min_periods=window).std(ddof=0)


def _p_variance(series, length):
    window = _p_window(length)
    return _p_as_series(series).rolling(window, min_periods=window).var(ddof=0)


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

    return _p_as_series(series).rolling(window, min_periods=window).apply(_value, raw=True)


def _p_cum(series):
    return _p_as_series(series).cumsum()


def _p_change(series, length=1):
    return _p_as_series(series).diff(_p_window(length))


def _p_roc(series, length):
    window = _p_window(length)
    return _p_as_series(series).pct_change(window) * 100.0


def _p_mom(series, length):
    return _p_as_series(series).diff(_p_window(length))


def _p_highest(series, length):
    window = _p_window(length)
    return _p_as_series(series).rolling(window, min_periods=window).max()


def _p_lowest(series, length):
    window = _p_window(length)
    return _p_as_series(series).rolling(window, min_periods=window).min()


def _p_highestbars(series, length):
    window = _p_window(length)
    positions = np.arange(window, dtype="float64")

    def _value(window_values):
        values = np.asarray(window_values, dtype="float64")
        if np.isnan(values).all():
            return np.nan
        return float(positions[int(np.nanargmax(values))] - (window - 1))

    return _p_as_series(series).rolling(window, min_periods=window).apply(_value, raw=True)


def _p_lowestbars(series, length):
    window = _p_window(length)
    positions = np.arange(window, dtype="float64")

    def _value(window_values):
        values = np.asarray(window_values, dtype="float64")
        if np.isnan(values).all():
            return np.nan
        return float(positions[int(np.nanargmin(values))] - (window - 1))

    return _p_as_series(series).rolling(window, min_periods=window).apply(_value, raw=True)


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
    '''Pine ta.pivothigh; a scalar source has no neighbourhood, so na.

    Scripts feed indicator outputs here (ta.pivothigh(macdLine, l, r)) and an
    indicator that degraded to a scalar used to raise the numpy.float64
    has-no-attribute-to_numpy error from inside a helper it never mentioned.
    '''
    left = _p_window(leftbars)
    right = _p_window(rightbars)
    span = left + right + 1
    series = _p_as_series(series)

    def _value(window_values):
        candidate = window_values[left]
        if np.isnan(candidate):
            return np.nan
        if np.nanmax(window_values) > candidate:
            return np.nan
        return float(candidate)

    return series.rolling(span, min_periods=span).apply(_value, raw=True).shift(-right)


def _p_pivotlow(series, leftbars, rightbars):
    '''Pine ta.pivotlow; mirrors ta.pivothigh above.'''
    left = _p_window(leftbars)
    right = _p_window(rightbars)
    span = left + right + 1
    series = _p_as_series(series)

    def _value(window_values):
        candidate = window_values[left]
        if np.isnan(candidate):
            return np.nan
        if np.nanmin(window_values) < candidate:
            return np.nan
        return float(candidate)

    pivots = series.rolling(span, min_periods=span).apply(_value, raw=True)
    return pivots.shift(-right).fillna(np.nan)
"""

_CROSSES: Final[str] = """def _p_crossover(first, second):
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
"""

_VOLATILITY: Final[str] = """def _p_true_range(high, low, close):
    previous = close.shift(1)
    ranges = pd.concat([(high - low), (high - previous).abs(), (low - previous).abs()], axis=1)
    return ranges.max(axis=1)


def _p_atr(high, low, close, length):
    return _p_rma(_p_true_range(high, low, close), length)


def _p_rsi(series, length):
    delta = _p_as_series(series).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    average_gain = _p_rma(gain, length)
    average_loss = _p_rma(loss, length)
    relative = average_gain / average_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + relative.replace(0.0, np.nan)))


def _p_stoch(close, high, low, length):
    '''Pine ``ta.stoch(source, high, low, length)``.

    Scripts in the wild call it with numeric arguments in the ``high`` and
    ``low`` slots — ``ta.stoch(close, 14, 1, 3)`` — which reads as a length and
    two smoothing constants. Binning them or widening every number into a
    constant series would both produce a wrong channel, so a non-series band
    substitutes the source series instead.
    '''
    source = _p_as_series(close)
    window = _p_window(length)
    upper = high if hasattr(high, "rolling") else source
    lower = low if hasattr(low, "rolling") else source
    lowest = _p_as_series(lower).rolling(window, min_periods=window).min()
    highest = _p_as_series(upper).rolling(window, min_periods=window).max()
    span = (highest - lowest).replace(0.0, np.nan)
    return 100.0 * (source - lowest) / span


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
    typical = (_p_as_series(high) + _p_as_series(low) + _p_as_series(close)) / 3.0
    flow = typical * _p_as_series(volume)
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
    average_range = _p_atr(_p_as_series(high), _p_as_series(low), _p_as_series(close), _p_window(length))
    midpoint = (_p_as_series(high) + _p_as_series(low)) / 2.0
    multiplier = float(_p_last(factor))
    upper = (midpoint + multiplier * average_range).to_numpy()
    lower = (midpoint - multiplier * average_range).to_numpy()
    closes = _p_as_series(close).to_numpy()
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

    return _p_as_series(series).rolling(window, min_periods=window).apply(_value, raw=True)


def _p_dmi(high, low, close, length, smoothing):
    high = _p_as_series(high)
    low = _p_as_series(low)
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


def _p_percentile_nearest_rank(series, length, percentile):
    '''Return the value at the nearest rank inside the trailing window.

    An unmapped ``ta.*`` call degrades to ``close_series``. That is survivable
    for a filter and fatal for a strategy that compares the result against price:
    the comparison becomes "close versus close" and never fires, so the strategy
    backtests as if it never wanted to trade.
    '''
    window = _p_window(length)
    target = float(percentile) / 100.0

    def _pick(values):
        usable = values[~np.isnan(values)]
        if usable.size == 0:
            return np.nan
        ordered = np.sort(usable)
        rank = int(np.ceil(target * ordered.size)) - 1
        return float(ordered[min(max(rank, 0), ordered.size - 1)])

    return _p_as_series(series).rolling(window, min_periods=window).apply(_pick, raw=True)


def _p_percentile_linear_interpolation(series, length, percentile):
    '''Return the linearly interpolated percentile of the trailing window.'''
    window = _p_window(length)

    def _pick(values):
        usable = values[~np.isnan(values)]
        if usable.size == 0:
            return np.nan
        return float(np.percentile(usable, float(percentile)))

    return _p_as_series(series).rolling(window, min_periods=window).apply(_pick, raw=True)


def _p_sar(high, low, step, maximum):
    '''Return the parabolic stop-and-reverse level, statefully bar by bar.'''
    highs = _p_as_series(high)
    lows = _p_as_series(low)
    index = highs.index
    size = len(highs)
    out = np.full(size, np.nan)
    if size < 2:
        return pd.Series(out, index=index)
    upper = highs.to_numpy()
    lower = lows.to_numpy()
    rising = True
    acceleration = float(step)
    extreme = upper[0]
    level = lower[0]
    for position in range(1, size):
        level = level + acceleration * (extreme - level)
        if rising:
            if lower[position] < level:
                rising = False
                level = extreme
                extreme = lower[position]
                acceleration = float(step)
            elif upper[position] > extreme:
                extreme = upper[position]
                acceleration = min(acceleration + float(step), float(maximum))
        else:
            if upper[position] > level:
                rising = True
                level = extreme
                extreme = upper[position]
                acceleration = float(step)
            elif lower[position] < extreme:
                extreme = lower[position]
                acceleration = min(acceleration + float(step), float(maximum))
        out[position] = level
    return pd.Series(out, index=index)


def _p_cmo(series, length):
    '''Return the Chande momentum oscillator.'''
    window = _p_window(length)
    delta = _p_as_series(series).diff()
    gains = delta.clip(lower=0.0).rolling(window, min_periods=window).sum()
    losses = (-delta).clip(lower=0.0).rolling(window, min_periods=window).sum()
    total = (gains + losses).replace(0.0, np.nan)
    return 100.0 * (gains - losses) / total


def _p_tsi(series, short, long):
    '''Return the true strength index.'''
    delta = _p_as_series(series).diff()
    smoothed = _p_ema(_p_ema(delta, long), short)
    absolute = _p_ema(_p_ema(delta.abs(), long), short)
    return 100.0 * smoothed / absolute.replace(0.0, np.nan)


def _p_dema(series, length):
    '''Return the double exponential moving average.'''
    once = _p_ema(series, length)
    return 2.0 * once - _p_ema(once, length)


def _p_tema(series, length):
    '''Return the triple exponential moving average.'''
    once = _p_ema(series, length)
    twice = _p_ema(once, length)
    return 3.0 * once - 3.0 * twice + _p_ema(twice, length)


def _p_median(series, length):
    '''Return the rolling median.'''
    window = _p_window(length)
    return _p_as_series(series).rolling(window, min_periods=window).median()


def _p_dev(series, length):
    '''Return the mean absolute deviation from the moving average.'''
    window = _p_window(length)
    values = _p_as_series(series)
    average = _p_sma(values, length)
    return (values - average).abs().rolling(window, min_periods=window).mean()


def _p_cog(series, length):
    '''Return the center of gravity of the trailing window.'''
    window = _p_window(length)

    def _pick(values):
        usable = values[~np.isnan(values)]
        if usable.size == 0:
            return np.nan
        weights = np.arange(usable.size - 1, -1, -1, dtype="float64")
        total = float(usable.sum())
        if total == 0.0:
            return np.nan
        return float((weights * usable).sum() / total)

    return _p_as_series(series).rolling(window, min_periods=window).apply(_pick, raw=True)


def _p_kc(series, high, low, length, multiplier, use_ema=True, use_true_range=True):
    '''Return the Keltner channel as ``(basis, upper, lower)``.'''
    values = _p_as_series(series)
    basis = _p_ema(values, length) if use_ema else _p_sma(values, length)
    if use_true_range:
        span = _p_atr(_p_as_series(high), _p_as_series(low), values, length)
    else:
        span = _p_stdev(values, length)
    band = span * float(multiplier)
    return basis, basis + band, basis - band
"""

STATE_SUPPORT: Final[str] = '''def _state_get(key, fallback):
    """Read a persistent Pine ``var`` slot, initialising it on first use.

    A ``None`` slot means "not initialised yet": the module-level literal cannot
    hold ``float('nan')``, so ``na`` initialisers are stored as ``None`` and are
    replaced by the first read. Pine has no null value, so nothing is lost.
    """
    if key not in _STATE or _STATE[key] is None:
        _STATE[key] = fallback
    return _STATE[key]


def _state_set(key, value):
    """Write a persistent Pine ``var`` slot, collapsing series to this bar."""
    _STATE[key] = _p_last(value)
    return _STATE[key]


def _p_iter(value):
    """Return what a Pine ``for ... in`` should walk, tolerating ``na``.

    A Pine loop over a value that never became an array — a ``var`` slot still
    holding ``na``, or a series where the script expected a collection — simply
    runs zero times. Python raises ``TypeError`` for the same expression, which
    turned a script that had merely not filled its buffer yet into a runtime
    failure with no useful line.
    """
    if isinstance(value, (list, tuple, dict, set, str)):
        return value
    if hasattr(value, "tolist"):
        try:
            items = value.tolist()
        except (TypeError, ValueError):
            return ()
        return items if isinstance(items, list) else ()
    if value is None:
        return ()
    return ()


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


def _p_closed_trades():
    """Closed trades the runtime has booked so far.

    Pine's ``strategy.closedtrades`` counters are the backbone of most exit
    logic, so they must reflect the real ledger rather than a state slot nothing
    ever writes. Reads go through the runtime binding when it is present and
    degrade to an empty ledger during compile-time discovery.
    """
    try:
        # Bound by the runtime; absent during compile-time discovery, so a bare
        # name read (never a call) keeps the emitted code inside the allowed
        # global-call surface.
        reader = get_closed_trades
    except NameError:
        return []
    try:
        return list(reader() or ())
    except Exception:
        return []


def _p_closed_trade(index, field, default=None):
    """Read a field from a closed trade, counting back from the newest."""
    if default is None:
        default = float("nan")
    trades = _p_closed_trades()
    try:
        offset = int(float(index))
    except (TypeError, ValueError):
        offset = 0
    # Pine indexes back from the most recent closed trade: 0 is the newest.
    position = len(trades) - 1 - offset if offset >= 0 else -offset - 1
    if position < 0 or position >= len(trades):
        return default
    value = trades[position].get(field, default)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _p_open_trades():
    position = get_position(_SYMBOL)
    if position is None:
        return 0
    try:
        return 1 if float(position.amount) != 0.0 else 0
    except (TypeError, ValueError):
        return 0


def _p_int(value, default=0):
    """Pine ``int()`` cast that tolerates ``na`` (it yields the default)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return int(number)


def _p_float(value, default=None):
    """Pine ``float()`` cast that tolerates non-numeric input."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan") if default is None else default


def _p_open_trade_count():
    """Number of open positions (the runtime holds at most one)."""
    return float(_p_open_trades())


def _p_close_trade_count():
    """Number of trades the runtime has closed so far."""
    return float(len(_p_closed_trades()))


def _p_position_profit():
    """Unrealised PnL of the open position in quote currency."""
    position = get_position(_SYMBOL)
    if position is None:
        return float("nan")
    try:
        amount = float(position.amount)
        cost = float(position.avg_cost)
        last = float(position.last_price)
    except (TypeError, ValueError):
        return float("nan")
    return (last - cost) * amount


def _p_position_entry_bar():
    """Bar index at which the current position was opened, if tracked."""
    return float(_STATE.get("entry_bar", 0.0))


# Running win/loss tally, keyed by how much of the append-only ledger has been
# counted. Module level because the helper is re-emitted into every strategy and
# has to survive across bars; a fresh run starts from an empty ledger, which the
# shrinking-total guard in ``_p_trade_tally`` detects and resets.
_TRADE_TALLY = {"seen": 0, "wins": 0.0, "losses": 0.0}


def _p_trade_tally():
    """Running win/loss counts, rebuilt only when the ledger grows.

    Rescanning the whole ledger on every bar is quadratic: a strategy that checks
    ``strategy.wintrades`` in its entry condition walks one more closed trade per
    bar, so a month of one-minute bars with thousands of trades spends minutes in
    ``dict.get``. The ledger is append-only, so the tally only has to inspect the
    trades it has not seen yet.
    """
    trades = _p_closed_trades()
    total = len(trades)
    cache = _TRADE_TALLY
    seen = cache.get("seen", 0)
    wins = cache.get("wins", 0.0)
    losses = cache.get("losses", 0.0)
    if total < seen:
        # A shorter ledger than last time means a new run: start over.
        seen, wins, losses = 0, 0.0, 0.0
    for index in range(seen, total):
        try:
            profit = float(trades[index].get("profit") or 0.0)
        except (TypeError, ValueError, AttributeError, IndexError):
            continue
        if profit > 0.0:
            wins += 1.0
        elif profit < 0.0:
            losses += 1.0
    cache["seen"] = total
    cache["wins"] = wins
    cache["losses"] = losses
    return wins, losses


def _p_win_trade_count():
    """Closed trades that booked a profit."""
    return _p_trade_tally()[0]


def _p_loss_trade_count():
    """Closed trades that booked a loss."""
    return _p_trade_tally()[1]


def _p_closed_profit(index=0):
    return _p_closed_trade(index, "profit")


def _p_closed_size(index=0):
    return _p_closed_trade(index, "quantity")


def _p_closed_entry_time(index=0):
    return _p_closed_trade(index, "entry_time")


def _p_closed_exit_time(index=0):
    return _p_closed_trade(index, "exit_time")


def _p_closed_entry_price(index=0):
    return _p_closed_trade(index, "entry_price")


def _p_closed_exit_price(index=0):
    return _p_closed_trade(index, "exit_price")


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
    "open_trade_count",
    "close_trade_count",
    "position_profit",
    "position_entry_bar",
    "win_trade_count",
    "loss_trade_count",
    "closed_trades",
    "closed_trade",
    "closed_profit",
    "closed_size",
    "closed_entry_time",
    "closed_exit_time",
    "closed_entry_price",
    "closed_exit_price",
    "int",
    "float",
    "bool",
    "col",
    "str",
    "str_upper",
    "str_lower",
    "str_contains",
    "str_fill",
    "str_length",
    "array_size",
)


ARRAY_SUPPORT: Final[str] = '''def _p_array_new(size=0, fill=0.0):
    """Pine ``array.new_*``.

    ``array.new_color(na)`` is how Pine writes "an empty array", and ``na``
    arrives here as NaN. Running ``int(NaN)`` raises, so a script that only ever
    declared an array this way died on its first bar with "cannot convert float
    NaN to integer" — a message that points at the helper, not at the source line
    that said ``na``.
    """
    try:
        count = int(size)
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return []
    return [fill for _ in range(count)]


def _p_array_size(values):
    """Pine ``array.size``; a slot that was never filled is an empty array.

    ``var array<box> heat = array.new_box()`` starts as an empty list, so a
    generated read has to stay usable even when the slot was predeclared with a
    neutral scalar. Mapping this onto the builtin ``len`` crashed every script
    that sized an array before its first write.
    """
    try:
        return len(values)
    except TypeError:
        return 0


def _p_array_get(values, position):
    """Pine ``array.get``; a slot read before its first write is empty.

    ``var array<...>`` slots are predeclared with a neutral scalar and only
    become lists once the script assigns an array to them. Every accessor has to
    survive that state instead of calling ``len`` on a float.
    """
    if not isinstance(values, (list, tuple)):
        return float("nan")
    index = _p_index(position)
    if index is None:
        return float("nan")
    if index < 0:
        index += len(values)
    if index < 0 or index >= len(values):
        return float("nan")
    return values[index]


def _p_array_set(values, position, value):
    """Write one element of a Pine array.

    An array element is a single value: ``array.set(buf, i, someSeries)`` stores
    what that series read on the current bar, not the series itself. Keeping the
    whole object made later reads depend on how long the frame happened to be
    when the bar was stored, and comparing two such survivors raised
    "Can only compare identically-labeled Series objects".
    """
    if not isinstance(values, list):
        return values
    value = _p_last(value)
    index = _p_index(position)
    if index is None:
        return values
    if index < 0:
        index += len(values)
    if 0 <= index < len(values):
        values[index] = value
    return values


def _p_array_remove(values, position):
    """Remove one element; an ``na`` index removes nothing."""
    if not isinstance(values, list):
        return values
    index = _p_index(position)
    if index is not None and 0 <= index < len(values):
        values.pop(index)
    return values


def _p_array_push(values, value):
    """Append one element to a Pine array, collapsing a series to this bar.

    Same reason as ``_p_array_set``: an element is a value, not a series.
    """
    if not isinstance(values, list):
        return values
    values.append(_p_last(value))
    return values


def _p_array_pop(values):
    if not isinstance(values, list) or not values:
        return float("nan")
    return values.pop()


def _p_array_unshift(values, value):
    """Prepend one element, collapsing a series to this bar."""
    if not isinstance(values, list):
        return values
    values.insert(0, _p_last(value))
    return values


def _p_array_clear(values):
    if not isinstance(values, list):
        return values
    values.clear()
    return values


def _p_array_index(values, value):
    """Pine ``array.indexof``: compare this bar's value, never a whole series."""
    if not isinstance(values, (list, tuple)):
        return -1
    probe = _p_last(value)
    for position, item in enumerate(values):
        if item == probe:
            return position
    return -1


def _p_array_lastindex(values, value):
    """Pine ``array.lastindexof``; mirrors ``array.indexof`` above."""
    probe = _p_last(value)
    for position in range(len(values) - 1, -1, -1):
        if values[position] == probe:
            return position
    return -1


def _p_array_includes(values, value):
    return _p_array_index(values, value) >= 0


def _p_array_shift(values):
    if not values:
        return float("nan")
    return values.pop(0)


def _p_array_slice(values, start=0, end=None):
    if not isinstance(values, (list, tuple)):
        return []
    items = list(values)
    first = max(0, int(float(start)))
    if end is None:
        return items[first:]
    return items[first : int(float(end))]


def _p_array_join(values, separator=","):
    items = values if isinstance(values, (list, tuple)) else [values]
    return str(separator).join(str(_p_last(item)) for item in items)


def _p_array_concat(first, second):
    left = list(first) if isinstance(first, (list, tuple)) else []
    right = list(second) if isinstance(second, (list, tuple)) else []
    return left + right


def _p_array_insert(values, position, value):
    if not isinstance(values, list):
        return []
    values.insert(int(float(position)), value)
    return values


def _p_array_fill(values, value):
    if not isinstance(values, list):
        return []
    for index in range(len(values)):
        values[index] = value
    return values


def _p_array_copy(values):
    return [item for item in values]


def _p_sort_key(item):
    """Order user-type records by their first numeric field, else by identity.

    Pine sorts numbers and strings. A script that stores ``Type.new(...)``
    records in an array and sorts it relies on a field ordering the transpiler
    cannot see, so fall back to the first numeric field — and to a stable
    constant when there is none, which keeps the loop running instead of raising
    ``'<' not supported between instances of 'dict' and 'dict'``.
    """
    if isinstance(item, dict):
        for value in item.values():
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value == value:
                return float(value)
        return 0.0
    if isinstance(item, (int, float)):
        return float(item)
    if isinstance(item, str):
        return item
    return 0.0


def _p_array_sort(values, order="asc"):
    items = [item for item in values]
    descending = str(order).lower().startswith("desc")
    try:
        return sorted(items, reverse=descending)
    except TypeError:
        return sorted(items, key=_p_sort_key, reverse=descending)


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
    if not isinstance(values, (list, tuple)):
        return float("nan")
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
        "sma",
        "ema",
        "rma",
        "wma",
        "hma",
        "swma",
        "alma",
        "vwma",
    ),
    "price_stats": (
        "stdev",
        "variance",
        "correlation",
        "linreg",
        "cum",
        "change",
        "roc",
        "mom",
        "highest",
        "lowest",
        "highestbars",
        "lowestbars",
        "barssince",
        "valuewhen",
        "pivothigh",
        "pivotlow",
    ),
    "crosses": ("crossover", "crossunder", "cross", "rising", "falling"),
    "volatility": (
        "tr",
        "true_range",
        "atr",
        "rsi",
        "stoch",
        "macd",
        "bollinger",
        "bb",
        "bbw",
        "cci",
        "wpr",
        "mfi",
        "vwap",
        "supertrend",
        "percentrank",
        "dmi",
        "percentile_nearest_rank",
        "percentile_linear_interpolation",
        "sar",
        "cmo",
        "tsi",
        "dema",
        "tema",
        "median",
        "dev",
        "cog",
        "kc",
    ),
    "array": (
        "array_new",
        "array_get",
        "array_set",
        "array_push",
        "array_pop",
        "array_shift",
        "array_unshift",
        "array_clear",
        "array_remove",
        "array_index",
        "array_lastindex",
        "array_includes",
        "array_stats",
        "array_copy",
        "array_sort",
        "array_last",
        "array_from",
        "array_median",
        "array_range",
        "array_reverse",
        "array_slice",
        "array_join",
        "array_concat",
        "array_insert",
        "array_fill",
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

_HELPER_GROUP: Final[dict[str, str]] = {name: group for group, names in _GROUPS.items() for name in names}


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
