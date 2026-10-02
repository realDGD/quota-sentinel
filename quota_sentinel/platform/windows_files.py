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
