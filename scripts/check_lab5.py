#!/usr/bin/env python3
"""Audit the effective Compose configuration against the Lab 5 rules.

Checks the replicated PostgreSQL cluster (Patroni, etcd, HAProxy), the MongoDB replica set,
the three Valkey cache nodes and the services that use them, and the warehouse with its ETL
and dashboard. It reads no private source and prints no environment values. Run
check_lab2.py to check_lab4.py too; then demo_lab5.py against the live stack.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_NODES = ('postgres-1', 'postgres-2', 'postgres-3')
MONGO_MEMBERS = ('mongo', 'mongo-2', 'mongo-3')
CACHE_NODES = ('cache-1', 'cache-2', 'cache-3')
# Every business service gets the cache settings (contracts/cache.md), replicas included.
CACHE_USERS = ('user-management', 'battle', 'guild', 'guild-2', 'guild-3', 'package-registry', 'map',
               'monster-raid', 'monster-raid-2', 'monster-raid-3', 'tamagotchi', 'notification')


def named_volume(service, target):
    return any(isinstance(v, dict) and v.get('type') == 'volume' and v.get('target') == target
               for v in service.get('volumes', []))


def depends_on(service):
    value = service.get('depends_on', {})
    return set(value) if isinstance(value, (dict, list)) else set()


def seconds(duration):
    """Compose writes durations like '1m0s'."""
    total, number = 0, ''
    for char in str(duration or ''):
        if char.isdigit():
            number += char
        elif number:
            total += int(number) * {'h': 3600, 'm': 60, 's': 1}.get(char, 0)
            number = ''
    return total


def published_on_localhost(service, port):
    return all(p.get('host_ip') == '127.0.0.1' for p in service.get('ports', []) if str(p.get('published')) == str(port)) \
        and any(str(p.get('published')) == str(port) for p in service.get('ports', []))


def audit(config, root=ROOT):
    failures = []
    services = config.get('services', {})

    # Replication (grade 5).
    if 'postgres' in services:
        failures.append('postgres: the single server must be replaced by the Patroni cluster')
    if 'etcd' not in services:
        failures.append('etcd: Patroni needs etcd for its leader lock')
    for name in POSTGRES_NODES:
        node = services.get(name)
        if node is None:
            failures.append(f'{name}: Patroni node missing')
            continue
        spilo = node.get('environment', {}).get('SPILO_CONFIGURATION', '')
        # max_prepared_transactions enables the raid join's two-phase commit (grade 8).
        for setting in ('synchronous_mode: true', 'synchronous_mode_strict: true', 'failsafe_mode: true',
                        'max_prepared_transactions: 64'):
            if setting not in spilo:
                failures.append(f'{name}: SPILO_CONFIGURATION must set {setting}')
        if not named_volume(node, '/home/postgres/pgdata'):
            failures.append(f'{name}: needs its own named data volume')
        if seconds(node.get('stop_grace_period')) < 30:
            failures.append(f'{name}: stop_grace_period must let Patroni hand over the leader lock (>= 30 s)')
        if 'healthcheck' not in node:
            failures.append(f'{name}: healthcheck missing')
    haproxy = services.get('haproxy')
    if haproxy is None:
        failures.append('haproxy: missing; it keeps postgres:5432 on the primary')
    else:
        aliases = set((haproxy.get('networks', {}).get('default') or {}).get('aliases') or [])
        if not {'postgres', 'postgres-replicas'} <= aliases:
            failures.append('haproxy: needs the network aliases postgres and postgres-replicas')
        if not published_on_localhost(haproxy, 7000):
            failures.append('haproxy: the statistics page must be published on 127.0.0.1:7000 only')
        cfg = root / 'deployment/haproxy/haproxy.cfg'
        text = cfg.read_text() if cfg.is_file() else ''
        for needle in ('GET /primary', 'GET /replica', 'on-marked-down shutdown-sessions', 'option redispatch',
                       'nbsrv(postgres-replicas) gt 0'):
            if needle not in text:
                failures.append(f'haproxy: haproxy.cfg must contain "{needle}"')
    for name, service in services.items():
        if 'postgres' in depends_on(service):
            failures.append(f'{name}: must wait for haproxy, not the removed postgres service')
    for name in MONGO_MEMBERS:
        member = services.get(name)
        if member is None or 'rs0' not in ' '.join(member.get('command') or []):
            failures.append(f'{name}: replica-set member of rs0 missing')
    registry = services.get('package-registry', {}).get('environment', {}).get('MONGODB_URL', '')
    if 'replicaSet=rs0' not in registry or 'w=majority' not in registry:
        failures.append('package-registry: MONGODB_URL needs replicaSet=rs0 and w=majority')
    if not all(f'{member}:27017' in registry for member in MONGO_MEMBERS):
        # Package Registry 5.x accepts a seed list; 4.x rejected one.
        failures.append('package-registry: MONGODB_URL must list every member, so the set is found when one is down')

    # Cache and sharding (grades 6 and 7).
    for name in CACHE_NODES:
        node = services.get(name)
        command = ' '.join(node.get('command') or []) if node else ''
        if node is None or 'valkey' not in node.get('image', ''):
            failures.append(f'{name}: Valkey node missing')
        elif '--save ""' not in command or '--requirepass' not in command:
            failures.append(f'{name}: must run without persistence and with a password')
    for name in CACHE_USERS:
        env = services.get(name, {}).get('environment', {})
        configured = {entry.split('=', 1)[0] for entry in (env.get('CACHE_NODES') or '').split(',') if entry}
        if configured != set(CACHE_NODES) or not env.get('CACHE_PASSWORD'):
            failures.append(f'{name}: CACHE_NODES must list cache-1..3 and CACHE_PASSWORD must be set')

    # Warehouse and ETL (grade 4).
    warehouse = services.get('warehouse')
    if warehouse is None or not named_volume(warehouse, '/var/lib/postgresql/data'):
        failures.append('warehouse: PostgreSQL with a named data volume missing')
    elif not published_on_localhost(warehouse, 5434):
        failures.append('warehouse: must be published on 127.0.0.1:5434 only')
    etl = services.get('etl')
    if etl is None:
        failures.append('etl: the ETL job is missing')
    else:
        if not etl.get('environment', {}).get('ETL_DB_PASSWORD'):
            failures.append('etl: ETL_DB_PASSWORD must be set (read-only role etl_reader)')
        if not {'warehouse', 'haproxy'} <= depends_on(etl):
            failures.append('etl: must wait for the warehouse and haproxy')
    for path in ('deployment/etl/etl.py', 'deployment/etl/warehouse.sql', 'deployment/etl/requirements.txt'):
        if not (root / path).is_file():
            failures.append(f'etl: {path} missing')
    datasources = root / 'deployment/monitoring/grafana/provisioning/datasources/datasources.yaml'
    if not datasources.is_file() or 'uid: warehouse' not in datasources.read_text():
        failures.append('grafana: the warehouse data source is not provisioned')
    try:
        panels = json.loads((root / 'deployment/monitoring/grafana/dashboards/lab5-analytics.json').read_text())['panels']
    except (OSError, ValueError, KeyError):
        panels = []
    if len(panels) < 5:
        failures.append('grafana: the Lab 5 "Game analytics" dashboard is missing or empty')
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file')
    args = parser.parse_args()
    command = ['docker', 'compose']
    if args.env_file:
        command += ['--env-file', args.env_file]
    result = subprocess.run(command + ['config', '--format', 'json'], capture_output=True, text=True, cwd=ROOT)
    if result.returncode:
        sys.exit('Cannot resolve Compose. Run setup and check required local settings.')
    failures = audit(json.loads(result.stdout))
    for failure in failures:
        print('FAIL:', failure)
    if failures:
        sys.exit(f'{len(failures)} Lab 5 gaps remain.')
    print('Replication, failover, cache nodes, warehouse, ETL and the analytics dashboard pass. Run demo_lab5.py next.')


if __name__ == '__main__':
    main()
