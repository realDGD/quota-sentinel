"""Credential Manager and private file checks on an actual Windows session."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
if os.name!='nt':
 print('UNVERIFIED: requires native Windows');raise SystemExit(77)
from quota_sentinel.config import CredentialReference
from quota_sentinel.platform.credentials import CredentialStore,CredentialUnavailable

class Native(unittest.TestCase):
 def test_roundtrip_and_cleanup(self):
  environment=dict(os.environ);environment.pop('QUOTA_SENTINEL_KEYCHAIN_DISABLED',None)
  store=CredentialStore(environment=environment)
  ref=CredentialReference('system','synthetic-'+uuid.uuid4().hex);created=False
  try:
   store.write(ref,'synthetic-only',timeout=5);created=True
   self.assertEqual(store.read(ref,timeout=5),'synthetic-only')
  finally:
   if created:store.delete(ref,timeout=5)
  with self.assertRaises(CredentialUnavailable):store.read(ref,timeout=5)
 def test_private_file_native_acl(self):
  with tempfile.TemporaryDirectory() as root:
   store=CredentialStore();ref=CredentialReference('file',str(Path(root)/'private token'))
   store.write(ref,'synthetic-only',timeout=5);self.assertEqual(store.read(ref,timeout=5),'synthetic-only')

if __name__=='__main__':unittest.main()
