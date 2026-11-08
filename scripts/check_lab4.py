#!/usr/bin/env python3
"""Audit the effective Compose configuration against the Lab 4 Message Broker rules.

Checks the broker (image, durable volume, certificates, Service Discovery, Loki), the
Gateway's broker routing, the services that already use the broker, and the monitoring
stack (Prometheus scraping the broker, Alloy shipping container logs to Loki, Grafana).
It reads no private source and prints no environment values. Run check_lab2.py and
check_lab3.py too; then demo_lab4.py against the live stack.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BROKER = 'message-broker'
GATEWAY_QUEUES = 'https://message-broker:8443'
BROKER_GRPC = 'message-broker:50051'
# Services already on the broker: events, Endpoint.Handle and Router.Call.
MIGRATED = ('user-management', 'battle', 'guild', 'package-registry', 'map', 'monster-raid', 'tamagotchi', 'notification')
MONITORING = ('prometheus', 'loki', 'alloy', 'grafana')
TLS_FILES = ('SERVICE_TLS_CERT_FILE', 'SERVICE_TLS_KEY_FILE', 'SERVICE_TLS_CA_FILE')


def mounts(service):
    """Container paths of the service's volumes and binds."""
    return {v.get('target') for v in service.get('volumes', []) if isinstance(v, dict)}


def named_volume(service, target):
    return any(isinstance(v, dict) and v.get('type') == 'volume' and v.get('target') == target
               for v in service.get('volumes', []))


def listed(value):
    return [name.strip() for name in (value or '').split(',') if name.strip()]


def audit(config, root=ROOT):
    failures = []
    services = config.get('services', {})
    broker = services.get(BROKER)
    if broker is None:
        return [f'{BROKER}: service missing']
    env = broker.get('environment', {})
    if not broker.get('image', '').endswith(':latest') or broker.get('pull_policy') != 'always':
        failures.append(f'{BROKER}: must run the latest published image with pull_policy always')
    if 'build' in broker:
        failures.append(f'{BROKER}: Compose must use the published image, not build it')
    if not named_volume(broker, '/data'):
        failures.append(f'{BROKER}: durable queues need a named volume on /data')
    for key in (*TLS_FILES, 'GATEWAY_CERT_FILE'):
        if not env.get(key) or env[key] not in mounts(broker):
            failures.append(f'{BROKER}: {key} must name a mounted file')
    if env.get('SERVICE_DISCOVERY_URL') != 'https://service-discovery:8443':
        failures.append(f'{BROKER}: SERVICE_DISCOVERY_URL must name Service Discovery')
    if env.get('LOKI_URL') != 'http://loki:3100':
        failures.append(f'{BROKER}: LOKI_URL must name Loki for GET /v1/logs')
    if 'healthcheck' not in broker:
        failures.append(f'{BROKER}: needs a healthcheck')

    gateway_env = services.get('gateway', {}).get('environment', {})
    if gateway_env.get('BROKER_URL') != GATEWAY_QUEUES:
        failures.append(f'gateway: BROKER_URL must be {GATEWAY_QUEUES}')
    brokered = listed(gateway_env.get('BROKER_SERVICES'))
    upstreams = json.loads(gateway_env.get('UPSTREAMS_JSON') or '{}')
    if upstreams.get(BROKER) != GATEWAY_QUEUES:
        failures.append('gateway: UPSTREAMS_JSON must include the broker for its admin routes')

    for name in MIGRATED:
        service = services.get(name, {})
        service_env = service.get('environment', {})
        if brokered and name not in brokered:
            failures.append(f'gateway: BROKER_SERVICES must include migrated {name}')
        if service_env.get('BROKER_URL') != BROKER_GRPC:
            failures.append(f'{name}: BROKER_URL must be {BROKER_GRPC}')
        cert = service_env.get('BROKER_CERT_FILE')
        if not cert or cert not in mounts(service):
            failures.append(f'{name}: BROKER_CERT_FILE must name the mounted message-broker.pem')
        if service_env.get('GATEWAY_ONLY') != 'true':
            failures.append(f'{name}: Endpoint.Handle needs GATEWAY_ONLY=true')
        if not str(service_env.get('GRPC_PORT', '')).isdigit():
            failures.append(f'{name}: GRPC_PORT must be set for Endpoint.Handle')

    for name in MONITORING:
        if name not in services:
            failures.append(f'{name}: monitoring service missing')
    alloy = services.get('alloy', {})
    if '/var/run/docker.sock' not in mounts(alloy):
        failures.append('alloy: needs the Docker socket to read container logs')
    prometheus = root / 'deployment/monitoring/prometheus.yml'
    if not prometheus.is_file() or f"'{BROKER}:8080'" not in prometheus.read_text():
        failures.append('prometheus: prometheus.yml must scrape message-broker:8080')
    alloy_config = root / 'deployment/monitoring/config.alloy'
    if not alloy_config.is_file() or 'loki.source.docker' not in alloy_config.read_text():
        failures.append('alloy: config.alloy must ship Docker logs to Loki')
    dashboard = root / 'deployment/monitoring/grafana/dashboards/lab4.json'
    try:
        panels = json.loads(dashboard.read_text())['panels']
    except (OSError, ValueError, KeyError):
        panels = []
    if len(panels) < 5:
        failures.append('grafana: the Lab 4 dashboard is missing or empty')
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
        sys.exit(f'{len(failures)} Message Broker gaps remain.')
    print('Message Broker, broker routing, migrated services and monitoring pass. Run demo_lab4.py next.')


if __name__ == '__main__':
    main()
