#!/usr/bin/env python3
"""Check all adopted services' registration and replica routing using safe reads.

Destructive checks are opt-in: --disruptive stops one Map replica and pauses one Raid
replica; --all-replicas-down [SERVICE] checks a complete managed pool outage; and
--restart-discovery checks that heartbeats rebuild the registry. Never prints credentials.
"""
import argparse
import collections
import concurrent.futures
import datetime
import json
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import lab
from smoke import LiveSocket, USERS, request

GATEWAY = 'http://127.0.0.1:8080'
MAP = GATEWAY + '/services/map'
RAIDS = GATEWAY + '/services/monster-raid'
NIGHT_OWLS = '22222222-2222-4222-8222-222222222222'
PRACTICE = '44444444-4444-4444-8444-444444444444'
REPLICAS = {name: 3 for name in ('map', 'monster-raid', 'tamagotchi', 'notification', 'user-management', 'battle')}
SAFE_READS = {
    'map': '/v1/map/nearby',
    'monster-raid': '/v1/raids?guildId=' + NIGHT_OWLS,
    'tamagotchi': '/v1/pets',
    'notification': '/v1/notifications?limit=20',
    'user-management': '/v1/users/me',
    'battle': '/v1/battles?limit=20',
}
SOCKET_PORTS = {8084, 8085, 8086}
PAUSED = 'monster-raid-3'


def read_url(service):
    return GATEWAY + '/services/' + service + SAFE_READS[service]


def call(url, token):
    """GET url and return its status and headers, including documented errors."""
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
    try:
        response = urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        response.read()
        return response.status, response.headers


def output(*args):
    return lab.compose(*args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode().strip()


def gateway_status():
    return request(GATEWAY, 'GET', '/status')


def discovery_status():
    # The registry has no published host port.
    read = "import sys, urllib.request; sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8080/status', timeout=5).read().decode())"
    return json.loads(output('exec', '-T', 'service-discovery', 'python', '-c', read))


def pool(service):
    return gateway_status()['pools'][service]


def wait(what, condition, seconds):
    deadline = time.monotonic() + seconds
    last_error = None
    while True:
        try:
            if condition():
                return
        except (OSError, subprocess.CalledProcessError, AssertionError, KeyError) as error:
            # Restart checks may briefly lose the container, HTTP response or pool entry.
            last_error = type(error).__name__
        if time.monotonic() >= deadline:
            detail = f' (last check: {last_error})' if last_error else ''
            raise AssertionError(f'{what} did not happen within {seconds} s{detail}')
        time.sleep(0.5)


def registered(service, count):
    entry = pool(service)
    return entry['source'] == 'service-discovery' and len(entry['instances']) == count


def spread(token, requests, service='map'):
    """Check that routed reads identify real replicas of the requested logical service."""
    served = collections.Counter()
    for _ in range(requests):
        status, headers = call(read_url(service), token)
        assert status == 200, f'{service} answered {status}'
        instance = headers.get('X-Served-By', '')
        assert instance.startswith(service + '/') and instance != service + '/static', \
            f'{service} did not identify a discovered replica'
        served[instance] += 1
    return served


def verify_spread(token, service, requests=30):
    expected = {service + '/' + instance for instance in pool(service)['instances']}
    assert len(expected) == REPLICAS[service], f'{service} has an unexpected pool'
    served = spread(token, requests, service)
    assert set(served) == expected, f'{service}: expected {sorted(expected)}, served {sorted(served)}'
    return served


def negotiate(token):
    req = urllib.request.Request(f'{GATEWAY}/v1/realtime/raids/{PRACTICE}/connection', headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.headers['X-Socket-Instance'], json.loads(response.read())


def authenticate(url, credential):
    with LiveSocket(url) as live:
        live.send({'type': 'raid.authenticate', 'accessToken': credential})
        frame = live.receive()
        if frame.get('type') == 'raid.error':
            frame['closed'] = live.receive().get('close')
    return frame


def sockets(token, tickets):
    offers = dict(negotiate(token) for _ in range(REPLICAS['monster-raid']))
    ports = {urlsplit(offer['url']).port for offer in offers.values()}
    assert ports == SOCKET_PORTS, f'Negotiation handed out the socket ports {sorted(ports)}'
    for replica, offer in offers.items():
        first = authenticate(offer['url'], offer['ticket'] if tickets else token)
        assert first.get('type') == 'raid.state', f'{replica} answered {first.get("type")} {first.get("code")}'
    if tickets:
        (_, used), (_, other) = list(offers.items())[:2]
        replay = authenticate(other['url'], used['ticket'])
        assert (replay.get('code'), replay.get('closed')) == ('UNAUTHENTICATED', 1008), \
            f'A ticket replayed on another replica answered {replay.get("type")} {replay.get("code")}'
    return sorted(offers)


def stop_one_map_replica(token):
    container = output('ps', '-q', 'map').split()[-1]
    try:
        lab.run(['docker', 'stop', '--time', '15', container], stdout=subprocess.DEVNULL)
        wait('the stopped Map replica leaving the pools', lambda: registered('map', REPLICAS['map'] - 1), 20)
        served = spread(token, 10)
    finally:
        lab.run(['docker', 'start', container], stdout=subprocess.DEVNULL)
    wait('the restarted Map replica rejoining the pools', lambda: registered('map', REPLICAS['map']), 60)
    return served


def pause_one_raid_replica(token, since):
    try:
        lab.compose('pause', PAUSED)
        reads = 3 * REPLICAS['monster-raid']
        statuses = []
        for _ in range(3):
            with concurrent.futures.ThreadPoolExecutor(reads) as executor:
                statuses += executor.map(lambda _: call(read_url('monster-raid'), token)[0], range(reads))
            if gateway_status()['circuits'].get('monster-raid/' + PAUSED) == 'open':
                break
        assert 504 in statuses, f'No read reached the paused replica: {statuses}'
        status = gateway_status()
        assert status['circuits'].get('monster-raid/' + PAUSED) == 'open', f'The breaker of {PAUSED} is not open'
        assert PAUSED in status['pools']['monster-raid'].get('ejected', []), f'{PAUSED} is still in the pool'
        offers = {negotiate(token)[0] for _ in range(REPLICAS['monster-raid'])}
        assert 'monster-raid/' + PAUSED not in offers, 'Negotiation still hands out the ejected replica'
        logs = output('logs', '--no-log-prefix', '--since', since, 'service-discovery')
        alerts = [json.loads(line) for line in logs.splitlines() if line.startswith('{')]
        assert any(a.get('alert') == 'instance.ejected' and a.get('targetInstance') == PAUSED for a in alerts), \
            'Service Discovery logged no ejection alert'
    finally:
        lab.compose('unpause', PAUSED)
    wait(f'{PAUSED} rejoining the pools', lambda: PAUSED in pool('monster-raid')['instances'], 120)


def containers(service):
    labels = [service] if service != 'monster-raid' else ['monster-raid', 'monster-raid-2', 'monster-raid-3']
    ids = output('ps', '-q', *labels).split()
    assert len(ids) == REPLICAS[service], f'{service}: expected {REPLICAS[service]} running containers, found {len(ids)}'
    return ids


def all_replicas_down(token, service):
    """An adopted zero-instance pool must not route through its static Compose DNS name."""
    ids = containers(service)
    try:
        lab.run(['docker', 'stop', '--time', '15', *ids], stdout=subprocess.DEVNULL)
        wait(f'all {service} replicas leaving the pools', lambda: not pool(service)['instances'], 60)
        assert pool(service)['source'] != 'static', f'{service}: managed empty pool fell back to static routing'
        status, _ = call(read_url(service), token)
        assert status == 503, f'{service}: empty pool answered {status}, expected 503'
        ready, _ = call(GATEWAY + '/readyz', token)
        assert ready == 503, f'Gateway readiness stayed {ready} with no {service} instances'
        if service == 'monster-raid':
            negotiation, _ = call(f'{GATEWAY}/v1/realtime/raids/{PRACTICE}/connection', token)
            assert negotiation == 503, 'Managed empty socket pool still negotiated a connection'
    finally:
        lab.run(['docker', 'start', *ids], stdout=subprocess.DEVNULL)
    wait(f'{service} replicas recovering', lambda: registered(service, REPLICAS[service]), 120)
    return verify_spread(token, service, 3 * REPLICAS[service])


def restart_discovery():
    before = {name: set(pool(name)['instances']) for name in REPLICAS}
    lab.compose('restart', 'service-discovery')
    wait('Discovery heartbeats rebuilding every pool',
         lambda: all(registered(name, count) for name, count in REPLICAS.items()), 60)
    for name, instances in before.items():
        assert set(pool(name)['instances']) == instances, f'{name}: instance identities changed after Discovery restart'
    wait('Discovery monitoring the rebuilt registry',
         lambda: discovery_status()['instances']['available'] >= sum(REPLICAS.values()) + 1, 60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disruptive', action='store_true', help='stop one Map replica and pause one Raid replica')
    parser.add_argument('--all-replicas-down', nargs='?', const='all', action='append', choices=['all', *REPLICAS], default=[],
                        help='stop/recover an entire managed pool; omitted service means all six, one at a time')
    parser.add_argument('--restart-discovery', action='store_true', help='restart the registry and verify heartbeat rebuilding')
    args = parser.parse_args()
    values = lab.environment()
    since = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    token = request(USERS, 'POST', '/v1/auth/login', {'email': 'alice@demo.invalid', 'password': values['SEED_PASSWORD']})['accessToken']
    for service, count in REPLICAS.items():
        wait(f'{count} {service} replicas registering', lambda: registered(service, count), 60)
    assert discovery_status()['instances']['available'] >= sum(REPLICAS.values()) + 1, 'Some adopted replicas are not available'
    for service in REPLICAS:
        print(service, 'requests served by', dict(sorted(verify_spread(token, service).items())))
    print('Raid sockets negotiated on', sockets(token, values.get('SOCKET_TICKETS_ENABLED', 'true').lower() == 'true'))
    if args.disruptive:
        print('With one Map replica stopped, served by', dict(sorted(stop_one_map_replica(token).items())))
        pause_one_raid_replica(token, since)
        print(f'The paused {PAUSED} left the pools and rejoined after it resumed.')
    outages = set(args.all_replicas_down)
    for service in REPLICAS if 'all' in outages else (name for name in REPLICAS if name in outages):
        print(service, 'recovered after complete outage:', dict(sorted(all_replicas_down(token, service).items())))
    if args.restart_discovery:
        restart_discovery()
        print('Discovery rebuilt every adopted pool from heartbeats after restart.')
    telegram = discovery_status()['telegram']
    print('Telegram alerts:', f"{telegram['sent']} sent, {telegram['failed']} failed" if telegram['enabled'] else 'disabled')
    print('Discovery registration, replica routing and socket checks passed.')


if __name__ == '__main__':
    main()
