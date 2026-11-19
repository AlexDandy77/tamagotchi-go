# Database replication contract

Lab 5 replicates every service database and fails over automatically. PostgreSQL runs as a three-node **Patroni** cluster, and MongoDB as a three-member **replica set**. Each database therefore has a primary and **two replicas**. Services keep their current connection settings: the address `postgres:5432` always reaches the current primary.

Status: **proposed** (Lab 5).

## PostgreSQL

```text
                 ┌──────────── etcd (leader lock) ────────────┐
                 │                    │                       │
           postgres-1             postgres-2              postgres-3
        Patroni + PG 17        Patroni + PG 17          Patroni + PG 17
          (primary)  ──WAL──▶   (sync replica)  ◀──WAL──  (replica)
                 ▲                    ▲                       ▲
                 └────────── HAProxy health checks ───────────┘
   services ──▶ postgres:5432 (primary, read-write)   ETL ──▶ postgres-replicas:5433 (read-only)
```

| Container | Role |
| --- | --- |
| `etcd` | Stores Patroni's leader lock and cluster configuration |
| `postgres-1`, `postgres-2`, `postgres-3` | PostgreSQL 17 with Patroni (Spilo image, pinned to a released tag), one data volume each |
| `haproxy` | Network aliases `postgres` and `postgres-replicas`; forwards to the right nodes using Patroni's health endpoints |

**How a node becomes primary.** Each Patroni tries to hold the leader key in etcd. The holder runs PostgreSQL as primary and renews the key every 10 s (`loop_wait`). The key expires after 30 s (`ttl`). If the primary stops renewing it, because the container stopped, the host froze or the node lost the network, a healthy replica takes the key and promotes itself. A node that loses the key demotes itself, so **two primaries can never accept writes at once** (no split brain). `failsafe_mode` keeps the current primary running if only etcd is down while all members still see each other.

**Replication.** Streaming replication with `synchronous_mode: true` and one synchronous replica. A commit is confirmed only after the primary **and** one replica have it on disk, so a failover never loses a confirmed write such as a coin transfer or an outbox row. If the synchronous replica fails, Patroni makes the other replica synchronous. If both fail, the primary keeps accepting writes asynchronously (`synchronous_mode_strict: false`): availability comes first, and the gap is logged.

**Failover time.** Normally 10–30 s from the primary's failure until the new primary accepts writes. HAProxy checks every 2 s and moves `postgres:5432` within one check after promotion.

**Recovery.** A failed former primary that comes back rejoins as a replica: Patroni rewinds it with `pg_rewind`. Nobody has to intervene.

### Addresses

| Address | Goes to | Who uses it |
| --- | --- | --- |
| `postgres:5432` | The current primary (HAProxy checks `GET :8008/primary`) | Every service's `DATABASE_URL`, unchanged; `lab.py` provisioning and migrations |
| `postgres-replicas:5433` | Replicas, round-robin (`GET :8008/replica`); falls back to the primary when no replica is healthy | The ETL; reads that tolerate up to a few seconds of lag |
| `localhost:7000` | HAProxy statistics page, bound to localhost only like Grafana | Demo: shows which node is primary |

HAProxy closes the client sessions of a node that stops being primary (`on-marked-down shutdown-sessions`), so no connection keeps writing to a demoted node.

### What a failover looks like to a service

- Open connections to the old primary are closed. Queries in progress fail with a connection error, and transactions in progress are rolled back.
- The service's pool discards broken connections and opens new ones through `postgres:5432`, which now reaches the new primary.
- A request that failed because of the dropped connection answers `503 DEPENDENCY_UNAVAILABLE`, unless the service retried it. A service may retry **once**, after 1 s, when the work is a read or keyed by an `Idempotency-Key`. Retrying is safe because every mutation is idempotent.
- **Advisory-lock leaders.** The Go services' worker leaders hold a PostgreSQL advisory lock on a session. A lost connection means lost leadership: the worker stops, and a replica of that service takes the lock on the new primary. The workers already publish with stable `eventId`s, so a repeat is harmless.
- `GET /readyz` reports not ready while the database is unreachable, so the broker routes around the instance for those seconds.

### Data and settings

- `lab.py setup/up` provisions databases and roles on the primary through `postgres:5432`, exactly as before; replication copies them to the replicas. On the first start, `lab.py up` copies the old single-server volume `postgres-data` into the cluster once, with `pg_dumpall`, and leaves the old volume untouched.
- `max_connections=250` and the other server settings move into Patroni's cluster configuration, so all nodes share them.
- Volumes: `postgres-1-data`, `postgres-2-data`, `postgres-3-data`, `etcd-data`. `down` keeps them.

## MongoDB

Package Registry's database runs on the replica set `rs0` with three members, `mongo-1`, `mongo-2` and `mongo-3` (previously one member):

- Members elect a primary by majority vote. If the primary fails, a new one is elected in about 10–12 s.
- Registry's URL lists every member: `mongodb://registry:…@mongo-1:27017,mongo-2:27017,mongo-3:27017/registry?replicaSet=rs0&authSource=registry`. The driver finds and follows the primary by itself.
- Writes use `w: majority`, so a confirmed write is on two members and survives a failover.
- The members share the existing keyfile. `lab.py up` initiates the set, or adds the new members to an existing one.

## Why three nodes

Leader election needs a **majority**. With two nodes, losing one leaves one of two, which is no majority, so no new primary could be chosen safely. Three nodes tolerate one failure. The lab asks for at least two replicas per private service; every service database gets two.

## Demo

**By hand (browser, Docker Desktop, Postman):**
1. `python3 scripts/lab.py db-status` shows the members, their roles and replication lag (`patronictl list`, `rs.status()`). The HAProxy page `http://localhost:7000` shows the same primary in green.
2. Stop the primary container (say `postgres-1`) in Docker Desktop.
3. Watch the replicas' logs: within about 30 s one of them logs Patroni's `promoted self to leader by acquiring session lock`. The `haproxy` log shows the old node `DOWN` and the new one `UP`, and the HAProxy page turns the new primary green.
4. Send Postman requests (`GET /services/tamagotchi/v1/pets`, a care action): they work, against the new primary. A service log shows `database connection lost, reconnecting` once, then normal lines.
5. Start `postgres-1` again: its log shows it rejoining as a replica (`pg_rewind`), and `db-status` lists it as `Replica`.
6. The same for Mongo: stop the primary member; the others log the election, and the Package Registry requests keep working.

**By script**, for the numbers: `python3 scripts/demo_lab5.py grade5` sends a request every 200 ms, stops the primary, and prints when the new primary took over, how long writes were unavailable, and how many requests needed a retry.

## What each owner changes

| Owner | Change |
| --- | --- |
| All PostgreSQL services | No URL change. Check that the pool replaces broken connections, retries once for reads and idempotent writes, and reports not ready while the database is unreachable |
| Go services with an advisory-lock leader (User Management, Battle, Map, Monster Raid) | Treat a lost database session as lost leadership: stop the workers and compete for the lock again |
| Package Registry | Use the three-host URL from Compose and `w: majority` |
| Tamagotchi, Notification | Same pool and readiness checks (`pg` pool `error` handling) |
| Every service | Log a lost database connection once at warning level (`database connection lost, reconnecting`) and the recovery at info level (`database connection restored`) |
