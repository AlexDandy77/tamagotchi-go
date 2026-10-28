"""Exercise leader proof and exact-container restoration without Docker or network calls."""
import contextlib
import io
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, call, patch

import leader_failover as leader

START = '2026-10-28T10:00:00Z'
A = leader.Container('id-a', 'battle-a', START)
B = leader.Container('id-b', 'battle-b', START)
C = leader.Container('id-c', 'battle-c', START)


def line(message, time='2026-10-28T10:01:00Z', instance='battle-a', service='battle'):
    return json.dumps({'msg': message, 'time': time, 'instanceId': instance, 'service': service})


def role(active, seconds=0):
    return leader.Role(active, (datetime(2026, 10, 28, 10, 1, seconds, tzinfo=timezone.utc), seconds))


class LeaderLogTests(unittest.TestCase):
    def test_scope_and_latest_role_exclude_old_processes_and_other_instances(self):
        logs = '\n'.join([
            line('worker leader acquired', time='2026-10-28T09:00:00Z'),
            line('service started', time=START),
            line('worker leader acquired', time='2026-10-28T10:01:00Z'),
            line('worker leader acquired', instance='old-container'),
            line('worker leader acquired', service='user-management'),
            line('worker leader released', time='2026-10-28T10:02:00Z'),
            line('ordinary request', time='2026-10-28T10:03:00Z'),
        ])
        self.assertFalse(leader.current_role(logs, 'battle', A).leader)
        # Release/acquire events may be read out of order; timestamps decide the latest role.
        logs += '\n' + line('worker leader acquired', time='2026-10-28T10:01:30Z')
        self.assertFalse(leader.current_role(logs, 'battle', A).leader)
        logs += '\n' + line('worker leader acquired', time='2026-10-28T10:04:00Z')
        self.assertTrue(leader.current_role(logs, 'battle', A).leader)

    def test_connection_loss_and_a_new_start_clear_previous_leadership(self):
        logs = '\n'.join([line('service started', time=START), line('worker leader acquired'),
                          line('worker leader connection lost', time='2026-10-28T10:02:00Z')])
        self.assertFalse(leader.current_role(logs, 'battle', A).leader)
        logs += '\n' + line('service started', time='2026-10-28T10:03:00Z')
        self.assertFalse(leader.current_role(logs, 'battle', A).leader)

    def test_missing_startup_and_invalid_timestamps_cannot_claim_success(self):
        with self.assertRaisesRegex(AssertionError, 'startup'):
            leader.current_role(line('worker leader acquired'), 'battle', A)
        with self.assertRaisesRegex(AssertionError, 'timestamp'):
            leader.current_role(line('worker leader acquired', time=None), 'battle', A)
        for state in ({A.id: role(False), B.id: role(False)}, {A.id: role(True), B.id: role(True)}):
            with self.assertRaisesRegex(AssertionError, 'one current worker leader'):
                leader.sole_leader([A, B], state)


class TakeoverTests(unittest.TestCase):
    def scenario(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        running = stack.enter_context(patch.object(leader, 'running', side_effect=[[B, A, C], [B, C]]))
        roles = stack.enter_context(patch.object(leader, 'roles', side_effect=[
            {A.id: role(True), B.id: role(False), C.id: role(False)},
            {B.id: role(True, 1), C.id: role(False)},
        ]))
        stack.enter_context(patch.object(leader, 'routing_pool', return_value={'source': 'service-discovery', 'instances': ['battle-b', 'battle-c']}))
        stack.enter_context(patch.object(leader.discovery, 'pool', return_value={'source': 'service-discovery', 'instances': ['battle-a', 'battle-b', 'battle-c']}))
        run = stack.enter_context(patch.object(leader.lab, 'run'))
        wait = stack.enter_context(patch.object(leader.discovery, 'wait', side_effect=lambda _, condition, __: self.assertTrue(condition())))
        return run, wait, running, roles

    def test_stop_actual_leader_and_yield_only_proven_alternate_then_restore(self):
        run, wait, _, _ = self.scenario()
        with leader.stopped_worker_leader() as evidence:
            self.assertEqual(evidence['stoppedInstance'], 'battle-a')
            self.assertEqual(evidence['leaderInstance'], 'battle-b')
            self.assertEqual(run.call_count, 1)  # no restart before the caller's business work
        self.assertEqual(run.call_args_list[0].args[0], ['docker', 'stop', '--time', '15', 'id-a'])
        self.assertEqual(run.call_args_list[-1].args[0], ['docker', 'start', 'id-a'])
        wait.assert_called_once()

    def test_always_restore_if_business_work_fails(self):
        run, wait, _, _ = self.scenario()
        with self.assertRaisesRegex(RuntimeError, 'caller failed'):
            with leader.stopped_worker_leader():
                raise RuntimeError('caller failed')
        self.assertEqual(run.call_args.args[0], ['docker', 'start', 'id-a'])
        wait.assert_called_once()

    def test_restore_if_stop_reports_failure_after_possibly_stopping(self):
        run, wait, _, _ = self.scenario()
        run.side_effect = [RuntimeError('stop interrupted'), None]
        with self.assertRaisesRegex(RuntimeError, 'stop interrupted'):
            with leader.stopped_worker_leader():
                self.fail('unproven takeover reached caller')
        self.assertEqual(run.call_args.args[0], ['docker', 'start', 'id-a'])
        wait.assert_called_once()

    def test_no_new_leadership_event_is_not_a_takeover(self):
        run, _, _, roles = self.scenario()
        roles.side_effect = [{A.id: role(True), B.id: role(False, 1), C.id: role(False)},
                             {B.id: role(True, 1), C.id: role(False)}]
        with self.assertRaisesRegex(AssertionError, 'no new leadership-acquired'):
            with leader.stopped_worker_leader():
                self.fail('stale role reached caller')
        self.assertEqual(run.call_args.args[0], ['docker', 'start', 'id-a'])

    def test_ambiguous_or_disappearing_replicas_restore_without_claiming_success(self):
        for state in ({B.id: role(True, 1), C.id: role(True, 1)},):
            run, _, _, roles = self.scenario()
            roles.side_effect = [{A.id: role(True), B.id: role(False), C.id: role(False)}, state]
            with self.assertRaisesRegex(AssertionError, 'Multiple'):
                with leader.stopped_worker_leader():
                    self.fail('ambiguous leader reached caller')
            self.assertEqual(run.call_args.args[0], ['docker', 'start', 'id-a'])

    def test_takeover_timeout_restores_the_exact_container(self):
        run, _, _, roles = self.scenario()
        roles.side_effect = [{A.id: role(True), B.id: role(False), C.id: role(False)},
                             {B.id: role(False), C.id: role(False)}]
        with patch.object(leader.time, 'monotonic', side_effect=[0.0, 0.0, 31.0]):
            with self.assertRaisesRegex(AssertionError, '30 seconds'):
                with leader.stopped_worker_leader():
                    self.fail('timed-out takeover reached caller')
        self.assertEqual(run.call_args.args[0], ['docker', 'start', 'id-a'])


    def test_initial_ambiguous_leadership_stops_nothing(self):
        run, _, _, roles = self.scenario()
        roles.side_effect = [{A.id: role(True), B.id: role(True), C.id: role(False)}]
        with self.assertRaisesRegex(AssertionError, 'one current worker leader'):
            with leader.stopped_worker_leader():
                self.fail('ambiguous preflight reached caller')
        run.assert_not_called()


class ContainerIdentityTests(unittest.TestCase):
    def test_inspection_uses_running_container_hostnames_and_start_times(self):
        output = Mock(stdout=b'id-a id-b\n')
        inspect = Mock(stdout=('id-a battle-a true ' + START + '\nid-b battle-b false ' + START + '\n').encode())
        with patch.object(leader.lab, 'compose', return_value=output) as compose, patch.object(leader.lab, 'run', return_value=inspect) as run:
            self.assertEqual(leader.running('battle', leader.time.monotonic()+30), [A])
        self.assertEqual(compose.call_args.args, ('ps', '-q', 'battle'))
        self.assertEqual(run.call_args.args[0][-2:], ['id-a', 'id-b'])
        self.assertIn('Config.Hostname', run.call_args.args[0][3])

    def test_log_reads_are_scoped_to_the_inspected_current_process(self):
        output = Mock(stdout=(line('service started', time=START) + '\n' + line('worker leader acquired')).encode())
        with patch.object(leader.lab, 'run', return_value=output) as run:
            self.assertTrue(leader.roles('battle', [A], leader.time.monotonic()+30)[A.id].leader)
        self.assertEqual(run.call_args.args[0], ['docker', 'logs', '--since', START, '--tail', str(leader.MAX_LOG_LINES), A.id])


if __name__ == '__main__':
    unittest.main()
