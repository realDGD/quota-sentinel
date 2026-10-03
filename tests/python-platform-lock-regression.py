"""Actual process exclusion, crash recovery and native protocol separation."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from private_file_fixtures import private_test_directory
ROOT=Path(__file__).resolve().parents[1]
CHILD='''import json,os,sys,time
from pathlib import Path
from quota_sentinel.platform.locks import acquire_lock
p=Path(sys.argv[1]);protocol=sys.argv[2];mode=sys.argv[3]
with acquire_lock(p,timeout=3,protocol=protocol):
 print('entered',flush=True)
 if mode=='crash':os._exit(0)
 if mode=='record':
  with (p.parent/'intervals').open('a') as f:f.write(json.dumps([os.getpid(),'start',time.monotonic()])+'\\n')
  time.sleep(.1)
  with (p.parent/'intervals').open('a') as f:f.write(json.dumps([os.getpid(),'end',time.monotonic()])+'\\n')
 else:time.sleep(.4)
'''

class Locks(unittest.TestCase):
 def setUp(self):
  self.assertIsNotNone(importlib.util.find_spec('quota_sentinel.platform.locks'),'portable lock protocol missing')
  from quota_sentinel.platform import locks
  self.locks=locks;self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.directory=private_test_directory(self.tmp)
  self.protocol=locks.expected_protocol()
 def child(self,mode):
  p=subprocess.Popen([sys.executable,'-c',CHILD,str(self.directory/'run.lock'),self.protocol,mode],cwd=str(ROOT),env=dict(os.environ,PYTHONPATH=str(ROOT)),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  def cleanup():
   if p.poll() is None:p.kill()
   p.communicate(timeout=2)
  self.addCleanup(cleanup);return p
 def test_two_process_mutual_exclusion(self):
  first=self.child('record');self.assertEqual(first.stdout.readline().strip(),b'entered')
  second=self.child('record')
  self.assertEqual(first.communicate(timeout=5)[1],b'');self.assertEqual(first.returncode,0)
  self.assertEqual(second.communicate(timeout=5)[1],b'');self.assertEqual(second.returncode,0)
  events=[json.loads(x)[1] for x in (self.directory/'intervals').read_text().splitlines()]
  self.assertEqual(events,['start','end','start','end'])
 def test_crashed_holder_releases(self):
  p=self.child('crash');p.communicate(timeout=3);self.assertEqual(p.returncode,0)
  with self.locks.acquire_lock(self.directory/'run.lock',timeout=2,protocol=self.protocol):pass
 def test_busy_deadline(self):
  p=self.child('sleep');self.assertEqual(p.stdout.readline().strip(),b'entered')
  start=time.monotonic()
  with self.assertRaises(self.locks.LockBusy):self.locks.acquire_lock(self.directory/'run.lock',timeout=.05,protocol=self.protocol)
  self.assertLess(time.monotonic()-start,.4)
 @unittest.skipIf(os.name=='nt','POSIX descriptor implementation')
 def test_fd_lock_file_is_not_unlinked(self):
  p=self.directory/'fd.lock'
  with self.locks.acquire_lock(p,timeout=0,protocol='posix-fd-v1'):
   identity=(p.stat().st_dev,p.stat().st_ino)
   with self.assertRaises(self.locks.LockBusy):self.locks.acquire_lock(p,timeout=.01,protocol='posix-fd-v1')
  self.assertTrue(p.is_file())
  with self.locks.acquire_lock(p,timeout=0,protocol='posix-fd-v1'):
   self.assertEqual((p.stat().st_dev,p.stat().st_ino),identity)
 @unittest.skipIf(os.name=='nt','POSIX descriptor implementation')
 def test_fd_crash_releases_kernel_lock(self):
  p=subprocess.Popen([sys.executable,'-c',CHILD,str(self.directory/'fd.lock'),'posix-fd-v1','crash'],cwd=str(ROOT),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  out,err=p.communicate(timeout=3);self.assertEqual(p.returncode,0);self.assertEqual(err,b'');self.assertIn(b'entered',out)
  with self.locks.acquire_lock(self.directory/'fd.lock',timeout=.1,protocol='posix-fd-v1'):pass
 def test_protocol_mismatch_refused_before_authority(self):
  from quota_sentinel.state import read_authority,AuthorityError
  wrong='windows-range-v1' if self.protocol!='windows-range-v1' else 'posix-fd-v1'
  (self.directory/'lock-protocol.json').write_text(json.dumps({'schema_version':1,'protocol':wrong}))
  (self.directory/'backend-authority.json').write_text('invalid JSON intentionally')
  with self.assertRaisesRegex(AuthorityError,'lock protocol'):read_authority(self.directory)
 def test_missing_nonmac_protocol_not_guessed(self):
  for system in ('Linux','Windows'):
   with self.assertRaises(self.locks.LockProtocolError):self.locks.state_protocol(self.directory,system=system)
  self.assertEqual(self.locks.state_protocol(self.directory,system='Darwin'),'macos-shlock-v1')
 def test_fresh_install_records_protocol(self):
  from quota_sentinel.state.new_installation import initialize_new_installation
  from quota_sentinel.config import new_user_defaults
  d=self.directory/'new';initialize_new_installation(d,new_user_defaults())
  self.assertEqual(json.loads((d/'lock-protocol.json').read_bytes()),{'schema_version':1,'protocol':self.protocol})
 def test_stale_config_editor_serialized(self):
  from quota_sentinel.config import new_user_defaults,save_config,read_config
  p=self.directory/'config.json';revision=save_config(p,new_user_defaults(),expected_revision=None)
  code='''import sys
from pathlib import Path
from dataclasses import replace
from quota_sentinel.config import new_user_defaults,save_config,ConfigurationError
try:save_config(Path(sys.argv[1]),replace(new_user_defaults(),origin=sys.argv[3]),expected_revision=sys.argv[2]);print('saved')
except ConfigurationError:print('stale')
'''
  ps=[subprocess.Popen([sys.executable,'-c',code,str(p),revision,str(i)],cwd=str(ROOT),stdout=subprocess.PIPE,stderr=subprocess.PIPE) for i in range(2)]
  outputs=[]
  for proc in ps:
   out,err=proc.communicate(timeout=5);self.assertEqual(proc.returncode,0);self.assertEqual(err,b'');outputs.append(out.strip())
  self.assertCountEqual(outputs,[b'saved',b'stale']);self.assertIn(read_config(p).settings.origin,('0','1'))
 @unittest.skipUnless(sys.platform=='darwin','existing macOS shlock protocol')
 def test_dead_pid_file_macos(self):
  dead=subprocess.Popen([sys.executable,'-c','pass']);dead.wait(timeout=2)
  path=self.directory/'run.lock';path.write_text(str(dead.pid)+'\n')
  with self.locks.acquire_lock(path,timeout=2,protocol=self.protocol):self.assertEqual(path.read_text().strip(),str(os.getpid()))
 @unittest.skipUnless(sys.platform=='darwin','PID-file release only')
 def test_release_does_not_delete_new_owner(self):
  path=self.directory/'run.lock';held=self.locks.acquire_lock(path,timeout=0,protocol=self.protocol)
  path.unlink();path.write_text('987654321\n');held.release();self.assertEqual(path.read_text().strip(),'987654321')

if __name__=='__main__':unittest.main()
