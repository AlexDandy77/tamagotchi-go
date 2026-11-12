"""Check how demo_lab4 starts its throwaway broker-image container, without Docker."""
import os
import subprocess
import types
import unittest
from unittest.mock import patch

import demo_lab4


class InsideTests(unittest.TestCase):
    def run_inside(self):
        stack = {'name': 'tamagotchi-lab1', 'services': {'message-broker': {'image': 'owner/broker:latest'}}}
        done = subprocess.CompletedProcess([], 0, stdout='ok\n', stderr='')
        with patch.object(demo_lab4, 'config', return_value=stack), \
                patch.object(demo_lab4.subprocess, 'run', return_value=done) as run:
            self.assertEqual(demo_lab4.inside('print("ok")', {'TOPIC': 'pet.used.v1'}), 'ok\n')
        return run.call_args.args[0]

    @unittest.skipUnless(hasattr(os, 'getuid'), 'POSIX hosts only')
    def test_reads_the_private_tls_directory_as_the_host_user(self):
        command = self.run_inside()
        self.assertIn(f"{demo_lab4.ROOT / '.secrets' / 'tls'}:/run/tls:ro", command)
        user = command.index('--user')
        self.assertEqual(command[user + 1], f'{os.getuid()}:{os.getgid()}')
        self.assertEqual(command[-2:], ['owner/broker:latest', '-'])
        self.assertIn('TOPIC=pet.used.v1', command)

    def test_keeps_the_image_user_where_the_host_has_no_uid(self):
        # Windows has no os.getuid; Docker Desktop's mounts are readable there anyway.
        with patch.object(demo_lab4, 'os', types.SimpleNamespace()):
            self.assertNotIn('--user', self.run_inside())


if __name__ == '__main__':
    unittest.main()
