"""Fallback is owned here; concrete configured runners are terminal."""
from pathlib import Path
from threading import RLock
from quota_sentinel.config import ConfigurationError
class AttemptChainRunner:
 def __init__(self,chains,runner_factory):
  self.chains=dict(chains);self.factory=runner_factory;self.runners={};self.prepared={};self.lock=RLock()
 def _runner(self,name):
  with self.lock:
   if name not in self.runners:self.runners[name]=self.factory(name)
   return self.runners[name]
 def _workspace(self,workspace,ch):
  return Path(workspace)/('channel-'+ch)
 def prepare(self,provider,workspace):
  key=(provider,Path(workspace)); failures=[]
  for i,ch in enumerate(self.chains.get(provider,())):
   try:
    paths=self._runner(ch).prepare(provider,self._workspace(workspace,ch));self.prepared[key]=(i,paths);return paths
   except Exception as e:failures.append(str(e) if isinstance(e,ConfigurationError) else type(e).__name__)
  raise ConfigurationError('no ready opening channel for '+provider+' ('+', '.join(failures)+')')
 def run(self,provider,workspace,phase,attempt,limit):
  key=(provider,Path(workspace))
  if key not in self.prepared:self.prepare(provider,workspace)
  start,_=self.prepared[key];last=None
  for i,ch in enumerate(self.chains[provider]):
   if i<start:continue
   try:
    runner=self._runner(ch);w=self._workspace(workspace,ch)
    if i!=start:runner.prepare(provider,w)
    last=runner.run(provider,w,phase,attempt,limit)
    if last.success:return last
   except Exception:
    if i==len(self.chains[provider])-1 and last is None:raise
  if last is None:raise ConfigurationError('no usable opening channel for '+provider)
  return last
