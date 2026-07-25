# Tesla Fleet Telemetry certificate incident — 2026-07-24

## Summary

Fleet Telemetry stopped updating even though the receiver Deployment, MQTT
dispatcher, public DNS and TCP listener were healthy. The vehicle repeatedly
reached `fleet.hs.mfis.net:6443` but rejected the receiver during TLS setup.

The vehicle's stored Fleet Telemetry configuration contained the previous
short-lived `hs.mfis.net` leaf certificate and its `R13` issuer. The Kubernetes
receiver had renewed to a leaf issued by `YR2`. Because the renewable leaf had
effectively been pinned in the vehicle configuration, an ordinary certificate
renewal made the two sides incompatible.

The configuration was repaired once with a CA-only trust bundle:

```text
Let's Encrypt YR2
  -> ISRG Root YR (cross-signed by ISRG Root X1)
  -> ISRG Root X1
```

The renewable `hs.mfis.net` leaf is deliberately absent. Normal leaf renewal,
including a change between YR intermediates under the same trusted roots,
should therefore remain valid.

## Impact

- Fleet MQTT topics retained their last values but stopped receiving fresh
  vehicle signals.
- Dashboard/manual refresh could obtain newer state through the guarded REST
  fallback, which initially obscured the fact that the Fleet stream itself was
  still stale.
- EV charge-control acknowledgement became unavailable until the stream was
  restored.
- No partner registration, virtual key, signing key or OAuth credential had
  been lost.

## Evidence and diagnosis

1. The receiver pod was healthy and listening, but initially logged only its
   startup messages.
2. A packet capture proved the vehicle could reach the MetalLB service and
   complete TCP setup.
3. Temporarily setting
   `SUPPRESS_TLS_HANDSHAKE_ERROR_LOGGING=false` exposed the decisive receiver
   messages:

   ```text
   remote error: tls: bad certificate
   tls: client didn't provide a certificate
   EOF
   ```

   `remote error: tls: bad certificate` was the useful vehicle-side evidence:
   the peer rejected the receiver certificate. The missing-client-certificate,
   EOF and reset messages also occur for curl/probe connections and were not
   the primary fault.
4. A read-only Fleet Telemetry configuration lookup showed the old
   `hs.mfis.net -> R13` bundle. `openssl s_client` showed the live receiver
   serving `hs.mfis.net -> YR2 -> Root YR`.
5. The old and live leaf fingerprints differed. The old configuration had
   included the renewable leaf instead of relying on durable CA trust.
6. `secrets/tesla-fleet/private-key.pem` and `public-key.pem` were verified as
   a matching command-signing pair, and the public key still matched Tesla's
   registered partner-domain key. They were unrelated to the receiver TLS
   certificate.

## Recovery performed

1. Extract the current issuer certificates from the Kubernetes TLS secret,
   excluding certificate number one (the renewable server leaf).
2. Append the stable `ISRG Root X1` trust anchor.
3. Confirm the candidate contains exactly `YR2`, `Root YR`, and
   `ISRG Root X1`, with no `hs.mfis.net` certificate.
4. Verify the live receiver leaf and hostname:

   ```bash
   openssl verify \
     -CAfile tesla-fleet-ca.pem \
     -verify_hostname fleet.hs.mfis.net \
     live-leaf.pem
   ```

5. Start `tesla/vehicle-command:latest` as a temporary localhost-only HTTP
   proxy using the existing read-only application signing key.
6. Run `scripts/tesla_push_telemetry_config.py` once with the same VIN,
   hostname, port and 15 fields, replacing only the CA bundle.
7. Tesla returned:

   ```json
   {"response":{"updated_vehicles":1}}
   ```

8. After waking the vehicle, MQTT published a new `CONNECTED` record and all
   15 configured fields. The temporary proxy was stopped and removed, and
   receiver TLS-error suppression was restored.

Do not repeatedly resend the configuration. First compare Tesla's stored
configuration with the live certificate and use the connectivity record as the
functional acknowledgement. Tesla's `synced` field may lag the actual vehicle
connection.

## Secondary network finding

The certificate mismatch—not routing—caused this incident. The investigation
did uncover a separate LAN hairpin weakness:

- EdgeRouter `eth1` owns both `192.168.0.0/24` and `192.168.1.0/24`.
- The generated `NETv4_eth1` ipset contained only `192.168.0.0/24`.
- Automatic hairpin SNAT therefore never matched a vehicle on
  `192.168.1.0/24`.

A narrow manual source-NAT rule was added only for TCP traffic from
`192.168.1.0/24` to the Fleet receiver service at `192.168.1.33:6443`,
translated to `192.168.1.1`. Packet capture then showed successful hairpin TCP
and TLS traffic. This hardens a future Wi-Fi connection, but the restored
vehicle connection used cellular and proves the public path independently.

Keep the public `/.well-known/` application key available on port 443. It was
not the cause here, but Tesla documents continued public-key availability as a
Fleet prerequisite.

## Fast diagnostic path for a future outage

1. Check `telemetry/<VIN>/connectivity` before polling the vehicle. Record
   `Status`, `CreatedAt`, and whether subsequent `v/*` topics are fresh.
2. Check receiver pod readiness, service endpoints and recent logs.
3. If normal logging is suppressed, temporarily expose TLS handshake errors.
4. Interpret `remote error: tls: bad certificate` as a certificate/trust
   problem before changing DNS, NAT, OAuth or partner registration.
5. Compare:
   - Tesla's stored Fleet `hostname`, `port`, CA subjects and CA fingerprint;
   - the live leaf/issuer chain from `openssl s_client`;
   - hostname and chain verification using Tesla's
     `check_server_cert.sh` approach.
6. Confirm the receiver presents its complete current chain.
7. Only if the trust configurations differ, build a CA-only bundle and push it
   once through the signed command proxy.
8. Wake the vehicle normally, then require a new `CONNECTED` event and fresh
   telemetry fields before declaring recovery.
9. Restore suppressed TLS logging and remove the temporary localhost proxy.

## Preventive follow-up

- Make `tesla_push_telemetry_config.py` reject a CA file containing the
  configured server leaf.
- Retain a documented CA-only bundle outside ephemeral `/tmp` storage.
- Validate the renewed live certificate against that bundle after every ACME
  renewal, without spending a Fleet API command.
- Alert on certificate validation failure before restarting the receiver with
  an incompatible chain.

