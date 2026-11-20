#!/usr/bin/env python3
"""Lab 5 ETL (contracts/data-warehouse.md): service databases -> warehouse star schema.

Stage 1 copies new and changed rows from every owner's `etl` views (read on the replicas as
the read-only role etl_reader) into etl.staging, one source at a time; an unreachable source
is skipped and caught up later. Stage 2 recomputes each fact for every key whose rows changed
in ANY of the fact's inputs during this run, so a late source repairs the facts it feeds.

    python etl.py --once      one run
    python etl.py --every 300 a run every 5 minutes (the Compose service)
    python etl.py --full      empty the warehouse, then one run from scratch
"""
import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path

LOOK_BACK = datetime.timedelta(seconds=60)
PAGE = 5000
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def source(database, view, key, updated, engine='postgres'):
    return {'source': database, 'view': view, 'key': key, 'updated': updated, 'engine': engine}


# What each owner publishes (contracts/data-warehouse.md, "Extract"). A view that doesn't exist
# yet is reported and skipped, so owners can add theirs one at a time.
SOURCES = [
    source('users', 'users', ('user_id',), 'updated_at'),
    source('users', 'enrollments', ('user_id', 'package_id'), 'updated_at'),
    source('users', 'currency_movements', ('entry_id',), 'created_at'),
    source('battles', 'battles', ('battle_id',), 'updated_at'),
    source('tamagotchi', 'pets', ('pet_id',), 'updated_at'),
    source('tamagotchi', 'care_actions', ('operation_id',), 'created_at'),
    source('tamagotchi', 'ownership_transfers', ('battle_id', 'pet_id'), 'transferred_at'),
    source('notification', 'notifications', ('notification_id',), 'updated_at'),
    source('locations', 'encounters', ('encounter_id',), 'created_at'),
    source('raids', 'raids', ('raid_id',), 'updated_at'),
    source('raids', 'raid_participants', ('raid_id', 'user_id'), 'updated_at'),
    source('guilds', 'guilds', ('guild_id',), 'updated_at'),
    source('guilds', 'guild_members', ('guild_id', 'user_id'), 'updated_at'),
    source('registry', 'etl_packages', ('packageId',), 'updatedAt', engine='mongo'),
    source('registry', 'etl_monsters', ('monsterId',), 'updatedAt', engine='mongo'),
]

DATE_KEY = "to_char(({}) AT TIME ZONE 'UTC', 'YYYYMMDD')::int"


def stg(alias, src, view):
    return f"etl.staging {alias} ON {alias}.source = '{src}' AND {alias}.view = '{view}'"


def changed(src, view, key_sql="s.row_key"):
    """Keys of a fact touched through one input since the last finished run: `key_sql` maps the
    input row to the fact's key. Rows a crashed run staged are newer than that run, so they
    are picked up by the next one."""
    return f"SELECT {key_sql} AS k FROM etl.staging s WHERE s.source = '{src}' AND s.view = '{view}' AND s.loaded_at > %(since)s"


# Stage 2: each fact, its inputs (for the changed-key set) and how it is computed.
FACTS = {
    'fact_care_action': f"""
WITH changed AS (
  {changed('tamagotchi', 'care_actions')}
  UNION SELECT c.row_key FROM etl.staging c
    JOIN etl.staging p ON p.source = 'tamagotchi' AND p.view = 'pets' AND p.loaded_at > %(since)s AND p.row_key = c.data->>'pet_id'
    WHERE c.source = 'tamagotchi' AND c.view = 'care_actions')
INSERT INTO fact_care_action (operation_id, date_key, user_id, package_id, pet_id, pet_type, action_id, xp_gained, complete)
SELECT c.row_key::uuid, {DATE_KEY.format("(c.data->>'created_at')::timestamptz")}, c.data->>'user_id',
       (c.data->>'package_id')::uuid, (c.data->>'pet_id')::uuid, p.data->>'pet_type', c.data->>'action_id',
       (c.data->>'xp_gained')::int, p.row_key IS NOT NULL
FROM etl.staging c LEFT JOIN {stg('p', 'tamagotchi', 'pets')} AND p.row_key = c.data->>'pet_id'
WHERE c.source = 'tamagotchi' AND c.view = 'care_actions' AND c.row_key IN (SELECT k FROM changed)
ON CONFLICT (operation_id) DO UPDATE SET date_key = EXCLUDED.date_key, user_id = EXCLUDED.user_id,
  package_id = EXCLUDED.package_id, pet_id = EXCLUDED.pet_id, pet_type = EXCLUDED.pet_type,
  action_id = EXCLUDED.action_id, xp_gained = EXCLUDED.xp_gained, complete = EXCLUDED.complete""",

    # A finished battle always moves the loser's primary, so it is complete once the transfer and
    # the captured pet (for its package) arrived. Battles still running are not facts yet.
    'fact_battle': f"""
WITH changed AS (
  {changed('battles', 'battles')}
  UNION {changed('tamagotchi', 'ownership_transfers', "s.data->>'battle_id'")}
  UNION SELECT t.data->>'battle_id' FROM etl.staging t
    JOIN etl.staging p ON p.source = 'tamagotchi' AND p.view = 'pets' AND p.loaded_at > %(since)s AND p.row_key = t.data->>'pet_id'
    WHERE t.source = 'tamagotchi' AND t.view = 'ownership_transfers')
INSERT INTO fact_battle (battle_id, date_key, package_id, challenger_id, opponent_id, winner_id, loser_id, status,
                         turns, duration_seconds, captured_pet_id, complete)
SELECT b.row_key::uuid,
       {DATE_KEY.format("COALESCE((b.data->>'finished_at')::timestamptz, (b.data->>'created_at')::timestamptz)")},
       (p.data->>'package_id')::uuid, b.data->>'challenger_id', b.data->>'opponent_id', b.data->>'winner_id',
       b.data->>'loser_id', b.data->>'status', (b.data->>'turns')::int,
       EXTRACT(EPOCH FROM (b.data->>'finished_at')::timestamptz - (b.data->>'created_at')::timestamptz)::int,
       (t.data->>'pet_id')::uuid, b.data->>'status' <> 'finished' OR (t.row_key IS NOT NULL AND p.row_key IS NOT NULL)
FROM etl.staging b
LEFT JOIN etl.staging t ON t.source = 'tamagotchi' AND t.view = 'ownership_transfers' AND t.data->>'battle_id' = b.row_key
LEFT JOIN {stg('p', 'tamagotchi', 'pets')} AND p.row_key = t.data->>'pet_id'
WHERE b.source = 'battles' AND b.view = 'battles' AND b.row_key IN (SELECT k FROM changed)
  AND b.data->>'status' IN ('finished', 'cancelled')
ON CONFLICT (battle_id) DO UPDATE SET date_key = EXCLUDED.date_key, package_id = EXCLUDED.package_id,
  challenger_id = EXCLUDED.challenger_id, opponent_id = EXCLUDED.opponent_id, winner_id = EXCLUDED.winner_id,
  loser_id = EXCLUDED.loser_id, status = EXCLUDED.status, turns = EXCLUDED.turns,
  duration_seconds = EXCLUDED.duration_seconds, captured_pet_id = EXCLUDED.captured_pet_id, complete = EXCLUDED.complete""",

    'fact_notification': f"""
INSERT INTO fact_notification (notification_id, date_key, user_id, type, read_after_seconds)
SELECT n.row_key::uuid, {DATE_KEY.format("(n.data->>'created_at')::timestamptz")}, n.data->>'user_id', n.data->>'type',
       EXTRACT(EPOCH FROM (n.data->>'read_at')::timestamptz - (n.data->>'created_at')::timestamptz)::int
FROM etl.staging n WHERE n.source = 'notification' AND n.view = 'notifications' AND n.loaded_at > %(since)s
ON CONFLICT (notification_id) DO UPDATE SET read_after_seconds = EXCLUDED.read_after_seconds""",

    'fact_currency_movement': f"""
INSERT INTO fact_currency_movement (entry_id, date_key, user_id, package_id, currency, amount, reason)
SELECT m.row_key::uuid, {DATE_KEY.format("(m.data->>'created_at')::timestamptz")}, m.data->>'user_id',
       (m.data->>'package_id')::uuid, m.data->>'currency', (m.data->>'amount')::bigint, m.data->>'reason'
FROM etl.staging m WHERE m.source = 'users' AND m.view = 'currency_movements' AND m.loaded_at > %(since)s
ON CONFLICT (entry_id) DO NOTHING""",

    'fact_raid_participation': f"""
WITH changed AS (
  {changed('raids', 'raid_participants')}
  UNION SELECT rp.row_key FROM etl.staging rp
    JOIN etl.staging r ON r.source = 'raids' AND r.view = 'raids' AND r.loaded_at > %(since)s AND r.row_key = rp.data->>'raid_id'
    WHERE rp.source = 'raids' AND rp.view = 'raid_participants')
INSERT INTO fact_raid_participation (raid_id, user_id, date_key, guild_id, monster_id, damage, reward_coins, won, complete)
SELECT (rp.data->>'raid_id')::uuid, rp.data->>'user_id',
       {DATE_KEY.format("COALESCE((r.data->>'ended_at')::timestamptz, (rp.data->>'updated_at')::timestamptz)")},
       (r.data->>'guild_id')::uuid, (r.data->>'monster_id')::uuid, (rp.data->>'damage')::int,
       (rp.data->>'reward_coins')::int, r.data->>'status' = 'won', r.row_key IS NOT NULL
FROM etl.staging rp LEFT JOIN {stg('r', 'raids', 'raids')} AND r.row_key = rp.data->>'raid_id'
WHERE rp.source = 'raids' AND rp.view = 'raid_participants' AND rp.row_key IN (SELECT k FROM changed)
ON CONFLICT (raid_id, user_id) DO UPDATE SET date_key = EXCLUDED.date_key, guild_id = EXCLUDED.guild_id,
  monster_id = EXCLUDED.monster_id, damage = EXCLUDED.damage, reward_coins = EXCLUDED.reward_coins,
  won = EXCLUDED.won, complete = EXCLUDED.complete""",

    'fact_guild_membership': f"""
INSERT INTO fact_guild_membership (guild_id, user_id, role, joined_date_key, left_date_key)
SELECT (g.data->>'guild_id')::uuid, g.data->>'user_id', g.data->>'role', {DATE_KEY.format("(g.data->>'joined_at')::timestamptz")},
       CASE WHEN g.data->>'left_at' IS NULL THEN NULL ELSE {DATE_KEY.format("(g.data->>'left_at')::timestamptz")} END
FROM etl.staging g WHERE g.source = 'guilds' AND g.view = 'guild_members' AND g.loaded_at > %(since)s
ON CONFLICT (guild_id, user_id) DO UPDATE SET role = EXCLUDED.role, left_date_key = EXCLUDED.left_date_key""",
}

# Dimensions: from their owner's view when it exists, otherwise from the ids the facts mention.
DIMENSIONS = [
    """INSERT INTO dim_user (user_id, username)
SELECT s.row_key, s.data->>'username' FROM etl.staging s WHERE s.source = 'users' AND s.view = 'users' AND s.loaded_at > %(since)s
ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username""",
    """INSERT INTO dim_user (user_id) SELECT DISTINCT user_id FROM (
  SELECT user_id FROM fact_care_action UNION SELECT user_id FROM fact_notification
  UNION SELECT user_id FROM fact_currency_movement UNION SELECT user_id FROM fact_raid_participation) u
ON CONFLICT (user_id) DO NOTHING""",
    """UPDATE dim_user d SET first_enrolled_at = e.first FROM (
  SELECT s.data->>'user_id' AS user_id, min((s.data->>'enrolled_at')::timestamptz) AS first
  FROM etl.staging s WHERE s.source = 'users' AND s.view = 'enrollments' GROUP BY 1) e WHERE d.user_id = e.user_id""",
    """INSERT INTO dim_package (package_id, name, status)
SELECT s.row_key::uuid, s.data->>'name', s.data->>'status' FROM etl.staging s
WHERE s.source = 'registry' AND s.view = 'etl_packages' AND s.loaded_at > %(since)s
ON CONFLICT (package_id) DO UPDATE SET name = EXCLUDED.name, status = EXCLUDED.status""",
    """INSERT INTO dim_package (package_id) SELECT DISTINCT (s.data->>'package_id')::uuid FROM etl.staging s
WHERE s.source = 'tamagotchi' AND s.view = 'pets' ON CONFLICT (package_id) DO NOTHING""",
    """INSERT INTO dim_guild (guild_id, name) SELECT s.row_key::uuid, s.data->>'name' FROM etl.staging s
WHERE s.source = 'guilds' AND s.view = 'guilds' AND s.loaded_at > %(since)s
ON CONFLICT (guild_id) DO UPDATE SET name = EXCLUDED.name""",
    """INSERT INTO dim_monster (monster_id, name, type) SELECT s.row_key::uuid, s.data->>'name', s.data->>'type' FROM etl.staging s
WHERE s.source = 'registry' AND s.view = 'etl_monsters' AND s.loaded_at > %(since)s
ON CONFLICT (monster_id) DO UPDATE SET name = EXCLUDED.name, type = EXCLUDED.type""",
    """INSERT INTO dim_date (date_key, date, year, month, day, weekday)
SELECT k, d, EXTRACT(YEAR FROM d), EXTRACT(MONTH FROM d), EXTRACT(DAY FROM d), EXTRACT(ISODOW FROM d)
FROM (SELECT DISTINCT date_key AS k, to_date(date_key::text, 'YYYYMMDD') AS d FROM (
  SELECT date_key FROM fact_care_action UNION SELECT date_key FROM fact_battle UNION SELECT date_key FROM fact_notification
  UNION SELECT date_key FROM fact_currency_movement UNION SELECT date_key FROM fact_raid_participation
  UNION SELECT joined_date_key FROM fact_guild_membership) all_keys) dates
ON CONFLICT (date_key) DO NOTHING""",
]


def log(message, **fields):
    """One JSON line per event on stdout: visible in Docker Desktop and shipped to Loki by Alloy."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(json.dumps({'time': now, 'level': 'info', 'service': 'etl', 'msg': message, **fields}, default=str), flush=True)


def row_key(row, key):
    return '|'.join(str(row[column]) for column in key)


def as_utc(value):
    if isinstance(value, str):
        value = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)


def staged_rows(rows, spec, run):
    """(source, view, row_key, json, updated, loaded_at) tuples for etl.staging."""
    return [(spec['source'], spec['view'], row_key(row, spec['key']), json.dumps(row, default=str),
             as_utc(row[spec['updated']]), run) for row in rows]


def extract_postgres(spec, since, env):
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(host=env.get('ETL_SOURCE_HOST', 'postgres-replicas'), port=int(env.get('ETL_SOURCE_PORT', '5433')),
                         dbname=spec['source'], user='etl_reader', password=env['ETL_DB_PASSWORD'],
                         connect_timeout=5, row_factory=dict_row) as conn:
        cursor = conn.execute(
            f'SELECT * FROM etl.{spec["view"]} WHERE {spec["updated"]} > %s ORDER BY {spec["updated"]}', (since,))
        while page := cursor.fetchmany(PAGE):
            yield page


def extract_mongo(spec, since, env):
    import pymongo
    # Every member as a seed, so the set is found whichever member is down.
    hosts = env.get('ETL_MONGO_HOSTS', 'mongo:27017,mongo-2:27017,mongo-3:27017').split(',')
    client = pymongo.MongoClient(host=hosts, username='etl_reader',
                                 password=env['ETL_DB_PASSWORD'], authSource='registry', replicaSet='rs0',
                                 readPreference='secondaryPreferred', serverSelectionTimeoutMS=5000)
    try:
        if spec['view'] not in client.registry.list_collection_names():
            raise LookupError(f'view registry.{spec["view"]} does not exist yet')
        found = client.registry[spec['view']].find({spec['updated']: {'$gt': since}}, {'_id': 0}).sort(spec['updated'])
        page = []
        for document in found:
            page.append(document)
            if len(page) == PAGE:
                yield page
                page = []
        if page:
            yield page
    finally:
        client.close()


def stage_source(warehouse, spec, run, env, extract=None):
    """Copies one view's new rows into etl.staging; returns how many rows were staged."""
    extract = extract or (extract_mongo if spec['engine'] == 'mongo' else extract_postgres)
    found = warehouse.execute('SELECT last_updated_at FROM etl.watermarks WHERE source = %s AND view = %s',
                              (spec['source'], spec['view'])).fetchone()
    since = (found[0] if found else EPOCH) - LOOK_BACK
    total = 0
    for page in extract(spec, since, env):
        rows = staged_rows(page, spec, run)
        with warehouse.transaction():
            warehouse.cursor().executemany(
                """INSERT INTO etl.staging (source, view, row_key, data, source_updated_at, loaded_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                   ON CONFLICT (source, view, row_key) DO UPDATE SET data = EXCLUDED.data,
                     source_updated_at = EXCLUDED.source_updated_at, loaded_at = EXCLUDED.loaded_at""", rows)
            warehouse.execute(
                """INSERT INTO etl.watermarks (source, view, last_updated_at) VALUES (%s, %s, %s)
                   ON CONFLICT (source, view) DO UPDATE SET last_updated_at = GREATEST(etl.watermarks.last_updated_at, EXCLUDED.last_updated_at)""",
                (spec['source'], spec['view'], max(row[4] for row in rows)))
        total += len(rows)
    return total


def run_once(warehouse, env, extract=None, now=None):
    run = (now or datetime.datetime.now)(datetime.timezone.utc)
    started = time.monotonic()
    staged, skipped = 0, []
    for spec in SOURCES:
        try:
            rows = stage_source(warehouse, spec, run, env, extract)
        except Exception as error:  # one source down must not stop the others
            skipped.append(f"{spec['source']}.{spec['view']}")
            log('etl source unavailable', source=spec['source'], view=spec['view'], error=str(error).splitlines()[0][:200])
            continue
        staged += rows
        log('etl view loaded', source=spec['source'], view=spec['view'], rows=rows)
    recomputed = 0
    # Everything staged since the last run that finished, so a run that died between the
    # stages is repaired by the next one rather than lost.
    last = warehouse.execute('SELECT max(started_at) FROM etl.runs').fetchone()
    since = {'since': (last[0] if last and last[0] else EPOCH)}
    with warehouse.transaction():
        for fact, sql in FACTS.items():
            count = warehouse.execute(sql, since).rowcount
            recomputed += max(count, 0)
            if count:
                log('etl fact recomputed', fact=fact, rows=count)
        for sql in DIMENSIONS:
            warehouse.execute(sql, since)
        warehouse.execute('INSERT INTO etl.runs VALUES (%s, now(), %s, %s, %s)', (run, staged, recomputed, skipped))
    log('etl run finished', rowsStaged=staged, factsRecomputed=recomputed, sourcesSkipped=skipped,
        durationMs=round((time.monotonic() - started) * 1000))
    return staged, recomputed, skipped


def connect_warehouse(env):
    import psycopg
    conn = psycopg.connect(host=env.get('WAREHOUSE_HOST', 'warehouse'), dbname='warehouse', user='postgres',
                           password=env['WAREHOUSE_PASSWORD'], connect_timeout=5, autocommit=True)
    conn.execute((Path(__file__).with_name('warehouse.sql')).read_text())
    if env.get('WAREHOUSE_READER_PASSWORD'):
        grant_reader(conn, env['WAREHOUSE_READER_PASSWORD'])
    return conn


def grant_reader(conn, password):
    """Grafana's login: it may read the star schema and the run log, and change nothing. Anonymous
    viewers can send its data source arbitrary SQL, so it must never be the superuser."""
    from psycopg import sql
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'grafana_reader'").fetchone()
    verb = sql.SQL('ALTER') if exists else sql.SQL('CREATE')
    conn.execute(sql.SQL('{} ROLE grafana_reader LOGIN PASSWORD {}').format(verb, sql.Literal(password)))
    conn.execute("""ALTER ROLE grafana_reader SET default_transaction_read_only = on;
GRANT USAGE ON SCHEMA public, etl TO grafana_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_reader;
GRANT SELECT ON etl.runs TO grafana_reader""")


def empty_warehouse(warehouse):
    tables = ['etl.staging', 'etl.watermarks', 'etl.runs', *FACTS, 'dim_date', 'dim_user', 'dim_package', 'dim_guild', 'dim_monster']
    warehouse.execute('TRUNCATE ' + ', '.join(tables))
    log('etl warehouse emptied for a full rebuild')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--full', action='store_true')
    mode.add_argument('--every', type=int, metavar='SECONDS')
    args = parser.parse_args(argv)
    env = os.environ
    while True:
        try:
            with connect_warehouse(env) as warehouse:
                if args.full:
                    empty_warehouse(warehouse)
                run_once(warehouse, env)
        except Exception as error:
            log('etl run failed', error=str(error).splitlines()[0][:200])
            if not args.every:
                return 1
        if not args.every:
            return 0
        time.sleep(args.every)


if __name__ == '__main__':
    sys.exit(main())
