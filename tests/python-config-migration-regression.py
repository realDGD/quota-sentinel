import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import *
from quota_sentinel.config.migration import resolve_config,capture_legacy_config
from quota_sentinel.state.new_installation import initialize_new_installation
from quota_sentinel.state.authority import read_authority,AuthorityMissingError
class MigrationTests(unittest.TestCase):
 def test_oversized_integer_override_keeps_saved_preference(self):
  c=capture_legacy_config({'QUOTA_SENTINEL_AGY_TRANSIENT_RETRIES':str(10**1000)},installed_preferences={})
  self.assertEqual(c.budgets['agy']['transient_attempts'],3)
 def test_zero_transient_retry_legacy_preference_preserved(self):
  c=capture_legacy_config({'QUOTA_SENTINEL_AGY_TRANSIENT_RETRIES':'0'},installed_preferences={})
  self.assertEqual(c.budgets['agy']['transient_attempts'],0)
 def test_personal_profile_preserved(self):
  c=capture_legacy_config({},installed_preferences={'QUOTA_SENTINEL_ORCHESTRATOR_ENABLED':'1'})
  self.assertEqual(c.providers['codex'].opening_chain,('pi','codex')); self.assertEqual(c.providers['antigravity'].opening_chain,('agy','pi')); self.assertEqual(c.providers['clinepass'].opening_chain,('direct',)); self.assertEqual(c.providers['codex'].quota_chain,('native','codexbar-live','codexbar-cache','pi-snapshot')); self.assertEqual(tuple(c.app.values()),(3,2,30,780,20,60)); self.assertTrue(c.features.feishu_listener)
 def test_existing_config_wins(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'config.json'; save_config(p,new_user_defaults(),expected_revision=None)
   c=resolve_config(p,Path(t),{},installed_preferences={'QUOTA_SENTINEL_ORCHESTRATOR_ENABLED':'1'})
   self.assertEqual(c.settings.origin,'new-installation'); self.assertFalse(c.settings.features.feishu_listener)
 def test_missing_authority_not_repaired(self):
  with tempfile.TemporaryDirectory() as t:
   d=Path(t); resolve_config(d/'config.json',d,{})
   with self.assertRaises(AuthorityMissingError):read_authority(d)
   with self.assertRaises(ConfigurationError):initialize_new_installation(d,new_user_defaults())
 def test_fresh_directory_only(self):
  with tempfile.TemporaryDirectory() as t:
   d=Path(t)/'new'; a=initialize_new_installation(d,new_user_defaults()); self.assertEqual((a.backend,a.epoch),('json',0)); self.assertEqual(read_authority(d),a); self.assertTrue((d/'config.json').is_file())
   with self.assertRaises(ConfigurationError):initialize_new_installation(d,new_user_defaults())
 def test_failed_provision_does_not_start(self):
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as t:
   d=Path(t)/'new'
   with patch('quota_sentinel.state.new_installation.save_config',side_effect=OSError('disk')):
    with self.assertRaises(OSError):initialize_new_installation(d,new_user_defaults())
   with self.assertRaises(AuthorityMissingError):read_authority(d)
 def test_env_precedence_no_tokens(self):
  c=capture_legacy_config({'QUOTA_SENTINEL_TRANSPORT':'codex=codex','SECRET':'never-copy','QUOTA_SENTINEL_MODEL_TIMEOUT':'444'},installed_preferences={})
  self.assertEqual(c.providers['codex'].opening_chain,('codex','pi'));self.assertEqual(c.budgets['pi']['timeout'],444);self.assertNotIn('never-copy',str(to_document(c)))
if __name__=='__main__':unittest.main()
