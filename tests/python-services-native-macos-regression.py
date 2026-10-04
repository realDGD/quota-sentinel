"""Temporary LaunchAgent lifecycle and owned child cleanup; no existing jobs touched."""
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
if sys.platform!='darwin':print('UNVERIFIED: requires native macOS');raise SystemExit(77)
from quota_sentinel.platform.services import ServiceDefinition,ServiceManager,service_environment

class NativeService(unittest.TestCase):
 def test_stop_cleans_execution_tree(self):
  with tempfile.TemporaryDirectory(prefix='qs-native-service-') as tmp:
   root=Path(tmp);ready=root/'ready'
   code='''import os,signal,time,subprocess
from pathlib import Path
from quota_sentinel.platform.process import spawn_owned
stop=False
def stopped(*args):
 global stop
 stop=True
signal.signal(signal.SIGTERM,stopped)
child=spawn_owned((__import__('sys').executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)'),cwd=os.getcwd(),environment=os.environ,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
Path('ready').write_text(str(os.getpid())+' '+str(child.pid))
try:
 while not stop:time.sleep(.05)
finally:child.stop(.2);child.close()
'''
   script=root/'synthetic host.py';script.write_text(code)
   manager=ServiceManager(home=root)
   definition=ServiceDefinition('quota-sentinel.synthetic-'+uuid.uuid4().hex,(sys.executable,str(script)),root,service_environment(os.environ),15)
   domain=manager._run(('/bin/launchctl','print','gui/'+str(os.getuid())),check=False)
   if domain.returncode:raise RuntimeError('native GUI launchd domain unavailable (UNVERIFIED)')
   try:
    manager.install(definition);self.assertFalse(ready.exists());manager.start(definition)
    deadline=time.monotonic()+15
    while not ready.exists() and time.monotonic()<deadline:time.sleep(.05)
    self.assertTrue(ready.exists(),'synthetic service did not start')
    pids=tuple(map(int,ready.read_text().split()));manager.stop(definition)
    for pid in pids:
     deadline=time.monotonic()+5
     while time.monotonic()<deadline:
      try:os.kill(pid,0)
      except ProcessLookupError:break
      time.sleep(.05)
     else:self.fail('owned process survived service stop')
   finally:manager.remove(definition)
   self.assertFalse(manager.path(definition).exists())
if __name__=='__main__':unittest.main()
