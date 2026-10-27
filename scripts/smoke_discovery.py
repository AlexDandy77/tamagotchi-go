#!/usr/bin/env python3
"""Check Lab 3 scaling on the running lab: Service Discovery lists every replica, the Gateway
spreads Map requests and raid sockets across them, and a socket ticket used on one replica is
refused on the others. With --disruptive it also stops one Map replica, which leaves the pools
and rejoins when it starts again, and pauses one Monster Raid replica until the Gateway's
circuit breaker ejects it. Never prints credentials."""
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
# The replicas compose.yaml runs; Monster Raid's publish these socket ports.
REPLICAS = {'map': 3, 'monster-raid': 3}
SOCKET_PORTS = {8084, 8085, 8086}
PAUSED = 'monster-raid-3'

def call(url, token):
    """GET url and return its status and headers, whatever the status."""
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
    # Service Discovery publishes no host port; read its status from inside its container.
    read = "import sys, urllib.request; sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8080/status', timeout=5).read().decode())"
    return json.loads(output('exec', '-T', 'service-discovery', 'python', '-c', read))

def pool(service):
    return gateway_status()['pools'][service]

def wait(what, condition, seconds):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f'{what} did not happen within {seconds} s')
        time.sleep(0.5)

def registered(service, count):
    entry = pool(service)
    return entry['source'] == 'service-discovery' and len(entry['instances']) == count

def spread(token, requests):
    served = collections.Counter()
    for _ in range(requests):
        status, headers = call(MAP + '/v1/map/nearby', token)
        assert status == 200, f'Map answered {status}'
        served[headers['X-Served-By']] += 1
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
    """Negotiate once per replica: each negotiation names the next replica's own socket port,
    and the ticket it hands out opens that socket but no other."""
    offers = dict(negotiate(token) for _ in range(REPLICAS['monster-raid']))
    ports = {urlsplit(offer['url']).port for offer in offers.values()}
    assert ports == SOCKET_PORTS, f'Negotiation handed out the socket ports {sorted(ports)}'
    for replica, offer in offers.items():
        first = authenticate(offer['url'], offer['ticket'] if tickets else token)
        assert first.get('type') == 'raid.state', f'{replica} answered {first.get("type")} {first.get("code")}'
    if tickets:
        # Used tickets are recorded in the database every replica shares.
        (_, used), (_, other) = list(offers.items())[:2]
        replay = authenticate(other['url'], used['ticket'])
        assert (replay.get('code'), replay.get('closed')) == ('UNAUTHENTICATED', 1008), \
            f'A ticket replayed on another replica answered {replay.get("type")} {replay.get("code")}'
    return sorted(offers)

def stop_one_map_replica(token):
    container = output('ps', '-q', 'map').split()[-1]
    lab.run(['docker', 'stop', container], stdout=subprocess.DEVNULL)
    try:
        # A replica that stops deregisters, so it leaves the Gateway's pools on its next refresh.
        wait('the stopped Map replica leaving the pools', lambda: registered('map', REPLICAS['map'] - 1), 20)
        served = spread(token, 10)
    finally:
        lab.run(['docker', 'start', container], stdout=subprocess.DEVNULL)
    wait('the restarted Map replica rejoining the pools', lambda: registered('map', REPLICAS['map']), 60)
    return served

def pause_one_raid_replica(token, since):
    lab.compose('pause', PAUSED)
    try:
        # Round robin sends a third of these reads to the paused replica; three of them
        # outlive the task deadline within one breaker window and trip its breaker.
        reads = 3 * REPLICAS['monster-raid']
        with concurrent.futures.ThreadPoolExecutor(reads) as executor:
            statuses = list(executor.map(lambda _: call(f'{RAIDS}/v1/raids?guildId={NIGHT_OWLS}', token)[0], range(reads)))
        assert 504 in statuses, f'No read reached the paused replica: {statuses}'
        status = gateway_status()
        assert status['circuits'].get('monster-raid/' + PAUSED) == 'open', f'The breaker of {PAUSED} is {status["circuits"].get("monster-raid/" + PAUSED)}'
        assert PAUSED in status['pools']['monster-raid'].get('ejected', []), f'{PAUSED} is still in the pool'
        offers = {negotiate(token)[0] for _ in range(REPLICAS['monster-raid'])}
        assert 'monster-raid/' + PAUSED not in offers, 'Negotiation still hands out the ejected replica'
        logs = output('logs', '--no-log-prefix', '--since', since, 'service-discovery')
        alerts = [json.loads(line) for line in logs.splitlines() if line.startswith('{')]
        assert any(a.get('alert') == 'instance.ejected' and a.get('targetInstance') == PAUSED for a in alerts), \
            'Service Discovery logged no ejection alert'
    finally:
        lab.compose('unpause', PAUSED)
    # The Gateway keeps it out for one breaker window and Service Discovery until it passes its quarantine.
    wait(f'{PAUSED} rejoining the pools', lambda: PAUSED in pool('monster-raid')['instances'], 120)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disruptive', action='store_true', help='stop one Map replica and pause one Monster Raid replica')
    args = parser.parse_args()
    values = lab.environment()
    since = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    token = request(USERS, 'POST', '/v1/auth/login', {'email': 'alice@demo.invalid', 'password': values['SEED_PASSWORD']})['accessToken']

    for service, count in REPLICAS.items():
        wait(f'{count} {service} replicas registering', lambda: registered(service, count), 60)
    # Every Map and Monster Raid replica and the Gateway itself register.
    available = discovery_status()['instances']['available']
    assert available >= sum(REPLICAS.values()) + 1, f'Service Discovery lists {available} available instances'

    served = spread(token, 30)
    assert len(served) == REPLICAS['map'], f'Only {sorted(served)} served Map'
    replicas = sockets(token, values.get('SOCKET_TICKETS_ENABLED', 'true').lower() == 'true')
    print('Map requests served by', dict(sorted(served.items())))
    print('Raid sockets negotiated on', replicas)

    if args.disruptive:
        print('With one Map replica stopped, served by', dict(sorted(stop_one_map_replica(token).items())))
        pause_one_raid_replica(token, since)
        print(f'The paused {PAUSED} tripped its breaker, left the pools and rejoined after it resumed.')
    telegram = discovery_status()['telegram']
    print('Telegram alerts:', f"{telegram['sent']} sent, {telegram['failed']} failed" if telegram['enabled'] else 'disabled')
    print('Service Discovery registration, round robin across replicas and per-replica sockets passed.')

if __name__ == '__main__':
    main()
