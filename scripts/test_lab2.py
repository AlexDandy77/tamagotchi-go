"""Ensure the deployment audit catches REST bypasses without exposing secrets."""
import json
import unittest
from check_lab2 import audit
from lab import SERVICES


def compliant():
    services = {name: {'image': 'owner/' + name + ':latest', 'pull_policy': 'always',
                      'environment': {'TASK_TIMEOUT_SECONDS': '5', 'MAX_CONCURRENT_TASKS': '64',
                                      'GATEWAY_ONLY': 'true', 'GATEWAY_CERT_FILE': '/run/tls/gateway.pem',
                                      'USER_MANAGEMENT_URL': 'https://gateway:8443/services/user-management'}}
                for name in (*SERVICES, 'gateway')}
    services['gateway']['environment'].update(UPSTREAMS_JSON=json.dumps({s: 'https://' + s + ':8443' for s in SERVICES}), READY_SERVICES=','.join(SERVICES), MAX_CONCURRENT_SERVICE_TASKS='64', SOCKET_TICKETS_ENABLED='true')
    services['monster-raid']['environment']['SOCKET_TICKETS_ENABLED'] = 'true'
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

    def test_rejects_legacy_url_settings_even_when_they_use_gateway(self):
        config = compliant()
        config['services']['monster-raid']['environment']['REGISTRY_INTERNAL_URL'] = 'https://gateway:8443/services/package-registry'
        config['services']['gateway']['environment']['JWKS_URL'] = 'https://user-management:8443/.well-known/jwks.json'
        failures = '\n'.join(audit(config))
        self.assertIn('monster-raid: REGISTRY_INTERNAL_URL must use a single', failures)
        self.assertIn('gateway: JWKS_URL must use a single', failures)

    def test_socket_destinations_require_tickets_exactly_when_the_gateway_issues_them(self):
        message = 'monster-raid: SOCKET_TICKETS_ENABLED must match the Gateway, which issues the tickets'
        config = compliant()
        del config['services']['monster-raid']['environment']['SOCKET_TICKETS_ENABLED']
        self.assertIn(message, audit(config))
        # Without the setting the Gateway still issues tickets.
        del config['services']['gateway']['environment']['SOCKET_TICKETS_ENABLED']
        self.assertIn(message, audit(config))

        config = compliant()
        config['services']['gateway']['environment']['SOCKET_TICKETS_ENABLED'] = 'false'
        self.assertIn(message, audit(config))
        config['services']['monster-raid']['environment']['SOCKET_TICKETS_ENABLED'] = 'false'
        self.assertEqual(audit(config), [])

    def test_requires_gateway_service_budget(self):
        config = compliant()
        del config['services']['gateway']['environment']['MAX_CONCURRENT_SERVICE_TASKS']
        self.assertIn('gateway: service task budget is not configured', audit(config))
