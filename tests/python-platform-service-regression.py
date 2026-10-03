"""Selected current-user services: isolated manager commands, never supplier calls."""
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import new_user_defaults, to_document, FeatureSettings, CredentialReference
from quota_sentinel.state.new_installation import initialize_new_installation
from quota_sentinel.platform.files import publish_private,private_directory
from quota_sentinel.platform.process import CommandResult, CLEANUP_ALLOWANCE_SECONDS, spawn_owned

class Services(unittest.TestCase):
 def test_corrupt_installed_budget_keeps_stop_identity(self):
  import json
  for system in ('Darwin','Linux','Windows'):
   with self.subTest(system=system):
    m=self.manager(system);d=self.definition();path=m.manifest_path(d) if system=='Windows' else m.path(d);private_directory(path.parent)
    value=str(10**1000)
    raw=('<plist version="1.0"><dict><key>ExitTimeOut</key><integer>'+value+'</integer></dict></plist>') if system=='Darwin' else json.dumps({'stop_timeout':int(value)}) if system=='Windows' else 'TimeoutStopSec='+value
    publish_private(path,raw.encode());stopping=m.installed_definition(d.name,self.state)
    self.assertEqual(stopping.name,d.name);self.assertEqual(stopping.stop_timeout,300)
 def test_listener_without_queries_can_be_installed(self):
  c=replace(self.c,features=FeatureSettings(False,False,False,True))
  self.assertIsNotNone(self.definition(c))
 def test_manual_query_selects_store_extras_and_diagnostics(self):
  from quota_sentinel.install import installation_extras
  from quota_sentinel.config.diagnostics import dependency_problems
  for backend,module in (('secret-service','secretstorage'),('kwallet','dbus')):
   with self.subTest(backend=backend):
    providers=dict(self.c.providers);providers['codex']=replace(providers['codex'],enabled=False);providers['opencode']=replace(providers['opencode'],enabled=True,opening_enabled=False)
    c=replace(self.c,features=FeatureSettings(False,True,False,False),providers=providers,clients={'curl':sys.executable},credentials={'opencode':CredentialReference('system',backend+':selected')})
    self.assertEqual(installation_extras(c),(backend,))
    with patch('sys.platform','linux'),patch('importlib.util.find_spec',side_effect=lambda name:None if name==module else object()):
     self.assertIn('Install selected extra: quota-sentinel['+backend+']',dependency_problems(c))
 def test_stop_and_remove_work_with_broken_config_or_authority(self):
  from quota_sentinel.__main__ import main
  for action in ('stop','uninstall'):
   for defect in ('invalid','missing','authority'):
    with self.subTest(action=action,defect=defect),tempfile.TemporaryDirectory() as tmp:
     state=Path(tmp)/'state';initialize_new_installation(state,new_user_defaults());config=state/'config.json'
     if defect=='invalid':config.write_text('{')
     elif defect=='missing':config.unlink()
     else:(state/'backend-authority.json').unlink()
     with patch('quota_sentinel.platform.services.ServiceManager') as manager:
      self.assertEqual(main(['--state-dir',str(state),'--config',str(config),'service',action,'--name','quota-sentinel.fixture']),0)
      getattr(manager.return_value,'remove' if action=='uninstall' else action).assert_called_once()
 def setUp(self):
  self.assertIsNotNone(importlib.util.find_spec('quota_sentinel.platform.services'),'service boundary missing')
  from quota_sentinel.platform.services import ServiceManager, ServiceDefinition, ServiceError
  from quota_sentinel.install import service_definition
  self.Manager=ServiceManager;self.Definition=ServiceDefinition;self.Error=ServiceError;self.build=service_definition
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=private_directory(Path(self.tmp.name)/'space 中文 & % $')
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
 def test_linux_start_replaces_active_host_with_saved_profile(self):
  # This runner models idempotent systemd start; the host/parser are real.
  # No systemd manager, suppliers, credentials, or bot clients are invoked.
  host=self.root/'profile-host.py';observed=self.root/'observed.json'
  host.write_text('''import json,os,sys,time
from pathlib import Path
from quota_sentinel.config import read_config
from quota_sentinel.platform.files import publish_private,private_directory
from quota_sentinel.runtime.selection import build_runtime_plan
config=read_config(Path(sys.argv[1])).settings
plan=build_runtime_plan(config,'serve')
publish_private(Path(sys.argv[2]),json.dumps(dict(pid=os.getpid(),listener=plan.start_listener,opening_roster=plan.opening_providers,codex_timeout=config.budgets['codex']['timeout'])).encode())
time.sleep(60)
''')
  active=[None];stop_deadlines=[]
  def cleanup():
   if active[0] is not None:
    active[0].stop(0);active[0].close();active[0]=None
  self.addCleanup(cleanup)
  environment={'HOME':str(self.root),'PATH':str(Path(sys.executable).parent),'PYTHONPATH':str(Path(__file__).resolve().parents[1]),'PYTHONDONTWRITEBYTECODE':'1','QUOTA_SENTINEL_KEYCHAIN_DISABLED':'1'}
  def systemd_fixture(argv,**options):
   self.assertEqual(tuple(argv[:2]),('systemctl','--user'))
   action=argv[2]
   if action in ('start','stop','restart','enable'):
    self.assertEqual(tuple(argv[3:]),('quota-sentinel.fixture.service',))
   elif action=='show':self.assertEqual(tuple(argv[3:]),('--property=Version','--value'))
   else:self.assertEqual(action,'daemon-reload')
   if action in ('stop','restart'):
    stop_deadlines.append(options['timeout']);cleanup()
   if action in ('start','restart') and (active[0] is None or active[0].poll() is not None):
    active[0]=spawn_owned((sys.executable,str(host),str(self.state/'config.json'),str(observed)),cwd=self.root,environment=environment,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   return CommandResult(b'',b'',0,False)
  def running_profile():
   deadline=time.monotonic()+5
   while time.monotonic()<deadline:
    if observed.exists():
     value=json.loads(observed.read_text())
     if value['pid']==active[0].pid:return value
    if active[0].poll() is not None:self.fail('profile fixture exited before publishing its saved configuration')
    time.sleep(.01)
   self.fail('profile fixture did not publish its saved configuration')
  m=self.Manager(system='Linux',home=self.root,environment={'HOME':str(self.root)},runner=systemd_fixture)
  d=self.build(self.c,self.state,self.state/'config.json',environment={'HOME':str(self.root)},name='quota-sentinel.fixture')
  m.install(d);self.assertIsNone(active[0]);m.start(d);before=running_profile();previous=active[0]
  self.assertEqual({k:v for k,v in before.items() if k!='pid'},{'listener':False,'opening_roster':['codex'],'codex_timeout':120})
  providers=dict(self.c.providers);providers['antigravity']=replace(providers['antigravity'],enabled=True,opening_enabled=True)
  budgets={k:dict(v) for k,v in self.c.budgets.items()};budgets['codex']['timeout']=240
  saved=replace(self.c,providers=providers,features=replace(self.c.features,feishu_listener=True),budgets=budgets)
  publish_private(self.state/'config.json',json.dumps(to_document(saved)).encode())
  changed=self.build(saved,self.state,self.state/'config.json',environment={'HOME':str(self.root)},name='quota-sentinel.fixture')
  m.install(changed)
  self.assertIs(active[0],previous);self.assertEqual(running_profile(),before)
  m.start(changed);after=running_profile()
  self.assertEqual({k:v for k,v in after.items() if k!='pid'},{'listener':True,'opening_roster':['codex','antigravity'],'codex_timeout':240})
  self.assertNotEqual(after['pid'],before['pid']);self.assertIsNotNone(previous.poll());self.assertIsNone(active[0].poll())
  self.assertEqual(stop_deadlines,[d.stop_timeout+CLEANUP_ALLOWANCE_SECONDS,changed.stop_timeout+CLEANUP_ALLOWANCE_SECONDS])
 def test_linux_start_reports_failed_stop_before_replacement(self):
  starts=[]
  def failing_stop(argv,**options):
   if argv[2] in ('stop','restart'):return CommandResult(b'',b'',124,True)
   if argv[2]=='start':starts.append(tuple(argv))
   return CommandResult(b'',b'',0,False)
  m=self.Manager(system='Linux',home=self.root,environment={'HOME':str(self.root)},runner=failing_stop);d=self.definition();m.install(d)
  with self.assertRaises(self.Error):m.start(d)
  self.assertFalse(starts)
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
