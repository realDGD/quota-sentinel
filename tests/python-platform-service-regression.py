"""Selected current-user services: isolated manager commands, never supplier calls."""
from dataclasses import replace
import importlib.util
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import new_user_defaults, FeatureSettings, CredentialReference
from quota_sentinel.state.new_installation import initialize_new_installation
from quota_sentinel.platform.process import CommandResult

class Services(unittest.TestCase):
 def setUp(self):
  self.assertIsNotNone(importlib.util.find_spec('quota_sentinel.platform.services'),'service boundary missing')
  from quota_sentinel.platform.services import ServiceManager, ServiceDefinition, ServiceError
  from quota_sentinel.install import service_definition
  self.Manager=ServiceManager;self.Definition=ServiceDefinition;self.Error=ServiceError;self.build=service_definition
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name)/'space 中文 & % $';self.root.mkdir()
  self.c=new_user_defaults();self.state=self.root/'state';initialize_new_installation(self.state,self.c)
  self.calls=[]
 def run_fake(self,argv,**kw):
  self.calls.append((tuple(argv),kw));return CommandResult(b'',b'',0,False)
 def manager(self,system):
  return self.Manager(system=system,home=self.root,runner=self.run_fake,user_id='S-1-5-21-123',uid=123)
 def definition(self,c=None):return self.build(c or self.c,self.state,self.state/'config.json',environment={'PATH':'/fixture/bin','TOKEN_CANARY':'never-render'},name='quota-sentinel.fixture')
 def test_selected_components_only(self):
  self.assertIsNotNone(self.definition())
  manual=replace(self.c,features=FeatureSettings(False,True,False,False))
  self.assertIsNone(self.definition(manual))
  bot=replace(self.c,features=FeatureSettings(False,True,False,True))
  self.assertIsNotNone(self.definition(bot))
  self.assertGreater(self.definition().stop_timeout,0)
 def test_render_no_secrets(self):
  for system in ('Darwin','Linux','Windows'):
   data=self.manager(system).render(self.definition())
   self.assertNotIn(b'never-render',data);self.assertNotIn(b'TOKEN_CANARY',data)
  with self.assertRaises(ValueError):self.Definition('quota-sentinel.fixture',('/python',),self.root,{'SECRET':'value'},30)
 def test_spaces_unicode_service_paths(self):
  d=self.definition();mac=plistlib.loads(self.manager('Darwin').render(d))
  self.assertEqual(mac['ProgramArguments'],list(d.argv));self.assertEqual(mac['WorkingDirectory'],str(d.cwd))
  linux=self.manager('Linux').render(d).decode();self.assertIn('%%',linux);self.assertIn('KillMode=control-group',linux);self.assertIn('ExecStart=:',linux)
  win=ET.fromstring(self.manager('Windows').render(d));ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
  self.assertEqual(win.find('t:Actions/t:Exec/t:WorkingDirectory',ns).text,str(d.cwd))
 def test_macos_no_duplicate_scheduler(self):
  m=self.manager('Darwin');d=self.definition();m.install(d);self.assertFalse(self.calls)
  m.start(d);self.assertFalse(any('quota-sentinel.timer' in str(x) for x in self.calls))
  self.calls.clear();d=replace(d,name='quota-sentinel.service');m.install(d);m.start(d)
  commands=[x[0] for x in self.calls]
  retired=[x for x in commands if 'bootout' in x]
  self.assertEqual(len(retired),4) # three known old entries plus current replacement
  self.assertTrue(all(x[2].startswith('gui/123/quota-sentinel') for x in retired))
  self.assertEqual(commands[-1][1],'bootstrap')
 def test_systemd_user_unavailable_foreground_guidance(self):
  def unavailable(*args,**kw):return CommandResult(b'',b'private diagnostic',1,False)
  m=self.Manager(system='Linux',home=self.root,runner=unavailable)
  with self.assertRaisesRegex(self.Error,'foreground.*serve'):m.install(self.definition())
  self.assertFalse((self.root/'.config/systemd/user/quota-sentinel.fixture.service').exists())
 def test_windows_user_identity(self):
  m=self.manager('Windows');d=self.definition();doc=ET.fromstring(m.render(d));ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
  self.assertEqual(doc.find('t:Principals/t:Principal/t:UserId',ns).text,'S-1-5-21-123')
  self.assertEqual(doc.find('t:Principals/t:Principal/t:LogonType',ns).text,'InteractiveToken')
  m.install(d);self.assertFalse(any('/Run' in x[0] for x in self.calls))
  self.assertTrue(any('/Create' in x[0] for x in self.calls));m.start(d);m.stop(d);m.remove(d)
  self.assertTrue(any('/End' in x[0] for x in self.calls));self.assertTrue(any('/Delete' in x[0] for x in self.calls))
 def test_windows_manifest_applies_coordinates_without_shell(self):
  import json
  from quota_sentinel.platform.service_host import main
  m=self.manager("Windows");d=self.definition();m.install(d)
  with patch("quota_sentinel.platform.service_host.Path.cwd",return_value=d.cwd),patch.dict(os.environ,{},clear=True),patch("quota_sentinel.__main__.main",return_value=0) as cli:
   self.assertEqual(main([str(m.manifest_path(d))]),0)
   self.assertEqual(os.environ["PATH"],"/fixture/bin")
   cli.assert_called_once_with(list(d.argv[3:]))
  m.manifest_path(d).write_text(json.dumps({"arguments":list(d.argv[3:]),"environment":{"SECRET":"never-render"}}))
  with self.assertRaises(ValueError):main([str(m.manifest_path(d))])
 def test_uninstall_keeps_state(self):
  for system in ('Darwin','Linux','Windows'):
   m=self.manager(system);d=self.definition();m.install(d);before={p.name:p.read_bytes() for p in self.state.glob('*.json')}
   m.remove(d);self.assertEqual(before,{p.name:p.read_bytes() for p in self.state.glob('*.json')})
 def test_missing_authority_never_repaired(self):
  (self.state/'backend-authority.json').unlink()
  with self.assertRaises(Exception):self.definition()
  self.assertFalse((self.state/'backend-authority.json').exists());self.assertFalse(self.calls)
 def test_cli_service_surface(self):
  from quota_sentinel.__main__ import build_parser
  args=build_parser().parse_args(['service','install']);self.assertEqual(args.service_action,'install')
 def test_portable_static_client_check(self):
  from quota_sentinel.config.diagnostics import dependency_problems
  client=self.root/"codex fixture.py";client.write_text("pass")
  c=replace(self.c,clients={"codex":str(client)})
  self.assertEqual(dependency_problems(c),[])
 def test_selected_credential_extras(self):
  from quota_sentinel.runtime.selection import build_runtime_plan,selected_extras
  c=replace(self.c,credentials={'opencode':CredentialReference('system','secret-service:ignored'),'feishu_app_secret':CredentialReference('system','kwallet:selected')})
  self.assertEqual(selected_extras(build_runtime_plan(c,'serve')),())
  c=replace(c,features=FeatureSettings(True,True,False,True))
  self.assertEqual(selected_extras(build_runtime_plan(c,'serve')),('feishu','kwallet'))

if __name__=='__main__':unittest.main()
