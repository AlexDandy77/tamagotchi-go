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


if __name__ == "__main__":
    unittest.main()
