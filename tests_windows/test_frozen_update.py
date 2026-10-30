import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from gateway.update_helper import apply_update, wait_for_parent
from gateway.tests.test_updates import signing_fixture


@unittest.skipUnless(os.name == 'nt' and os.environ.get('TEST_FROZEN_EXE'), 'Requires packaged Windows application')
class FrozenUpdateTests(unittest.TestCase):
    def test_replacement_boots_real_exe_and_acknowledges_version(self):
        binaries = {'Parmakizi.exe': Path(os.environ['TEST_FROZEN_EXE']).read_bytes(), 'ParmakiziUpdater.exe': Path(os.environ['TEST_FROZEN_UPDATER']).read_bytes()}
        public, _, data, sign = signing_fixture(binaries)
        child_pid = None
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stage = root / '.parmakizi-updates' / 'windows-test'
            stage.mkdir(parents=True)
            for name, content in binaries.items():
                (stage / name).write_bytes(content)
                (root / name).write_bytes(('previous-' + name).encode())
            (root / 'local-user-file.txt').write_text('Keep this')
            job = stage / 'job.json'
            job.write_text(json.dumps({'envelope': sign(data), 'target_dir': str(root), 'stage_dir': str(stage), 'parent_pid': os.getpid()}))
            try:
                self.assertTrue(apply_update(job, wait_parent=False, public_key=public, boot_timeout=30))
                result = json.loads((stage / 'result.json').read_text())
                child_pid = result['pid']
                self.assertTrue(result['ok'])
                self.assertEqual((root / 'local-user-file.txt').read_text(), 'Keep this')
                self.assertEqual(hashlib.sha256((root / 'Parmakizi.exe').read_bytes()).hexdigest(), data['files'][0]['sha256'])
            finally:
                if child_pid:
                    subprocess.run(['taskkill', '/PID', str(child_pid), '/T', '/F'], check=True, stdout=subprocess.DEVNULL)
                    wait_for_parent(child_pid, timeout=10)

    def test_native_parent_wait(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.3)'])
        wait_for_parent(child.pid, timeout=10)
        self.assertIsNotNone(child.poll())


if __name__ == '__main__':
    unittest.main()
