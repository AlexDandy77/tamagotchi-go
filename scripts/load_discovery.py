#!/usr/bin/env python3
"""Drive concurrent Map reads through the Gateway until Service Discovery's health monitor
sees critical load, then print the load alerts it raised and the recoveries that followed,
without printing credentials.

The Gateway runs at most MAX_CONCURRENT_TASKS player requests at once, so more readers than
that fill its lane and push the Gateway past LOAD_CRITICAL_PERCENT. The Map replicas share
the requests the Gateway admits; to see them cross the threshold as well, lower
LOAD_CRITICAL_PERCENT in .env and recreate service-discovery."""
import argparse
import collections
import datetime
import json
import subprocess
import threading
import time
import lab
from smoke import USERS, request
from smoke_discovery import MAP, call

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--readers', type=int, default=96)
    parser.add_argument('--seconds', type=int, default=15, help='how long to read; cover a few health polls')
    args = parser.parse_args()
    values = lab.environment()
    interval = int(values.get('HEALTH_CHECK_INTERVAL_SECONDS', '5'))
    token = request(USERS, 'POST', '/v1/auth/login', {'email': 'alice@demo.invalid', 'password': values['SEED_PASSWORD']})['accessToken']
    since = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    statuses, served = collections.Counter(), collections.Counter()
    lock = threading.Lock()
    deadline = time.monotonic() + args.seconds

    def read():
        while time.monotonic() < deadline:
            status, headers = call(MAP + '/v1/map/nearby', token)
            with lock:
                statuses[status] += 1
                served[headers.get('X-Served-By', 'gateway')] += 1

    readers = [threading.Thread(target=read) for _ in range(args.readers)]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join()
    # Two more polls see the load fall again.
    time.sleep(2 * interval + 1)

    logs = lab.compose('logs', '--no-log-prefix', '--since', since, 'service-discovery',
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode()
    alerts = [a for a in (json.loads(line) for line in logs.splitlines() if line.startswith('{')) if a.get('alert') == 'load.critical']
    print('Answers:', dict(sorted(statuses.items())))
    print('Served by:', dict(sorted(served.items())))
    for alert in alerts:
        print(f"{alert['time']} {alert['msg']}: {alert['targetService']}/{alert['targetInstance']}",
              f"at {alert['percent']}% (threshold {alert['thresholdPercent']}%)" if 'percent' in alert else '')
    assert any(not a.get('cleared') for a in alerts), 'No load reached LOAD_CRITICAL_PERCENT; add --readers or lower the threshold'
    print('Service Discovery saw critical load and alerted on it.')

if __name__ == '__main__':
    main()
