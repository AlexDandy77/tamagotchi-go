#!/usr/bin/env python3
"""Check cross-service APIs with fresh accounts; retain test data and report blockers.

Requires live User Management/Battle/Registry/Raid and Alice as a Registry admin.
Does not alter Alice/Bob's pets, balances or relationships. No secrets are printed.
"""
import datetime
import sys
import time
import uuid
from lab import environment
from smoke import request, USERS, BATTLE

PETS = 'http://127.0.0.1:8080/services/tamagotchi'
NOTIFICATIONS = 'http://127.0.0.1:8080/services/notification'
REGISTRY = 'http://127.0.0.1:8080/services/package-registry'
GUILD = 'http://127.0.0.1:8080/services/guild'
MAP = 'http://127.0.0.1:8080/services/map'
RAIDS = 'http://127.0.0.1:8080/services/monster-raid'
PACKAGE = '11111111-1111-4111-8111-111111111111'


def key():
    return str(uuid.uuid4())


def eventually(read, predicate, description, seconds=20):
    deadline = time.monotonic() + seconds
    while True:
        value = read()
        if predicate(value):
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(description)
        time.sleep(.5)


def create_battle_package(admin):
    package = request(REGISTRY, 'POST', '/v1/packages', {
        'name': 'Live battle ' + uuid.uuid4().hex[:12],
        'appVersion': '1.0.0',
        'description': 'Temporary package for the full live battle workflow',
    }, admin, key(), 201)
    request(REGISTRY, 'POST', f"/v1/packages/{package['id']}/configurations", {
        'expectedVersion': 0,
        'starter': {
            'name': 'Sparky',
            'combatType': 'electric',
            'spriteUrls': ['https://example.invalid/sparky.png'],
        },
        'statistics': {
            'energy': {
                'minimum': 0,
                'maximum': 100,
                'initial': 80,
                'changePerMinute': -1,
                'bonus': None,
            },
        },
        'careActions': {
            'charge': {
                'statChanges': {'energy': 20},
                'cooldownSeconds': 60,
                'xp': 5,
                'localCurrency': 1,
            },
        },
        'localCurrencyName': 'Volts',
    }, admin, key(), 201)
    package = request(REGISTRY, 'GET', f"/v1/packages/{package['id']}")
    request(REGISTRY, 'PATCH', f"/v1/packages/{package['id']}", {
        'expectedVersion': package['version'],
        'status': 'active',
    }, admin, key())
    return package['id']


def main():
    failures = []

    def check(name, action):
        try:
            action()
            print('PASS:', name, flush=True)
        except (AssertionError, OSError, KeyError) as error:
            failures.append(name)
            print('FAIL:', name, '-', error, flush=True)

    check('Gateway readiness', lambda: request('http://127.0.0.1:8080', 'GET', '/readyz'))
    password = environment()['SEED_PASSWORD']
    admin = request(USERS, 'POST', '/v1/auth/login', {'email': 'alice@demo.invalid', 'password': password})['accessToken']
    sessions = []
    for _ in range(2):
        name = 'live_' + uuid.uuid4().hex[:16]
        body = {'username': name, 'email': name + '@demo.invalid', 'password': password, 'packageId': PACKAGE}
        sessions.append(request(USERS, 'POST', '/v1/auth/register', body, key=key(), expected=201))
    a, b = (s['accessToken'] for s in sessions)
    alice, bob = (s['user']['id'] for s in sessions)
    print('PASS: fresh registration through real Package Registry', flush=True)
    rosters = [eventually(lambda token=t: request(PETS, 'GET', '/v1/pets', token=token), lambda x: len(x['pets']) > 0,
                          'starter pet was not provisioned') for t in (a, b)]
    print('PASS: starter provisioning into Tamagotchi', flush=True)

    battle_package = create_battle_package(admin)
    for token in (a, b):
        request(USERS, 'PUT', f'/v1/users/me/packages/{battle_package}', token=token, key=key())
    rosters = [eventually(lambda token=t: request(PETS, 'GET', '/v1/pets', token=token),
                          lambda x: len(x['pets']) >= 2 and bool(x['secondaryPetIds']),
                          'second package enrollment did not provision a second pet') for t in (a, b)]
    print('PASS: second package enrollment provisioned battle loadouts', flush=True)

    def projection():
        eventually(lambda: request(REGISTRY, 'GET', f'/v1/packages/{PACKAGE}/users?limit=100', token=admin),
                   lambda x: {alice, bob} <= {i['userId'] for i in x['items']}, 'enrollments missing from Registry')
    check('Kafka enrollment projection', projection)

    config = request(REGISTRY, 'GET', f'/v1/packages/{PACKAGE}/configurations/1')
    def starter_rules():
        pet = rosters[0]['pets'][0]
        assert pet['name'] == config['starter']['name'] and pet['packageStats'] == {
            n: v['initial'] for n, v in config['statistics'].items()
        }, 'Tamagotchi starter does not match Registry configuration'
    check('starter matches Registry rules', starter_rules)

    def notification(token, kind, target):
        return eventually(lambda: request(NOTIFICATIONS, 'GET', '/v1/notifications?limit=100', token=token),
                          lambda x: any(n['type'] == kind and n['targetId'] == target for n in x['items']),
                          f'{kind} notification not delivered')

    def friends():
        f = request(USERS, 'POST', '/v1/friend-requests', {'recipientId': bob}, a, key(), 201)
        notification(b, 'friend.requested.v1', f['id'])
        request(USERS, 'PUT', f"/v1/friend-requests/{f['id']}/decision", {'decision': 'accept'}, b, key())
    check('friend request → Kafka → Notification', friends)

    def mapping():
        at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for token, lat in ((a, 47.0617), (b, 47.061727)):
            request(MAP, 'PUT', '/v1/map/location', {'latitude': lat, 'longitude': 28.8683, 'accuracyMeters': 3, 'recordedAt': at}, token)
        entries = request(MAP, 'GET', '/v1/map/nearby', token=a)['items']
        assert any(e['user']['id'] == bob and e['relationship'] == 'friend' for e in entries), 'friend missing on map'
    check('Map → User Management relationships', mapping)

    guild = request(GUILD, 'POST', '/v1/guilds', {'name': 'Live integration', 'description': 'Isolated smoke-test guild'}, a, key(), 201)
    def invitation():
        invite = request(GUILD, 'POST', f"/v1/guilds/{guild['id']}/invitations", {'recipientId': bob}, a, key(), 201)
        request(GUILD, 'PUT', f"/v1/guild-invitations/{invite['id']}/decision", {'decision': 'accept'}, b, key())
        notification(b, 'guild.invited.v1', invite['id'])
    check('Guild membership and Kafka notification', invitation)

    def care():
        pet = rosters[0]['pets'][0]
        before = request(USERS, 'GET', '/v1/wallet', token=a)
        request(PETS, 'POST', f"/v1/pets/{pet['id']}/care", {'actionId': 'feed', 'expectedVersion': pet['version']}, a, key())
        eventually(lambda: request(USERS, 'GET', '/v1/wallet', token=a), lambda x: x != before,
                   'care succeeded but User Management wallet received no local currency', seconds=5)
    check('Tamagotchi care → User Management reward', care)

    def raid():
        monster = request(REGISTRY, 'POST', '/v1/monsters', {'name': 'Integration target', 'description': '',
            'spriteUrls': ['https://example.invalid/monster.png'], 'maxHp': 3, 'baseAttack': 0,
            'weaknesses': [], 'resistances': [], 'specialProperties': ['none']}, admin, key(), 201)
        starts = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=3)
        schedule = request(REGISTRY, 'POST', '/v1/raid-schedules', {'guildId': guild['id'], 'monsterId': monster['id'],
            'monsterVersion': monster['version'], 'startsAt': starts.isoformat(), 'durationSeconds': 300,
            'participantLimit': 2, 'rewards': {'globalCurrencyPerParticipant': 20, 'xpPerParticipant': 10}}, admin, key(), 201)
        path = '/v1/raids/' + schedule['id']
        eventually(lambda: request(RAIDS, 'GET', '/v1/raids?guildId='+guild['id'], token=a),
                   lambda x: any(r['id'] == schedule['id'] for r in x['items']), 'Registry did not dispatch raid')
        print('PASS: Registry → Monster Raid → Guild start', flush=True)
        before = [request(USERS, 'GET', '/v1/wallet', token=token)['globalBalance'] for token in (a, b)]
        for token, roster in zip((a, b), rosters):
            request(RAIDS, 'POST', path+'/participants', {'primaryPetId': roster['primaryPetId']}, token, key(), 201)
        request(RAIDS, 'POST', path+'/attacks', token=a, key=key())
        request(RAIDS, 'POST', path+'/attacks', token=b, key=key())
        eventually(lambda: request(RAIDS, 'GET', path, token=a), lambda x: x['status'] == 'won', 'raid settlement did not complete')
        for token, balance in zip((a, b), before):
            assert request(USERS, 'GET', '/v1/wallet', token=token)['globalBalance'] == balance + 20, 'raid wallet reward missing'
        notification(a, 'raid.finished.v1', schedule['id'])
    check('raid reservation, combat, pet/wallet settlement and notification', raid)

    def challenge():
        # A pending challenge stores IDs; ownership is checked only on acceptance.
        # Deliberately unused secondary ID: never accept this challenge.
        body = {'opponentId': bob, 'primaryPetId': rosters[0]['primaryPetId'], 'secondaryPetId': key(), 'boostIds': []}
        idem = key()
        battle = request(BATTLE, 'POST', '/v1/battles', body, a, idem, 201)
        try:
            assert request(BATTLE, 'POST', '/v1/battles', body, a, idem, 201)['id'] == battle['id']
            request(BATTLE, 'GET', '/v1/battles/'+battle['id'], expected=401)
            notification(b, 'battle.requested.v1', battle['id'])
        finally:
            request(BATTLE, 'DELETE', '/v1/battles/'+battle['id'], token=a, key=key(), expected=204)
        assert request(BATTLE, 'GET', '/v1/battles/'+battle['id'], token=a)['status'] == 'cancelled'
    check('Battle challenge, replay, cancellation and Kafka notification (no combat)', challenge)

    def battle():
        before = [request(USERS, 'GET', '/v1/wallet', token=token)['globalBalance'] for token in (a, b)]
        loadouts = [{
            'primaryPetId': roster['primaryPetId'],
            'secondaryPetId': roster['secondaryPetIds'][0],
            'boostIds': [],
        } for roster in rosters]
        created = request(BATTLE, 'POST', '/v1/battles', {'opponentId': bob, **loadouts[0]}, a, key(), 201)
        path = '/v1/battles/' + created['id']
        request(BATTLE, 'POST', path+'/accept', loadouts[1], b, key(), 202)
        state = eventually(lambda: request(BATTLE, 'GET', path, token=a),
                           lambda x: x['status'] == 'active', 'battle did not become active')
        state = request(BATTLE, 'POST', path+'/actions', {'action': 'attack', 'expectedTurn': state['turn']}, a, key())
        request(BATTLE, 'POST', path+'/actions', {'action': 'forfeit', 'expectedTurn': state['turn']}, b, key())
        finished = eventually(lambda: request(BATTLE, 'GET', path, token=a),
                              lambda x: x['status'] == 'finished', 'battle settlement did not finish')
        assert finished['winnerId'] == alice, 'challenger did not win after opponent forfeited'
        assert request(USERS, 'GET', '/v1/wallet', token=a)['globalBalance'] == before[0] + 10, 'winner stake missing'
        assert request(USERS, 'GET', '/v1/wallet', token=b)['globalBalance'] == before[1] - 10, 'loser stake missing'
        captured = eventually(lambda: request(PETS, 'GET', '/v1/pets', token=a),
                              lambda x: any(p['id'] == rosters[1]['primaryPetId'] for p in x['pets']),
                              'battle winner did not receive the captured pet')
        assert rosters[1]['primaryPetId'] in captured['secondaryPetIds'], 'captured pet is not a secondary pet'
        notification(a, 'battle.finished.v1', created['id'])
        notification(b, 'battle.finished.v1', created['id'])
    check('live Battle pet/wallet settlement and notification', battle)

    check('Tamagotchi rejects invalid JWTs', lambda: request(PETS, 'GET', '/v1/pets', token='not-a-jwt', expected=401))
    check('Notification rejects invalid JWTs', lambda: request(NOTIFICATIONS, 'GET', '/v1/notifications', token='not-a-jwt', expected=401))
    print(f'{len(failures)} integration checks failed; test accounts and records retained.', flush=True)
    return bool(failures)


if __name__ == '__main__':
    sys.exit(main())
