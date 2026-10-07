"""Ensure the deployment audit catches REST bypasses without exposing secrets."""
import json
import unittest
from check_lab2 import audit
from lab import SERVICES


def compliant():
    services = {name: {'image': 'owner/' + name + ':latest', 'pull_policy': 'always',
                      'environment': {'TASK_TIMEOUT_SECONDS': '5', 'MAX_CONCURRENT_TASKS': '64',
                                      'GATEWAY_ONLY': 'true', 'GATEWAY_CERT_FILE': '/run/tls/gateway.pem',
                                      'JWKS_URL': 'https://gateway:8443/services/user-management/.well-known/jwks.json'}}
                for name in (*SERVICES, 'gateway')}
    services['gateway']['environment'].update(UPSTREAMS_JSON=json.dumps({s: 'https://' + s + ':8443' for s in SERVICES}), READY_SERVICES=','.join(SERVICES))
    return {'services': services}


class DeploymentTests(unittest.TestCase):
    def test_complete_configuration_and_direct_socket_port(self):
        config = compliant()
        config['services']['guild']['ports'] = [{'target': 8080}]
        self.assertEqual(audit(config), [])

    def test_detects_direct_calls_compatibility_and_stale_images(self):
        config = compliant()
        service = config['services']['battle']
        service['image'] = 'owner/battle:2.0.0'
        service['ports'] = [{'target': 8080}]
        service['environment']['REGISTRY_URL'] = 'http://package-registry:8080/private-secret'
        service['environment']['GATEWAY_ALLOW_DIRECT'] = 'true'
        failures = '\n'.join(audit(config))
        for text in ('does not use latest', 'direct REST port', 'bypasses Gateway', 'compatibility'):
            self.assertIn(text, failures)
        self.assertNotIn('private-secret', failures)

    def test_detects_missing_service_limits_and_upstream(self):
        config = compliant()
        del config['services']['notification']
        env = config['services']['guild']['environment']
        env.pop('TASK_TIMEOUT_SECONDS')
        env.pop('GATEWAY_CERT_FILE')
        env['GATEWAY_ONLY'] = 'false'
        config['services']['gateway']['environment']['UPSTREAMS_JSON'] = '{}'
        failures = '\n'.join(audit(config))
        for text in ('missing from Compose', 'TASK_TIMEOUT_SECONDS', 'Gateway verification', 'Gateway-only', 'missing Gateway'):
            self.assertIn(text, failures)
