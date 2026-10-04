"""Native private filesystem fixtures; no credentials or external services."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat


def minimal_process_environment(**selected):
    """Keep the Windows loader coordinate without inheriting ambient tokens."""
    environment={'SYSTEMROOT':os.environ['SYSTEMROOT']} if os.name=='nt' else {}
    environment.update(selected)
    return environment


def private_test_directory(temporary, name='owned'):
    from quota_sentinel.platform.files import private_directory
    return private_directory(Path(temporary.name) / name)


def assert_private_path(case, path, *, directory=False):
    """Explicit private objects require the current user's owner and DACL."""
    path = Path(path)
    if os.name != 'nt':
        info = path.stat()
        case.assertEqual(info.st_uid, os.getuid())
        case.assertEqual(stat.S_IMODE(info.st_mode), 0o700 if directory else 0o600)
        return
    _assert_windows_access(case,path)


def assert_inherited_private_child(case, path, parent):
    """Validate an ordinary child or SQLite sidecar under a proven private dir.

    Windows may assign an elevated token's Administrators owner. Accept it
    only here, where every grant is inherited from the checked private parent.
    This does not change the strict owner contract for credential files.
    """
    path,parent=Path(path),Path(parent)
    case.assertEqual(path.parent,parent)
    assert_private_path(case,parent,directory=True)
    if os.name=='nt':_assert_windows_access(case,path,require_inherited=True)
    else:case.assertEqual(path.stat().st_uid,os.getuid())


def _assert_windows_access(case, path, *, require_inherited=False):
    import ctypes as c
    from ctypes import wintypes as w
    from quota_sentinel.platform.windows_files import current_user_sid
    api = c.WinDLL('advapi32', use_last_error=True)
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD,c.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [c.c_void_p]
    api.GetSecurityInfo.argtypes = [w.HANDLE,w.DWORD,w.DWORD,c.POINTER(c.c_void_p),c.c_void_p,c.POINTER(c.c_void_p),c.c_void_p,c.POINTER(c.c_void_p)]
    api.GetSecurityInfo.restype = w.DWORD
    api.ConvertSidToStringSidW.argtypes = [c.c_void_p,c.POINTER(w.LPWSTR)]
    api.ConvertSidToStringSidW.restype = w.BOOL
    api.GetAce.argtypes = [c.c_void_p,w.DWORD,c.POINTER(c.c_void_p)]
    api.GetAce.restype = w.BOOL
    class ACL(c.Structure):
        _fields_ = [('revision',c.c_ubyte),('reserved',c.c_ubyte),('size',w.WORD),('count',w.WORD),('reserved2',w.WORD)]
    class Header(c.Structure):
        _fields_ = [('type',c.c_ubyte),('flags',c.c_ubyte),('size',w.WORD)]
    def sid_text(sid):
        text=w.LPWSTR()
        if not api.ConvertSidToStringSidW(sid,c.byref(text)):raise c.WinError(c.get_last_error())
        try:return text.value
        finally:kernel.LocalFree(c.cast(text,c.c_void_p))
    handle=kernel.CreateFileW(str(path),0x20000,7,None,3,0x02200000,None)
    if handle==c.c_void_p(-1).value:raise c.WinError(c.get_last_error())
    descriptor=c.c_void_p();owner=c.c_void_p();dacl=c.c_void_p()
    try:
        error=api.GetSecurityInfo(handle,1,5,c.byref(owner),None,c.byref(dacl),None,c.byref(descriptor))
        if error:raise c.WinError(error)
        user=current_user_sid()
        owners={user,'S-1-5-32-544'} if require_inherited else {user}
        case.assertTrue(owner);case.assertIn(sid_text(owner),owners)
        case.assertTrue(dacl, 'a null DACL grants unrestricted access')
        allowed={user,'S-1-5-18','S-1-5-32-544','S-1-3-4'}
        granted=set()
        for index in range(c.cast(dacl,c.POINTER(ACL)).contents.count):
            ace=c.c_void_p()
            if not api.GetAce(dacl,index,c.byref(ace)):raise c.WinError(c.get_last_error())
            header=c.cast(ace,c.POINTER(Header)).contents
            if header.type==1:continue
            case.assertEqual(header.type,0,'unexpected object/callback access rule')
            case.assertGreaterEqual(header.size,12)
            if require_inherited:case.assertTrue(header.flags&0x10,'ordinary child access must come from its private parent')
            identity=sid_text(c.c_void_p(ace.value+8))
            case.assertIn(identity,allowed)
            granted.add(identity)
        case.assertIn(user,granted,'current user must have an explicit or inherited access rule')
    finally:
        if descriptor:kernel.LocalFree(descriptor)
        kernel.CloseHandle(handle)


def loosen_directory_access(path):
    """Synthetic permissive directory, keeping the explicit current owner."""
    path=Path(path)
    if os.name!='nt':path.chmod(0o755);return
    import ctypes as c
    from ctypes import wintypes as w
    from quota_sentinel.platform.windows_files import current_user_sid
    api=c.WinDLL('advapi32',use_last_error=True);kernel=c.WinDLL('kernel32',use_last_error=True)
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[w.LPCWSTR,w.DWORD,c.POINTER(c.c_void_p),c.c_void_p]
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype=w.BOOL
    api.GetSecurityDescriptorDacl.argtypes=[c.c_void_p,c.POINTER(w.BOOL),c.POINTER(c.c_void_p),c.POINTER(w.BOOL)]
    api.GetSecurityDescriptorDacl.restype=w.BOOL
    api.SetNamedSecurityInfoW.argtypes=[w.LPWSTR,w.DWORD,w.DWORD,c.c_void_p,c.c_void_p,c.c_void_p,c.c_void_p]
    api.SetNamedSecurityInfoW.restype=w.DWORD;kernel.LocalFree.argtypes=[c.c_void_p]
    descriptor=c.c_void_p()
    try:
        sddl='D:P(A;OICI;FA;;;'+current_user_sid()+')(A;OICI;FA;;;WD)'
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,c.byref(descriptor),None):raise c.WinError(c.get_last_error())
        present=w.BOOL();defaulted=w.BOOL();dacl=c.c_void_p()
        if not api.GetSecurityDescriptorDacl(descriptor,c.byref(present),c.byref(dacl),c.byref(defaulted)):raise c.WinError(c.get_last_error())
        error=api.SetNamedSecurityInfoW(str(path),1,0x80000004,None,None,dacl,None)
        if error:raise c.WinError(error)
    finally:
        if descriptor:kernel.LocalFree(descriptor)


@contextmanager
def _without_windows_backup_restore_privileges():
    """Temporarily enforce ordinary DACL checks in this test process only.

    SSH/elevated test tokens can have both bypass privileges enabled. Save
    exactly the entries changed by Windows; absent or disabled privileges
    stay untouched, and unrelated privileges are never requested.
    """
    import ctypes as c
    from ctypes import wintypes as w
    api=c.WinDLL('advapi32',use_last_error=True);kernel=c.WinDLL('kernel32',use_last_error=True)
    class LUID(c.Structure):
        _fields_=[('LowPart',c.c_uint32),('HighPart',c.c_int32)]
    class Entry(c.Structure):
        _fields_=[('Luid',LUID),('Attributes',c.c_uint32)]
    class Privileges(c.Structure):
        _fields_=[('PrivilegeCount',c.c_uint32),('Privileges',Entry*2)]
    kernel.GetCurrentProcess.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE]
    api.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,c.POINTER(w.HANDLE)]
    api.OpenProcessToken.restype=w.BOOL
    api.LookupPrivilegeValueW.argtypes=[w.LPCWSTR,w.LPCWSTR,c.POINTER(LUID)]
    api.LookupPrivilegeValueW.restype=w.BOOL
    api.AdjustTokenPrivileges.argtypes=[w.HANDLE,w.BOOL,c.POINTER(Privileges),w.DWORD,c.POINTER(Privileges),c.POINTER(w.DWORD)]
    api.AdjustTokenPrivileges.restype=w.BOOL
    token=w.HANDLE()
    if not api.OpenProcessToken(kernel.GetCurrentProcess(),0x28,c.byref(token)):raise c.WinError(c.get_last_error())
    previous=Privileges();adjusted=False
    try:
        requested=Privileges();requested.PrivilegeCount=2
        for entry,name in zip(requested.Privileges,('SeBackupPrivilege','SeRestorePrivilege')):
            if not api.LookupPrivilegeValueW(None,name,c.byref(entry.Luid)):raise c.WinError(c.get_last_error())
            entry.Attributes=0
        returned=w.DWORD();c.set_last_error(0)
        if not api.AdjustTokenPrivileges(token,False,c.byref(requested),c.sizeof(previous),c.byref(previous),c.byref(returned)):
            raise c.WinError(c.get_last_error())
        adjusted=True
        error=c.get_last_error()
        # ERROR_NOT_ALL_ASSIGNED means a requested privilege was absent; the
        # returned previous state still records every entry actually changed.
        if error not in (0,1300):raise c.WinError(error)
        yield
    finally:
        try:
            if adjusted and previous.PrivilegeCount:
                c.set_last_error(0)
                if not api.AdjustTokenPrivileges(token,False,c.byref(previous),0,None,None):raise c.WinError(c.get_last_error())
                error=c.get_last_error()
                if error:raise c.WinError(error)
        finally:kernel.CloseHandle(token)


@contextmanager
def deny_child_directory_creation(path):
    """Make mkdir fail at the actual native permissions boundary."""
    path=Path(path)
    if os.name!='nt':
        mode=stat.S_IMODE(path.stat().st_mode);path.chmod(0o500)
        try:yield
        finally:path.chmod(mode)
        return
    import ctypes as c
    from ctypes import wintypes as w
    from quota_sentinel.platform.windows_files import current_user_sid
    api=c.WinDLL('advapi32',use_last_error=True);kernel=c.WinDLL('kernel32',use_last_error=True)
    api.GetNamedSecurityInfoW.argtypes=[w.LPWSTR,w.DWORD,w.DWORD,c.c_void_p,c.c_void_p,c.POINTER(c.c_void_p),c.c_void_p,c.POINTER(c.c_void_p)]
    api.GetNamedSecurityInfoW.restype=w.DWORD
    api.SetNamedSecurityInfoW.argtypes=[w.LPWSTR,w.DWORD,w.DWORD,c.c_void_p,c.c_void_p,c.c_void_p,c.c_void_p]
    api.SetNamedSecurityInfoW.restype=w.DWORD
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes=[w.LPCWSTR,w.DWORD,c.POINTER(c.c_void_p),c.c_void_p]
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype=w.BOOL
    api.GetSecurityDescriptorDacl.argtypes=[c.c_void_p,c.POINTER(w.BOOL),c.POINTER(c.c_void_p),c.POINTER(w.BOOL)]
    api.GetSecurityDescriptorDacl.restype=w.BOOL
    kernel.LocalFree.argtypes=[c.c_void_p]
    original=c.c_void_p();original_dacl=c.c_void_p();descriptor=c.c_void_p()
    try:
        error=api.GetNamedSecurityInfoW(str(path),1,4,None,None,c.byref(original_dacl),None,c.byref(original))
        if error:raise c.WinError(error)
        sid=current_user_sid();sddl='D:P(D;;0x4;;;'+sid+')(A;;FA;;;'+sid+')(A;;FA;;;SY)(A;;FA;;;BA)'
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl,1,c.byref(descriptor),None):raise c.WinError(c.get_last_error())
        dacl=c.c_void_p();present=w.BOOL();defaulted=w.BOOL()
        if not api.GetSecurityDescriptorDacl(descriptor,c.byref(present),c.byref(dacl),c.byref(defaulted)):raise c.WinError(c.get_last_error())
        error=api.SetNamedSecurityInfoW(str(path),1,0x80000004,None,None,dacl,None)
        if error:raise c.WinError(error)
        try:
            with _without_windows_backup_restore_privileges():yield
        finally:
            error=api.SetNamedSecurityInfoW(str(path),1,0x80000004,None,None,original_dacl,None)
            if error:raise c.WinError(error)
    finally:
        if descriptor:kernel.LocalFree(descriptor)
        if original:kernel.LocalFree(original)
