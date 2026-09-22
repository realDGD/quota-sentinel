"""Quota probe serialization uses the same shlock protocol as the shell."""

import tempfile
import unittest
from pathlib import Path

from quota_sentinel.runtime.locks import LockBusyError, acquire_quota_lock


class QuotaLockTests(unittest.TestCase):
    def test_second_owner_is_busy_until_first_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp)
            with acquire_quota_lock(state_dir, timeout=0) as lock:
                self.assertEqual(lock.path, state_dir / "quota.lock")
                self.assertTrue(lock.path.is_file())
                with self.assertRaises(LockBusyError):
                    acquire_quota_lock(state_dir, timeout=0)
            self.assertFalse((state_dir / "quota.lock").exists())
            with acquire_quota_lock(state_dir, timeout=0):
                pass


if __name__ == "__main__":
    unittest.main()
