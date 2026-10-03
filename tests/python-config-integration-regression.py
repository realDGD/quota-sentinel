"""Use real composition/state and fake external clients; never supplier auth."""
import sys,tempfile,unittest,json,os
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import *
from quota_sentinel.config.migration import capture_legacy_config
from quota_sentinel.state.new_installation import initialize_new_installation
from quota_sentinel.runtime.selection import build_runtime_plan
from quota_sentinel.runtime.factory import create_application

class IntegrationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.state=self.root/'state';self.log=self.root/'calls';self.used=30
 def config(self,*,quota=True):
  cli=self.root/'codex'
  cli.write_text('#!'+sys.executable+'\n'+f'''import sys,json,time
from pathlib import Path
with Path({str(self.log)!r}).open('a') as log:log.write(' '.join(sys.argv[1:2])+'\\n')
if sys.argv[1:]==['login','status']:raise SystemExit(0)
if sys.argv[1:]==['--version']:print('codex fixture');raise SystemExit(0)
if sys.argv[1]=='exec':
 print(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":"1"}}}}))
 print(json.dumps({{"type":"turn.completed","usage":{{"input_tokens":50,"output_tokens":1}}}}));raise SystemExit(0)
if sys.argv[1]=='app-server':
 for line in sys.stdin:
  req=json.loads(line);value={{}}
  if req['method']=='account/rateLimits/read':
   a={{'windowDurationMins':300,'resetsAt':int(time.time())+18000}};b={{'usedPercent':10,'windowDurationMins':10080,'resetsAt':int(time.time())+604800}}
   if {self.used!r} is not None:a['usedPercent']={self.used!r}
   value={{'rateLimits':{{'primary':a,'secondary':b}}}}
  print(json.dumps({{'id':req['id'],'result':value}}),flush=True)
''');cli.chmod(0o700)
  forbidden=self.root/'forbidden';forbidden.write_text('#!'+sys.executable+'\nraise RuntimeError("unselected external client invoked")\n');forbidden.chmod(0o700)
  d=to_document(new_user_defaults());d['clients']={'codex':str(cli),'pi':str(forbidden),'agy':str(forbidden),'codexbar':str(forbidden),'uv':str(forbidden),'curl':str(forbidden),'codex_home':str(self.root/'codex-profile')};d['app'].update(initial_attempts=1,watchdog_attempts=1,quota_wait=0,retry_interval=1);d['features']['quota_queries']=quota
  return parse_config(d)
 def app(self,c,command):
  return create_application(self.state,software_config=c,runtime_plan=build_runtime_plan(c,command),environment={'HOME':str(self.root),'QUOTA_SENTINEL_CONFIG':str(self.state/'config.json')})
 def test_fresh_codex_only_end_to_end(self):
  c=self.config();initialize_new_installation(self.state,c)
  with patch('quota_sentinel.runtime.keychain.read',side_effect=AssertionError('unselected credential check')),patch('quota_sentinel.runtime.feishu.FeishuClient',side_effect=AssertionError('unselected Feishu')):
   self.assertEqual(self.app(c,'run').run(),{'codex':'发送成功'})
   self.app(c,'usage').usage()
  self.assertEqual(set(self.log.read_text().splitlines()),{'login','--version','exec','app-server'})
  from quota_sentinel.scheduler.service import load_state
  self.assertIsNotNone(load_state(self.state,'codex').last_attempt_at)
  self.assertIsNone(load_state(self.state,'antigravity').last_attempt_at)
 def test_personal_migration_end_to_end(self):
  c=capture_legacy_config({'HOME':str(self.root)},installed_preferences={'QUOTA_SENTINEL_ORCHESTRATOR_ENABLED':'1'})
  initialize_new_installation(self.state,c);saved=read_config(self.state/'config.json').settings
  self.assertEqual(saved,c);self.assertEqual(saved.providers['codex'].opening_chain,('pi','codex'));self.assertNotIn('pi-live',saved.providers['codex'].quota_chain)
  self.assertFalse(new_user_defaults().features.feishu_listener)
 def test_direct_pi_orders_with_plugin_failure(self):
  from quota_sentinel.runtime.chains import AttemptChainRunner
  from quota_sentinel.runtime.models import ModelRunner,ModelRunnerConfig
  from quota_sentinel.runtime.direct import DirectRunner
  class Direct(DirectRunner):
   def _post_json(self,*args):return SimpleNamespace(stdout='{"success":true,"data":{"choices":[{"message":{"content":"1"}}]}}',status='200',returncode=0,timed_out=False,error='')
  for order in (('pi','direct'),('direct','pi')):
   with self.subTest(order=order):
    pi=ModelRunner(replace(ModelRunnerConfig.from_env({'HOME':str(self.root)}),pi_bin=self.root/'missing-pi',verify_plugins=True))
    direct=Direct(key_reader=lambda p:'fixture-key',capture_providers=frozenset())
    runner=AttemptChainRunner({'clinepass':order},lambda ch:pi if ch=='pi' else direct)
    work=self.root/('work-'+order[0]);runner.prepare('clinepass',work)
    self.assertTrue(runner.run('clinepass',work,'initial',1,1).success)
 def test_disabled_bot_capabilities_end_to_end(self):
  c=self.config(quota=False);initialize_new_installation(self.state,c)
  from quota_sentinel.__main__ import main
  self.assertEqual(main(['--state-dir',str(self.state),'usage']),3);self.assertFalse(self.log.exists())
  self.assertEqual(main(['--state-dir',str(self.state),'status']),0);self.assertFalse(self.log.exists())
 def test_native_missing_measurement_never_claims_full_quota(self):
  self.used=None;c=self.config();initialize_new_installation(self.state,c)
  app=self.app(c,'usage');collector=app.quota_collector_factory(self.root/'probe')
  self.assertIsNone(collector.collect()['codex'].quota)
 def test_global_reenable_waits_for_fresh_quota_without_replaying_debt(self):
  from quota_sentinel.app import Application,AppConfig
  from quota_sentinel.scheduler import service
  from quota_sentinel.runtime.models import AttemptResult
  from quota_sentinel.runtime.quota_probe import QuotaReading
  from quota_sentinel.quota.adapters import Tier
  from quota_sentinel.quota.models import ProviderQuota,QuotaWindow
  c=new_user_defaults();initialize_new_installation(self.state,c);events=[]
  fresh=QuotaReading(ProviderQuota('fixture',True,False,1000,QuotaWindow(80,4000),QuotaWindow(90,8000)),Tier.NATIVE,True)
  readings={'codex':fresh}
  class Collector:
   def collect(self,*args,**kwargs):events.append('quota');return readings
   def save_pi_snapshots(self,*args):pass
  class Runner:
   def prepare(self,provider,workspace):events.append('prepare:'+provider)
   def run(self,provider,workspace,phase,attempt,limit):
    events.append(phase+':'+provider)
    return AttemptResult(False,1,False,0,workspace/'out',workspace/'err',workspace/'quota','fixture failure')
  class Notifier:
   def validate_ready(self):pass
   def task(self,*args):events.append('card')
  app=Application(self.state,Runner(),lambda _:Collector(),Notifier(),clock=lambda:1000,sleep=lambda _:None,config=AppConfig(watchdog_attempts=1,quota_wait=0),runtime_plan=build_runtime_plan(c,'check'))
  app.check();service.begin_attempt(self.state,'codex',100);debt=service.load_state(self.state,'codex')
  path=self.state/'config.json';disabled=replace(c,features=replace(c.features,automatic_opening=False))
  revision=save_config(path,disabled,expected_revision=read_config(path).revision)
  save_config(path,c,expected_revision=revision)
  cached=QuotaReading(replace(fresh.quota,fresh=False,cached=True),Tier.CODEXBAR_CACHE,False)
  expired=QuotaReading(replace(fresh.quota,five_hour=QuotaWindow(80,500)),Tier.NATIVE,True)
  for label,reading in (('missing',None),('cached',cached),('expired',expired)):
   readings={} if reading is None else {'codex':reading};events.clear()
   self.assertEqual(app.check(),())
   self.assertEqual(events,['quota'],label+' quota must not permit a model attempt during reactivation')
   self.assertEqual(service.load_state(self.state,'codex'),debt)
  readings={'codex':fresh};events.clear();self.assertEqual(app.check(),())
  self.assertEqual(events,['quota']);resumed=service.load_state(self.state,'codex')
  self.assertFalse(resumed.retry_pending);self.assertEqual(resumed.reset_anchor,4000);self.assertGreater(resumed.next_due_at,1000)
  self.assertEqual(json.loads(path.with_suffix('.activations.json').read_bytes())['pending'],[])
 def test_manual_opening_remains_available_when_automatic_opening_is_disabled(self):
  c=self.config();c=replace(c,features=replace(c.features,automatic_opening=False));initialize_new_installation(self.state,c)
  self.assertEqual(build_runtime_plan(c,'check').opening_providers,())
  self.assertEqual(self.app(c,'run').run(),{'codex':'发送成功'})
  from quota_sentinel.scheduler import service
  self.assertIsNotNone(service.load_state(self.state,'codex').last_task_at)

 def test_old_host_cannot_consume_unapplied_disable_before_reenable(self):
  from quota_sentinel import daemon
  from quota_sentinel.__main__ import _application,build_parser
  from quota_sentinel.app import Application,AppConfig
  from quota_sentinel.scheduler import service
  from quota_sentinel.runtime.models import AttemptResult
  from quota_sentinel.runtime.quota_probe import QuotaReading
  from quota_sentinel.quota.adapters import Tier
  from quota_sentinel.quota.models import ProviderQuota,QuotaWindow
  for switch in ('automatic_opening','enabled','opening_enabled'):
   with self.subTest(switch=switch):
    state=self.root/switch;c=new_user_defaults();initialize_new_installation(state,c)
    path=state/'config.json';journal=path.with_suffix('.activations.json');events=[]
    fresh=QuotaReading(ProviderQuota('fixture',True,False,1000,QuotaWindow(80,4000),QuotaWindow(90,8000)),Tier.NATIVE,True)
    readings={'codex':fresh}
    class Collector:
     def collect(self,*args,**kwargs):events.append('quota');return readings
     def save_pi_snapshots(self,*args):pass
    class Runner:
     def prepare(self,provider,workspace):events.append('prepare:'+provider)
     def run(self,provider,workspace,phase,attempt,limit):
      events.append(phase+':'+provider)
      return AttemptResult(False,1,False,0,workspace/'out',workspace/'err',workspace/'quota','fixture failure')
    class Notifier:
     def validate_ready(self):pass
     def task(self,*args):events.append('card')
    class Factory:
     def create_application(self,state_dir,**options):
      return Application(state_dir,Runner(),lambda _:Collector(),Notifier(),clock=lambda:1000,sleep=lambda _:None,
          config=AppConfig(watchdog_attempts=1,watchdog_retry_gap=0,quota_wait=0),runtime_plan=options['runtime_plan'])
    def child(command):
     args=build_parser().parse_args(command[3:])
     return _application(Factory(),state,args,'check')
    def old_host(config,plan,*,scheduler_factory,listener_factory):
     command=scheduler_factory().scheduler_command
     child(command).check();service.begin_attempt(state,'codex',100)
     if switch=='automatic_opening':disabled=replace(c,features=replace(c.features,automatic_opening=False))
     else:
      providers=dict(c.providers);providers['codex']=replace(providers['codex'],**{switch:False})
      disabled=replace(c,providers=providers)
     save_config(path,disabled,expected_revision=read_config(path).revision)
     events.clear();child(command).check()
     self.assertEqual(json.loads(journal.read_bytes())['pending'],['codex'],
         'old applied checks must not acknowledge a saved, unapplied disable')
     self.assertIn('watchdog-retry:codex',events,'old checks keep their applied profile')
     self.assertTrue(service.load_state(state,'codex').retry_pending)
     save_config(path,c,expected_revision=read_config(path).revision)
     return 0
    with patch.object(daemon,'serve',side_effect=old_host):
     self.assertEqual(daemon.run_selected_host(c,build_runtime_plan(c,'serve'),state,path),0)
    def reapplied_host(config,plan,*,scheduler_factory,listener_factory):
     nonlocal readings
     command=scheduler_factory().scheduler_command;debt=service.load_state(state,'codex')
     readings={};events.clear();child(command).check()
     self.assertEqual(events,['quota'],'fresh quota must precede a reactivated model retry')
     self.assertEqual(service.load_state(state,'codex'),debt)
     self.assertEqual(json.loads(journal.read_bytes())['pending'],['codex'])
     readings={'codex':fresh};events.clear();child(command).check()
     self.assertEqual(events,['quota']);self.assertFalse(service.load_state(state,'codex').retry_pending)
     self.assertEqual(json.loads(journal.read_bytes())['pending'],[])
     return 0
    with patch.object(daemon,'serve',side_effect=reapplied_host):
     self.assertEqual(daemon.run_selected_host(c,build_runtime_plan(c,'serve'),state,path),0)

if __name__=='__main__':unittest.main()
