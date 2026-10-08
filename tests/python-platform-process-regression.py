"""Owned process cleanup, pipe bounds and argv preservation on native hosts."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
FIXTURE=Path(__file__).parent/'fixtures/process-tree.py'

def alive(pid):
 if os.name=='nt':
  import ctypes
  from ctypes import wintypes as w
  k=ctypes.WinDLL('kernel32',use_last_error=True)
  k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
  k.CloseHandle.argtypes=[w.HANDLE]
  h=k.OpenProcess(0x1000,False,pid)
  if not h:return False
  code=w.DWORD();k.GetExitCodeProcess.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD)]
  try:return bool(k.GetExitCodeProcess(h,ctypes.byref(code))) and code.value==259
  finally:k.CloseHandle(h)
 try:
  os.kill(pid,0)
  # Zombies are dead, but may await the container's init reaper on Linux.
  stat=Path('/proc')/str(pid)/'stat'
  return not (stat.exists() and stat.read_text().rsplit(')',1)[1].split()[0]=='Z')
 except ProcessLookupError:return False

class Processes(unittest.TestCase):
 def setUp(self):
  self.assertIsNotNone(importlib.util.find_spec('quota_sentinel.platform.process'),'owned-process interface missing')
  from quota_sentinel.platform import process
  self.process=process;self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.cwd=Path(self.tmp.name)
 def run_cli(self,*args,timeout=1,max_bytes=1048576,input_data=None):
  return self.process.run_bounded([sys.executable,'-X','utf8',str(FIXTURE),*args],cwd=self.cwd,environment=os.environ,input_data=input_data,timeout=timeout,kill_grace=.15,max_bytes=max_bytes)
 def assert_dead(self,pid):
  deadline=time.monotonic()+2
  while alive(pid) and time.monotonic()<deadline:time.sleep(.02)
  self.assertFalse(alive(pid),f'owned descendant {pid} survived')
 def test_timeout_kills_term_ignoring_child(self):
  p=self.cwd/'pid';started=time.monotonic();r=self.run_cli('tree',str(p),timeout=.3)
  self.assertTrue(r.timed_out);self.assertEqual(r.returncode,124);self.assertLess(time.monotonic()-started,2)
  self.assert_dead(int(p.read_text()))
 def test_leader_exit_keeps_tree_owned(self):
  p=self.cwd/'pid';r=self.run_cli('leader-exit',str(p))
  self.assertEqual(r.returncode,0);self.assertFalse(r.timed_out);self.assert_dead(int(p.read_text()))
 @unittest.skipIf(os.name=='nt','Windows uses Job Object ownership instead of POSIX sessions')
 def test_detached_descendant_tracked_before_leader_exit(self):
  p=self.cwd/'pid';r=self.run_cli('detached',str(p))
  self.assertEqual(r.returncode,0);self.assert_dead(int(p.read_text()))
 def test_stalled_or_flooded_pipes(self):
  started=time.monotonic();r=self.run_cli('flood',timeout=2,max_bytes=8192)
  self.assertEqual(r.returncode,125);self.assertLessEqual(len(r.stdout),8192);self.assertLess(time.monotonic()-started,2)
  started=time.monotonic();r=self.run_cli('stdin-stall',timeout=.2,input_data=b'X'*2097152)
  self.assertTrue(r.timed_out);self.assertLess(time.monotonic()-started,2)
 def test_argv_roundtrip(self):
  values=['目录 spaces','a;b','$HOME','a"b','trailing\\','']
  r=self.run_cli('argv',*values);self.assertEqual(r.returncode,0,r.stderr.decode('utf-8',errors='replace'))
  self.assertEqual(json.loads(r.stdout),values)
 def test_continuous_pipe_keeps_line_bounds_without_a_lifetime_byte_cap(self):
  script="import sys\nfor n in range(2500):\n print('x'*2048,flush=True);sys.stdin.buffer.read(1)\n"
  p=self.process.spawn_owned([sys.executable,'-u','-c',script],cwd=self.cwd,environment=os.environ,
   stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
  self.addCleanup(p.close)
  with self.process.BoundedPipes(p,max_bytes=None,max_line_bytes=4096) as pipes:
   for _ in range(2500):
    self.assertEqual(pipes.readline(deadline=time.monotonic()+3),b'x'*2048)
    pipes.write(b'\n',deadline=time.monotonic()+3)
   self.assertEqual(p.wait(timeout=3),0)
 def test_continuous_pipe_rejects_oversized_unterminated_lines(self):
  script="import sys,time;sys.stdout.write('x'*8192);sys.stdout.flush();time.sleep(120)"
  p=self.process.spawn_owned([sys.executable,'-u','-c',script],cwd=self.cwd,environment=os.environ,
   stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
  self.addCleanup(p.close)
  with self.process.BoundedPipes(p,max_bytes=None,max_line_bytes=1024) as pipes:
   with self.assertRaisesRegex(ValueError,'pipe output limit exceeded'):
    pipes.readline(deadline=time.monotonic()+3)
 def test_finite_pipe_retains_its_total_byte_limit(self):
  p=self.process.spawn_owned([sys.executable,'-u','-c',"print('x'*8192)"],cwd=self.cwd,environment=os.environ,
   stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
  with self.process.BoundedPipes(p,max_bytes=1024) as pipes:
   with self.assertRaisesRegex(ValueError,'pipe output limit exceeded'):
    pipes.readline(deadline=time.monotonic()+3)
 def test_unrelated_process_survives(self):
  other=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
  self.addCleanup(lambda:other.kill() if other.poll() is None else None)
  try:
   p=self.cwd/'pid';self.run_cli('tree',str(p),timeout=.2)
   self.assertIsNone(other.poll())
  finally:other.kill();other.wait(timeout=2)
 def test_assign_failure_before_resume(self):
  self.assertIsNotNone(importlib.util.find_spec('quota_sentinel.platform.windows_process'),'Windows containment missing')
  from quota_sentinel.platform.windows_process import start_contained
  class API:
   def __init__(self):self.events=[]
   def create_job(self):self.events.append('job');return 'job'
   def create_suspended(self):self.events.append('suspended');return 'process','thread',42
   def assign(self,job,process):self.events.append('assign');raise OSError('nested job assignment rejected')
   def resume(self,thread):self.events.append('EXECUTED')
   def terminate(self,process):self.events.append('terminate')
   def close(self,handle):self.events.append('close '+handle)
  api=API()
  with self.assertRaises(OSError):start_contained(api)
  self.assertNotIn('EXECUTED',api.events);self.assertIn('terminate',api.events)
  self.assertEqual({x for x in api.events if x.startswith('close ')},{'close job','close process','close thread'})
 def test_nested_job_assignment_before_execution(self):
  from quota_sentinel.platform.windows_process import start_contained
  class API:
   def __init__(self):self.events=[]
   def create_job(self):return 'job'
   def create_suspended(self):self.events.append('suspended');return 'process','thread',42
   def assign(self,job,process):self.events.append('nested assignment')
   def resume(self,thread):self.events.append('execute')
   def terminate(self,process):raise AssertionError('successful process terminated during launch')
   def close(self,handle):self.events.append('close '+handle)
  api=API();self.assertEqual(start_contained(api),('job','process',42))
  self.assertLess(api.events.index('nested assignment'),api.events.index('execute'));self.assertIn('close thread',api.events)
 def test_launch_cleanup_continues_after_termination_error(self):
  from quota_sentinel.platform.windows_process import start_contained
  class API:
   def __init__(self):self.closed=[]
   def create_job(self):return 'job'
   def create_suspended(self):return 'process','thread',42
   def assign(self,*args):raise OSError('assignment failed')
   def resume(self,*args):raise AssertionError('child must stay suspended')
   def terminate(self,*args):raise OSError('termination failed')
   def close(self,handle):self.closed.append(handle)
  api=API()
  with self.assertRaisesRegex(OSError,'assignment failed'):start_contained(api)
  self.assertEqual(set(api.closed),{'process','thread','job'})

if __name__=='__main__':unittest.main()
