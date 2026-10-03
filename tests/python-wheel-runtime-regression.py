"""Install an offline-built wheel and run it away from repository files."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
ROOT=Path(__file__).resolve().parents[1]

class Wheel(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory();cls.addClassCleanup(cls.tmp.cleanup);cls.root=Path(cls.tmp.name)
  uv=shutil.which('uv')
  if not uv:raise RuntimeError('uv is required to verify the installed wheel')
  cls.environment={k:v for k,v in os.environ.items() if not k.startswith(('UV_','PIP_')) and k!='PYTHONPATH'}
  cls.environment['QUOTA_SENTINEL_KEYCHAIN_DISABLED']='1'
  cache=os.environ.get('QUOTA_SENTINEL_TEST_UV_CACHE')
  if cache:cls.environment['UV_CACHE_DIR']=cache
  built=subprocess.run([uv,'--no-config','build','--offline','--wheel','--out-dir',str(cls.root/'dist')],cwd=ROOT,env=cls.environment,capture_output=True,timeout=90)
  if built.returncode:raise AssertionError(built.stderr.decode())
  wheel=next((cls.root/'dist').glob('*.whl'))
  venv.EnvBuilder(with_pip=False).create(cls.root/'venv')
  cls.python=cls.root/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
  # Locked sync caches artifacts without necessarily caching index version
  # lists. Reuse that lock offline to provision only core dependencies in the
  # fresh venv, then install the wheel with normal dependency verification.
  core_environment=dict(cls.environment,UV_PROJECT_ENVIRONMENT=str(cls.root/'venv'))
  core=subprocess.run([uv,'--no-config','sync','--offline','--frozen','--no-dev','--no-default-groups','--no-install-project'],cwd=ROOT,env=core_environment,capture_output=True,timeout=30)
  if core.returncode:raise AssertionError(core.stderr.decode())
  installed=subprocess.run([uv,'--no-config','pip','install','--offline','--python',str(cls.python),str(wheel)],env=cls.environment,capture_output=True,timeout=45)
  if installed.returncode:raise AssertionError(installed.stderr.decode())
  cls.foreign=cls.root/'foreign';cls.foreign.mkdir()
 def run_python(self,code):
  result=subprocess.run([str(self.python),'-c',code],cwd=self.foreign,env=self.environment,capture_output=True,timeout=20)
  self.assertEqual(result.returncode,0,result.stderr.decode());return result.stdout
 def test_helpers_without_repo_files(self):
  self.run_python('from quota_sentinel.helpers import resource_path\nfor n in ("run_with_timeout.py","antigravity_usage.py","opencode_usage.py","clinepass_usage.py","pi_quota_query.mjs","pi_quota/auth.mjs","capture-codex-quota.ts"):\n assert resource_path(n).is_file(),n\nimport quota_sentinel.helpers.task_orchestrator, quota_sentinel.helpers.feishu_listener')
 def test_wheel_foreign_cwd(self):
  result=subprocess.run([str(self.python),'-m','quota_sentinel','--help'],cwd=self.foreign,env=self.environment,capture_output=True,timeout=10)
  self.assertEqual(result.returncode,0,result.stderr.decode());self.assertIn(b'quota-sentinel',result.stdout)
 def test_manual_daemon_without_optional_imports(self):
  self.run_python('from dataclasses import replace\nfrom quota_sentinel.config import new_user_defaults, FeatureSettings\nfrom quota_sentinel.daemon import serve\nfrom pathlib import Path\nimport sys\nc=replace(new_user_defaults(),features=FeatureSettings(False,True,False,False))\ndef unused(*args):raise AssertionError("unselected component started")\nfrom quota_sentinel.runtime.selection import build_runtime_plan\nassert serve(c,build_runtime_plan(c,"serve"),scheduler_factory=unused,listener_factory=unused)==0\nassert "lark_oapi" not in sys.modules')
 def test_core_imports_without_dependencies(self):
  self.run_python('from quota_sentinel.platform.credentials import CredentialStore\nfrom quota_sentinel.runtime.selected_factory import create_selected_application\nfrom quota_sentinel.helpers.task_orchestrator import TaskOrchestrator\nimport sys, importlib.util\nassert "secretstorage" not in sys.modules and "dbus" not in sys.modules and "lark_oapi" not in sys.modules\nassert all(importlib.util.find_spec(n) is None for n in ("secretstorage","dbus","lark_oapi"))')

if __name__=='__main__':unittest.main()
