"""Guard the live-check tools' routing, cleanup and alert/recovery assertions without Docker."""
import collections
import datetime
import subprocess
import unittest
from unittest.mock import call, patch
import load_discovery as load
import smoke_discovery as smoke


class RoutingChecks(unittest.TestCase):
    def test_safe_reads_verify_each_actual_replica(self):
        for service in smoke.REPLICAS:
            instances = [f'{service}-{i}' for i in range(3)]
            replies = [(200, {'X-Served-By': service + '/' + name}) for name in instances]
            with patch.object(smoke, 'pool', return_value={'instances': instances}), patch.object(smoke, 'call', side_effect=replies) as request:
                served = smoke.verify_spread('not-printed', service, 3)
            self.assertEqual(set(served), {service + '/' + name for name in instances})
            self.assertTrue(all(c.args == (smoke.read_url(service), 'not-printed') for c in request.call_args_list))

    def test_static_and_wrong_service_headers_do_not_pass_replica_coverage(self):
        for value in ('battle/static', 'map/map-1', ''):
            with patch.object(smoke, 'call', return_value=(200, {'X-Served-By': value})):
                with self.assertRaisesRegex(AssertionError, 'discovered replica'):
                    smoke.spread('not-printed', 1, 'battle')

    def test_http_error_is_not_counted_as_successful_replica_read(self):
        with patch.object(smoke, 'call', return_value=(503, {})):
            with self.assertRaisesRegex(AssertionError, 'battle answered 503'):
                smoke.spread('not-printed', 1, 'battle')

    def test_replica_collection_includes_each_named_raid_socket_instance(self):
        with patch.object(smoke, 'output', return_value='id-1\nid-2\nid-3') as output:
            self.assertEqual(smoke.containers('monster-raid'), ['id-1', 'id-2', 'id-3'])
        output.assert_called_once_with('ps', '-q', 'monster-raid', 'monster-raid-2', 'monster-raid-3')


class DisruptiveChecks(unittest.TestCase):
    def test_full_outage_checks_empty_pool_and_readiness_then_restores_exact_containers(self):
        ids = ['original-1', 'original-2', 'original-3']
        with patch.object(smoke, 'containers', return_value=ids), patch.object(smoke.lab, 'run') as run, \
                patch.object(smoke, 'wait'), patch.object(smoke, 'pool', return_value={'source': 'service-discovery', 'instances': []}), \
                patch.object(smoke, 'call', side_effect=[(503, {}), (503, {})]) as read, \
                patch.object(smoke, 'verify_spread', return_value=collections.Counter({'battle/new-1': 3})) as spread:
            result = smoke.all_replicas_down('not-printed', 'battle')
        self.assertEqual(result['battle/new-1'], 3)
        self.assertEqual([c.args[0] for c in run.call_args_list], [
            ['docker', 'stop', '--time', '15', *ids], ['docker', 'start', *ids]])
        self.assertEqual([c.args[0] for c in read.call_args_list], [smoke.read_url('battle'), smoke.GATEWAY + '/readyz'])
        spread.assert_called_once_with('not-printed', 'battle', 9)

    def test_failed_outage_assertion_still_restores_the_original_containers(self):
        for pool, responses in [
                ({'source': 'static', 'instances': []}, []),
                ({'source': 'service-discovery', 'instances': []}, [(200, {})])]:
            with patch.object(smoke, 'containers', return_value=['original']), patch.object(smoke.lab, 'run') as run, \
                    patch.object(smoke, 'wait'), patch.object(smoke, 'pool', return_value=pool), \
                    patch.object(smoke, 'call', side_effect=responses), patch.object(smoke, 'verify_spread') as spread:
                with self.assertRaises(AssertionError):
                    smoke.all_replicas_down('not-printed', 'battle')
            self.assertEqual(run.call_args.args[0], ['docker', 'start', 'original'])
            spread.assert_not_called()

    def test_socket_empty_pool_cannot_succeed_by_handing_out_a_static_url(self):
        with patch.object(smoke, 'containers', return_value=['raid-id']), patch.object(smoke.lab, 'run') as run, \
                patch.object(smoke, 'wait'), patch.object(smoke, 'pool', return_value={'source': 'service-discovery', 'instances': []}), \
                patch.object(smoke, 'call', side_effect=[(503, {}), (503, {}), (200, {})]):
            with self.assertRaisesRegex(AssertionError, 'socket pool'):
                smoke.all_replicas_down('not-printed', 'monster-raid')
        self.assertEqual(run.call_args.args[0], ['docker', 'start', 'raid-id'])

    def test_discovery_restart_checks_rebuilt_instance_identities(self):
        before = [{'instances': [name + '-1', name + '-2', name + '-3']} for name in smoke.REPLICAS]
        with patch.object(smoke, 'pool', side_effect=[*before, {'instances': ['different-id']}]), \
                patch.object(smoke.lab, 'compose') as compose, patch.object(smoke, 'wait'):
            with self.assertRaisesRegex(AssertionError, 'identities changed'):
                smoke.restart_discovery()
        compose.assert_called_once_with('restart', 'service-discovery')

    def test_wait_retries_transient_restart_errors_but_obeys_deadline(self):
        with patch.object(smoke.time, 'sleep') as sleep, patch.object(smoke.time, 'monotonic', return_value=0):
            answers = iter([OSError(), subprocess.CalledProcessError(1, 'docker'), True])
            def recovered():
                answer = next(answers)
                if isinstance(answer, Exception):
                    raise answer
                return answer
            smoke.wait('restart', recovered, 1)
            self.assertEqual(sleep.call_count, 2)
        with patch.object(smoke.time, 'monotonic', side_effect=[0, 2]), patch.object(smoke.time, 'sleep') as sleep:
            with self.assertRaisesRegex(AssertionError, 'did not happen'):
                smoke.wait('restart', lambda: False, 1)
            sleep.assert_not_called()


class LoadAlertChecks(unittest.TestCase):
    def alert(self, instance='a', cleared=False, service='battle'):
        return {'targetService': service, 'targetInstance': instance, 'cleared': cleared}

    def test_unrelated_service_alert_does_not_satisfy_the_requested_load_test(self):
        self.assertEqual(load.alert_state([self.alert(service='map')], {'battle'}), (set(), set()))

    def test_recovery_must_follow_the_last_raise_and_cover_every_affected_replica(self):
        events = [self.alert(cleared=True), self.alert(), self.alert(instance='b')]
        raised, pending = load.alert_state(events, {'battle'})
        self.assertEqual(raised, {('battle', 'a'), ('battle', 'b')})
        self.assertEqual(pending, raised)
        events.append(self.alert(cleared=True))
        self.assertEqual(load.alert_state(events, {'battle'})[1], {('battle', 'b')})
        events.append(self.alert(instance='b', cleared=True))
        self.assertFalse(load.alert_state(events, {'battle'})[1])

    def test_cooldown_uses_only_raised_alerts_and_never_waits_a_negative_duration(self):
        now = datetime.datetime(2026, 10, 28, 12, 0, 0, tzinfo=datetime.timezone.utc)
        events = [{'time': '2026-10-28T11:59:30Z'}, {'time': '2026-10-28T12:00:00Z', 'cleared': True}]
        self.assertEqual(load.cooldown_remaining(events, 60, now), 31)
        self.assertEqual(load.cooldown_remaining(events, 10, now), 0)
        self.assertEqual(load.cooldown_remaining([], 60, now), 0)


if __name__ == '__main__':
    unittest.main()
