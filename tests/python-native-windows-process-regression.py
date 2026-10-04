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
 def test_cleanup_deadline_covers_job_process_snapshot(self):
  from ctypes import wintypes as w
  kernel=ctypes.WinDLL('kernel32',use_last_error=True)
  kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
  kernel.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];kernel.CloseHandle.argtypes=[w.HANDLE]
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);markers=[root/(str(i)+'.pid') for i in range(6)]
   leaf='import os,sys,time\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(60)\n'
   leader=('import subprocess,sys,time\nfrom pathlib import Path\n'
           'paths='+repr([str(path) for path in markers])+'\n'
           'for path in paths:subprocess.Popen([sys.executable,"-c",'+repr(leaf)+',path])\n'
           'time.sleep(60)\n')
   process=spawn_owned([sys.executable,'-c',leader],cwd=root,environment=os.environ,
                       stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   original=process._api.kernel.OpenProcess;handles=[]
   try:
    deadline=time.monotonic()+8
    while not all(path.exists() for path in markers) and time.monotonic()<deadline:time.sleep(.01)
    self.assertTrue(all(path.exists() for path in markers),'owned fixture descendants did not initialize')
    for path in markers:
     handle=kernel.OpenProcess(0x100000,False,int(path.read_text()));self.assertTrue(handle);handles.append(handle)
    def delayed_open(*args):
     time.sleep(.15);return original(*args)
    process._api.kernel.OpenProcess=delayed_open
    start=time.monotonic()
    with self.assertRaises(subprocess.TimeoutExpired):process.stop(0)
    self.assertLess(time.monotonic()-start,1.8,'snapshot work bypassed the shared cleanup deadline')
   finally:
    process._api.kernel.OpenProcess=original;process.close()
    for handle in handles:
     try:self.assertEqual(kernel.WaitForSingleObject(handle,2000),0,'cleanup timeout left an owned descendant alive')
     finally:kernel.CloseHandle(handle)
 def test_close_waits_for_descendant_exit_and_file_release(self):
  from ctypes import wintypes as w
  kernel=ctypes.WinDLL('kernel32',use_last_error=True)
  kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
  kernel.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];kernel.WaitForSingleObject.restype=w.DWORD
  kernel.CloseHandle.argtypes=[w.HANDLE]
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);ready=root/'leaf.pid';held=root/'held.bin'
   leaf=('import os,time\nfrom pathlib import Path\n'
         'file=open('+repr(str(held))+',"wb");file.write(b"synthetic");file.flush()\n'
         'Path('+repr(str(ready))+').write_text(str(os.getpid()))\ntime.sleep(60)\n')
   leader=('import subprocess,sys,time\nfrom pathlib import Path\n'
           'subprocess.Popen([sys.executable,"-c",'+repr(leaf)+'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
           'deadline=time.monotonic()+5\n'
           'while not Path('+repr(str(ready))+').exists() and time.monotonic()<deadline:time.sleep(.001)\n'
           'assert Path('+repr(str(ready))+').exists()\n')
   process=spawn_owned([sys.executable,'-c',leader],cwd=root,environment=os.environ,
                       stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   handle=None
   try:
    self.assertEqual(process.wait(8),0)
    handle=kernel.OpenProcess(0x100000,False,int(ready.read_text()));self.assertTrue(handle)
    self.assertEqual(kernel.WaitForSingleObject(handle,0),258,'fixture descendant must still own its file')
    start=time.monotonic();process.close()
    self.assertLess(time.monotonic()-start,2,'job close exceeded its cleanup budget')
    self.assertEqual(kernel.WaitForSingleObject(handle,0),0,'close returned before its descendant exited')
    held.unlink()
   finally:
    process.close()
    if handle:
     kernel.WaitForSingleObject(handle,2000);kernel.CloseHandle(handle)
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
  # Import native APIs and initialize the interpreter's Windows I/O before
  # measuring handles retained by repeated process lifecycle operations.
  warm=spawn_owned([sys.executable,'-c','pass'],cwd=Path.cwd(),environment=os.environ,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  try:warm.wait(3)
  finally:warm.close()
  before=count()
  for i in range(12):
   p=spawn_owned([sys.executable,'-c','import time;time.sleep(.03)'],cwd=Path.cwd(),environment=os.environ,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
   if i%2:p.stop(0)
   else:p.wait(3)
   p.close();p.close()
  self.assertLessEqual(count(),before+2)

if __name__=='__main__':unittest.main()
