#!/usr/bin/env python3
"""Check shared User Management state through discovered replicas and public Gateway APIs.

Creates fresh test accounts and leaves their game data intact; never changes Alice/Bob.
Requires two healthy User Management instances, the active seeded package and local
SEED_PASSWORD. Prints replica names and verdicts, never credentials or response bodies.
"""
import collections
import json
import re
import time
import urllib.error
import urllib.request
import uuid

from lab import environment
from smoke import USERS, request

PACKAGE = '11111111-1111-4111-8111-111111111111'
MAX_ATTEMPTS = 12
TIMEOUT_SECONDS = 90


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Probe:
    def __init__(self):
        self.deadline = time.monotonic() + TIMEOUT_SECONDS
        self.observed = collections.defaultdict(set)
        self.opener = urllib.request.build_opener(NoRedirect)

    def call(self, label, method, path, body=None, token=None, key=None, expected=200):
        remaining = self.deadline - time.monotonic()
        assert remaining > 0, 'Replica-state check exceeded its 90-second deadline'
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if key:
            headers['Idempotency-Key'] = key
        request = urllib.request.Request(USERS + path, method=method, headers=headers,
                                         data=None if body is None else json.dumps(body).encode())
        try:
            response = self.opener.open(request, timeout=min(5, remaining))
        except urllib.error.HTTPError as error:
            response = error
        with response:
            assert response.status == expected, f'{label}: expected HTTP {expected}, got {response.status}'
            instance = response.headers.get('X-Served-By', '')
            assert re.fullmatch(r'user-management/[a-z0-9][a-z0-9-]{0,62}', instance) and instance != 'user-management/static', \
                f'{label}: Gateway did not identify a discovered User Management replica'
            self.observed[label].add(instance)
            self.last_instance = instance
            raw = response.read(1_048_577)
            assert len(raw) <= 1_048_576, f'{label}: unexpected response size'
        return json.loads(raw) if raw else None

    def across(self, label, method, path, check, **kwargs):
        """Repeat a bounded operation until the actual responses prove two replicas handled it."""
        result, seen = None, set()
        for _ in range(MAX_ATTEMPTS):
            result = self.call(label, method, path, **kwargs)
            assert check(result), f'{label}: shared state differs between replicas'
            seen.add(self.last_instance)
            if len(seen) >= 2:
                print('PASS:', label, 'on', ', '.join(sorted(seen)), flush=True)
                return result
        raise AssertionError(f'{label}: did not observe two replicas in {MAX_ATTEMPTS} requests')


def verify_session(session, owner=None):
    assert session['accessToken'] and session['refreshToken'], 'Session credentials missing'
    assert session['tokenType'] == 'Bearer', 'Unexpected token type'
    assert owner is None or session['user']['id'] == owner, 'Session belongs to a different account'
    return session


def main():
    probe = Probe()
    password = environment()['SEED_PASSWORD']
    sessions = []
    pool = request(USERS.split('/services/')[0], 'GET', '/status')['pools']['user-management']
    assert pool['source'] == 'service-discovery' and len(pool['instances']) >= 2, 'Start at least two registered healthy User Management replicas first'
    # Registration is not an idempotent route; never replay it expecting another 201.
    for _ in range(MAX_ATTEMPTS):
        name = 'replica_' + uuid.uuid4().hex[:16]
        session = verify_session(probe.call('signup', 'POST', '/v1/auth/register', {
            'username': name, 'email': name + '@demo.invalid', 'password': password,
            'packageId': PACKAGE,
        }, expected=201))
        sessions.append(session)
        if len(probe.observed['signup']) >= 2:
            break
    assert len(probe.observed['signup']) >= 2, 'Signup did not reach two discovered replicas'
    print('PASS: signup on', ', '.join(sorted(probe.observed['signup'])), flush=True)

    primary = sessions[0]
    owner = primary['user']['id']
    # Every replica-issued signup JWT must work on other replicas through Gateway.
    for session in sessions:
        user_id = session['user']['id']
        probe.across('profile', 'GET', '/v1/users/me', lambda user: user['id'] == user_id,
                     token=session['accessToken'])
    login = {'email': primary['user']['email'], 'password': password}
    login_sessions = []
    for _ in range(MAX_ATTEMPTS):
        current = verify_session(probe.call('login', 'POST', '/v1/auth/login', login), owner)
        sessions.append(current)
        login_sessions.append(current)
        if len(probe.observed['login']) >= 2:
            break
    assert len(probe.observed['login']) >= 2, 'Login did not reach two discovered replicas'
    print('PASS: login on', ', '.join(sorted(probe.observed['login'])), flush=True)
    for session in login_sessions:
        probe.across('login JWT profile', 'GET', '/v1/users/me', lambda user: user['id'] == owner,
                     token=session['accessToken'])

    # Rotate the same session chain on multiple replicas, consuming each previous token.
    consumed = []
    for _ in range(MAX_ATTEMPTS):
        previous = current['refreshToken']
        current = verify_session(probe.call('refresh', 'POST', '/v1/auth/refresh', {'refreshToken': previous}), owner)
        assert current['refreshToken'] != previous, 'Refresh did not rotate its token'
        sessions.append(current)
        consumed.append(previous)
        if len(probe.observed['refresh']) >= 2:
            break
    assert len(probe.observed['refresh']) >= 2, 'Refresh did not reach two discovered replicas'
    print('PASS: rotating shared refresh session on', ', '.join(sorted(probe.observed['refresh'])), flush=True)
    for previous in consumed:
        probe.call('consumed refresh', 'POST', '/v1/auth/refresh', {'refreshToken': previous}, expected=401)

    body, key = {'username': 'updated_' + uuid.uuid4().hex[:16]}, str(uuid.uuid4())
    first = probe.call('idempotent PATCH', 'PATCH', '/v1/users/me', body,
                       current['accessToken'], key)
    assert first['id'] == owner and first['username'] == body['username'], 'Profile update did not apply'
    probe.across('idempotent PATCH', 'PATCH', '/v1/users/me', lambda user: user == first,
                 body=body, token=current['accessToken'], key=key)
    probe.call('changed idempotency body', 'PATCH', '/v1/users/me',
               {'username': 'conflict_' + uuid.uuid4().hex[:16]}, current['accessToken'], key, expected=409)
    probe.across('updated profile', 'GET', '/v1/users/me', lambda user: user == first,
                 token=current['accessToken'])
    print('PASS: shared idempotency replay and changed-body conflict', flush=True)

    # Close all sessions created by this check, including already-consumed token handles.
    for session in sessions:
        probe.call('logout', 'POST', '/v1/auth/logout', {'refreshToken': session['refreshToken']}, expected=204)
    probe.across('revoked refresh', 'POST', '/v1/auth/refresh', lambda body: body['error']['code'] == 'INVALID_REFRESH',
                 body={'refreshToken': current['refreshToken']}, expected=401)
    print('PASS: shared signup, JWT/profile state, refresh rotation, idempotency and revocation; test accounts retained.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (AssertionError, KeyError, OSError, ValueError) as error:
        # Our assertions contain only operation labels/statuses, never API bodies or tokens.
        raise SystemExit('FAIL: ' + str(error))
