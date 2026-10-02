import importlib.util,sys,tempfile,unittest,os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class DispatcherTests(unittest.TestCase):
 def module(self):
  spec=importlib.util.spec_from_file_location('regressions',ROOT/'tests/run-regressions.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
 def test_discovers_new_suites(self):
  names={p.name for p in self.module().discover('portable')};self.assertIn('python-regression-dispatcher-regression.py',names);self.assertIn('python-selection-regression.py',names)
 def test_failure_propagates(self):
  with tempfile.TemporaryDirectory() as t:
   script=Path(t)/'fails.py';script.write_text('raise SystemExit(7)\n')
   self.assertEqual(self.module().run_script(script,Path(t)/'log',timeout=2)[0],7)
 def test_timeout_is_bounded(self):
  import time
  with tempfile.TemporaryDirectory() as t:
   script=Path(t)/'hang.py';script.write_text('import time;time.sleep(30)\n');start=time.monotonic()
   self.assertEqual(self.module().run_script(script,Path(t)/'log',timeout=.1)[0],124);self.assertLess(time.monotonic()-start,3)
 def test_environment_cannot_reuse_real_credentials(self):
  from unittest.mock import patch
  with patch.dict(os.environ,{'OPENAI_API_KEY':'private','FEISHU_APP_SECRET':'private','QUOTA_SENTINEL_CONFIG':'/real/config','CODEX_HOME':'/real/auth'}):
   with tempfile.TemporaryDirectory() as t:
    env=self.module().fixture_environment(Path(t));self.assertNotIn('OPENAI_API_KEY',env);self.assertNotIn('FEISHU_APP_SECRET',env);self.assertNotIn('QUOTA_SENTINEL_CONFIG',env);self.assertNotIn('CODEX_HOME',env);self.assertEqual(env['HOME'],t)
if __name__=='__main__':unittest.main()
