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
