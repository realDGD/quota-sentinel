import sys,tempfile,unittest,json
from pathlib import Path
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.runtime.pi_plugins import plugin_requirement,plugin_guidance,check_pi_plugin
from quota_sentinel.runtime.models import ModelRunnerConfig,ModelRunner
class PluginTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name)
  self.pi=self.root/'pi';self.pi.write_text('#!/usr/bin/env python3\nimport sys\nassert "--list-models" in sys.argv\nassert "--print" not in sys.argv\nprint("clinepass  cline-pass/deepseek-v4.1-flash  200K")\n');self.pi.chmod(0o700)
  self.pkg=self.root/'plugin';self.pkg.mkdir();(self.pkg/'package.json').write_text(json.dumps({'name':'pi-clinepass-provider','pi':{'extensions':['index.ts']}}));(self.pkg/'index.ts').write_text('fixture')
  self.auth=self.root/'auth.json';self.auth.write_text('{"clinepass":{"type":"api_key","key":"fixture-secret"}}')
  self.cfg=ModelRunnerConfig(self.pi,self.auth,self.root,self.root/'missing',plugin_entries={'clinepass':self.pkg/'index.ts'})
 def test_antigravity_primary_and_fallback_guidance(self):self.assertIn('pi install npm:pi-antigravity',' '.join(plugin_guidance('antigravity')))
 def test_clinepass_guidance(self):self.assertIn('pi install npm:pi-clinepass-provider',' '.join(plugin_guidance('clinepass')))
 def test_missing_plugin_zero_model_calls(self):
  c=replace(self.cfg,plugin_entries={});r=check_pi_plugin('clinepass',c);self.assertFalse(r.available)
 def test_wrong_provider_registration(self):
  self.pi.write_text('#!/usr/bin/env python3\nprint("other  cline-pass/deepseek-v4.1-flash")\n');r=check_pi_plugin('clinepass',self.cfg);self.assertFalse(r.available)
 def test_globally_installed_explicit_entry(self):
  r=check_pi_plugin('clinepass',self.cfg);self.assertTrue(r.available);self.assertEqual(r.entry,self.pkg/'index.ts')
 def test_direct_only_no_plugin_check(self):self.assertIsNone(plugin_requirement('opencode'))
 def test_clinepass_pi_identity(self):
  r=ModelRunner(self.cfg);cmd=r._command('clinepass');self.assertEqual(cmd[cmd.index('--provider')+1],'clinepass');self.assertEqual(cmd[cmd.index('--model')+1],'cline-pass/deepseek-v4.1-flash');self.assertEqual(cmd[cmd.index('--thinking')+1],'off');self.assertIn(str(self.pkg/'index.ts'),cmd);self.assertFalse(any('capture-clinepass' in x for x in cmd))
 def test_prepare_refuses_missing_plugin_before_creating_attempt_workspace(self):
  from quota_sentinel.config import ConfigurationError
  work=self.root/'work';r=ModelRunner(replace(self.cfg,plugin_entries={},verify_plugins=True))
  with self.assertRaisesRegex(ConfigurationError,'pi-clinepass-provider'):r.prepare('clinepass',work)
  self.assertFalse(work.exists())
if __name__=='__main__':unittest.main()
