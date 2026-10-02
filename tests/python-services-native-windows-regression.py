"""Synthetic same-user Task Scheduler lifecycle; no supplier clients or credentials."""
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
if os.name!='nt':print('UNVERIFIED: requires native Windows');raise SystemExit(77)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import new_user_defaults
from quota_sentinel.state.new_installation import initialize_new_installation
from quota_sentinel.install import service_definition
from quota_sentinel.platform.services import ServiceManager

class Native(unittest.TestCase):
 def test_task_lifecycle_without_supplier_work(self):
  with tempfile.TemporaryDirectory(prefix='qs-native-task-') as tmp:
   root=Path(tmp);state=root/'state';c=new_user_defaults()
   c=replace(c,providers={p:replace(v,enabled=False) for p,v in c.providers.items()})
   initialize_new_installation(state,c)
   definition=service_definition(c,state,state/'config.json',name='quota-sentinel.synthetic-'+uuid.uuid4().hex)
   manager=ServiceManager(home=root,environment=dict(os.environ,LOCALAPPDATA=str(root/'AppData/Local')))
   try:
    manager.install(definition);self.assertFalse((state/'task-orchestrator.sqlite3').exists())
    manager.start(definition);deadline=time.monotonic()+20
    while not (state/'task-orchestrator.sqlite3').exists() and time.monotonic()<deadline:time.sleep(.1)
    self.assertTrue((state/'task-orchestrator.sqlite3').exists(),'registered task did not execute the selected host')
    manager.stop(definition)
   finally:manager.remove(definition)
   self.assertTrue((state/'backend-authority.json').exists());self.assertFalse(manager.path(definition).exists())
if __name__=='__main__':unittest.main()
