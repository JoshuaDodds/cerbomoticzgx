# Vehicle control UI — 2026-10-21

Vehicle is now a top-level dashboard page at `#vehicle`, beside Victron in the
desktop header and in the mobile application menu. Existing `#ess/vehicle`
bookmarks are redirected to the new route. The Power Flow EV card opens it.

The Charge control card now shows a Start/Stop request as it is sent, whether
the dashboard service accepted it, and the latest measured charger power.
Accepted means the controller received an intent; it is not confirmation that
the car has started or stopped. Failed and timed-out feedback remains visible
for one minute.
The same action is disabled for 30 seconds after acceptance to prevent repeated
requests; Stop remains available after an accepted Start. The existing Grid
assist toggle is available on this page for immediate grid-backed starts.

Manual Start now requires Grid assist at the dashboard service. When it is off,
`POST /api/control/ev-charge` returns HTTP 409 with `grid_assist_required`
before writing `ev_charge_requested` or publishing the retained control topic.
The Vehicle card shows **Start blocked** and leaves the button available; the
service logs the rejection rather than “START requested.” Smart and protected
solar charging remain automatic and independent of the manual Start button.

The EV controller now publishes its observed manual Start decision as a
`code|timestamp` value on `Cerbomoticzgx/GlobalState/ev_charge_decision` and
logs each change once. The Vehicle card shows why a current Start has not begun:
unavailable vehicle telemetry, away/unplugged state,
charge limit reached, or pending charger delivery. The dashboard shows a Grid
assist hint immediately after the request; the controller's own decision replaces
that hint on its next tick (normally within 30 seconds). This is diagnostic only;
the Start authorization and charger command rules are unchanged.
While a Start remains pending the controller refreshes the decision timestamp
every two minutes.

## 24 October 2026: pending-status lifecycle

Manual Start now records its original request time. If the car is idle, the
original controller cleared the intent when Grid assist was off or 15 minutes
after the request. The age-based cancellation was removed on 1 November; see
the correction below.

The Vehicle card drops local button feedback after one minute. It shows a
pending controller decision only when the retained Start intent is on, Grid
assist is on, the original request is under 15 minutes old, and the decision
was refreshed within three minutes and after that request began. Otherwise it
shows a neutral status and keeps live charger power visible. This prevents
retained MQTT messages and long-open browser tabs from showing an obsolete
Start as pending.

## 31 October 2026: charging evidence freshness

ABB phase-current samples now carry individual receive timestamps. A stale
retained current cannot establish active charging or prevent cleanup after
Grid assist is disabled. Fresh EVCS power and recent vehicle charging evidence remain available
to the existing reconciliation logic.

## 1 November 2026: preserve manual authorization

The 09:10 incident showed the pending timeout clearing manual authorization
during missing/idle delivery, after which normal reconciliation stopped the car
as unauthorized. Manual Start now remains authorized while Grid assist is on,
including legacy undated requests and temporary EVCS or telemetry pauses.
Only presentation feedback expires. Stop and disabling Grid assist retain their
existing authority. After progress feedback expires, the card reports that manual
grid charging is enabled rather than claiming no request exists.
