"""Check upgrade hints without reading or changing real local credentials."""

import contextlib
import io
import os
from pathlib import Path
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

class StartupImagesTests(unittest.TestCase):
    def test_startup_refreshes_all_service_repositories_and_preserves_credentials(self):
        values = {s.upper().replace('-', '_') + '_IMAGE': 'registry:5000/team/' + s + ':2.0.0'
                  for s in (*lab.SERVICES, 'gateway')}
        values['SEED_PASSWORD'] = 'never-display-this'
        output = io.StringIO()
        with patch.object(lab, 'environment', return_value=values), patch.object(lab, 'compose') as compose, patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
            lab.refresh_images()
            for s in (*lab.SERVICES, 'gateway'):
                self.assertEqual(os.environ[s.upper().replace('-', '_') + '_IMAGE'], 'registry:5000/team/' + s + ':latest')
            self.assertNotIn('SEED_PASSWORD', os.environ)
            compose.assert_called_once_with('pull', '--policy', 'always')
        self.assertNotIn(values['SEED_PASSWORD'], output.getvalue())

    def test_missing_release_aborts_startup(self):
        import subprocess
        with patch.object(lab, 'refresh_images', side_effect=subprocess.CalledProcessError(1, 'pull')), patch.object(lab, 'compose') as compose, patch('sys.argv', ['lab.py', 'up']):
            with self.assertRaises(subprocess.CalledProcessError):
                lab.main()
            compose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
