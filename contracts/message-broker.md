# Message Broker contract

The Message Broker carries every event and every service-to-service request. It replaces Kafka and takes over the routing that Gateway did for service calls. It has two parts:

- an **event bus**: topic-based, durable, with a dead letter channel;
- a **request router**: subscriber-based queues for Gateway traffic, gRPC for service-to-service calls, with load-based balancing, circuit breakers and rerouting.

It is written in Python 3.13, lives in the private repository `arturtugui/pad-team-8-message-broker` and is published as `arturtugui/pad-team-8-message-broker`. The gRPC API is [proto/broker.proto](proto/broker.proto); the HTTP routes are in [message-broker.openapi.yaml](message-broker.openapi.yaml).

Status: **Lab 4 contract proposal**. While services migrate, Kafka keeps running next to the broker. It is removed once every producer and consumer uses the broker.

## Addressing and identity

| Listener | Address | Used for |
| --- | --- | --- |
| gRPC, mTLS | `message-broker:50051` | `EventBus` and `Router`, called by services |
| HTTPS, mTLS | `https://message-broker:8443` | Gateway's subscriber queues and the admin routes |
| HTTP | `http://message-broker:8080` | `/healthz`, `/readyz`, `/status`, `/metrics` on the internal network |

Services set `BROKER_URL=message-broker:50051`, which replaces `KAFKA_BROKERS`, `KAFKA_USERNAME` and `KAFKA_PASSWORD`. Gateway sets `BROKER_URL=https://message-broker:8443`.

The caller's service name is the DNS SAN of its CA-signed certificate, exactly as Gateway and Service Discovery read it. Neither a request body nor a header can choose the caller. The broker uses its own certificate, `message-broker`, from the same TLS bundle.

## Event bus

### Topics

Topic names, envelopes and payloads are unchanged: [events.schema.json](events.schema.json) and the README event table still apply. The broker enforces the same producer and consumer permissions that the Kafka ACLs did.

| Topic | Producers | Consumer groups |
| --- | --- | --- |
| `user.package-registered.v1` | user-management | package-registry, tamagotchi |
| `friend.requested.v1` | user-management | notification |
| `map.encountered.v1` | map | notification |
| `battle.requested.v1` | battle | notification |
| `battle.finished.v1` | battle | notification |
| `pet.used.v1` | tamagotchi | notification |
| `pet.captured.v1` | tamagotchi | notification |
| `guild.invited.v1` | guild | notification |
| `raid.started.v1` | monster-raid | notification |
| `raid.finished.v1` | monster-raid | notification |

Only a group's own service may fetch for it: group `notification` belongs to the certificate `notification`, and so on. Every topic has a dead letter channel `<topic>.dlq`. Only the broker writes to it, and services no longer publish to `.dlq` topics.

### Publishing

`EventBus.Publish(topic, key, value)`:

- `value` is the JSON event envelope.
- `key` is its `aggregateId`.
- `topic` must equal the envelope's `type`.

The broker checks that `eventId` is a UUID and that `type` matches the topic. Full schema validation stays with consumers.

The event is written to disk (SQLite WAL, `synchronous=FULL`) **before** the call returns, so an answered publish survives a broker crash. A second publish with the same `eventId` stores nothing and returns the first offset with `duplicate: true`, which replaces Kafka's idempotent producer. Producers keep publishing from their outbox and retry with the same `eventId`.

| Error (gRPC status) | When |
| --- | --- |
| `PERMISSION_DENIED` | The caller is not a producer of the topic |
| `NOT_FOUND` | Unknown topic |
| `INVALID_ARGUMENT` | Not JSON, no UUID `eventId`, `type` ≠ topic, larger than 256 KiB |
| `UNAVAILABLE` | Storage unavailable; retry later with the same `eventId` |
| `RESOURCE_EXHAUSTED` | All task slots are busy (`TASK_LIMIT_REACHED`) |

### Consuming

A consumer replica calls `EventBus.Fetch(group, topics)` in a loop, processes each delivery and then calls `Ack`. The rules:

- **Fan-out.** Each published event is delivered once to **every** group of its topic. Within a group, one replica gets it.
- **Leases.** A fetched delivery is leased to that replica for `lease_ms` (default 30 s). The other replicas of the group don't see it until the lease ends. A crashed replica's deliveries therefore come back after the lease.
- **At least once.** A delivery can arrive twice: after a lease expiry, or after a crash between processing and `Ack`. Consumers keep their inbox and processed-events tables and ack only after durable processing, as they did with Kafka offsets.
- **Order per key.** Within a group, the deliveries of one `key` arrive in publish order. The next delivery of a key is held until the previous one is acked or dead-lettered. This is the guarantee Kafka's partitions gave.
- **Long poll.** Consumers set `wait_ms: 10000` for a 10-second long poll. Zero or omitted means an immediate poll. Cancelled calls stop before leasing another delivery.
- **New groups.** Groups in the table above exist from the broker's first start and receive every event since. A group first announced later through Service Discovery starts at the current end of the topic.

### Retries and the dead letter channel

`Nack(delivery_id, reason)` returns the delivery to the group after a backoff of 1, 2, 4 and 8 seconds. An expired lease counts as a failed attempt.

The message moves to `<topic>.dlq` with the group, the attempts and the last reason when either:

- the fifth attempt fails (`MAX_DELIVERY_ATTEMPTS`), or
- the consumer nacks with `permanent: true`: invalid JSON, a schema violation, or a type that doesn't match the topic.

A dead-lettered message is never delivered again on its own. Developers inspect it with `GET /v1/dlq/{topic}`.

Topics keep messages for `RETENTION_HOURS` (168, like Kafka). A message is removed only after its retention has passed **and** every group has acked or dead-lettered it. Dead letters are kept for the same retention.

### Topic interest in Service Discovery

A consuming service lists what it reads in its Service Discovery registration, `topics: [{topic, group}]` (see [service-discovery.md](service-discovery.md)). The broker reads registrations every 2 seconds:

- it accepts a `{topic, group}` only when the table above allows that group for that service;
- it rejects other pairs with a logged warning.

Producers never name consumers. A new consumer only has to register.

## Request routing

### Gateway to service: subscriber-based queues

Gateway names the destination: it sends each authorized client request to `https://message-broker:8443/queues/{service}/{path}`, with the same method, body and its `X-Gateway-Identity`.

- The broker keeps **one queue per destination service** (`QUEUE_CAPACITY`, 128). A full queue answers `503 TASK_LIMIT_REACHED`.
- The broker picks an instance, forwards the request with the Gateway's headers unchanged, and returns the answer with `X-Served-By: {service}/{instanceId}`.
- Only the certificate `gateway` may use `/queues`.

Gateway keeps authentication, header stripping, its own task limits and WebSocket negotiation. It no longer balances REST, keeps breakers for REST, or accepts service certificates.

### Service to service: gRPC

Services call `Router.Call(destination, method, path, headers, body)` instead of `https://gateway:8443/services/{destination}/{path}`. The request is the same REST request as before: the same paths, `Idempotency-Key`, body and error envelope. Allowed routes:

- an `internal` route whose OpenAPI `x-allowed-callers` lists the caller;
- a `public` route.

Any other route returns `403 SERVICE_FORBIDDEN`, and a service can never act as a player. The broker reads these rules from `contracts/broker-routes.json`, generated from `openapi.yaml` by `scripts/sync_gateway_routes.py`. The same command synchronizes the Gateway catalog and the broker's copy.

Every **service that serves internal routes** implements `Endpoint.Handle` on its own gRPC listener (`GRPC_PORT`, 50051):

- It accepts only the certificate `message-broker`.
- It runs the request through its existing HTTP handler (Fastify `inject()`, Go `http.Handler.ServeHTTP`) and returns status, headers and body unchanged.
- It registers the port as `grpcPort` in Service Discovery.

The broker uses gRPC for an instance with a `grpcPort`, and HTTPS on its registered `port` otherwise. That way owners can migrate one at a time; the log field `transport` shows which was used.

Header maps contain one value per name: repeated headers such as multiple `Set-Cookie` values are unsupported. Authentication uses JSON tokens rather than cookies. Both transports strip hop-by-hop headers.

`Router.Call` always answers with an `HttpResponse`: errors are HTTP statuses with the common `Error` body, so callers keep their current error handling. gRPC statuses are used only when no `HttpResponse` can be built: `UNAUTHENTICATED` (no client certificate) and `INVALID_ARGUMENT` (malformed envelope).

### Identity forwarded to the owner

Owners keep verifying an ES256 assertion in `X-Gateway-Identity`, with the claims from [gateway.md](gateway.md). Two changes:

- **Trusted peers.** The TLS peer may be `gateway` or `message-broker`.
- **Trusted issuers.** `iss: tamagotchi-gateway` is verified with `gateway.pem`, and `iss: tamagotchi-message-broker` with `message-broker.pem`.

| Path | Assertion the owner receives |
| --- | --- |
| Client → Gateway → broker queue → owner | Gateway's, forwarded unchanged (`kind` `player` or `public`) |
| Service → `Router.Call` → owner | The broker's: `kind: service`, `sub` = the caller's SAN, the same `method`, `uri`, `bodySha256`, 30-second expiry |

### Load balancing, circuit breaker and rerouting

- **Load-based choice.** The broker sends each request to the available instance with the lowest score. Score = the `load.percent` that Service Discovery last read for the instance plus 100 × in-flight requests / `MAX_CONCURRENT_TASKS` for the broker's own in-flight requests to it. Equal scores rotate round-robin.
- **Circuit breaker per instance.** It counts transport errors, timeouts and `5xx`; a `4xx` never counts. Three failures within `3.5 × TASK_TIMEOUT_SECONDS` (17.5 s) open the breaker:
  - it logs `circuit opened`;
  - the instance leaves the broker's pool;
  - the broker reports it with `POST /v1/services/{service}/instances/{instanceId}/eject`.

  After 5 seconds one trial request decides whether the breaker closes. Service Discovery's quarantine rules are unchanged.
- **Rerouting (high availability).** When an attempt fails, the same request goes to the next instance within the same task deadline, and the broker logs `request rerouted` with both instances. This happens:
  - always, when the failure came before any response byte: connect error or open breaker;
  - for a timeout or `5xx`, only when the request is a `GET`/`HEAD` or carries an `Idempotency-Key`. Every internal route requires one.

  When no instance is left, the answer is `503 DEPENDENCY_UNAVAILABLE`.

### Threads

The broker handles every request and RPC on **its own new thread**, named `req-<id>` in the logs. It has no shared worker pool. Concurrency is bounded by `MAX_CONCURRENT_TASKS` (64) and the queue capacities. Over the limit the answer is `503 TASK_LIMIT_REACHED` (`RESOURCE_EXHAUSTED` on gRPC), and over the deadline it is `504 TASK_TIMEOUT` (`DEADLINE_EXCEEDED`).

## Admin and observability routes

Routes are in [message-broker.openapi.yaml](message-broker.openapi.yaml). Admin routes reach the broker through Gateway as `admin` routes of the service `message-broker`, and the broker verifies Gateway's player assertion. An `admin` role or a player ID listed in `BROKER_ADMIN_USER_IDS` grants access; Compose uses the same configured demo admins as Package Registry.

| Route | Purpose |
| --- | --- |
| `GET /v1/topics` | Topics, producers, groups, stored and dead-lettered counts, pending deliveries per group |
| `GET /v1/topics/{topic}/messages` | Read stored messages from an offset (debugging and smoke tests) |
| `GET /v1/dlq` | Dead letter channels with counts |
| `GET /v1/dlq/{topic}` | Dead-lettered messages of one topic with group, attempts and reason |
| `GET /v1/queues` | Subscriber queues: depth, in-flight, and instances with score and breaker state |
| `GET /v1/logs` | Download the system's logs as NDJSON, filtered by service, instance, level, time and text |

`GET /status` follows the common `Status` schema and adds `queues`. `GET /metrics` serves Prometheus text:

- requests and latency per destination and instance;
- breaker states and reroutes;
- queue depth;
- published, delivered, acked and dead-lettered events per topic and group;
- pending deliveries per group.

**Log download budget.** `GET /v1/logs` returns at most 10,000 lines and 3 MiB within the task deadline, leaving Gateway response headroom. A partial page includes `X-Logs-Truncated: true` and, when at least one line fits, `X-Logs-Next-Since`. Reuse that timestamp as inclusive `since`; boundary lines can repeat, so callers deduplicate overlap. Very dense single-timestamp ranges may require narrower filters. No continuation header means no line fit; narrow the query.

**Logs.** Every container writes JSON logs to stdout as today. Grafana Alloy ships them to Loki, and `GET /v1/logs` reads Loki. Services need no logging change for this. Prometheus scrapes the broker, and Grafana shows both.

## Broker settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `PORT`, `INTERNAL_PORT`, `GRPC_PORT` | `8080`, `8443`, `50051` | Probe/metrics listener, mTLS HTTPS listener, mTLS gRPC listener |
| `SERVICE_TLS_CERT_FILE`, `SERVICE_TLS_KEY_FILE`, `SERVICE_TLS_CA_FILE` | required | Its certificate, key and the shared CA |
| `SERVICE_DISCOVERY_URL` | required | Pools, topic interest and ejections |
| `BROKER_DATA_DIR` | `/data` | SQLite store; a named volume in Compose |
| `TASK_TIMEOUT_SECONDS`, `MAX_CONCURRENT_TASKS` | `5`, `64` | Task limits; the breaker window is 3.5 × the timeout |
| `QUEUE_CAPACITY` | `128` | Waiting requests per destination queue |
| `MAX_DELIVERY_ATTEMPTS` | `5` | Attempts before dead-lettering |
| `DEFAULT_LEASE_SECONDS` | `30` | Lease when `Fetch` sets none |
| `RETENTION_HOURS` | `168` | Event and dead letter retention |
| `BROKER_ADMIN_USER_IDS` | empty | Comma-separated player UUIDs authorized to inspect admin routes |
| `LOKI_URL` | empty | Log source for `GET /v1/logs`; the route answers `503 DEPENDENCY_UNAVAILABLE` when empty |

## What each owner must change

1. **Events.** Replace the Kafka producer and consumer with the broker client: `Publish` from the outbox; `Fetch`, process, `Ack`; `Nack` with `permanent: true` for invalid events instead of publishing to `.dlq`. Same topics, envelopes and inbox. Set `BROKER_URL`; drop `KAFKA_*`.
2. **Registration.** Add `grpcPort` if the service serves internal routes, and `topics` if it consumes events.
3. **Serving.** Implement `Endpoint.Handle` on `GRPC_PORT` and accept the peer `message-broker` and the issuer `tamagotchi-message-broker`.
4. **Calling.** Send service-to-service requests with `Router.Call` instead of `https://gateway:8443/services/...`. Keep the 2-second timeout and the per-destination breaker.
5. **Releasing.** Publish validated merges to `main` as `4.MINOR.PATCH` and `latest`, then update the submodule pointer in the common Lab 4 integration PR.

Generate language-specific gRPC stubs from `contracts/proto/broker.proto` inside each owner repository; no private broker source access is needed. The broker repository's `MIGRATION.md` describes the steps. During migration, `BROKER_DESTINATIONS` explicitly lists broker-ready outgoing dependencies; unlisted dependencies retain the documented Gateway path. A broker failure never falls back to Gateway.
