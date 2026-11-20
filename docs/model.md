# Model and limitations

The optimizer chooses candidate equal-duration preheat, shutdown, and recovery periods from normalized price intervals. It schedules potential load reduction; it does not control a device or observe actual power consumption.

## Reference calculation

All modeled amounts use a 1 kW reference load. For an interval, modeled energy cost is:

```text
price per kWh × interval hours × 1 kW × phase factor
```

The baseline assumes factor 1 throughout. The planned schedule uses a configurable preheat factor, zero during shutdown, and a recovery factor. Model saving per kW is the baseline cost minus planned cost across the complete preheat/shutdown/recovery window. The reported currency is the selected source currency.

This calculation does not multiply by your heat pump's nameplate power, infer real consumption, or guarantee the realized bill saving. System losses, weather, building thermal mass, domestic hot water, tariffs, taxes, and behavior are not modeled. A percentage is undefined when baseline modeled cost is zero or negative.

## Temperature multipliers

Fixed mode uses configured factors (current defaults: preheat 1.5, recovery 1.2). The optional outdoor-temperature mode applies heuristic bands inspired by the earlier HVAC script. Missing, stale, or implausible temperature data should fall back to the configured fixed factors and expose a diagnostic warning. These values are not a calibrated building model.

## Optimization bounds

Price slots are normalized to currency/kWh, with timestamps stored as aware UTC instants. Candidate windows must fit entirely within contiguous price coverage. Gaps are not bridged. The core accepts at most 200 slots and at most 24 periods for bounded internal performance testing. The intended user-facing setting is `K = 1..6`, default 6; the UI cap stays six until weaker-device measurements justify a change.

The optimizer maximizes modeled savings subject to at most K non-overlapping periods. A schedule is not a safety policy. Any automation that acts on it needs its own limits, device-specific safeguards, and monitoring.
