import sys,unittest,tempfile,json,time,os,signal
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import *
from quota_sentinel.runtime.pi_live import PiLiveQuotaClient
NOW=int(time.time())
def payload():return {'rate_limit':{'primary_window':{'used_percent':19,'reset_at':NOW+18000,'limit_window_seconds':18000},'secondary_window':{'used_percent':8,'reset_at':NOW+604800,'limit_window_seconds':604800}}}
class ClientTests(unittest.TestCase):
 def config(self,selected=True):
  d=to_document(new_user_defaults());d['providers']['codex']['quota_chain']=['pi-live'] if selected else ['native'];return parse_config(d)
 def client(self,change=lambda r:r,selected=True):
  self.calls=[]
  def run(command,timeout,grace,stdin,**kw):
   self.calls.append((command,timeout,grace,json.loads(stdin),kw));q=json.loads(stdin);r={'protocol_version':1,'request_id':q['request_id'],'provider':q['provider'],'status':'ok','account_scope':'pi:codex:'+'a'*64,'queried_at':NOW,'payload':payload()}
   raw=change(r);return SimpleNamespace(stdout=raw if isinstance(raw,bytes) else json.dumps(raw).encode(),stderr=b'fixture-secret',returncode=0,timed_out=False)
  return PiLiveQuotaClient(self.config(selected),helper_path=Path('/fake/helper'),node_bin=Path('/fake/node'),run_bounded=run)
 def test_verified_live_result(self):
  result=self.client().query('codex');self.assertTrue(result.fresh);self.assertEqual(result.quota.five_hour.remaining_percent,81);self.assertNotIn('fixture-secret',str(self.calls))
 def test_nonce_and_provider_mismatch(self):
  for key,value in (('request_id','old-nonce'),('provider','opencode'),('protocol_version',2),('queried_at',NOW-10000),('account_scope','foreign'),('status','unknown')):
   with self.subTest(key=key):self.assertFalse(self.client(lambda r:dict(r,**{key:value})).query('codex').fresh)
 def test_truncated_or_oversized_result(self):
  for raw in (b'{',b'{}\n{}',b'x'*1048577):self.assertFalse(self.client(lambda r:raw).query('codex').fresh)
 def test_secret_redaction(self):
  r=self.client(lambda r:dict(r,status='error',error_code='fixture-secret')).query('codex');self.assertNotIn('fixture-secret',str(r));self.assertIsNone(r.quota)
 def test_no_helper_on_unselected_tier(self):
  self.assertFalse(self.client(selected=False).query('codex').fresh);self.assertEqual(self.calls,[])
 def test_hung_helper_no_survivors(self):
  from quota_sentinel.runtime.quota_probe import _run_bounded
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);pidfile=root/'pid';helper=root/'helper.py'
   helper.write_text('import os,sys,signal,time\nfrom pathlib import Path\nsys.stdin.read()\np=os.fork()\nif p==0:\n signal.signal(signal.SIGTERM,signal.SIG_IGN)\n Path('+repr(str(pidfile))+').write_text(str(os.getpid()))\n while True:time.sleep(.1)\nwhile True:time.sleep(.1)\n')
   d=to_document(self.config());d['budgets']['pi_live'].update(timeout=.4,kill_grace=.1)
   result=PiLiveQuotaClient(parse_config(d),helper_path=helper,node_bin=Path(sys.executable),run_bounded=_run_bounded).query('codex')
   self.assertFalse(result.fresh);self.assertEqual(result.error,'pi_live_timeout');pid=int(pidfile.read_text());deadline=time.monotonic()+3
   try:
    while time.monotonic()<deadline:
     try:os.kill(pid,0)
     except ProcessLookupError:break
     time.sleep(.02)
    else:self.fail('owned query descendant survived')
   finally:
    try:os.kill(pid,signal.SIGKILL)
    except ProcessLookupError:pass
if __name__=='__main__':unittest.main()
