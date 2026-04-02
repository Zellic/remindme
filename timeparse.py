from datetime import timedelta

# Note: "m" appears in both minutes and months in the original — minutes wins (first match).
_TIME_UNITS: list[tuple[frozenset[str], callable]] = [
    (frozenset(["s", "sec", "secs", "second", "seconds"]), lambda n: timedelta(seconds=n)),
    (frozenset(["m", "min", "mins", "minute", "minutes"]), lambda n: timedelta(minutes=n)),
    (frozenset(["h", "hr", "hrs", "hour", "hours"]),        lambda n: timedelta(hours=n)),
    (frozenset(["d", "day", "days"]),                        lambda n: timedelta(days=n)),
    (frozenset(["w", "week", "weeks"]),                      lambda n: timedelta(weeks=n)),
    (frozenset(["month", "months"]),                         lambda n: timedelta(days=n * 30)),
    (frozenset(["y", "yr", "yrs", "year", "years"]),         lambda n: timedelta(days=n * 365)),
]


def _split_into_parts(raw: str) -> list[str]:
    """Break string into alternating digit/letter tokens, ignoring commas and whitespace."""
    parts: list[str] = []
    buf: list[str] = []

    for ch in raw:
        if ch.isdigit() or ch.isalpha():
            if buf and (buf[-1].isalpha() != ch.isalpha()):
                parts.append("".join(buf))
                buf.clear()
            buf.append(ch)
        elif ch.isspace() or ch == ",":
            if buf:
                parts.append("".join(buf))
                buf.clear()
        else:
            raise ValueError(f"Invalid character for duration: {ch!r}")

    if buf:
        parts.append("".join(buf))

    return parts


def parse_duration(raw: str) -> timedelta:
    """
    Parse a human-readable duration string into a timedelta.

    Examples: "2h 30m", "2 hours 30 minutes", "9000", "2w", "1d 6h"
    A bare integer is treated as seconds.
    """
    raw = raw.strip()

    # Bare integer → seconds
    try:
        return timedelta(seconds=int(raw))
    except ValueError:
        pass

    parts = _split_into_parts(raw)

    if len(parts) % 2 != 0:
        raise ValueError(f"Unexpected token sequence in duration: {raw!r}")

    result = timedelta()
    for i in range(0, len(parts), 2):
        count_str, unit_str = parts[i], parts[i + 1]
        try:
            count = int(count_str)
        except ValueError:
            raise ValueError(f"Invalid number in duration: {count_str!r}")

        unit_fn = next(
            (fn for keys, fn in _TIME_UNITS if unit_str.lower() in keys),
            None,
        )
        if unit_fn is None:
            raise ValueError(f"Unknown time unit: {unit_str!r}")

        result += unit_fn(count)

    return result


def human_readable_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    if total <= 0:
        return "0 seconds"

    days    = total // 86400
    hours   = (total % 86400) // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60

    def fmt(n: int, unit: str) -> str:
        return f"{n} {unit}{'s' if n != 1 else ''}" if n > 0 else ""

    parts = list(filter(None, [
        fmt(days,    "day"),
        fmt(hours,   "hour"),
        fmt(minutes, "minute"),
        fmt(seconds, "second"),
    ]))
    return ", ".join(parts)
