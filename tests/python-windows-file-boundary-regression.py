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
  with patch.object(c,'WinDLL',create=True,return_value=api),patch.dict(sys.modules,msvcrt=msvcrt),patch('quota_sentinel.platform.windows_files.current_user_sid',return_value='current-user'),patch.object(os,'O_BINARY',0,create=True):
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
if __name__=='__main__':unittest.main()
