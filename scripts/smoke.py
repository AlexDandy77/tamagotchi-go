#!/usr/bin/env python3
"""Exercise live Lab 1 APIs without printing credentials or tokens."""
import json
import time
import urllib.error
import urllib.request
import uuid
from lab import environment

USERS = 'http://127.0.0.1:8081'
BATTLE = 'http://127.0.0.1:8082'

def request(base, method, path, body=None, token=None, key=None, expected=200):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if key:
        headers['Idempotency-Key'] = key
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        status = response.status
        raw = response.read()
    if status != expected:
        raise AssertionError(f'{method} {path}: expected {expected}, got {status}')
    return json.loads(raw) if raw else None

def main():
    password = environment()['SEED_PASSWORD']
    sessions = {name: request(USERS, 'POST', '/v1/auth/login', {'email': name+'@demo.invalid', 'password': password}) for name in ('alice', 'bob')}
    alice, bob = sessions['alice'], sessions['bob']
    a, b = alice['accessToken'], bob['accessToken']
    request(USERS, 'GET', '/v1/users/me', token=a)
    request(USERS, 'GET', '/v1/users/me', expected=401)
    request(USERS, 'GET', '/v1/wallet', token=a, expected=404)
    key = str(uuid.uuid4())
    profile = {'username': 'alice'}
    first = request(USERS, 'PATCH', '/v1/users/me', profile, a, key)
    assert request(USERS, 'PATCH', '/v1/users/me', profile, a, key) == first
    request(USERS, 'PATCH', '/v1/users/me', {'username': 'changed'}, a, key, 409)
    request(USERS, 'DELETE', '/v1/friends/'+bob['user']['id'], token=a, key=str(uuid.uuid4()), expected=204)
    friend = request(USERS, 'POST', '/v1/friend-requests', {'recipientId': bob['user']['id']}, a, str(uuid.uuid4()), 201)
    request(USERS, 'PUT', '/v1/friend-requests/'+friend['id']+'/decision', {'decision': 'accept'}, b, str(uuid.uuid4()))
    assert request(USERS, 'GET', '/v1/relationships', token=a)['items']
    def loadout(user):
        return {'primaryPetId': str(uuid.uuid5(uuid.UUID(user), 'primary')), 'secondaryPetId': str(uuid.uuid5(uuid.UUID(user), 'secondary')), 'boostIds': []}
    challenge = request(BATTLE, 'POST', '/v1/battles', {'opponentId': bob['user']['id'], **loadout(alice['user']['id'])}, a, str(uuid.uuid4()), 201)
    path = '/v1/battles/'+challenge['id']
    request(BATTLE, 'POST', path+'/accept', loadout(bob['user']['id']), b, str(uuid.uuid4()), 202)
    for _ in range(30):
        state = request(BATTLE, 'GET', path, token=a)
        if state['status'] == 'active':
            break
        time.sleep(.2)
    assert state['status'] == 'active'
    request(BATTLE, 'POST', path+'/actions', {'action': 'attack', 'expectedTurn': 1}, b, str(uuid.uuid4()), 403)
    state = request(BATTLE, 'POST', path+'/actions', {'action': 'attack', 'expectedTurn': 1}, a, str(uuid.uuid4()))
    assert state['turn'] == 2
    request(BATTLE, 'POST', path+'/actions', {'action': 'forfeit', 'expectedTurn': 2}, b, str(uuid.uuid4()))
    for _ in range(30):
        state = request(BATTLE, 'GET', path, token=a)
        if state['status'] == 'finished':
            break
        time.sleep(.2)
    assert state['status'] == 'finished' and state['winnerId'] == alice['user']['id']
    print('Live authentication, friendship, battle, authorization and idempotency checks passed.')

if __name__ == '__main__':
    main()
