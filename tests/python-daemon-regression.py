import sys,unittest,signal,tempfile,json
from pathlib import Path
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import *
from quota_sentinel.runtime.selection import build_runtime_plan
from quota_sentinel.daemon import serve
class DaemonTests(unittest.TestCase):
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
if __name__=='__main__':unittest.main()
