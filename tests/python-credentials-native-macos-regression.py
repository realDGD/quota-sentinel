"""Native Keychain synthetic roundtrip; never reads existing user items."""
import os
from pathlib import Path
import sys
import unittest
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
if sys.platform!='darwin':
 print('UNVERIFIED: requires native macOS');raise SystemExit(77)
import pwd
from quota_sentinel.config import CredentialReference
from quota_sentinel.platform.credentials import CredentialStore

class Native(unittest.TestCase):
 def test_roundtrip_update_and_cleanup(self):
  environment=dict(os.environ);environment.pop('QUOTA_SENTINEL_KEYCHAIN_DISABLED',None)
  # The native Keychain is bound to the logged-in account. A fake HOME tests
  # an unavailable password store rather than the actual native API. Only this
  # synthetic-service worker uses the real session home; all fixtures stay isolated.
  environment['HOME']=pwd.getpwuid(os.getuid()).pw_dir
  store=CredentialStore(environment=environment)
  reference=CredentialReference('system','quota-sentinel.synthetic-test.'+uuid.uuid4().hex)
  try:
   store.write(reference,'synthetic-one',timeout=5)
   self.assertEqual(store.read(reference,timeout=5),'synthetic-one')
   store.write(reference,'synthetic-two',timeout=5)
   self.assertEqual(store.read(reference,timeout=5),'synthetic-two')
  finally:
   store.delete(reference,timeout=5)

if __name__=='__main__':unittest.main()
