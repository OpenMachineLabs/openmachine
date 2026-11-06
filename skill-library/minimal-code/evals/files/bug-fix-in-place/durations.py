import re


def parse_duration(text: str) -> int:
    """Return seconds for strings like '90s', '5m' or '2h'."""
    match = re.fullmatch(r"(\d+)([smh])", text)
    if not match:
        raise ValueError(f"invalid duration: {text!r}")
    value, unit = match.groups()
    return int(value) * {"s": 1, "m": 60, "h": 3600}[unit]
