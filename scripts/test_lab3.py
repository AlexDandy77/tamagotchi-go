"""Ensure the scaling audit catches missing replicas and registration gaps without exposing secrets."""
import copy
import unittest
from check_lab3 import audit
from test_lab2 import compliant as lab2

SD = 'https://service-discovery:8443'


def compliant():
    config = lab2()
    services = config['services']
    services['gateway']['environment'].update(
        SERVICE_DISCOVERY_URL=SD,
        DISCOVERY_MANAGED_SERVICES='user-management,battle,guild,package-registry,map,monster-raid,tamagotchi,notification',
    )
    services['service-discovery'] = {
        'image': 'owner/service-discovery:latest',
        'pull_policy': 'always',
        'healthcheck': {'test': ['CMD', 'python', '-m', 'discovery', 'healthcheck']},
        'environment': {
            'SERVICE_TLS_CERT_FILE': '/run/tls/service-discovery.pem',
            'SERVICE_TLS_KEY_FILE': '/run/tls/service-discovery-key.pem',
            'SERVICE_TLS_CA_FILE': '/run/tls/ca.pem',
            'TASK_TIMEOUT_SECONDS': '5',
            'MAX_CONCURRENT_TASKS': '64',
            'TELEGRAM_BOT_TOKEN': '',
            'TELEGRAM_CHAT_ID': '',
        },
    }
    services['map']['environment']['SERVICE_DISCOVERY_URL'] = SD
    services['map']['deploy'] = {'replicas': 3}
    for scaled in ('user-management', 'battle', 'package-registry', 'tamagotchi', 'notification'):
        services[scaled]['environment']['SERVICE_DISCOVERY_URL'] = SD
        services[scaled]['deploy'] = {'replicas': 3}
    raid = services['monster-raid']
    raid['environment'].update(SERVICE_DISCOVERY_URL=SD, INSTANCE_ID='monster-raid', PUBLIC_SOCKET_URL='ws://localhost:8084')
    raid['ports'] = [{'host_ip': '127.0.0.1', 'published': '8084', 'target': 8080}]
    for n, port in ((2, '8085'), (3, '8086')):
        replica = copy.deepcopy(raid)
        replica['environment'].update(INSTANCE_ID=f'monster-raid-{n}', PUBLIC_SOCKET_URL=f'ws://localhost:{port}')
        replica['ports'] = [{'host_ip': '127.0.0.1', 'published': port, 'target': 8080}]
        services[f'monster-raid-{n}'] = replica
    guild = services['guild']
    guild['environment'].update(SERVICE_DISCOVERY_URL=SD, INSTANCE_ID='guild', PUBLIC_SOCKET_URL='ws://localhost:8087')
    guild['ports'] = [{'host_ip': '127.0.0.1', 'published': '8087', 'target': 8080}]
    for n, port in ((2, '8088'), (3, '8089')):
        replica = copy.deepcopy(guild)
        replica['environment'].update(INSTANCE_ID=f'guild-{n}', PUBLIC_SOCKET_URL=f'ws://localhost:{port}')
        replica['ports'] = [{'host_ip': '127.0.0.1', 'published': port, 'target': 8080}]
        services[f'guild-{n}'] = replica
    return config


class ScalingTests(unittest.TestCase):
    def test_complete_configuration(self):
        self.assertEqual(audit(compliant()), [])

    def test_every_scaled_service_requires_managed_routing(self):
        config = compliant()
        config['services']['gateway']['environment']['DISCOVERY_MANAGED_SERVICES'] = 'map,monster-raid,tamagotchi,notification'
        failures = audit(config)
        self.assertIn('user-management: Gateway must require discovery routing', failures)
        self.assertIn('battle: Gateway must require discovery routing', failures)

    def test_managed_services_cannot_be_unknown_or_unregistered(self):
        config = compliant()
        gateway = config['services']['gateway']['environment']
        gateway['DISCOVERY_MANAGED_SERVICES'] += ',unknown'
        config['services']['guild']['environment'].pop('SERVICE_DISCOVERY_URL')
        failures = audit(config)
        self.assertIn('gateway: unknown discovery-managed service unknown', failures)
        self.assertIn('guild: managed routing requires service registration', failures)

    def test_requires_service_discovery_and_the_gateway_pools(self):
        config = compliant()
        del config['services']['service-discovery']
        config['services']['gateway']['environment'].pop('SERVICE_DISCOVERY_URL')
        failures = audit(config)
        self.assertIn('service-discovery: missing from Compose', failures)
        self.assertIn('gateway: SERVICE_DISCOVERY_URL does not name Service Discovery', failures)

    def test_service_discovery_follows_the_image_and_privacy_rules(self):
        config = compliant()
        discovery = config['services']['service-discovery']
        discovery['image'] = 'owner/service-discovery:3.0.0'
        discovery['pull_policy'] = 'missing'
        discovery['ports'] = [{'published': '8090', 'target': 8080}]
        del discovery['healthcheck']
        del discovery['environment']['SERVICE_TLS_CA_FILE']
        discovery['environment']['TELEGRAM_BOT_TOKEN'] = '123:never-printed-secret'
        failures = '\n'.join(audit(config))
        for text in ('does not use latest', 'does not refresh', 'publishes a host port', 'no healthcheck',
                     'SERVICE_TLS_CA_FILE', 'must be set together'):
            self.assertIn(text, failures)
        self.assertNotIn('never-printed-secret', failures)

    def test_scaled_services_need_enough_instances_that_register(self):
        config = compliant()
        del config['services']['map']['deploy']
        for n in (2, 3):
            del config['services'][f'monster-raid-{n}']
        config['services']['monster-raid']['environment'].pop('SERVICE_DISCOVERY_URL')
        failures = audit(config)
        self.assertIn('map: runs 1 instance(s); Lab 3 needs at least 2', failures)
        self.assertIn('monster-raid: runs 1 instance(s); Lab 3 needs at least 2', failures)
        self.assertIn('monster-raid: SERVICE_DISCOVERY_URL does not name Service Discovery', failures)

    def test_replicas_follow_the_lab2_rules_and_run_one_image(self):
        config = compliant()
        replica = config['services']['monster-raid-2']
        replica['environment']['GATEWAY_ONLY'] = 'false'
        replica['environment']['GUILD_URL'] = 'http://guild:8080'
        replica['image'] = 'owner/monster-raid:2.0.7'
        failures = audit(config)
        for text in ('monster-raid-2: Gateway-only REST is not configured', 'monster-raid-2: GUILD_URL bypasses Gateway',
                     'monster-raid-2: image does not use latest', 'monster-raid: replicas run different images'):
            self.assertIn(text, failures)

    def test_socket_replicas_need_their_own_registered_port(self):
        config = compliant()
        services = config['services']
        services['monster-raid-2']['environment']['PUBLIC_SOCKET_URL'] = 'ws://localhost:9999'
        services['monster-raid-3']['ports'] = [{'host_ip': '127.0.0.1', 'published': '8084', 'target': 8080}]
        services['monster-raid-3']['environment']['PUBLIC_SOCKET_URL'] = 'ws://localhost:8084'
        services['monster-raid-3']['environment']['INSTANCE_ID'] = 'monster-raid'
        failures = audit(config)
        self.assertIn('monster-raid-2: PUBLIC_SOCKET_URL does not match its published port', failures)
        self.assertIn('socket replicas publish the same host port', failures)
        self.assertIn('monster-raid: replicas share an INSTANCE_ID', failures)

    def test_counted_replicas_cannot_share_a_port_or_an_instance_id(self):
        config = compliant()
        services = config['services']
        services['map']['ports'] = [{'published': '8083', 'target': 8080}]
        services['map']['environment']['INSTANCE_ID'] = 'map'
        del services['monster-raid-3']['ports']
        failures = '\n'.join(audit(config))
        for text in ('map: 3 replicas cannot share a published port', 'map: 3 replicas cannot share one INSTANCE_ID',
                     'monster-raid-3: must publish exactly one socket port'):
            self.assertIn(text, failures)

    def test_tamagotchi_notification_and_registry_each_need_two_replicas(self):
        for name in ('tamagotchi', 'notification', 'package-registry'):
            config = compliant()
            config['services'][name]['deploy'] = {'replicas': 1}
            self.assertIn(f'{name}: runs 1 instance(s); Lab 3 needs at least 2', audit(config))

    def test_guild_runs_socket_replicas_on_their_own_ports(self):
        config = compliant()
        services = config['services']
        del services['guild-2']
        del services['guild-3']
        self.assertIn('guild: runs 1 instance(s); Lab 3 needs at least 2', audit(config))
        config = compliant()
        services = config['services']
        services['guild-2']['ports'] = [{'host_ip': '127.0.0.1', 'published': '8086', 'target': 8080}]
        services['guild-2']['environment']['PUBLIC_SOCKET_URL'] = 'ws://localhost:8086'
        services['guild-3']['environment']['PUBLIC_SOCKET_URL'] = 'ws://localhost:8087'
        failures = audit(config)
        self.assertIn('socket replicas publish the same host port', failures)
        self.assertIn('guild-3: PUBLIC_SOCKET_URL does not match its published port', failures)

    def test_other_services_may_register_only_with_service_discovery(self):
        config = compliant()
        config['services']['battle']['environment']['SERVICE_DISCOVERY_URL'] = 'https://elsewhere:8443'
        self.assertIn('battle: SERVICE_DISCOVERY_URL does not name Service Discovery', audit(config))
        config['services']['battle']['environment']['SERVICE_DISCOVERY_URL'] = SD
        self.assertEqual(audit(config), [])
