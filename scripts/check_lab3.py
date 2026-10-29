#!/usr/bin/env python3
"""Audit the effective Compose configuration against the Lab 3 scaling rules.

Checks Service Discovery, the replicas of the scaled services and their registration,
without reading private source or printing environment values. Run check_lab2.py too:
every replica must also follow the Lab 2 rules, which this audit applies to replicas
that check_lab2.py does not know by name.
"""
import argparse
import json
import re
import subprocess
import sys
from check_lab2 import SERVICE_DISCOVERY_URL, service_failures, tickets_enabled
from lab import SERVICES

# Scaled services and the fewest instances each must run.
SCALED = {'user-management': 2, 'battle': 2, 'guild': 2, 'package-registry': 2, 'map': 2, 'monster-raid': 2, 'tamagotchi': 2, 'notification': 2}
# Services whose replicas each publish a socket port registered as their socket origin.
SOCKET_SERVICES = ('monster-raid', 'guild')
DISCOVERY_SETTINGS = ('SERVICE_TLS_CERT_FILE', 'SERVICE_TLS_KEY_FILE', 'SERVICE_TLS_CA_FILE',
                      'TASK_TIMEOUT_SECONDS', 'MAX_CONCURRENT_TASKS')


def instances(services, name):
    """The Compose services that run name: itself and replicas named name-2, name-3, ..."""
    return [n for n in services if n == name or re.fullmatch(re.escape(name) + r'-[0-9]+', n)]


def published(service):
    return [(p.get('host_ip'), str(p.get('published')), int(p.get('target', 0))) for p in service.get('ports', [])]


def audit(config):
    failures = []
    services = config.get('services', {})
    discovery = services.get('service-discovery')
    if discovery is None:
        failures.append('service-discovery: missing from Compose')
    else:
        env = discovery.get('environment', {})
        if not discovery.get('image', '').endswith(':latest'):
            failures.append('service-discovery: image does not use latest')
        if discovery.get('pull_policy') != 'always':
            failures.append('service-discovery: startup does not refresh its image')
        if not discovery.get('healthcheck'):
            failures.append('service-discovery: no healthcheck')
        if discovery.get('ports'):
            failures.append('service-discovery: publishes a host port; the registry stays inside the deployment')
        for key in DISCOVERY_SETTINGS:
            if key not in env:
                failures.append(f'service-discovery: {key} is not configured')
        if bool(env.get('TELEGRAM_BOT_TOKEN')) != bool(env.get('TELEGRAM_CHAT_ID')):
            failures.append('service-discovery: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set together')

    gateway = services.get('gateway', {}).get('environment', {})
    if gateway.get('SERVICE_DISCOVERY_URL') != SERVICE_DISCOVERY_URL:
        failures.append('gateway: SERVICE_DISCOVERY_URL does not name Service Discovery')
    upstreams = json.loads(gateway.get('UPSTREAMS_JSON', '{}'))
    managed = {name.strip() for name in gateway.get('DISCOVERY_MANAGED_SERVICES', '').split(',') if name.strip()}
    for name in sorted(managed):
        if name not in upstreams:
            failures.append(f'gateway: unknown discovery-managed service {name}')
        elif services.get(name, {}).get('environment', {}).get('SERVICE_DISCOVERY_URL') != SERVICE_DISCOVERY_URL:
            failures.append(f'{name}: managed routing requires service registration')
    ready = gateway.get('READY_SERVICES', '').split(',')
    gateway_tickets = tickets_enabled(gateway, 'true')

    for name, fewest in SCALED.items():
        if name not in managed:
            failures.append(f'{name}: Gateway must require discovery routing')
        group = instances(services, name)
        count = sum(int(services[n].get('deploy', {}).get('replicas', 1)) for n in group)
        if count < fewest:
            failures.append(f'{name}: runs {count} instance(s); Lab 3 needs at least {fewest}')
        images = {services[n].get('image') for n in group}
        if len(images) > 1:
            failures.append(f'{name}: replicas run different images')
        ids = []
        for label in group:
            service = services[label]
            env = service.get('environment', {})
            # check_lab2.py already audits the service named exactly like the owner.
            if label != name:
                failures += service_failures(label, name, service, upstreams, ready, gateway_tickets)
            if env.get('SERVICE_DISCOVERY_URL') != SERVICE_DISCOVERY_URL:
                failures.append(f'{label}: SERVICE_DISCOVERY_URL does not name Service Discovery')
            replicas = int(service.get('deploy', {}).get('replicas', 1))
            if replicas > 1 and service.get('ports'):
                failures.append(f'{label}: {replicas} replicas cannot share a published port')
            if replicas > 1 and env.get('INSTANCE_ID'):
                failures.append(f'{label}: {replicas} replicas cannot share one INSTANCE_ID')
            if env.get('INSTANCE_ID'):
                ids.append(env['INSTANCE_ID'])
            if name in SOCKET_SERVICES:
                ports = published(service)
                if len(ports) != 1 or ports[0][2] != 8080:
                    failures.append(f'{label}: must publish exactly one socket port to 8080')
                elif env.get('PUBLIC_SOCKET_URL') != f'ws://localhost:{ports[0][1]}':
                    failures.append(f'{label}: PUBLIC_SOCKET_URL does not match its published port')
        if len(ids) != len(set(ids)):
            failures.append(f'{name}: replicas share an INSTANCE_ID')
    socket_ports = [published(services[n])[0][1] for name in SOCKET_SERVICES for n in instances(services, name) if published(services[n])]
    if len(socket_ports) != len(set(socket_ports)):
        failures.append('socket replicas publish the same host port')
    for name in SERVICES:
        if name not in SCALED and name in services and services[name].get('environment', {}).get('SERVICE_DISCOVERY_URL') not in (None, SERVICE_DISCOVERY_URL):
            failures.append(f'{name}: SERVICE_DISCOVERY_URL does not name Service Discovery')
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file')
    args = parser.parse_args()
    command = ['docker', 'compose']
    if args.env_file:
        command += ['--env-file', args.env_file]
    result = subprocess.run(command + ['config', '--format', 'json'], capture_output=True, text=True)
    if result.returncode:
        sys.exit('Cannot resolve Compose. Run setup and check required local settings.')
    failures = audit(json.loads(result.stdout))
    for failure in failures:
        print('FAIL:', failure)
    if failures:
        sys.exit(f'{len(failures)} scaling gaps remain. Run the Service Discovery smoke checks against a live deployment too.')
    print('Service Discovery, replicas, registration and socket ports pass. Run smoke_discovery.py next.')


if __name__ == '__main__':
    main()
