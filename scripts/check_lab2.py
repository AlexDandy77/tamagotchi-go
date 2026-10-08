#!/usr/bin/env python3
"""Audit the effective Compose configuration against the lab's shared runtime rules.

Does not read private source or print environment values. A nonzero exit means
that the deployment still needs work; this is not a replacement for live tests.
"""
import argparse
import json
import subprocess
import sys
from urllib.parse import urlsplit
from lab import SERVICES

# Destinations whose live sockets validate the Gateway's socket tickets. Each must require
# tickets exactly when the Gateway issues them: either mismatch leaves its clients unable to
# authenticate.
TICKET_SOCKETS = ('monster-raid', 'guild')


def tickets_enabled(env, default):
    return str(env.get('SOCKET_TICKETS_ENABLED', default)).lower() == 'true'


def audit(config):
    failures = []
    services = config.get('services', {})
    gateway = services.get('gateway', {})
    upstreams = json.loads(gateway.get('environment', {}).get('UPSTREAMS_JSON', '{}'))
    ready = gateway.get('environment', {}).get('READY_SERVICES', '').split(',')
    # The Gateway issues tickets unless told not to.
    gateway_tickets = tickets_enabled(gateway.get('environment', {}), 'true')
    for name in (*SERVICES, 'gateway'):
        service = services.get(name)
        if service is None:
            failures.append(f'{name}: missing from Compose')
            continue
        env = service.get('environment', {})
        if not service.get('image', '').endswith(':latest'):
            failures.append(f'{name}: image does not use latest')
        if service.get('pull_policy') != 'always':
            failures.append(f'{name}: startup does not refresh its image')
        for key in ('TASK_TIMEOUT_SECONDS', 'MAX_CONCURRENT_TASKS'):
            if key not in env:
                failures.append(f'{name}: {key} is not configured')
        for key in env:
            if key.endswith('_INTERNAL_URL') or key == 'JWKS_URL':
                failures.append(f'{name}: {key} must use a single destination base URL')
        if name == 'gateway':
            if 'MAX_CONCURRENT_SERVICE_TASKS' not in env:
                failures.append('gateway: service task budget is not configured')
            continue
        if name not in upstreams or name not in ready:
            failures.append(f'{name}: missing Gateway upstream or readiness check')
        if str(env.get('GATEWAY_ONLY', '')).lower() != 'true':
            failures.append(f'{name}: Gateway-only REST is not configured')
        if str(env.get('GATEWAY_ALLOW_DIRECT', '')).lower() == 'true':
            failures.append(f'{name}: direct REST compatibility is enabled')
        if 'GATEWAY_CERT_FILE' not in env:
            failures.append(f'{name}: Gateway verification certificate is not configured')
        if service.get('ports') and name not in ('guild', 'monster-raid'):
            failures.append(f'{name}: publishes a direct REST port')
        if name in TICKET_SOCKETS and tickets_enabled(env, 'false') != gateway_tickets:
            failures.append(f'{name}: SOCKET_TICKETS_ENABLED must match the Gateway, which issues the tickets')
        for key, value in env.items():
            if not key.endswith('_URL') or not isinstance(value, str):
                continue
            url = urlsplit(value)
            if url.scheme in ('http', 'https') and (url.hostname != 'gateway' or not url.path.startswith('/services/')):
                failures.append(f'{name}: {key} bypasses Gateway')
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
        sys.exit(f'{len(failures)} deployment gaps remain. Service code, release CI and live workflows also require verification.')
    print('Compose routing, task-limit settings and image-refresh policy pass. Run the live smoke checks next.')


if __name__ == '__main__':
    main()
