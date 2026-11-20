"""Pure helpers for resolving load multipliers."""

from __future__ import annotations

import math
import numbers
from typing import Optional

from ..const import (
    MULTIPLIER_MODE_FIXED,
    MULTIPLIER_MODE_OUTDOOR_TEMPERATURE,
)
from .models import MultiplierResult


def resolve_multipliers(
    *,
    mode: str = MULTIPLIER_MODE_FIXED,
    temperature_c: Optional[float] = None,
    preheat_factor: float = 1.5,
    recovery_factor: float = 1.2,
) -> MultiplierResult:
    """Resolve fixed settings or the legacy outdoor-temperature bands.

    Missing or implausible temperature data falls back to the configured
    factors. The function is deterministic and does not read HA entities.
    """
    if isinstance(preheat_factor, bool) or not isinstance(preheat_factor, numbers.Real):
        raise ValueError("preheat_factor must be numeric")
    if isinstance(recovery_factor, bool) or not isinstance(recovery_factor, numbers.Real):
        raise ValueError("recovery_factor must be numeric")
    preheat_factor = float(preheat_factor)
    recovery_factor = float(recovery_factor)
    if not math.isfinite(preheat_factor) or preheat_factor <= 0:
        raise ValueError("preheat_factor must be finite and greater than zero")
    if not math.isfinite(recovery_factor) or recovery_factor <= 0:
        raise ValueError("recovery_factor must be finite and greater than zero")
    if mode == MULTIPLIER_MODE_FIXED:
        return MultiplierResult(
            preheat_factor, recovery_factor, mode, "configured"
        )
    if mode != MULTIPLIER_MODE_OUTDOOR_TEMPERATURE:
        raise ValueError(f"unsupported multiplier mode: {mode!r}")

    try:
        temperature = float(temperature_c) if temperature_c is not None else math.nan
    except (TypeError, ValueError, OverflowError):
        temperature = math.nan
    if not math.isfinite(temperature) or not -60.0 <= temperature <= 60.0:
        return MultiplierResult(
            preheat_factor,
            recovery_factor,
            mode,
            "configured_fallback",
            warning="missing_or_invalid_temperature",
        )

    if temperature >= 7.0:
        preheat, recovery = 1.08, 1.04
    elif temperature >= 2.0:
        preheat, recovery = 1.14, 1.08
    elif temperature >= -3.0:
        preheat, recovery = 1.24, 1.12
    elif temperature >= -10.0:
        preheat, recovery = 1.34, 1.15
    else:
        preheat, recovery = 1.45, 1.20

    return MultiplierResult(
        preheat,
        recovery,
        mode,
        "outdoor_temperature_band",
        temperature_c=temperature,
    )
