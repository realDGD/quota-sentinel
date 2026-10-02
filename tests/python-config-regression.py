"""Configuration contracts: reject invalid choices and preserve prior edits."""
import sys,json,tempfile,unittest,threading
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import new_user_defaults,parse_config,read_config,save_config,ConfigurationError,to_document
class ConfigTests(unittest.TestCase):
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
