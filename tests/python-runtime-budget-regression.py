import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import *
from quota_sentinel.config.migration import capture_legacy_config
from quota_sentinel.runtime.selection import build_runtime_plan
from quota_sentinel.runtime.budgets import check_budget,usage_budget
class BudgetTests(unittest.TestCase):
 def change(self,c,group,key,value):
  d=to_document(c);d['budgets'][group][key]=value;return parse_config(d)
 def test_single_codex_excludes_pi(self):
  c=new_user_defaults();p=build_runtime_plan(c,'check');large=self.change(c,'pi','auth_timeout',10000)
  self.assertEqual(check_budget(c,p),check_budget(large,build_runtime_plan(large,'check')));self.assertLess(check_budget(c,p),1800);self.assertGreater(check_budget(c,p),900)
 def test_each_fallback_prepare_counted(self):
  c=capture_legacy_config({'QUOTA_SENTINEL_TRANSPORT':'codex=codex'},installed_preferences={});d=to_document(c)
  for p in ('antigravity','opencode','clinepass'):d['providers'][p]['enabled']=False
  c=parse_config(d);large=self.change(c,'pi','auth_timeout',10000)
  self.assertAlmostEqual(check_budget(large,build_runtime_plan(large,'check'))-check_budget(c,build_runtime_plan(c,'check')),54835) # 5 fallback prepares * 9970 * 1.1
 def test_selected_probe_sum(self):
  c=new_user_defaults();d=to_document(c);d['providers']['codex']['quota_chain']=['native','codexbar-live'];c=parse_config(d);large=self.change(c,'probes','codexbar_timeout',4000)
  self.assertAlmostEqual(usage_budget(large,build_runtime_plan(large,'usage'))-usage_budget(c,build_runtime_plan(c,'usage')),8756)
 def test_personal_budget_not_understated(self):
  c=capture_legacy_config({},installed_preferences={'QUOTA_SENTINEL_ORCHESTRATOR_ENABLED':'1'});self.assertGreaterEqual(check_budget(c,build_runtime_plan(c,'check')),6142.4)
 def test_plugin_and_cleanup_bounds(self):
  c=capture_legacy_config({},installed_preferences={});large=self.change(c,'pi','plugin_timeout',100)
  self.assertGreater(check_budget(large,build_runtime_plan(large,'check')),check_budget(c,build_runtime_plan(c,'check')))
 def test_saved_runner_parameters_are_used(self):
  from quota_sentinel.runtime.factory import create_application
  d=to_document(new_user_defaults());d['clients']={'codex':'/usr/bin/true','codex_home':'/tmp/custom-codex'};d['budgets']['codex']['input_ceiling']=77;d['budgets']['codex']['timeout']=17;c=parse_config(d)
  app=create_application(Path('/tmp/budget-fixture'),software_config=c,runtime_plan=build_runtime_plan(c,'run'),environment={});r=app.model_runner._runner('codex');self.assertEqual(r.config.timeout,17);self.assertEqual(r.config.input_ceiling,77);self.assertEqual(r.config.codex_home,Path('/tmp/custom-codex'))
 def test_explicit_outer_override(self):
  import task_orchestrator as t
  c=new_user_defaults();self.assertEqual(t.check_command_timeout({'QUOTA_SENTINEL_CHECK_TIMEOUT':'123'},software_config=c,runtime_plan=build_runtime_plan(c,'check')),123)
if __name__=='__main__':unittest.main()
