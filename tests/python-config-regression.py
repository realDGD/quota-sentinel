"""Configuration contracts: reject invalid choices and preserve prior edits."""
import sys,json,tempfile,unittest,threading
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import new_user_defaults,parse_config,read_config,save_config,ConfigurationError,to_document
class ConfigTests(unittest.TestCase):
 def test_unrepresentable_app_count_is_typed_configuration_error(self):
  d=to_document(new_user_defaults());d['app']['initial_attempts']=10**1000
  with self.assertRaises(ConfigurationError):parse_config(d)
 def test_zero_retry_count_allowed(self):
  d=to_document(new_user_defaults());d['budgets']['agy']['transient_attempts']=0
  self.assertEqual(parse_config(d).budgets['agy']['transient_attempts'],0)
 def test_retry_counts_and_preflight_have_discrete_ranges(self):
  for key,value in [('transient_attempts',.5),('transient_attempts',1.5),('input_ceiling',1.5),('preflight',2),('timeout',10**1000)]:
   with self.subTest(key=key,value=str(value)[:20]):
    d=to_document(new_user_defaults());d['budgets']['agy'][key]=value
    with self.assertRaises(ConfigurationError):parse_config(d)
 def test_invalid_credentials_container_is_typed(self):
  for value in (None,[], 'invalid'):
   with self.subTest(value=value),tempfile.TemporaryDirectory() as t:
    d=to_document(new_user_defaults());d['credentials']=value;p=Path(t)/'config.json';p.write_text(json.dumps(d))
    with self.assertRaises(ConfigurationError):read_config(p)
 def test_invalid_activation_journal_keeps_prior_configuration(self):
  for value in ([],{'schema_version':1,'pending':[['codex']]},{'schema_version':1,'pending':['unknown']},{'schema_version':True,'pending':['codex']}):
   with self.subTest(value=value),tempfile.TemporaryDirectory() as t:
    p=Path(t)/'config.json';c=new_user_defaults();rev=save_config(p,c,expected_revision=None);raw=p.read_bytes()
    p.with_suffix('.activations.json').write_text(json.dumps(value));providers=dict(c.providers);providers['codex']=replace(providers['codex'],enabled=False)
    with self.assertRaises(ConfigurationError):save_config(p,replace(c,providers=providers),expected_revision=rev)
    self.assertEqual(p.read_bytes(),raw)
 def test_invalid_runtime_activation_is_typed(self):
  from quota_sentinel.app import Application
  from quota_sentinel.runtime.selection import build_runtime_plan
  for values in (['unknown'],[['codex']], [1]):
   with self.subTest(value=values),tempfile.TemporaryDirectory() as t:
    p=Path(t);(p/'runtime-providers.json').write_text(json.dumps({'schema_version':1,'enabled':values,'awaiting_resume':[]}))
    app=Application(p,None,None,None,runtime_plan=build_runtime_plan(new_user_defaults(),'check'))
    with self.assertRaises(ValueError):app._active_transitions()
 def test_fresh_defaults(self):
  c=new_user_defaults(); self.assertEqual([p for p,v in c.providers.items() if v.enabled],['codex']); self.assertEqual(c.providers['codex'].opening_chain,('codex',)); self.assertEqual(c.providers['codex'].quota_chain,('native',)); self.assertFalse(c.features.feishu_push); self.assertFalse(c.features.feishu_listener)
 def test_invalid_chain_and_budget(self):
  for chain in ([],['pi','pi'],['agy'],['bad']):
   d=to_document(new_user_defaults()); d['providers']['codex']['opening_chain']=chain
   with self.assertRaises(ConfigurationError): parse_config(d)
  for value in (True,float('nan'),float('inf'),-1,0):
   d=to_document(new_user_defaults()); d['budgets']['codex']['timeout']=value
   with self.assertRaises(ConfigurationError): parse_config(d)
 def test_unknown_schema(self):
  d=to_document(new_user_defaults()); d['schema_version']=2
  with self.assertRaises(ConfigurationError): parse_config(d)
 def test_atomic_edit_failure(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'config.json'; rev=save_config(p,new_user_defaults(),expected_revision=None); raw=p.read_bytes()
   with patch('quota_sentinel.state.store.os.replace',side_effect=OSError('interrupted')):
    with self.assertRaises(OSError): save_config(p,replace(new_user_defaults(),origin='edited'),expected_revision=rev)
   self.assertEqual(p.read_bytes(),raw); self.assertEqual(read_config(p).revision,rev)
 def test_stale_editor(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'config.json'; r=save_config(p,new_user_defaults(),expected_revision=None)
   save_config(p,replace(new_user_defaults(),origin='edited'),expected_revision=r)
   with self.assertRaises(ConfigurationError): save_config(p,new_user_defaults(),expected_revision=r)
 def test_unlisted_and_immutable(self):
  d=to_document(new_user_defaults()); del d['providers']['antigravity']; c=parse_config(d)
  self.assertNotIn('antigravity',c.providers)
  with self.assertRaises(TypeError): c.providers['bad']=None
 def test_competing_editors_and_reader(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'config.json'; r=save_config(p,new_user_defaults(),expected_revision=None); outcomes=[]
   def edit(i):
    try: save_config(p,replace(new_user_defaults(),origin=str(i)),expected_revision=r); outcomes.append('saved')
    except ConfigurationError: outcomes.append('stale')
   ts=[threading.Thread(target=edit,args=(i,)) for i in range(2)]
   for x in ts:x.start()
   while any(x.is_alive() for x in ts): self.assertEqual(read_config(p).settings.schema_version,1)
   for x in ts:x.join()
   self.assertCountEqual(outcomes,['saved','stale'])
if __name__=='__main__':unittest.main()
