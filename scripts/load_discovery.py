#!/usr/bin/env python3
"""Load one adopted service through Gateway and require a critical-load alert and recovery.

Gateway may be the first capacity bottleneck. Use --require-service-alert when the chosen
service must itself cross LOAD_CRITICAL_PERCENT (lower the threshold if necessary). This
script never changes configuration or sends an explicit Telegram test message.
"""
import argparse
import collections
import datetime
import json
import subprocess
import threading
import time
import lab
from smoke import USERS, request
from smoke_discovery import SAFE_READS, call, read_url, wait


def load_alerts(since):
    logs = lab.compose('logs', '--no-log-prefix', '--since', since, 'service-discovery',
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode()
    return [a for a in (json.loads(line) for line in logs.splitlines() if line.startswith('{')) if a.get('alert') == 'load.critical']


def utc(at):
    return at.strftime('%Y-%m-%dT%H:%M:%SZ')


def alert_state(alerts, targets):
    """Track current state, not merely whether a recovery happened before a later alert."""
    raised, pending = set(), set()
    for alert in alerts:
        if alert.get('targetService') not in targets:
            continue
        key = (alert['targetService'], alert['targetInstance'])
        if alert.get('cleared'):
            pending.discard(key)
        else:
            raised.add(key)
            pending.add(key)
    return raised, pending


def cooldown_remaining(alerts, cooldown, now):
    times = [datetime.datetime.fromisoformat(a['time'].replace('Z', '+00:00')) for a in alerts if not a.get('cleared')]
    return max(0, cooldown - (now - max(times)).total_seconds() + 1) if times else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--service', choices=SAFE_READS, default='map')
    parser.add_argument('--readers', type=int, default=96)
    parser.add_argument('--seconds', type=float, default=15, help='duration of concurrent safe GET requests')
    parser.add_argument('--require-service-alert', action='store_true', help='require the selected service, not only Gateway, to report critical load')
    args = parser.parse_args()
    if args.readers < 1 or args.seconds <= 0:
        parser.error('readers and seconds must be positive')
    values = lab.environment()
    interval = float(values.get('HEALTH_CHECK_INTERVAL_SECONDS', '5'))
    cooldown = float(values.get('ALERT_COOLDOWN_SECONDS', '60'))
    token = request(USERS, 'POST', '/v1/auth/login', {'email': 'alice@demo.invalid', 'password': values['SEED_PASSWORD']})['accessToken']
    targets = {args.service} if args.require_service_alert else {args.service, 'gateway'}
    now = datetime.datetime.now(datetime.timezone.utc)
    previous = [a for a in load_alerts(utc(now - datetime.timedelta(seconds=cooldown))) if a.get('targetService') in targets]
    remaining = cooldown_remaining(previous, cooldown, now)
    if remaining:
        print(f'Waiting {remaining:.0f} s for the previous load-alert cooldown.')
        end = time.monotonic() + remaining
        while time.monotonic() < end:
            time.sleep(min(5, max(0, end - time.monotonic())))
    since = utc(datetime.datetime.now(datetime.timezone.utc))
    statuses, served = collections.Counter(), collections.Counter()
    lock = threading.Lock()
    deadline = time.monotonic() + args.seconds

    def read():
        while time.monotonic() < deadline:
            try:
                status, headers = call(read_url(args.service), token)
            except OSError as error:
                with lock:
                    statuses[type(error).__name__] += 1
                continue
            with lock:
                statuses[status] += 1
                served[headers.get('X-Served-By', 'gateway')] += 1

    readers = [threading.Thread(target=read) for _ in range(args.readers)]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join()
    assert statuses[200] > 0, 'No successful read reached the selected service'
    observed = []

    def recovered():
        observed[:] = load_alerts(since)
        raised, pending = alert_state(observed, targets)
        return bool(raised) and not pending

    wait('a critical-load alert followed by recovery for the selected target', recovered, max(30, 3 * interval + 5))
    print('Service:', args.service)
    print('Answers:', dict(sorted(statuses.items(), key=str)))
    print('Served by:', dict(sorted(served.items())))
    for alert in observed:
        if alert.get('targetService') in targets:
            print(f"{alert['time']} {alert['msg']}: {alert['targetService']}/{alert['targetInstance']}",
                  f"at {alert['percent']}% (threshold {alert['thresholdPercent']}%)" if 'percent' in alert else '')
    print('Discovery observed critical load and then confirmed recovery.')


if __name__ == '__main__':
    main()
