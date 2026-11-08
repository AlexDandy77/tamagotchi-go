"""Ensure the Lab 4 audit catches a missing broker, broker routing and monitoring gaps."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from check_lab4 import audit

ROOT = Path(__file__).resolve().parents[1]


def volume(target, kind='bind'):
    return {'type': kind, 'target': target}


def compliant():
    tls = {'SERVICE_TLS_CERT_FILE': '/run/tls/message-broker.pem',
           'SERVICE_TLS_KEY_FILE': '/run/tls/message-broker-key.pem',
           'SERVICE_TLS_CA_FILE': '/run/tls/ca.pem', 'GATEWAY_CERT_FILE': '/run/tls/gateway.pem'}
    services = {
        'message-broker': {
            'image': 'owner/pad-team-8-message-broker:latest', 'pull_policy': 'always',
            'healthcheck': {'test': ['CMD', 'python', '-m', 'broker', 'healthcheck']},
            'environment': {**tls, 'SERVICE_DISCOVERY_URL': 'https://service-discovery:8443',
                            'LOKI_URL': 'http://loki:3100'},
            'volumes': [volume('/data', 'volume'), *(volume(path) for path in tls.values())],
        },
        'gateway': {'environment': {
            'BROKER_URL': 'https://message-broker:8443',
            'BROKER_SERVICES': 'user-management,battle,tamagotchi,notification,message-broker',
            'UPSTREAMS_JSON': json.dumps({'message-broker': 'https://message-broker:8443'}),
        }},
        'alloy': {'volumes': [volume('/var/run/docker.sock')]},
        'prometheus': {}, 'loki': {}, 'grafana': {},
    }
    for name in ('user-management', 'battle', 'tamagotchi', 'notification'):
        services[name] = {
            'environment': {'BROKER_URL': 'message-broker:50051', 'BROKER_CERT_FILE': '/run/tls/message-broker.pem',
                            'GATEWAY_ONLY': 'true', 'GRPC_PORT': '50051'},
            'volumes': [volume('/run/tls/message-broker.pem')],
        }
    return {'services': services}


class Lab4AuditTests(unittest.TestCase):
    def test_the_common_deployment_files_pass(self):
        self.assertEqual(audit(compliant(), ROOT), [])

    def test_empty_broker_services_means_every_service(self):
        config = compliant()
        config['services']['gateway']['environment']['BROKER_SERVICES'] = ''
        self.assertEqual(audit(config, ROOT), [])

    def test_a_missing_broker_is_reported_alone(self):
        config = compliant()
        del config['services']['message-broker']
        self.assertEqual(audit(config, ROOT), ['message-broker: service missing'])

    def test_broker_gaps(self):
        config = compliant()
        broker = config['services']['message-broker']
        broker.update(image='owner/broker:4.0.0', build='.')
        broker['volumes'] = [v for v in broker['volumes'] if v['target'] != '/data']
        broker['environment'].update(SERVICE_DISCOVERY_URL='', LOKI_URL='http://elsewhere:3100',
                                     GATEWAY_CERT_FILE='/not/mounted.pem')
        del broker['healthcheck']
        failures = audit(config, ROOT)
        for text in ('latest published image', 'not build', 'named volume on /data', 'GATEWAY_CERT_FILE',
                     'SERVICE_DISCOVERY_URL', 'LOKI_URL', 'healthcheck'):
            self.assertTrue(any(text in f for f in failures), text)

    def test_routing_and_migrated_service_gaps(self):
        config = compliant()
        services = config['services']
        services['gateway']['environment'].update(BROKER_URL='', BROKER_SERVICES='battle', UPSTREAMS_JSON='{}')
        services['tamagotchi']['environment'].update(BROKER_URL='kafka:9092', GATEWAY_ONLY='false', GRPC_PORT='')
        services['notification']['volumes'] = []
        failures = audit(config, ROOT)
        for text in ('gateway: BROKER_URL', 'UPSTREAMS_JSON', 'BROKER_SERVICES must include migrated tamagotchi',
                     'tamagotchi: BROKER_URL', 'GATEWAY_ONLY', 'GRPC_PORT', 'notification: BROKER_CERT_FILE'):
            self.assertTrue(any(text in f for f in failures), text)

    def test_monitoring_gaps(self):
        config = compliant()
        del config['services']['grafana']
        config['services']['alloy']['volumes'] = []
        with tempfile.TemporaryDirectory() as empty:
            failures = audit(copy.deepcopy(config), Path(empty))
        for text in ('grafana: monitoring service missing', 'Docker socket', 'prometheus.yml',
                     'config.alloy', 'dashboard is missing'):
            self.assertTrue(any(text in f for f in failures), text)


if __name__ == '__main__':
    unittest.main()
