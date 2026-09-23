#!/usr/bin/env python3
"""Exercise live Map with real User Management tokens and relationships and read its Kafka encounter event, without printing credentials."""
import datetime
import json
import subprocess
import time
import uuid
import lab
from smoke import request, USERS

MAP = 'http://127.0.0.1:8083'
TOPIC = 'map.encountered.v1'
ADMIN = '/run/secrets/kafka-admin.properties'
HERE = {'latitude': 47.0617, 'longitude': 28.8683}
NEAR = {'latitude': 47.061727, 'longitude': 28.8683}  # about 3 m north
FAR = {'latitude': 47.0717, 'longitude': 28.8683}  # about 1.1 km north

def key():
    return str(uuid.uuid4())

def report(position, token):
    # Map accepts reports up to 30 s ahead of its clock; 5 s keeps each one newer than earlier
    # reports and clears even when the Docker clock runs slightly ahead of the host.
    at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=5)
    body = {**position, 'accuracyMeters': 3, 'recordedAt': at.isoformat().replace('+00:00', 'Z')}
    assert request(MAP, 'PUT', '/v1/map/location', body, token)['accepted'], 'Map ignored a current report'

def output(*args):
    return lab.compose('exec', '-T', *args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode().strip()

def kafka(tool, *args):
    return output('kafka', '/opt/kafka/bin/' + tool, '--bootstrap-server', 'kafka:9092', *args)

def wait_for_empty_outbox():
    for _ in range(40):
        if output('postgres', 'psql', '-U', 'postgres', '-d', 'locations', '-At', '-c', 'SELECT count(*) FROM outbox') == '0':
            return
        time.sleep(.25)
    raise AssertionError('Map did not publish its outbox')

def on_map(token, user):
    return next((e for e in request(MAP, 'GET', '/v1/map/nearby', token=token)['items'] if e['user']['id'] == user), None)

def main():
    password = lab.environment()['SEED_PASSWORD']
    sessions = {name: request(USERS, 'POST', '/v1/auth/login', {'email': name + '@demo.invalid', 'password': password}) for name in ('alice', 'bob')}
    a, b = sessions['alice']['accessToken'], sessions['bob']['accessToken']
    alice, bob = sessions['alice']['user']['id'], sessions['bob']['user']['id']
    request(MAP, 'GET', '/v1/map/nearby', expected=401)

    # Strangers in User Management: no friendship and no enemy mark in either direction.
    request(USERS, 'DELETE', '/v1/friends/' + bob, token=a, key=key(), expected=204)
    request(USERS, 'DELETE', '/v1/enemies/' + bob, token=a, key=key(), expected=204)
    request(USERS, 'DELETE', '/v1/enemies/' + alice, token=b, key=key(), expected=204)
    try:
        # Bob walks away first, so coming back within 6 meters starts a new encounter.
        report(FAR, b)
        report(HERE, a)
        wait_for_empty_outbox()
        offset = kafka('kafka-get-offsets.sh', '--command-config', ADMIN, '--topic', TOPIC).rsplit(':', 1)[1]
        report(NEAR, b)
        entry = on_map(a, bob)
        assert entry and entry['relationship'] == 'unknown' and entry['distanceMeters'] <= 6 and not entry['stale']
        wait_for_empty_outbox()
        # The relay deletes an outbox row only after Kafka acknowledges it.
        message = kafka('kafka-console-consumer.sh', '--consumer.config', ADMIN, '--topic', TOPIC,
                        '--partition', '0', '--offset', offset, '--max-messages', '1', '--timeout-ms', '10000')
        assert message, 'No encounter event reached Kafka'
        event = json.loads(message)
        assert event['type'] == TOPIC and event['producer'] == 'map'
        assert set(event['data']['userIds']) == {alice, bob} and event['data']['distanceMeters'] <= 6
    finally:
        # Restore the seeded friendship.
        friend = request(USERS, 'POST', '/v1/friend-requests', {'recipientId': bob}, a, key(), 201)
        request(USERS, 'PUT', f"/v1/friend-requests/{friend['id']}/decision", {'decision': 'accept'}, b, key())
    entry = on_map(a, bob)
    assert entry and entry['relationship'] == 'friend'
    print('Live Map reports, User Management relationships and the Kafka encounter event passed.')

if __name__ == '__main__':
    main()
