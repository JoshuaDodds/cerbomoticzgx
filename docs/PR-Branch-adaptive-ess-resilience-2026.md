# PR: Adaptive ESS strategy and outage resilience

## Purpose

Make ESS dispatch resilient to cloudy, thin-spread and incomplete-horizon days
without regressing the existing Summer Trading path or the restart-isolated
Winter engine.

## Behaviour

- Summer Trading remains byte-for-byte selected when
  `ESS_ADAPTIVE_POLICY_ENABLED=False` (the default).
- When enabled, the live Summer DP evaluates Trading, PV-first and protected
  hybrid constraint sets. Trading must clear a configured benefit plus a
  configured fraction of capped learned forecast risk; common historical error
  is not charged in full after per-candidate lifecycle/arbitrage risk. A durable
  dwell prevents quarter-hour policy flapping.
- Protected hybrid reserves forecast household energy until the next cheap
  replenishment opportunity and a bounded continuation after the last known
  price slot. Neighbouring cheap slots form one procurement valley; the required
  energy must be reached by its final cheap slot, while grid charging remains
  restricted to genuinely cheap slots. A low live SoC can no longer remain in
  RETAIN indefinitely below the candidate's own household requirement.
- Winter Mode protects its 40% emergency backup floor while grid-connected,
  self-supplies expensive household demand above it, and applies the same
  unknown-horizon allowance at every final horizon boundary.
- An explicit Victron grid-offline signal suspends all optimizer control writes.
  Forecasting, planning, history and settlement continue, and the emergency
  reserve is available to the house. Missing startup state is unknown, not
  offline.
- Manual Override now suppresses writes only; the dashboard and historical
  evidence continue updating.

## Auditability and safety

The selected strategy, candidate scores, decision hurdle, controller authority
and suppression reason are included in plan JSON and cycle history. The feature
does not modify `VICTRON_HARDWARE_MIN_SOC`. The separate Advisor/CLI strategy
evaluator remains observational and cannot select live control.

## Validation

Targeted tests cover policy constraints, material-benefit selection, horizon
continuity, winter self-supply, explicit outage pass-through, manual-override
observability, and absence of Victron writes while control is suppressed. See
the top-level TODO for the attended multi-day production checklist before
enabling adaptive Summer dispatch permanently.
