import sys,tempfile,unittest,time
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import *
from quota_sentinel.config.migration import capture_legacy_config
from quota_sentinel.quota import Tier
from quota_sentinel.quota.models import ProviderQuota,QuotaWindow
from quota_sentinel.runtime.quota_probe import QuotaCollector,QuotaReading
from quota_sentinel.runtime.selection import build_runtime_plan
from quota_sentinel.runtime.factory import create_application
from quota_sentinel.state.new_installation import initialize_new_installation
class IntegrationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.state=self.root/'state';self.now=int(time.time());self.q=ProviderQuota('Pi · live',True,False,self.now,QuotaWindow(80,self.now+18000),QuotaWindow(90,self.now+604800))
 def collector(self,chain,success=True):
  self.calls=[];owner=self
  class Live:
   def query(self,p):owner.calls.append('pi-live');return QuotaReading(owner.q if success else None,Tier.PI_LIVE,success)
  class Collector(QuotaCollector):
   def _native(self,p):owner.calls.append('native');return owner.q
   def _read_pi(self,*a):owner.calls.append('pi-snapshot');return replace(owner.q,fresh=False,cached=True,source='Pi 快照')
  return Collector(self.state,self.root,providers=('codex',),tier_chains={'codex':chain},pi_live_client=Live())
 def test_custom_native_pi_live_order(self):
  self.assertEqual(self.collector(('pi-live','native')).collect()['codex'].tier,Tier.PI_LIVE);self.assertEqual(self.calls,['pi-live'])
  self.assertEqual(self.collector(('native','pi-live')).collect()['codex'].tier,Tier.NATIVE);self.assertEqual(self.calls,['native'])
 def test_unavailable_pi_live_only_listed_fallback(self):
  self.assertEqual(self.collector(('pi-live','pi-snapshot'),False).collect()['codex'].tier,Tier.PI_SNAPSHOT);self.assertEqual(self.calls,['pi-live','pi-snapshot'])
  self.assertIsNone(self.collector(('pi-live',),False).collect()['codex'].quota);self.assertEqual(self.calls,['pi-live'])
 def test_fresh_and_personal_defaults_unchanged(self):
  for c in (new_user_defaults(),capture_legacy_config({},installed_preferences={})):
   self.assertFalse(any('pi-live' in v.quota_chain for v in c.providers.values()))
  d=to_document(new_user_defaults());d['providers']['clinepass']['quota_chain']=['pi-live']
  with self.assertRaises(ConfigurationError):parse_config(d)
 def config(self):
  d=to_document(new_user_defaults());d['providers']['codex']['quota_chain']=['pi-live'];d['clients']={'node':sys.executable,'codex':sys.executable,'pi_sdk':str(self.root/'missing'),'pi_auth':str(self.root/'missing-auth')};d['app']['quota_wait']=0
  return parse_config(d)
 def test_missing_sdk_query_failure_zero_models(self):
  c=self.config();initialize_new_installation(self.state,c)
  with patch('quota_sentinel.runtime.models.ModelRunner.prepare',side_effect=AssertionError('model')),patch('quota_sentinel.runtime.codex_exec.CodexExecRunner.prepare',side_effect=AssertionError('model')):
   app=create_application(self.state,software_config=c,runtime_plan=build_runtime_plan(c,'usage'),environment={});r=app.quota_collector_factory(self.root).collect()['codex'];self.assertFalse(r.fresh);self.assertIsNone(r.quota)
 def test_pi_live_updates_anchor_only_on_success(self):
  from quota_sentinel.scheduler.service import load_state
  c=self.config();initialize_new_installation(self.state,c)
  app=create_application(self.state,software_config=c,runtime_plan=build_runtime_plan(c,'check'),environment={},clock=lambda:self.now)
  with patch('quota_sentinel.runtime.pi_live.PiLiveQuotaClient.query',return_value=QuotaReading(self.q,Tier.PI_LIVE,True)),patch('quota_sentinel.runtime.codex_exec.CodexExecRunner.prepare',side_effect=AssertionError('not due')):app.check()
  state=load_state(self.state,'codex');self.assertEqual(state.reset_anchor,self.q.five_hour.reset_at)
  with patch('quota_sentinel.runtime.pi_live.PiLiveQuotaClient.query',return_value=QuotaReading(None,Tier.PI_LIVE,False)):app.check()
  self.assertEqual(load_state(self.state,'codex'),state)
if __name__=='__main__':unittest.main()
