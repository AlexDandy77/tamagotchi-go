#!/usr/bin/env python3
"""Exercise live Guild and Package Registry APIs with real User Management tokens, and open Guild chat as
the Gateway negotiates it, without printing credentials."""
import uuid
from lab import environment
from smoke import LiveSocket, request, USERS

GATEWAY = 'http://127.0.0.1:8080'
GUILD = GATEWAY + '/services/guild'
REGISTRY = 'http://127.0.0.1:8080/services/package-registry'
PACKAGE = '11111111-1111-4111-8111-111111111111'

def key():
    return str(uuid.uuid4())

def open_chat(url, credential):
    """Opens the guild chat socket with credential in its first frame and returns the first frame
    the server sends, with the close code that follows a refusal."""
    with LiveSocket(url) as chat:
        chat.send({'type': 'authenticate', 'accessToken': credential})
        frame = chat.receive()
        if frame.get('type') == 'error':
            frame['closed'] = chat.receive().get('close')
    return frame

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

    # Chat: the Gateway checks membership and returns Guild's direct socket. With socket tickets, it adds a
    # single-use ticket, which the first frame carries instead of Bob's access token; the socket then
    # refuses the used ticket and the access token alike.
    tickets = environment().get('SOCKET_TICKETS_ENABLED', 'true').lower() == 'true'
    chat = request(GATEWAY, 'GET', f"/v1/realtime/guilds/{guild['id']}/connection", token=b)
    assert chat['url'].endswith(f"/v1/guilds/{guild['id']}/chat") and chat['authentication'] == 'ChatAuthenticate' \
        and bool(chat.get('ticket')) == tickets, f'Chat negotiation answered the fields {sorted(chat)}'
    first = open_chat(chat['url'], chat['ticket'] if tickets else b)
    assert (first.get('type'), first.get('guildId'), first.get('userId')) == ('authenticated', guild['id'], bob), \
        f'The chat socket answered {first.get("type")} {first.get("code")}'
    if tickets:
        for credential, what in ((chat['ticket'], 'A used ticket'), (b, 'An access token')):
            refusal = open_chat(chat['url'], credential)
            assert (refusal.get('type'), refusal.get('code'), refusal.get('closed')) == ('error', 'UNAUTHENTICATED', 1008), \
                f'{what} answered {refusal.get("type")} {refusal.get("code")} {refusal.get("closed")}'
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
    print('Live Guild membership, relationship checks, chat socket opened as negotiated, Registry projection and permission checks passed.')

if __name__ == '__main__':
    main()
