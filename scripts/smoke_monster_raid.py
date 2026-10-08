#!/usr/bin/env python3
"""Exercise paired Monster Raid through the Gateway with real User Management tokens, Guild membership and Package Registry schedules, negotiate and open its live socket, call its mutual TLS API as Package Registry through the Gateway, check that it refuses direct REST, and read its Kafka start event, without printing credentials."""
import json
import subprocess
import time
import uuid
import lab
from smoke import LiveSocket, request, USERS

GATEWAY = 'http://127.0.0.1:8080'
RAIDS = GATEWAY + '/services/monster-raid'
DIRECT = 'http://127.0.0.1:8084'  # only the live socket and the probes answer here
PETS = 'http://127.0.0.1:8080/services/tamagotchi'
NIGHT_OWLS = '22222222-2222-4222-8222-222222222222'
PRACTICE = '44444444-4444-4444-8444-444444444444'  # the raid of the schedule Package Registry seeds
FIXTURE = 'a6c9ad3a-3890-5750-adae-a8b0a30af066'  # the won raid whose reward User Management's seed pays
TOPIC = 'raid.started.v1'
ADMIN = '/run/secrets/kafka-admin.properties'
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
    return json.loads(output('package-registry', 'node', '-', method, path, json.dumps(body), host, stdin=INTERNAL_CALL.encode()))

def primary_pet(token):
    # Tamagotchi provisions the starter from the enrollment event, so it can lag a fresh start.
    for _ in range(20):
        pet = request(PETS, 'GET', '/v1/pets', token=token).get('primaryPetId')
        if pet:
            return pet
        time.sleep(0.5)
    raise AssertionError('Tamagotchi never provisioned a primary pet')

def kafka(tool, *args):
    return output('kafka', '/opt/kafka/bin/' + tool, '--bootstrap-server', 'kafka:9092', *args)

def start_events():
    count = int(kafka('kafka-get-offsets.sh', '--command-config', ADMIN, '--topic', TOPIC).rsplit(':', 1)[1])
    if count == 0:
        return []
    messages = kafka('kafka-console-consumer.sh', '--consumer.config', ADMIN, '--topic', TOPIC, '--from-beginning',
                     '--max-messages', str(count), '--timeout-ms', '10000')
    return [json.loads(line) for line in messages.splitlines() if line.startswith('{')]

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
    a, b = sessions['alice']['accessToken'], sessions['bob']['accessToken']
    alice, bob = sessions['alice']['user']['id'], sessions['bob']['user']['id']
    request(RAIDS, 'GET', '/v1/raids?guildId=' + NIGHT_OWLS, expected=401)

    # The seeded raids, listed through real Guild membership.
    raids = {r['id']: r for r in request(RAIDS, 'GET', '/v1/raids?guildId=' + NIGHT_OWLS, token=a)['items']}
    assert raids[PRACTICE]['status'] == 'active' and raids[FIXTURE]['status'] == 'won', 'The seeded raids are missing'
    request(RAIDS, 'GET', f'/v1/raids?guildId={uuid.uuid4()}', token=a, expected=403)

    # Alice joins once with her primary pet; the join reads her package's care rules from Package Registry.
    pet = primary_pet(a)
    practice = request(RAIDS, 'GET', '/v1/raids/' + PRACTICE, token=a)
    if alice not in [p['userId'] for p in practice['participants']]:
        request(RAIDS, 'POST', f'/v1/raids/{PRACTICE}/participants', {'primaryPetId': pet}, a, key(), 201)
    request(RAIDS, 'POST', f'/v1/raids/{PRACTICE}/participants', {'primaryPetId': pet}, a, key(), 409)

    # Damage is computed on the server, each member attacks at most once a second, and
    # members who did not join cannot attack. The pause lets an earlier run's cooldown pass.
    time.sleep(1)
    hp = request(RAIDS, 'GET', '/v1/raids/' + PRACTICE, token=a)['hp']
    assert request(RAIDS, 'POST', f'/v1/raids/{PRACTICE}/attacks', None, a, key())['hp'] < hp, 'The attack did no damage'
    request(RAIDS, 'POST', f'/v1/raids/{PRACTICE}/attacks', None, a, key(), 429)
    request(RAIDS, 'POST', f'/v1/raids/{PRACTICE}/attacks', None, b, key(), 403)

    # The Gateway checks that Alice may read the raid and hands back Monster Raid's direct socket.
    # With socket tickets, it adds a single-use ticket, which the first frame carries instead of
    # Alice's access token; the socket then refuses the used ticket and the access token alike.
    live = request(GATEWAY, 'GET', f'/v1/realtime/raids/{PRACTICE}/connection', token=a)
    tickets = lab.environment().get('SOCKET_TICKETS_ENABLED', 'true').lower() == 'true'
    assert live['url'].endswith(f'/v1/raids/{PRACTICE}/live') and live['authentication'] == 'RaidAuthenticate' \
        and bool(live.get('ticket')) == tickets, f'Raid negotiation answered the fields {sorted(live)}'
    first = open_live(live['url'], live['ticket'] if tickets else a)
    assert first.get('type') == 'raid.state' and first.get('raidId') == PRACTICE, \
        f'The live socket answered {first.get("type")} {first.get("code")}'
    if tickets:
        for credential, what in ((live['ticket'], 'A used ticket'), (a, 'An access token')):
            refusal = open_live(live['url'], credential)
            assert (refusal.get('type'), refusal.get('code'), refusal.get('closed')) == ('raid.error', 'UNAUTHENTICATED', 1008), \
                f'{what} answered {refusal.get("type")} {refusal.get("code")} {refusal.get("closed")}'

    # REST reaches Monster Raid only through the Gateway: a player calling the published port and
    # Package Registry calling the mutual TLS port directly are both refused.
    request(DIRECT, 'GET', '/v1/raids?guildId=' + NIGHT_OWLS, token=a, expected=401)
    start = {'scheduleId': PRACTICE, 'scheduleVersion': 1}
    direct = internal('PUT', '/internal/v1/raids/' + PRACTICE, start)
    assert direct['status'] == 401 and direct['body']['error']['code'] == 'GATEWAY_REQUIRED', f'Direct start answered {direct["status"]}'

    # Internal routes need a service certificate, which the Gateway checks before it forwards
    # Package Registry's signed identity.
    request(RAIDS, 'PUT', '/internal/v1/raids/' + PRACTICE, start, key=key(), expected=401)
    replay = internal('PUT', '/services/monster-raid/internal/v1/raids/' + PRACTICE, start, host='gateway')
    assert replay['status'] == 200 and replay['body']['id'] == PRACTICE, f'Start replay answered {replay["status"]}'
    # An unknown schedule makes Monster Raid ask Package Registry over mutual TLS, which does not know it.
    unknown = str(uuid.uuid4())
    missing = internal('PUT', '/services/monster-raid/internal/v1/raids/' + unknown, {'scheduleId': unknown, 'scheduleVersion': 1}, host='gateway')
    assert missing['status'] == 422 and missing['body']['error']['details'][0]['field'] == 'scheduleVersion', \
        f'Unknown schedule answered {missing["status"]}'

    # The seed queued the practice raid's start, which the relay published to Kafka.
    event = next((e for e in reversed(start_events()) if e['aggregateId'] == PRACTICE), None)
    assert event, 'No start event of the practice raid reached Kafka'
    assert event['producer'] == 'monster-raid' and set(event['data']['recipientIds']) == {alice, bob}
    print('Paired Monster Raid reads, joins and attacks through the Gateway, its live socket opened as negotiated, its Package Registry mutual TLS API through the Gateway, its refusal of direct REST and its Kafka start event passed.')

if __name__ == '__main__':
    main()
