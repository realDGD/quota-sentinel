"""Explicit temporary user-service lifecycle; never acts on existing app units."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
if sys.platform!='linux' or os.environ.get('QUOTA_SENTINEL_TEST_LINUX_SERVICES')!='1':
 print('UNVERIFIED: requires Linux and explicit synthetic user-manager testing');raise SystemExit(77)
import pwd
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.platform.services import ServiceDefinition,ServiceManager,ServiceError,service_environment
from quota_sentinel.platform.process import run_bounded
home=Path(pwd.getpwuid(os.getuid()).pw_dir)
environment=service_environment(os.environ);environment['HOME']=str(home);environment['XDG_CONFIG_HOME']=str(home/'.config')
manager=ServiceManager(home=home,environment=environment)
try:manager._linux_available()
except ServiceError:print('UNVERIFIED: systemd user manager unavailable');raise SystemExit(77)

class Native(unittest.TestCase):
 def test_working_directory_is_literal_in_native_systemd(self):
  with tempfile.TemporaryDirectory(prefix='qs-native-unit-parser-') as tmp:
   directory=Path(tmp)/'space 中文 & % $ " \\ trailing ';directory.mkdir(mode=0o700)
   definition=ServiceDefinition('quota-sentinel.synthetic-'+uuid.uuid4().hex,(sys.executable,'-c','pass'),directory,{},15)
   unit=Path(tmp)/(definition.name+'.service');unit.write_bytes(manager.render(definition))
   result=run_bounded(('systemd-analyze','--user','verify',str(unit)),cwd=directory,environment=environment,timeout=15,kill_grace=0,max_bytes=65536)
   self.assertFalse(result.timed_out)
   self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
 def test_service_lifecycle_and_child_cleanup(self):
  with tempfile.TemporaryDirectory(prefix='qs-native-user-service-') as tmp:
   root=Path(tmp)/'space 中文 & % $ " \\ trailing ';root.mkdir(mode=0o700)
   ready=root/'ready';script=root/'host.py'
   script.write_text('''import os,signal,time,subprocess,sys
from pathlib import Path
from quota_sentinel.platform.process import spawn_owned
stop=False
def stopped(*args):
 global stop
 stop=True
signal.signal(signal.SIGTERM,stopped)
child=spawn_owned((sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)'),cwd=os.getcwd(),environment=os.environ,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
Path('ready').write_text(str(os.getpid())+' '+str(child.pid))
try:
 while not stop:time.sleep(.05)
finally:child.stop(.2);child.close()
''')
   definition=ServiceDefinition('quota-sentinel.synthetic-'+uuid.uuid4().hex,(sys.executable,str(script)),root,environment,15)
   try:
    manager.install(definition);self.assertFalse(ready.exists());manager.start(definition)
    deadline=time.monotonic()+15
    while not ready.exists() and time.monotonic()<deadline:time.sleep(.05)
    self.assertTrue(ready.exists());pids=tuple(map(int,ready.read_text().split()));manager.stop(definition)
    for pid in pids:
     deadline=time.monotonic()+5
     while time.monotonic()<deadline:
      try:os.kill(pid,0)
      except ProcessLookupError:break
      time.sleep(.05)
     else:self.fail('process survived user-service stop')
   finally:manager.remove(definition)
   self.assertFalse(manager.path(definition).exists())
if __name__=='__main__':unittest.main()
