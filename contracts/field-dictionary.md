# Field dictionary

Request, response, event and WebSocket message types referenced by the [endpoint catalog](../README.md#endpoint-catalog), the [event contract](../README.md#kafka-event-contract) and the [guild chat contract](../README.md#guild-chat-websocket-contract) in the README.

Notation: `T[]` is an array; `map<T>` is a JSON object whose values are `T`; `?` marks an optional key; keys without `?` are required; `T or null` requires the key but permits `null`. Closed objects reject additional fields. [openapi.yaml](openapi.yaml) additionally records numeric ranges, string limits, enums and path/query/header requirements, and is authoritative if a future edit creates a discrepancy.

#### ID

UUID string identifying an existing resource.

```text
string(uuid)
```

#### Time

RFC 3339 UTC timestamp, e.g. 2026-09-09T10:00:00Z.

```text
string(date-time)
```

#### URI

HTTPS asset reference; binary assets are not embedded in JSON.

```text
string(uri)
```

#### Name

Display name shown to players; length limits are in OpenAPI.

```text
string
```

#### Version

Positive integer revision of a resource or configuration; it increases by one per change.

```text
integer
```

#### Coins

Whole currency units, within JavaScript safe-integer range.

```text
integer
```

#### Type

One of the six combat types; the advantage cycle is in [game-rules.md](game-rules.md).

```text
"flame" / "nature" / "earth" / "electric" / "water" / "shadow"
```

#### Stats

Package-specific statistics keyed by the package's own stat names, in the package's own units.

```text
map<number>
```

#### SettlementState

Progress of a cross-service settlement: `pending` (in progress), `complete`, or `blocked` (needs repair).

```text
"pending" / "complete" / "blocked"
```

#### Error

Standard non-success JSON response. Never contains credentials.

```text
error: {code: string, message: string, requestId: ID, details: ({field: string, reason: string})[]}
```

#### User

Private profile; returned only to its owner.

```text
id: ID
username: Name
email: string(email)
roles: ("player" / "admin")[]
packageIds: (ID)[]
createdAt: Time
```

#### PublicUser

Profile visible to other users; excludes email and credentials.

```text
id: ID
username: Name
```

#### Register

Creates an account and starts package enrollment.

```text
username: string
email: string(email)
password: string
packageId: ID
```

#### Login

Password is input only.

```text
email: string(email)
password: string
```

#### Refresh

Opaque refresh token; rotated on refresh, revoked on logout.

```text
refreshToken: string
```

#### Session

Access token lifetime 900 seconds. Store refresh tokens securely; never publish in events.

```text
accessToken: string
refreshToken: string
tokenType: "Bearer"
expiresIn: integer
user: User
```

#### ProfileUpdate

Only editable public username.

```text
username: Name
```

#### JWK

RSA public verification key; no private key fields.

```text
kty: "RSA"
use: "sig"
alg: "RS256"
kid: string
n: string
e: string
```

#### JWKS

Public signing keys for offline access-token validation.

```text
keys: (JWK)[]
```

#### Enrollment

One record per user and package; pet provisioning is eventually consistent.

```text
userId: ID
packageId: ID
version: Version
createdAt: Time
```

#### FriendRequestInput

The sender is the authenticated user.

```text
recipientId: ID
```

#### FriendRequest

Only sender and recipient can view the request.

```text
id: ID
senderId: ID
recipientId: ID
status: "pending" / "accepted" / "declined" / "cancelled"
createdAt: Time
```

#### RequestDecision

Recipient decides a pending invitation/request.

```text
decision: "accept" / "decline"
```

#### Relationship

Friends are mutual; enemies are a directed relation from userId to otherUserId.

```text
userId: ID
otherUserId: ID
kind: "friend" / "enemy" / "unknown"
```

#### Wallet

available excludes reservations; local balances are keyed by enrolled package UUID.

```text
globalBalance: Coins
globalAvailable: Coins
localBalances: map<Coins>
boosts: ({boostId: "power", quantity: integer, available: integer})[]
```

#### ParticipantLoadout

Primary and secondary must be distinct, owned by this user, and available.

```text
userId: ID
primaryPetId: ID
secondaryPetId: ID
boostIds: ("power")[]
```

#### BattleHoldInput

Battle-only command. Reserve fixed stake and selected boosts for both users atomically.

```text
participants: (ParticipantLoadout)[]
rulesVersion: Version
```

#### Hold

A durable reservation retained until explicit release or successful settlement.

```text
activityId: ID
status: "reserved" / "released" / "settled"
```

#### BattleMoneyResult

User Management checks winner/loser against the reservation and computes stake transfer.

```text
winnerId: ID
loserId: ID
```

#### WalletResult

Exactly one settlement per activity; reward amounts are server-validated.

```text
activityId: ID
status: "complete"
balances: ({userId: ID, balance: Coins})[]
```

#### RaidMoneyInput

Raid-only command. Uses the pinned admin-created schedule; no client-chosen amount.

```text
scheduleId: ID
scheduleVersion: Version
recipientIds: (ID)[]
```

#### LocalRewardInput

Tamagotchi-only command, one grant per completed care action.

```text
userId: ID
packageId: ID
configVersion: Version
actionId: Name
```

#### LocalRewardResult

Result of one package-local currency grant.

```text
operationId: ID
userId: ID
packageId: ID
balance: Coins
```

#### Pet

Persistent pet; primary assignment is separate. packageStats preserve package-specific keys and units.

```text
id: ID
packageId: ID
configVersion: Version
ownerId: ID
name: Name
combatType: Type
level: integer
xp: integer
spriteUrls: (URI)[]
packageStats: Stats
version: Version
```

#### PetRoster

Owned pets and selected primary. All secondary entries are existing pet IDs.

```text
pets: (Pet)[]
primaryPetId: ID or null
secondaryPetIds: (ID)[]
```

#### PrimaryInput

Select another owned, unreserved pet.

```text
petId: ID
```

#### CareInput

Server applies configured effects, XP/currency caps and cooldown; client supplies no stat deltas.

```text
actionId: Name
expectedVersion: Version
```

#### CareResult

Pet update is durable; local currency settlement may still be pending.

```text
actionInstanceId: ID
pet: Pet
localRewardStatus: SettlementState
```

#### StarterInput

User Management only; requires a canonical enrollment. Unique by user and package.

```text
userId: ID
packageId: ID
```

#### TypeCatalog

Directed cycle means each type has advantage over the next one.

```text
types: (Type)[]
advantages: ({attacker: Type, defender: Type, multiplier: number})[]
```

#### PetReserveInput

Coordinator reserves all pets atomically. Battle uses two loadouts; raid uses one primary per reservation.

```text
kind: "battle" / "raid"
users: ({userId: ID, primaryPetId: ID, secondaryPetId: ID or null})[]
```

#### PetReservation

Frozen combat inputs. Each returned pet includes its package configuration version.

```text
activityId: ID
status: "reserved" / "released" / "settled"
pets: (Pet)[]
```

#### PetBattleResult

Battle-only: split XP and transfer the existing loser primary atomically, then release holds.

```text
winnerId: ID
loserId: ID
rulesVersion: Version
```

#### PetRaidResult

Raid-only: award the configured XP to one reserved primary, then release its hold.

```text
raidId: ID
scheduleId: ID
scheduleVersion: Version
rewarded: boolean
```

#### PetResult

Pet changes applied once per reservation.

```text
activityId: ID
status: "complete"
pets: (Pet)[]
```

#### BattleCreate

Challenge a different user using an owned primary, an owned secondary and optional boost.

```text
opponentId: ID
primaryPetId: ID
secondaryPetId: ID
boostIds: ("power")[]
```

#### BattleAccept

Recipient selects their loadout. Validation and all reservations precede active combat.

```text
primaryPetId: ID
secondaryPetId: ID
boostIds: ("power")[]
```

#### BattleAction

Only the current player can attack. Forfeit is allowed for either participant while active.

```text
expectedTurn: integer
action: "attack" / "forfeit"
```

#### Battle

Only participants may read. Currency/pet effects become final when settlement is complete.

```text
id: ID
challengerId: ID
opponentId: ID
status: "pending" / "preparing" / "active" / "settling" / "finished" / "declined" / "cancelled" / "expired"
loadouts: (ParticipantLoadout)[]
rulesVersion: Version
turn: integer
currentPlayerId: ID or null
turnDeadline: Time or null
hp: ({userId: ID, current: integer, maximum: integer})[]
winnerId: ID or null
loserId: ID or null
settlement: SettlementState or null
createdAt: Time
expiresAt: Time
```

#### DeviceInput

Token is delivered only to Firebase; unique per owner/device. Never echoed.

```text
token: string
platform: "web" / "android" / "ios"
packageId: ID
```

#### Device

Safe push-registration response.

```text
id: ID
packageId: ID
platform: "web" / "android" / "ios"
updatedAt: Time
```

#### Notification

Durable notification generated from a domain event.

```text
id: ID
eventId: ID
type: string
title: string
body: string
targetId: ID
createdAt: Time
readAt: Time or null
```

#### LocationInput

Measured coordinates from the current client; older timestamps are ignored.

```text
latitude: number
longitude: number
accuracyMeters: number
recordedAt: Time
```

#### LocationResult

An ignored stale update does not replace the stored location.

```text
accepted: boolean
latest: LocationInput
```

#### MapEntry

Friends/enemies stay listed even with missing or stale location. Only fresh nearby strangers appear.

```text
user: PublicUser
relationship: "friend" / "enemy" / "unknown"
location: LocationInput or null
distanceMeters: number or null
stale: boolean
```

#### GuildInput

Guild name and description.

```text
name: Name
description: string
```

#### Guild

Guild leader is also a member; exactly one leader per guild.

```text
id: ID
name: Name
description: string
leaderId: ID
memberCount: integer
createdAt: Time
```

#### Member

Guild-local role, independent of global privileges.

```text
userId: ID
role: "leader" / "officer" / "member"
joinedAt: Time
```

#### RoleInput

Leader-only; use the leadership endpoint to change leader.

```text
role: "officer" / "member"
```

#### LeaderInput

Transfer leadership to an existing member.

```text
userId: ID
```

#### GuildInvite

Only guild officers/leader and the invited user may read.

```text
id: ID
guildId: ID
inviterId: ID
recipientId: ID
status: "pending" / "accepted" / "declined" / "revoked"
createdAt: Time
```

#### Eligibility

Guild authority response. eligible is false for a non-member.

```text
guildId: ID
userId: ID
eligible: boolean
role: "leader" / "officer" / "member" or null
```

#### ChatMessage

Sequence is monotonically increasing within a guild.

```text
id: ID
guildId: ID
authorId: ID
sequence: integer
content: string
createdAt: Time
```

#### ChatHistory

Replay messages after a known sequence; hasMore controls continuation.

```text
items: (ChatMessage)[]
hasMore: boolean
```

#### PackageInput

Admin registers a participating app. Moderator assignments are separate.

```text
name: Name
appVersion: string
description: string
```

#### Package

Package metadata; configVersion identifies immutable game rules, independently of appVersion.

```text
id: ID
name: Name
appVersion: string
description: string
status: "draft" / "active" / "suspended"
configVersion: Version or null
moderatorIds: (ID)[]
version: Version
```

#### PackageUpdate

Admin controls status; moderators may edit name, description and appVersion for their package.

```text
expectedVersion: Version
name?: Name
appVersion?: string
description?: string
status?: "draft" / "active" / "suspended"
```

#### BonusRule

Package-defined interpretation; aggregate care bonus is capped by global combat rules.

```text
threshold: number
direction: "gte" / "lte"
percent: number
```

#### StatDefinition

Values are package-local; min <= initial <= max. changePerMinute is applied using server elapsed time.

```text
minimum: number
maximum: number
initial: number
changePerMinute: number
bonus: BonusRule or null
```

#### CareDefinition

Server-selected effects for an action. Currency grants and XP are subject to global caps.

```text
statChanges: Stats
cooldownSeconds: integer
xp: integer
localCurrency: Coins
```

#### PackageConfigInput

Publish a new immutable revision. First publication expects version 0; later publications expect the current revision.

```text
expectedVersion: integer
starter: {name: Name, combatType: Type, spriteUrls: (URI)[]}
statistics: map<StatDefinition>
careActions: map<CareDefinition>
localCurrencyName: Name
```

#### PackageConfig

Immutable snapshot. Existing pets keep their creation version; migrations require a future explicit contract.

```text
packageId: ID
version: Version
starter: {name: Name, combatType: Type, spriteUrls: (URI)[]}
statistics: map<StatDefinition>
careActions: map<CareDefinition>
localCurrencyName: Name
```

#### CombatRules

Global, admin-controlled server rules; initial version 1 is fixed by this contract.

```text
version: Version
stake: Coins
winnerXp: integer
loserXp: integer
primaryXpPercent: integer
careBonusCapPercent: integer
powerBoostPercent: integer
turnTimeoutSeconds: integer
maxLevel: integer
dailyCareXpCap: integer
dailyLocalCurrencyCap: Coins
```

#### MonsterInput

Admin-created monster definition. Resistances/weaknesses use the six global types.

```text
name: Name
description: string
spriteUrls: (URI)[]
maxHp: integer
baseAttack: integer
weaknesses: (Type)[]
resistances: (Type)[]
specialProperties: ("none" / "armored")[]
```

#### Monster

Immutable versioned monster configuration.

```text
id: ID
version: Version
name: Name
description: string
spriteUrls: (URI)[]
maxHp: integer
baseAttack: integer
weaknesses: (Type)[]
resistances: (Type)[]
specialProperties: ("none" / "armored")[]
```

#### RaidRewards

Each participant who dealt damage receives this reward on victory; no reward on failure/cancellation.

```text
globalCurrencyPerParticipant: Coins
xpPerParticipant: integer
```

#### ScheduleInput

One guild and one immutable monster version per scheduled raid.

```text
guildId: ID
monsterId: ID
monsterVersion: Version
startsAt: Time
durationSeconds: integer
participantLimit: integer
rewards: RaidRewards
```

#### Schedule

Desired lifecycle configuration; actual raid state is owned by Monster Raid.

```text
id: ID
version: Version
guildId: ID
monsterId: ID
monsterVersion: Version
startsAt: Time
durationSeconds: integer
participantLimit: integer
rewards: RaidRewards
status: "scheduled" / "inactive" / "cancelled"
dispatchStatus: "pending" / "applied"
```

#### ScheduleStatus

Reactivation requires no previous start/cancellation dispatch; deactivating a live instance cancels it.

```text
expectedVersion: Version
status: "scheduled" / "inactive" / "cancelled"
```

#### RaidStart

Registry-only command; uses the immutable schedule ID and version.

```text
scheduleId: ID
scheduleVersion: Version
```

#### RaidJoin

Reserves the caller primary pet for this raid.

```text
primaryPetId: ID
```

#### RaidParticipant

One member per raid. reservationId identifies the exclusive primary-pet hold.

```text
userId: ID
primaryPetId: ID
reservationId: ID
damageDealt: integer
joinedAt: Time
rewardStatus: SettlementState or null
```

#### Raid

Current raid state; version changes when HP/status changes.

```text
id: ID
scheduleId: ID
scheduleVersion: Version
guildId: ID
monster: Monster
hp: integer
startsAt: Time
endsAt: Time
status: "active" / "settling" / "won" / "failed" / "cancelled"
version: Version
participants: (RaidParticipant)[]
settlement: SettlementState or null
```

#### FriendRequestPage

Cursor page; nextCursor is null at the end.

```text
items: (FriendRequest)[]
nextCursor: string or null
```

#### RelationshipPage

Cursor page; nextCursor is null at the end.

```text
items: (Relationship)[]
nextCursor: string or null
```

#### BattlePage

Cursor page; nextCursor is null at the end.

```text
items: (Battle)[]
nextCursor: string or null
```

#### NotificationPage

Cursor page; nextCursor is null at the end.

```text
items: (Notification)[]
nextCursor: string or null
```

#### MapEntryPage

Cursor page; nextCursor is null at the end.

```text
items: (MapEntry)[]
nextCursor: string or null
```

#### GuildPage

Cursor page; nextCursor is null at the end.

```text
items: (Guild)[]
nextCursor: string or null
```

#### MemberPage

Cursor page; nextCursor is null at the end.

```text
items: (Member)[]
nextCursor: string or null
```

#### GuildInvitePage

Cursor page; nextCursor is null at the end.

```text
items: (GuildInvite)[]
nextCursor: string or null
```

#### PackagePage

Cursor page; nextCursor is null at the end.

```text
items: (Package)[]
nextCursor: string or null
```

#### EnrollmentPage

Cursor page; nextCursor is null at the end.

```text
items: (Enrollment)[]
nextCursor: string or null
```

#### MonsterPage

Cursor page; nextCursor is null at the end.

```text
items: (Monster)[]
nextCursor: string or null
```

#### SchedulePage

Cursor page; nextCursor is null at the end.

```text
items: (Schedule)[]
nextCursor: string or null
```

#### RaidPage

Cursor page; nextCursor is null at the end.

```text
items: (Raid)[]
nextCursor: string or null
```

#### EventEnvelope

Every broker message has stable identity and producer resource revision.

```text
eventId: ID
type: string
schemaVersion: "1"
occurredAt: Time
producer: "user-management" / "tamagotchi" / "battle" / "map" / "guild" / "monster-raid"
aggregateId: ID
aggregateVersion: Version
correlationId: ID
data: object
```

#### UserPackageRegisteredV1Data

Payload for user.package-registered.v1

```text
userId: ID
packageId: ID
enrollmentVersion: Version
createdAt: Time
```

#### FriendRequestedV1Data

Payload for friend.requested.v1

```text
requestId: ID
senderId: ID
recipientId: ID
```

#### MapEncounteredV1Data

Payload for map.encountered.v1

```text
userIds: (ID)[]
distanceMeters: number
```

#### BattleRequestedV1Data

Payload for battle.requested.v1

```text
battleId: ID
challengerId: ID
opponentId: ID
expiresAt: Time
```

#### BattleFinishedV1Data

Payload for battle.finished.v1

```text
battleId: ID
winnerId: ID
loserId: ID
```

#### PetUsedV1Data

Payload for pet.used.v1

```text
activityId: ID
petIds: (ID)[]
ownerIds: (ID)[]
```

#### PetCapturedV1Data

Payload for pet.captured.v1

```text
battleId: ID
petId: ID
previousOwnerId: ID
newOwnerId: ID
```

#### GuildInvitedV1Data

Payload for guild.invited.v1

```text
invitationId: ID
guildId: ID
inviterId: ID
recipientId: ID
```

#### RaidStartedV1Data

Payload for raid.started.v1

```text
raidId: ID
guildId: ID
recipientIds: (ID)[]
endsAt: Time
```

#### RaidFinishedV1Data

Payload for raid.finished.v1

```text
raidId: ID
guildId: ID
outcome: "won" / "failed" / "cancelled"
recipientIds: (ID)[]
```

#### ChatAuthenticate

First client frame, before any chat data. Do not persist or echo the token.

```text
type: "authenticate"
accessToken: string
```

#### ChatAuthenticated

Server confirms membership and current sequence; replay uses REST.

```text
type: "authenticated"
guildId: ID
userId: ID
latestSequence: integer
```

#### ChatSend

Client frame. requestId is also its deduplication key within the guild/user.

```text
type: "message.send"
requestId: ID
content: string
```

#### ChatAck

Server replies to the sender after durable persistence.

```text
type: "message.ack"
requestId: ID
message: ChatMessage
```

#### ChatCreated

Server broadcast to current guild members; deduplicate by message.id.

```text
type: "message.created"
message: ChatMessage
```

#### ChatError

Server protocol/business error; requestId is null for handshake errors.

```text
type: "error"
requestId: ID or null
code: "UNAUTHENTICATED" / "FORBIDDEN" / "INVALID_MESSAGE" / "RATE_LIMITED" / "IDEMPOTENCY_CONFLICT"
message: string
```
