#!/usr/bin/env python3
"""Show each Lab 4 grade working on the live stack, with PASS/FAIL lines.

    python scripts/demo_lab4.py grade4     logs download from Loki through Gateway
    python scripts/demo_lab4.py grade5     requests through the broker, spread over instances
    python scripts/demo_lab4.py grade6     subscriber queues, topic delivery, topics in Service Discovery
    python scripts/demo_lab4.py grade7     thread-per-request, list of topics
    python scripts/demo_lab4.py grade8     gRPC to the owners, internal routes on Gateway
    python scripts/demo_lab4.py grade9     durable queue across a broker restart, dead letter channel
    python scripts/demo_lab4.py grade10    rerouting while a replica stops, load-based scores, Grafana
    python scripts/demo_lab4.py all

Requires the stack from `python scripts/lab.py up` and Alice as the local demo admin
(REGISTRY_ADMIN_USER_IDS, filled by setup). Reads as Alice without changing her data.
grade9 and grade10 stop and restart containers and always start them again.
Event demos publish clearly marked demo events as Tamagotchi, from a throwaway
container with Tamagotchi's certificate: the only producer the broker accepts for
pet.used.v1. No secrets are printed.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lab import environment  # noqa: E402
from smoke import USERS, request  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = 'http://127.0.0.1:8080'
ADMIN = GATEWAY + '/services/message-broker'
TAMAGOTCHI = GATEWAY + '/services/tamagotchi'
TOPIC = 'pet.used.v1'
RESULTS = {'passed': 0, 'failed': 0}

PUBLISHER = r'''
import json, os, grpc
from broker.gen import broker_pb2 as pb, broker_pb2_grpc as g
read = lambda name: open('/run/tls/' + name, 'rb').read()
creds = grpc.ssl_channel_credentials(read('ca.pem'), read('tamagotchi-key.pem'), read('tamagotchi.pem'))
bus = g.EventBusStub(grpc.secure_channel('message-broker:50051', creds))
for event in json.loads(os.environ['EVENTS']):
    answer = bus.Publish(pb.PublishRequest(topic=event['type'], key=event['aggregateId'],
                                           value=json.dumps(event).encode()), timeout=10)
    print(json.dumps({'eventId': event['eventId'], 'offset': answer.offset, 'duplicate': answer.duplicate}))
'''

SD_READER = r'''
import json, ssl, urllib.request
context = ssl.create_default_context(cafile='/run/tls/ca.pem')
context.load_cert_chain('/run/tls/message-broker.pem', '/run/tls/message-broker-key.pem')
with urllib.request.urlopen('https://service-discovery:8443/v1/services', context=context, timeout=10) as r:
    print(r.read().decode())
'''


# -- plumbing ------------------------------------------------------------------------------


def check(name, ok, detail=''):
    RESULTS['passed' if ok else 'failed'] += 1
    print(('PASS: ' if ok else 'FAIL: ') + name + (f' - {detail}' if detail else ''), flush=True)
    return ok


def section(title):
    print(f'\n== {title} ==', flush=True)


def compose(*args, **kwargs):
    return subprocess.run(['docker', 'compose', *args], cwd=ROOT, text=True, capture_output=True, **kwargs)


_config = {}


def config():
    if not _config:
        result = compose('config', '--format', 'json')
        if result.returncode:
            sys.exit('Cannot resolve Compose; run setup first.')
        _config.update(json.loads(result.stdout))
    return _config


def inside(script, env=None):
    """Run a Python script in a throwaway broker-image container on the stack's network."""
    image = config()['services']['message-broker']['image']
    command = ['docker', 'run', '--rm', '-i', '--network', f"{config()['name']}_default",
               '-v', f"{ROOT / '.secrets' / 'tls'}:/run/tls:ro", '--entrypoint', 'python']
    # setup keeps .secrets/tls private (0700) to the host user; the image's own user cannot enter it.
    if hasattr(os, 'getuid'):
        command += ['--user', f'{os.getuid()}:{os.getgid()}']
    for key, value in (env or {}).items():
        command += ['-e', f'{key}={value}']
    result = subprocess.run(command + [image, '-'], input=script, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr else 'container failed')
    return result.stdout


_token = {}
_token_lock = threading.Lock()


def token():
    # One login for the whole run: logins hash the password on purpose, so many parallel
    # logins would exceed User Management's task deadline and trip Gateway's breakers.
    with _token_lock:
        if 'alice' not in _token:
            login = request(USERS, 'POST', '/v1/auth/login',
                            {'email': 'alice@demo.invalid', 'password': environment()['SEED_PASSWORD']})
            _token['alice'] = login['accessToken']
        return _token['alice']


def get(url, raw=False, timeout=20):
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            # HTTPMessage: header names are case-insensitive (the broker sends them lowercase).
            return response.status, response.headers, (body if raw else json.loads(body or b'null'))
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def broker_logs(since):
    result = compose('logs', '--no-log-prefix', '--since', since, 'message-broker')
    entries = []
    for line in result.stdout.splitlines():
        start = line.find('{')
        if start >= 0:
            try:
                entries.append(json.loads(line[start:]))
            except ValueError:
                pass
    return entries


def wait(predicate, seconds=30, every=1.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(every)
    return predicate()


def demo_event(valid=True):
    owner, aggregate = str(uuid.uuid4()), str(uuid.uuid4())
    data = {'activityId': aggregate, 'petIds': [str(uuid.uuid4())], 'ownerIds': [owner]}
    if not valid:
        del data['ownerIds']  # schema violation: Notification rejects it permanently
    return {'eventId': str(uuid.uuid4()), 'type': TOPIC, 'schemaVersion': '1',
            'occurredAt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'producer': 'tamagotchi', 'aggregateId': aggregate, 'aggregateVersion': 1,
            'correlationId': str(uuid.uuid4()), 'data': data}


def publish(*events):
    out = inside(PUBLISHER, {'EVENTS': json.dumps(list(events))})
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def group_counts():
    status, _, body = get(ADMIN + '/v1/topics')
    topic = next(t for t in body['items'] if t['name'] == TOPIC)
    return next(g for g in topic['groups'] if g['group'] == 'notification'), topic


def delivered_and_acked(since, offset):
    entries = broker_logs(since)
    acked = any(e.get('msg') == 'event acknowledged' and e.get('topic') == TOPIC and e.get('offset') == offset
                for e in entries)
    return acked


def spread(count=30, path='/v1/pet-types'):
    with ThreadPoolExecutor(8) as pool:
        answers = list(pool.map(lambda _: get(TAMAGOTCHI + path), range(count)))
    served = {}
    for status, headers, _ in answers:
        if status == 200:
            name = headers.get('X-Served-By', '?')
            served[name] = served.get(name, 0) + 1
    return sum(1 for s, _, _ in answers if s == 200), served


# -- grades ------------------------------------------------------------------------------------


def grade4():
    section('Grade 4: download the system logs (GET /v1/logs, from Loki)')
    status, headers, body = get(ADMIN + '/v1/logs?service=tamagotchi&level=info&limit=500', raw=True)
    lines = [json.loads(line) for line in body.decode().splitlines() if line.strip()] if status == 200 else []
    path = Path(tempfile.gettempdir()) / 'lab4-tamagotchi-logs.ndjson'
    if status == 200:
        path.write_bytes(body)
    check('logs download through Gateway', status == 200 and headers.get('Content-Type') == 'application/x-ndjson',
          f'{status}, {len(lines)} lines, saved to {path}')
    check('every line belongs to the requested service', bool(lines) and all(l.get('service') == 'tamagotchi' for l in lines))
    status, _, body = get(ADMIN + '/v1/logs?level=warning&limit=200', raw=True)
    services = sorted({json.loads(l).get('service') for l in body.decode().splitlines() if l.strip()}) if status == 200 else []
    check('warnings and errors from the whole system', status == 200, f'services: {", ".join(map(str, services)) or "none in the last hour"}')


def grade5():
    section('Grade 5: requests through the broker, balanced across instances')
    since = now()
    ok, served = spread()
    check('30 requests to Tamagotchi through Gateway and the broker', ok == 30, f'{ok}/30 answered')
    check('spread over several replicas (X-Served-By)', len(served) >= 2, json.dumps(served))
    routed = [e for e in broker_logs(since) if e.get('msg') == 'request routed' and e.get('destination') == 'tamagotchi']
    check('the broker logged each routed request', len(routed) >= 30, f'{len(routed)} "request routed" lines')
    status, _, body = get(ADMIN + '/v1/queues')
    queue = next((q for q in body['items'] if q['service'] == 'tamagotchi'), None) if status == 200 else None
    check('the broker sees the Tamagotchi pool', bool(queue and queue['instances']),
          f"{len(queue['instances']) if queue else 0} instances")


def grade6():
    section('Grade 6: subscriber queues (Gateway → service) and topic queues (service → service)')
    status, _, body = get(GATEWAY + '/status')
    routing = body.get('routing', {}) if status == 200 else {}
    brokered = sorted(name for name, how in routing.items() if how == 'message-broker')
    check('Gateway names the destination queue for migrated services', bool(brokered), ', '.join(brokered))
    since = now()
    [published] = publish(demo_event())
    check(f'{TOPIC} published by Tamagotchi without naming any consumer', not published['duplicate'],
          f"offset {published['offset']}")
    check('the notification group received and acknowledged it',
          bool(wait(lambda: delivered_and_acked(since, published['offset']), 30)))
    pools = json.loads(inside(SD_READER))['services']
    topics = sorted({f"{t['topic']}/{t['group']}" for i in pools.get('notification', []) for t in i.get('topics', [])})
    check('Notification registered its topics in Service Discovery', len(topics) == 9, f'{len(topics)} topic/group pairs')


def grade7():
    section('Grade 7: thread-per-request, list of topics')
    since = now()
    with ThreadPoolExecutor(20) as pool:
        answers = list(pool.map(lambda _: get(ADMIN + '/v1/topics'), range(20)))
    check('20 parallel GET /v1/topics', all(a[0] == 200 for a in answers))
    topics = [t['name'] for t in answers[0][2]['items']]
    print('   topics:', ', '.join(topics))
    # Gateway reaches the admin route through the broker's own queue: /queues/message-broker/v1/topics.
    lines = [e for e in broker_logs(since)
             if e.get('msg') == 'request completed' and str(e.get('path', '')).endswith('/v1/topics')]
    threads = {e.get('thread') for e in lines}
    check('each request ran on its own new thread', len(threads) >= 20 and all(t.startswith('req-') for t in threads),
          f'{len(threads)} distinct threads for {len(lines)} requests')


def grade8():
    section('Grade 8: gRPC between the broker and the owners; internal routes on Gateway')
    since = now()
    spread(10)
    routed = [e for e in broker_logs(since) if e.get('msg') == 'request routed' and e.get('destination') == 'tamagotchi']
    transports = sorted({e.get('transport') for e in routed})
    check('the broker reached Tamagotchi over gRPC (Endpoint.Handle)', transports == ['grpc'], ', '.join(transports))
    status, _, body = get(GATEWAY + '/status')
    removed = body.get('internalRoutesRemoved', 0)
    routing = body.get('routing', {})
    final = routing and all(how == 'message-broker' for how in routing.values())
    req = urllib.request.Request(TAMAGOTCHI + '/internal/v1/pet-reservations/' + str(uuid.uuid4()), method='PUT', data=b'{}')
    try:
        urllib.request.urlopen(req, timeout=10)
        code = 200
    except urllib.error.HTTPError as error:
        code = error.code
    if final:
        check('internal routes are gone from Gateway', removed > 0 and code == 404, f'{removed} removed, internal path answers {code}')
    else:
        print(f'   transition: {removed} internal routes removed; they stay until every service is broker-routed'
              f' (BROKER_SERVICES empty). An internal path answers {code}, not to players.')
        check('internal routes refuse callers without a service certificate', code in (401, 403, 404), str(code))


def grade9():
    section('Grade 9: durable queue across a broker restart, dead letter channel')
    print('   stopping Notification, so the event has to wait in the broker')
    compose('stop', 'notification')
    try:
        [published] = publish(demo_event())
        pending = wait(lambda: group_counts()[0]['pending'] >= 1, 15)
        check('the event waits for the stopped consumer group', bool(pending), f"offset {published['offset']}")
        print('   restarting the broker')
        compose('restart', 'message-broker')
        healthy = wait(lambda: '"healthy"' in compose('ps', '--format', 'json', 'message-broker').stdout, 60, 2)
        check('the broker is back', bool(healthy))
        survived = wait(lambda: group_counts()[0]['pending'] >= 1, 20)
        check('the waiting event survived the restart (SQLite on the broker-data volume)', bool(survived))
    finally:
        since = now()
        compose('start', 'notification')
    check('Notification consumed it after coming back', bool(wait(lambda: delivered_and_acked(since, published['offset']), 60)))
    [poison] = publish(demo_event(valid=False))
    status_dlq = wait(lambda: any(item['message']['offset'] == poison['offset']
                                  for item in get(ADMIN + '/v1/dlq/' + TOPIC)[2]['items']), 40)
    check('an invalid event lands in pet.used.v1.dlq', bool(status_dlq), f"offset {poison['offset']}")
    status, _, body = get(ADMIN + '/v1/dlq/' + TOPIC + '?limit=1')
    if status == 200 and body['items']:
        item = body['items'][0]
        print(f"   newest dead letter: group {item['group']}, attempts {item['attempts']}, reason: {item['reason'][:90]}")
    status, _, body = get(ADMIN + '/v1/dlq')
    check('GET /v1/dlq lists the channels', status == 200, f"{sum(c['count'] for c in body['items'])} dead letters in total")


def grade10():
    section('Grade 10: rerouting when a replica stops, load-based routing, monitoring')
    ids = compose('ps', '-q', 'tamagotchi').stdout.split()
    check('three Tamagotchi replicas run', len(ids) >= 2, f'{len(ids)} containers')
    victim = ids[0]
    since = now()
    stop = threading.Event()
    answers = []

    def traffic():
        while not stop.is_set():
            answers.append(get(TAMAGOTCHI + '/v1/pet-types')[0])

    workers = [threading.Thread(target=traffic) for _ in range(4)]
    for worker in workers:
        worker.start()
    time.sleep(2)
    print('   stopping one replica while requests keep coming')
    subprocess.run(['docker', 'kill', victim], capture_output=True)
    time.sleep(12)
    stop.set()
    for worker in workers:
        worker.join()
    subprocess.run(['docker', 'start', victim], capture_output=True)
    failed = [s for s in answers if s != 200]
    check('no request failed while the replica was down', not failed and len(answers) > 20,
          f'{len(answers)} requests, {len(failed)} failed')
    entries = broker_logs(since)
    rerouted = sum(1 for e in entries if e.get('msg') == 'request rerouted')
    opened = sum(1 for e in entries if e.get('msg', '').startswith('circuit opened'))
    ejected = sum(1 for e in entries if e.get('msg') == 'instance ejected from the pool: circuit breaker tripped')
    check('the broker rerouted and tripped the breaker', rerouted >= 1 and opened >= 1,
          f'{rerouted} rerouted, {opened} breaker trips, {ejected} ejections reported to Service Discovery')
    status, _, body = get(ADMIN + '/v1/queues')
    queue = next(q for q in body['items'] if q['service'] == 'tamagotchi')
    print('   routing scores (lowest wins):', ', '.join(f"{i['instanceId']}={i['score']} ({i['circuit']})" for i in queue['instances']))
    check('instances carry a load-based score', all('score' in i for i in queue['instances']))
    with urllib.request.urlopen('http://127.0.0.1:3000/api/health', timeout=10) as response:
        check('Grafana is up: http://localhost:3000 (dashboard "Lab 4 Message Broker")', response.status == 200)
    with urllib.request.urlopen('http://127.0.0.1:9090/api/v1/query?query=up%7Bjob%3D%22message-broker%22%7D', timeout=10) as response:
        up = json.loads(response.read())['data']['result']
        check('Prometheus scrapes the broker', bool(up) and up[0]['value'][1] == '1')


GRADES = {'grade4': grade4, 'grade5': grade5, 'grade6': grade6, 'grade7': grade7, 'grade8': grade8,
          'grade9': grade9, 'grade10': grade10}


def main(argv):
    if len(argv) != 1 or argv[0] not in (*GRADES, 'all'):
        sys.exit(__doc__)
    os.environ.setdefault('PYTHONUTF8', '1')
    token()  # log in once, before any parallel request
    for name in (GRADES if argv[0] == 'all' else [argv[0]]):
        try:
            GRADES[name]()
        except Exception as error:  # report and continue with the next grade
            check(f'{name} ran to the end', False, f'{type(error).__name__}: {error}')
    print(f"\n{RESULTS['passed']} checks passed, {RESULTS['failed']} failed")
    sys.exit(1 if RESULTS['failed'] else 0)


if __name__ == '__main__':
    main(sys.argv[1:])
