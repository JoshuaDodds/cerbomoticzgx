"""Read-only evidence checks for weather and HVAC forecast shadow models.

This module intentionally has no dependency on the running optimizer, MQTT, or
configuration writes.  It analyses persisted settlement records only and makes
the conservative distinction between evidence that is useful for diagnosis and
evidence that is safe to use when considering a live forecast gate.

In particular, a PV weather shadow is *not* eligible to justify changing the
live PV branch until both baseline and shadow are recorded after the identical
PV-nowcast stage.  The explicit field contract below makes that distinction
machine-checkable instead of relying on a human remembering which historical
field happened to be selected by a feature flag.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import random
from statistics import mean
from typing import Any, Callable, Iterable


VALIDATION_SCHEMA_VERSION = 1

# Contract for a fair, production-pipeline PV comparison.  These intentionally
# do not alias the legacy ``final_pv_forecast_kwh`` field: that value identifies
# only whichever branch was selected at runtime and cannot prove that both
# candidate branches received the same nowcast processing.
PV_FINAL_BASELINE_FIELD = "final_baseline_pv_forecast_kwh"
PV_FINAL_SHADOW_FIELD = "final_weather_pv_shadow_kwh"
PV_FINAL_STAGE_FIELD = "pv_forecast_stage"
PV_FINAL_STAGE_VALUE = "post_nowcast"
PV_PIPELINE_VERSION_FIELD = "pv_nowcast_pipeline_version"
_SLOT_ALIGNMENT_TOLERANCE_SECONDS = 120.0


@dataclass(frozen=True)
class ValidationCriteria:
    """Predeclared evidence gate for a shadow model.

    Passing this gate never turns a feature on.  It only indicates that the
    recorded data is strong enough to deserve a human review before a separate,
    explicit configuration change.
    """

    min_complete_days: int = 14
    min_slots_per_day: int = 80
    min_relative_mae_improvement: float = 0.05
    bootstrap_samples: int = 5000
    bootstrap_seed: int = 20260805
    max_bias_degradation_kwh: float = 0.005
    max_abs_shadow_adjustment_kwh: float = 0.5
    min_slot_coverage_ratio: float = 0.80
    require_calendar_day_boundaries: bool = True

    def __post_init__(self) -> None:
        if self.min_complete_days < 1:
            raise ValueError("min_complete_days must be positive")
        if self.min_slots_per_day < 1:
            raise ValueError("min_slots_per_day must be positive")
        if not 0.0 <= self.min_relative_mae_improvement <= 1.0:
            raise ValueError("min_relative_mae_improvement must be between 0 and 1")
        if self.bootstrap_samples < 0:
            raise ValueError("bootstrap_samples must not be negative")
        if self.max_bias_degradation_kwh < 0:
            raise ValueError("max_bias_degradation_kwh must not be negative")
        if self.max_abs_shadow_adjustment_kwh <= 0:
            raise ValueError("max_abs_shadow_adjustment_kwh must be positive")
        if not 0.0 < self.min_slot_coverage_ratio <= 1.0:
            raise ValueError("min_slot_coverage_ratio must be in (0, 1]")


class HistoryReadError(RuntimeError):
    """Persisted history could not be read safely enough for an evidence gate."""


def _number(value: Any) -> float | None:
    """Return a finite number, never silently accepting bool/NaN/inf values."""
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _record_date(record: dict[str, Any]) -> str | None:
    """Use slot start first so an interval ending at midnight stays on its day."""
    for field in ("slot_start", "ts", "slot_end"):
        value = record.get(field)
        if not isinstance(value, str):
            continue
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            continue
    return None


def _canonical_slot_start(record: dict[str, Any]) -> datetime | None:
    """Return one jitter-tolerant canonical quarter-hour settlement start.

    Forecast validation is based on the optimizer's 15-minute settlement
    intervals.  Counting arbitrary `ts` rows would let retries, duplicate
    writes, or a burst of bunched records masquerade as a full day of evidence.
    The normal scheduler records the start a few seconds after the intended
    quarter-hour, so accept a tightly bounded timestamp jitter and snap it to
    the nearest 15-minute start.  A burst of arbitrary in-between timestamps
    still collapses into the same canonical interval rather than inflating the
    evidence count.  Keep the source offset (important at DST) and require a
    ``slot_start`` rather than guessing from a later settlement timestamp.
    """
    value = record.get("slot_start")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    seconds_into_hour = (
        parsed.minute * 60.0 + parsed.second + parsed.microsecond / 1_000_000.0
    )
    nearest_quarter = int(round(seconds_into_hour / (15 * 60)))
    canonical = parsed.replace(minute=0, second=0, microsecond=0) + timedelta(
        minutes=15 * nearest_quarter
    )
    if abs((parsed - canonical).total_seconds()) > _SLOT_ALIGNMENT_TOLERANCE_SECONDS:
        return None
    return canonical


def _calendar_day_coverage(
    entries: list[tuple[datetime, tuple[float, str], float, float, float]],
) -> tuple[int, float, bool]:
    """Return expected local slots, coverage, and whole-day boundary evidence.

    A contiguous 20-hour capture must not masquerade as a complete day merely
    because its first-to-last *observed* span has no gaps.  Settlement starts
    retain their source UTC offsets, so a daylight-saving transition is inferred
    from the first and last local offsets: 92 slots for spring-forward and 100
    slots for fall-back.  We allow one missing edge slot (00:15 / 23:30) for a
    normal process-boundary handover, but reject a truncated early/late segment.
    """
    if not entries:
        return 0, 0.0, False
    first = entries[0][0]
    last = entries[-1][0]
    first_offset = first.utcoffset()
    last_offset = last.utcoffset()
    offset_delta_seconds = (
        (last_offset - first_offset).total_seconds()
        if first_offset is not None and last_offset is not None else 0.0
    )
    expected_slots = 96 - int(round(offset_delta_seconds / (15 * 60)))
    # Keep a malformed/mixed-offset source from manufacturing an absurdly small
    # denominator. European-style DST is ±4 slots, but the bounds are harmless
    # for any supported fixed-offset source.
    expected_slots = max(88, min(104, expected_slots))
    coverage_ratio = len(entries) / expected_slots
    first_minutes = first.hour * 60 + first.minute
    last_minutes = last.hour * 60 + last.minute
    has_boundaries = first_minutes <= 15 and last_minutes >= (23 * 60 + 30)
    return expected_slots, coverage_ratio, has_boundaries


def _record_revision_key(record: dict[str, Any]) -> tuple[float, str]:
    """Choose the latest duplicate settlement deterministically.

    A later settlement write may correct a previous record.  Prefer its ``ts``
    when valid, then use canonical JSON as a stable tie-breaker so validation
    results do not depend on filesystem line order.
    """
    revision = float("-inf")
    for field in ("ts", "slot_end", "slot_start"):
        value = record.get(field)
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            revision = parsed.timestamp()
            break
    return revision, json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


def _round(value: float | None, places: int = 6) -> float | None:
    return round(value, places) if value is not None else None


def _percentile(sorted_values: list[float], fraction: float) -> float | None:
    if not sorted_values:
        return None
    location = max(0.0, min(1.0, fraction)) * (len(sorted_values) - 1)
    lower = int(math.floor(location))
    upper = int(math.ceil(location))
    if lower == upper:
        return sorted_values[lower]
    weight = location - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _day_block_bootstrap(
    day_deltas: list[float], *, samples: int, seed: int
) -> dict[str, float | int | None]:
    """Bootstrap mean day-level MAE deltas, preserving within-day correlation."""
    if len(day_deltas) < 2 or samples <= 0:
        raw_mean = mean(day_deltas) if day_deltas else None
        return {
            "days": len(day_deltas),
            "samples": samples,
            "mean_delta_kwh": _round(raw_mean),
            "ci95_low_kwh": None,
            "ci95_high_kwh": None,
            # Keep decision precision separate from human-friendly rendering.
            # A 4.99996% improvement must not pass a 5.0% evidence threshold
            # merely because the display rounds it up.
            "_raw_mean_delta_kwh": raw_mean,
            "_raw_ci95_low_kwh": None,
            "_raw_ci95_high_kwh": None,
        }
    generator = random.Random(seed)
    count = len(day_deltas)
    boot = [
        sum(day_deltas[generator.randrange(count)] for _ in range(count)) / count
        for _ in range(samples)
    ]
    boot.sort()
    raw_mean = mean(day_deltas)
    raw_low = _percentile(boot, 0.025)
    raw_high = _percentile(boot, 0.975)
    return {
        "days": count,
        "samples": samples,
        "mean_delta_kwh": _round(raw_mean),
        "ci95_low_kwh": _round(raw_low),
        "ci95_high_kwh": _round(raw_high),
        "_raw_mean_delta_kwh": raw_mean,
        "_raw_ci95_low_kwh": raw_low,
        "_raw_ci95_high_kwh": raw_high,
    }


def _gate(
    metrics: dict[str, Any],
    criteria: ValidationCriteria,
    *,
    decision_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a conservative review gate, never an automatic activation decision."""
    decision_metrics = decision_metrics or metrics
    reasons: list[str] = []
    complete_days = int(decision_metrics.get("eligible_days") or 0)
    if complete_days < criteria.min_complete_days:
        reasons.append(
            f"need at least {criteria.min_complete_days} complete days; found {complete_days}"
        )

    improvement = decision_metrics.get("relative_mae_improvement")
    if improvement is None or improvement < criteria.min_relative_mae_improvement:
        shown = "unavailable" if improvement is None else f"{improvement:.1%}"
        reasons.append(
            "relative MAE improvement "
            f"{shown} is below {criteria.min_relative_mae_improvement:.1%}"
        )

    improved_days = int(decision_metrics.get("improved_days") or 0)
    if complete_days == 0 or improved_days <= complete_days / 2:
        reasons.append(
            f"shadow improves only {improved_days}/{complete_days} complete days"
        )

    confidence = decision_metrics.get("day_block_bootstrap") or {}
    ci_high = confidence.get("_raw_ci95_high_kwh", confidence.get("ci95_high_kwh"))
    if ci_high is None:
        reasons.append("need at least two complete days for a day-block confidence interval")
    elif ci_high >= 0:
        reasons.append(
            "day-block 95% confidence interval does not exclude harm "
            f"(upper {ci_high:.6f} kWh/slot)"
        )

    bias_degradation = decision_metrics.get("absolute_bias_degradation_kwh")
    if (
        bias_degradation is None
        or bias_degradation > criteria.max_bias_degradation_kwh
    ):
        shown = "unavailable" if bias_degradation is None else f"{bias_degradation:.6f}"
        reasons.append(
            "absolute bias degradation "
            f"{shown} kWh/slot exceeds {criteria.max_bias_degradation_kwh:.6f}"
        )

    max_adjustment = decision_metrics.get("max_abs_shadow_adjustment_kwh")
    if (
        max_adjustment is None
        or max_adjustment > criteria.max_abs_shadow_adjustment_kwh
    ):
        shown = "unavailable" if max_adjustment is None else f"{max_adjustment:.6f}"
        reasons.append(
            "maximum shadow adjustment "
            f"{shown} kWh exceeds {criteria.max_abs_shadow_adjustment_kwh:.6f}"
        )

    passed = not reasons
    return {
        "pass": passed,
        # A PASS is deliberately not an enablement command or recommendation.
        "recommendation": "MANUAL_REVIEW_ONLY" if passed else "KEEP_APPLY_OFF",
        "reasons": reasons,
    }


def _pair_metrics(
    records: Iterable[dict[str, Any]],
    *,
    actual_field: str,
    baseline_field: str,
    shadow_field: str,
    criteria: ValidationCriteria,
    extra_validator: Callable[[dict[str, Any]], str | None] | None = None,
) -> dict[str, Any]:
    """Calculate per-day and aggregate errors for one baseline/shadow pair."""
    # Date -> canonical 15-minute start -> (revision key, actual, baseline, shadow).
    # The mapping prevents duplicate settlement records from inflating a day's
    # sample count; a corrected newer record deterministically replaces an old
    # one for the same interval.
    by_day: dict[str, dict[str, tuple[datetime, tuple[float, str], float, float, float]]] = defaultdict(dict)
    rejected: Counter[str] = Counter()
    input_records = 0
    candidate_records = 0
    duplicate_slot_records = 0

    for record in records:
        input_records += 1
        if record.get("kind") != "settlement":
            rejected["kind_not_settlement"] += 1
            continue
        if record.get("incomplete"):
            rejected["incomplete"] += 1
            continue
        if extra_validator is not None:
            reason = extra_validator(record)
            if reason:
                rejected[reason] += 1
                continue
        canonical_start = _canonical_slot_start(record)
        if canonical_start is None:
            rejected["invalid_or_unaligned_slot_start"] += 1
            continue
        values: list[float] = []
        bad = None
        for field in (actual_field, baseline_field, shadow_field):
            value = _number(record.get(field))
            if value is None:
                bad = field
                break
            values.append(value)
        if bad is not None:
            rejected[f"missing_or_invalid:{bad}"] += 1
            continue
        actual, baseline, shadow = values
        if actual < 0 or baseline < 0 or shadow < 0:
            rejected["negative_energy_value"] += 1
            continue
        candidate_records += 1
        date = canonical_start.date().isoformat()
        slot_key = canonical_start.isoformat()
        candidate = (
            canonical_start,
            _record_revision_key(record),
            actual,
            baseline,
            shadow,
        )
        previous = by_day[date].get(slot_key)
        if previous is not None:
            duplicate_slot_records += 1
            if candidate[1] <= previous[1]:
                continue
        by_day[date][slot_key] = candidate

    day_reports: list[dict[str, Any]] = []
    selected: list[tuple[float, float, float]] = []
    dropped_days: list[dict[str, Any]] = []
    raw_day_deltas: list[float] = []
    for date in sorted(by_day):
        entries = sorted(by_day[date].values(), key=lambda item: item[0])
        rows = [(actual, baseline, shadow) for _, _, actual, baseline, shadow in entries]
        slot_count = len(rows)
        if entries:
            span_slots = int(round(
                (entries[-1][0].timestamp() - entries[0][0].timestamp()) / (15 * 60)
            )) + 1
            expected_calendar_slots, coverage_ratio, has_calendar_boundaries = (
                _calendar_day_coverage(entries)
            )
        else:  # pragma: no cover - maps only receive validated entries
            span_slots = 0
            expected_calendar_slots = 0
            coverage_ratio = 0.0
            has_calendar_boundaries = False
        if slot_count < criteria.min_slots_per_day:
            dropped_days.append({
                "date": date,
                "distinct_slots": slot_count,
                "span_slots": span_slots,
                "expected_calendar_slots": expected_calendar_slots,
                "coverage_ratio": _round(coverage_ratio),
                "reason": "insufficient_distinct_slots",
            })
            continue
        if criteria.require_calendar_day_boundaries and not has_calendar_boundaries:
            dropped_days.append({
                "date": date,
                "distinct_slots": slot_count,
                "span_slots": span_slots,
                "expected_calendar_slots": expected_calendar_slots,
                "coverage_ratio": _round(coverage_ratio),
                "reason": "missing_calendar_day_boundary",
            })
            continue
        if coverage_ratio < criteria.min_slot_coverage_ratio:
            dropped_days.append({
                "date": date,
                "distinct_slots": slot_count,
                "span_slots": span_slots,
                "expected_calendar_slots": expected_calendar_slots,
                "coverage_ratio": _round(coverage_ratio),
                "reason": "insufficient_slot_coverage",
            })
            continue
        baseline_errors = [abs(baseline - actual) for actual, baseline, _ in rows]
        shadow_errors = [abs(shadow - actual) for actual, _, shadow in rows]
        baseline_bias = mean(baseline - actual for actual, baseline, _ in rows)
        shadow_bias = mean(shadow - actual for actual, _, shadow in rows)
        baseline_mae = mean(baseline_errors)
        shadow_mae = mean(shadow_errors)
        day_reports.append({
            "date": date,
            "slots": slot_count,
            "span_slots": span_slots,
            "expected_calendar_slots": expected_calendar_slots,
            "coverage_ratio": _round(coverage_ratio),
            "baseline_mae_kwh": _round(baseline_mae),
            "shadow_mae_kwh": _round(shadow_mae),
            "mae_delta_kwh": _round(shadow_mae - baseline_mae),
            "baseline_bias_kwh": _round(baseline_bias),
            "shadow_bias_kwh": _round(shadow_bias),
        })
        # Keep the unrounded value for the confidence calculation.  Presentation
        # rounding must never change a statistical decision near a gate boundary.
        raw_day_deltas.append(shadow_mae - baseline_mae)
        selected.extend(rows)

    if selected:
        baseline_mae = mean(abs(baseline - actual) for actual, baseline, _ in selected)
        shadow_mae = mean(abs(shadow - actual) for actual, _, shadow in selected)
        baseline_bias = mean(baseline - actual for actual, baseline, _ in selected)
        shadow_bias = mean(shadow - actual for actual, _, shadow in selected)
        relative_improvement = (
            (baseline_mae - shadow_mae) / baseline_mae if baseline_mae > 0 else None
        )
        adjustments = [abs(shadow - baseline) for _, baseline, shadow in selected]
    else:
        baseline_mae = shadow_mae = baseline_bias = shadow_bias = None
        relative_improvement = None
        adjustments = []

    day_deltas = raw_day_deltas
    improved_days = sum(delta < -1e-12 for delta in day_deltas)
    tied_days = sum(abs(delta) <= 1e-12 for delta in day_deltas)
    worsened_days = sum(delta > 1e-12 for delta in day_deltas)
    absolute_bias_degradation = (
        abs(shadow_bias) - abs(baseline_bias)
        if shadow_bias is not None and baseline_bias is not None else None
    )
    bootstrap = _day_block_bootstrap(
        raw_day_deltas,
        samples=criteria.bootstrap_samples,
        seed=criteria.bootstrap_seed,
    )
    decision_metrics = {
        "eligible_days": len(day_reports),
        "relative_mae_improvement": relative_improvement,
        "improved_days": improved_days,
        "absolute_bias_degradation_kwh": absolute_bias_degradation,
        "max_abs_shadow_adjustment_kwh": max(adjustments) if adjustments else None,
        "day_block_bootstrap": bootstrap,
    }
    metrics: dict[str, Any] = {
        "input_records": input_records,
        "candidate_records": candidate_records,
        "duplicate_slot_records": duplicate_slot_records,
        "distinct_candidate_slots": sum(len(day) for day in by_day.values()),
        "eligible_days": len(day_reports),
        "eligible_slots": len(selected),
        "dropped_partial_days": dropped_days,
        "rejected_records": dict(sorted(rejected.items())),
        "baseline": {
            "mae_kwh": _round(baseline_mae),
            "bias_kwh": _round(baseline_bias),
        },
        "shadow": {
            "mae_kwh": _round(shadow_mae),
            "bias_kwh": _round(shadow_bias),
        },
        "relative_mae_improvement": _round(relative_improvement),
        "improved_days": improved_days,
        "tied_days": tied_days,
        "worsened_days": worsened_days,
        "absolute_bias_degradation_kwh": _round(absolute_bias_degradation),
        "max_abs_shadow_adjustment_kwh": _round(max(adjustments)) if adjustments else None,
        "day_reports": day_reports,
        "day_block_bootstrap": bootstrap,
        # Preserve unrounded values used by the gate so the report remains
        # auditable near a threshold without making its normal display noisy.
        "decision_metrics": {
            "relative_mae_improvement": relative_improvement,
            "absolute_bias_degradation_kwh": absolute_bias_degradation,
            "max_abs_shadow_adjustment_kwh": max(adjustments) if adjustments else None,
            "day_block_ci95_low_kwh": bootstrap.get("_raw_ci95_low_kwh"),
            "day_block_ci95_high_kwh": bootstrap.get("_raw_ci95_high_kwh"),
        },
    }
    metrics["gate"] = _gate(metrics, criteria, decision_metrics=decision_metrics)
    return metrics


def _load_validator(record: dict[str, Any]) -> str | None:
    """Require the measured EV-excluded load field, never total site load."""
    quality = record.get("load_meter_quality")
    if quality != "measured":
        return f"load_meter_quality:{quality or 'missing'}"
    return None


def analyze_load_shadow(
    records: Iterable[dict[str, Any]], *, criteria: ValidationCriteria | None = None
) -> dict[str, Any]:
    """Evaluate weather/HVAC load shadow against settled base-load records."""
    criteria = criteria or ValidationCriteria()
    report = _pair_metrics(
        records,
        actual_field="base_load_kwh",
        baseline_field="baseline_load_forecast_kwh",
        shadow_field="weather_load_shadow_kwh",
        criteria=criteria,
        extra_validator=_load_validator,
    )
    report.update({
        "status": "ready",
        "actual_field": "base_load_kwh",
        "baseline_field": "baseline_load_forecast_kwh",
        "shadow_field": "weather_load_shadow_kwh",
        "scope": "settled measured base load (total load minus measured EV charging)",
    })
    return report


def _pv_stage_validator(record: dict[str, Any]) -> str | None:
    stage = record.get(PV_FINAL_STAGE_FIELD)
    if stage != PV_FINAL_STAGE_VALUE:
        return f"{PV_FINAL_STAGE_FIELD}:{stage or 'missing'}"
    version = record.get(PV_PIPELINE_VERSION_FIELD)
    if not isinstance(version, str) or not version.strip():
        return f"{PV_PIPELINE_VERSION_FIELD}:missing"
    return None


def _matched_pv_report(
    records: list[dict[str, Any]], criteria: ValidationCriteria
) -> dict[str, Any]:
    required = [
        PV_FINAL_BASELINE_FIELD,
        PV_FINAL_SHADOW_FIELD,
        PV_FINAL_STAGE_FIELD,
        PV_PIPELINE_VERSION_FIELD,
    ]
    staged = []
    instrumentation_rejections: Counter[str] = Counter()
    for record in records:
        if record.get("kind") != "settlement" or record.get("incomplete"):
            continue
        reason = _pv_stage_validator(record)
        if reason:
            instrumentation_rejections[reason] += 1
            continue
        if _number(record.get(PV_FINAL_BASELINE_FIELD)) is None:
            instrumentation_rejections[f"missing_or_invalid:{PV_FINAL_BASELINE_FIELD}"] += 1
            continue
        if _number(record.get(PV_FINAL_SHADOW_FIELD)) is None:
            instrumentation_rejections[f"missing_or_invalid:{PV_FINAL_SHADOW_FIELD}"] += 1
            continue
        staged.append(record)

    if not staged:
        return {
            "status": "needs_instrumentation",
            "required_fields": required,
            "expected_stage": PV_FINAL_STAGE_VALUE,
            "rejected_records": dict(sorted(instrumentation_rejections.items())),
            "gate": {
                "pass": False,
                "recommendation": "KEEP_APPLY_OFF",
                "reasons": [
                    "no records contain both PV branches after the explicit post-nowcast stage"
                ],
            },
        }

    versions = sorted({str(record[PV_PIPELINE_VERSION_FIELD]).strip() for record in staged})
    if len(versions) != 1:
        return {
            "status": "mixed_pipeline_versions",
            "required_fields": required,
            "expected_stage": PV_FINAL_STAGE_VALUE,
            "pipeline_versions": versions,
            "rejected_records": dict(sorted(instrumentation_rejections.items())),
            "gate": {
                "pass": False,
                "recommendation": "KEEP_APPLY_OFF",
                "reasons": [
                    "matched PV records span multiple nowcast pipeline versions; analyse each version separately"
                ],
            },
        }

    report = _pair_metrics(
        staged,
        actual_field="actual_pv_kwh",
        baseline_field=PV_FINAL_BASELINE_FIELD,
        shadow_field=PV_FINAL_SHADOW_FIELD,
        criteria=criteria,
        extra_validator=_pv_stage_validator,
    )
    report.update({
        "status": "ready",
        "required_fields": required,
        "expected_stage": PV_FINAL_STAGE_VALUE,
        "pipeline_version": versions[0],
        "instrumentation_rejections": dict(sorted(instrumentation_rejections.items())),
    })
    return report


def analyze_pv_shadow(
    records: Iterable[dict[str, Any]], *, criteria: ValidationCriteria | None = None
) -> dict[str, Any]:
    """Report matched PV evidence and clearly-labelled raw diagnostics.

    The legacy raw comparison remains useful for trend inspection, but it is
    deliberately incapable of passing the live-apply evidence gate.
    """
    criteria = criteria or ValidationCriteria()
    rows = list(records)
    matched = _matched_pv_report(rows, criteria)
    raw = _pair_metrics(
        rows,
        actual_field="actual_pv_kwh",
        baseline_field="baseline_pv_forecast_kwh",
        shadow_field="weather_pv_shadow_kwh",
        criteria=criteria,
    )
    raw["status"] = "diagnostic_only"
    raw["stage"] = "raw_pre_nowcast"
    raw["gate"] = {
        "pass": False,
        "recommendation": "KEEP_APPLY_OFF",
        "reasons": [
            "raw PV branches are not a matched representation of the final live nowcast pipeline"
        ],
    }
    return {
        "matched": matched,
        "raw_pre_nowcast_diagnostic": raw,
    }


def analyze_records(
    records: Iterable[dict[str, Any]], *, criteria: ValidationCriteria | None = None
) -> dict[str, Any]:
    """Build the complete read-only weather/HVAC validation report."""
    criteria = criteria or ValidationCriteria()
    rows = list(records)
    load = analyze_load_shadow(rows, criteria=criteria)
    pv = analyze_pv_shadow(rows, criteria=criteria)
    matched_gate = (pv.get("matched") or {}).get("gate") or {}
    load_ready = bool(load.get("gate", {}).get("pass"))
    pv_ready = bool(matched_gate.get("pass"))
    reviewable_branches = [
        name for name, passed in (
            ("HVAC_LOAD_APPLY", load_ready),
            ("PV_WEATHER_APPLY", pv_ready),
        ) if passed
    ]
    return {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "read_only": True,
        "criteria": asdict(criteria),
        "records_loaded": len(rows),
        "load": load,
        "pv": pv,
        "overall": {
            # The two gates are independent. A strong load result should not be
            # hidden behind newly-added PV instrumentation, and vice versa.
            # This aggregate means "at least one branch deserves a human
            # review", never "turn both branches on".
            "pass": bool(reviewable_branches),
            "recommendation": (
                "MANUAL_REVIEW_ONLY" if reviewable_branches else "KEEP_APPLY_OFF"
            ),
            "reviewable_branches": reviewable_branches,
            "feature_gates": {
                "HVAC_LOAD_APPLY": load.get("gate") or {},
                "PV_WEATHER_APPLY": matched_gate,
            },
            "note": (
                "Each branch is independent. A pass is evidence for a human review "
                "only; this tool never changes HVAC_LOAD_APPLY or PV_WEATHER_APPLY."
            ),
        },
    }


def load_history_records(history_dir: str | Path, only_date: str | None = None) -> list[dict[str, Any]]:
    """Read stored history without writing or importing the optimizer.

    The normal path uses the existing history-store reader, so compacted Parquet
    months are included where DuckDB is available.  A small NDJSON fallback is
    used *only* when that reader cannot be imported.  Any actual store/read
    failure raises :class:`HistoryReadError`: silently ignoring a Parquet or
    filesystem error could produce a plausible but incomplete evidence report.
    """
    directory = str(history_dir)
    try:
        from lib import history_store
    except ImportError:
        history_store = None

    if history_store is not None:
        try:
            dates = [only_date] if only_date else history_store.available_days_strict(directory)
            rows: list[dict[str, Any]] = []
            for date in dates:
                rows.extend(history_store.read_day_strict(date, directory))
            return rows
        except Exception as error:
            raise HistoryReadError(
                f"history-store read failed for {directory}: {error}"
            ) from error

    pattern = f"ess-{only_date}.ndjson" if only_date else "ess-*.ndjson"
    rows = []
    for path in sorted(Path(directory).glob(pattern)):
        try:
            with path.open(encoding="utf-8") as handle:
                for raw in handle:
                    try:
                        value = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        rows.append(value)
        except OSError:
            continue
    return rows
