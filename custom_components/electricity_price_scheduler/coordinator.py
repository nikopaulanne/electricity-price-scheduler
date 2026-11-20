"""Price acquisition, bounded optimization, active-window locking and state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Literal

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_point_in_time, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DEFAULT_MAX_CUTOFF_MINUTES,
    DEFAULT_MAX_PERIODS,
    DEFAULT_MIN_CUTOFF_MINUTES,
    DEFAULT_MIN_PRICE_DIFF_PER_KWH,
    DEFAULT_PREHEAT_FACTOR,
    DEFAULT_RECOVERY_FACTOR,
    DEFAULT_MULTIPLIER_MODE,
    ACTIVE_WINDOW_STORE_VERSION,
    DOMAIN,
    MAX_CUTOFF_ENTITY_UNIT_HOURS,
    MAX_CUTOFF_ENTITY_UNIT_MINUTES,
    MAX_CUTOFF_MINUTES,
    MIN_PRICE_DIFF_ENTITY_UNIT_CURRENCY_PER_KWH,
    MIN_PRICE_DIFF_ENTITY_UNIT_SUBUNIT_PER_KWH,
    MULTIPLIER_MODE_OUTDOOR_TEMPERATURE,
    MAX_UI_MAX_PERIODS,
    SLOT_MINUTES,
    WARNING_MAX_CUTOFF_HELPER_INVALID,
    WARNING_MAX_CUTOFF_HELPER_UNAVAILABLE,
    WARNING_MIN_PRICE_DIFF_HELPER_INVALID,
    WARNING_MIN_PRICE_DIFF_HELPER_UNAVAILABLE,
)
from .core.models import (
    CutoffPeriod,
    MultiplierResult,
    OptimizationResult,
    OptimizerConfig,
    PriceSnapshot,
)
from .core.multipliers import resolve_multipliers
from .core.optimizer import optimize
from .providers import (
    ProviderConfig,
    ProviderError,
    ProviderUnavailableError,
    async_fetch_snapshot,
    provider_config,
    source_entities,
)

if TYPE_CHECKING:  # pragma: no cover - static typing only
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
_UTC = timezone.utc
_PROVIDER_REFRESH_INTERVAL = timedelta(hours=1)


class DataStatus(str, Enum):
    READY = "ready"
    WAITING_FOR_PRICES = "waiting_for_prices"
    NO_PROFITABLE_PERIODS = "no_profitable_periods"
    SOURCE_UNAVAILABLE = "source_unavailable"
    INVALID_DATA = "invalid_data"


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Immutable coordinator output consumed by every platform entity."""

    status: DataStatus
    available: bool
    price_snapshot: PriceSnapshot | None
    result: OptimizationResult | None
    phase: Literal["normal", "preheat", "shutdown", "recovery"] | None
    current_period: CutoffPeriod | None
    next_period: CutoffPeriod | None
    last_success: datetime | None
    calculated_at: datetime | None
    error_code: str | None
    warnings: tuple[str, ...] = ()
    effective_settings: dict[str, object] | None = None
    locked_period: CutoffPeriod | None = None
    locked_effective_settings: dict[str, object] | None = None
    next_change: datetime | None = None

    @property
    def decision_reason(self) -> str:
        if not self.available:
            return "source_unavailable" if self.status == DataStatus.SOURCE_UNAVAILABLE else "invalid_data"
        if self.current_period is not None and self.phase is not None:
            return self.phase
        if self.status == DataStatus.WAITING_FOR_PRICES:
            return "waiting_for_prices"
        if self.next_period is not None:
            return "next_cutoff_selected"
        return "no_cutoff_selected"


class OptimizerCoordinator(DataUpdateCoordinator[RuntimeSnapshot]):
    """Home Assistant data coordinator with one bounded calculation at a time."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, semaphore: asyncio.Semaphore):
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.title}",
            update_interval=_PROVIDER_REFRESH_INTERVAL,
            config_entry=entry,
            always_update=False,
        )
        self.hass = hass
        self.entry = entry
        self._semaphore = semaphore
        self._update_lock = asyncio.Lock()
        self._generation = 0
        self._active = True
        self._locked_period: CutoffPeriod | None = None
        self._locked_effective_settings: dict[str, object] | None = None
        self._locked_provenance_unknown = False
        self._last_price_snapshot: PriceSnapshot | None = None
        self._last_optimizer_config: OptimizerConfig | None = None
        self._last_multiplier: MultiplierResult | None = None
        self._last_result: OptimizationResult | None = None
        self._calculation_key: tuple[object, ...] | None = None
        self._last_success: datetime | None = None
        self._last_error_code: str | None = None
        self._dynamic_warnings: tuple[str, ...] = ()
        self._effective_settings: dict[str, object] | None = None
        self._active_window_store = Store(
            hass,
            ACTIVE_WINDOW_STORE_VERSION,
            f"{DOMAIN}.{entry.entry_id}.active_window",
        )
        self._settings_fingerprint = _entry_settings_fingerprint(entry)
        self._restore_checked = False
        self._store_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[object]] = set()
        self._remove_transition = None
        self._remove_state_listener = None

    async def async_start(self) -> None:
        """Begin source/temperature listeners after first data refresh."""
        entity_ids = set(source_entities(self.entry.data))
        temperature_id = self.entry.options.get("temperature_entity_id")
        if isinstance(temperature_id, str) and temperature_id:
            entity_ids.add(temperature_id)
        for option_key in ("max_cutoff_entity_id", "min_price_diff_entity_id"):
            entity_id = self.entry.options.get(option_key)
            if isinstance(entity_id, str) and entity_id:
                entity_ids.add(entity_id)
        if entity_ids:
            self._remove_state_listener = async_track_state_change_event(
                self.hass, entity_ids, self._async_handle_state_change
            )
        self._schedule_next_transition()

    async def async_shutdown(self) -> None:
        """Invalidate pending work and remove listeners/timers on unload."""
        if not self._active:
            return
        self._active = False
        self._generation += 1
        if self._remove_transition is not None:
            self._remove_transition()
            self._remove_transition = None
        if self._remove_state_listener is not None:
            self._remove_state_listener()
            self._remove_state_listener = None
        await super().async_shutdown()
        pending = tuple(self._background_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        async with self._update_lock:
            await self._async_persist_locked_period()

    def _create_background_task(self, coroutine) -> None:
        task = self.hass.async_create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _async_update_data(self) -> RuntimeSnapshot:
        from homeassistant.util import dt as dt_util

        async with self._update_lock:
            generation = self._generation + 1
            self._generation = generation
            now_utc = dt_util.utcnow().astimezone(_UTC)
            today_local = dt_util.now().date()
            try:
                await self._async_restore_locked_period(now_utc)
                source = provider_config(self.entry.data)
                options = dict(self.entry.options)
                config_options, self._dynamic_warnings, self._effective_settings = (
                    _resolve_helper_options_with_provenance(self.hass, options)
                )
                optimizer_config = _optimizer_config(source, config_options)
                snapshot = await async_fetch_snapshot(
                    self.hass,
                    source,
                    today_local=today_local,
                )
                self._last_success = now_utc
                multipliers = _resolve_entry_multipliers(self.hass, options)
                locked = self._locked_period
                if locked is not None and not _supports_locked_window(snapshot, locked, now_utc):
                    locked = None
                    self._locked_period = None
                key = _calculation_key(snapshot, optimizer_config, multipliers, locked)
                if key == self._calculation_key and self._last_result is not None:
                    result = self._last_result
                    new_lock = locked
                else:
                    result, new_lock = await self._async_calculate(
                        snapshot,
                        optimizer_config,
                        multipliers,
                        now_utc,
                        locked,
                    )
                if not self._active or generation != self._generation:
                    return self.data or _unavailable_snapshot(DataStatus.SOURCE_UNAVAILABLE)
                self._locked_period = new_lock
                if new_lock is None:
                    self._locked_effective_settings = None
                    self._locked_provenance_unknown = False
                elif locked != new_lock:
                    self._locked_effective_settings = self._effective_settings
                    self._locked_provenance_unknown = False
                elif (
                    self._locked_effective_settings is None
                    and not self._locked_provenance_unknown
                ):
                    self._locked_effective_settings = self._effective_settings
                await self._async_persist_locked_period()
                self._last_price_snapshot = snapshot
                self._last_optimizer_config = optimizer_config
                self._last_multiplier = multipliers
                self._last_result = result
                self._calculation_key = _calculation_key(
                    snapshot, optimizer_config, multipliers, new_lock
                )
                runtime = _runtime_snapshot(
                    snapshot,
                    result,
                    now_utc,
                    last_success=self._last_success,
                    multipliers=multipliers,
                    additional_warnings=self._dynamic_warnings,
                    effective_settings=self._effective_settings,
                    locked_period=new_lock,
                    locked_effective_settings=self._locked_effective_settings,
                )
                if self._last_error_code is not None:
                    _LOGGER.info(
                        "Price source recovered after %s", self._last_error_code
                    )
                    self._last_error_code = None
                self._schedule_next_transition(runtime)
                return runtime
            except ProviderError as err:
                await self._async_clear_calculation_state()
                status = (
                    DataStatus.SOURCE_UNAVAILABLE
                    if isinstance(err, ProviderUnavailableError)
                    else DataStatus.INVALID_DATA
                )
                self._log_error_transition(err.code, "Price source update failed")
                runtime = _unavailable_snapshot(
                    status,
                    error_code=err.code,
                    last_success=self._last_success,
                    calculated_at=now_utc,
                    effective_settings=self._effective_settings,
                )
                self._schedule_next_transition(runtime)
                return runtime
            except (ValueError, TypeError, OverflowError) as err:
                await self._async_clear_calculation_state()
                self._log_error_transition("invalid_data", "Optimizer input was invalid")
                runtime = _unavailable_snapshot(
                    DataStatus.INVALID_DATA,
                    error_code="invalid_data",
                    last_success=self._last_success,
                    calculated_at=now_utc,
                    effective_settings=self._effective_settings,
                )
                self._schedule_next_transition(runtime)
                return runtime
            except Exception:
                await self._async_clear_calculation_state()
                self._log_error_transition(
                    "calculation_failed", "Unexpected optimizer update failure"
                )
                runtime = _unavailable_snapshot(
                    DataStatus.INVALID_DATA,
                    error_code="calculation_failed",
                    last_success=self._last_success,
                    calculated_at=now_utc,
                    effective_settings=self._effective_settings,
                )
                self._schedule_next_transition(runtime)
                return runtime

    async def _async_restore_locked_period(self, now_utc: datetime) -> None:
        """Restore only an active lock tied to this exact profile configuration."""
        if self._restore_checked:
            return
        self._restore_checked = True
        try:
            stored = await self._active_window_store.async_load()
        except Exception:
            _LOGGER.warning("Could not load persisted active cutoff window")
            return
        if stored is None:
            return
        period = _period_from_store(stored)
        if (
            period is None
            or not isinstance(stored, dict)
            or stored.get("settings_fingerprint") != self._settings_fingerprint
            or not period.preheat_start <= now_utc < period.recovery_end
        ):
            self._locked_period = None
            await self._async_persist_locked_period()
            return
        self._locked_period = period
        stored_settings = stored.get("effective_settings")
        self._locked_effective_settings = (
            dict(stored_settings) if isinstance(stored_settings, dict) else None
        )
        self._locked_provenance_unknown = self._locked_effective_settings is None

    async def _async_persist_locked_period(self) -> None:
        """Flush the current active lock, or remove storage when no lock exists."""
        async with self._store_lock:
            period = self._locked_period
            try:
                if period is None:
                    await self._active_window_store.async_remove()
                    return
                payload = _period_to_store(period)
                payload["settings_fingerprint"] = self._settings_fingerprint
                payload["effective_settings"] = self._locked_effective_settings
                await self._active_window_store.async_save(payload)
            except Exception:
                _LOGGER.exception("Could not persist active cutoff window")

    async def _async_clear_calculation_state(self) -> None:
        self._clear_calculation_state()
        await self._async_persist_locked_period()

    async def _async_calculate(
        self,
        snapshot: PriceSnapshot,
        config: OptimizerConfig,
        multiplier: MultiplierResult,
        now_utc: datetime,
        locked: CutoffPeriod | None,
    ) -> tuple[OptimizationResult, CutoffPeriod | None]:
        generation = self._generation
        async with self._semaphore:
            job = self.hass.async_add_executor_job(
                _calculate_result,
                snapshot,
                config,
                multiplier,
                now_utc,
                locked,
            )
            try:
                result, new_lock = await asyncio.shield(job)
            except asyncio.CancelledError:
                # Keep the per-HA semaphore held until the executor thread has
                # actually stopped. Cancelling its asyncio wrapper alone does
                # not terminate Python work already running in a thread.
                try:
                    await asyncio.shield(job)
                except Exception:
                    pass
                raise
        if not self._active or generation != self._generation:
            raise asyncio.CancelledError
        return result, new_lock

    async def _async_handle_state_change(self, event: object) -> None:
        if self._active:
            await self.async_request_refresh()

    async def _async_handle_transition(self, now: datetime) -> None:
        from homeassistant.util import dt as dt_util

        if not self._active or not self.data or not self.data.available:
            self._schedule_next_transition()
            return
        now_utc = now.astimezone(_UTC) if now.tzinfo else dt_util.utcnow()
        if self._locked_period is not None and now_utc >= self._locked_period.recovery_end:
            # A daily source rotation and a finished lock release the reserved
            # K slot. Replan from cached prices; do not refetch at a phase edge.
            self._locked_period = None
            self._locked_effective_settings = None
            self._locked_provenance_unknown = False
            self._calculation_key = None
            await self._async_persist_locked_period()
            self._create_background_task(
                self._async_recalculate_cached(now_utc, locked_override=None)
            )
            return
        if self._locked_period is None:
            for period in self.data.result.periods if self.data.result else ():
                if period.preheat_start <= now_utc < period.recovery_end:
                    self._locked_period = period
                    self._locked_effective_settings = self._effective_settings
                    self._locked_provenance_unknown = False
                    await self._async_persist_locked_period()
                    self._create_background_task(
                        self._async_recalculate_cached(now_utc, locked_override=period)
                    )
                    return
        runtime = _runtime_snapshot(
            self._last_price_snapshot,
            self._last_result,
            now_utc,
            last_success=self._last_success,
            multipliers=self._last_multiplier,
            additional_warnings=self._dynamic_warnings,
            effective_settings=self._effective_settings,
            locked_period=self._locked_period,
            locked_effective_settings=self._locked_effective_settings,
        )
        self.async_set_updated_data(runtime)
        self._schedule_next_transition(runtime)

    async def _async_recalculate_cached(
        self, now_utc: datetime, *, locked_override: CutoffPeriod | None
    ) -> None:
        async with self._update_lock:
            if (
                not self._active
                or self._last_price_snapshot is None
                or self._last_optimizer_config is None
                or self._last_multiplier is None
            ):
                return
            self._generation += 1
            generation = self._generation
            try:
                result, locked = await self._async_calculate(
                    self._last_price_snapshot,
                    self._last_optimizer_config,
                    self._last_multiplier,
                    now_utc,
                    locked_override,
                )
            except asyncio.CancelledError:
                return
            except Exception:
                _LOGGER.exception("Could not recalculate after a cutoff window transition")
                await self._async_clear_calculation_state()
                runtime = _unavailable_snapshot(
                    DataStatus.INVALID_DATA,
                    error_code="calculation_failed",
                    last_success=self._last_success,
                    calculated_at=now_utc,
                    effective_settings=self._effective_settings,
                )
                self.async_set_updated_data(runtime)
                self._schedule_next_transition(runtime)
                return
            if not self._active or generation != self._generation:
                return
            self._locked_period = locked
            if locked is None:
                self._locked_effective_settings = None
                self._locked_provenance_unknown = False
            elif self._locked_effective_settings is None and not self._locked_provenance_unknown:
                self._locked_effective_settings = self._effective_settings
            await self._async_persist_locked_period()
            self._last_result = result
            if self._last_price_snapshot is not None:
                self._calculation_key = _calculation_key(
                    self._last_price_snapshot,
                    self._last_optimizer_config,
                    self._last_multiplier,
                    locked,
                )
            runtime = _runtime_snapshot(
                self._last_price_snapshot,
                result,
                now_utc,
                last_success=self._last_success,
                multipliers=self._last_multiplier,
                additional_warnings=self._dynamic_warnings,
                effective_settings=self._effective_settings,
                locked_period=locked,
                locked_effective_settings=self._locked_effective_settings,
            )
            self.async_set_updated_data(runtime)
            self._schedule_next_transition(runtime)

    def _schedule_next_transition(self, snapshot: RuntimeSnapshot | None = None) -> None:
        from homeassistant.util import dt as dt_util

        if self._remove_transition is not None:
            self._remove_transition()
            self._remove_transition = None
        runtime = snapshot or self.data
        if not self._active or runtime is None or not runtime.available or runtime.result is None:
            return
        now = dt_util.utcnow().astimezone(_UTC)
        boundaries: list[datetime] = []
        for period in runtime.result.periods:
            boundaries.extend(
                value
                for value in (
                    period.preheat_start,
                    period.shutdown_start,
                    period.recovery_start,
                    period.recovery_end,
                )
                if value > now
            )
        if not boundaries:
            return
        next_transition = min(boundaries)

        @callback
        def _handle(now_local: datetime) -> None:
            # Point-in-time callbacks may run late if HA's event loop is busy.
            # Re-read wall clock here instead of treating the scheduled deadline
            # as the current phase time.
            actual_now_utc = dt_util.utcnow().astimezone(_UTC)
            self._create_background_task(self._async_handle_transition(actual_now_utc))

        self._remove_transition = async_track_point_in_time(
            self.hass, _handle, next_transition
        )

    def _clear_calculation_state(self) -> None:
        self._locked_period = None
        self._locked_effective_settings = None
        self._locked_provenance_unknown = False
        self._last_price_snapshot = None
        self._last_optimizer_config = None
        self._last_multiplier = None
        self._last_result = None
        self._calculation_key = None

    def _log_error_transition(self, code: str, message: str) -> None:
        """Log only a new error state, not every refresh of the same failure."""
        if self._last_error_code == code:
            return
        if self._last_error_code is not None:
            _LOGGER.info("Optimizer error changed from %s to %s", self._last_error_code, code)
        _LOGGER.warning("%s (%s)", message, code)
        self._last_error_code = code


def _entry_settings_fingerprint(entry: ConfigEntry) -> str:
    """Hash stable profile settings without storing source IDs in the lock file."""
    settings = {"data": dict(entry.data), "options": dict(entry.options)}
    serialized = json.dumps(settings, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _period_to_store(period: CutoffPeriod) -> dict[str, object]:
    return {
        "preheat_start": period.preheat_start.isoformat(),
        "shutdown_start": period.shutdown_start.isoformat(),
        "recovery_start": period.recovery_start.isoformat(),
        "recovery_end": period.recovery_end.isoformat(),
        "savings_per_kw": period.savings_per_kw,
        "savings_percent": period.savings_percent,
        "baseline_cost_per_kw": period.baseline_cost_per_kw,
        "planned_cost_per_kw": period.planned_cost_per_kw,
        "price_difference_per_kwh": period.price_difference_per_kwh,
        "min_price_diff_per_kwh": period.min_price_diff_per_kwh,
        "shutdown_duration_hours": period.shutdown_duration_hours,
    }


def _period_from_store(value: object) -> CutoffPeriod | None:
    if not isinstance(value, dict):
        return None
    try:
        times = {
            key: datetime.fromisoformat(value[key])
            for key in (
                "preheat_start",
                "shutdown_start",
                "recovery_start",
                "recovery_end",
            )
        }
        if any(time.tzinfo is None for time in times.values()):
            return None
        numeric = {
            key: _stored_number(value.get(key))
            for key in (
                "savings_per_kw",
                "baseline_cost_per_kw",
                "planned_cost_per_kw",
                "price_difference_per_kwh",
                "min_price_diff_per_kwh",
                "shutdown_duration_hours",
            )
        }
        savings_percent = value.get("savings_percent")
        if savings_percent is not None:
            savings_percent = _stored_number(savings_percent)
        preheat_start = times["preheat_start"].astimezone(_UTC)
        shutdown_start = times["shutdown_start"].astimezone(_UTC)
        recovery_start = times["recovery_start"].astimezone(_UTC)
        recovery_end = times["recovery_end"].astimezone(_UTC)
        if not preheat_start < shutdown_start < recovery_start < recovery_end:
            return None
        if numeric["shutdown_duration_hours"] <= 0:
            return None
        return CutoffPeriod(
            preheat_start=preheat_start,
            shutdown_start=shutdown_start,
            recovery_start=recovery_start,
            recovery_end=recovery_end,
            savings_per_kw=numeric["savings_per_kw"],
            savings_percent=savings_percent,
            baseline_cost_per_kw=numeric["baseline_cost_per_kw"],
            planned_cost_per_kw=numeric["planned_cost_per_kw"],
            price_difference_per_kwh=numeric["price_difference_per_kwh"],
            min_price_diff_per_kwh=numeric["min_price_diff_per_kwh"],
            shutdown_duration_hours=numeric["shutdown_duration_hours"],
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _stored_number(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("stored period number is invalid")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("stored period number is invalid")
    return number


def _resolve_helper_options(
    hass: object, options: dict[str, object]
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Apply live numeric helpers, falling back to static values with warnings."""
    resolved, warnings, _ = _resolve_helper_options_with_provenance(hass, options)
    return resolved, warnings


def _resolve_helper_options_with_provenance(
    hass: object, options: dict[str, object]
) -> tuple[dict[str, object], tuple[str, ...], dict[str, object]]:
    """Resolve live helpers and retain the effective value's source for diagnostics."""
    resolved = dict(options)
    warnings: list[str] = []

    max_value, warning = _resolve_numeric_helper(
        hass,
        options,
        entity_key="max_cutoff_entity_id",
        unit_key="max_cutoff_entity_unit",
        accepted_units={MAX_CUTOFF_ENTITY_UNIT_HOURS, MAX_CUTOFF_ENTITY_UNIT_MINUTES},
        unavailable_warning=WARNING_MAX_CUTOFF_HELPER_UNAVAILABLE,
        invalid_warning=WARNING_MAX_CUTOFF_HELPER_INVALID,
    )
    if warning:
        warnings.append(warning)
    elif max_value is not None:
        unit = options["max_cutoff_entity_unit"]
        minutes = max_value * 60 if unit == MAX_CUTOFF_ENTITY_UNIT_HOURS else max_value
        if (
            not math.isfinite(minutes)
            or not minutes.is_integer()
            or not 2 * SLOT_MINUTES <= minutes <= MAX_CUTOFF_MINUTES
            or int(minutes) % SLOT_MINUTES
        ):
            warnings.append(WARNING_MAX_CUTOFF_HELPER_INVALID)
        else:
            resolved["max_cutoff_minutes"] = int(minutes)

    price_value, warning = _resolve_numeric_helper(
        hass,
        options,
        entity_key="min_price_diff_entity_id",
        unit_key="min_price_diff_entity_unit",
        accepted_units={
            MIN_PRICE_DIFF_ENTITY_UNIT_CURRENCY_PER_KWH,
            MIN_PRICE_DIFF_ENTITY_UNIT_SUBUNIT_PER_KWH,
        },
        unavailable_warning=WARNING_MIN_PRICE_DIFF_HELPER_UNAVAILABLE,
        invalid_warning=WARNING_MIN_PRICE_DIFF_HELPER_INVALID,
    )
    if warning:
        warnings.append(warning)
    elif price_value is not None:
        unit = options["min_price_diff_entity_unit"]
        normalized_price = (
            price_value / 100.0
            if unit == MIN_PRICE_DIFF_ENTITY_UNIT_SUBUNIT_PER_KWH
            else price_value
        )
        if not math.isfinite(normalized_price) or normalized_price < 0:
            warnings.append(WARNING_MIN_PRICE_DIFF_HELPER_INVALID)
        else:
            resolved["min_price_diff_per_kwh"] = normalized_price

    unique_warnings = tuple(dict.fromkeys(warnings))
    max_warning = next(
        (warning for warning in unique_warnings if warning.startswith("max_cutoff_helper_")),
        None,
    )
    price_warning = next(
        (warning for warning in unique_warnings if warning.startswith("min_price_diff_helper_")),
        None,
    )
    max_helper_id = options.get("max_cutoff_entity_id")
    price_helper_id = options.get("min_price_diff_entity_id")
    effective = {
        "max_cutoff_minutes": _int_option(
            resolved, "max_cutoff_minutes", DEFAULT_MAX_CUTOFF_MINUTES
        ),
        "max_cutoff_source": (
            "fallback" if max_warning else "helper" if max_helper_id else "fixed"
        ),
        "max_cutoff_fallback_reason": max_warning,
        "min_price_difference_per_kwh": _float_option(
            resolved, "min_price_diff_per_kwh", DEFAULT_MIN_PRICE_DIFF_PER_KWH
        ),
        "min_price_difference_source": (
            "fallback" if price_warning else "helper" if price_helper_id else "fixed"
        ),
        "min_price_difference_fallback_reason": price_warning,
    }
    return resolved, unique_warnings, effective


def _resolve_numeric_helper(
    hass: object,
    options: dict[str, object],
    *,
    entity_key: str,
    unit_key: str,
    accepted_units: set[str],
    unavailable_warning: str,
    invalid_warning: str,
) -> tuple[float | None, str | None]:
    entity_id = options.get(entity_key)
    if entity_id in (None, ""):
        return None, None
    unit = options.get(unit_key)
    if (
        not isinstance(entity_id, str)
        or not entity_id.strip()
        or not isinstance(unit, str)
        or unit not in accepted_units
    ):
        return None, invalid_warning
    state = hass.states.get(entity_id)
    raw_value = getattr(state, "state", None) if state is not None else None
    if state is None or not isinstance(raw_value, str) or raw_value.lower() in {
        "unknown", "unavailable", "none", ""
    }:
        return None, unavailable_warning
    try:
        value = float(raw_value)
    except (TypeError, ValueError, OverflowError):
        return None, invalid_warning
    if not math.isfinite(value):
        return None, invalid_warning
    return value, None


def _optimizer_config(
    provider: ProviderConfig, options: dict[str, object]
) -> OptimizerConfig:
    currency = provider.currency.upper()
    max_cutoff = _int_option(options, "max_cutoff_minutes", DEFAULT_MAX_CUTOFF_MINUTES)
    max_periods = _int_option(options, "max_periods", DEFAULT_MAX_PERIODS)
    if not 1 <= max_periods <= MAX_UI_MAX_PERIODS:
        raise ValueError("max_periods must be between 1 and 6")
    if not 2 * SLOT_MINUTES <= max_cutoff <= MAX_CUTOFF_MINUTES:
        raise ValueError("max_cutoff_minutes is outside the supported range")
    if max_cutoff % SLOT_MINUTES:
        raise ValueError("max_cutoff_minutes must be a 15-minute multiple")
    return OptimizerConfig(
        currency=currency,
        min_cutoff_minutes=DEFAULT_MIN_CUTOFF_MINUTES,
        max_cutoff_minutes=max_cutoff,
        min_price_diff_per_kwh=_float_option(
            options, "min_price_diff_per_kwh", DEFAULT_MIN_PRICE_DIFF_PER_KWH
        ),
        max_periods=max_periods,
        preheat_factor=_float_option(options, "preheat_factor", DEFAULT_PREHEAT_FACTOR),
        recovery_factor=_float_option(options, "recovery_factor", DEFAULT_RECOVERY_FACTOR),
    )


def _resolve_entry_multipliers(hass: object, options: dict[str, object]) -> MultiplierResult:
    mode = options.get("multiplier_mode", DEFAULT_MULTIPLIER_MODE)
    temperature_c = None
    entity_id = options.get("temperature_entity_id")
    if isinstance(entity_id, str) and entity_id:
        state = hass.states.get(entity_id)
        if state is not None:
            raw_state = getattr(state, "state", None)
            attrs = getattr(state, "attributes", {})
            unit = attrs.get("unit_of_measurement") if isinstance(attrs, Mapping) else None
            temperature_c = _temperature_to_celsius(raw_state, unit)
    result = resolve_multipliers(
        mode=str(mode),
        temperature_c=temperature_c,
        preheat_factor=_float_option(options, "preheat_factor", DEFAULT_PREHEAT_FACTOR),
        recovery_factor=_float_option(options, "recovery_factor", DEFAULT_RECOVERY_FACTOR),
    )
    if entity_id and result.warning is None and temperature_c is None:
        return replace(result, warning="missing_or_invalid_temperature")
    return result


def _temperature_to_celsius(value: object, unit: object) -> float | None:
    if not isinstance(value, str) or value.lower() in {"unknown", "unavailable", "none", ""}:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric):
        return None
    normalized_unit = str(unit or "").strip().lower().replace("°", "")
    if normalized_unit in {"c", "celsius"}:
        return numeric
    if normalized_unit in {"f", "fahrenheit"}:
        return (numeric - 32.0) * 5.0 / 9.0
    if normalized_unit in {"k", "kelvin"}:
        return numeric - 273.15
    return None


def _calculate_result(
    snapshot: PriceSnapshot,
    config: OptimizerConfig,
    multiplier: MultiplierResult,
    now_utc: datetime,
    locked: CutoffPeriod | None,
) -> tuple[OptimizationResult, CutoffPeriod | None]:
    if locked is not None:
        result = _optimize_after_locked(snapshot, config, multiplier, locked)
        return result, locked
    initial = optimize(snapshot.slots, _apply_multipliers(config, multiplier), not_before=now_utc)
    active = next(
        (
            period
            for period in initial.periods
            if period.preheat_start <= now_utc < period.recovery_end
        ),
        None,
    )
    if active is None:
        return initial, None
    return _optimize_after_locked(snapshot, config, multiplier, active), active


def _optimize_after_locked(
    snapshot: PriceSnapshot,
    config: OptimizerConfig,
    multiplier: MultiplierResult,
    locked: CutoffPeriod,
) -> OptimizationResult:
    remaining_k = config.max_periods - 1
    if remaining_k <= 0:
        future = OptimizationResult((), 0.0, 0, 0, len(snapshot.slots), config.currency.upper())
    else:
        future = optimize(
            snapshot.slots,
            _apply_multipliers(replace(config, max_periods=remaining_k), multiplier),
            not_before=locked.recovery_end,
        )
    return OptimizationResult(
        periods=(locked, *future.periods),
        total_savings_per_kw=locked.savings_per_kw + future.total_savings_per_kw,
        candidates_scanned=future.candidates_scanned,
        candidates_accepted=future.candidates_accepted,
        slot_count=len(snapshot.slots),
        currency=config.currency.upper(),
        warnings=future.warnings,
    )


def _apply_multipliers(config: OptimizerConfig, multiplier: MultiplierResult) -> OptimizerConfig:
    return replace(
        config,
        preheat_factor=multiplier.preheat_factor,
        recovery_factor=multiplier.recovery_factor,
    )


def _runtime_snapshot(
    price_snapshot: PriceSnapshot | None,
    result: OptimizationResult | None,
    now_utc: datetime,
    *,
    last_success: datetime | None,
    multipliers: MultiplierResult | None,
    additional_warnings: tuple[str, ...] = (),
    effective_settings: dict[str, object] | None = None,
    locked_period: CutoffPeriod | None = None,
    locked_effective_settings: dict[str, object] | None = None,
) -> RuntimeSnapshot:
    if price_snapshot is None or result is None:
        return _unavailable_snapshot(
            DataStatus.INVALID_DATA,
            last_success=last_success,
            calculated_at=now_utc,
        )
    periods = result.periods
    current = next(
        (
            period
            for period in periods
            if period.preheat_start <= now_utc < period.recovery_end
        ),
        None,
    )
    next_period = next(
        (period for period in periods if period.shutdown_start >= now_utc),
        None,
    )
    phase: Literal["normal", "preheat", "shutdown", "recovery"] = "normal"
    if current is not None:
        if now_utc < current.shutdown_start:
            phase = "preheat"
        elif now_utc < current.recovery_start:
            phase = "shutdown"
        else:
            phase = "recovery"

    if not price_snapshot.tomorrow_available:
        status = DataStatus.WAITING_FOR_PRICES
    elif not periods:
        status = DataStatus.NO_PROFITABLE_PERIODS
    else:
        status = DataStatus.READY
    warnings = list(price_snapshot.warnings)
    warnings.extend(additional_warnings)
    if multipliers and multipliers.warning:
        warnings.append(multipliers.warning)
    warnings.extend(result.warnings)
    future_boundaries = [
        boundary
        for period in periods
        for boundary in (
            period.preheat_start,
            period.shutdown_start,
            period.recovery_start,
            period.recovery_end,
        )
        if boundary > now_utc
    ]
    return RuntimeSnapshot(
        status=status,
        available=True,
        price_snapshot=price_snapshot,
        result=result,
        phase=phase,
        current_period=current,
        next_period=next_period,
        last_success=last_success,
        calculated_at=now_utc,
        error_code=None,
        warnings=tuple(dict.fromkeys(warnings)),
        effective_settings=effective_settings,
        locked_period=locked_period,
        locked_effective_settings=locked_effective_settings,
        next_change=min(future_boundaries) if future_boundaries else None,
    )


def _unavailable_snapshot(
    status: DataStatus,
    *,
    error_code: str | None = None,
    last_success: datetime | None = None,
    calculated_at: datetime | None = None,
    effective_settings: dict[str, object] | None = None,
) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        status=status,
        available=False,
        price_snapshot=None,
        result=None,
        phase=None,
        current_period=None,
        next_period=None,
        last_success=last_success,
        calculated_at=calculated_at,
        error_code=error_code,
        effective_settings=effective_settings,
    )


def _supports_locked_window(
    snapshot: PriceSnapshot, locked: CutoffPeriod, now_utc: datetime
) -> bool:
    # Once preheat has begun, earlier prices can disappear when provider data
    # rolls over at local midnight. Only remaining coverage is required.
    cursor = max(now_utc, locked.preheat_start)
    if cursor >= locked.recovery_end:
        return False
    for slot in snapshot.slots:
        if slot.end <= cursor:
            continue
        if slot.start > cursor:
            return False
        if slot.start <= cursor < slot.end:
            cursor = slot.end
            if cursor >= locked.recovery_end:
                return True
    return False


def _calculation_key(
    snapshot: PriceSnapshot,
    config: OptimizerConfig,
    multiplier: MultiplierResult,
    locked: CutoffPeriod | None,
) -> tuple[object, ...]:
    return (
        snapshot.slots,
        snapshot.currency,
        config,
        multiplier.preheat_factor,
        multiplier.recovery_factor,
        locked,
    )


def _provider_key(provider: ProviderConfig) -> str:
    return provider.provider_type


def _int_option(options: dict[str, object], key: str, default: int) -> int:
    value = options.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as err:
        raise ValueError(f"{key} must be an integer") from err
    if isinstance(value, float) and parsed != value:
        raise ValueError(f"{key} must be an integer")
    return parsed


def _float_option(options: dict[str, object], key: str, default: float) -> float:
    value = options.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as err:
        raise ValueError(f"{key} must be numeric") from err
    if not math.isfinite(parsed):
        raise ValueError(f"{key} must be finite")
    return parsed
