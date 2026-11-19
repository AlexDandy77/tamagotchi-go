# Data warehouse and ETL contract

Lab 5 adds a **data warehouse**, one PostgreSQL database shaped for analytics, and an **ETL job** that fills it from the service databases every 5 minutes. The service databases stay built for the game. The warehouse answers questions that need several services' data at once, such as battles per day per package or XP earned by pet type.

Status: **proposed** (Lab 5).

## Components

| Component | What it is |
| --- | --- |
| `warehouse` | Stock `postgres:17.9`, database `warehouse`, volume `warehouse-data`. Bound to `localhost:5434` for analysis, like Grafana. Not replicated: everything in it can be rebuilt from the sources |
| `etl` | A CPR job (`deployment/etl/`), run by Compose on the stock `python:3.13-slim` image, every `ETL_INTERVAL_SECONDS` (300). `python3 scripts/lab.py etl` runs one pass on demand; `lab.py etl --full` rebuilds the warehouse from scratch |
| Grafana | A provisioned PostgreSQL data source for the warehouse and a "Game analytics" dashboard |

The ETL lives in the CPR for the same reason Kafka did: it belongs to no single service. It is the only component that reads every owner's data, and it reads only what owners publish for it.

## Extract: owner-defined `etl` views

The ETL **never reads an owner's tables**. Each owner creates a schema `etl` in its own database, holding the views below in its own migrations. The owner may change its tables freely, as long as the views keep their columns. This works like an API, but for data.

- `lab.py setup` creates the login role `etl_reader` (`ETL_DB_PASSWORD`) in every service database before migrations run. Owners grant it `USAGE` on schema `etl` and `SELECT` on the views, and nothing else.
- The ETL connects to `postgres-replicas:5433` (see [replication.md](replication.md)), so analytics never load the primary.
- Package Registry exposes the same as **MongoDB views** (`db.createView`) named `etl_packages` and `etl_monsters`, readable by a Mongo user `etl_reader`.
- Every view has `updated_at` (or `created_at` when rows never change), which the ETL uses as its watermark.

| Owner | View | Columns |
| --- | --- | --- |
| User Management | `etl.users` | `user_id`, `username`, `created_at`, `updated_at` |
| User Management | `etl.enrollments` | `user_id`, `package_id`, `enrolled_at`, `updated_at` |
| User Management | `etl.currency_movements` | `entry_id`, `user_id`, `package_id` (null for global), `currency` (`global`/`local`), `amount` (signed), `reason` (`battle`/`raid`/`care`), `activity_id`, `created_at` |
| Battle | `etl.battles` | `battle_id`, `challenger_id`, `opponent_id`, `winner_id`, `loser_id`, `status`, `turns`, `rules_version`, `created_at`, `finished_at`, `updated_at` |
| Tamagotchi | `etl.pets` | `pet_id`, `owner_id`, `package_id`, `pet_type`, `level`, `xp`, `created_at`, `updated_at` |
| Tamagotchi | `etl.care_actions` | `operation_id`, `pet_id`, `user_id`, `package_id`, `action_id`, `xp_gained`, `created_at` |
| Tamagotchi | `etl.ownership_transfers` | `battle_id`, `pet_id`, `from_user_id`, `to_user_id`, `transferred_at` |
| Notification | `etl.notifications` | `notification_id`, `user_id`, `type`, `created_at`, `read_at`, `updated_at` |
| Map | `etl.encounters` | `encounter_id`, `user_id`, `other_user_id`, `relation` (`friend`/`enemy`/`stranger`), `created_at` |
| Monster Raid | `etl.raids` | `raid_id`, `guild_id`, `monster_id`, `status`, `started_at`, `ended_at`, `updated_at` |
| Monster Raid | `etl.raid_participants` | `raid_id`, `user_id`, `pet_id`, `damage`, `reward_coins`, `updated_at` |
| Guild | `etl.guilds` | `guild_id`, `name`, `created_at`, `updated_at` |
| Guild | `etl.guild_members` | `guild_id`, `user_id`, `role`, `joined_at`, `left_at`, `updated_at` |
| Package Registry | `etl_packages` | `packageId`, `name`, `status`, `updatedAt` |
| Package Registry | `etl_monsters` | `monsterId`, `name`, `type`, `updatedAt` |

**Never exposed:** emails, password hashes, refresh or device tokens, chat message text, raw locations and coordinates, idempotency records.

## Transform and load: the star schema

```text
                 dim_date ─┐
     dim_user ─┐           │           ┌─ dim_package
               ├── fact_battle ────────┤
               ├── fact_care_action ───┤
               ├── fact_raid_participation ── dim_guild, dim_monster
               ├── fact_guild_membership
               ├── fact_currency_movement
               └── fact_notification
```

| Table | Grain (one row per) | Main columns |
| --- | --- | --- |
| `dim_date` | calendar day | `date_key` (YYYYMMDD), `date`, `year`, `month`, `day`, `weekday` |
| `dim_user` | user | `user_id`, `username`, `first_enrolled_at` |
| `dim_package` | package | `package_id`, `name`, `status` |
| `dim_guild` | guild | `guild_id`, `name` |
| `dim_monster` | monster | `monster_id`, `name`, `type` |
| `fact_battle` | finished or cancelled battle | `battle_id`, `date_key`, `package_id`, `challenger_id`, `opponent_id`, `winner_id`, `loser_id`, `status`, `turns`, `duration_seconds`, `captured_pet_id` |
| `fact_care_action` | care action | `operation_id`, `date_key`, `user_id`, `package_id`, `pet_type`, `action_id`, `xp_gained` |
| `fact_raid_participation` | user in a raid | `raid_id`, `date_key`, `user_id`, `guild_id`, `monster_id`, `damage`, `reward_coins`, `won` |
| `fact_guild_membership` | membership period | `guild_id`, `user_id`, `role`, `joined_date_key`, `left_date_key` |
| `fact_currency_movement` | ledger entry | `entry_id`, `date_key`, `user_id`, `package_id`, `currency`, `amount`, `reason` |
| `fact_notification` | notification | `notification_id`, `date_key`, `user_id`, `type`, `read_after_seconds` |

Transform rules:
- **Joins happen here, not in the sources.** `fact_care_action.pet_type` comes from `etl.pets`. `fact_battle.captured_pet_id` comes from `etl.ownership_transfers`, and `fact_battle.package_id` is the package of the captured pet, or the winner's when nothing was captured. `fact_raid_participation.won` comes from the raid's status.
- **Times → `date_key`** in UTC. Durations are computed from the two timestamps.
- **IDs stay the services' UUIDs.** They're unique across services, so no surrogate keys are needed.

## Incremental runs

1. For each view, read `etl.watermarks(source, view, last_updated_at)` from the warehouse.
2. Extract rows with `updated_at > last_updated_at − 60 s`, ordered by `updated_at`, in pages of 5,000. The 60-second look-back catches transactions that committed late with an older timestamp.
3. Transform, then **upsert** into the target table on its natural key (`INSERT … ON CONFLICT DO UPDATE`), so re-reading a row is harmless.
4. Commit the page and advance the watermark in the same warehouse transaction. A crashed run repeats at most one page.

One run handles each source independently. An unreachable source is logged (`etl source unavailable`), skipped and retried next run, and the other sources still load. Every run logs `etl run finished` with rows per view and its duration, and writes a row to `etl.runs`.

## Example questions

```sql
-- Battles per day and package
SELECT d.date, p.name, count(*) FROM fact_battle b
JOIN dim_date d USING (date_key) JOIN dim_package p ON p.package_id = b.package_id
WHERE b.status = 'finished' GROUP BY 1, 2 ORDER BY 1;

-- XP earned through care, by pet type
SELECT pet_type, sum(xp_gained) FROM fact_care_action GROUP BY pet_type;

-- Coins entering the economy, by source
SELECT reason, sum(amount) FROM fact_currency_movement WHERE currency = 'global' AND amount > 0 GROUP BY reason;
```

## Demo

1. Note the counts on Grafana → "Game analytics" (or `SELECT count(*) FROM fact_care_action` on `localhost:5434`).
2. Do a few care actions and finish a battle in Postman.
3. `python3 scripts/lab.py etl`, or wait for the next run. The `etl` container's log in Docker Desktop shows one info line per view and a summary:

   ```text
   {"level":"info","msg":"etl view loaded","source":"tamagotchi","view":"etl.care_actions","rows":3,"watermark":"…"}
   {"level":"info","msg":"etl run finished","rows":11,"durationMs":…,"sourcesSkipped":[]}
   ```

4. Refresh the dashboard: the new care actions and the battle appear. Run the ETL again: the counts stay the same. Rows re-read within the 60-second look-back are upserted, not duplicated, which shows the watermarks and upserts work.

## What each owner changes

| Owner | Change |
| --- | --- |
| Every PostgreSQL service | A migration that creates schema `etl`, the views listed for it, and grants `etl_reader` `USAGE` and `SELECT` |
| Package Registry | `etl_packages` and `etl_monsters` Mongo views and a read-only `etl_reader` user |
| Artur | The `warehouse` and `etl` services, `lab.py setup` (role, passwords) and `lab.py etl`, the Grafana dashboard |
