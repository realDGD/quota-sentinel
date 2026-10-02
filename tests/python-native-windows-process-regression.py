"""Actual Windows Job Object lifecycle gate; never counted on another OS."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
if os.name!='nt':
 print('UNVERIFIED: this required gate needs native Windows');sys.exit(77)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.platform.process import spawn_owned
FIXTURE=Path(__file__).parent/'fixtures/process-tree.py'

class NativeWindows(unittest.TestCase):
 def test_parent_exit_cleans_job(self):
  with tempfile.TemporaryDirectory() as tmp:
   pid=Path(tmp)/'leaf.pid'
   code='''import os,sys,time
from pathlib import Path
from quota_sentinel.platform.process import spawn_owned
p=spawn_owned([sys.executable,sys.argv[1],'tree',sys.argv[2]],cwd=Path.cwd(),environment=os.environ)
deadline=time.monotonic()+3
while not Path(sys.argv[2]).exists() and time.monotonic()<deadline:time.sleep(.01)
print(p.pid,flush=True)
os._exit(0)
'''
   r=subprocess.run([sys.executable,'-c',code,str(FIXTURE),str(pid)],capture_output=True,timeout=5,env=dict(os.environ,PYTHONPATH=str(FIXTURE.parents[2])))
   self.assertEqual(r.returncode,0);leader=int(r.stdout);leaf=int(pid.read_text())
   from ctypes import wintypes as w
   kernel=ctypes.WinDLL('kernel32',use_last_error=True)
   kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
   kernel.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];kernel.CloseHandle.argtypes=[w.HANDLE]
   for process in (leader,leaf):
    handle=kernel.OpenProcess(0x100000,False,process)
    if handle:
     try:self.assertEqual(kernel.WaitForSingleObject(handle,2000),0,'owned process survived parent death')
     finally:kernel.CloseHandle(handle)
 def test_native_nested_job(self):
  # Hosted Windows runners are commonly already in a Job Object. There is
  # no CREATE_BREAKAWAY_FROM_JOB flag; native assignment must succeed safely.
  p=spawn_owned([sys.executable,'-c','print("nested")'],cwd=Path.cwd(),environment=os.environ,stdout=subprocess.PIPE)
  try:self.assertEqual(p.wait(3),0);self.assertEqual(p.stdout.read().strip(),b'nested')
  finally:p.close()
 def test_handle_release_after_success_and_timeout(self):
  from ctypes import wintypes as w
  kernel=ctypes.WinDLL('kernel32',use_last_error=True)
  kernel.GetCurrentProcess.restype=w.HANDLE
  kernel.GetProcessHandleCount.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD)]
  def count():
   n=w.DWORD();self.assertTrue(kernel.GetProcessHandleCount(kernel.GetCurrentProcess(),ctypes.byref(n)));return n.value
  before=count()
  for i in range(12):
   p=spawn_owned([sys.executable,'-c','import time;time.sleep(.03)'],cwd=Path.cwd(),environment=os.environ,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
   if i%2:p.stop(0)
   else:p.wait(3)
   p.close();p.close()
  self.assertLessEqual(count(),before+2)

if __name__=='__main__':unittest.main()
