#!/usr/bin/env python3
"""Exercise live Lab 1 APIs without printing credentials or tokens."""
import base64
import hashlib
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit
from lab import environment

USERS = 'http://127.0.0.1:8080/services/user-management'
BATTLE = 'http://127.0.0.1:8080/services/battle'

def request(base, method, path, body=None, token=None, key=None, expected=200, extra=None):
    headers = {'Content-Type': 'application/json', **(extra or {})}
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

class LiveSocket:
    """A minimal ws:// client for the negotiated sockets: it sends and reads JSON text frames,
    reads close frames and answers pings. Never print the credentials it carries."""
    ACCEPT_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

    def __init__(self, url, timeout=5):
        parts = urlsplit(url)
        if parts.scheme != 'ws':
            raise AssertionError(f'expected a ws:// socket, got {parts.scheme}://')
        self.sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f'GET {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n'
                           f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        head = b''
        while not head.endswith(b'\r\n\r\n'):
            head += self._exactly(1)
        status, *fields = head.decode('latin-1').split('\r\n')
        headers = {name.strip().lower(): value.strip() for name, _, value in (f.partition(':') for f in fields if f)}
        accept = base64.b64encode(hashlib.sha1((key + self.ACCEPT_GUID).encode()).digest()).decode()
        if status.split(' ')[1:2] != ['101'] or headers.get('sec-websocket-accept') != accept:
            self.sock.close()
            raise AssertionError(f'Socket upgrade answered {status}')

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _exactly(self, n):
        data = b''
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise AssertionError('The socket closed without a close frame')
            data += chunk
        return data

    def _send(self, opcode, payload):
        # Client frames are masked; servers send whole, unmasked frames for these small messages.
        mask = os.urandom(4)
        size = len(payload)
        if size < 126:
            header = struct.pack('!BB', 0x80 | opcode, 0x80 | size)
        elif size < 1 << 16:
            header = struct.pack('!BBH', 0x80 | opcode, 0x80 | 126, size)
        else:
            header = struct.pack('!BBQ', 0x80 | opcode, 0x80 | 127, size)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, message):
        self._send(0x1, json.dumps(message).encode())

    def receive(self):
        """Returns the next text frame as JSON, or {'close': code} for a close frame."""
        while True:
            first, second = self._exactly(2)
            opcode, size = first & 0x0F, second & 0x7F
            if size == 126:
                size, = struct.unpack('!H', self._exactly(2))
            elif size == 127:
                size, = struct.unpack('!Q', self._exactly(8))
            payload = self._exactly(size)
            if opcode == 0x1:
                return json.loads(payload)
            if opcode == 0x8:
                return {'close': struct.unpack('!H', payload[:2])[0] if len(payload) >= 2 else None}
            if opcode == 0x9:
                self._send(0xA, payload)

    def close(self):
        try:
            self._send(0x8, struct.pack('!H', 1000))
        except OSError:
            pass
        self.sock.close()

def main():
    password = environment()['SEED_PASSWORD']
    sessions = {name: request(USERS, 'POST', '/v1/auth/login', {'email': name+'@demo.invalid', 'password': password}) for name in ('alice', 'bob')}
    alice, bob = sessions['alice'], sessions['bob']
    a, b = alice['accessToken'], bob['accessToken']
    request(USERS, 'GET', '/v1/users/me', token=a)
    request(USERS, 'GET', '/v1/users/me', expected=401)
    before = request(USERS, 'GET', '/v1/wallet', token=a)
    assert before['globalAvailable'] >= 10
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
    after = request(USERS, 'GET', '/v1/wallet', token=a)
    assert after['globalBalance'] == before['globalBalance'] + 10
    assert after['globalAvailable'] == after['globalBalance']
    print('Live authentication, friendship, battle, authorization and idempotency checks passed.')

if __name__ == '__main__':
    main()
