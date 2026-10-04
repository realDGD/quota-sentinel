"""Synthetic same-user Task Scheduler lifecycle; no supplier clients or credentials."""
from dataclasses import replace
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
if os.name!='nt':print('UNVERIFIED: requires native Windows');raise SystemExit(77)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import new_user_defaults
from quota_sentinel.state.new_installation import initialize_new_installation
from quota_sentinel.install import service_definition
from quota_sentinel.platform.services import ServiceManager
from quota_sentinel.platform.paths import resolve_launcher
from quota_sentinel.platform.process import run_bounded

@contextmanager
def synthetic_task_directory():
 temporary=tempfile.mkdtemp(prefix='qs-native-task-')
 try:yield temporary
 finally:
  # Task Scheduler has stopped its instance, but native handle disposal may
  # briefly lag the successful /End response. Keep cleanup finite and loud.
  deadline=time.monotonic()+2
  # Retry the filesystem operation itself: Python3.9 cleanup() detaches its
  # finalizer on the first attempt and can silently skip subsequent retries.
  def missing_child(function,path,error):
   # SQLite may remove its own WAL/SHM while the stopped host exits.
   if not isinstance(error[1],FileNotFoundError):raise error[1]
  while Path(temporary).exists():
   try:shutil.rmtree(temporary,onerror=missing_child);break
   except PermissionError as exc:
    if exc.winerror not in (5,32) or time.monotonic()>=deadline:raise
    time.sleep(min(.02,max(0,deadline-time.monotonic())))

class Native(unittest.TestCase):
 def test_cleanup_retries_sharing_without_leaving_directory(self):
  # Python3.9 detaches TemporaryDirectory's finalizer before rmtree succeeds.
  # Repeating cleanup() can then return while the owned directory still exists.
  remove=shutil.rmtree;first=True;owned=None
  def transient(path,*args,**kwargs):
   nonlocal first
   if first:
    first=False;error=PermissionError('synthetic sharing violation');error.winerror=32;raise error
   return remove(path,*args,**kwargs)
  try:
   with patch('shutil.rmtree',side_effect=transient):
    with synthetic_task_directory() as owned:
     (Path(owned)/'owned-fixture.txt').write_text('synthetic-only')
   self.assertFalse(Path(owned).exists(),'cleanup returned with its owned directory still present')
  finally:
   if owned and Path(owned).exists():remove(owned)
 def test_cleanup_reports_persistent_sharing_within_its_budget(self):
  remove=shutil.rmtree;owned=None
  def unavailable(path,*args,**kwargs):
   error=PermissionError('synthetic sharing violation');error.winerror=32;raise error
  start=time.monotonic()
  try:
   with patch('shutil.rmtree',side_effect=unavailable):
    with self.assertRaises(PermissionError):
     with synthetic_task_directory() as owned:
      (Path(owned)/'owned-fixture.txt').write_text('synthetic-only')
   self.assertLess(time.monotonic()-start,3,'cleanup exceeded its finite budget')
  finally:
   if owned and Path(owned).exists():remove(owned)
 def test_cleanup_tolerates_an_owned_child_disappearing(self):
  remove=shutil.rmtree;unlink=os.unlink;owned=None
  def disappeared(path,*args,**kwargs):
   unlink(path,*args,**kwargs)
   if Path(path).name=='disappearing.txt':
    raise FileNotFoundError(2,'synthetic concurrent removal',str(path))
  try:
   try:
    with patch('os.unlink',side_effect=disappeared):
     with synthetic_task_directory() as owned:
      (Path(owned)/'disappearing.txt').write_text('synthetic-only')
   except FileNotFoundError:self.fail('an already removed child must not prevent owned directory cleanup')
   self.assertFalse(Path(owned).exists())
  finally:
   if owned and Path(owned).exists():remove(owned)
 def running_instance(self,name):
  # Explicit Windows PowerShell avoids whichever pwsh alias the user selects.
  # This read addresses only the unique synthetic task created below.
  powershell=Path(os.environ['SYSTEMROOT'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
  script=("$ErrorActionPreference='Stop'; $ownedService=New-Object -ComObject Schedule.Service; "
          "$ownedService.Connect(); $ownedTask=$ownedService.GetFolder('\\').GetTask('"+name+"'); "
          "$ownedInstances=$ownedTask.GetInstances(0); "
          "if ($ownedInstances.Count -gt 1) {throw 'Multiple synthetic task instances'}; "
          "if ($ownedInstances.Count -eq 1) {[Console]::Write($ownedInstances.Item(1).InstanceGuid)}")
  environment={key:os.environ[key] for key in ('SYSTEMROOT','WINDIR','PATH','TEMP','TMP','COMSPEC') if key in os.environ}
  result=run_bounded((str(powershell),'-NoLogo','-NoProfile','-NonInteractive','-Command',script),cwd=Path.cwd(),environment=environment,timeout=10,kill_grace=0,max_bytes=4096)
  self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'));self.assertFalse(result.timed_out)
  return result.stdout.decode('ascii').strip()
 def wait_instance(self,name,*,previous=None,stopped=False):
  deadline=time.monotonic()+20
  while True:
   current=self.running_instance(name)
   if (stopped and not current) or (not stopped and current and current!=previous):return current
   if time.monotonic()>=deadline:self.fail('Synthetic task instance did not reach the requested lifecycle state')
   time.sleep(.05)
 def test_task_lifecycle_without_supplier_work(self):
  with synthetic_task_directory() as tmp:
   root=Path(tmp);state=root/'state';c=new_user_defaults()
   c=replace(c,providers={p:replace(v,enabled=False) for p,v in c.providers.items()})
   initialize_new_installation(state,c)
   definition=service_definition(c,state,state/'config.json',name='quota-sentinel.synthetic-'+uuid.uuid4().hex)
   def native_command(argv,**options):
    # These commands address only this synthetic task. Preserve native error
    # diagnostics so registration/session failures can be distinguished.
    options['discard_stdout']=False
    result=run_bounded((*resolve_launcher(argv[0]),*argv[1:]),**options)
    if result.returncode or result.timed_out:
     print('synthetic Task Scheduler command:',argv[1], 'exit:',result.returncode,'timeout:',result.timed_out)
     print((result.stdout+result.stderr).decode('oem',errors='replace'))
    return result
   manager=ServiceManager(home=root,environment=dict(os.environ,LOCALAPPDATA=str(root/'AppData/Local')),runner=native_command)
   installed=False
   try:
    manager.install(definition);installed=True;self.assertFalse((state/'task-orchestrator.sqlite3').exists())
    manager.start(definition);deadline=time.monotonic()+20
    while not (state/'task-orchestrator.sqlite3').exists() and time.monotonic()<deadline:time.sleep(.1)
    self.assertTrue((state/'task-orchestrator.sqlite3').exists(),'registered task did not execute the selected host')
    initial=self.wait_instance(definition.name)
    manager.start(definition)
    replacement=self.wait_instance(definition.name,previous=initial)
    self.assertNotEqual(replacement,initial,'start must replace the active host rather than retain its old profile')
    manager.stop(definition)
    self.wait_instance(definition.name,stopped=True)
   finally:
    if installed:manager.remove(definition)
   self.assertTrue((state/'backend-authority.json').exists());self.assertFalse(manager.path(definition).exists())
  self.assertFalse(Path(tmp).exists(),'native task cleanup left its owned directory behind')
if __name__=='__main__':unittest.main()
