#!/usr/bin/env python3
"""Lab 5 live demo, one section per grade, for what one request at a time can't show.

    python3 scripts/demo_lab5.py grade4   ETL: a care action reaches the warehouse
    python3 scripts/demo_lab5.py grade5   stop the primary under writes; time the failover
    python3 scripts/demo_lab5.py grade6   the same GET twice: MISS (database), then HIT
    python3 scripts/demo_lab5.py grade7   consistent hashing: stop a key's node, only its keys move
    python3 scripts/demo_lab5.py grade8   a raid join as a two-phase commit: commit, a no vote, a stopped coordinator
    python3 scripts/demo_lab5.py grade9   a battle saga started as Battle, followed step by step
    python3 scripts/demo_lab5.py all

Run it while the lab is up (python3 scripts/lab.py up). It prints PASS/FAIL lines and the
headers and log lines to point at; it never prints secrets.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lab  # noqa: E402
from smoke import USERS, request  # noqa: E402
from smoke_monster_raid import fresh_raid  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PETS = 'http://127.0.0.1:8080/services/tamagotchi'
ADMIN = 'http://127.0.0.1:8080/services/message-broker'
GUILDS = 'http://127.0.0.1:8080/services/guild'
RAIDS = 'http://127.0.0.1:8080/services/monster-raid'
MONSTER_RAIDS = ('monster-raid', 'monster-raid-2', 'monster-raid-3')
RESULTS = {'passed': 0, 'failed': 0}
CACHE_NODES = ('cache-1', 'cache-2', 'cache-3')


def check(name, ok, detail=''):
    RESULTS['passed' if ok else 'failed'] += 1
    print(('PASS: ' if ok else 'FAIL: ') + name + (f' - {detail}' if detail else ''), flush=True)
    return ok


def section(title):
    print(f'\n== {title} ==', flush=True)


def compose(*args):
    return subprocess.run(['docker', 'compose', *args], cwd=ROOT, text=True, capture_output=True)


_token = {}


def token():
    if 'alice' not in _token:
        login = request(USERS, 'POST', '/v1/auth/login',
                        {'email': 'alice@demo.invalid', 'password': lab.environment()['SEED_PASSWORD']})
        _token['alice'] = login['accessToken']
    return _token['alice']


def http(method, url, body=None, key=False, bearer=None, retry=True):
    headers = {'Authorization': 'Bearer ' + (bearer or token()), 'Content-Type': 'application/json'}
    if key:
        headers['Idempotency-Key'] = str(uuid.uuid4())
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, response.headers, json.loads(response.read() or b'null')
    except urllib.error.HTTPError as error:
        if error.code == 401 and bearer is None and retry:
            _token.clear()  # access tokens last 15 minutes: log in again once
            return http(method, url, body, key, bearer, retry=False)
        return error.code, error.headers, error.read()


def fresh_player():
    """A new account with its starter pet, so the demo never depends on what earlier battles captured."""
    name = 'lab5demo' + uuid.uuid4().hex[:10]
    session = request(USERS, 'POST', '/v1/auth/register',
                      {'username': name, 'email': name + '@demo.invalid', 'password': lab.environment()['SEED_PASSWORD'],
                       'packageId': '11111111-1111-4111-8111-111111111111'}, expected=201)
    bearer = session['accessToken']
    for _ in range(60):  # the starter pet arrives through user.package-registered.v1
        roster = http('GET', PETS + '/v1/pets', bearer=bearer)[2]
        if roster.get('pets'):
            return bearer, roster
        time.sleep(1)
    raise RuntimeError('the new player received no starter pet within 60 s')


def show(status, headers):
    return (f"{status}  X-Cache={headers.get('X-Cache')}  X-Cache-Node={headers.get('X-Cache-Node')}  "
            f"X-Served-By={headers.get('X-Served-By')}")


def logs(service, since, message):
    output = compose('logs', service, '--since', since, '--no-log-prefix').stdout
    found = []
    for line in output.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if str(entry.get('msg', '')).startswith(message):
            found.append(entry)
    return found


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# -- the ring, exactly as contracts/cache.md defines it -------------------------------------


def position(value):
    return int.from_bytes(hashlib.md5(value.encode()).digest()[:4], 'big')


RING = sorted((position(f'{node}#{i}'), node) for node in CACHE_NODES for i in range(160))


def node_for(key, down=()):
    target = position(key)
    start = next((i for i, (p, _) in enumerate(RING) if p >= target), 0)
    for step in range(len(RING)):
        node = RING[(start + step) % len(RING)][1]
        if node not in down:
            return node
    return None


# -- grades ------------------------------------------------------------------------------------


def grade4():
    section('Grade 4: the ETL copies a care action into the warehouse')

    def facts():
        result = compose('exec', '-T', 'warehouse', 'psql', '-U', 'postgres', '-d', 'warehouse', '-Atc',
                         'SELECT count(*) FROM fact_care_action')
        return int(result.stdout.strip() or 0) if result.returncode == 0 else 0

    lab.compose('run', '--rm', '--no-deps', '--pull', 'never', 'etl', '--once', stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=False)
    before = facts()
    bearer, roster = fresh_player()
    pet = roster['pets'][0]
    cared = False
    for action in ('feed', 'play', 'rest'):
        status, _, body = http('POST', f"{PETS}/v1/pets/{pet['id']}/care", {'actionId': action, 'expectedVersion': pet['version']},
                               key=True, bearer=bearer)
        if status == 200:
            cared = True
            print(f'   cared for {pet["name"]} with "{action}"')
            break
    check('a care action was recorded', cared, 'every action is cooling down; try again in a minute' if not cared else '')
    run = subprocess.run([sys.executable, str(ROOT / 'scripts/lab.py'), 'etl'], cwd=ROOT, text=True, capture_output=True)
    lines = [json.loads(line) for line in run.stdout.splitlines() if line.startswith('{')]
    for entry in lines:
        if entry['msg'] in ('etl view loaded', 'etl run finished') and (entry.get('rows') or entry['msg'] == 'etl run finished'):
            print('   etl log:', {k: v for k, v in entry.items() if k not in ('time', 'level', 'service')})
    after = facts()
    check('the warehouse has the new care action', after > before, f'fact_care_action {before} -> {after}')
    print('   Grafana: http://localhost:3000, dashboard "Game analytics (Lab 5 warehouse)"; warehouse on localhost:5434')


PROBE = r"""
end=$(( $(date +%s) + SECONDS_TO_RUN ))
while [ $(date +%s) -lt $end ]; do
  if psql -h postgres -U postgres -qAtc "INSERT INTO lab5_failover_probe DEFAULT VALUES" >/dev/null 2>&1; then
    echo "$(date +%s.%N) ok"
  else
    echo "$(date +%s.%N) fail"
  fi
  sleep 0.5
done
"""


def grade5():
    section('Grade 5: stop the primary while writes continue')
    nodes = json.loads(compose('exec', '-T', 'postgres-1', 'patronictl', 'list', '-f', 'json').stdout or '[]')
    leader = next((n['Member'] for n in nodes if n['Role'] == 'Leader'), None)
    if not check('the cluster has a leader', leader is not None):
        return
    print(f'   primary now: {leader}; replicas: {", ".join(n["Member"] for n in nodes if n["Role"] != "Leader")}')
    lab.psql('CREATE TABLE IF NOT EXISTS lab5_failover_probe (at timestamptz DEFAULT now())', timeout=10)
    # One long-lived client writes every 0.5 s through postgres:5432 (HAProxy) and timestamps each
    # attempt, so the measurement is the database's outage, not container start-up time.
    env = {**os.environ, 'PGPASSWORD': lab.environment()['POSTGRES_PASSWORD'], 'PGCONNECT_TIMEOUT': '2',
           'PGOPTIONS': '-c statement_timeout=3000'}
    probe = subprocess.Popen(
        ['docker', 'run', '--rm', '-i', '--network', f'{lab.PROJECT}_default', '-e', 'PGPASSWORD', '-e', 'PGCONNECT_TIMEOUT',
         '-e', 'PGOPTIONS', lab.TOOLS_IMAGE, 'bash', '-c', PROBE.replace('SECONDS_TO_RUN', '75')],
        stdout=subprocess.PIPE, text=True, env=env)
    time.sleep(6)
    stopped_at = time.time()
    print(f'   stopping {leader} (as the Stop button in Docker Desktop does)')
    compose('stop', leader)
    output, _ = probe.communicate(timeout=120)
    results = [(float(line.split()[0]), line.split()[1] == 'ok') for line in output.splitlines() if line.strip()]
    first_fail = next((t for t, ok in results if not ok and t >= stopped_at - 1), None)
    recovered = next((t for t, ok in results if ok and first_fail is not None and t > first_fail), None)
    nodes = json.loads(compose('exec', '-T', next(n for n in lab.POSTGRES_NODES if n != leader), 'patronictl', 'list', '-f', 'json').stdout or '[]')
    new_leader = next((n['Member'] for n in nodes if n['Role'] == 'Leader'), None)
    check('a replica was promoted', new_leader not in (None, leader), f'new primary: {new_leader}')
    if first_fail is None:
        check('writes kept working', bool(results) and results[-1][1], f'{len(results)} writes, none failed')
    else:
        failed = sum(1 for t, ok in results if not ok)
        check('writes resumed after the failover', recovered is not None,
              f'{len(results)} writes, {failed} failed; writes unavailable for {recovered - first_fail:.1f} s' if recovered
              else f'{len(results)} writes, {failed} failed, never recovered in 75 s')
    compose('start', leader)
    try:
        lab.psql('DROP TABLE IF EXISTS lab5_failover_probe', timeout=10)
    except subprocess.CalledProcessError:
        print('   (could not drop the probe table now; it is harmless)')
    print(f'   {leader} restarted; it rejoins as a replica (python3 scripts/lab.py db-status)')


def grade6():
    section('Grade 6: the first read goes to the database, the second is a cache hit')
    # A new player's pet has never been read, so its first read is sure to miss.
    bearer, roster = fresh_player()
    pet = PETS + '/v1/pets/' + roster['pets'][0]['id']
    since = now()
    first = http('GET', pet, bearer=bearer)
    second = http('GET', pet, bearer=bearer)
    print('   1st:', show(first[0], first[1]))
    print('   2nd:', show(second[0], second[1]))
    check('MISS, then HIT on the same node', first[1].get('X-Cache') == 'MISS' and second[1].get('X-Cache') == 'HIT'
          and first[1].get('X-Cache-Node') == second[1].get('X-Cache-Node'))
    missed = logs('tamagotchi', since, 'cache miss') + logs('tamagotchi', since, 'cache hit')
    for entry in missed[:2]:
        print(f"   log {entry['instanceId']}: {entry['msg']} key={entry['key']} node={entry['node']}")
    check('the service logged it at info level', bool(missed))


def grade7():
    section('Grade 7: consistent hashing over cache-1..3')
    keys = [f'demo:{i}' for i in range(1000)]
    for down in CACHE_NODES:
        moved = sum(1 for k in keys if node_for(k) != node_for(k, (down,)))
        own = sum(1 for k in keys if node_for(k) == down)
        print(f'   without {down}: {moved} of 1000 keys move, exactly the {own} it held')
    status, _, me = http('GET', USERS + '/v1/users/me')
    if not check('Alice can read her profile', status == 200, f'GET /v1/users/me answered {status}'):
        return
    user_id = me['id']
    key = f'tamagotchi:v1:pets-of:{user_id}'
    home, fallback = node_for(key), node_for(key, (node_for(key),))
    status, headers, _ = http('GET', PETS + '/v1/pets')
    check(f'Alice\'s roster lives where the ring says ({home})', headers.get('X-Cache-Node') == home, show(status, headers))
    other_key, other_node = 'tamagotchi:v1:pet-types', node_for('tamagotchi:v1:pet-types')
    http('GET', PETS + '/v1/pet-types')
    print(f'   stopping {home}')
    compose('stop', home)
    try:
        time.sleep(2)
        # Each replica skips a node after 3 failures within 10 s; until then it answers BYPASS
        # (read from the database). Keep asking until the replicas have all moved on.
        results = []
        for _ in range(40):
            results.append(http('GET', PETS + '/v1/pets'))
            if [r[1].get('X-Cache-Node') for r in results[-4:]] == [fallback] * 4:
                break
        bypassed = sum(1 for r in results if r[1].get('X-Cache') == 'BYPASS')
        print(f'   {bypassed} requests answered BYPASS (database) while the replicas noticed {home} was gone')
        for status, headers, _ in results[-2:]:
            print('   ', show(status, headers))
        check('every request still answers 200', all(r[0] == 200 for r in results))
        check(f'the key moved to the next node on the ring ({fallback}) and hits there',
              results[-1][1].get('X-Cache-Node') == fallback and results[-1][1].get('X-Cache') == 'HIT')
        if other_node != home:
            status, headers, _ = http('GET', PETS + '/v1/pet-types')
            check(f'a key on {other_node} did not move and still hits', headers.get('X-Cache-Node') == other_node
                  and headers.get('X-Cache') == 'HIT', f'{other_key}: ' + show(status, headers))
    finally:
        compose('start', home)
        print(f'   {home} started again; services flush it before using it ("cache node flushed after an outage")')


INSIDE_SAGA = r'''
import json, os, sys, time, grpc
sys.path.insert(0, '/app')
from broker.gen import broker_pb2 as pb, broker_pb2_grpc as pb_grpc
read = lambda name: open(f'/run/tls/{name}', 'rb').read()
credentials = grpc.ssl_channel_credentials(read('ca.pem'), read('battle-key.pem'), read('battle.pem'))
channel = grpc.secure_channel('message-broker:50051', credentials)
stub = pb_grpc.SagaStub(channel)
saga_id = os.environ['SAGA_ID']
state = stub.Start(pb.StartSagaRequest(type='battle', saga_id=saga_id, input=os.environ['SAGA_INPUT'].encode()))
for _ in range(120):
    state = stub.Get(pb.GetSagaRequest(saga_id=saga_id))
    if state.state in ('waiting', 'compensated', 'completed'):
        break
    time.sleep(1)
print(json.dumps({'state': state.state, 'reason': state.failure_reason,
                  'steps': [[s.name, s.status, s.attempts, s.last_http_status] for s in state.steps]}))
'''


def grade9():
    section('Grade 9: a battle saga coordinated by the Message Broker')
    # The image the broker is running now (it may be a local build), not what Compose would pull.
    image = compose('ps', 'message-broker', '--format', '{{.Image}}').stdout.strip().splitlines()[0]
    saga_id = str(uuid.uuid4())
    bearer, roster = fresh_player()
    me = http('GET', USERS + '/v1/users/me', bearer=bearer)[2]['id']
    primary = roster.get('primaryPetId') or roster['pets'][0]['id']
    # Started as Battle would: hold both stakes, then reserve the pets. A real battle has two players.
    data = {'hold': {'participants': [{'userId': me, 'boostIds': []}], 'rulesVersion': 1},
            'pets': {'kind': 'battle', 'users': [{'userId': me, 'primaryPetId': primary, 'secondaryPetId': None}]}}
    since = now()
    command = ['docker', 'run', '--rm', '-i', '--network', f'{lab.PROJECT}_default', '-v',
               f"{ROOT / '.secrets' / 'tls'}:/run/tls:ro", '-e', f'SAGA_ID={saga_id}', '-e', f'SAGA_INPUT={json.dumps(data)}',
               '--entrypoint', 'python']
    if hasattr(os, 'getuid'):
        command += ['--user', f'{os.getuid()}:{os.getgid()}']
    run = subprocess.run(command + [image, '-'], input=INSIDE_SAGA, text=True, capture_output=True)
    if not check('Saga.Start accepted the battle saga (as the battle certificate)', run.returncode == 0,
                 run.stderr.strip().splitlines()[-1] if run.returncode and run.stderr else ''):
        return
    result = json.loads(run.stdout.strip().splitlines()[-1])
    print(f"   saga {saga_id}: {result['state']}" + (f" ({result['reason']})" if result['reason'] else ''))
    for name, status, attempts, http_status in result['steps']:
        print(f'     {name:15} {status:12} attempts={attempts} last={http_status or "-"}')
    attempted = [st for st in result['steps'] if st[0] in ('reserve-stakes', 'reserve-pets') and st[2] > 0]
    released = all(st[1] in ('compensated', 'done') for st in attempted)
    if result['state'] in ('compensating', 'compensated'):
        check('every attempted reservation was released', released and all(st[1] == 'compensated' for st in attempted),
              ', '.join(f'{st[0]}={st[1]}' for st in attempted))
        if result['state'] == 'compensating':
            print('   The broker keeps retrying the "cancelled" outcome on Battle (PUT /internal/v1/battle-outcomes):')
            print('   it completes once Battle serves its Lab 5 saga routes (MIGRATION.md in the broker repository).')
    else:
        check('the saga reached a resting state', result['state'] in ('waiting', 'completed'), result['state'])
    for entry in logs('message-broker', since, 'saga')[:8]:
        print(f"   broker log: {entry['msg']} step={entry.get('step', '-')} status={entry.get('status', '-')}")
    status, _, body = http('GET', f'{ADMIN}/v1/sagas/{saga_id}')
    check('GET /services/message-broker/v1/sagas/{id} shows it', status == 200 and body.get('sagaId') == saga_id)

    # The saga is stored before every step, so a restarted broker carries on where it was.
    print('   restarting the message broker mid-saga')
    since = now()
    compose('restart', 'message-broker')
    for _ in range(60):
        status, _, body = http('GET', f'{ADMIN}/v1/sagas/{saga_id}')
        if status == 200:
            break
        time.sleep(1)
    check('the saga survived the restart', status == 200 and body.get('state') in (result['state'], 'waiting', 'completed', 'compensated'),
          f"before {result['state']}, after {body.get('state') if status == 200 else status}")
    if result['state'] in ('started', 'settling', 'compensating'):
        resumed = [e for e in logs('message-broker', since, 'saga resumed after restart') if e.get('sagaId') == saga_id]
        check('the broker resumed it from its last recorded step', bool(resumed),
              f"step {resumed[0].get('step')}" if resumed else 'no "saga resumed after restart" line')


# -- grade 8 helpers -----------------------------------------------------------------------


def joins(since, message, user_id):
    """Monster Raid's log lines starting with message about user_id, with the replica that wrote each."""
    return [(service, entry) for service in MONSTER_RAIDS for entry in logs(service, since, message)
            if entry.get('userId') == user_id]


def waiting(find, seconds):
    """find() until it returns something, for up to seconds: log lines take a moment to appear."""
    deadline = time.monotonic() + seconds
    while True:
        found = find()
        if found or time.monotonic() > deadline:
            return found
        time.sleep(1)


def prepared():
    """The raid join's prepared transactions. Both databases are on the same PostgreSQL cluster,
    and pg_prepared_xacts lists every database's."""
    rows = lab.psql("SELECT database || ' ' || gid FROM pg_prepared_xacts WHERE gid LIKE 'monster-raid-join:%' "
                    "OR gid LIKE 'pet-reservation:%' ORDER BY prepared", capture=True, timeout=10)
    return rows.splitlines() if rows else []


def join(raid, bearer, pet):
    return http('POST', f'{RAIDS}/v1/raids/{raid}/participants', {'primaryPetId': pet}, key=True, bearer=bearer)


def error_code(body):
    try:
        return json.loads(body)['error']['code'] if isinstance(body, bytes) else ''
    except (ValueError, KeyError, TypeError):
        return ''


def participants(raid):
    status, _, body = http('GET', f'{RAIDS}/v1/raids/{raid}')
    return {p['userId'] for p in body['participants']} if status == 200 else set()


def monster_raid_pause(seconds):
    """Recreates the Monster Raid replicas with JOIN_PREPARED_PAUSE_SECONDS (empty: none), which
    also starts a stopped one, and waits until the raid API answers through the broker again."""
    env = {**os.environ, 'MONSTER_RAID_JOIN_PREPARED_PAUSE_SECONDS': str(seconds or '')}
    subprocess.run(['docker', 'compose', 'up', '-d', '--no-deps', '--wait', '--pull', 'never', *MONSTER_RAIDS],
                   cwd=ROOT, env=env, text=True, capture_output=True, check=True)
    waiting(lambda: http('GET', f'{RAIDS}/v1/raids?guildId=00000000-0000-4000-8000-000000000000')[0] in (200, 403), 60)


def join_mode():
    container = compose('ps', '-q', 'monster-raid').stdout.strip()
    env = subprocess.run(['docker', 'inspect', '-f', '{{json .Config.Env}}', container], text=True, capture_output=True).stdout
    return next((v.split('=', 1)[1] for v in json.loads(env or '[]') if v.startswith('JOIN_COMMIT=')), '')


def grade8():
    section('Grade 8: joining a raid is a two-phase commit over Monster Raid and Tamagotchi')
    mode = join_mode()
    if not check('Monster Raid runs JOIN_COMMIT=two-phase', mode == 'two-phase',
                 '' if mode == 'two-phase' else f'it runs {mode or "compensation"}; set MONSTER_RAID_JOIN_COMMIT=two-phase in .env and run python3 scripts/lab.py up'):
        return
    # Alice, the Registry admin, leads a new guild and schedules its raids; two new players join
    # them, so the demo never depends on pets that earlier runs reserved.
    (first_bearer, first_roster), (second_bearer, second_roster) = fresh_player(), fresh_player()
    players = []
    for bearer, roster in ((first_bearer, first_roster), (second_bearer, second_roster)):
        user = http('GET', USERS + '/v1/users/me', bearer=bearer)[2]['id']
        players.append((bearer, user, roster.get('primaryPetId') or roster['pets'][0]['id']))
    guild = http('POST', GUILDS + '/v1/guilds', {'name': 'Two-phase demo', 'description': ''}, key=True)[2]['id']
    for bearer, user, _ in players:
        invitation = http('POST', f'{GUILDS}/v1/guilds/{guild}/invitations', {'recipientId': user}, key=True)[2]['id']
        http('PUT', f'{GUILDS}/v1/guild-invitations/{invitation}/decision', {'decision': 'accept'}, key=True, bearer=bearer)
    first_raid, second_raid = fresh_raid(token(), guild)[0], fresh_raid(token(), guild)[0]
    (bearer, user, pet), (other_bearer, other_user, other_pet) = players

    print('\n   1. Both vote yes: the join commits on both databases')
    since = now()
    status, _, body = join(first_raid, bearer, pet)
    check('the join answered 201', status == 201, f'{status} {body}' if status != 201 else '')
    committed = waiting(lambda: joins(since, 'join transaction committed', user), 10)
    reservation = committed[0][1]['reservationId'] if committed else None
    for service, entry in joins(since, 'join transaction', user):
        print(f"   {service}: {entry['msg']} transactionId={entry['transactionId']} reservationId={entry['reservationId']}")
    check('Monster Raid prepared, then committed it', bool(committed))
    pets = waiting(lambda: [e for e in logs('tamagotchi', since, 'pet reservation') if e.get('activityId') == reservation], 5) \
        if reservation else []
    for entry in sorted(pets, key=lambda e: e.get('time', 0)):
        print(f"   tamagotchi {entry.get('instanceId', '')}: {entry['msg']} activityId={entry['activityId']}")
    check('Tamagotchi prepared, then committed the same reservation',
          {'pet reservation prepared', 'pet reservation committed'} <= {e['msg'] for e in pets})
    check('no prepared transaction is left', not prepared())

    print('\n   2. Tamagotchi votes no: the same pet for a second raid, which nothing changes')
    since = now()
    status, _, body = join(second_raid, bearer, pet)
    check('the join is refused with 409', status == 409, error_code(body) if status == 409 else f'{status} {body}')
    aborted = waiting(lambda: joins(since, 'join transaction aborted', user), 10)
    if aborted:
        print(f"   {aborted[0][0]}: join transaction aborted, reason: {aborted[0][1].get('reason')}")
    check('Monster Raid aborted it after the no vote', bool(aborted) and 'voted no' in aborted[0][1].get('reason', ''))
    check('the player is not in the second raid', user not in participants(second_raid))
    check('no prepared transaction is left', not prepared())

    print('\n   3. The coordinator stops between the phases')
    pause = 30
    print(f'   recreating the Monster Raid replicas with JOIN_PREPARED_PAUSE_SECONDS={pause}: each join waits after both votes')
    monster_raid_pause(pause)
    answer = {}

    def joining():  # the player's request, which the stopped coordinator never answers itself
        try:
            status, _, body = join(second_raid, other_bearer, other_pet)
            answer['status'] = f'{status} {error_code(body)}'.strip()
        except OSError as error:
            answer['status'] = type(error).__name__

    try:
        since = now()
        request_thread = threading.Thread(target=joining, daemon=True)
        request_thread.start()
        voted = waiting(lambda: joins(since, 'join transaction prepared', other_user), 20)
        if not check('both sides voted yes and prepared', bool(voted)):
            return
        coordinator, entry = voted[0]
        transaction, reservation = entry['transactionId'], entry['reservationId']
        held = prepared()
        for row in held:
            print('   pg_prepared_xacts:', row)
        check('each database holds its prepared transaction',
              any(r.endswith('monster-raid-join:' + transaction) for r in held) and any(r.endswith('pet-reservation:' + reservation) for r in held))
        # A retry with a new Idempotency-Key, as after a timeout: it must not touch the first join,
        # whose prepared reservation at Tamagotchi carries the same activityId.
        status, _, body = join(second_raid, other_bearer, other_pet)
        check('a second join meanwhile is refused and leaves the first one prepared',
              status == 409 and error_code(body) == 'JOIN_IN_PROGRESS' and set(prepared()) >= set(held),
              f'{status} {error_code(body)}')
        print(f'   stopping {coordinator}, the coordinator, before it records a decision')
        compose('stop', coordinator)
        request_thread.join(30)
        print(f"   the player's request ended with {answer.get('status', 'no answer yet')}")
        recovered = waiting(lambda: [e for _, e in joins(since, 'join transaction recovered', other_user)
                                     if e.get('transactionId') == transaction], 45)
        if recovered:
            print(f"   {recovered[0].get('instanceId')}: join transaction recovered, decision {recovered[0].get('decision')}")
        check('another replica aborted the undecided transaction', bool(recovered) and recovered[0].get('decision') == 'abort')
        check('both prepared transactions were rolled back', waiting(lambda: not prepared(), 10))
        check('the player is not in the raid', other_user not in participants(second_raid))
    finally:
        print('   recreating the Monster Raid replicas without the pause')
        monster_raid_pause(None)
    status, _, body = join(second_raid, other_bearer, other_pet)
    check('the player joins normally now: the abort left nothing held', status == 201, f'{status} {body}' if status != 201 else '')


GRADES = {'grade4': grade4, 'grade5': grade5, 'grade6': grade6, 'grade7': grade7, 'grade8': grade8, 'grade9': grade9}


def main(argv):
    if len(argv) != 1 or argv[0] not in (*GRADES, 'all'):
        sys.exit(__doc__)
    os.environ.setdefault('PYTHONUTF8', '1')
    token()
    for name in (GRADES if argv[0] == 'all' else [argv[0]]):
        _token.clear()  # a fresh login per grade: a long grade must not leave an expired token behind
        try:
            GRADES[name]()
        except Exception as error:  # report and continue with the next grade
            check(f'{name} ran to the end', False, f'{type(error).__name__}: {error}')
    print(f"\n{RESULTS['passed']} checks passed, {RESULTS['failed']} failed")
    sys.exit(1 if RESULTS['failed'] else 0)


if __name__ == '__main__':
    main(sys.argv[1:])
