import sys,tempfile,unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import *
from quota_sentinel.runtime.selection import build_runtime_plan
from quota_sentinel.runtime.quota_probe import QuotaCollector,QuotaReading
from quota_sentinel.quota.adapters import Tier
from quota_sentinel.quota.models import ProviderQuota,QuotaWindow
from quota_sentinel.app import Application
from quota_sentinel.scheduler import service
from quota_sentinel.scheduler.models import QuotaObservation
from quota_sentinel.state.new_installation import initialize_new_installation
class PruningTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name);self.state=self.root/'state';initialize_new_installation(self.state,new_user_defaults())
 def quota(self,fresh):return ProviderQuota('test',fresh,not fresh,1000,QuotaWindow(80,4000),QuotaWindow(90,8000))
 def collector(self):
  owner=self
  class C(QuotaCollector):
   def _tier(self,p,t,raw):owner.calls.append((p,t));return owner.quota(t is Tier.NATIVE)
  self.calls=[]
  return C(self.state,self.root,providers=('codex',),tier_chains={'codex':(Tier.CODEXBAR_CACHE,Tier.NATIVE)})
 def test_disabled_dependencies_never_called(self):
  c=self.collector();self.assertEqual(tuple(c.collect()),('codex',));self.assertEqual([x[0] for x in self.calls],['codex'])
 def test_cached_first_is_display_only(self):
  r=self.collector().collect()['codex'];self.assertFalse(r.fresh);self.assertEqual(r.tier,Tier.CODEXBAR_CACHE)
 def test_scheduler_skips_cached_result(self):
  r=self.collector().collect(purpose='schedule')['codex'];self.assertTrue(r.fresh);self.assertEqual(r.tier,Tier.NATIVE);self.assertEqual(self.calls,[('codex',Tier.CODEXBAR_CACHE),('codex',Tier.NATIVE)])
 def test_reenable_does_not_replay_debt(self):
  service.begin_attempt(self.state,'codex',100)
  before=service.load_state(self.state,'codex');service.resume_provider(self.state,'codex',QuotaObservation(False,4000),1000);self.assertEqual(service.load_state(self.state,'codex'),before)
  result=service.resume_provider(self.state,'codex',QuotaObservation(True,4000),1000);self.assertFalse(result.state.retry_pending);self.assertEqual(result.state.reset_anchor,4000);self.assertGreater(result.state.next_due_at,1000)
 def test_inactive_roster_preserves_history(self):
  service.begin_attempt(self.state,'antigravity',100);before=service.load_state(self.state,'antigravity')
  c=new_user_defaults();p=build_runtime_plan(c,'check');events=[];owner=self
  class Collector:
   def collect(self,*a,**k):return {'codex':QuotaReading(owner.quota(True),Tier.NATIVE,True)}
   def save_pi_snapshots(self,raw):pass
  class Runner:
   def prepare(self,*a):events.append('prepare')
   def run(self,*a):events.append('run');raise AssertionError('no due models expected')
  class N:
   def validate_ready(self):pass
   def task(self,*a):events.append('card')
  app=Application(self.state,Runner(),lambda w:Collector(),N(),clock=lambda:1000,runtime_plan=p)
  app.check();self.assertNotIn('run',events);self.assertEqual(service.load_state(self.state,'antigravity'),before);self.assertEqual(tuple(app.status()),('codex',))
 def test_reenable_without_disabled_tick_does_not_replay_debt(self):
  owner=self;events=[]
  class C:
   def collect(self,*a,**k):return {'codex':QuotaReading(owner.quota(True),Tier.NATIVE,True)}
   def save_pi_snapshots(self,raw):pass
  class R:
   def prepare(self,*a):events.append('prepare');raise AssertionError('old debt replayed')
  class N:
   def validate_ready(self):pass
   def task(self,*a):events.append('card')
  c=new_user_defaults();p=build_runtime_plan(c,'check');app=Application(self.state,R(),lambda w:C(),N(),clock=lambda:1000,runtime_plan=p)
  app.check();service.begin_attempt(self.state,'codex',100)
  path=self.state/'config.json';saved=read_config(path);d=to_document(c);d['providers']['codex']['enabled']=False
  rev=save_config(path,parse_config(d),expected_revision=saved.revision)
  save_config(path,c,expected_revision=rev)
  app.check();self.assertEqual(events,[]);self.assertFalse(service.load_state(self.state,'codex').retry_pending)
 def test_direct_opening_without_snapshot_does_not_issue_usage(self):
  from quota_sentinel.runtime.direct import DirectRunner
  class R(DirectRunner):
   def _post_json(self,*a):return SimpleNamespace(stdout='{"choices":[{"message":{"content":"1"}}]}',status='200',returncode=0,timed_out=False,error='')
   def _write_snapshot(self,*a):raise AssertionError('unselected snapshot request')
  r=R(key_reader=lambda p:'fake',capture_providers=frozenset());r.prepare('opencode',self.root)
  self.assertTrue(r.run('opencode',self.root,'initial',1,1).success)
 def test_native_cli_auth_without_auth_json(self):
  from quota_sentinel.runtime.factory import create_application
  cli=self.root/'codex';seen=self.root/'seen';cli.write_text('#!'+sys.executable+'\nimport sys\nfrom pathlib import Path\nassert sys.argv[1:] == ["login","status"]\nPath('+repr(str(seen))+').write_text("metadata")\n');cli.chmod(0o700)
  d=to_document(new_user_defaults());d['clients']={'codex':str(cli),'codex_home':str(self.root/'no-auth-file')};c=parse_config(d)
  app=create_application(self.state,software_config=c,runtime_plan=build_runtime_plan(c,'run'),environment={})
  app.model_runner.prepare('codex',self.root/'work');self.assertTrue(seen.exists(),'official-client auth status must be checked before an attempt')
 def test_only_opening_internal_observation_and_bot_refusal(self):
  c=replace(new_user_defaults(),features=FeatureSettings(True,False,False,False));self.assertEqual(build_runtime_plan(c,'check').probe_providers,('codex',))
  with self.assertRaises(ConfigurationError):build_runtime_plan(c,'usage')
if __name__=='__main__':unittest.main()
