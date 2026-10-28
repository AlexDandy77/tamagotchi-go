# Service Discovery contract

Service Discovery keeps the list of running service instances, polls their health and alerts the developers. It owns no game data and no database; its registry lives in memory and is rebuilt from heartbeats after a restart. It is written in Python 3.13 with aiohttp, lives in the private repository `AlexDandy77/pad-team-8-service-discovery` and is published as `alexdandy77/pad-team-8-service-discovery`. Routes are described in [service-discovery.openapi.yaml](service-discovery.openapi.yaml).

## Addressing and identity

Gateway and every registered service set `SERVICE_DISCOVERY_URL=https://service-discovery:8443` and call it with their own CA-signed certificate. These control-plane calls go directly to Service Discovery: Gateway reads its routing pools from it, so they cannot pass through Gateway. Business REST still goes only through Gateway.

Service Discovery takes the caller's service name from the certificate's DNS SAN, as Gateway does, and the instance address from the TLS peer IP. A request body can choose neither. Gateway and Service Discovery reach an instance at `https://<ip>:<port>` and verify its certificate against the service name, so every replica uses its service's existing certificate.

## Instance lifecycle

| Step | Request | Rule |
| --- | --- | --- |
| Register | `PUT /v1/services/{service}/instances/{instanceId}` | Sent once the listeners are open. The caller's SAN must equal `{service}`. Body `{port, version, socketUrl?}`; `socketUrl` is the public WebSocket origin of a service with negotiated sockets. |
| Renew | The same request every `heartbeatSeconds` (5) | The lease lasts `leaseSeconds` (15). An unknown instance is registered again, so a Service Discovery restart heals within one heartbeat. |
| Deregister | `DELETE /v1/services/{service}/instances/{instanceId}` | Sent on graceful shutdown before the listeners close. Repeating it is harmless. |
| Expire | none | An instance without a renewal for one lease is removed and reported. |

`instanceId` is lowercase letters, digits and hyphens, at most 63 characters, unique per service. It defaults to the container hostname; Compose sets `INSTANCE_ID` only where a readable name helps, such as `monster-raid-2`. The registration answer repeats the instance with `leaseSeconds` and `heartbeatSeconds`, so callers never hardcode them.

`GET /v1/services` returns the available instances of every service and is allowed only for Gateway. Expired, unhealthy and ejected instances are left out. Gateway refreshes its pools every two seconds and keeps the last answer while Service Discovery is unreachable.

Gateway's `DISCOVERY_MANAGED_SERVICES` is a comma-separated subset of its `UPSTREAMS_JSON` names. It defaults to empty for standalone compatibility; configuring names requires `SERVICE_DISCOVERY_URL`. The common deployment lists every adopted service explicitly, so a restart never forgets which services use discovery. A managed service with no usable instances returns `503 DEPENDENCY_UNAVAILABLE`, including REST and socket negotiation; Gateway readiness returns `503 NOT_READY`. It never falls back to that service's static origin, even when the registry is empty or unreachable. Only services outside the managed list retain static fallback during migration. Cached signing keys remain usable; a managed User Management outage without usable keys returns `503 IDENTITY_UNAVAILABLE`.

Gateway and Service Discovery may probe an instance's `/status` directly over mTLS to check recovery. Those probes may reach quarantined instances but cannot send business requests through them. Business REST still passes through Gateway. No additional Discovery response field is needed: the managed-services list is deployment configuration, separate from the registry's available-instance map.

## Load balancing

Gateway picks instances of a service in round-robin order for every REST request and returns `X-Served-By: {service}/{instanceId}` so the distribution is visible. It never retries a mutation. A `GET` or `HEAD` that fails before any response byte may move to the next instance within the same task deadline.

Socket negotiation picks the next usable instance's registered `socketUrl` of Guild or Monster Raid in the same way. Static `GUILD_WS_URL` or `RAID_WS_URL` fallback is allowed only outside `DISCOVERY_MANAGED_SERVICES`; a managed empty pool cannot hand out a static socket URL. A socket stays on the instance that accepted it. [Socket tickets](socket-tickets.md) are single-use across replicas because each owner records them in its own database.

## Circuit breaker

Every caller counts failures per destination: transport errors, timeouts and `5xx` answers. `4xx` answers are business outcomes and never count. Three failures within `3.5 × TASK_TIMEOUT_SECONDS` (17.5 seconds by default) trip the breaker, which logs `circuit opened` with the destination, the failure count and the window.

- **Gateway** keys breakers by instance. A trip removes the instance from its pool at once and reports it with `POST /v1/services/{service}/instances/{instanceId}/eject`. Service Discovery quarantines the instance: it stays out of every pool until it passes three consecutive status probes, and never earlier than one window after the ejection. Heartbeats alone never return it.
- **Services** call every dependency through Gateway, so they key breakers by destination name. While open, a call fails at once with the owner's existing dependency-unavailable error (`503 DEPENDENCY_UNAVAILABLE` on REST). After 5 seconds one trial call is let through; success closes the breaker and logs `circuit closed`.

## Status endpoint

Every service, Gateway and Service Discovery serve `GET /status` on every listener. It takes no task slot, accepts any CA-signed client certificate on the mTLS listener and never returns secrets. The body is the `Status` schema:

| Field | Meaning |
| --- | --- |
| `service`, `instanceId`, `version` | Who answered |
| `status` | `ok`, or `degraded` when a check fails |
| `uptimeSeconds` | Seconds since the process started |
| `load` | `inFlight` tasks now, `peakInFlight` since the previous status read, `capacity` (`MAX_CONCURRENT_TASKS`) and `percent` = 100 × `peakInFlight` / `capacity`, rounded down. A service with another bounded resource, such as socket slots, reports the higher percentage. |
| `checks` | `ok` or `failed` per dependency the instance cannot work without, such as `database` |
| `circuits` | `closed`, `open` or `halfOpen` per outbound destination |

Reading `peakInFlight` resets it, so a burst between two polls is never missed.

## Health monitor and alerts

Service Discovery polls the status of every registered instance every `HEALTH_CHECK_INTERVAL_SECONDS` (5) with a 2-second limit.

| Alert | Raised when | Cleared when |
| --- | --- | --- |
| `load.critical` | `load.percent` ≥ `LOAD_CRITICAL_PERCENT` (80) | A later poll is below the limit |
| `instance.unhealthy` | `status` is `degraded`, or three polls in a row fail; the instance leaves every pool | Three polls in a row succeed with `ok` |
| `instance.ejected` | Gateway reports a tripped breaker | The quarantine rule above |
| `instance.expired` | The lease runs out | The instance registers again |

Each alert and each recovery is one structured log line (`WARNING` when raised, `INFO` when cleared) and, when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, a Telegram message to the developers' chat. The same alert for the same instance is repeated at most once per `ALERT_COOLDOWN_SECONDS` (60). Telegram failures are logged and never delay polling or routing. Registrations and graceful deregistrations are logged without a message.

## Logging

Every service writes JSON, one event per line, with `service` and `instanceId` on every line and `requestId` on every line that belongs to a request. Log at least:

- startup with the effective non-secret settings, each shutdown step and the stop;
- every request with method, path, status and duration;
- every outbound call with destination, method, path, status and duration;
- rejected tasks (`503 TASK_LIMIT_REACHED`, `504 TASK_TIMEOUT`), breaker changes, registration and heartbeat failures, leader changes of background workers, and socket opens and closes.

Never log tokens, tickets, keys, passwords, idempotency payloads or Telegram credentials.

## Replicas

The common deployment runs two or three instances of a service once it is instance-agnostic:

- All state lives in the owner's database. In-memory caches may differ between replicas but must not decide outcomes.
- Background workers and outbox relays run on one leader at a time, so events keep their order and remote calls are not duplicated. PostgreSQL owners use an advisory lock held by a dedicated connection; another replica takes over when that connection ends. Package Registry owns MongoDB and instead uses an atomic database lease or claim with renewal and fencing, so a previous leader cannot continue after takeover. No service writes another owner's database to obtain leadership.
- Limits such as `MAX_CONCURRENT_TASKS` apply per instance.
- A service with negotiated sockets gives each replica its own published port and `PUBLIC_SOCKET_URL`, registered as `socketUrl`. Messages for players connected to other replicas must still reach them.

## Service Discovery settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `PORT`, `INTERNAL_PORT` | `8080`, `8443` | Probe and status listener; mTLS registry listener |
| `SERVICE_TLS_CERT_FILE`, `SERVICE_TLS_KEY_FILE`, `SERVICE_TLS_CA_FILE` | required | Its certificate, key and the shared CA |
| `TASK_TIMEOUT_SECONDS`, `MAX_CONCURRENT_TASKS` | `5`, `64` | Task limits; the breaker window is 3.5 × the timeout |
| `LEASE_SECONDS`, `HEARTBEAT_SECONDS` | `15`, `5` | Registration lease and the renewal interval it announces |
| `HEALTH_CHECK_INTERVAL_SECONDS` | `5` | Status poll interval |
| `LOAD_CRITICAL_PERCENT` | `80` | Load alert threshold |
| `UNHEALTHY_AFTER_FAILURES` | `3` | Failed or recovered polls needed to change health |
| `ALERT_COOLDOWN_SECONDS` | `60` | Minimum gap between repeats of one alert |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty | Telegram delivery; alerts are logged only when either is empty. Keep them in the ignored `.env`. |

## What each owner must change

1. Serve `GET /status` as above.
2. Register, renew and deregister with `SERVICE_DISCOVERY_URL`, using `INSTANCE_ID` or the hostname, and `PUBLIC_SOCKET_URL` when the service has sockets.
3. Wrap every outbound client in the circuit breaker.
4. Apply the logging rules.
5. Make background work and sockets replica-safe as described under Replicas.
6. Publish validated merges to `main` as `3.MINOR.PATCH` and `latest`, then scale the service and update its submodule pointer in one common integration PR.

## Remaining private-service handoff

Guild and Package Registry owners keep their sources private and complete the same status, registration, breaker and logging rules before joining `DISCOVERY_MANAGED_SERVICES` and scaling. Guild also needs a distinct socket origin per replica, cross-replica chat delivery and shared ticket-replay records in its own database. Registry must prove its MongoDB scheduler/outbox leadership and enrollment inbox remain safe across replicas. Test concurrent requests, leader takeover and removal/recovery of every instance, then publish and update the common deployment. Existing Compose entries and published images do not establish that these private implementations meet the rules.
