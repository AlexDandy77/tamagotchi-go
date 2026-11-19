# Distributed transactions contract

A battle changes three databases: User Management (stakes and boosts), Tamagotchi (pets, XP and ownership) and Battle (the battle itself). Lab 5 makes this change all-or-nothing with a **long-running saga** whose **coordinator is the Message Broker**. The saga replaces the two-phase commit (2PC) that the lab asks for first; [why](#from-two-phase-commit-to-a-saga) is explained below.

Status: **proposed** (Lab 5). The gRPC API is the `Saga` service in [proto/broker.proto](proto/broker.proto); the admin routes are in [message-broker.openapi.yaml](message-broker.openapi.yaml).

## From two-phase commit to a saga

**Two-phase commit** makes participants vote before anyone commits:

| 2PC phase | Battle participant call |
| --- | --- |
| Prepare: lock and vote | User Management `PUT /internal/v1/battle-holds/{battleId}`, Tamagotchi `PUT /internal/v1/pet-reservations/{battleId}` |
| Commit | User Management `PUT /internal/v1/battle-settlements/{battleId}`, Tamagotchi `PUT /internal/v1/pet-battle-settlements/{battleId}` |
| Abort | `DELETE` of the same holds and reservations |

For a battle this is a poor fit. The "prepared" state would last for the **whole combat**, minutes of 30-second turns, with the participants blocked on the coordinator's decision. If the coordinator crashed between the phases, they would stay blocked.

A **saga** commits each step locally right away and undoes earlier steps with **compensating actions** if a later one fails. Nothing waits on a vote. The holds and reservations are business-level states that the game already understands (a reserved pet can't be cared for), not database locks. The saga's progress is stored durably by the coordinator, so it survives restarts. This is the version that is implemented.

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
| 6 | `finish` | Battle `PUT /internal/v1/battle-outcomes/{battleId}` with `{status: "finished"}` | none: after the pivot |

**The pivot is the `finished` signal.** Before it, the saga can still be undone. After it, the result is decided, and the README rule applies: a completed reward is never reversed. The settlement steps are therefore only ever **retried forward**, never compensated.

**Undoing before the pivot.** A step can fail *permanently*: a `4xx` other than `408`/`429`, for example a `409` because a player lacks the 10-coin stake or a pet is already reserved. The step can also fail *transiently* until its 60-second budget runs out: transport errors, `503` and `504`. Either way, the broker runs the compensations of the completed steps in reverse order, then calls Battle `PUT /internal/v1/battle-outcomes/{battleId}` with `{status: "cancelled", reason}`. The same happens when Battle signals `cancelled` before combat starts, for example when the challenger cancels during `preparing`.

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

**By script**, because timing a crash by hand is unreliable: `python3 scripts/demo_lab5.py grade9` starts a battle, stops the broker container right after the `finished` signal, starts it again, and prints the saga before and after. It resumes at the step it reached, completes once, and the coins and pets moved exactly once.

## What each owner changes

| Owner | Change |
| --- | --- |
| Battle (Alexei) | On accept, call `Saga.Start` instead of calling User Management and Tamagotchi. When combat ends, call `Saga.Signal("finished")`; when cancelled before combat, `Saga.Signal("cancelled")`. Serve `PUT /internal/v1/battle-starts/{battleId}` and `PUT /internal/v1/battle-outcomes/{battleId}`. Remove the direct hold, reservation and settlement calls |
| User Management (Alexei) | Log the received `X-Saga-Id` on the hold, release and settlement routes, which are already idempotent per battle |
| Tamagotchi (Artur) | Log the received `X-Saga-Id` on the reservation, release and settlement routes, which are already idempotent per activity |
| Message Broker (Artur) | The `Saga` service, the SQLite saga store, the step runner and the admin routes |
| Monster Raid | Nothing: raids keep their current settlement flow |
