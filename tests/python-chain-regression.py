import sys,unittest,tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.runtime.chains import AttemptChainRunner
class ChainTests(unittest.TestCase):
 def chain(self,names,success):
  events=[]
  class Runner:
   def __init__(self,name):self.name=name
   def prepare(self,p,w):events.append(('prepare',self.name));return SimpleNamespace()
   def run(self,*args):events.append(('run',self.name));return SimpleNamespace(success=success.get(self.name,False),error_summary='cost-regression')
  return AttemptChainRunner({'codex':names},lambda name:Runner(name)),events
 def test_single_channel_no_fallback(self):
  r,e=self.chain(('codex',),{});r.prepare('codex',Path('/tmp'));self.assertFalse(r.run('codex',Path('/tmp'),'initial',1,3).success);self.assertEqual(e,[('prepare','codex'),('run','codex')])
 def test_both_pi_codex_orders(self):
  for names in (('pi','codex'),('codex','pi')):
   r,e=self.chain(names,{names[1]:True});r.prepare('codex',Path('/tmp'));self.assertTrue(r.run('codex',Path('/tmp'),'initial',1,3).success);self.assertEqual(e,[('prepare',names[0]),('run',names[0]),('prepare',names[1]),('run',names[1])])
 def test_cost_success_stops(self):
  r,e=self.chain(('codex','pi'),{'codex':True});r.prepare('codex',Path('/tmp'));r.run('codex',Path('/tmp'),'initial',1,3);self.assertEqual(e,[('prepare','codex'),('run','codex')])
 def test_primary_prepare_reused_fallback_prepared_each_attempt(self):
  r,e=self.chain(('codex','pi'),{});r.prepare('codex',Path('/tmp'))
  for n in (1,2):r.run('codex',Path('/tmp'),'initial',n,3)
  self.assertEqual(e.count(('prepare','codex')),1);self.assertEqual(e.count(('prepare','pi')),2)
 def test_unavailable_primary_uses_only_listed_next(self):
  e=[]
  class R:
   def prepare(self,p,w):e.append('next');return None
   def run(self,*a):return SimpleNamespace(success=True)
  def factory(ch):
   if ch=='pi':raise RuntimeError('missing')
   return R()
  r=AttemptChainRunner({'codex':('pi','codex')},factory);r.prepare('codex',Path('/tmp'));self.assertTrue(r.run('codex',Path('/tmp'),'initial',1,1).success);self.assertEqual(e,['next'])
if __name__=='__main__':unittest.main()
