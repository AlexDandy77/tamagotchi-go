# Gateway transport contract

Adding Gateway keeps service bodies, statuses, idempotency and data ownership unchanged. The Gateway handles REST authentication and routing; Kafka remains direct.

## Routes

Clients call `http://localhost:8080/services/{service}/{path}`. For example, `GET /services/user-management/v1/users/me` reaches `GET /v1/users/me`. Service names are the Compose names. Only methods and paths in [gateway-routes.json](gateway-routes.json) are allowed. Guild `/chat` is a direct WebSocket, never a REST proxy route.

Services call `https://gateway:8443/services/{destination}/{path}` with their own CA-signed certificate. The Gateway checks its DNS SAN against the route's original caller allowlist. A service certificate cannot impersonate a player. Internal routes reject external callers.

## Identity

The Gateway validates player `Authorization: Bearer <accessToken>` using User Management's RS256 keys, issuer/audience `tamagotchi-go`, expiration and player UUID. Public routes need no token; a supplied token must still be valid. The Gateway removes Authorization, cookies, caller identity headers and hop-by-hop headers before forwarding. It never retries mutations or follows redirects.

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

With `GATEWAY_ONLY=true`, User Management and Battle reject direct business requests. Health/readiness remain available internally. The Gateway reads User Management's JWKS directly over mTLS to bootstrap verification; this key-discovery call carries no player credentials. Business REST calls go through the Gateway. Replay protection for mutations remains the persistent service idempotency key, not the short-lived transport assertion.

## Deadlines and capacity

All three updated processes default to `TASK_TIMEOUT_SECONDS=5` and `MAX_CONCURRENT_TASKS=64`, shared across their listeners. Full capacity returns `503 TASK_LIMIT_REACHED`; an expired task returns `504 TASK_TIMEOUT`, using the common error envelope. Health/readiness do not consume business slots. Go cancels the request context and retains the slot until its handler exits. Retry mutations with the same idempotency key after a timeout; cancellation does not guarantee a transaction was never committed.

## Guild WebSocket

Gateway-owned routes are described in [gateway.openapi.yaml](gateway.openapi.yaml).

`GET /v1/realtime/guilds/{guildId}/connection` requires a player token. The Gateway verifies membership through Guild's existing REST read and returns `{url, expiresAt, authentication: "ChatAuthenticate"}`. The URL is a direct Guild `ws://`/`wss://` URL without credentials. Send the existing `chat.authenticate` frame described in [realtime.schema.json](realtime.schema.json) as the first frame within five seconds. Guild validates that token and membership again. The Gateway holds no WebSocket connection.

## What other service owners must change

1. Accept the verified Gateway assertion on player/public/internal routes, preserving ownership and original caller permissions. Keep private keys and signing credentials private.
2. Send REST dependencies through the Gateway using the service certificate. Add mTLS to public APIs that previously only served HTTP, including Notification. Keep a safe key-discovery bootstrap for direct Guild socket authentication.
3. Add configurable deadlines and concurrency limits with the same errors; test failures and recovery.
4. Publish validated merges to `main` as immutable `2.MINOR.PATCH` images plus `latest`, for AMD64 and ARM64. Give each repository its own `DOCKERHUB_TOKEN` secret.
5. After publishing, update the shared image pins and remove public REST ports. Guild still needs its direct WebSocket port; reject direct REST business calls there.

Their current images do not implement this contract. Update the existing shared deployment as each owner publishes a compatible release; full-team integration remains pending.
