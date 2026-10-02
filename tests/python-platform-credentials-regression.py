"""Selected credential references, bounded workers and secret-safe errors."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr,redirect_stdout
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.config import CredentialReference,new_user_defaults

class Credentials(unittest.TestCase):
 def setUp(self):
  self.assertIsNotNone(importlib.util.find_spec('quota_sentinel.platform.credentials'),'portable credential boundary missing')
  from quota_sentinel.platform import credentials
  self.credentials=credentials;self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
 def test_environment_reference_only(self):
  store=self.credentials.CredentialStore(environment={'SELECTED_KEY':'private-value','OTHER_KEY':'unused'})
  with patch.object(store,'_request',side_effect=AssertionError('environment reference must not consult a password service')):
   self.assertEqual(store.read(CredentialReference('environment','SELECTED_KEY'),timeout=1),'private-value')
   with self.assertRaises(self.credentials.CredentialUnavailable):store.read(CredentialReference('environment','MISSING'),timeout=1)
 def test_file_owner_permissions(self):
  from quota_sentinel.platform.files import publish_private
  p=self.root/'private key';publish_private(p,b'fixture-secret')
  store=self.credentials.CredentialStore(environment={})
  self.assertEqual(store.read(CredentialReference('file',str(p)),timeout=2),'fixture-secret')
  if os.name!='nt':
   p.chmod(0o644)
   with self.assertRaises(self.credentials.CredentialUnavailable):store.read(CredentialReference('file',str(p)),timeout=2)
  if os.name!='nt':
   p.unlink();p.symlink_to(self.root/'missing')
   with self.assertRaises(self.credentials.CredentialUnavailable):store.read(CredentialReference('file',str(p)),timeout=2)
 def test_explicit_file_roundtrip(self):
  p=self.root/'explicit-credentials';store=self.credentials.CredentialStore(environment={});ref=CredentialReference('file',str(p))
  store.write(ref,'synthetic-only',timeout=2);self.assertEqual(store.read(ref,timeout=2),'synthetic-only')
 def test_secret_never_in_logs_or_argv(self):
  from quota_sentinel.platform.process import CommandResult
  calls=[]
  def capture(command,**options):
   calls.append((command,options));return CommandResult(b'{"status":"unavailable"}',b'synthetic-secret',69,False)
  store=self.credentials.CredentialStore(environment={'FEISHU_APP_SECRET':'ambient-secret'},runner=capture,system='Windows')
  output=io.StringIO()
  with redirect_stdout(output),redirect_stderr(output):
   with self.assertRaises(self.credentials.CredentialUnavailable) as exc:store.write(CredentialReference('system','quota-sentinel.test'),'synthetic-secret',timeout=2)
  self.assertNotIn('synthetic-secret',str(exc.exception));self.assertEqual(output.getvalue(),'')
  command,options=calls[0];self.assertNotIn('synthetic-secret',' '.join(command));self.assertNotIn('ambient-secret',str(options['environment']))
  self.assertEqual(json.loads(options['input_data'])['value'],'synthetic-secret')
 def test_macos_service_names_unchanged(self):
  from quota_sentinel.platform.process import CommandResult
  calls=[]
  def capture(command,**kw):calls.append(json.loads(kw['input_data']));return CommandResult(b'{"status":"ok","value":"fixture"}',b'',0,False)
  store=self.credentials.CredentialStore(environment={},system='Darwin',runner=capture)
  self.assertEqual(store.read(CredentialReference('system','quota-sentinel.feishu-app-secret','quota-sentinel'),timeout=2),'fixture')
  self.assertEqual(calls[0]['reference'],{'kind':'system','locator':'quota-sentinel.feishu-app-secret','account':'quota-sentinel'})
 def test_no_insecure_backend_fallback(self):
  store=self.credentials.CredentialStore(environment={},system='Linux')
  with self.assertRaises(self.credentials.CredentialUnavailable):store.read(CredentialReference('system','quota-sentinel.test'),timeout=2)
  self.assertEqual(list(self.root.iterdir()),[])
 def test_linux_locked_backend_bounded(self):
  fake=self.root/'worker.py';pid=self.root/'child.pid'
  fake.write_text('import subprocess,sys,time\np=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\nopen('+repr(str(pid))+',"w").write(str(p.pid))\ntime.sleep(60)\n')
  store=self.credentials.CredentialStore(environment={},system='Linux',worker_command=(sys.executable,str(fake)))
  start=time.monotonic()
  with self.assertRaises(self.credentials.CredentialUnavailable):store.read(CredentialReference('system','secret-service:quota-sentinel.test'),timeout=.3)
  self.assertLess(time.monotonic()-start,2)
  if os.name!='nt':
   deadline=time.monotonic()+2
   while time.monotonic()<deadline:
    try:os.kill(int(pid.read_text()),0)
    except ProcessLookupError:break
    # Linux containers may leave a dead zombie awaiting init.
    info=Path('/proc')/pid.read_text()/'stat'
    if info.exists() and info.read_text().rsplit(')',1)[1].split()[0]=='Z':break
    time.sleep(.02)
   else:self.fail('credential worker left its child alive')
 def test_codex_only_no_store_calls(self):
  from quota_sentinel.runtime.selection import build_runtime_plan
  from quota_sentinel.runtime.selected_factory import create_selected_application
  from quota_sentinel.state.new_installation import initialize_new_installation
  config=new_user_defaults();state=self.root/'state';initialize_new_installation(state,config)
  with patch.object(self.credentials.CredentialStore,'read',side_effect=AssertionError('Codex-only must not use software credential stores')):
   app=create_selected_application(state,config,build_runtime_plan(config,'status'),environment={},clock=lambda:1700000000,sleep=lambda _:None)
   self.assertIsNotNone(app)
 def test_native_cli_auth_without_auth_json(self):
  from quota_sentinel.runtime.selection import build_runtime_plan
  from quota_sentinel.runtime.selected_factory import create_selected_application
  from quota_sentinel.state.new_installation import initialize_new_installation
  config=new_user_defaults();state=self.root/'state';initialize_new_installation(state,config)
  cli=self.root/'codex.py';cli.write_text('print("Logged in using system credential store")')
  cli.chmod(0o755)
  from dataclasses import replace
  config=replace(config,clients={'codex':str(cli),'codex_home':str(self.root/'no-auth-file')})
  app=create_selected_application(state,config,build_runtime_plan(config,'run'),environment={},clock=lambda:1700000000,sleep=lambda _:None)
  from quota_sentinel.platform.process import CommandResult
  with patch('quota_sentinel.runtime.quota_probe._run_bounded',return_value=CommandResult(b'Logged in',b'',0,False)):
   self.assertIsNotNone(app.model_runner.prepare('codex',self.root/'work'))
  self.assertFalse((self.root/'no-auth-file/auth.json').exists())

 def test_windows_roundtrip_and_missing_session(self):
  import ctypes as c
  from quota_sentinel.platform.credential_worker import windows_operation,windows_target
  from unittest.mock import MagicMock
  api=MagicMock();saved={};allocations=[];ref=CredentialReference('system','synthetic-service','synthetic-account')
  def write(pointer,flags):
   item=pointer._obj;saved[item.TargetName]=c.string_at(item.CredentialBlob,item.CredentialBlobSize);return True
  def read(target,kind,flags,pointer):
   if target not in saved:return False
   item_type=pointer._obj._type_;blob=(c.c_ubyte*len(saved[target])).from_buffer_copy(saved[target])
   item=item_type(CredentialBlobSize=len(blob),CredentialBlob=blob);allocations.extend((blob,item))
   c.cast(pointer,c.POINTER(c.POINTER(item_type)))[0]=c.pointer(item);return True
  api.CredWriteW.side_effect=write;api.CredReadW.side_effect=read
  windows_operation('write',ref,'synthetic-secret',api)
  self.assertEqual(windows_operation('read',ref,None,api),'synthetic-secret');api.CredFree.assert_called_once()
  api.CredReadW.return_value=False;api.CredReadW.side_effect=None
  with self.assertRaises(self.credentials.CredentialUnavailable):windows_operation('read',ref,None,api)
  self.assertNotEqual(windows_target(ref),windows_target(CredentialReference('system','synthetic-service','other-account')))

 def test_locked_secret_service_does_not_unlock_or_create(self):
  from quota_sentinel.platform.credential_worker import secret_service_operation
  from unittest.mock import MagicMock
  module=MagicMock();collection=module.get_collection_by_alias.return_value;collection.is_locked.return_value=True
  with self.assertRaises(self.credentials.CredentialUnavailable):secret_service_operation('read',CredentialReference('system','secret-service:fixture'),None,module)
  collection.unlock.assert_not_called();collection.create_item.assert_not_called();module.get_default_collection.assert_not_called();module.dbus_init.return_value.close.assert_called_once()

 def test_locked_kwallet_does_not_open_or_start_service(self):
  from quota_sentinel.platform.credential_worker import kwallet_operation
  from unittest.mock import MagicMock
  module=MagicMock();module.SessionBus.return_value.list_names.return_value=['org.kde.kwalletd6'];module.Interface.return_value.isOpen.return_value=False
  with self.assertRaises(self.credentials.CredentialUnavailable):kwallet_operation('read',CredentialReference('system','kwallet:fixture'),None,module)
  module.Interface.return_value.open.assert_not_called();module.SessionBus.return_value.close.assert_called_once()

 def test_selected_references_reach_direct_query_and_notifier(self):
  from dataclasses import replace
  from quota_sentinel.config import FeatureSettings,ProviderSettings
  from quota_sentinel.runtime.selection import build_runtime_plan
  from quota_sentinel.runtime.selected_factory import create_selected_application
  from quota_sentinel.state.new_installation import initialize_new_installation
  c=new_user_defaults();providers={p:replace(v,enabled=False,opening_enabled=False) for p,v in c.providers.items()}
  providers['opencode']=ProviderSettings(True,True,('direct',),('native',))
  refs={k:CredentialReference('environment',k.upper()) for k in ('opencode','clinepass','feishu_app_id','feishu_app_secret','feishu_user_id')}
  c=replace(c,providers=providers,features=FeatureSettings(True,True,True,False),credentials=refs)
  state=self.root/'selected';initialize_new_installation(state,c)
  calls=[]
  def selected_read(store,reference,**kw):calls.append(reference.locator);return 'synthetic-only'
  with patch.object(self.credentials.CredentialStore,'read',selected_read):
   app=create_selected_application(state,c,build_runtime_plan(c,'run'),environment={},clock=lambda:1700000000,sleep=lambda _:None)
   self.assertEqual(calls,[])
   self.assertEqual(app.model_runner._runner('direct')._key_reader('opencode'),'synthetic-only')
   collector=app.quota_collector_factory(self.root/'work')
   self.assertEqual(collector._api_key('quota-sentinel.opencode-go-api-key','OPENCODE_API_KEY'),'synthetic-only')
   app.notifier.validate_ready()
  self.assertEqual(calls,['OPENCODE','OPENCODE','FEISHU_APP_ID','FEISHU_APP_SECRET','FEISHU_USER_ID'])
  self.assertNotIn('CLINEPASS',calls)

if __name__=='__main__':unittest.main()
