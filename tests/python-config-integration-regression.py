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

if __name__=='__main__':unittest.main()
