"""Check upgrade hints without reading or changing real local credentials."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import lab


class SetupTests(unittest.TestCase):
    def test_upgrade_preserves_existing_credentials_and_warns_about_old_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env.example").write_text(
                "SEED_PASSWORD=generated-by-setup\nUSER_MANAGEMENT_IMAGE=users:latest\nGATEWAY_IMAGE=gateway:latest\n"
            )
            original = "SEED_PASSWORD=private-local-test-value\nUSER_MANAGEMENT_IMAGE=users:0.1.0\n"
            (root / ".env").write_text(original)
            output = io.StringIO()
            with patch.object(lab, "ROOT", root), contextlib.redirect_stdout(output):
                lab.env_file()
                after = (root / ".env").read_text()
                lab.env_file()
            self.assertTrue(after.startswith(original))
            self.assertIn("GATEWAY_IMAGE=gateway:latest", after)
            self.assertEqual((root / ".env").read_text(), after)
            self.assertIn(
                "Kept existing image selections: USER_MANAGEMENT_IMAGE",
                output.getvalue(),
            )
            self.assertNotIn("private-local-test-value", output.getvalue())

    def test_environment_honors_overrides_with_custom_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.env"
            path.write_text("SEED_PASSWORD=file-value\n")
            with patch.dict(os.environ, {"SEED_PASSWORD": "override"}, clear=True):
                self.assertEqual(lab.environment(path)["SEED_PASSWORD"], "override")

    def test_notification_is_prepared_in_initial_tls_bundle(self):
        self.assertIn("notification", lab.TLS_SERVICES)
        self.assertIn("notification", lab.NON_ROOT_SERVICES)

    def test_service_discovery_gets_a_readable_certificate_and_its_image_refreshes(self):
        self.assertIn("service-discovery", lab.TLS_SERVICES)
        self.assertIn("service-discovery", lab.NON_ROOT_SERVICES)
        self.assertEqual(lab.IMAGES, (*lab.SERVICES, "gateway", "service-discovery", "message-broker"))
        self.assertIn("message-broker", lab.TLS_SERVICES)
        self.assertIn("message-broker", lab.NON_ROOT_SERVICES)
        # It has no database, so setup neither migrates nor seeds it.
        self.assertNotIn("service-discovery", lab.SERVICES)

class StartupImagesTests(unittest.TestCase):
    def test_startup_refreshes_all_service_repositories_and_preserves_credentials(self):
        values = {s.upper().replace('-', '_') + '_IMAGE': 'registry:5000/team/' + s + ':2.0.0'
                  for s in lab.IMAGES}
        values['SEED_PASSWORD'] = 'never-display-this'
        output = io.StringIO()
        with patch.object(lab, 'environment', return_value=values), patch.object(lab, 'compose') as compose, patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
            lab.refresh_images()
            for s in lab.IMAGES:
                self.assertEqual(os.environ[s.upper().replace('-', '_') + '_IMAGE'], 'registry:5000/team/' + s + ':latest')
            self.assertNotIn('SEED_PASSWORD', os.environ)
            compose.assert_called_once_with('pull', '--policy', 'always')
        self.assertNotIn(values['SEED_PASSWORD'], output.getvalue())

    def test_startup_completes_without_deleted_demo_helper(self):
        with patch.object(lab, 'refresh_images'), patch.object(lab, 'provision'), patch.object(lab, 'compose') as compose, \
                patch.object(lab, 'wait_for_primary') as wait, patch.object(lab, 'ensure_cluster_settings'), \
                patch.object(lab, 'copy_lab4_database') as copy, patch.object(lab, 'grant_etl_reader') as grant, \
                patch('sys.argv', ['lab.py', 'up']):
            lab.main()
        # The data stores start first (Lab 5: the Patroni cluster, HAProxy and the Mongo replica set);
        # every event goes through the Message Broker, so no Kafka.
        self.assertEqual(compose.call_args_list[0].args, ('up', '-d', '--wait', '--pull', 'never', '--remove-orphans', *lab.DATA_SERVICES))
        self.assertIn('haproxy', lab.DATA_SERVICES)
        wait.assert_called_once_with()
        copy.assert_called_once_with()
        grant.assert_called_once_with()  # after migrations, so every service's etl views are readable
        self.assertEqual(compose.call_args.args, ('up', '-d', '--wait', '--pull', 'never', '--remove-orphans'))

    def test_missing_release_aborts_startup(self):
        import subprocess
        with patch.object(lab, 'refresh_images', side_effect=subprocess.CalledProcessError(1, 'pull')), patch.object(lab, 'compose') as compose, patch('sys.argv', ['lab.py', 'up']):
            with self.assertRaises(subprocess.CalledProcessError):
                lab.main()
            compose.assert_not_called()


class RaidJoinDefaultTests(unittest.TestCase):
    def test_moves_the_previous_default_to_two_phase_and_keeps_other_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for before, after in (('compensation', 'two-phase'), ('two-phase', 'two-phase')):
                (root / '.env').write_text(f'SEED_PASSWORD=x\nMONSTER_RAID_JOIN_COMMIT={before}\n')
                with patch.object(lab, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()):
                    lab.raid_join_default()
                self.assertEqual((root / '.env').read_text(), f'SEED_PASSWORD=x\nMONSTER_RAID_JOIN_COMMIT={after}\n')


class DemoAdminTests(unittest.TestCase):
    def test_sets_demo_admin_once_without_changing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = 'SEED_PASSWORD=private-demo-value\nREGISTRY_ADMIN_USER_IDS=\n'
            (root / '.env').write_text(original)
            with patch.object(lab, 'ROOT', root), patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()):
                lab.demo_admin()
                once = (root / '.env').read_text()
                lab.demo_admin()
            self.assertEqual(once, (root / '.env').read_text())
            self.assertIn('SEED_PASSWORD=private-demo-value', once)
            self.assertNotIn('REGISTRY_ADMIN_USER_IDS=\n', once)

    def test_preserves_explicit_admin_in_file_and_process_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value, override in [('custom-admin', {}), ('', {'REGISTRY_ADMIN_USER_IDS': 'override-admin'})]:
                original = 'REGISTRY_ADMIN_USER_IDS=' + value + '\n'
                (root / '.env').write_text(original)
                with patch.object(lab, 'ROOT', root), patch.dict(os.environ, override, clear=True):
                    lab.demo_admin()
                self.assertEqual((root / '.env').read_text(), original)


class ManagedServicesTests(unittest.TestCase):
    CURRENT = 'user-management,battle,guild,package-registry,map,monster-raid,tamagotchi,notification'

    def run_setup(self, root, value):
        (root / '.env.example').write_text('DISCOVERY_MANAGED_SERVICES=' + self.CURRENT + '\n')
        (root / '.env').write_text('SEED_PASSWORD=private-value\nDISCOVERY_MANAGED_SERVICES=' + value + '\n')
        with patch.object(lab, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()):
            lab.managed_services()
        return (root / '.env').read_text()

    def test_moves_the_previous_default_to_every_service(self):
        with tempfile.TemporaryDirectory() as directory:
            after = self.run_setup(Path(directory), lab.PREVIOUS_MANAGED_DEFAULTS[0])
        self.assertEqual(after, 'SEED_PASSWORD=private-value\nDISCOVERY_MANAGED_SERVICES=' + self.CURRENT + '\n')

    def test_keeps_an_explicit_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            for value in ('map,monster-raid', ''):
                after = self.run_setup(Path(directory), value)
                self.assertIn('DISCOVERY_MANAGED_SERVICES=' + value + '\n', after)


if __name__ == "__main__":
    unittest.main()

class BrokerDefaultsTests(unittest.TestCase):
    def test_upgrades_old_defaults_but_preserves_custom_and_final_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env.example').write_text('BROKER_SERVICES=\nBROKER_DESTINATIONS=\n')
            previous = ('tamagotchi,notification,message-broker', 'user-management,battle,tamagotchi,notification,message-broker',
                        'user-management,battle,map,monster-raid,tamagotchi,notification,message-broker',
                        'user-management,battle,guild,package-registry,map,monster-raid,tamagotchi,notification,message-broker')
            for value in [*previous, 'battle', 'user-management,battle', '']:
                original = 'SEED_PASSWORD=private-value\nBROKER_SERVICES=' + value + '\n'
                (root / '.env').write_text(original)
                with patch.object(lab, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()):
                    lab.broker_defaults()
                    once = (root / '.env').read_text()
                    lab.broker_defaults()
                self.assertEqual((root / '.env').read_text(), once)
                self.assertIn('SEED_PASSWORD=private-value', once)
                if value in previous:
                    self.assertEqual(once, 'SEED_PASSWORD=private-value\nBROKER_SERVICES=\n')
                else:
                    self.assertEqual(once, original)
            for value, expected in (('user-management,battle,guild,package-registry,map,monster-raid,tamagotchi,notification', ''),
                                    ('monster-raid', 'monster-raid')):
                (root / '.env').write_text('BROKER_DESTINATIONS=' + value + '\n')
                with patch.object(lab, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()):
                    lab.broker_defaults()
                self.assertEqual((root / '.env').read_text(), 'BROKER_DESTINATIONS=' + expected + '\n')


class Lab4CopyTests(unittest.TestCase):
    def test_roles_can_be_applied_again_and_the_superuser_is_left_alone(self):
        dump = '\n'.join(['CREATE ROLE postgres;', 'ALTER ROLE postgres WITH SUPERUSER;', 'CREATE ROLE users;',
                          "ALTER ROLE users WITH LOGIN PASSWORD 'SCRAM-SHA-256$x';", 'GRANT etl_reader TO users;'])
        sql = lab.tolerant_roles(dump)
        self.assertNotIn('ROLE postgres', sql)
        self.assertIn('DO $$ BEGIN CREATE ROLE users; EXCEPTION WHEN duplicate_object THEN NULL; END $$;', sql)
        self.assertIn("ALTER ROLE users WITH LOGIN PASSWORD 'SCRAM-SHA-256$x';", sql)
        self.assertIn('GRANT etl_reader TO users;', sql)

    def test_a_volume_still_in_use_is_never_copied(self):
        answers = {"SELECT to_regclass('public.lab5_copied_from_lab4') IS NOT NULL": 'f',
                   "SELECT count(*) FROM pg_database WHERE datname NOT IN ('postgres','template0','template1')": '0',
                   "SELECT to_regclass('public.lab5_copy_started') IS NOT NULL": 'f'}

        def fake_run(args, **kwargs):
            if args[:3] == ['docker', 'ps', '-q']:
                return subprocess.CompletedProcess(args, 0, stdout=b'abc123\n')
            return subprocess.CompletedProcess(args, 0, stdout=b'')

        with patch.object(lab, 'psql', side_effect=lambda sql, **kw: answers.get(sql)), \
                patch.object(lab, 'run', side_effect=fake_run) as run:
            with self.assertRaises(SystemExit) as stopped:
                lab.copy_lab4_database()
        self.assertIn('still in use by container abc123', str(stopped.exception))
        self.assertFalse(any(call.args[0][:3] == ['docker', 'run', '-d'] for call in run.call_args_list))
