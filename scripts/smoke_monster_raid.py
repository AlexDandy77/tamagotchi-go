#!/usr/bin/env python3
"""Exercise paired Monster Raid with real User Management tokens, Guild membership and Package Registry schedules, call its mutual TLS API as Package Registry, and read its Kafka start event, without printing credentials."""
import json
import subprocess
import time
import uuid
import lab
from smoke import request, USERS

RAIDS = 'http://127.0.0.1:8084'
NIGHT_OWLS = '22222222-2222-4222-8222-222222222222'
PRACTICE = '44444444-4444-4444-8444-444444444444'  # the raid of the schedule Package Registry seeds
FIXTURE = 'a6c9ad3a-3890-5750-adae-a8b0a30af066'  # the won raid whose reward User Management's seed pays
TOPIC = 'raid.started.v1'
ADMIN = '/run/secrets/kafka-admin.properties'
# Runs inside the Package Registry container, the only caller the internal routes allow, and
# calls Monster Raid's mutual TLS port with Registry's certificate.
INTERNAL_CALL = r'''
const https = require('https'), fs = require('fs'), crypto = require('crypto');
const [method, path, body] = process.argv.slice(2);
const req = https.request({
  host: 'monster-raid', port: 8443, method, path,
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

def internal(method, path, body):
    return json.loads(output('package-registry', 'node', '-', method, path, json.dumps(body), stdin=INTERNAL_CALL.encode()))

def kafka(tool, *args):
    return output('kafka', '/opt/kafka/bin/' + tool, '--bootstrap-server', 'kafka:9092', *args)

def start_events():
    count = int(kafka('kafka-get-offsets.sh', '--command-config', ADMIN, '--topic', TOPIC).rsplit(':', 1)[1])
    if count == 0:
        return []
    messages = kafka('kafka-console-consumer.sh', '--consumer.config', ADMIN, '--topic', TOPIC, '--from-beginning',
                     '--max-messages', str(count), '--timeout-ms', '10000')
    return [json.loads(line) for line in messages.splitlines() if line.startswith('{')]

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
    pet = str(uuid.uuid5(uuid.UUID(alice), 'primary'))
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

    # The internal API refuses the public port and answers Package Registry's certificate.
    start = {'scheduleId': PRACTICE, 'scheduleVersion': 1}
    request(RAIDS, 'PUT', '/internal/v1/raids/' + PRACTICE, start, key=key(), expected=401)
    replay = internal('PUT', '/internal/v1/raids/' + PRACTICE, start)
    assert replay['status'] == 200 and replay['body']['id'] == PRACTICE, f'Start replay answered {replay["status"]}'
    # An unknown schedule makes Monster Raid ask Package Registry over mutual TLS, which does not know it.
    unknown = str(uuid.uuid4())
    missing = internal('PUT', '/internal/v1/raids/' + unknown, {'scheduleId': unknown, 'scheduleVersion': 1})
    assert missing['status'] == 422 and missing['body']['error']['details'][0]['field'] == 'scheduleVersion', \
        f'Unknown schedule answered {missing["status"]}'

    # The seed queued the practice raid's start, which the relay published to Kafka.
    event = next((e for e in reversed(start_events()) if e['aggregateId'] == PRACTICE), None)
    assert event, 'No start event of the practice raid reached Kafka'
    assert event['producer'] == 'monster-raid' and set(event['data']['recipientIds']) == {alice, bob}
    print('Paired Monster Raid reads, joins and attacks, its Package Registry mutual TLS API and its Kafka start event passed.')

if __name__ == '__main__':
    main()
