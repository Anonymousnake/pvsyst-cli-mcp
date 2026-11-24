"""Shared strict numeric validation helpers."""
from __future__ import annotations

import math
from typing import Any


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
