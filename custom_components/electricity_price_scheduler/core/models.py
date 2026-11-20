"""Small immutable data contracts used by the pure optimizer core.

All times are timezone-aware UTC datetimes. Price values are expressed in the
configured currency per kWh. Savings model a 1 kW reference load.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple


@dataclass(frozen=True, slots=True)
class PriceSlot:
    """One priced half-open interval ``[start, end)``."""

    start: datetime
    end: datetime
    price_per_kwh: float


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    """Normalized provider data and non-sensitive source diagnostics."""

    slots: Tuple[PriceSlot, ...]
    currency: str
    provider: str
    coverage_start: Optional[datetime] = None
    coverage_end: Optional[datetime] = None
    tomorrow_available: bool = False
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OptimizerConfig:
    """Validated optimizer settings; all price thresholds use currency/kWh."""

    currency: str = "EUR"
    min_cutoff_minutes: int = 30
    max_cutoff_minutes: int = 300
    min_price_diff_per_kwh: float = 0.03
    max_periods: int = 6
    preheat_factor: float = 1.5
    recovery_factor: float = 1.2
    cutoff_factor: float = 0.0
    slot_minutes: int = 15


@dataclass(frozen=True, slots=True)
class MultiplierResult:
    """Resolved phase factors and diagnostic metadata."""

    preheat_factor: float
    recovery_factor: float
    mode: str
    source: str
    temperature_c: Optional[float] = None
    warning: Optional[str] = None


@dataclass(frozen=True, slots=True)
class CutoffPeriod:
    """A selected equal-duration preheat/cutoff/recovery schedule.

    ``savings_per_kw`` is the modeled currency saving for a 1 kW reference
    load over this period. It is not a measured bill saving.
    """

    preheat_start: datetime
    shutdown_start: datetime
    recovery_start: datetime
    recovery_end: datetime
    savings_per_kw: float
    savings_percent: Optional[float]
    baseline_cost_per_kw: float
    planned_cost_per_kw: float
    price_difference_per_kwh: float
    min_price_diff_per_kwh: float
    shutdown_duration_hours: float


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    """Optimizer output and bounded diagnostic counters."""

    periods: Tuple[CutoffPeriod, ...]
    total_savings_per_kw: float
    candidates_scanned: int
    candidates_accepted: int
    slot_count: int
    currency: str
    warnings: Tuple[str, ...] = ()
