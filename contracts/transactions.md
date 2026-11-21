# Distributed transactions contract

A battle changes three databases: User Management (stakes and boosts), Tamagotchi (pets, XP and ownership) and Battle (the battle itself). Lab 5 makes this change all-or-nothing with a **long-running saga** whose **coordinator is the Message Broker**. For the battle, the saga replaces the two-phase commit (2PC) that the lab asks for first; [why](#from-two-phase-commit-to-a-saga) is explained below. Joining a raid, which changes Monster Raid's and Tamagotchi's databases in one short step, is a real [two-phase commit](#two-phase-commit-joining-a-raid) coordinated by Monster Raid.

Status: **proposed** (Lab 5). The gRPC API is the `Saga` service in [proto/broker.proto](proto/broker.proto); the admin routes are in [message-broker.openapi.yaml](message-broker.openapi.yaml).

## From two-phase commit to a saga

**Two-phase commit** makes participants vote before anyone commits:

| 2PC phase | Battle participant call |
| --- | --- |
| Prepare: lock and vote | User Management `PUT /internal/v1/battle-holds/{battleId}`, Tamagotchi `PUT /internal/v1/pet-reservations/{battleId}` |
| Commit | User Management `PUT /internal/v1/battle-settlements/{battleId}`, Tamagotchi `PUT /internal/v1/pet-battle-settlements/{battleId}` |
| Abort | `DELETE` of the same holds and reservations |

For a battle this is a poor fit. The "prepared" state would last for the **whole combat**, minutes of 30-second turns, with the participants blocked on the coordinator's decision. If the coordinator crashed between the phases, they would stay blocked.

A **saga** commits each step locally right away and undoes earlier steps with **compensating actions** if a later one fails. Nothing waits on a vote. The holds and reservations are business-level states that the game already understands (a reserved pet can't be cared for), not database locks. The saga's progress is stored durably by the coordinator, so it survives restarts. This is the version that is implemented for the battle.

Two-phase commit is still the right tool where the prepared state lasts only milliseconds: [joining a raid](#two-phase-commit-joining-a-raid) changes two databases in one short step, and Monster Raid coordinates it as a real PostgreSQL two-phase commit.

## Two-phase commit: joining a raid

Joining a raid changes two databases: Monster Raid records the participant, and Tamagotchi reserves the pet, frozen for the fight. Both changes must happen together or not at all. The whole join takes milliseconds, so participants that hold their locks while they wait for the decision cost nothing, which is the case two-phase commit fits. Monster Raid is the **coordinator**, and both databases are **participants**, using PostgreSQL's own prepared transactions (`PREPARE TRANSACTION`, `COMMIT PREPARED`, `ROLLBACK PREPARED`).

| Phase | Monster Raid (coordinator and participant) | Tamagotchi (participant) |
| --- | --- | --- |
| Begin | Records the transaction as `preparing` in its `join_transactions` table and commits that row | — |
| 1. Prepare | — | `PUT /internal/v1/pet-reservations/{activityId}/prepare` with `PetReserveInput` (`kind: "raid"`). Tamagotchi runs the reservation in one transaction and ends it with `PREPARE TRANSACTION 'pet-reservation:{activityId}'`. `200` with the frozen `PetReservation` is a **yes** vote; a refusal is a **no** vote with nothing prepared |
| 1. Prepare | Its own transaction locks the raid, checks that the raid is active and has a free place, and stores the joined participant with the frozen pet. It ends with `PREPARE TRANSACTION 'monster-raid-join:{transactionId}'`, a yes vote; a failed check is a no vote | — |
| Decision | Two yes votes: records `commit` in `join_transactions`, which is the **commit point**. Any no vote, error or timeout: records `abort` | — |
| 2. Commit or abort | `COMMIT PREPARED` or `ROLLBACK PREPARED` of its own transaction | `PUT /internal/v1/pet-reservations/{activityId}/commit` or `.../abort` |

- **Order:** Tamagotchi prepares first, because Monster Raid's participant row stores the frozen pet that Tamagotchi returns.
- **`activityId`** is the participant's reservation ID, `UUIDv5(raidId, userId)`, so the raid's settlement later rewards or releases the pet exactly as before. An aborted prepare leaves no reservation and no tombstone, so the player can simply try again.
- **Nobody decides alone.** A prepared transaction keeps its row locks and stays invisible until the coordinator's decision arrives, even across a restart: it is in the WAL, so a promoted replica holds it too. That waiting is the price of two-phase commit, and the reason the battle, which would wait for minutes, uses the saga instead.
- **Recovery:** Monster Raid's leader resolves every unfinished transaction every 2 seconds:
  - a decided `commit` is committed on both sides, retried until each answers;
  - a decided `abort`, or a `preparing` older than 10 seconds whose coordinator crashed, is rolled back on both sides.
  - `abort` answers `204` even when nothing was prepared, so an unanswered prepare is always safe to abort.
- **Idempotency:**
  - `commit` repeated after success answers `204` again;
  - `prepare` for an `activityId` that is already prepared or reserved answers `409`, a no vote;
  - each call carries an `Idempotency-Key` derived from the transaction ID and the phase, never from the `activityId`: a later join of the same player to the same raid must run its phases, not replay an earlier join's answers.
- **One join at a time:** Monster Raid keeps at most one unfinished join transaction per `activityId`. A second join of the same player to the same raid answers `409 JOIN_IN_PROGRESS` until the first is finished. Both would share Tamagotchi's `pet-reservation:{activityId}`, so the second one's abort would otherwise roll back the first one's prepare.
- **Settings:** PostgreSQL disables prepared transactions by default (`max_prepared_transactions = 0`). The cluster sets it to 64 on every node ([replication.md](replication.md)).

### Logs and demo

Monster Raid logs `join transaction prepared`, `join transaction committed` or `join transaction aborted` (with the reason), and `join transaction recovered`, each with the `transactionId` and the `reservationId`. Tamagotchi logs `pet reservation prepared`, `committed` and `aborted` with the `activityId`, which is that `reservationId`, so filtering either container's logs by it shows both sides of one join.

1. **Commit:** join a raid in Postman. Both containers' logs show the prepare and the commit, and `SELECT * FROM pg_prepared_xacts` on either database is empty again.
2. **Abort on a no vote:** join with a pet that is already reserved. Tamagotchi votes no, the participant is never visible, and the raid's participant count doesn't change.
3. **Blocking and recovery:** with `JOIN_PREPARED_PAUSE_SECONDS` set on Monster Raid, a join stops after both votes. Stop that replica: `pg_prepared_xacts` on both databases shows the prepared transactions, and the raid's row stays locked. Within about 10 seconds another replica's leader aborts them (`join transaction recovered`), and the raid is free again.

**By script:** `python3 scripts/demo_lab5.py grade8` runs all three with two new players in a new guild. It checks each side's log lines and `pg_prepared_xacts` after every step. For the third, it recreates the Monster Raid replicas with a 30-second pause, stops the replica that logged `join transaction prepared`, waits for `join transaction recovered`, recreates the replicas without the pause, and then joins the same player again.

## The battle saga

Battle starts one saga per battle when the opponent accepts. The saga's ID is the battle's ID. It spans the whole battle: reservation, combat, settlement.

| # | Step | Call (made by the broker) | Compensation |
| --- | --- | --- | --- |
| 1 | `reserve-stakes` | User Management `PUT /internal/v1/battle-holds/{battleId}` with `BattleHoldInput` | `DELETE /internal/v1/battle-holds/{battleId}` |
| 2 | `reserve-pets` | Tamagotchi `PUT /internal/v1/pet-reservations/{battleId}` with `PetReserveInput` (`kind: "battle"`) | `DELETE /internal/v1/pet-reservations/{battleId}` |
| 3 | `start-combat` | Battle `PUT /internal/v1/battle-starts/{battleId}` with the `PetReservation` returned by step 2 | — (step 3 is the last step before waiting) |
| — | *wait* | The saga waits until Battle signals the result with `Saga.Signal(battleId, "finished", {winnerId, loserId})` | — |
| 4 | `settle-stakes` | User Management `PUT /internal/v1/battle-settlements/{battleId}` with `BattleMoneyResult` | none: after the pivot |
| 5 | `settle-pets` | Tamagotchi `PUT /internal/v1/pet-battle-settlements/{battleId}` with `PetBattleResult` | none: after the pivot |
| 6 | `finish` | Battle `PUT /internal/v1/battle-outcomes/{battleId}` with `{status: "finished"}` (`reason` is only sent with `cancelled`) | none: after the pivot |

**The pivot is the `finished` signal.** Before it, the saga can still be undone. After it, the result is decided, and the README rule applies: a completed reward is never reversed. The settlement steps are therefore only ever **retried forward**, never compensated.

**Undoing before the pivot.** A step can fail *permanently*: a `4xx` other than `408`/`429`, for example a `409` because a player lacks the 10-coin stake or a pet is already reserved. It can also fail *transiently* until its 60-second budget runs out: transport errors, `503` and `504`. A transient failure leaves the outcome **unknown**: the hold may have committed even though its answer was lost. So the broker compensates every step it **attempted**, not only the ones it saw succeed:

1. **Resolve `start-combat` first** if it was attempted. The broker calls Battle `PUT /internal/v1/battle-outcomes/{battleId}` with `{status: "cancelled", reason}`.
   - If Battle answers `200`, the battle is fenced: it can never become active, and a late `battle-starts` call answers `409 BATTLE_CANCELLED`.
   - If Battle answers `409 BATTLE_ACTIVE`, combat did start, so there is nothing to undo; the saga continues to *waiting* instead.
2. **Release the reservations** in reverse order, pets then stakes: `DELETE /internal/v1/pet-reservations/{battleId}`, then `DELETE /internal/v1/battle-holds/{battleId}`. Each release is called even when its reserve step's outcome is unknown. Both are idempotent: `204` means released, and `404` means nothing was held.
3. **Close the battle.** If step 1 didn't run, the broker calls Battle `battle-outcomes` with `cancelled` last.

The saga is `compensated` only after every release answered `204` or `404`. Releases are retried until they do.

**A release closes the reservation for good.** A release for an id that holds nothing records a tombstone. A reserve that arrives later for that id answers `409 RESERVATION_CLOSED`, so a delayed or retried reserve can never take coins or pets after the saga gave them back. The same happens when Battle signals `cancelled` before combat starts, for example when the challenger cancels during `preparing`.

```text
started ──1──▶ ──2──▶ ──3──▶ waiting ──signal "finished"──▶ settling ──4──▶ ──5──▶ ──6──▶ completed
   │            │       │        │
   └────────────┴───────┴────────┴──(permanent failure / timeout / signal "cancelled")──▶ compensating ──▶ compensated
```

Battle's statuses follow the saga: `preparing` while steps 1–3 run, `active` after step 3, `settling` after the signal, and then `finished` or `cancelled` from the outcome call.

## Coordinator rules

- **Durable state.** Each saga and each step attempt is written to the broker's SQLite store (`synchronous=FULL`) before the next call is made. After a restart the broker resumes every saga that is not `completed` or `compensated` from its last recorded step.
- **Idempotent calls.** Every step and compensation is sent with `Idempotency-Key: UUIDv5(namespace = sagaId, name = "{step}")` or `"{step}:compensate"`, plus `X-Saga-Id: {sagaId}`. Repeating a call after a crash returns the stored result. A compensation that answers `404` counts as done (nothing was reserved).
- **Retries.** Transient failures are retried with a backoff of 1, 2, 4, 8 and 16 s:
  - before the pivot, until the step's 60-second budget ends;
  - after the pivot and for compensations, forever, with the backoff capped at 60 s. A saga still settling after 5 minutes logs `saga stuck` at error level, and the dashboard shows it.
- **Calls go through the router.** Steps use the same load-based choice, circuit breakers and rerouting as `Router.Call`, so a saga survives an instance failure.
- **Identity.**
  - Participant calls carry a broker-signed assertion on behalf of the saga's initiator (`kind: service`, `sub: battle`), so User Management's and Tamagotchi's caller allowlists stay as they are.
  - Calls to the initiator's own step routes (`battle-starts`, `battle-outcomes`) carry the broker's own identity (`sub: message-broker`), and those routes allow only `message-broker`.
- **One saga per ID.** `Saga.Start` with an existing `sagaId` and the same input returns the existing saga; with different input it fails with `ALREADY_EXISTS`.
- **Limits.** Sagas count against `MAX_CONCURRENT_TASKS` only while a step call is in flight; a waiting saga holds no thread. Completed and compensated sagas are kept for `RETENTION_HOURS`.

## gRPC API

```proto
service Saga {
  rpc Start(StartSagaRequest) returns (SagaState);   // initiator only; idempotent per saga_id
  rpc Signal(SignalSagaRequest) returns (SagaState); // initiator only: "finished" or "cancelled"
  rpc Get(GetSagaRequest) returns (SagaState);
}
```

Only the service that started a saga may signal or read it (checked against the certificate SAN). Messages: [proto/broker.proto](proto/broker.proto).

## Admin routes

| Route | Purpose |
| --- | --- |
| `GET /v1/sagas` | Sagas with their state and current step; filters `type?`, `state?`, `cursor?`, `limit?` |
| `GET /v1/sagas/{sagaId}` | One saga with every step attempt, its status code and timing |

Both are `admin` routes reached through Gateway, like the other broker admin routes. Metrics: `broker_sagas_total{type, state}` and `broker_saga_step_duration_seconds{type, step}`.

## Logs

The broker writes one **info-level** line per transition, and every participant logs the `X-Saga-Id` it received. Filtering any container's logs by the battle ID in Docker Desktop therefore shows that container's part of the saga:

```text
message-broker   {"level":"info","msg":"saga started","sagaId":"…","type":"battle","initiator":"battle"}
message-broker   {"level":"info","msg":"saga step done","sagaId":"…","step":"reserve-stakes","status":200,"instanceId":"user-management-2"}
tamagotchi-1     {"level":"info","msg":"pet reservation stored","sagaId":"…","activityId":"…"}
message-broker   {"level":"warning","msg":"saga step failed","sagaId":"…","step":"reserve-pets","status":409}
message-broker   {"level":"info","msg":"saga compensation done","sagaId":"…","step":"reserve-stakes"}
message-broker   {"level":"info","msg":"saga resumed after restart","sagaId":"…","step":"settle-pets"}
message-broker   {"level":"info","msg":"saga completed","sagaId":"…","durationMs":…}
```

## Demo

**By hand (Postman and Docker Desktop):**
1. **Happy path.** Play a battle to the end. `GET /services/message-broker/v1/sagas/{battleId}` shows steps 1–6 `done` and the state `completed`. The broker's logs show the same steps in order, and the User Management and Tamagotchi logs show their part with the same `sagaId`.
2. **Compensation.** Challenge with a player who has fewer than 10 coins, or reserve a pet that is already reserved, then accept. The saga shows the failed step, the compensated steps and the state `compensated`. `GET /v1/battles/{battleId}` is `cancelled` with the reason, and the wallet shows the stake released.
3. **Unknown outcome.** Pause the Tamagotchi containers in Docker Desktop during an accept. The reservation step runs out of budget with an unknown outcome; the broker still calls both releases and logs `saga compensation done` for each, so nothing stays held.

**By script**, because timing a crash by hand is unreliable: `python3 scripts/demo_lab5.py grade9` starts a battle saga with the battle certificate, as Battle does, prints its steps, restarts the broker container mid-saga, and checks that the saga is still there and resumed from its last recorded step (`saga resumed after restart`). Every call carries its step's `Idempotency-Key`, so the resumed steps change nothing twice.

## What each owner changes

| Owner | Change |
| --- | --- |
| Battle (Alexei) | On accept, call `Saga.Start` instead of calling User Management and Tamagotchi. When combat ends, call `Saga.Signal("finished")`; when cancelled before combat, `Saga.Signal("cancelled")`. Serve `PUT /internal/v1/battle-starts/{battleId}` and `PUT /internal/v1/battle-outcomes/{battleId}`: `cancelled` on an active battle answers `409 BATTLE_ACTIVE`, and a start after `cancelled` answers `409 BATTLE_CANCELLED`. Remove the direct hold, reservation and settlement calls |
| User Management (Alexei) | Log the received `X-Saga-Id` on the hold, release and settlement routes. A release of an unknown `battleId` answers `404` and records a tombstone; a later hold for that id answers `409 RESERVATION_CLOSED` |
| Tamagotchi (Artur) | Log the received `X-Saga-Id` on the reservation, release and settlement routes. Same tombstone rule for `pet-reservations`. For the raid join, also serve `PUT /internal/v1/pet-reservations/{activityId}/prepare`, `/commit` and `/abort` for Monster Raid, with `PREPARE TRANSACTION 'pet-reservation:{activityId}'`, `COMMIT PREPARED` and `ROLLBACK PREPARED` |
| Message Broker (Artur) | The `Saga` service, the SQLite saga store, the step runner and the admin routes |
| Monster Raid | Coordinates the raid join as a two-phase commit ([above](#two-phase-commit-joining-a-raid)) when `JOIN_COMMIT=two-phase`: the `join_transactions` log, its own prepared transaction, the decision and the recovery worker. Raids keep their settlement flow |
