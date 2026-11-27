"""Shared strict numeric validation helpers and native PVsyst version allow-lists."""
from __future__ import annotations

import math
from typing import Any

NATIVE_TEXT: tuple[str, ...] = ("8.1.6", "8.1.7")
NATIVE_TEXT_OR_SEED: tuple[str, ...] = ("8.1.0",) + NATIVE_TEXT
HEADLESS_RECORD_VERSION = "8.1.6"
SEED_RECORD_VERSION = NATIVE_TEXT_OR_SEED[0]
SYNTH_VERSION = NATIVE_TEXT[-1]
PV_TABLE_SOURCES = frozenset({("osPVCollada", "8.1.6"), ("osPVCollada", "8.1.7")})
CLI_TRACKING_BUILDS: tuple[str, ...] = (
    "PVsystCLI 8.1.6 (rev. 49278)",
    "PVsystCLI 8.1.7 (rev. 49382)",
)
TEXT_LABEL = "/".join(NATIVE_TEXT)
SEED_LABEL = "/".join(NATIVE_TEXT_OR_SEED)


def finite_number(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    strict_min: bool = False,
    positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{name} is too large to represent") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if positive and number <= 0.0:
        raise ValueError(f"{name} must be greater than zero")
    if minimum is not None and (number <= minimum if strict_min else number < minimum):
        op = ">" if strict_min else ">="
        raise ValueError(f"{name} must be {op} {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return number


def pos_finite(value: Any, name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{name} is too large to represent") from None
    if not math.isfinite(number) or (number < 0.0 if allow_zero else number <= 0.0):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'} and finite")
    return number


def bounded_number(value: Any, name: str, lo: float = -math.inf, hi: float = math.inf) -> float:
    """Validate a finite numeric value constrained to the closed interval [lo, hi]."""
    number = finite_number(value, name)
    if not lo <= number <= hi:
        raise ValueError(f"{name} must lie in [{lo}, {hi}]")
    return number


def is_strict_int(value: Any) -> bool:
    """Return True when value is a strict int (excluding bool)."""
    return type(value) is int


def is_finite_number(value: Any) -> bool:
    """Return True for non-boolean int/float values that are finite."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def is_printable_text(value: Any, limit: int | None = None) -> bool:
    """Return True for non-empty printable strings optionally bounded in length."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value.isprintable()
        and (limit is None or len(value) <= limit)
    )


def append_issue(result: dict[str, Any], field: str, message: Any) -> None:
    """Append a standardized validation issue entry to a structured result dict."""
    result["issues"].append({"field": field, "message": str(message)})


def append_warning(result: dict[str, Any], field: str, message: Any) -> None:
    """Append a ``{field, message}`` warning entry to ``result['warnings']``."""
    result["warnings"].append({"field": field, "message": str(message)})


def pos_int(value: Any, context: str, *, maximum: int = 2147483647) -> int:
    """Validate that value is a strict positive integer up to maximum."""
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{context} must be a positive 32-bit integer")
    return value


def clean_line_text(value: str, label: str) -> str:
    """Validate single-line non-empty text and return it stripped."""
    if not isinstance(value, str) or not value.strip() or any(ch in value for ch in ("\r", "\n", "\x00")):
        raise ValueError(f"{label} must be single-line non-empty text")
    return value.strip()
