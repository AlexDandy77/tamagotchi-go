#!/usr/bin/env python3
"""Exercise live Guild and Package Registry APIs with real User Management tokens, without printing credentials."""
import uuid
from lab import environment
from smoke import request, USERS

GUILD = 'http://127.0.0.1:8087'
REGISTRY = 'http://127.0.0.1:8088'
PACKAGE = '11111111-1111-4111-8111-111111111111'

def key():
    return str(uuid.uuid4())

def main():
    password = environment()['SEED_PASSWORD']
    sessions = {name: request(USERS, 'POST', '/v1/auth/login', {'email': name + '@demo.invalid', 'password': password}) for name in ('alice', 'bob')}
    a, b = sessions['alice']['accessToken'], sessions['bob']['accessToken']
    alice, bob = sessions['alice']['user']['id'], sessions['bob']['user']['id']

    # Guild: membership flow; the invitation asks User Management for relationships over mutual TLS.
    guild = request(GUILD, 'POST', '/v1/guilds', {'name': 'Smoke', 'description': ''}, a, key(), 201)
    invitation = request(GUILD, 'POST', f"/v1/guilds/{guild['id']}/invitations", {'recipientId': bob}, a, key(), 201)
    request(GUILD, 'PUT', f"/v1/guild-invitations/{invitation['id']}/decision", {'decision': 'accept'}, b, key())
    assert request(GUILD, 'GET', f"/v1/guilds/{guild['id']}", token=b)['memberCount'] == 2
    request(GUILD, 'GET', f"/v1/guilds/{guild['id']}", expected=401)
    # An enemy mark in User Management blocks invitations.
    other = request(GUILD, 'POST', '/v1/guilds', {'name': 'Smoke enemies', 'description': ''}, a, key(), 201)
    request(USERS, 'PUT', '/v1/enemies/' + alice, token=b, key=key())
    try:
        request(GUILD, 'POST', f"/v1/guilds/{other['id']}/invitations", {'recipientId': bob}, a, key(), 422)
    finally:
        request(USERS, 'DELETE', '/v1/enemies/' + alice, token=b, key=key(), expected=204)

    # Registry: public reads, the enrollment projection fed by User Management events, and admin/moderator rules.
    assert request(REGISTRY, 'GET', '/v1/combat-rules/1')['stake'] == 10
    enrolled = {e['userId'] for e in request(REGISTRY, 'GET', f'/v1/packages/{PACKAGE}/users', token=a)['items']}
    assert {alice, bob} <= enrolled, 'User Management enrollments are not projected yet'
    request(REGISTRY, 'POST', '/v1/packages', {'name': 'Smoke', 'appVersion': '1', 'description': ''}, b, key(), 403)
    package = request(REGISTRY, 'POST', '/v1/packages', {'name': 'Smoke', 'appVersion': '1', 'description': ''}, a, key(), 201)
    request(REGISTRY, 'PATCH', f"/v1/packages/{package['id']}", {'expectedVersion': 1, 'status': 'active'}, a, key(), 422)
    print('Live Guild membership, relationship checks, Registry projection and permission checks passed.')

if __name__ == '__main__':
    main()
