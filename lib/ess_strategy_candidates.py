"""Pure, shadow-only ESS strategy candidate evaluator.

This module deliberately has no dependency on settings, MQTT, the energy
broker, or either live optimizer.  It evaluates three deterministic planning
policies against caller-supplied slots so research and later scenario analysis
cannot alter an active Victron plan by import side effect.

The candidates are intentionally small and explicit:

``market_arbitrage``
    Normal price-led grid charging and battery export, constrained by the
    supplied physical limits.
``pv_first_self_sufficiency``
    No active grid charging and no active battery-to-grid export.  PV can still
    charge the battery or export naturally once the battery cannot accept it.
``protected_hybrid``
    May use grid energy only to restore a supplied protected household reserve,
    and may export only energy above that reserve.

It is *not* an executor and it does not choose a winner.  A future caller may
run scenario variants and apply a separate, explicitly guarded Pareto policy.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from math import isfinite
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional


EPS = 1e-9


@dataclass(frozen=True)
class DeterministicSlot:
    """One fully specified, deterministic energy-planning interval.

    ``load_kwh`` and ``pv_kwh`` are AC-side quantities for this interval.
    ``sell_price`` is optional; when absent it is derived from the supplied
    export factor and fee in :class:`CandidateConfig`.
    """

    start: Any
    duration_h: float
    buy_price: float
    load_kwh: float
    pv_kwh: float
    sell_price: Optional[float] = None


@dataclass(frozen=True)
class CandidateConfig:
    """Physical/economic assumptions supplied by the caller.

    Values are deliberately explicit rather than read from ``.env``.  That
    makes research replayable and prevents this shadow layer from changing
    operational configuration or behavior.

    ``protected_soc_percent`` is a hard household-energy floor for the two
    protected candidates.  If the live battery starts below it, they may only
    hold or recover until the reserve is restored; they cannot discharge a
    reserve that is not physically present.  Recovery may span several slots
    when the charge-rate limit requires it.
    """

    battery_capacity_kwh: float
    min_soc_percent: float
    protected_soc_percent: float
    charge_efficiency: float
    discharge_efficiency: float
    max_charge_kw: float
    max_discharge_kw: float
    max_import_kw: float
    max_export_kw: float
    soc_step_percent: float = 1.0
    grid_charge_soc_cap_percent: float = 100.0
    cycle_cost_eur_per_dc_kwh: float = 0.0
    arbitrage_margin_eur_per_dc_kwh: float = 0.0
    export_price_factor: float = 1.0
    export_fee_eur_per_kwh: float = 0.0
    # Value of one DC-side kWh retained above the policy floor at the end of a
    # horizon which demonstrably crosses a local day boundary.  The caller must
    # already account for discharge efficiency when deriving this from an
    # AC-side price, matching the live optimizer's terminal-value convention.
    terminal_value_eur_per_dc_kwh: float = 0.0

    def __post_init__(self) -> None:
        numeric = {
            "battery_capacity_kwh": self.battery_capacity_kwh,
            "min_soc_percent": self.min_soc_percent,
            "protected_soc_percent": self.protected_soc_percent,
            "charge_efficiency": self.charge_efficiency,
            "discharge_efficiency": self.discharge_efficiency,
            "max_charge_kw": self.max_charge_kw,
            "max_discharge_kw": self.max_discharge_kw,
            "max_import_kw": self.max_import_kw,
            "max_export_kw": self.max_export_kw,
            "soc_step_percent": self.soc_step_percent,
            "grid_charge_soc_cap_percent": self.grid_charge_soc_cap_percent,
            "cycle_cost_eur_per_dc_kwh": self.cycle_cost_eur_per_dc_kwh,
            "arbitrage_margin_eur_per_dc_kwh": self.arbitrage_margin_eur_per_dc_kwh,
            "export_price_factor": self.export_price_factor,
            "export_fee_eur_per_kwh": self.export_fee_eur_per_kwh,
            "terminal_value_eur_per_dc_kwh": self.terminal_value_eur_per_dc_kwh,
        }
        for name, value in numeric.items():
            if not isfinite(float(value)):
                raise ValueError(f"{name} must be finite")

        if self.battery_capacity_kwh <= 0:
            raise ValueError("battery_capacity_kwh must be positive")
        if not 0 <= self.min_soc_percent <= 100:
            raise ValueError("min_soc_percent must be between 0 and 100")
        if not self.min_soc_percent <= self.protected_soc_percent <= 100:
            raise ValueError("protected_soc_percent must be between min_soc_percent and 100")
        if not 0 < self.charge_efficiency <= 1:
            raise ValueError("charge_efficiency must be in (0, 1]")
        if not 0 < self.discharge_efficiency <= 1:
            raise ValueError("discharge_efficiency must be in (0, 1]")
        if not 0 < self.soc_step_percent <= 100:
            raise ValueError("soc_step_percent must be in (0, 100]")
        if not 0 <= self.grid_charge_soc_cap_percent <= 100:
            raise ValueError("grid_charge_soc_cap_percent must be between 0 and 100")
        if any(value < 0 for value in (
            self.max_charge_kw,
            self.max_discharge_kw,
            self.max_import_kw,
            self.max_export_kw,
            self.cycle_cost_eur_per_dc_kwh,
            self.arbitrage_margin_eur_per_dc_kwh,
        )):
            raise ValueError("power limits and per-kWh costs must not be negative")


@dataclass(frozen=True)
class CandidateStep:
    """One simulated candidate transition, useful for later visualisation."""

    start: Any
    duration_h: float
    soc_start_percent: float
    soc_end_percent: float
    dc_change_kwh: float
    grid_energy_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    buy_price: float
    sell_price: float
    active_grid_charge: bool
    active_battery_export: bool


@dataclass(frozen=True)
class CandidateResult:
    """A candidate's physical and economic metrics; never a control command."""

    candidate_id: str
    feasible: bool
    rejection_reason: Optional[str]
    cash_net_eur: float
    import_cost_eur: float
    export_reward_eur: float
    lifecycle_cost_eur: float
    risk_hurdle_eur: float
    economic_net_eur: float
    model_score_eur: float
    grid_import_kwh: float
    grid_export_kwh: float
    dc_charge_kwh: float
    dc_discharge_kwh: float
    dc_throughput_kwh: float
    full_equivalent_cycles: float
    terminal_soc_percent: float
    minimum_soc_percent: float
    protected_soc_percent: float
    protected_energy_kwh: float
    protection_shortfall_kwh: float
    schedule: tuple[CandidateStep, ...]


@dataclass(frozen=True)
class _Policy:
    candidate_id: str
    required_floor_soc_percent: float
    allow_active_grid_charge: bool
    grid_charge_ceiling_soc_percent: float
    allow_active_battery_export: bool


@dataclass(frozen=True)
class _Path:
    objective_cost_eur: float
    cash_net_eur: float
    import_cost_eur: float
    export_reward_eur: float
    dc_charge_kwh: float
    dc_discharge_kwh: float
    previous: Optional["_Path"]
    step: Optional[CandidateStep]


def evaluate_shadow_candidates(
    slots: Iterable[DeterministicSlot],
    *,
    initial_soc_percent: float,
    config: CandidateConfig,
) -> "OrderedDict[str, CandidateResult]":
    """Evaluate isolated market/PV-first/hybrid candidates.

    The caller supplies a *deterministic* price/load/PV horizon.  No current
    clock, configuration value, filesystem state, network service, or live
    optimizer is read.  Results are deliberately returned side-by-side instead
    of selecting or executing one.
    """

    normalized = _validate_slots(slots)
    initial_soc = _validate_soc(initial_soc_percent, "initial_soc_percent")
    policies = (
        _Policy(
            candidate_id="market_arbitrage",
            required_floor_soc_percent=config.min_soc_percent,
            allow_active_grid_charge=True,
            grid_charge_ceiling_soc_percent=config.grid_charge_soc_cap_percent,
            allow_active_battery_export=True,
        ),
        _Policy(
            candidate_id="pv_first_self_sufficiency",
            required_floor_soc_percent=config.protected_soc_percent,
            allow_active_grid_charge=False,
            grid_charge_ceiling_soc_percent=config.protected_soc_percent,
            allow_active_battery_export=False,
        ),
        _Policy(
            candidate_id="protected_hybrid",
            required_floor_soc_percent=config.protected_soc_percent,
            allow_active_grid_charge=True,
            grid_charge_ceiling_soc_percent=min(
                config.grid_charge_soc_cap_percent,
                config.protected_soc_percent,
            ),
            allow_active_battery_export=True,
        ),
    )
    return OrderedDict(
        (policy.candidate_id, _evaluate_policy(normalized, initial_soc, config, policy))
        for policy in policies
    )


def _validate_slots(slots: Iterable[DeterministicSlot]) -> tuple[DeterministicSlot, ...]:
    normalized = tuple(slots)
    for index, slot in enumerate(normalized):
        if not isinstance(slot, DeterministicSlot):
            raise TypeError(f"slots[{index}] must be DeterministicSlot")
        for name, value in (
            ("duration_h", slot.duration_h),
            ("buy_price", slot.buy_price),
            ("load_kwh", slot.load_kwh),
            ("pv_kwh", slot.pv_kwh),
        ):
            if not isfinite(float(value)):
                raise ValueError(f"slots[{index}].{name} must be finite")
        if slot.sell_price is not None and not isfinite(float(slot.sell_price)):
            raise ValueError(f"slots[{index}].sell_price must be finite")
        if slot.duration_h <= 0:
            raise ValueError(f"slots[{index}].duration_h must be positive")
        if slot.load_kwh < 0 or slot.pv_kwh < 0:
            raise ValueError(f"slots[{index}] load_kwh and pv_kwh must not be negative")
    return normalized


def _validate_soc(value: float, name: str) -> float:
    if not isfinite(float(value)) or not 0 <= float(value) <= 100:
        raise ValueError(f"{name} must be between 0 and 100")
    return float(value)


def _evaluate_policy(
    slots: tuple[DeterministicSlot, ...],
    initial_soc_percent: float,
    config: CandidateConfig,
    policy: _Policy,
) -> CandidateResult:
    if not slots:
        return _infeasible_result(policy, initial_soc_percent, config, "no_slots")

    states = _soc_states(initial_soc_percent, config, policy.required_floor_soc_percent)
    paths: dict[float, _Path] = {
        initial_soc_percent: _Path(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, None, None)
    }

    for slot in slots:
        next_paths: dict[float, _Path] = {}
        for soc_start, path in paths.items():
            for soc_end in states:
                if not _state_is_allowed(
                    soc_start,
                    soc_end,
                    policy.required_floor_soc_percent,
                ):
                    continue
                step = _transition(soc_start, soc_end, slot, config)
                if step is None:
                    continue
                if not _transition_is_allowed(step, policy, config):
                    continue

                import_cost = step.grid_import_kwh * step.buy_price
                export_reward = step.grid_export_kwh * step.sell_price
                discharge_hurdle = (
                    config.cycle_cost_eur_per_dc_kwh
                    + config.arbitrage_margin_eur_per_dc_kwh
                )
                objective_delta = import_cost - export_reward
                if step.dc_change_kwh < -EPS:
                    objective_delta += (-step.dc_change_kwh) * discharge_hurdle

                objective_cost = path.objective_cost_eur + objective_delta
                incumbent = next_paths.get(soc_end)
                if (incumbent is not None
                        and objective_cost >= incumbent.objective_cost_eur - EPS):
                    continue

                candidate = _Path(
                    objective_cost_eur=objective_cost,
                    cash_net_eur=path.cash_net_eur + export_reward - import_cost,
                    import_cost_eur=path.import_cost_eur + import_cost,
                    export_reward_eur=path.export_reward_eur + export_reward,
                    dc_charge_kwh=path.dc_charge_kwh + max(0.0, step.dc_change_kwh),
                    dc_discharge_kwh=path.dc_discharge_kwh + max(0.0, -step.dc_change_kwh),
                    previous=path,
                    step=step,
                )
                next_paths[soc_end] = candidate
        paths = next_paths
        if not paths:
            return _infeasible_result(
                policy,
                initial_soc_percent,
                config,
                "no_physical_feasible_schedule",
            )

    horizon_crosses_day = _horizon_crosses_day(slots)
    selected_pair = _best_terminal_path(
        paths,
        config,
        policy,
        horizon_crosses_day=horizon_crosses_day,
    )
    if selected_pair is None:
        return _infeasible_result(
            policy,
            initial_soc_percent,
            config,
            "cannot_restore_required_reserve",
        )
    selected_soc, selected = selected_pair
    lifecycle_cost = selected.dc_discharge_kwh * config.cycle_cost_eur_per_dc_kwh
    risk_hurdle = selected.dc_discharge_kwh * config.arbitrage_margin_eur_per_dc_kwh
    terminal_value = _terminal_value(
        selected_soc,
        config,
        policy,
        horizon_crosses_day=horizon_crosses_day,
    )
    throughput = selected.dc_charge_kwh + selected.dc_discharge_kwh
    schedule = _reconstruct_schedule(selected)
    protected_energy = max(
        0.0,
        (policy.required_floor_soc_percent - config.min_soc_percent)
        / 100.0
        * config.battery_capacity_kwh,
    )
    minimum_soc = min(
        initial_soc_percent,
        *(step.soc_end_percent for step in schedule),
    )
    return CandidateResult(
        candidate_id=policy.candidate_id,
        feasible=True,
        rejection_reason=None,
        cash_net_eur=selected.cash_net_eur,
        import_cost_eur=selected.import_cost_eur,
        export_reward_eur=selected.export_reward_eur,
        lifecycle_cost_eur=lifecycle_cost,
        risk_hurdle_eur=risk_hurdle,
        economic_net_eur=selected.cash_net_eur - lifecycle_cost,
        model_score_eur=selected.cash_net_eur - lifecycle_cost - risk_hurdle + terminal_value,
        grid_import_kwh=sum(step.grid_import_kwh for step in schedule),
        grid_export_kwh=sum(step.grid_export_kwh for step in schedule),
        dc_charge_kwh=selected.dc_charge_kwh,
        dc_discharge_kwh=selected.dc_discharge_kwh,
        dc_throughput_kwh=throughput,
        full_equivalent_cycles=throughput / (2.0 * config.battery_capacity_kwh),
        terminal_soc_percent=selected_soc,
        minimum_soc_percent=minimum_soc,
        protected_soc_percent=policy.required_floor_soc_percent,
        protected_energy_kwh=protected_energy,
        protection_shortfall_kwh=max(
            0.0,
            (policy.required_floor_soc_percent - minimum_soc)
            / 100.0
            * config.battery_capacity_kwh,
        ),
        schedule=schedule,
    )


def _infeasible_result(
    policy: _Policy,
    initial_soc_percent: float,
    config: CandidateConfig,
    reason: str,
) -> CandidateResult:
    protected_energy = max(
        0.0,
        (policy.required_floor_soc_percent - config.min_soc_percent)
        / 100.0
        * config.battery_capacity_kwh,
    )
    return CandidateResult(
        candidate_id=policy.candidate_id,
        feasible=False,
        rejection_reason=reason,
        cash_net_eur=0.0,
        import_cost_eur=0.0,
        export_reward_eur=0.0,
        lifecycle_cost_eur=0.0,
        risk_hurdle_eur=0.0,
        economic_net_eur=0.0,
        model_score_eur=0.0,
        grid_import_kwh=0.0,
        grid_export_kwh=0.0,
        dc_charge_kwh=0.0,
        dc_discharge_kwh=0.0,
        dc_throughput_kwh=0.0,
        full_equivalent_cycles=0.0,
        terminal_soc_percent=initial_soc_percent,
        minimum_soc_percent=initial_soc_percent,
        protected_soc_percent=policy.required_floor_soc_percent,
        protected_energy_kwh=protected_energy,
        protection_shortfall_kwh=max(
            0.0,
            (policy.required_floor_soc_percent - initial_soc_percent)
            / 100.0
            * config.battery_capacity_kwh,
        ),
        schedule=(),
    )


def _soc_states(
    initial_soc_percent: float,
    config: CandidateConfig,
    protected_soc_percent: float,
) -> tuple[float, ...]:
    """Build a fixed lattice while preserving initial and reserve states exactly."""
    count = int(100.0 // config.soc_step_percent)
    states = {
        round(index * config.soc_step_percent, 10)
        for index in range(count + 1)
    }
    states.update({
        round(initial_soc_percent, 10),
        round(config.min_soc_percent, 10),
        round(protected_soc_percent, 10),
        100.0,
    })
    return tuple(sorted(state for state in states if -EPS <= state <= 100.0 + EPS))


def _state_is_allowed(
    soc_start_percent: float,
    soc_end_percent: float,
    required_floor_soc_percent: float,
) -> bool:
    """Respect a reserve that may initially be physically unavailable.

    When the current real SoC is already below the required reserve, a
    rate-limited model cannot honestly teleport it to safety in one interval.
    It may hold or make monotonic progress toward the floor over subsequent
    slots.  Once the floor is reached, every resulting boundary must retain it.
    Terminal selection separately requires full recovery, so this flexibility
    never represents a partial reserve as safely restored.
    """
    if soc_start_percent < required_floor_soc_percent - EPS:
        return soc_end_percent >= soc_start_percent - EPS
    return soc_end_percent >= required_floor_soc_percent - EPS


def _transition(
    soc_start_percent: float,
    soc_end_percent: float,
    slot: DeterministicSlot,
    config: CandidateConfig,
) -> Optional[CandidateStep]:
    dc_change = (
        (soc_end_percent - soc_start_percent)
        / 100.0
        * config.battery_capacity_kwh
    )
    batt_kw = abs(dc_change) / slot.duration_h
    if dc_change > EPS:
        if batt_kw > config.max_charge_kw + EPS:
            return None
        ac_for_battery = dc_change / config.charge_efficiency
    elif dc_change < -EPS:
        if batt_kw > config.max_discharge_kw + EPS:
            return None
        ac_for_battery = dc_change * config.discharge_efficiency
    else:
        ac_for_battery = 0.0

    grid_energy = slot.load_kwh - slot.pv_kwh + ac_for_battery
    if grid_energy > config.max_import_kw * slot.duration_h + EPS:
        return None
    if -grid_energy > config.max_export_kw * slot.duration_h + EPS:
        return None

    sell_price = (
        float(slot.sell_price)
        if slot.sell_price is not None
        else float(slot.buy_price) * config.export_price_factor - config.export_fee_eur_per_kwh
    )
    return CandidateStep(
        start=slot.start,
        duration_h=float(slot.duration_h),
        soc_start_percent=soc_start_percent,
        soc_end_percent=soc_end_percent,
        dc_change_kwh=dc_change,
        grid_energy_kwh=grid_energy,
        grid_import_kwh=max(0.0, grid_energy),
        grid_export_kwh=max(0.0, -grid_energy),
        buy_price=float(slot.buy_price),
        sell_price=sell_price,
        active_grid_charge=(dc_change > EPS and grid_energy > EPS),
        active_battery_export=(dc_change < -EPS and grid_energy < -EPS),
    )


def _transition_is_allowed(
    step: CandidateStep,
    policy: _Policy,
    config: CandidateConfig,
) -> bool:
    # Do not use an already-missing protected reserve to cover load or export.
    # Holding below the floor is allowed only while a later transition may
    # recover it gradually within the physical charge-rate limit.
    if (
        step.soc_start_percent < policy.required_floor_soc_percent - EPS
        and step.dc_change_kwh < -EPS
    ):
        return False
    if step.active_grid_charge:
        if not policy.allow_active_grid_charge:
            return False
        if step.soc_end_percent > policy.grid_charge_ceiling_soc_percent + EPS:
            return False
    if step.active_battery_export and not policy.allow_active_battery_export:
        return False
    # Active battery export may end exactly at the protected floor, but never
    # below it. The generic floor check has already prevented a lower state.
    if (step.active_battery_export
            and step.soc_end_percent < policy.required_floor_soc_percent - EPS):
        return False
    return True


def _best_terminal_path(
    paths: dict[float, _Path],
    config: CandidateConfig,
    policy: _Policy,
    *,
    horizon_crosses_day: bool,
) -> tuple[float, _Path] | None:
    def value(item: tuple[float, _Path]) -> float:
        soc, path = item
        return path.objective_cost_eur - _terminal_value(
            soc,
            config,
            policy,
            horizon_crosses_day=horizon_crosses_day,
        )

    recovered = [
        item for item in paths.items()
        if item[0] >= policy.required_floor_soc_percent - EPS
    ]
    return min(recovered, key=value) if recovered else None


def _reconstruct_schedule(path: _Path) -> tuple[CandidateStep, ...]:
    """Reconstruct once after selection instead of copying paths for every DP edge."""
    steps = []
    current: Optional[_Path] = path
    while current is not None and current.step is not None:
        steps.append(current.step)
        current = current.previous
    return tuple(reversed(steps))


def _terminal_value(
    soc_percent: float,
    config: CandidateConfig,
    policy: _Policy,
    *,
    horizon_crosses_day: bool,
) -> float:
    if not horizon_crosses_day:
        return 0.0
    usable_dc = max(
        0.0,
        (soc_percent - policy.required_floor_soc_percent)
        / 100.0
        * config.battery_capacity_kwh,
    )
    return usable_dc * config.terminal_value_eur_per_dc_kwh


def _local_date(value: Any) -> Optional[date]:
    """Return the calendar day a slot/step timestamp falls on, or None.

    Aware timestamps resolve in their own UTC offset, which is the local day the
    price slot was published for.  ``datetime`` is checked first because it is a
    subclass of ``date``.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _horizon_crosses_day(slots: tuple[DeterministicSlot, ...]) -> bool:
    """True only when supplied slot timestamps prove a multi-day horizon.

    The live optimizer deliberately gives retained energy a terminal value only
    when tomorrow is in the known price horizon.  Treating a same-day horizon
    as multi-day would overvalue stored energy and make the comparison unfair.
    Unknown/non-calendar timestamps are conservative: no terminal value.
    """
    dates = set()
    for slot in slots:
        parsed = _local_date(slot.start)
        if parsed is None:
            return False
        dates.add(parsed)
    return len(dates) > 1


@dataclass(frozen=True)
class WindowTotals:
    """Metrics for one sub-window of an already-evaluated candidate schedule.

    This is a *reporting* projection, never a second optimization.  The
    candidate still plans over the whole supplied horizon, so a strategy that
    rationally holds charge through midnight keeps that decision; only the
    attribution of its cash flows is restricted to the window.
    """

    slot_count: int
    window_start: Any
    window_end: Any
    cash_net_eur: float
    import_cost_eur: float
    export_reward_eur: float
    lifecycle_cost_eur: float
    economic_net_eur: float
    grid_import_kwh: float
    grid_export_kwh: float
    dc_charge_kwh: float
    dc_discharge_kwh: float
    dc_throughput_kwh: float
    full_equivalent_cycles: float
    opening_soc_percent: Optional[float]
    closing_soc_percent: Optional[float]


def first_local_day_steps(
    steps: Iterable[CandidateStep],
) -> tuple[CandidateStep, ...]:
    """Return the steps falling on the first calendar day of a schedule.

    A published plan begins at the current slot, so this is the remainder of
    today.  An unparseable leading timestamp yields no steps rather than a
    silently mis-attributed window.
    """
    ordered = tuple(steps)
    if not ordered:
        return ()
    first_day = _local_date(ordered[0].start)
    if first_day is None:
        return ()
    return tuple(
        step for step in ordered if _local_date(step.start) == first_day
    )


def _window_end(steps: tuple[CandidateStep, ...]) -> Any:
    if not steps:
        return None
    last = steps[-1]
    if isinstance(last.start, datetime):
        return last.start + timedelta(hours=last.duration_h)
    return None


def summarize_steps(
    steps: Iterable[CandidateStep],
    config: CandidateConfig,
) -> WindowTotals:
    """Total one window of candidate steps using the evaluator's own arithmetic.

    Import/export are taken exactly as the model booked them so a candidate and
    a live-plan baseline summed by this function stay directly comparable.
    """
    ordered = tuple(steps)
    import_cost = sum(step.grid_import_kwh * step.buy_price for step in ordered)
    export_reward = sum(step.grid_export_kwh * step.sell_price for step in ordered)
    dc_charge = sum(max(0.0, step.dc_change_kwh) for step in ordered)
    dc_discharge = sum(max(0.0, -step.dc_change_kwh) for step in ordered)
    lifecycle_cost = dc_discharge * config.cycle_cost_eur_per_dc_kwh
    cash_net = export_reward - import_cost
    throughput = dc_charge + dc_discharge
    return WindowTotals(
        slot_count=len(ordered),
        window_start=ordered[0].start if ordered else None,
        window_end=_window_end(ordered),
        cash_net_eur=cash_net,
        import_cost_eur=import_cost,
        export_reward_eur=export_reward,
        lifecycle_cost_eur=lifecycle_cost,
        economic_net_eur=cash_net - lifecycle_cost,
        grid_import_kwh=sum(step.grid_import_kwh for step in ordered),
        grid_export_kwh=sum(step.grid_export_kwh for step in ordered),
        dc_charge_kwh=dc_charge,
        dc_discharge_kwh=dc_discharge,
        dc_throughput_kwh=throughput,
        full_equivalent_cycles=throughput / (2.0 * config.battery_capacity_kwh),
        opening_soc_percent=ordered[0].soc_start_percent if ordered else None,
        closing_soc_percent=ordered[-1].soc_end_percent if ordered else None,
    )


__all__ = [
    "CandidateConfig",
    "CandidateResult",
    "CandidateStep",
    "DeterministicSlot",
    "WindowTotals",
    "evaluate_shadow_candidates",
    "first_local_day_steps",
    "summarize_steps",
]
