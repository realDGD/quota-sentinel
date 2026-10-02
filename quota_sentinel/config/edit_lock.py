"""Serialize compare-and-save across threads and legacy macOS processes."""
import os,subprocess,time,threading
from contextlib import contextmanager
from pathlib import Path
from .types import ConfigurationError
_guard=threading.Lock()
_locks={}
@contextmanager
def configuration_lock(path,*,timeout=15):
 path=Path(path); path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 with _guard: lock=_locks.setdefault(str(path.absolute()),threading.Lock())
 if not lock.acquire(timeout=timeout): raise ConfigurationError('configuration editor is busy')
 held=False
 try:
  deadline=time.monotonic()+timeout
  while True:
   r=subprocess.run(['/usr/bin/shlock','-p',str(os.getpid()),'-f',str(path)],capture_output=True,timeout=max(.1,min(5,deadline-time.monotonic())))
   if r.returncode==0: held=True; break
   if time.monotonic()>=deadline: raise ConfigurationError('configuration editor is busy')
   time.sleep(.05)
  yield
 finally:
  if held:
   try:
    if path.read_text().strip()==str(os.getpid()):path.unlink()
   except FileNotFoundError:pass
  lock.release()
