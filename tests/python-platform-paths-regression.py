"""Platform paths and exact, shell-free launcher prefixes."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

class Paths(unittest.TestCase):
 def setUp(self):
  self.assertIsNotNone(importlib.util.find_spec('quota_sentinel.platform'), 'platform boundary missing')
  from quota_sentinel.platform import paths
  self.paths=paths
 def test_platform_state_dirs(self):
  env={'HOME':'/profile','USERPROFILE':'/profile','LOCALAPPDATA':'/local','XDG_STATE_HOME':'/xdg'}
  self.assertEqual(self.paths.default_state_dir('Darwin',env),Path('/profile/Library/Application Support/Quota-Sentinel'))
  self.assertEqual(self.paths.default_state_dir('Linux',env),Path('/xdg/quota-sentinel'))
  self.assertEqual(self.paths.default_state_dir('Windows',env),Path('/local/Quota-Sentinel'))
  self.assertEqual(self.paths.default_state_dir('Linux',{'HOME':'/profile'}),Path('/profile/.local/state/quota-sentinel'))
 def test_explicit_override(self):
  for system in ('Darwin','Windows','Linux'):
   self.assertEqual(self.paths.default_state_dir(system,{'QUOTA_SENTINEL_STATE_DIR':'/custom path'}),Path('/custom path'))
 def test_spaces_unicode_and_metacharacters(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'目录 $x ; helper.py';path.write_text('')
   self.assertEqual(self.paths.resolve_launcher('helper',path),(sys.executable,str(path.resolve())))
 def test_selected_client_only(self):
  with patch('shutil.which',return_value=sys.executable) as lookup:
   self.assertEqual(self.paths.resolve_launcher('codex'),(sys.executable,))
   lookup.assert_called_once_with('codex')
 def test_npm_shim_manifest_entry(self):
  with tempfile.TemporaryDirectory() as tmp:
   base=Path(tmp);shim=base/'codex.cmd';shim.write_text('untrusted shell text')
   package=base/'node_modules/@openai/codex';(package/'bin').mkdir(parents=True)
   (package/'package.json').write_text(json.dumps({'name':'@openai/codex','bin':{'codex':'bin/codex.js'}}))
   entry=package/'bin/codex.js';entry.write_text('')
   with patch('shutil.which',return_value=sys.executable):
    self.assertEqual(self.paths.resolve_launcher('codex',shim),(sys.executable,str(entry.resolve())))
   (package/'package.json').write_text(json.dumps({'name':'@openai/codex','bin':{'codex':'../../evil.js'}}))
   with self.assertRaises(ValueError):self.paths.resolve_launcher('codex',shim)
 def test_unknown_shim_refused(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'unknown.cmd';path.write_text('')
   with self.assertRaises(ValueError):self.paths.resolve_launcher('unknown',path)

if __name__=='__main__':unittest.main()
