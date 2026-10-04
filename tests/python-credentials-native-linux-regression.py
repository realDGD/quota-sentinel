"""Explicit Linux password service, in a synthetic desktop/session fixture."""
import os
from pathlib import Path
import sys
import unittest
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
backend=os.environ.get('QUOTA_SENTINEL_TEST_LINUX_CREDENTIAL_BACKEND')
if sys.platform!='linux' or backend not in ('secret-service','kwallet'):
 print('UNVERIFIED: requires native Linux and an explicit synthetic password-service session');raise SystemExit(77)
from quota_sentinel.config import CredentialReference
from quota_sentinel.platform.credentials import CredentialStore,CredentialUnavailable

class Native(unittest.TestCase):
 def test_roundtrip_and_cleanup(self):
  environment=dict(os.environ);environment.pop('QUOTA_SENTINEL_KEYCHAIN_DISABLED',None)
  store=CredentialStore(environment=environment)
  ref=CredentialReference('system',backend+':synthetic-'+uuid.uuid4().hex);created=False
  try:
   store.write(ref,'synthetic-only',timeout=5);created=True
   self.assertEqual(store.read(ref,timeout=5),'synthetic-only')
  finally:
   if created:store.delete(ref,timeout=5)
  with self.assertRaises(CredentialUnavailable):store.read(ref,timeout=5)

if __name__=='__main__':unittest.main()
