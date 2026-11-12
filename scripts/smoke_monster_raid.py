#!/usr/bin/env python3
"""Exercise paired Monster Raid through the Gateway with real User Management tokens, Guild membership and Package Registry schedules, negotiate and open its live socket, call its mutual TLS API as Package Registry through the Gateway, check that it refuses direct REST, and read its start event from the Message Broker, without printing credentials.

The event is read through Gateway's admin route of the broker, so Alice must be a demo admin
(REGISTRY_ADMIN_USER_IDS, which setup fills and the broker shares). The seeded practice raid runs
for a limited time after seeding; on an older lab Alice, as Package Registry admin, schedules a new
Night Owls raid for the checks and Package Registry cancels it at the end."""
import datetime
import json
import subprocess
import time
import urllib.parse
import uuid
import lab
from smoke import LiveSocket, request, USERS

GATEWAY = 'http://127.0.0.1:8080'
RAIDS = GATEWAY + '/services/monster-raid'
DIRECT = 'http://127.0.0.1:8084'  # only the live socket and the probes answer here
PETS = 'http://127.0.0.1:8080/services/tamagotchi'
REGISTRY = GATEWAY + '/services/package-registry'
NIGHT_OWLS = '22222222-2222-4222-8222-222222222222'
PRACTICE = '44444444-4444-4444-8444-444444444444'  # the raid of the schedule Package Registry seeds
FIXTURE = 'a6c9ad3a-3890-5750-adae-a8b0a30af066'  # the won raid whose reward User Management's seed pays
BROKER = GATEWAY + '/services/message-broker'
TOPIC = 'raid.started.v1'
# Runs inside the Package Registry container, the only caller the internal routes allow, and
# calls a mutual TLS port, Monster Raid's own or the Gateway's, with Registry's certificate.
INTERNAL_CALL = r'''
const https = require('https'), fs = require('fs'), crypto = require('crypto');
const [method, path, body, host] = process.argv.slice(2);
const req = https.request({
  host, port: 8443, method, path,
  cert: fs.readFileSync('/run/tls/package-registry.pem'),
  key: fs.readFileSync('/run/tls/package-registry-key.pem'),
  ca: fs.readFileSync('/run/tls/ca.pem'),
  headers: {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
}, res => {
  let data = '';
  res.on('data', chunk => data += chunk);
  res.on('end', () => console.log(JSON.stringify({status: res.statusCode, body: data ? JSON.parse(data) : null})));
});
req.on('error', error => { console.error(error.message); process.exit(1); });
req.end(body);
'''

def key():
    return str(uuid.uuid4())

def output(*args, stdin=None):
    return lab.compose('exec', '-T', *args, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode().strip()

def internal(method, path, body, host='monster-raid'):
    # No body at all for None: Node sends a DELETE body without a length, which a server then
    # reads as the start of the next request.
    payload = '' if body is None else json.dumps(body)
    return json.loads(output('package-registry', 'node', '-', method, path, payload, host, stdin=INTERNAL_CALL.encode()))

def primary_pet(token):
    # Tamagotchi provisions the starter from the enrollment event, so it can lag a fresh start.
    for _ in range(20):
        pet = request(PETS, 'GET', '/v1/pets', token=token).get('primaryPetId')
        if pet:
            return pet
        time.sleep(0.5)
    raise AssertionError('Tamagotchi never provisioned a primary pet')

def start_events(token):
    """Every raid start the broker stores, read page by page as a demo admin."""
    events, start = [], 1
    while True:
        query = urllib.parse.urlencode({'fromOffset': start})
        page = request(BROKER, 'GET', f'/v1/topics/{TOPIC}/messages?{query}', token=token)
        if not page['items']:
            return events
        assert all(m['producer'] == 'monster-raid' for m in page['items']), 'The broker accepted a raid start from another service'
        events += [m['value'] if isinstance(m['value'], dict) else json.loads(m['value']) for m in page['items']]
        start = page['nextOffset']

def fresh_raid(token):
    """Schedules a new Night Owls raid as Package Registry admin and waits for Package Registry to
    start it. Returns the raid ID, which is the schedule's, and the schedule version."""
    monster = request(REGISTRY, 'POST', '/v1/monsters', {'name': 'Smoke target', 'description': '',
        'spriteUrls': ['https://example.invalid/monster.png'], 'maxHp': 1000, 'baseAttack': 0,
        'weaknesses': [], 'resistances': [], 'specialProperties': ['none']}, token, key(), 201)
    starts = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=3)
    schedule = request(REGISTRY, 'POST', '/v1/raid-schedules', {'guildId': NIGHT_OWLS, 'monsterId': monster['id'],
        'monsterVersion': monster['version'], 'startsAt': starts.isoformat(), 'durationSeconds': 300,
        'participantLimit': 2, 'rewards': {'globalCurrencyPerParticipant': 20, 'xpPerParticipant': 10}}, token, key(), 201)
    for _ in range(60):
        listed = request(RAIDS, 'GET', '/v1/raids?guildId=' + NIGHT_OWLS, token=token)['items']
        if any(r['id'] == schedule['id'] and r['status'] == 'active' for r in listed):
            return schedule['id'], schedule['version']
        time.sleep(0.5)
    raise AssertionError('Package Registry never started the scheduled raid')

def open_live(url, credential):
    """Opens the live raid socket with credential in its first frame and returns the first frame
    the server sends, with the close code that follows a refusal."""
    with LiveSocket(url) as live:
        live.send({'type': 'raid.authenticate', 'accessToken': credential})
        frame = live.receive()
        if frame.get('type') == 'raid.error':
            frame['closed'] = live.receive().get('close')
    return frame

def main():
    password = lab.environment()['SEED_PASSWORD']
    sessions = {name: request(USERS, 'POST', '/v1/auth/login', {'email': name + '@demo.invalid', 'password': password}) for name in ('alice', 'bob')}
    a = sessions['alice']['accessToken']
    request(RAIDS, 'GET', '/v1/raids?guildId=' + NIGHT_OWLS, expected=401)

    # The seeded raids, listed through real Guild membership. The practice raid is active only for
    # a while after seeding; a new raid from a fresh schedule stands in on an older lab.
    raids = {r['id']: r for r in request(RAIDS, 'GET', '/v1/raids?guildId=' + NIGHT_OWLS, token=a)['items']}
    assert raids.get(FIXTURE, {}).get('status') == 'won', 'The seeded won raid is missing'
    request(RAIDS, 'GET', f'/v1/raids?guildId={uuid.uuid4()}', token=a, expected=403)
    if raids.get(PRACTICE, {}).get('status') == 'active':
        raid, version, scheduled = PRACTICE, 1, False
    else:
        (raid, version), scheduled = fresh_raid(a), True
    try:
        exercise(sessions, raid, version)
    finally:
        if scheduled:
            cancelled = internal('DELETE', '/services/monster-raid/internal/v1/raids/' + raid, None, host='gateway')
    if scheduled:
        assert cancelled['status'] == 204, f'Package Registry could not cancel the scheduled raid: {cancelled["status"]}'
    print('Paired Monster Raid reads, joins and attacks through the Gateway, its live socket opened as negotiated, its Package Registry mutual TLS API through the Gateway, its refusal of direct REST and its start event on the Message Broker passed.')

def exercise(sessions, raid, version):
    """Joins, attacks, opens the live socket, checks the internal API and reads the start event
    of one active Night Owls raid."""
    a, b = sessions['alice']['accessToken'], sessions['bob']['accessToken']
    alice, bob = sessions['alice']['user']['id'], sessions['bob']['user']['id']

    # Alice joins once with her primary pet; the join reads her package's care rules from Package Registry.
    pet = primary_pet(a)
    current = request(RAIDS, 'GET', '/v1/raids/' + raid, token=a)
    if alice not in [p['userId'] for p in current['participants']]:
        request(RAIDS, 'POST', f'/v1/raids/{raid}/participants', {'primaryPetId': pet}, a, key(), 201)
    request(RAIDS, 'POST', f'/v1/raids/{raid}/participants', {'primaryPetId': pet}, a, key(), 409)

    # Damage is computed on the server, each member attacks at most once a second, and
    # members who did not join cannot attack. The pause lets an earlier run's cooldown pass.
    time.sleep(1)
    hp = request(RAIDS, 'GET', '/v1/raids/' + raid, token=a)['hp']
    assert request(RAIDS, 'POST', f'/v1/raids/{raid}/attacks', None, a, key())['hp'] < hp, 'The attack did no damage'
    request(RAIDS, 'POST', f'/v1/raids/{raid}/attacks', None, a, key(), 429)
    request(RAIDS, 'POST', f'/v1/raids/{raid}/attacks', None, b, key(), 403)

    # The Gateway checks that Alice may read the raid and hands back Monster Raid's direct socket.
    # With socket tickets, it adds a single-use ticket, which the first frame carries instead of
    # Alice's access token; the socket then refuses the used ticket and the access token alike.
    live = request(GATEWAY, 'GET', f'/v1/realtime/raids/{raid}/connection', token=a)
    tickets = lab.environment().get('SOCKET_TICKETS_ENABLED', 'true').lower() == 'true'
    assert live['url'].endswith(f'/v1/raids/{raid}/live') and live['authentication'] == 'RaidAuthenticate' \
        and bool(live.get('ticket')) == tickets, f'Raid negotiation answered the fields {sorted(live)}'
    first = open_live(live['url'], live['ticket'] if tickets else a)
    assert first.get('type') == 'raid.state' and first.get('raidId') == raid, \
        f'The live socket answered {first.get("type")} {first.get("code")}'
    if tickets:
        for credential, what in ((live['ticket'], 'A used ticket'), (a, 'An access token')):
            refusal = open_live(live['url'], credential)
            assert (refusal.get('type'), refusal.get('code'), refusal.get('closed')) == ('raid.error', 'UNAUTHENTICATED', 1008), \
                f'{what} answered {refusal.get("type")} {refusal.get("code")} {refusal.get("closed")}'

    # REST reaches Monster Raid only through the Gateway: a player calling the published port and
    # Package Registry calling the mutual TLS port directly are both refused.
    request(DIRECT, 'GET', '/v1/raids?guildId=' + NIGHT_OWLS, token=a, expected=401)
    start = {'scheduleId': raid, 'scheduleVersion': version}
    direct = internal('PUT', '/internal/v1/raids/' + raid, start)
    assert direct['status'] == 401 and direct['body']['error']['code'] == 'GATEWAY_REQUIRED', f'Direct start answered {direct["status"]}'

    # Internal routes need a service certificate, which the Gateway checks before it forwards
    # Package Registry's signed identity.
    request(RAIDS, 'PUT', '/internal/v1/raids/' + raid, start, key=key(), expected=401)
    replay = internal('PUT', '/services/monster-raid/internal/v1/raids/' + raid, start, host='gateway')
    assert replay['status'] == 200 and replay['body']['id'] == raid, f'Start replay answered {replay["status"]}'
    # An unknown schedule makes Monster Raid ask Package Registry over mutual TLS, which does not know it.
    unknown = str(uuid.uuid4())
    missing = internal('PUT', '/services/monster-raid/internal/v1/raids/' + unknown, {'scheduleId': unknown, 'scheduleVersion': 1}, host='gateway')
    assert missing['status'] == 422 and missing['body']['error']['details'][0]['field'] == 'scheduleVersion', \
        f'Unknown schedule answered {missing["status"]}'

    # Monster Raid's relay published the raid's start to the broker.
    event = next((e for e in reversed(start_events(a)) if e['aggregateId'] == raid), None)
    assert event, 'No start event of the raid reached the Message Broker'
    assert event['producer'] == 'monster-raid' and set(event['data']['recipientIds']) == {alice, bob}

if __name__ == '__main__':
    main()
