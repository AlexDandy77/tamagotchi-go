"""check_lab5.py: the Lab 5 Compose audit flags each missing piece."""
import copy
import unittest

import check_lab5

SPILO = ('bootstrap:\n  dcs:\n    synchronous_mode: true\n    synchronous_mode_strict: true\n    failsafe_mode: true\n'
         '    postgresql:\n      parameters:\n        max_prepared_transactions: 64\n')


def good():
    volume = lambda target: {'type': 'volume', 'target': target}
    node = lambda: {'environment': {'SPILO_CONFIGURATION': SPILO}, 'volumes': [volume('/home/postgres/pgdata')],
                    'stop_grace_period': '1m0s', 'healthcheck': {}}
    member = lambda: {'command': ['mongod', '--replSet', 'rs0']}
    cache = lambda: {'image': 'valkey/valkey:8.1.3', 'command': ['sh', '-c', 'exec valkey-server --save "" --requirepass x']}
    cache_env = {'CACHE_NODES': 'cache-1=cache-1:6379,cache-2=cache-2:6379,cache-3=cache-3:6379', 'CACHE_PASSWORD': 'x'}
    return {'services': {
        'etcd': {}, 'postgres-1': node(), 'postgres-2': node(), 'postgres-3': node(),
        'haproxy': {'networks': {'default': {'aliases': ['postgres', 'postgres-replicas']}},
                    'ports': [{'host_ip': '127.0.0.1', 'published': '7000'}]},
        'mongo': member(), 'mongo-2': member(), 'mongo-3': member(),
        'package-registry': {'environment': {
            'MONGODB_URL': 'mongodb://r@mongo:27017,mongo-2:27017,mongo-3:27017/registry?replicaSet=rs0&w=majority', **cache_env}},
        'cache-1': cache(), 'cache-2': cache(), 'cache-3': cache(),
        'tamagotchi': {'environment': dict(cache_env), 'depends_on': {'haproxy': {}}},
        'notification': {'environment': dict(cache_env)},
        **{name: {'environment': dict(cache_env)} for name in ('user-management', 'battle', 'guild', 'guild-2', 'guild-3', 'map',
                                                               'monster-raid', 'monster-raid-2', 'monster-raid-3')},
        'warehouse': {'volumes': [volume('/var/lib/postgresql/data')], 'ports': [{'host_ip': '127.0.0.1', 'published': '5434'}]},
        'etl': {'environment': {'ETL_DB_PASSWORD': 'x'}, 'depends_on': {'warehouse': {}, 'haproxy': {}}},
    }}


class AuditTests(unittest.TestCase):
    def test_the_repository_files_and_a_complete_config_pass(self):
        self.assertEqual(check_lab5.audit(good()), [])

    def test_each_gap_is_reported(self):
        cases = {
            'postgres: the single server': lambda s: s.update(postgres={}),
            'synchronous_mode_strict: true': lambda s: s['postgres-2']['environment'].update(SPILO_CONFIGURATION='x'),
            'stop_grace_period': lambda s: s['postgres-1'].update(stop_grace_period='10s'),
            'network aliases': lambda s: s['haproxy']['networks']['default'].update(aliases=['postgres']),
            '127.0.0.1:7000 only': lambda s: s['haproxy'].update(ports=[{'host_ip': '0.0.0.0', 'published': '7000'}]),
            'not the removed postgres': lambda s: s['tamagotchi'].update(depends_on={'postgres': {}}),
            'mongo-3: replica-set member': lambda s: s.pop('mongo-3'),
            'w=majority': lambda s: s['package-registry']['environment'].update(MONGODB_URL='mongodb://mongo/registry'),
            'without persistence': lambda s: s['cache-2'].update(command=['valkey-server']),
            'notification: CACHE_NODES': lambda s: s['notification']['environment'].update(CACHE_NODES='cache-1=cache-1:6379'),
            'monster-raid-2: CACHE_NODES': lambda s: s['monster-raid-2']['environment'].pop('CACHE_PASSWORD'),
            'max_prepared_transactions: 64': lambda s: s['postgres-3']['environment'].update(SPILO_CONFIGURATION=SPILO.split('    postgresql')[0]),
            'must list every member': lambda s: s['package-registry']['environment'].update(
                MONGODB_URL='mongodb://r@mongo:27017,mongo-2:27017/registry?replicaSet=rs0&w=majority'),
            'warehouse: must be published': lambda s: s['warehouse'].update(ports=[]),
            'etl: the ETL job is missing': lambda s: s.pop('etl'),
        }
        for expected, damage in cases.items():
            with self.subTest(expected):
                config = copy.deepcopy(good())
                damage(config['services'])
                failures = check_lab5.audit(config)
                self.assertTrue(any(expected in f for f in failures), failures)

    def test_durations(self):
        self.assertEqual(check_lab5.seconds('1m0s'), 60)
        self.assertEqual(check_lab5.seconds('1h2m3s'), 3723)
        self.assertEqual(check_lab5.seconds(None), 0)


if __name__ == '__main__':
    unittest.main()
