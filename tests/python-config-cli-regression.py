import sys,json,tempfile,subprocess,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import *
from quota_sentinel.config.migration import capture_legacy_config
from quota_sentinel.configure import configure
class ConfigCliTests(unittest.TestCase):
 def cli(self,*a):return subprocess.run([sys.executable,'-m','quota_sentinel',*a],text=True,capture_output=True,timeout=15)
 def test_edit_existing_starts_from_personal_choices(self):
  c=capture_legacy_config({},installed_preferences={'QUOTA_SENTINEL_ORCHESTRATOR_ENABLED':'1'});out=[]
  inputs=iter(['']*20+['y']);r=configure(EffectiveConfig(c,{},None),input_fn=lambda q:next(inputs),output_fn=out.append)
  self.assertEqual(r.providers,c.providers);self.assertEqual(r.app,c.app);self.assertTrue(any('pi install npm:pi-antigravity' in x for x in out))
 def test_cancel_keeps_file(self):
  c=new_user_defaults();self.assertIsNone(configure(EffectiveConfig(c,{},None),input_fn=lambda q:'cancel',output_fn=lambda x:None))
 def test_pi_plugin_notice_even_as_fallback(self):
  c=capture_legacy_config({},installed_preferences={});out=[]
  configure(EffectiveConfig(c,{},None),input_fn=lambda q:'n' if q.startswith('Save') else '',output_fn=out.append)
  self.assertIn('pi install npm:pi-antigravity',' '.join(out))
 def test_config_sources_redacted(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'config.json';save_config(p,new_user_defaults(),expected_revision=None)
   r=self.cli('--config',str(p),'config','show');self.assertEqual(r.returncode,0,r.stderr);d=json.loads(r.stdout);self.assertEqual(d['sources']['configuration'],'saved');self.assertFalse(d['settings']['features']['feishu_listener'])
 def test_config_validate_no_runtime_install(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'c.json';d=to_document(new_user_defaults());d['clients']['codex']=sys.executable;save_config(p,parse_config(d),expected_revision=None);r=self.cli('--config',str(p),'config','validate');self.assertEqual(r.returncode,0,r.stderr)
 def test_core_install_has_no_lark(self):
  from importlib.metadata import metadata
  requirements=metadata('quota-sentinel').get_all('Requires-Dist') or []
  self.assertFalse(any(r.startswith('lark-oapi') and 'extra ==' not in r for r in requirements))
  self.assertTrue(any(r.startswith('lark-oapi') and 'feishu' in r for r in requirements))
if __name__=='__main__':unittest.main()
