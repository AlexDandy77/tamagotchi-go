# Gateway transport contract

Adding Gateway keeps service bodies, statuses, idempotency and data ownership unchanged. The Gateway handles REST authentication and routing; Kafka remains direct.

## Routes

Clients call `http://localhost:8080/services/{service}/{path}`. For example, `GET /services/user-management/v1/users/me` reaches `GET /v1/users/me`. Service names are the Compose names. Only methods and paths in [gateway-routes.json](gateway-routes.json) are allowed. Guild `/chat` is a direct WebSocket, never a REST proxy route.

Services call `https://gateway:8443/services/{destination}/{path}` with their own CA-signed certificate. The Gateway checks its DNS SAN against the route's original caller allowlist. A service certificate cannot impersonate a player. Internal routes reject external callers.

## Identity

The Gateway validates player `Authorization: Bearer <accessToken>` using User Management's RS256 keys, issuer/audience `tamagotchi-go`, expiration and player UUID. Public routes ignore and strip Authorization, even when the access token is expired or malformed. Login and refresh credentials are validated by User Management; a stale bearer header cannot prevent refresh. The Gateway removes Authorization, cookies, caller identity headers and hop-by-hop headers before forwarding. It never retries mutations or follows redirects.

Downstream requests use mTLS plus an ES256 JWT in `X-Gateway-Identity`, signed with the Gateway's EC certificate key. Each service mounts `gateway.pem` as its trusted public key and checks the peer certificate's DNS SAN is `gateway`.

| Claim | Required value |
| --- | --- |
| `iss`, `aud` | `tamagotchi-gateway`, destination service name |
| `kind`, `sub` | `player` and player UUID; `service` and original caller SAN; or `public` and empty subject |
| `iat`, `exp`, `jti` | Issued time, expiration within 30 seconds, unique request identifier |
| `method`, `uri` | HTTP method and exact downstream path/query, preserving encoding |
| `bodySha256` | Lowercase SHA-256 of the exact forwarded bytes |
| `roles` | Validated access-token roles; empty for service/public callers |

Services reject invalid signatures, audiences, expired assertions, changed requests and incoming Authorization headers. Player handlers retain ownership checks. Internal handlers use `sub` as the original caller and enforce their existing allowlists. Never trust an unsigned user-ID header.

With `GATEWAY_ONLY=true`, direct business requests are rejected. Health/readiness remain available internally. During migration, User Management explicitly sets `GATEWAY_ALLOW_DIRECT=true`: direct JWT requests, public JWKS and original internal mTLS caller allowlists remain available. Gateway requests still require verified mTLS and signed identities; forged assertions never fall back to legacy authentication. Remove this flag and port 8081 once all callers migrate. Battle remains strict. The Gateway reads User Management's JWKS directly over mTLS to bootstrap verification; this key-discovery call carries no player credentials. Migrate REST dependencies one destination at a time. Until an owner supports signed identities, keep its direct URL; add its Gateway upstream and change its callers/client URLs together after publishing. Replay protection for mutations remains the persistent service idempotency key, not the short-lived transport assertion.

## Deadlines and capacity

Every service and Gateway must default to `TASK_TIMEOUT_SECONDS=5` and `MAX_CONCURRENT_TASKS=64`, shared across their listeners. Full capacity returns `503 TASK_LIMIT_REACHED`; an expired task returns `504 TASK_TIMEOUT`, using the common error envelope. Health/readiness do not consume business slots. Gateway, User Management and Battle implement these limits; other owners must add them before migrating. Go cancels the request context and retains the slot until its handler exits. Retry mutations with the same idempotency key after a timeout; cancellation does not guarantee a transaction was never committed.

## Gateway errors

Gateway and destination errors share the common envelope. Use `error.code`, not only the HTTP status: Gateway's `SERVICE_FORBIDDEN` is not Guild's membership decision. Request bodies are limited to 64 KiB; destination responses to 4 MiB.

| Status | Gateway code | Meaning |
| --- | --- | --- |
| 401 | `INVALID_TOKEN`, `UNAUTHENTICATED` | Missing, invalid or expired player credentials |
| 401 | `SERVICE_CERT_REQUIRED` | Internal route needs a verified service certificate |
| 403 | `SERVICE_FORBIDDEN` | Caller not permitted; service cannot act as a player |
| 404 | `NOT_FOUND` | Destination/path/method is not exposed |
| 413 | `BODY_TOO_LARGE` | Request exceeds 64 KiB |
| 422 | `INVALID_ID` | Invalid negotiation guild UUID |
| 502 | `DEPENDENCY_RESPONSE` | Destination response exceeds 4 MiB |
| 503 | `DEPENDENCY_UNAVAILABLE` | Destination cannot be reached |
| 503 | `DESTINATION_NOT_CONFIGURED` | Owner's upstream has not been enabled |
| 503 | `IDENTITY_UNAVAILABLE` | Player verification keys unavailable |
| 503 | `TASK_LIMIT_REACHED` | All task slots are occupied |
| 504 | `TASK_TIMEOUT` | Task deadline exceeded |

## Guild WebSocket

Gateway-owned routes are described in [gateway.openapi.yaml](gateway.openapi.yaml).

`GET /v1/realtime/guilds/{guildId}/connection` requires a player token. The Gateway verifies membership through Guild's existing REST read and returns `{url, expiresAt, authentication: "ChatAuthenticate"}`. The URL is a direct Guild `ws://`/`wss://` URL without credentials. Send the existing `chat.authenticate` frame described in [realtime.schema.json](realtime.schema.json) as the first frame within five seconds. Guild validates that token and membership again. The Gateway holds no WebSocket connection.

## What other service owners must change

1. Accept the verified Gateway assertion on player/public/internal routes, preserving ownership and original caller permissions. Keep private keys and signing credentials private.
2. Send REST dependencies through the Gateway using the service certificate. Add mTLS to public APIs that previously only served HTTP, including Notification. Keep a safe key-discovery bootstrap for direct Guild socket authentication.
3. Add configurable deadlines and concurrency limits with the same errors; test failures and recovery.
4. Publish validated merges to `main` as immutable `2.MINOR.PATCH` images plus `latest`, for AMD64 and ARM64. Give each repository its own `DOCKERHUB_TOKEN` secret.
5. After publishing, add the Gateway upstream, switch the owner’s client/dependency URLs and update its merged submodule pointer in one integration PR. Shared image defaults use `latest`; version tags remain available. Remove public REST ports only once their callers migrate. Guild still needs its direct WebSocket port; reject direct REST business calls there.

Their current images do not implement this contract. Update the existing shared deployment as each owner publishes a compatible release; full-team integration remains pending.

Before Map migrates, User Management must serve the batch public-profile read `GET /internal/v1/users`; Map 2.0.4 and later read usernames only through it. Service certificates must not call player-only profile routes.
