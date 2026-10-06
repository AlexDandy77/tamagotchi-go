# Deployment architecture

## Running environment

One `compose.yaml` runs the existing project: Python Gateway, four Go services, four TypeScript services, PostgreSQL, MongoDB and Kafka. API processes run their own workers. No new database or worker container is needed for Gateway. The existing Compose project name is retained so upgrades use the same volumes.

```mermaid
flowchart LR
  P[Player / Postman] -->|REST :8080| GW[Python Gateway]
  GW -->|mTLS + signed identity| U[User Management / Go] & B[Battle / Go] & MR[Monster Raid / Go]
  B & MR -->|User Management REST| GW
  GW -.->|Pending migration| MP[Map / Go] & G[Guild / TypeScript] & R[Registry / TypeScript] & T[Tamagotchi / TypeScript] & N[Notification / TypeScript]
  MP & G & R & T -.->|REST through Gateway after migration| GW
  U -->|Direct Registry / starter recovery| R & T
  B -->|Direct rules / pet reservations| R & T
  MP -->|Direct profiles / relationships| U
  G -->|Direct relationships| U
  MR -->|Direct rules / pets / membership| R & T & G
  R -->|Raid dispatch| GW
  P -->|Direct REST during migration| MP & G & R & T & N
  P -->|Direct WebSocket :8087; negotiation pending| G
  P -->|Negotiated direct WebSocket :8084| MR
  U --> Users[(PostgreSQL users)]
  B --> Battles[(PostgreSQL battles)]
  MP --> Locations[(PostgreSQL locations)]
  MR --> Raids[(PostgreSQL raids)]
  G --> Guilds[(PostgreSQL guilds)]
  R --> Registry[(MongoDB registry)]
  T --> Pets[(PostgreSQL tamagotchi)]
  N --> Notifications[(PostgreSQL notification)]
  U & B & MP & MR & G & R & T -->|Events / outbox| K[Kafka KRaft]
  K -->|Enrollment| R & T
  K -->|Notifications| N
```

Solid edges show current traffic; dashed edges show pending Gateway transport. Each owner switches its client and dependency URLs only after publishing a compatible image. Tamagotchi's outbound rules/rewards remain mocked in its last verified image.


| Container | Responsibility | Storage |
| --- | --- | --- |
| Gateway | REST routing, JWT verification, signed identities, deadlines and WebSocket negotiation | None |
| User Management | Accounts, relationships, enrollments, wallets and rewards | PostgreSQL `users` |
| Battle | Challenges, turns, deadlines, outcomes and settlement | PostgreSQL `battles` |
| Map | Locations and nearby encounters | PostgreSQL `locations` |
| Monster Raid | Raids, participants, attacks and rewards | PostgreSQL `raids` |
| Guild | Membership, invitations, roles and chat | PostgreSQL `guilds` |
| Package Registry | Packages, configurations, rules, monsters and schedules | MongoDB `registry` |
| Tamagotchi | Pets, care, XP, reservations and ownership | PostgreSQL `tamagotchi` |
| Notification | Devices and notifications | PostgreSQL `notification` |
| PostgreSQL | Separate databases and credentials for each owner | `postgres-data` |
| MongoDB | Single-node replica set for Registry transactions | `mongo-data`, `mongo-config` |
| Kafka | Events with service SASL credentials and topic ACLs | `kafka-data` |

## Networking and authentication

Gateway uses Python 3.13/aiohttp; Go services use `net/http`, pgx and franz-go; TypeScript services use Fastify, Ajv and Kafka clients. Containers resolve each other by Compose service name.

Clients call `http://localhost:8080/services/{service}/{original-path}`. Gateway verifies RS256 access tokens, strips Authorization and spoofed headers, then forwards an ES256 request-bound identity over TLS 1.3 with client certificates. User Management, Battle and Monster Raid check this identity and retain their ownership/caller rules. Battle’s and Monster Raid’s User Management dependencies, Monster Raid’s token-key read included, point through Gateway. Monster Raid refuses direct REST (`GATEWAY_ONLY`), so Package Registry dispatches raids through Gateway. Registry, Tamagotchi and Guild destinations remain direct during migration. User Management explicitly enables temporary direct JWT/mTLS compatibility for the other callers; Gateway assertions are still verified and never fall back. Gateway's direct mTLS JWKS read bootstraps authentication. See [transport contract](contracts/gateway.md).

Every service is required to default to 5-second request deadlines and 64 active tasks. Capacity returns `503 TASK_LIMIT_REACHED`; timeout returns `504 TASK_TIMEOUT`. Gateway, User Management, Battle, Map and Monster Raid implement these limits; the other owners must add them. Health/readiness are exempt. Retry mutations with the same idempotency key because cancellation does not prove they never committed.

Gateway negotiates Guild chat through an authenticated membership read and returns Guild's direct WebSocket URL. Guild checks the first authentication frame and membership again. The membership REST route requires its owner's transport update before negotiation works with the current published image. Live raids work the same way: Gateway checks the player's raid read and returns Monster Raid's direct `/v1/raids/{raidId}/live` URL, which pushes the raid's HP, damage and result. Monster Raid checks the first authentication frame and raid access again.

Battle has no published host REST port. User Management temporarily exposes localhost:8081 with explicit migration compatibility, preserving existing JWKS, profile and internal caller access. Monster Raid's port 8084 stays only for its negotiated live socket and its probes, and the other five service ports remain direct. Remove each direct REST port and User Management’s compatibility flag only after its callers migrate. PostgreSQL, MongoDB, Kafka and internal mTLS ports stay private. Kafka and database traffic do not pass through Gateway.

Setup creates development certificates in ignored `.secrets/tls`. Each container receives its own private key and CA; the two Go services also receive Gateway's public certificate. Adding a certificate or replacing an expired bundle regenerates the bundle, preserves a backup and requires restarting every service.

Compose suits this single-PC lab. A four-PC or cloud cluster requires separate networking, storage and availability decisions. Single-node databases/Kafka and plaintext local HTTP are development choices, not a highly available deployment.

## Ownership and durability

Only the owning service writes its database. Provisioning creates missing databases/roles without deleting data; migrations belong to each service. Seeds populate empty databases and preserve existing records. Named volumes survive container recreation.

Business changes and outbox events commit together. Kafka outages retain pending rows; publication retries with stable event IDs. Consumers deduplicate repeats. Battle persists deadlines and reservation/settlement progress, and retries remote work outside transactions using stable activity IDs.

## Integration checks

Validated with fresh isolated data and existing teammate images: Gateway login/refresh, JWT rejection, spoofed-header removal, protected internal routes, Battle → Gateway → User Management mTLS, challenge replay/cancellation, registration through live Registry, legacy JWKS/profile reads, Map relationships and Kafka events, Guild membership, and Registry permissions/projection. The new Gateway/User Management fixes used local test images; the presentation data was untouched. Earlier fixture checks completed combat and real wallet settlement with explicit Registry/Tamagotchi fixtures.

Use `python3 scripts/smoke_gateway.py` for Gateway, User Management and Battle, and `python3 scripts/smoke_monster_raid.py` for Monster Raid behind Gateway. Use `scripts/smoke_live.py` for team workflows after their owners migrate.

Remaining work in the services that have not migrated:

- Guild, Package Registry, Tamagotchi and Notification: adopt signed Gateway identities, route REST dependencies through Gateway and add request limits. Notification needs mTLS for its REST listener.
- Guild must accept the identity on the membership read used for direct socket negotiation.
- Add merge-triggered image publishing and publish versioned plus latest tags. Until then, their Postman/smoke URLs and dependency calls stay direct, and their Gateway upstreams stay disabled. Map’s batch profile read `GET /internal/v1/users` needs its User Management handler before Map migrates.

Existing Tamagotchi/Notification 0.4.1 business gaps also remain: mock authentication/dependencies, hardcoded care/starter rules, missing real wallet credits and Firebase delivery. One starter per owner/package cannot supply Battle's two-pet loadout. Do not hide these gaps with fake production pets or direct database writes.

Seeded accounts are Alice and Bob at `alice@demo.invalid`/`bob@demo.invalid`, using local `SEED_PASSWORD`; fresh accounts start at zero. Guild seeds Night Owls. Configure `REGISTRY_ADMIN_USER_IDS` locally for Registry admin tasks. Existing balances and records are preserved.

## Extending the deployment

1. Add the owning database/role/password entry to `deployment/databases.json` or MongoDB provisioning. Add secret placeholders and generation keys to `.env.example`/`scripts/lab.py`.
2. Add the public image, private database credentials and healthcheck to the existing `compose.yaml`. Add its certificate to setup and mount only the required keys.
3. Follow [Gateway transport contract](contracts/gateway.md), update the route catalog from OpenAPI, and configure Kafka topics/ACLs for its events.
4. Run `setup`, `provision`, migrations and seeds on the existing volumes. Publish merged service commits with versioned and latest tags, then update Gateway routing and submodule pointers through a common PR.

## Future full-system architecture

The following agreed design is the team's target architecture. The running deployment is described above; remaining integrations are listed under Integration checks.


### System overview

![Shared backend architecture](images/architecture.png)

Client apps reach every service through a single **API Gateway**, and each service owns its database and credentials: MongoDB for Package Registry, PostgreSQL for the other seven services. Services never share a database. Neither of these is drawn as a separate box per service in the diagram above but both apply to all eight backend services. The one exception on the gateway side is Guild's chat: client apps hold a direct WebSocket connection to Guild Service for real-time messages, shown as the green line bypassing the gateway. Firebase Cloud Messaging is also reached directly by client apps for push delivery, independent of the gateway.

Black arrows show logical HTTP dependencies between services; Gateway carries their REST traffic. Orange arrows are events flowing through Kafka.

### Service dependencies

The diagram shows logical owner-to-owner dependencies. Their REST calls pass through Gateway as shown above. Gateway verifies player JWTs using User Management's public keys; services verify the forwarded identity and enforce permissions. Authentication discovery is omitted from the diagram for readability.

- **Monster Raid → Package Registry** — raid configuration and lifecycle, plus reward rule lookups.
- **Tamagotchi → Package Registry** — starter pet definitions, care and growth rules.
- **Battle → Package Registry** — package combat rules, plus reward rule lookups.
- **Battle → Tamagotchi** — pet properties, XP and capture at battle settlement.
- **Monster Raid → Tamagotchi** — primary pet properties and XP for participating members.
- **Tamagotchi → User Management** — enrollment checks and local reward settlement.
- **User Management → Tamagotchi** — starter-pet provisioning recovery, a fallback if the enrollment event was missed.
- **Monster Raid → User Management** — currency reward settlement.
- **Battle → User Management** — currency and boost settlement.
- **Map → User Management** — friends/enemies lookups, used to decide what an encounter should trigger, and usernames for map entries.
- **Guild → User Management** — identity and relationship lookups for membership and invite eligibility.
- **Monster Raid → Guild** — membership and permission checks before a member can join a raid.

### Event flow

The event flow is represented with orange arrows above. Anything that doesn't have to happen before a response is sent travels as an event through Kafka instead of a direct call.

User Management publishes `user.package-registered.v1` on enrollment; Package Registry and Tamagotchi consume it independently: Registry updates its enrollment projection, while Tamagotchi provisions the starter pet. Six services — User Management, Map, Battle, Tamagotchi, Guild and Monster Raid — publish their own domain events (friend requests, encounters, battle results, pet use/capture, guild invites, raid results) onto one Kafka topic per event type, consumed by Notification; Notification turns every one of those into a push message and hands it to Firebase Cloud Messaging, which delivers it straight to the client app.

### Example: finishing a battle

The player's request is answered as soon as the result is stored; settlement with the data owners and the notification happen afterwards.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client app
    participant B as Battle
    participant UM as User Management
    participant T as Tamagotchi
    participant K as Kafka
    participant N as Notification
    C->>B: POST /v1/battles/:id/actions (JWT, Idempotency-Key)
    B->>B: validate turn, apply damage, detect winner, persist result
    B-->>C: 200 Battle (status: settling)
    B->>UM: PUT /internal/v1/battle-settlements/:id
    UM-->>B: WalletResult (stake moved, boost consumed)
    B->>T: PUT /internal/v1/pet-battle-settlements/:id
    T-->>B: PetResult (XP applied, loser's primary transferred)
    B->>B: mark finished and write battle.finished.v1 to the outbox
    B->>K: publish battle.finished.v1 (key: battleId)
    K->>N: consume (group: notification)
    N-->>C: push to both players via Firebase
```

### Decisions to confirm

The brief leaves some boundaries open. These are our choices; they can change after discussion with the professor.

| Question | Our choice |
| --- | --- |
| The brief tells Guild to use "Registry" for identity and relationships, but User Management owns those. | Guild asks User Management. |
| Both User Management and Package Registry record user-package enrollments. | User Management is authoritative; Registry keeps a read-only projection fed by events. |
| Who creates battle challenges and matches? | Battle owns challenges, acceptance and match creation. |
| Local currency rules differ per package. | User Management stores balances per user and package; Registry stores the rules. |
| Losing a battle transfers the loser's primary pet. | Tamagotchi transfers the existing record; the winner keeps its primary and gains a secondary; the loser must pick a new primary. |
| Proximity threshold is written as "6(?)" meters. | 6 meters, locations fresh for 120 seconds. |

## Services

Each service is the only writer of its data. Other services ask the owner through its API; a reference to a user, pet, package or guild points at the existing record and never creates a copy.

### 1. User Management

- **Owns:** accounts, credentials and roles; friend requests, friendships and enemy marks; which packages a user is enrolled in; global and local currency balances; battle boosts and their holds.
- **Does:** issues JWTs, answers "who is this user", "are they friends", "can they afford this stake"; applies currency results reported by Battle and Monster Raid.
- **Not here:** pets (Tamagotchi), guild membership (Guild), package definitions (Package Registry).

### 2. Battle

- **Owns:** challenges, accepted matches, chosen pets and boosts, frozen combat inputs, turn and HP state, the outcome and settlement progress.
- **Does:** computes damage from levels, type advantage, boosts and package care bonuses; decides winner rewards and the primary/secondary XP split; asks User Management and Tamagotchi to apply them.
- **Not here:** currency balances or persistent pet records (never edited directly); cooperative fights (Monster Raid).

### 3. Tamagotchi

- **Owns:** every pet: identity, origin package, owner, combat type, level, XP, sprites and package-specific care statistics; primary and secondary assignments; pet reservations for battles and raids.
- **Does:** creates the starter pet on enrollment, validates care actions against the package's pinned rules, applies XP and ownership transfers after battles and raids.
- **Not here:** a battle's temporary HP (Battle); the definition of a statistic (Package Registry).

### 4. Notification

- **Owns:** push device registrations and notification records.
- **Does:** consumes events (friend request, encounter, battle request or result, pet used or captured, guild invitation, raid start or result), decides who receives what, and delivers it through Firebase.
- **Not here:** guild chat (Guild); any game decision.

### 5. Map

- **Owns:** each user's latest location and timestamp; encounter state.
- **Does:** ignores stale updates, lets players read or clear their own location, keeps friends and enemies visible, detects strangers within 6 meters and publishes an encounter event that may lead to a friend request or a battle.
- **Not here:** challenges (Battle) and alerts (Notification).

### 6. Monster Raid

- **Owns:** active raid instances: monster HP, participants, damage per participant, timer and status.
- **Does:** starts raids from Registry schedules, checks guild eligibility and reserves primary pets, processes clicker-style attacks with a one-second cooldown, and requests rewards from User Management and Tamagotchi on victory.
- **Not here:** monster and schedule definitions (Package Registry); membership (Guild).

### 7. Guild

- **Owns:** guilds, invitations, members and roles (leader, officer, member); chat messages with a per-guild sequence.
- **Does:** enforces membership and permission rules, runs the guild chat over WebSockets, and tells Monster Raid who is eligible.
- **Not here:** friendships and enemies (User Management); the raid itself (Monster Raid).

### 8. Package Registry

- **Owns:** packages and their moderators; immutable package configurations (starter pets, statistics, care actions, combat bonus thresholds); global combat rules; monster definitions; raid schedules.
- **Does:** lets admins register packages and configure monsters and raids, lets moderators publish package rules, keeps a projection of enrollments, and tells Monster Raid when a raid starts or is cancelled.
- **Not here:** a pet's current statistic values (Tamagotchi); live raid state (Monster Raid).

## Future technologies and communication patterns

The team works in **Go and TypeScript**. Each person implements both of their services in one language.

| Service | Owner | Language and framework | Storage | Communication |
| --- | --- | --- | --- | --- |
| User Management | Alexei | Go, `net/http` | PostgreSQL `users` | HTTP/JSON; publishes enrollment and friend events |
| Battle | Alexei | Go, `net/http` | PostgreSQL `battles` | HTTP/JSON with client polling of battle state; publishes battle events |
| Tamagotchi | Artur | TypeScript, Fastify | PostgreSQL `pets` (JSONB for package statistics) | HTTP/JSON; consumes enrollment events; publishes pet events |
| Notification | Artur | TypeScript, Fastify | PostgreSQL `notifications` | HTTP/JSON for devices; consumes Kafka events; Firebase push |
| Map | Alexandru | Go, `net/http` | PostgreSQL `locations` | HTTP/JSON; publishes encounter events |
| Monster Raid | Alexandru | Go, `net/http` | PostgreSQL `raids` | HTTP/JSON and a live raid WebSocket negotiated by Gateway; publishes raid events |
| Guild | Nicolae | TypeScript, Fastify | PostgreSQL `guilds` | HTTP/JSON; WebSocket chat; publishes invitation events |
| Package Registry | Nicolae | TypeScript, Fastify | MongoDB `registry` (versioned configuration documents) | HTTP/JSON; consumes enrollment events |

**Why two languages, and these two.** Go's standard library gives small, fast binaries with built-in concurrency, which fits the request-heavy, timer-driven services (settlement, combat, location updates, raid attacks). TypeScript with Fastify gives schema-validated routes, JSON-native handling of package-specific statistics and easy WebSocket support, which fits pets, notifications, chat and configuration. One language per person avoids context switching. The cost is keeping validation and serialization equivalent in both stacks; the language-neutral contract below is the shared reference.

**PostgreSQL for seven services.** Balances, ownership, holds and combat state need local transactions; PostgreSQL gives them, and JSONB stores each package's differently named statistics without a shared schema. Separate databases make ownership explicit at the cost of cross-service consistency work, handled with the outbox and settlement flows below. For the lab, one PostgreSQL server can host the seven relational databases with separate credentials.

**MongoDB for Package Registry.** Store each immutable configuration version as a document containing starter pets, statistics and care actions. Keep packages, enrollments, monsters and schedules in separate collections, with unique indexes for identity and version pairs. Publish configurations and advance their current-version pointer atomically; persist enrollment projections and processed-event inbox entries in one transaction before committing Kafka offsets. Run MongoDB as a replica set to support these transactions. See the [Registry storage design](services/package-registry/README.md#storage-design).

**HTTP/JSON for requests.** Synchronous calls handle decisions the caller must know immediately, such as reserving pets or checking eligibility. JSON is inspectable from both languages and from any client app. Calls time out after two seconds and unfinished work stays visible for retry. Clients poll battle and raid resources; guild chat uses WebSockets because it needs continuous delivery, and therefore reconnect and history replay.

**Kafka for events.** Events are an append-only log: a consumer that was down (Notification, Registry) catches up from its last offset, and a new projection can replay history. Partitioning by `aggregateId` keeps events for one battle, raid or request in order. The cost is a heavier broker to run and no per-message routing; the lab uses a single-node Kafka in Docker and one topic per event type.

**Firebase Cloud Messaging** is the push provider required by the brief. A push carries only `eventId`, `type` and `targetId`; the client fetches the authoritative state after opening it. Firebase needs a project, a service-account key kept out of Git, and a client that produces device tokens.
