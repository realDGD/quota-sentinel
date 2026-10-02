import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import *
from quota_sentinel.runtime.selection import build_runtime_plan
class SelectionTests(unittest.TestCase):
 def test_query_plan_has_no_model_dependencies(self):
  p=build_runtime_plan(new_user_defaults(),'usage');self.assertEqual(p.active_providers,('codex',));self.assertEqual(p.opening_providers,());self.assertFalse(p.notify);self.assertFalse(any(x.startswith('opening:') for x in p.dependency_ids));self.assertNotIn('pi',str(p.dependency_ids))
 def test_selected_factory_no_credentials_during_construction(self):
  from quota_sentinel.runtime.factory import create_application
  from unittest.mock import patch
  c=new_user_defaults();p=build_runtime_plan(c,'usage')
  with patch('quota_sentinel.runtime.keychain.read',side_effect=AssertionError('disabled credentials')):
   app=create_application(Path('/tmp/selection-fixture'),environment={},software_config=c,runtime_plan=p)
  self.assertEqual(app.runtime_plan.opening_providers,())
 def test_unselected_factory_never_called(self):
  p=build_runtime_plan(new_user_defaults(),'check');self.assertEqual(p.opening_chains,{'codex':('codex',)});self.assertNotIn('feishu',str(p.dependency_ids))
 def test_disabled_request_refused_and_empty_stays_empty(self):
  with self.assertRaises(ConfigurationError):build_runtime_plan(new_user_defaults(),'run',requested=('antigravity',))
  d=to_document(new_user_defaults());d['providers']['codex']['enabled']=False
  self.assertEqual(build_runtime_plan(parse_config(d),'check').active_providers,())
 def test_only_opening_internal_observation(self):
  d=to_document(new_user_defaults());d['features']['quota_queries']=False;c=parse_config(d)
  self.assertEqual(build_runtime_plan(c,'check').probe_providers,('codex',))
  with self.assertRaises(ConfigurationError):build_runtime_plan(c,'usage')
if __name__=='__main__':unittest.main()
