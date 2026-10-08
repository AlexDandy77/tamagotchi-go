# Direct socket tickets

Gateway ticket issuance is enabled with `SOCKET_TICKETS_ENABLED=true`. Guild and Monster Raid still need to publish matching validators; their current player-JWT validators reject Gateway-signed tickets. REST remains independent of this socket rollout.

## Gateway response

After verifying the player's access token and resource permission, the existing connection endpoint returns `url`, `authentication`, `expiresAt` and, when enabled, `ticket`. The ticket never appears in the URL. It is an ES256 JWT signed by the same Gateway key used for REST identity assertions.

| Field | Required value |
| --- | --- |
| JWT header | `alg=ES256`, `typ=ws-ticket+jwt` |
| `iss` | `tamagotchi-gateway` |
| `aud` | `guild` or `monster-raid`, matching the destination |
| `sub` | Verified player UUID |
| `kind` | `socket` |
| `resourceType` | `guild` or `raid` |
| `resourceId` | UUID from the negotiated socket path |
| `iat`, `exp` | Unix seconds; lifetime at most 30 seconds and never beyond the access token's expiry |
| `jti` | Unique UUID, consumed once by the destination |

The client opens the returned direct socket and sends the ticket in the existing first frame's `accessToken` field: `{"type":"authenticate","accessToken":"<ticket>"}` for Guild, or `{"type":"raid.authenticate","accessToken":"<ticket>"}` for Monster Raid. This field carries a socket credential, not the player's original bearer token, in ticket mode. Never log, echo or persist the credential itself.

## Work for the service owners

Guild and Monster Raid must each:

1. Verify the signature using the mounted `GATEWAY_CERT_FILE`, allow ES256 only, and check every header/claim above. Reject expired tickets without the access token's 30-second leeway; allow no lifetime longer than 30 seconds.
2. Match the ticket's destination, resource type and resource ID to the socket being opened. Recheck current guild membership or raid access before accepting.
3. Atomically consume `jti` in the service's own database, with a unique constraint. Concurrent connections, multiple replicas and restarts must not allow reuse. Retain replay records until expiry; do not put these records in Gateway or share databases.
4. In ticket mode, reject ordinary player JWTs and REST identity assertions. Close sockets that do not authenticate within five seconds. Keep existing frame validation and error/close conventions.
5. Test valid authentication, changed signatures, wrong algorithm/header/issuer/audience/kind/resource/user, expiry, membership removal after negotiation, duplicate/concurrent use, restart replay and missing first frames. A failed authorization must never produce an authenticated socket.

Publish validated service releases, configure both destinations to require tickets, and run real negotiation/authentication/replay tests against both services. Gateway issuance is already enabled. REST/Postman checks can run independently, but direct ticket authentication remains incomplete until both destinations support it.
