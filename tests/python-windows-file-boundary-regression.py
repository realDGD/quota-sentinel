"""Windows API fixtures: validate the opened object before mutating bytes/ACLs.

These exercise ordering and refusal on any host, not native Windows ABI proof.
"""
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.platform.files import _windows_open
# Import before patch.dict restores sys.modules, so every case patches the
# same module that _windows_open imports, including on Python 3.9.
from quota_sentinel.platform import windows_files

class Function:
 def __init__(self,call):self.call=call
 def __call__(self,*args):return self.call(*args)

class Boundary(unittest.TestCase):
 def opened(self,path,owner,events,attributes=0x80):
  buffers=[]
  def assign(pointer,kind,value):c.cast(pointer,c.POINTER(kind))[0]=value
  def create(path,*args):events.append('open');return os.open(path,os.O_RDWR)
  def security_info(handle,kind,flags,owner_ptr,group,dacl,sacl,descriptor):
   events.append('owner');assign(owner_ptr,c.c_void_p,7);assign(descriptor,c.c_void_p,8);return 0
  def sid_string(sid,text):
   buffer=c.create_unicode_buffer(owner);buffers.append(buffer);assign(text,w.LPWSTR,c.cast(buffer,w.LPWSTR));return True
  def information(handle,pointer):assign(pointer,w.DWORD,attributes);return True
  def descriptor_acl(descriptor,present,dacl,defaulted):
   assign(present,w.BOOL,True);assign(dacl,c.c_void_p,9);return True
  calls={'ConvertStringSecurityDescriptorToSecurityDescriptorW':lambda text,version,output,size:(assign(output,c.c_void_p,1) or True),
   'CreateFileW':create,'CloseHandle':lambda fd:os.close(fd),'LocalFree':lambda pointer:None,
   'GetFileAttributesW':lambda path:0x80,'GetFileType':lambda handle:1,
   'GetFileInformationByHandle':information,'GetSecurityInfo':security_info,
   'ConvertSidToStringSidW':sid_string,'GetSecurityDescriptorDacl':descriptor_acl,
   'SetFileSecurityW':lambda *args:(events.append('path-acl') or True),
   'SetSecurityInfo':lambda *args:(events.append('handle-acl') or 0)}
  api=types.SimpleNamespace(**{name:Function(call) for name,call in calls.items()})
  msvcrt=types.ModuleType('msvcrt');msvcrt.open_osfhandle=lambda handle,flags:handle
  with patch.object(c,'WinDLL',create=True,return_value=api),patch.dict(sys.modules,msvcrt=msvcrt),patch.object(windows_files,'current_user_sid',return_value='current-user'),patch.object(os,'O_BINARY',0,create=True):
   return _windows_open(path,'wb')
 def test_foreign_owner_refused_before_acl_or_truncation(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'auth';path.write_bytes(b'prior');events=[]
   with self.assertRaises(OSError):self.opened(path,'foreign-user',events)
   self.assertEqual(path.read_bytes(),b'prior');self.assertNotIn('handle-acl',events);self.assertNotIn('path-acl',events)
 def test_current_owner_restricted_on_opened_handle(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'auth';path.write_bytes(b'prior');events=[]
   with self.opened(path,'current-user',events) as handle:handle.write(b'synthetic')
   self.assertEqual(path.read_bytes(),b'synthetic');self.assertEqual(events,['open','owner','handle-acl'])
 def test_reparse_object_refused_before_acl_or_truncation(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'auth';path.write_bytes(b'prior');events=[]
   with self.assertRaises(OSError):self.opened(path,'current-user',events,0x480)
   self.assertEqual(path.read_bytes(),b'prior');self.assertNotIn('handle-acl',events)

class ExclusiveCleanup(unittest.TestCase):
 def opened(self,path,failure,mode='xb',same_file=False):
  real_close=os.close;created={};self.opened_fds=[]
  def assign(pointer,kind,value):c.cast(pointer,c.POINTER(kind))[0]=value
  def create(path,access,share,attributes,disposition,flags,template):
   open_flags=os.O_RDWR|(os.O_CREAT|os.O_EXCL if disposition==1 else os.O_CREAT if disposition==4 else 0)
   try:fd=os.open(path,open_flags,0o600)
   except FileExistsError:return c.c_void_p(-1).value
   self.opened_fds.append(fd);created[fd]={'path':Path(path),'identity':os.stat(fd),'delete':False};return fd
  def close(fd):
   real_close(fd)
   record=created.pop(fd,None)
   if record is not None and record['delete']:
    target=record['path'];identity=record['identity']
    try:
     current=target.lstat()
     if (identity.st_dev,identity.st_ino)==(current.st_dev,current.st_ino):target.unlink()
    except FileNotFoundError:pass
  def disposition(handle,kind,info,size):
   self.assertEqual(size,1)
   if kind!=4 or not c.cast(info,c.POINTER(c.c_ubyte)).contents.value:return False
   created[handle]['delete']=True;return True
  def protect(*args,**kwargs):
   if failure=='protect':raise OSError('synthetic protect failure')
  def convert(handle,flags):
   if failure=='fdconvert':raise OSError('synthetic fdconvert failure')
   return handle
  calls={'ConvertStringSecurityDescriptorToSecurityDescriptorW':lambda text,version,output,size:(assign(output,c.c_void_p,1) or True),
   'CreateFileW':create,'CloseHandle':close,'LocalFree':lambda pointer:None,'SetFileInformationByHandle':disposition}
  api=types.SimpleNamespace(**{name:Function(call) for name,call in calls.items()})
  msvcrt=types.ModuleType('msvcrt');msvcrt.open_osfhandle=convert
  with patch.object(c,'WinDLL',create=True,return_value=api),patch.object(c,'get_last_error',create=True,return_value=183),patch.object(c,'WinError',create=True,side_effect=lambda value:FileExistsError('synthetic collision')),patch.dict(sys.modules,msvcrt=msvcrt),patch.object(windows_files,'current_user_sid',return_value='current-user'),patch.object(windows_files,'protect_file_handle',side_effect=protect),patch.object(os,'O_BINARY',0,create=True),patch.object(os,'close',side_effect=close):
   if failure in ('stream','raw-close','reader'):
    target=path if same_file else path.with_name('other')
    def failed_legacy(fd,*args,**kwargs):
     if failure in ('raw-close','reader'):os.close(fd)
     if failure=='reader':self.foreign_fd=os.open(target,os.O_RDONLY)
     raise OSError('synthetic '+failure+' failure')
    def failed_buffer(raw,*args,**kwargs):
     fd=raw.fileno()
     if failure in ('raw-close','reader'):raw.close()
     os.fstat(fd)  # Raw construction has not taken the caller's descriptor.
     if failure=='reader':
      self.foreign_fd=os.open(target,os.O_RDONLY);self.assertNotEqual(self.foreign_fd,fd)
     raise OSError('synthetic '+failure+' failure')
    buffer='BufferedReader' if mode=='rb' else 'BufferedRandom' if mode=='r+b' else 'BufferedWriter'
    with patch.object(os,'fdopen',side_effect=failed_legacy),patch('io.'+buffer,side_effect=failed_buffer):return _windows_open(path,mode)
   return _windows_open(path,mode)
 def test_created_handle_failure_removes_only_created_file(self):
  for failure in ('protect','fdconvert','stream','raw-close'):
   with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
    path=Path(tmp)/'new'
    with self.assertRaisesRegex(OSError,'synthetic '+failure):self.opened(path,failure).close()
    self.assertFalse(path.exists())
    for fd in self.opened_fds:
     with self.assertRaises(OSError):os.fstat(fd)
 def test_success_stream_closes_once_and_preserves_later_reader(self):
  for mode in ('xb','wb','ab','rb','r+b'):
   with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
    path=Path(tmp)/'new';other=path.with_name('other');other.write_bytes(b'other caller')
    if mode in ('rb','r+b'):path.write_bytes(b'synthetic')
    with self.opened(path,None,mode) as stream:
     fd=stream.fileno()
     if mode=='rb':self.assertEqual(stream.read(),b'synthetic')
     else:stream.write(b'synthetic')
    with self.assertRaises(OSError):os.fstat(fd)
    replacement=os.open(other,os.O_RDONLY)
    try:
     self.assertEqual(replacement,fd);stream.close();stream.raw.close()
     self.assertEqual(os.read(replacement,32),b'other caller')
    finally:os.close(replacement)
    self.assertEqual(path.read_bytes(),b'synthetic')
 def test_stream_constructor_failure_preserves_other_readers(self):
  cases=[(mode,False) for mode in ('xb','wb','ab','rb','r+b')]+[(mode,True) for mode in ('rb','r+b')]
  for mode,same_file in cases:
   with self.subTest(mode=mode,same_file=same_file),tempfile.TemporaryDirectory() as tmp:
    path=Path(tmp)/'new';other=path.with_name('other');other.write_bytes(b'other caller')
    if mode in ('rb','r+b'):path.write_bytes(b'prior')
    self.foreign_fd=None
    try:
     with self.assertRaisesRegex(OSError,'synthetic reader'):self.opened(path,'reader',mode,same_file)
     self.assertEqual(os.read(self.foreign_fd,32),b'prior' if same_file else b'other caller')
     if mode=='xb':self.assertFalse(path.exists())
     else:self.assertTrue(path.exists())
    finally:
     if self.foreign_fd is not None:
      try:os.close(self.foreign_fd)
      except OSError:pass
 def test_create_new_collision_preserves_existing_file(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'existing';path.write_bytes(b'other caller')
   with self.assertRaises(FileExistsError):self.opened(path,'protect')
   self.assertEqual(self.opened_fds,[])
   self.assertEqual(path.read_bytes(),b'other caller')
if __name__=='__main__':unittest.main()
