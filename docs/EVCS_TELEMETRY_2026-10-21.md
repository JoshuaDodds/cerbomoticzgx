# EVCS and ABB telemetry mapping — 2026-10-21

Live inspection of the Cerbo MQTT broker found two separate services:

| Device | MQTT service | Measurements used |
|---|---|---|
| Victron EV Charging Station | `N/<portal-id>/evcharger/40` | `Ac/Power` for live EV watts; `ChargingTime` for the current session |
| ABB B23 meter | `N/<portal-id>/acload/42` | `Ac/Power` for the existing Domoticz EV counter and charge-transition history; `Ac/Energy/Forward` for lifetime energy and settlement; `Ac/L1/Current` through `Ac/L3/Current` for measured phase currents |

The ABB meter still reports its established lifetime energy total. The EVCS
reports a much newer lifetime total, so substituting that value would break
historical continuity. Historical records are neither rewritten nor rebased.
The existing Tesla Fleet API commands and vehicle topics remain in use.

The ABB device instance stayed at `42`, but its Venus OS service role is now
`acload` rather than `evcharger`. This explains why the old topic paths stopped
updating even though the ABB meter itself still publishes on the bus.
