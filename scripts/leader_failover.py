"""Opt-in worker-leader takeover check for replicated Go services.

Only call stopped_worker_leader() after the caller's explicit disruptive flag.
Identity/lifecycle JSON is the evidence; no business endpoint or database is bypassed.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import subprocess
import time
import urllib.request

import lab
import smoke_discovery as discovery

MAX_LOG_LINES = 10000
ROLE_EVENTS = {
    'service started': False,
    'worker leader acquired': True,
    'worker leader released': False,
    'worker leader connection lost': False,
    'shutdown started': False,
    'service stopped': False,
}


@dataclass(frozen=True)
class Container:
    id: str
    instance: str
    started: str


@dataclass(frozen=True)
class Role:
    leader: bool
    position: tuple


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp must be a string')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    assert parsed.tzinfo is not None, 'Leadership proof needs timezone-aware timestamps'
    return parsed.astimezone(timezone.utc)


def current_role(logs, service, container):
    """Use only this running container's current process lifetime, in event-time order."""
    events, startup = [], False
    for index, line in enumerate(logs.splitlines()):
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict) or event.get('service') != service or event.get('instanceId') != container.instance:
            continue
        message = event.get('msg')
        if message not in ROLE_EVENTS:
            continue
        try:
            when = timestamp(event['time'])
        except (KeyError, TypeError, ValueError) as error:
            raise AssertionError('Leadership event lacks a valid timestamp') from error
        if when < timestamp(container.started):
            continue
        startup |= message == 'service started'
        events.append(((when, index), ROLE_EVENTS[message]))
    assert startup, 'Cannot prove leadership: current-process startup is missing from retained logs'
    position, leader = max(events)
    return Role(leader, position)


def remaining(deadline):
    seconds = deadline - time.monotonic()
    assert seconds > 0, 'Leader takeover did not complete within 30 seconds'
    return min(5, seconds)


def running(service, deadline):
    ids = lab.compose('ps', '-q', service, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                      timeout=remaining(deadline)).stdout.decode().split()
    if not ids:
        return []
    template = '{{.Id}} {{.Config.Hostname}} {{.State.Running}} {{.State.StartedAt}}'
    result = lab.run(['docker', 'inspect', '--format', template, *ids], stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, timeout=remaining(deadline)).stdout.decode()
    containers = []
    for line in result.splitlines():
        identity, hostname, active, started = line.split()
        if active != 'true':
            continue
        assert re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', hostname), 'Container hostname cannot identify a service instance'
        timestamp(started)
        containers.append(Container(identity, hostname, started))
    assert len({c.instance for c in containers}) == len(containers), 'Container hostnames are not unique'
    return containers


def roles(service, containers, deadline):
    result = {}
    for container in containers:
        logs = lab.run(['docker', 'logs', '--since', container.started, '--tail', str(MAX_LOG_LINES), container.id],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       timeout=remaining(deadline)).stdout.decode()
        result[container.id] = current_role(logs, service, container)
    return result


def routing_pool(service, deadline):
    with urllib.request.urlopen(discovery.GATEWAY + '/status', timeout=remaining(deadline)) as response:
        return json.loads(response.read(1 << 20))['pools'][service]


def sole_leader(containers, state):
    leaders = [container for container in containers if state[container.id].leader]
    assert len(leaders) == 1, f'Cannot prove one current worker leader: observed {len(leaders)}'
    return leaders[0]


@contextmanager
def stopped_worker_leader(service='battle'):
    """Yield a proven replacement while the exact original leader remains stopped.

    The caller can exercise real business work inside the context. Restoration runs
    even when stopping, takeover detection or the caller fails; the data is untouched.
    """
    assert service in ('battle', 'user-management'), 'Leader check supports the owned Go services only'
    deadline = time.monotonic() + 30
    original = running(service, deadline)
    assert len(original) >= 2, 'Leader check needs at least two running replicas'
    baseline = roles(service, original, deadline)
    leader = sole_leader(original, baseline)
    original_instances = {c.instance for c in original}
    replacement = None
    try:
        # A stop may succeed before its command reports an error; always start it again.
        lab.run(['docker', 'stop', '--time', '15', leader.id], stdout=subprocess.DEVNULL, timeout=20)
        deadline = time.monotonic() + 30
        while True:
            current = running(service, deadline)
            assert leader.id not in {c.id for c in current}, 'The original worker leader is still running'
            assert {c.id for c in current} == set(baseline) - {leader.id}, 'Replica set changed during the leader proof'
            state = roles(service, current, deadline)
            candidates = [c for c in current if state[c.id].leader]
            assert len(candidates) <= 1, 'Multiple running replicas claim worker leadership'
            if candidates:
                candidate = candidates[0]
                assert state[candidate.id].position > baseline[candidate.id].position, 'Replacement has no new leadership-acquired event'
                entry = routing_pool(service, deadline)
                if entry['source'] == 'service-discovery' and leader.instance not in entry['instances'] and candidate.instance in entry['instances']:
                    replacement = candidate
                    break
            time.sleep(min(0.25, remaining(deadline)))
        print(f'PASS: {service} worker leadership moved from {leader.instance} to {replacement.instance}; original remains stopped.', flush=True)
        yield {'service': service, 'stoppedInstance': leader.instance, 'leaderInstance': replacement.instance}
    finally:
        lab.run(['docker', 'start', leader.id], stdout=subprocess.DEVNULL, timeout=20)
        def recovered():
            entry = discovery.pool(service)
            return entry['source'] == 'service-discovery' and set(entry['instances']) == original_instances
        discovery.wait(f'{service} original replicas recovering after leader test', recovered, 120)
        print(f'PASS: restored the stopped {service} container and full discovery pool.', flush=True)
