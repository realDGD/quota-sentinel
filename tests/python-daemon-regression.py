import sys,unittest,signal,tempfile,json
from pathlib import Path
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import *
from quota_sentinel.runtime.selection import build_runtime_plan
from quota_sentinel.daemon import serve
class DaemonTests(unittest.TestCase):
 def test_listener_import_and_start_with_queries_disabled(self):
  import subprocess,os
  from quota_sentinel.state.new_installation import initialize_new_installation
  with tempfile.TemporaryDirectory() as tmp:
   state=Path(tmp)/'state';c=replace(new_user_defaults(),features=FeatureSettings(False,False,False,True));initialize_new_installation(state,c)
   code='''import asyncio,sys,types
from unittest.mock import Mock,patch,AsyncMock
from pathlib import Path
import feishu_listener as m
from quota_sentinel.config import read_config
c=read_config(Path(sys.argv[1])).settings
listener=m.FeishuListener(c,Path(sys.argv[1]).parent,Path(sys.argv[1]),None)
loop=asyncio.new_event_loop();sdk=Mock();sdk.ws.Client.return_value._disconnect=AsyncMock();credentials=Mock();credentials.get.side_effect={'app_id':'fixture-id','app_secret':'fixture-secret','user_id':'fixture-user'}.get
with patch.object(m,'_load_sdk',return_value=sdk),patch('quota_sentinel.runtime.feishu.SelectedCredentials',return_value=credentials),patch.dict(sys.modules,{'lark_oapi.ws.client':types.SimpleNamespace(loop=loop)}):
 listener.run();assert not m.commands_enabled;assert not m.submit_usage_command('fixture-user','fixture');listener.stop()
loop.close()
'''
   env=dict(os.environ,QUOTA_SENTINEL_CONFIG=str(state/'config.json'),PYTHONPATH=str(Path(__file__).resolve().parents[1]))
   result=subprocess.run([sys.executable,'-c',code,str(state/'config.json')],env=env,capture_output=True,text=True,timeout=10)
   self.assertEqual(result.returncode,0,result.stderr)
 def run_host(self,automatic,listener,*,fail=False):
  events=[];c=replace(new_user_defaults(),features=FeatureSettings(automatic,True,False,listener));p=build_runtime_plan(c,'serve')
  class S:
   def start(self):events.append('scheduler-start')
   def stop(self):events.append('scheduler-stop')
   def wait(self,stop):events.append('wait');return
  scheduler=S()
  class L:
   def run(self):
    events.append('listener-run')
    if fail:raise RuntimeError('startup failure')
   def stop(self):events.append('listener-stop')
  def sf():events.append('scheduler-create');return scheduler
  def lf(s):events.append(('listener-create',s is scheduler));return L()
  result=serve(c,p,scheduler_factory=sf,listener_factory=lf)
  return events,result
 def test_opening_only_no_lark_import(self):
  before='lark_oapi' in sys.modules;e,r=self.run_host(True,False);self.assertEqual(r,0);self.assertEqual(e,['scheduler-create','scheduler-start','wait','scheduler-stop']);self.assertEqual('lark_oapi' in sys.modules,before)
 def test_listener_only_no_scheduler(self):
  e,r=self.run_host(False,True);self.assertNotIn('scheduler-create',e);self.assertIn(('listener-create',False),e);self.assertEqual(r,0)
 def test_both_single_scheduler(self):
  e,r=self.run_host(True,True);self.assertEqual(e.count('scheduler-create'),1);self.assertIn(('listener-create',True),e);self.assertEqual(e[-2:],['listener-stop','scheduler-stop'])
 def test_failed_listener_start_no_orphan_scheduler(self):
  e,r=self.run_host(True,True,fail=True);self.assertEqual(r,1);self.assertEqual(e[-2:],['listener-stop','scheduler-stop'])
 def test_listener_reply_with_push_disabled(self):
  from quota_sentinel.runtime.selected_factory import NullNotifier
  from quota_sentinel.daemon import reply_usage
  from unittest.mock import patch
  with patch('quota_sentinel.runtime.feishu.FeishuClient') as client:
   reply_usage({'codex':None},'authorized-user',1000)
   payload=client.return_value.send.call_args.args[0];self.assertEqual(payload['receive_id'],'authorized-user');self.assertEqual(payload['msg_type'],'interactive')
 def test_signal_stops_both_and_restores_handler(self):
  import os
  previous=signal.getsignal(signal.SIGTERM);events=[]
  c=replace(new_user_defaults(),features=FeatureSettings(True,True,False,True));p=build_runtime_plan(c,'serve')
  class S:
   def start(self):events.append('start')
   def stop(self):events.append('scheduler-stop')
  class L:
   def run(self):os.kill(os.getpid(),signal.SIGTERM)
   def stop(self):events.append('listener-stop')
  self.assertEqual(serve(c,p,scheduler_factory=S,listener_factory=lambda s:L()),0)
  self.assertEqual(events,['start','listener-stop','scheduler-stop']);self.assertEqual(signal.getsignal(signal.SIGTERM),previous)
 def test_stop_joins_and_reaps(self):
  import feishu_listener as module,os,time
  from unittest.mock import patch
  c=replace(new_user_defaults(),features=FeatureSettings(False,True,False,True))
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'pids'; script=Path(tmp)/'cli.py'
   script.write_text('import os,signal,time\nfrom pathlib import Path\nchild=os.fork()\nif child==0:\n os.setsid()\n signal.signal(signal.SIGTERM,signal.SIG_IGN)\n while True:time.sleep(.1)\nPath('+repr(str(path))+').write_text(str(os.getpid())+" "+str(child))\nwhile True:time.sleep(.1)\n')
   component=module.FeishuListener(c,Path(tmp),Path(tmp)/'config.json',None)
   with patch.object(module,'USAGE_COMMAND',(sys.executable,str(script))),patch.object(module,'commands_closing',False),patch.object(module,'commands_enabled',True):
    self.assertTrue(module.submit_usage_command('authorized-user','fixture'))
    deadline=time.monotonic()+5
    while not path.exists() and time.monotonic()<deadline:time.sleep(.02)
    self.assertTrue(path.exists())
    component.stop()
    self.assertFalse(module.active_futures);self.assertFalse(module.active_processes)
    for pid in map(int,path.read_text().split()):
     deadline=time.monotonic()+3
     while time.monotonic()<deadline:
      try:os.kill(pid,0)
      except ProcessLookupError:break
      time.sleep(.02)
     else:self.fail('surviving owned process '+str(pid))
 def test_foreign_directory_manual_only_no_sdk(self):
  import subprocess,os
  from quota_sentinel.state.new_installation import initialize_new_installation
  c=replace(new_user_defaults(),features=FeatureSettings(False,True,False,False))
  with tempfile.TemporaryDirectory() as tmp:
   state=Path(tmp)/'new';initialize_new_installation(state,c)
   result=subprocess.run([sys.executable,'-m','quota_sentinel','--state-dir',str(state),'serve'],cwd=tmp,capture_output=True,text=True,timeout=5)
   self.assertEqual(result.returncode,0,result.stderr)
 def test_cleanup_after_leader_already_exited(self):
  import os,time,subprocess,feishu_listener
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'pid'
   code='import os,signal,time\nfrom pathlib import Path\np=os.fork()\nif p==0:\n signal.signal(signal.SIGTERM,signal.SIG_IGN)\n Path('+repr(str(path))+').write_text(str(os.getpid()))\n while True:time.sleep(.1)\nwhile not Path('+repr(str(path))+').exists():time.sleep(.01)\n'
   from quota_sentinel.platform.process import spawn_owned
   process=spawn_owned((sys.executable,'-c',code),cwd=tmp,environment=os.environ)
   process.wait(timeout=5);pid=int(path.read_text())
   try:
    feishu_listener.terminate_process_group(process)
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
     try:os.kill(pid,0)
     except ProcessLookupError:break
     time.sleep(.02)
    else:self.fail('child survives completed leader')
   finally:
    process.close()
    try:os.kill(pid,signal.SIGKILL)
    except ProcessLookupError:pass
class AppliedProfileTests(unittest.TestCase):
 def setUp(self):
  import os
  from unittest.mock import patch
  from quota_sentinel.state.new_installation import initialize_new_installation
  self.temporary = tempfile.TemporaryDirectory()
  self.addCleanup(self.temporary.cleanup)
  self.root = Path(self.temporary.name).resolve()
  self.environment = patch.dict(os.environ, {'HOME': str(self.root),
      'QUOTA_SENTINEL_KEYCHAIN_DISABLED': '1'}, clear=True)
  self.environment.start()
  self.addCleanup(self.environment.stop)
  self.state = self.root / 'state'
  budgets = {name: dict(values) for name, values in new_user_defaults().budgets.items()}
  budgets['codex']['timeout'] = 90
  self.config = replace(new_user_defaults(), budgets=budgets)
  initialize_new_installation(self.state, self.config)
  self.path = self.state / 'config.json'

 def edited_profile(self):
  budgets = {name: dict(values) for name, values in self.config.budgets.items()}
  budgets['codex']['timeout'] = 2000
  providers = dict(self.config.providers)
  providers['codex'] = replace(providers['codex'], opening_chain=('pi', 'codex'))
  providers['antigravity'] = default_provider('antigravity', enabled=True)
  return replace(self.config, budgets=budgets, providers=providers,
      features=replace(self.config.features, quota_queries=False))

 def child_application(self, command):
  from types import SimpleNamespace
  from quota_sentinel.__main__ import _application, build_parser
  class Factory:
   def create_application(self, state_dir, **options):
    return SimpleNamespace(**options)
  args = build_parser().parse_args(command[3:])
  return _application(Factory(), self.state, args, args.command)

 def test_selected_host_honors_explicit_check_timeout(self):
  import os
  from unittest.mock import patch
  from quota_sentinel import daemon
  with patch.dict(os.environ, {'QUOTA_SENTINEL_CHECK_TIMEOUT': '123'}):
   def inspect(config, plan, *, scheduler_factory, listener_factory):
    owner = scheduler_factory()
    self.assertEqual(owner.check_timeout, 123)
    return 0
   with patch.object(daemon, 'serve', side_effect=inspect):
    self.assertEqual(daemon.run_selected_host(self.config,
        build_runtime_plan(self.config, 'serve'), self.state, self.path), 0)

 def test_applied_profile_creates_private_child_under_temp_container(self):
  from unittest.mock import patch
  from quota_sentinel import daemon
  from quota_sentinel.platform.files import private_directory as real_directory
  protected=[]
  def protect(path,**options):
   path=Path(path)
   # Elevated Windows tokens can give the stdlib-created container an
   # Administrators owner; private_directory must keep refusing that owner.
   if path.name.startswith('quota-sentinel-applied-'):
    raise OSError('synthetic elevated-token owner must not be adopted')
   result=real_directory(path,**options);protected.append(path);return result
  with patch('quota_sentinel.platform.files.private_directory',side_effect=protect),patch.object(daemon,'serve',return_value=0):
   self.assertEqual(daemon.run_selected_host(self.config,
       build_runtime_plan(self.config,'serve'),self.state,self.path,
       activation_snapshot=(set(),None)),0)
  self.assertTrue(protected)
  self.assertFalse(protected[0].exists(),'applied profile child must be removed after the host exits')

 def test_host_children_keep_applied_profile_until_next_host(self):
  import os
  from unittest.mock import patch
  from quota_sentinel import daemon
  from quota_sentinel.config import read_config, save_config
  snapshot_paths = []
  original = self.path.read_bytes()
  def inspect(config, plan, *, scheduler_factory, listener_factory):
   owner = scheduler_factory()
   revision = read_config(self.path).revision
   save_config(self.path, self.edited_profile(), expected_revision=revision)
   child = self.child_application(owner.scheduler_command)
   self.assertEqual(child.software_config.budgets['codex']['timeout'], 90)
   self.assertEqual(child.runtime_plan.opening_providers, ('codex',))
   self.assertEqual(child.runtime_plan.opening_chains['codex'], ('codex',))
   self.assertTrue(child.software_config.features.quota_queries)
   snapshot = Path(owner.scheduler_command[owner.scheduler_command.index('--config') + 1])
   self.assertEqual(child.activation_path, snapshot.with_suffix('.activations.json'))
   self.assertNotEqual(snapshot, self.path)
   self.assertTrue(snapshot.is_file())
   if os.name != 'nt':
    self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
    self.assertEqual(snapshot.parent.stat().st_mode & 0o777, 0o700)
   snapshot_paths.append(snapshot)
   return 0
  with patch.object(daemon, 'serve', side_effect=inspect):
   self.assertEqual(daemon.run_selected_host(self.config,
       build_runtime_plan(self.config, 'serve'), self.state, self.path), 0)
  self.assertFalse(snapshot_paths[0].exists())
  self.assertNotEqual(self.path.read_bytes(), original)
  def inspect_reapplied(config, plan, *, scheduler_factory, listener_factory):
   child = self.child_application(scheduler_factory().scheduler_command)
   self.assertEqual(child.software_config.budgets['codex']['timeout'], 2000)
   self.assertCountEqual(child.runtime_plan.opening_providers, ('codex', 'antigravity'))
   self.assertFalse(child.software_config.features.quota_queries)
   return 0
  applied = read_config(self.path).settings
  with patch.object(daemon, 'serve', side_effect=inspect_reapplied):
   self.assertEqual(daemon.run_selected_host(applied,
       build_runtime_plan(applied, 'serve'), self.state, self.path), 0)

 def test_listener_usage_uses_the_same_applied_profile(self):
  import asyncio
  import types
  from unittest.mock import patch, Mock, AsyncMock
  from quota_sentinel import daemon
  from quota_sentinel.config import read_config, save_config
  import quota_sentinel.helpers.feishu_listener as listener_module
  self.config = replace(self.config,
      features=replace(self.config.features, feishu_listener=True))
  save_config(self.path, self.config, expected_revision=read_config(self.path).revision)
  loop = asyncio.new_event_loop()
  self.addCleanup(loop.close)
  sdk = Mock()
  sdk.ws.Client.return_value._disconnect = AsyncMock()
  credentials = Mock()
  credentials.get.side_effect = {'app_id': 'fixture-app',
      'app_secret': 'fixture-secret', 'user_id': 'fixture-user'}.get
  snapshots = []
  def inspect(config, plan, *, scheduler_factory, listener_factory):
   owner = scheduler_factory()
   listener = listener_factory(owner)
   snapshot = Path(owner.scheduler_command[owner.scheduler_command.index('--config') + 1])
   def inspect_usage():
    save_config(self.path, self.edited_profile(), expected_revision=read_config(self.path).revision)
    try:
     child = self.child_application(listener_module.USAGE_COMMAND)
    except ConfigurationError as exc:
     self.fail('saved edits changed the applied listener query: ' + str(exc))
    self.assertEqual(child.software_config.budgets['codex']['timeout'], 90)
    self.assertTrue(child.software_config.features.quota_queries)
    self.assertEqual(child.runtime_plan.active_providers, ('codex',))
    self.assertEqual(child.activation_path, snapshot.with_suffix('.activations.json'))
    self.assertTrue(listener_module.commands_enabled)
    usage_snapshot = Path(listener_module.USAGE_COMMAND[listener_module.USAGE_COMMAND.index('--config') + 1])
    self.assertEqual(usage_snapshot, snapshot)
    snapshots.append(snapshot)
   sdk.ws.Client.return_value.start.side_effect = inspect_usage
   try:
    listener.run()
   finally:
    listener.stop()
   return 0
  with patch.object(daemon, 'serve', side_effect=inspect), \
       patch.object(listener_module, '_load_sdk', return_value=sdk), \
       patch('quota_sentinel.runtime.feishu.SelectedCredentials', return_value=credentials), \
       patch.multiple(listener_module, command_environment=None,
           commands_enabled=True, commands_closing=False, AUTHORIZED_USER_ID=None,
           TASK_ORCHESTRATOR=None, command_executor=listener_module.command_executor,
           USAGE_COMMAND=listener_module.USAGE_COMMAND,
           USAGE_COMMAND_TIMEOUT_SECONDS=listener_module.USAGE_COMMAND_TIMEOUT_SECONDS), \
       patch.dict(sys.modules, {'lark_oapi.ws.client': types.SimpleNamespace(loop=loop)}):
   self.assertEqual(daemon.run_selected_host(self.config,
       build_runtime_plan(self.config, 'serve'), self.state, self.path), 0)
  self.assertEqual(len(snapshots), 1)
  self.assertFalse(snapshots[0].exists())

 def test_applied_profile_is_removed_after_startup_exception(self):
  from unittest.mock import patch
  from quota_sentinel import daemon
  snapshots = []
  def fail(config, plan, *, scheduler_factory, listener_factory):
   command = scheduler_factory().scheduler_command
   snapshot = Path(command[command.index('--config') + 1])
   snapshots.append(snapshot)
   raise RuntimeError('fixture startup failure')
  with patch.object(daemon, 'serve', side_effect=fail):
   with self.assertRaisesRegex(RuntimeError, 'fixture startup failure'):
    daemon.run_selected_host(self.config, build_runtime_plan(self.config, 'serve'),
        self.state, self.path)
  self.assertFalse(snapshots[0].exists())
  self.assertTrue(self.path.is_file())

 def test_applied_journal_acknowledges_each_provider_without_losing_later_edits(self):
  from unittest.mock import patch
  from quota_sentinel import daemon
  from quota_sentinel.state.activation import persist_activation,read_journal
  providers=dict(self.config.providers)
  providers['opencode']=default_provider('opencode',enabled=True)
  self.config=replace(self.config,providers=providers)
  revision=save_config(self.path,self.config,expected_revision=read_config(self.path).revision)
  disabled=replace(self.config,features=replace(self.config.features,automatic_opening=False))
  revision=save_config(self.path,disabled,expected_revision=revision)
  save_config(self.path,self.config,expected_revision=revision)
  source=self.path.with_suffix('.activations.json')
  def inspect(config,plan,*,scheduler_factory,listener_factory):
   child=self.child_application(scheduler_factory().scheduler_command)
   self.assertNotEqual(child.activation_path,source,'applied journal must be isolated from saved edits')
   pending,revision=read_journal(child.activation_path)
   self.assertEqual(pending,{'codex','opencode'})
   revision=persist_activation(self.state,('codex','opencode'),{'opencode'},child.activation_path,revision)
   self.assertEqual(read_journal(source)[0],{'opencode'})
   revision=persist_activation(self.state,('codex','opencode'),set(),child.activation_path,revision)
   self.assertEqual(read_journal(source)[0],set(),'a second provider must acknowledge the same applied generation')
   return 0
  with patch.object(daemon,'serve',side_effect=inspect):
   daemon.run_selected_host(self.config,build_runtime_plan(self.config,'serve'),self.state,self.path)
  revision=save_config(self.path,disabled,expected_revision=read_config(self.path).revision)
  save_config(self.path,self.config,expected_revision=revision)
  def inspect_new_edit(config,plan,*,scheduler_factory,listener_factory):
   child=self.child_application(scheduler_factory().scheduler_command)
   pending,revision=read_journal(child.activation_path)
   later=dict(self.config.providers);later['opencode']=replace(later['opencode'],enabled=False)
   save_config(self.path,replace(self.config,providers=later),expected_revision=read_config(self.path).revision)
   saved_revision=read_journal(source)[1]
   persist_activation(self.state,('codex','opencode'),set(),child.activation_path,revision)
   self.assertEqual(read_journal(source),({'codex','opencode'},saved_revision),
       'acknowledging an applied snapshot must preserve the newer saved generation')
   self.assertEqual(read_journal(child.activation_path)[0],set())
   return 0
  with patch.object(daemon,'serve',side_effect=inspect_new_edit):
   daemon.run_selected_host(self.config,build_runtime_plan(self.config,'serve'),self.state,self.path)

 def test_serve_captures_profile_and_journal_under_the_same_configuration_lock(self):
  import threading
  from unittest.mock import patch
  from quota_sentinel import daemon,__main__ as cli
  completed=threading.Event();threads=[];errors=[]
  original=cli._effective_settings
  disabled=replace(self.config,features=replace(self.config.features,automatic_opening=False))
  def resolve(state,args):
   result=original(state,args)
   def edit():
    try:save_config(self.path,disabled,expected_revision=result[1].revision)
    except Exception as exc:errors.append(exc)
    finally:completed.set()
   thread=threading.Thread(target=edit);threads.append(thread);thread.start()
   self.assertFalse(completed.wait(.1),'a save cannot interleave profile and journal capture')
   return result
  def host(config,plan,state,path,**options):
   self.assertTrue(completed.wait(3));self.assertFalse(errors)
   self.assertTrue(config.features.automatic_opening)
   self.assertIn('activation_snapshot',options)
   self.assertEqual(options['activation_snapshot'],(set(),None))
   self.assertEqual(json.loads(self.path.with_suffix('.activations.json').read_bytes())['pending'],['codex'])
   return 0
  args=cli.build_parser().parse_args(['--config',str(self.path),'serve'])
  try:
   with patch.object(cli,'_effective_settings',side_effect=resolve),patch.object(daemon,'run_selected_host',side_effect=host):
    self.assertEqual(cli.run_serve(self.state,args),0)
  finally:
   for thread in threads:thread.join(3)

 def test_applied_journal_provisions_its_native_lock_protocol(self):
  from unittest.mock import patch
  from quota_sentinel import daemon
  from quota_sentinel.platform import locks
  from quota_sentinel.app import Application
  for system in ('Linux','Windows'):
   with self.subTest(system=system),patch.object(locks.platform,'system',return_value=system):
    def inspect(config,plan,*,scheduler_factory,listener_factory):
     child=self.child_application(scheduler_factory().scheduler_command)
     try:protocol=locks.state_protocol(child.activation_path.parent,system=system)
     except locks.LockProtocolError as exc:self.fail('applied journal cannot be locked: '+str(exc))
     self.assertEqual(protocol,locks.expected_protocol(system))
     if system=='Linux':
      app=Application(self.state,None,None,None,runtime_plan=build_runtime_plan(config,'check'))
      app.activation_path=child.activation_path
      self.assertEqual(app._active_transitions(),set(),'real POSIX locks must permit activation persistence')
     return 0
    with patch.object(daemon,'serve',side_effect=inspect):
     daemon.run_selected_host(self.config,build_runtime_plan(self.config,'serve'),self.state,self.path,
         activation_snapshot=(set(),None))

if __name__=='__main__':unittest.main()
