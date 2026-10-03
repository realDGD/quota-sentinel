"""Private publication never exposes partial bytes or weak permissions."""
import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from private_file_fixtures import assert_private_path

class Files(unittest.TestCase):
 def setUp(self):
  self.assertIsNotNone(importlib.util.find_spec('quota_sentinel.platform'),'platform boundary missing')
  from quota_sentinel.platform import files
  self.files=files
 def test_private_permissions(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'private'
   with self.files.private_open(p,'wb') as f:f.write(b'secret')
   self.assertEqual(p.read_bytes(),b'secret')
   assert_private_path(self,p)
   self.files.publish_private(p,b'next')
   assert_private_path(self,p)
 def test_private_directory(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/"owned"/"history"
   self.files.private_directory(p)
   self.assertTrue(p.is_dir())
   assert_private_path(self,p,directory=True)
   self.files.private_directory(p)
 def test_exclusive_directory_creation_preserves_existing_directory(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'new';self.files.private_directory(p,exclusive=True)
   marker=p/'marker';marker.write_bytes(b'prior')
   with self.assertRaises(FileExistsError):self.files.private_directory(p,exclusive=True)
   self.assertEqual(marker.read_bytes(),b'prior')
 @unittest.skipIf(os.name=="nt","POSIX symlink fixture")
 def test_directory_symlink_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   target=Path(tmp)/"target";target.mkdir();link=Path(tmp)/"link";link.symlink_to(target)
   with self.assertRaises(OSError):self.files.private_directory(link)
 def test_failed_publish_keeps_prior_state(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'state';p.write_bytes(b'prior')
   with patch('os.replace',side_effect=OSError('synthetic failure')):
    with self.assertRaises(OSError):self.files.publish_private(p,b'new')
   self.assertEqual(p.read_bytes(),b'prior');self.assertEqual(list(p.parent.iterdir()),[p])
 @unittest.skipIf(os.name=='nt','native post-create failures are covered by the Windows file gates')
 def test_exclusive_open_cleans_created_file_after_post_create_failure(self):
  real_open=os.open
  for operation in ('fstat','fchmod','stream'):
   with self.subTest(operation=operation),tempfile.TemporaryDirectory() as tmp:
    p=Path(tmp)/'new';opened=[]
    def recorded_open(*args,**kwargs):
     fd=real_open(*args,**kwargs);opened.append(fd);return fd
    target='quota_sentinel.platform.files.io.BufferedWriter' if operation=='stream' else 'os.'+operation
    with patch('os.open',side_effect=recorded_open),patch(target,side_effect=OSError('synthetic '+operation+' failure')),patch('os.fdopen',side_effect=OSError('synthetic '+operation+' failure')):
     with self.assertRaisesRegex(OSError,'synthetic '+operation):self.files.private_open(p,'xb')
    self.assertFalse(p.exists())
    for fd in opened:
     with self.assertRaises(OSError):os.fstat(fd)
 @unittest.skipIf(os.name=='nt','native and API Windows gates cover raw ownership transfer')
 def test_stream_constructor_failure_preserves_other_readers(self):
  cases=[(mode,False) for mode in ('xb','wb','ab','rb','r+b')]+[(mode,True) for mode in ('rb','r+b')]
  for mode,same_file in cases:
   with self.subTest(mode=mode,same_file=same_file),tempfile.TemporaryDirectory() as tmp:
    p=Path(tmp)/'new';other=p.with_name('other');other.write_bytes(b'other caller');foreign=[]
    if mode in ('rb','r+b'):p.write_bytes(b'prior')
    target=p if same_file else other
    def failed_legacy_stream(fd,*args,**kwargs):
     os.close(fd);replacement=os.open(target,os.O_RDONLY);foreign.append(replacement)
     self.assertEqual(replacement,fd);raise OSError('synthetic stream ownership failure')
    def failed_buffer(raw,*args,**kwargs):
     fd=raw.fileno();raw.close();os.fstat(fd)
     replacement=os.open(target,os.O_RDONLY);foreign.append(replacement)
     self.assertNotEqual(replacement,fd);raise OSError('synthetic stream ownership failure')
    buffer='BufferedReader' if mode=='rb' else 'BufferedRandom' if mode=='r+b' else 'BufferedWriter'
    try:
     with patch('os.fdopen',side_effect=failed_legacy_stream),patch.object(self.files.io,buffer,side_effect=failed_buffer):
      with self.assertRaisesRegex(OSError,'synthetic stream ownership failure'):self.files.private_open(p,mode)
     self.assertEqual(os.read(foreign[0],32),b'prior' if same_file else b'other caller')
     if mode=='xb':self.assertFalse(p.exists())
    finally:
     for fd in foreign:
      try:os.close(fd)
      except OSError:pass
 @unittest.skipIf(os.name=='nt','native cleanup targets the opened Windows handle')
 def test_exclusive_open_cleanup_preserves_replacement_file(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'new';original=p.with_name('original')
   def failed_stream(*args,**kwargs):
    p.rename(original);p.write_bytes(b'other caller');raise OSError('synthetic stream failure')
   with patch('os.fdopen',side_effect=failed_stream),patch.object(self.files.io,'BufferedWriter',side_effect=failed_stream):
    with self.assertRaisesRegex(OSError,'synthetic stream failure'):self.files.private_open(p,'xb')
   self.assertEqual(p.read_bytes(),b'other caller')
 def test_publish_collision_preserves_existing_temporary_file(self):
  import types
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'state';p.write_bytes(b'prior');other=p.with_name('state.tmp.collision');other.write_bytes(b'other caller')
   with patch.object(self.files.uuid,'uuid4',return_value=types.SimpleNamespace(hex='collision')):
    with self.assertRaises(FileExistsError):self.files.publish_private(p,b'new')
   self.assertEqual(p.read_bytes(),b'prior');self.assertEqual(other.read_bytes(),b'other caller')
 def test_reader_observes_complete_file(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'state';first=b'a'*65536;second=b'b'*32768
   self.files.publish_private(p,first);stop=threading.Event();observed=[]
   def reader():
    while not stop.is_set():
     with self.files.private_open(p,'rb') as stream:observed.append(stream.read())
   thread=threading.Thread(target=reader);thread.start()
   try:
    for _ in range(12):self.files.publish_private(p,second);self.files.publish_private(p,first)
   finally:stop.set();thread.join(2)
   self.assertTrue(observed);self.assertTrue(all(x in (first,second) for x in observed))
 @unittest.skipIf(os.name=='nt','POSIX symlink fixture')
 def test_symlink_not_followed(self):
  with tempfile.TemporaryDirectory() as tmp:
   target=Path(tmp)/'target';target.write_bytes(b'prior');link=Path(tmp)/'link';link.symlink_to(target)
   with self.assertRaises(OSError):self.files.private_open(link,'wb')
   self.assertEqual(target.read_bytes(),b'prior')

if __name__=='__main__':unittest.main()
