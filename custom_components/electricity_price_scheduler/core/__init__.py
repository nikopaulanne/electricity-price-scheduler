"""Pure Python optimization core; no Home Assistant imports."""

from .models import (
    CutoffPeriod,
    MultiplierResult,
    OptimizationResult,
    OptimizerConfig,
    PriceSnapshot,
    PriceSlot,
)
from .multipliers import resolve_multipliers
from .normalization import PriceDataError, normalize_slots
from .optimizer import optimize
from .price_schedule import (
    PriceScheduleConfig,
    PriceScheduleError,
    PriceScheduleResult,
    plan_price_schedule,
)

__all__ = [
    "CutoffPeriod",
    "MultiplierResult",
    "OptimizationResult",
    "OptimizerConfig",
    "PriceSnapshot",
    "PriceSlot",
    "PriceDataError",
    "normalize_slots",
    "resolve_multipliers",
    "optimize",
    "PriceScheduleConfig",
    "PriceScheduleError",
    "PriceScheduleResult",
    "plan_price_schedule",
]
