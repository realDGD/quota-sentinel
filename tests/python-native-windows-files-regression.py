"""Native restricted directory inheritance, including SQLite WAL sidecars."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
if os.name!='nt':
 print('UNVERIFIED: native Windows file permissions require Windows');raise SystemExit(77)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.platform.files import private_directory, private_open
from quota_sentinel.platform.windows_files import verify_private_handle
from private_file_fixtures import assert_private_path,assert_inherited_private_child

class NativeFiles(unittest.TestCase):
 def test_exclusive_stream_failure_removes_created_file(self):
  for close_first in (False,True):
   with self.subTest(close_first=close_first),tempfile.TemporaryDirectory() as tmp:
    parent=private_directory(Path(tmp)/'owned');path=parent/'new'
    def failed_stream(raw,*args,**kwargs):
     fd=raw.fileno()
     if close_first:raw.close()
     os.fstat(fd)
     raise OSError('synthetic stream failure')
    with patch('io.BufferedWriter',side_effect=failed_stream):
     with self.assertRaisesRegex(OSError,'synthetic stream failure'):private_open(path,'xb')
    self.assertEqual(list(parent.iterdir()),[])
 def test_success_transfers_and_releases_descriptor_once(self):
  import ctypes as c
  from ctypes import wintypes as w
  kernel=c.WinDLL('kernel32',use_last_error=True)
  kernel.GetCurrentProcess.restype=w.HANDLE
  kernel.GetProcessHandleCount.argtypes=[w.HANDLE,c.POINTER(w.DWORD)]
  kernel.GetProcessHandleCount.restype=w.BOOL
  def handle_count():
   count=w.DWORD()
   if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(),c.byref(count)):raise c.WinError(c.get_last_error())
   return count.value
  with tempfile.TemporaryDirectory() as tmp:
   parent=private_directory(Path(tmp)/'owned')
   with private_open(parent/'warm','xb') as stream:stream.write(b'warm')
   for mode in ('xb','wb','ab','rb','r+b'):
    with self.subTest(mode=mode):
     path=parent/('new-'+mode)
     if mode in ('rb','r+b'):
      with private_open(path,'xb') as stream:stream.write(b'synthetic')
     baseline=handle_count()
     with private_open(path,mode) as stream:
      fd=stream.fileno()
      self.assertEqual(handle_count(),baseline+1)
      if mode=='rb':self.assertEqual(stream.read(),b'synthetic')
      else:stream.write(b'synthetic')
     self.assertEqual(handle_count(),baseline)
     replacement=os.open(path,os.O_RDONLY)
     try:
      self.assertEqual(replacement,fd);stream.close();stream.raw.close()
      self.assertEqual(os.read(replacement,32),b'synthetic')
     finally:os.close(replacement)
     self.assertEqual(path.read_bytes(),b'synthetic')
 def test_stream_constructor_failure_preserves_other_readers(self):
  cases=[(mode,False) for mode in ('xb','wb','ab','rb','r+b')]+[(mode,True) for mode in ('rb','r+b')]
  for mode,same_file in cases:
   with self.subTest(mode=mode,same_file=same_file),tempfile.TemporaryDirectory() as tmp:
    parent=private_directory(Path(tmp)/'owned');path=parent/'new';other=parent/'other';other.write_bytes(b'other caller');foreign=[]
    if mode in ('rb','r+b'):
     with private_open(path,'xb') as stream:stream.write(b'prior')
    def failed_stream(raw,*args,**kwargs):
     fd=raw.fileno();raw.close();os.fstat(fd)
     replacement=os.open(path if same_file else other,os.O_RDONLY);foreign.append(replacement)
     self.assertNotEqual(replacement,fd);raise OSError('synthetic stream ownership failure')
    buffer='BufferedReader' if mode=='rb' else 'BufferedRandom' if mode=='r+b' else 'BufferedWriter'
    try:
     with patch('io.'+buffer,side_effect=failed_stream):
      with self.assertRaisesRegex(OSError,'synthetic stream ownership failure'):private_open(path,mode)
     self.assertEqual(os.read(foreign[0],32),b'prior' if same_file else b'other caller')
     if mode=='xb':self.assertFalse(path.exists())
     else:self.assertTrue(path.exists())
    finally:
     for fd in foreign:
      try:os.close(fd)
      except OSError:pass
 def test_new_installation_and_migration_use_explicit_private_owners(self):
  from quota_sentinel.config import new_user_defaults,ConfigurationError
  from quota_sentinel.state.new_installation import initialize_new_installation
  from quota_sentinel.state import migrate_provider,JsonStateStore
  with tempfile.TemporaryDirectory() as tmp:
   state=Path(tmp)/'new';initialize_new_installation(state,new_user_defaults())
   assert_private_path(self,state,directory=True)
   for entry in state.iterdir():assert_private_path(self,entry)
   with self.assertRaises(ConfigurationError):initialize_new_installation(state,new_user_defaults())
   legacy=private_directory(Path(tmp)/'legacy');(legacy/'codex-next-due-at').write_bytes(b'77\n')
   migrate_provider(legacy,'codex')
   assert_private_path(self,legacy/'codex-state.json')
   self.assertEqual(JsonStateStore(legacy).load('codex').next_due_at,77)
 def test_explicit_acl_is_not_accepted_as_parent_inheritance(self):
  with tempfile.TemporaryDirectory() as tmp:
   parent=private_directory(Path(tmp)/'owned');child=parent/'explicit'
   with private_open(child,'xb') as handle:handle.write(b'synthetic')
   assert_private_path(self,child)
   with self.assertRaises(AssertionError):assert_inherited_private_child(self,child,parent)
 def test_private_directory_children_and_database(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=private_directory(Path(tmp)/'历史 space &')
   assert_private_path(self,path,directory=True)
   child=path/'ordinary';child.write_bytes(b'synthetic')
   assert_inherited_private_child(self,child,path)
   db=path/'history.sqlite3'
   with private_open(db,'ab'):pass
   connection=sqlite3.connect(db)
   try:
    connection.execute('PRAGMA journal_mode=WAL');connection.execute('CREATE TABLE example(value)');connection.commit()
    for entry in (db,Path(str(db)+'-wal'),Path(str(db)+'-shm')):
     self.assertTrue(entry.exists())
     if entry==db:assert_private_path(self,entry)
     else:assert_inherited_private_child(self,entry,path)
    with db.open('rb') as handle:verify_private_handle(handle.fileno())
   finally:connection.close()
if __name__=='__main__':unittest.main()
