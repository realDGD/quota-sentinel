"""Evidence classes and skipped native capabilities never imply support."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]

class Matrix(unittest.TestCase):
 def module(self):
  spec=importlib.util.spec_from_file_location('matrix_dispatch',ROOT/'tests/run-regressions.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
 def test_evidence_classes(self):
  m=self.module()
  self.assertEqual(m.classify(Path('python-selection-regression.py')),'platform-independent')
  self.assertEqual(m.classify(Path('python-native-windows-process-regression.py')),'native-platform')
  self.assertEqual(m.classify(Path('supplier-live-smoke.py')),'supplier-smoke')
 def test_skipped_required_native_is_incomplete(self):
  m=self.module();p=Path('python-native-windows-process-regression.py')
  result=m.evidence_result(p,77,required=True)
  self.assertEqual(result['status'],'UNVERIFIED');self.assertFalse(result['complete'])
  self.assertTrue(m.evidence_result(p,0,required=True)['complete'])
 def test_skip_in_portable_suite_is_failure(self):
  self.assertEqual(self.module().evidence_result(Path('python-selection-regression.py'),77)['status'],'FAIL')
 def test_windows_server_is_not_windows11(self):
  m=self.module()
  self.assertFalse(m.windows11_evidence('Windows Server 2022'))
  self.assertTrue(m.windows11_evidence('Windows 11 Pro'))
 def test_owned_dispatch_timeout_cleans_children(self):
  m=self.module()
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);script=root/'hang.py';script.write_text('import time;time.sleep(30)')
   self.assertEqual(m.run_script(script,root/'log',timeout=.1)[0],124)
 def test_native_required_refuses_portable_only(self):
  m=self.module();m.discover=lambda *args:[];m.discover_node=lambda:[]
  with tempfile.TemporaryDirectory() as tmp:
   with self.assertRaises(SystemExit):m.main(['--platform','portable','--require-native','--log-dir',tmp])
 def test_python39_build_backend_is_pinned_separately(self):
  text=(ROOT/'pyproject.toml').read_text()
  self.assertIn("hatchling==1.27.0; python_version < '3.10'",text)
  self.assertIn("hatchling==1.32.4; python_version >= '3.10'",text)
 def test_checked_in_native_matrix(self):
  workflow=(ROOT/'.github/workflows/platform-regressions.yml').read_text()
  for value in ('ubuntu-22.04','ubuntu-24.04','macos','windows','3.9','3.13','quota-sentinel-win11','dbus-run-session','--require-native','--extra feishu'):
   self.assertIn(value,workflow)
  docs=(ROOT/'docs/PLATFORMS.md').read_text()
  for value in ('UNVERIFIED','Windows 11','systemd','service install','service start','service stop','service uninstall','--offline','core'):
   self.assertIn(value,docs)
if __name__=='__main__':unittest.main()
