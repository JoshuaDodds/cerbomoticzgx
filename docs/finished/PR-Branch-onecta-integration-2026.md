# PR branch: Onecta HVAC integration exploration

## Status

Phase 0 passed. Phase 1 provides a feature-gated background collector,
cumulative HVAC history, retained `hvac/#` MQTT state, bounded optimizer
freshness coordination, and a top-level HVAC dashboard. Monitoring and manual
control have independent gates: read-only cards remain available when control
is off. The collected values remain observational and do not yet alter the
optimizer's load forecast.

## Goal

Determine whether the official Daikin ONECTA Cloud API exposes meaningful,
stable data for the four installed split-unit heat pumps. In particular:

- discover all indoor units without hardcoding a device count;
- identify the unit model, friendly name, availability, operating mode, power
  state, target temperature, indoor/outdoor temperatures, and fan state;
- determine whether electrical consumption data is exposed, its units, time
  resolution, update delay, and whether it represents indoor or shared outdoor
  equipment;
- determine whether the four indoor units can be mapped to the two shared
  outdoor units;
- assess whether the data can improve HVAC load classification and forecasting.

## Safety and privacy boundaries

- Monitoring and control both default to disabled, independently.
- The POC performs only OAuth token operations and `GET` requests.
- Runtime writes require `ONECTA_CONTROL_ENABLED=True`, advertised device
  capability, server-side range/enum validation and available request reserve.
- Client credentials and rotating tokens belong only in the writable
  `.secrets` file and must never be printed, logged, committed, or included in
  captured fixtures.
- Diagnostic output must redact access tokens, refresh tokens, client secrets,
  device identifiers, embedded identifiers, account identifiers, and precise
  location data before it is saved.
- Network work must not run in or block the main energy-control loop.

## Official API constraints

Private applications are limited by Daikin to 200 calls per rolling day. The
runtime connector fetches all gateway devices in one request on a 20-minute
cadence (approximately 72 reads/day), reuses a snapshot younger than 19 minutes
on restart, and retains headroom for authentication, retries and guarded
controls.

## Configuration shape

Runtime feature gates, if the POC succeeds:

```dotenv
ONECTA_ENABLED=False
ONECTA_CONTROL_ENABLED=False
ONECTA_POLL_INTERVAL_MIN=20
ONECTA_EXPECTED_UNITS=0
ONECTA_DAILY_REQUEST_RESERVE=10
ONECTA_STARTUP_MAX_AGE_SECONDS=1140
ONECTA_OPTIMIZER_MAX_AGE_SECONDS=1140
ONECTA_OPTIMIZER_WAIT_SECONDS=5
ONECTA_LATEST_PATH=data/hvac/latest.json
ONECTA_HISTORY_PATH=data/hvac/history.ndjson
ONECTA_CONTROL_REGISTRY_PATH=data/hvac/control_registry.json
```

Credentials and rotating tokens:

```dotenv
ONECTA_CLIENT_ID=
ONECTA_CLIENT_SECRET=
ONECTA_REDIRECT_URI=
ONECTA_ACCESS_TOKEN=
ONECTA_REFRESH_TOKEN=
```

No secret values will be added to `.env` or `.env.example`.

## Phase 0: read-only proof of concept

1. Register a private application in the Daikin Developer Portal using the same
   account and login method as the ONECTA mobile app. **Complete.**
2. Register `https://ess.hs.mfis.net/onecta/oauth/callback`. Daikin's dynamic
   client registration rejects `localhost` and loopback redirects, including
   HTTPS loopback URLs. **Complete.**
3. Ignore the disabled catalog `Register for v1` action. Private ONECTA clients
   authorize directly with the application credentials; the portal can continue
   to display `No Products`.
4. Complete the authorization-code flow interactively with
   `scripts/onecta_poc.py authorize` and `exchange`. **Complete.**
5. Atomically store rotating access/refresh tokens in `.secrets`. **Complete.**
6. Execute one `GET /v1/gateway-devices` request with
   `scripts/onecta_poc.py discover`. **Complete.**
7. Produce an owner-only, redacted capability report. **Complete.**
8. Stop and review the evidence before implementing a runtime connector.
   **Complete; Phase 1 authorized.**

The probe deliberately contains no PATCH/control implementation. It validates
the exact callback origin/path and short-lived OAuth state, rejects stale or
replayed authorization state, sanitizes remote failures, refreshes once after a
401, and never writes the raw gateway-device response to disk.

### Continue criteria

Continue to a production read-only connector only when:

- all expected units are discoverable;
- useful operating-state fields are present and update reliably;
- authentication refresh can be made durable without repeated user login;
- the polling budget is sustainable; and
- at least state-based forecasting features, consumption counters, or both are
  materially useful.

### Stop or reconsider criteria

Pause the integration when:

- the expected units are missing;
- the API exposes no useful state beyond what is already inferred;
- consumption data is absent and unit state is too delayed for forecasting;
- token refresh is unreliable; or
- the required API usage cannot fit safely within the private rate limit.

## Delivery phases

1. Read-only background connector and retained `hvac/#` MQTT state.
   **Implemented.**
2. Detailed HVAC history plus compact ESS forecasting features.
   **Cumulative history and ESS context implemented; forecast use pending
   validation.**
3. Shadow-model validation against the existing weather-only forecast.
4. Guarded manual controls behind `ONECTA_CONTROL_ENABLED`.
   **Implemented for capabilities advertised by the installed units.**
5. Comfort-aware optimizer orchestration only after separate review.

## Phase 0 result — 2026-07-27

The authorization-code exchange succeeded, returned a refresh token, and stored
the rotating token pair atomically in `.secrets`. The pending OAuth state was
consumed and deleted. One read-only `GET /v1/gateway-devices` request found all
four expected units and left 199 of the private application's 200 rolling daily
requests available.

The owner-only redacted report at `data/onecta_capabilities.json` confirms:

- four connected `dx4` devices using BRP069C4x gateways;
- exact on/off state and cooling/heating/auto operation mode;
- room and outdoor temperature readings;
- separate cooling/heating target setpoints and permitted ranges;
- fan direction/speed capabilities, powerful mode, holiday/error/warning state;
- per-unit electrical consumption in kWh, separated into cooling and heating;
- a 24-value `d` series, 14-value `w` series, and 24-value `m` series for each
  mode on every unit.

At discovery time all units were off in cooling mode, room temperatures were
24–25 C, and reported outdoor temperatures were 20–22.5 C. The four `d`
cooling series contained 2.0 kWh in total. Comparison with the official mobile
app established that the 24 entries are two adjacent days of twelve two-hour
buckets: the first 12 summed to yesterday's 1.6 kWh and the final 12 summed to
today's 0.4 kWh. The collector rejects any other array length rather than
publishing a plausible but incorrect total.

### Decision

Phase 0 passes the continue criteria. The data is materially useful for HVAC
classification and forecasting. It does not expose instantaneous compressor
watts, so the existing weather model must remain in place while Onecta state
and consumption deltas are evaluated in shadow mode.

## Phase 1: read-only runtime collector

Phase 1 is implemented with these boundaries:

- `ONECTA_ENABLED=False` by default and a dashboard Configuration toggle;
- a daemon collector reuses cached state younger than 19 minutes at startup,
  otherwise refreshes in the background, then reads at `:10`, `:30` and `:50`;
- a single-flight guard and minimum request spacing prevent manual replans from
  multiplying API reads;
- incomplete responses are rejected against the configured or previously
  learned unit count, preventing partial household totals;
- the last known rate-limit headers reserve the final ten daily requests;
- an optimizer cycle accepts a snapshot up to 19 minutes old, or waits at
  most five seconds for an in-flight refresh before continuing safely;
- an unavailable or slow Daikin cloud never blocks application startup and
  never prevents ESS optimization;
- rotating OAuth tokens continue to be persisted atomically;
- `data/hvac/latest.json` is replaced atomically and
  `data/hvac/history.ndjson` appends only source-state changes;
- ESS cycle history also records freshness, cumulative HVAC kWh, powered unit
  count and powered modes for later correlation with weather and base load;
- no HVAC energy is applied to the load forecast in this phase.

Retained MQTT state:

```text
hvac/status
hvac/summary
hvac/units/unit-<stable anonymous hash>
```

Each public unit state includes its local display name, cloud connection, source
timestamp, on/off state, configured mode, target/room/outdoor temperatures, fan
state, health flags, and correctly separated today/yesterday heating and
cooling kWh. Raw IDs are never published; the minimum gateway/embedded ID pair
needed for control is stored only in a mode-0600 private registry and never
returned to the browser. Serials, network identifiers and credentials are not
included.

## Phase 2: HVAC page and guarded manual control

- The **HVAC** top-level view appears between Battery and Victron only when
  monitoring is enabled and a valid snapshot exists.
- Monitoring remains useful with `ONECTA_CONTROL_ENABLED=False`: unit state,
  temperatures, modes and energy are visible, controls are disabled, and a
  read-only notice explains why.
- The server renders controls from each unit's live advertised capabilities;
  unsupported features are omitted rather than guessed.
- Supported controls include on/off, operating mode, mode-specific setpoint,
  fan mode/level, horizontal/vertical airflow and powerful mode where the unit
  advertises them as settable.
- The responsive dashboard uses an at-a-glance daily heating/cooling rail and
  always-visible unit cards with temperature rings, segmented modes/fan speeds,
  a setpoint stepper, airflow buttons, and power switches. Off units remain
  readable and interactive rather than hiding their controls behind expansion.
- Several quick setpoint taps are debounced into one final API write, protecting
  the 200-request daily allowance without making local interaction feel slow.
- Operating-mode buttons expose concise Daikin behavior descriptions through
  native hover help and accessible labels.
- Streamer is deliberately absent: the four installed units expose it in the
  ONECTA mobile app, but their official API payloads advertise no Streamer,
  purification, or equivalent readable/settable characteristic. The integration
  must not guess an undocumented PATCH path that it cannot safely confirm.
- Every command is idempotence-checked, serialized per unit, validated against
  advertised enum/range/step constraints, and rejected before transmission if
  it would cross the daily request reserve.
- A fresh public cache can be reused without a cloud call while control is off.
  If control is enabled and its separate private registry is absent, startup—or
  the first command after a runtime toggle—performs one all-unit hydration and
  then continues automatically.
- An accepted PATCH gets a bounded two-read convergence window (10 seconds,
  then one final read after another 20 seconds) because the physical unit can
  change before ONECTA's read model. Control never blocks application startup
  or the ESS optimizer.
- Browser and MQTT payloads contain anonymous unit keys. The private control
  registry is atomically replaced with mode 0600.

### Phase 1 validation criteria

Before forecast application is considered:

1. run the collector for multiple complete cooling and heating days;
2. compare combined `today_total_kwh` with the mobile app at several times;
3. verify the midnight shift moves today's total into yesterday without
   combining the two;
4. correlate cumulative increments with measured AC base-load changes;
5. fit and hold out weather/HVAC forecast coefficients using historical data;
6. only enable forecast application after it materially lowers error.
