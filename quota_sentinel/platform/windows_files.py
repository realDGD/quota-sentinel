"""Validate the owner and allow-list of a credential file's native DACL."""
def current_user_sid():
    import ctypes as c
    from ctypes import wintypes as w
    api=c.WinDLL('advapi32',use_last_error=True);kernel=c.WinDLL('kernel32',use_last_error=True)
    api.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,c.POINTER(w.HANDLE)];api.OpenProcessToken.restype=w.BOOL
    api.GetTokenInformation.argtypes=[w.HANDLE,w.DWORD,c.c_void_p,w.DWORD,c.POINTER(w.DWORD)];api.GetTokenInformation.restype=w.BOOL
    api.ConvertSidToStringSidW.argtypes=[c.c_void_p,c.POINTER(w.LPWSTR)];api.ConvertSidToStringSidW.restype=w.BOOL
    kernel.GetCurrentProcess.restype=w.HANDLE;kernel.CloseHandle.argtypes=[w.HANDLE];kernel.LocalFree.argtypes=[c.c_void_p]
    token=w.HANDLE();text=w.LPWSTR()
    try:
        if not api.OpenProcessToken(kernel.GetCurrentProcess(),8,c.byref(token)):raise c.WinError(c.get_last_error())
        size=w.DWORD();api.GetTokenInformation(token,1,None,0,c.byref(size));buffer=c.create_string_buffer(size.value)
        if not api.GetTokenInformation(token,1,buffer,size,c.byref(size)):raise c.WinError(c.get_last_error())
        sid=c.cast(buffer,c.POINTER(c.c_void_p))[0]
        if not api.ConvertSidToStringSidW(sid,c.byref(text)):raise c.WinError(c.get_last_error())
        return text.value
    finally:
        if text:kernel.LocalFree(c.cast(text,c.c_void_p))
        if token:kernel.CloseHandle(token)

def verify_private_handle(fd):
    import ctypes as c
    from ctypes import wintypes as w
    import msvcrt
    api=c.WinDLL('advapi32',use_last_error=True);kernel=c.WinDLL('kernel32',use_last_error=True)
    handle=msvcrt.get_osfhandle(fd)
    owner=c.c_void_p();dacl=c.c_void_p();descriptor=c.c_void_p()
    api.GetSecurityInfo.argtypes=[w.HANDLE,w.DWORD,w.DWORD,c.POINTER(c.c_void_p),c.c_void_p,c.POINTER(c.c_void_p),c.c_void_p,c.POINTER(c.c_void_p)]
    api.GetSecurityInfo.restype=w.DWORD
    api.EqualSid.argtypes=[c.c_void_p,c.c_void_p];api.EqualSid.restype=w.BOOL
    api.GetAce.argtypes=[c.c_void_p,w.DWORD,c.POINTER(c.c_void_p)];api.GetAce.restype=w.BOOL
    api.ConvertStringSidToSidW.argtypes=[w.LPCWSTR,c.POINTER(c.c_void_p)];api.ConvertStringSidToSidW.restype=w.BOOL
    api.GetTokenInformation.argtypes=[w.HANDLE,w.DWORD,c.c_void_p,w.DWORD,c.POINTER(w.DWORD)];api.GetTokenInformation.restype=w.BOOL
    api.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,c.POINTER(w.HANDLE)];api.OpenProcessToken.restype=w.BOOL
    kernel.GetCurrentProcess.restype=w.HANDLE;kernel.CloseHandle.argtypes=[w.HANDLE];kernel.LocalFree.argtypes=[c.c_void_p]
    token=w.HANDLE();allocated=[]
    class ACL(c.Structure):_fields_=[('revision',c.c_ubyte),('reserved',c.c_ubyte),('size',w.WORD),('count',w.WORD),('reserved2',w.WORD)]
    class Header(c.Structure):_fields_=[('type',c.c_ubyte),('flags',c.c_ubyte),('size',w.WORD)]
    try:
        if not api.OpenProcessToken(kernel.GetCurrentProcess(),8,c.byref(token)):raise c.WinError(c.get_last_error())
        size=w.DWORD();api.GetTokenInformation(token,1,None,0,c.byref(size))
        buffer=c.create_string_buffer(size.value)
        if not api.GetTokenInformation(token,1,buffer,size,c.byref(size)):raise c.WinError(c.get_last_error())
        user=c.cast(buffer,c.POINTER(c.c_void_p))[0]
        error=api.GetSecurityInfo(handle,1,5,c.byref(owner),None,c.byref(dacl),None,c.byref(descriptor))
        if error:raise c.WinError(error)
        if not owner or not api.EqualSid(owner,user) or not dacl:raise OSError('credential file has an unsafe owner or DACL')
        allowed=[user]
        for text in ('S-1-5-18','S-1-5-32-544','S-1-3-4'):
            sid=c.c_void_p()
            if not api.ConvertStringSidToSidW(text,c.byref(sid)):raise c.WinError(c.get_last_error())
            allocated.append(sid);allowed.append(sid)
        for index in range(c.cast(dacl,c.POINTER(ACL)).contents.count):
            ace=c.c_void_p()
            if not api.GetAce(dacl,index,c.byref(ace)):raise c.WinError(c.get_last_error())
            header=c.cast(ace,c.POINTER(Header)).contents
            # Ordinary allow ACE only; deny ACEs cannot broaden access. Reject
            # callback/object ACE layouts rather than guessing SID offsets.
            if header.type==1:continue
            if header.type!=0 or header.size<12:raise OSError('unsupported credential file access rule')
            sid=c.c_void_p(ace.value+8)
            if not any(api.EqualSid(sid,entry) for entry in allowed):raise OSError('credential file is accessible by other users')
    finally:
        for pointer in allocated:kernel.LocalFree(pointer)
        if descriptor:kernel.LocalFree(descriptor)
        if token:kernel.CloseHandle(token)

def protect_file_handle(handle, descriptor, expected_sid, *, writable):
    """Validate this opened file's identity/owner before changing its DACL."""
    import ctypes as c
    from ctypes import wintypes as w
    api=c.WinDLL('advapi32',use_last_error=True);kernel=c.WinDLL('kernel32',use_last_error=True)
    class Info(c.Structure):
        _fields_=[('attributes',w.DWORD),('creation',w.FILETIME),('access',w.FILETIME),('write',w.FILETIME),('volume',w.DWORD),('sizeHigh',w.DWORD),('sizeLow',w.DWORD),('links',w.DWORD),('indexHigh',w.DWORD),('indexLow',w.DWORD)]
    kernel.GetFileType.argtypes=[w.HANDLE];kernel.GetFileType.restype=w.DWORD
    kernel.GetFileInformationByHandle.argtypes=[w.HANDLE,c.POINTER(Info)];kernel.GetFileInformationByHandle.restype=w.BOOL
    api.GetSecurityInfo.argtypes=[w.HANDLE,w.DWORD,w.DWORD,c.POINTER(c.c_void_p),c.c_void_p,c.c_void_p,c.c_void_p,c.POINTER(c.c_void_p)];api.GetSecurityInfo.restype=w.DWORD
    api.ConvertSidToStringSidW.argtypes=[c.c_void_p,c.POINTER(w.LPWSTR)];api.ConvertSidToStringSidW.restype=w.BOOL
    api.GetSecurityDescriptorDacl.argtypes=[c.c_void_p,c.POINTER(w.BOOL),c.POINTER(c.c_void_p),c.POINTER(w.BOOL)];api.GetSecurityDescriptorDacl.restype=w.BOOL
    api.SetSecurityInfo.argtypes=[w.HANDLE,w.DWORD,w.DWORD,c.c_void_p,c.c_void_p,c.c_void_p,c.c_void_p];api.SetSecurityInfo.restype=w.DWORD
    kernel.LocalFree.argtypes=[c.c_void_p]
    existing=c.c_void_p();owner_text=w.LPWSTR()
    try:
        info=Info()
        if kernel.GetFileType(handle)!=1 or not kernel.GetFileInformationByHandle(handle,c.byref(info)):
            raise OSError('private file must be a regular disk file')
        if info.attributes&(0x10|0x400):raise OSError('private file cannot be a directory or reparse point')
        owner=c.c_void_p();error=api.GetSecurityInfo(handle,1,1,c.byref(owner),None,None,None,c.byref(existing))
        if error:raise c.WinError(error)
        if not owner or not api.ConvertSidToStringSidW(owner,c.byref(owner_text)):
            raise OSError('private file owner unavailable')
        if owner_text.value!=expected_sid:raise OSError('private file must be owned by this user')
        if writable:
            present=w.BOOL();defaulted=w.BOOL();dacl=c.c_void_p()
            if not api.GetSecurityDescriptorDacl(descriptor,c.byref(present),c.byref(dacl),c.byref(defaulted)) or not present or not dacl:
                raise OSError('private file DACL unavailable')
            error=api.SetSecurityInfo(handle,1,0x80000004,None,None,dacl,None)
            if error:raise c.WinError(error)
    finally:
        if owner_text:kernel.LocalFree(c.cast(owner_text,c.c_void_p))
        if existing:kernel.LocalFree(existing)


def protect_directory(path):
    """Create or restrict a directory, with a DACL inherited by SQLite sidecars."""
    import ctypes as c
    from ctypes import wintypes as w
    api=c.WinDLL('advapi32',use_last_error=True);kernel=c.WinDLL('kernel32',use_last_error=True)
    class Attributes(c.Structure):
        _fields_=[('length',w.DWORD),('descriptor',c.c_void_p),('inherit',w.BOOL)]
    sid=current_user_sid();descriptor=c.c_void_p()
    sddl='O:'+sid+'D:P(A;OICI;FA;;;'+sid+')(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)'
    convert=api.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes=[w.LPCWSTR,w.DWORD,c.POINTER(c.c_void_p),c.c_void_p];convert.restype=w.BOOL
    kernel.CreateDirectoryW.argtypes=[w.LPCWSTR,c.POINTER(Attributes)];kernel.CreateDirectoryW.restype=w.BOOL
    kernel.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,c.c_void_p,w.DWORD,w.DWORD,w.HANDLE];kernel.CreateFileW.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE];kernel.LocalFree.argtypes=[c.c_void_p]
    api.GetSecurityInfo.argtypes=[w.HANDLE,w.DWORD,w.DWORD,c.POINTER(c.c_void_p),c.c_void_p,c.c_void_p,c.c_void_p,c.POINTER(c.c_void_p)];api.GetSecurityInfo.restype=w.DWORD
    api.ConvertSidToStringSidW.argtypes=[c.c_void_p,c.POINTER(w.LPWSTR)];api.ConvertSidToStringSidW.restype=w.BOOL
    api.GetSecurityDescriptorDacl.argtypes=[c.c_void_p,c.POINTER(w.BOOL),c.POINTER(c.c_void_p),c.POINTER(w.BOOL)];api.GetSecurityDescriptorDacl.restype=w.BOOL
    api.SetSecurityInfo.argtypes=[w.HANDLE,w.DWORD,w.DWORD,c.c_void_p,c.c_void_p,c.c_void_p,c.c_void_p];api.SetSecurityInfo.restype=w.DWORD
    handle=None;existing=c.c_void_p();owner_text=w.LPWSTR()
    try:
        if not convert(sddl,1,c.byref(descriptor),None):raise c.WinError(c.get_last_error())
        attrs=Attributes(c.sizeof(Attributes),descriptor,False)
        if not kernel.CreateDirectoryW(str(path),c.byref(attrs)) and c.get_last_error()!=183:
            raise c.WinError(c.get_last_error())
        # Open the directory itself rather than following a junction.
        handle=kernel.CreateFileW(str(path),0x00060000,7,None,3,0x02200000,None)
        if handle==c.c_void_p(-1).value:handle=None;raise c.WinError(c.get_last_error())
        class Info(c.Structure):
            _fields_=[('attributes',w.DWORD),('creation',w.FILETIME),('access',w.FILETIME),('write',w.FILETIME),('volume',w.DWORD),('sizeHigh',w.DWORD),('sizeLow',w.DWORD),('links',w.DWORD),('indexHigh',w.DWORD),('indexLow',w.DWORD)]
        info=Info();kernel.GetFileInformationByHandle.argtypes=[w.HANDLE,c.POINTER(Info)];kernel.GetFileInformationByHandle.restype=w.BOOL
        if not kernel.GetFileInformationByHandle(handle,c.byref(info)):raise c.WinError(c.get_last_error())
        if not info.attributes&0x10 or info.attributes&0x400:raise OSError('private directory cannot be a reparse point')
        owner=c.c_void_p();error=api.GetSecurityInfo(handle,1,1,c.byref(owner),None,None,None,c.byref(existing))
        if error:raise c.WinError(error)
        if not api.ConvertSidToStringSidW(owner,c.byref(owner_text)):raise c.WinError(c.get_last_error())
        if owner_text.value!=sid:raise OSError('private directory must be owned by this user')
        present=w.BOOL();defaulted=w.BOOL();dacl=c.c_void_p()
        if not api.GetSecurityDescriptorDacl(descriptor,c.byref(present),c.byref(dacl),c.byref(defaulted)):raise c.WinError(c.get_last_error())
        error=api.SetSecurityInfo(handle,1,0x80000004,None,None,dacl,None)
        if error:raise c.WinError(error)
    finally:
        if owner_text:kernel.LocalFree(c.cast(owner_text,c.c_void_p))
        if existing:kernel.LocalFree(existing)
        if handle:kernel.CloseHandle(handle)
        if descriptor:kernel.LocalFree(descriptor)
