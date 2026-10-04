"""Isolated OS password APIs. Secret input/output uses private pipes only."""
import contextlib
import json
import os
from pathlib import Path
import stat
import sys
from quota_sentinel.config import CredentialReference
from .credentials import CredentialUnavailable, MAX_SECRET_BYTES, validate_reference
from .files import private_open, publish_private

def file_operation(action, reference, value):
    path=Path(reference.locator)
    if action=='write':
        if path.is_symlink():raise CredentialUnavailable('unsafe credential file')
        publish_private(path,value.encode('utf-8'));return None
    with private_open(path,'rb') as handle:
        info=os.fstat(handle.fileno())
        if os.name=='nt':
            from .windows_files import verify_private_handle
            verify_private_handle(handle.fileno())
        elif info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
            raise CredentialUnavailable('credential file is accessible by other users')
        raw=handle.read(MAX_SECRET_BYTES+1)
        if len(raw)>MAX_SECRET_BYTES:raise CredentialUnavailable('credential file is too large')
    if action=='delete':path.unlink();return None
    return raw.decode('utf-8')

def macos_operation(action, reference, value, security_bin, timeout):
    if action=='read' and security_bin!='/usr/bin/security':
        return macos_security_read(reference,security_bin,timeout)
    # Native reads avoid a CLI permission dialog for entries written by this
    # worker; existing security-trusted entries retain their CLI read path.
    import ctypes as c
    cf=c.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    security=c.CDLL('/System/Library/Frameworks/Security.framework/Security')
    cf.CFStringCreateWithCString.argtypes=[c.c_void_p,c.c_char_p,c.c_uint32];cf.CFStringCreateWithCString.restype=c.c_void_p
    cf.CFDataCreate.argtypes=[c.c_void_p,c.c_void_p,c.c_long];cf.CFDataCreate.restype=c.c_void_p
    cf.CFDataGetLength.argtypes=[c.c_void_p];cf.CFDataGetLength.restype=c.c_long
    cf.CFDataGetBytePtr.argtypes=[c.c_void_p];cf.CFDataGetBytePtr.restype=c.c_void_p
    cf.CFDictionaryCreate.argtypes=[c.c_void_p,c.POINTER(c.c_void_p),c.POINTER(c.c_void_p),c.c_long,c.c_void_p,c.c_void_p];cf.CFDictionaryCreate.restype=c.c_void_p
    cf.CFArrayCreate.argtypes=[c.c_void_p,c.POINTER(c.c_void_p),c.c_long,c.c_void_p];cf.CFArrayCreate.restype=c.c_void_p
    cf.CFRelease.argtypes=[c.c_void_p]
    security.SecItemCopyMatching.argtypes=[c.c_void_p,c.POINTER(c.c_void_p)];security.SecItemCopyMatching.restype=c.c_int32
    security.SecItemAdd.argtypes=[c.c_void_p,c.c_void_p];security.SecItemAdd.restype=c.c_int32
    security.SecItemUpdate.argtypes=[c.c_void_p,c.c_void_p];security.SecItemUpdate.restype=c.c_int32
    security.SecItemDelete.argtypes=[c.c_void_p];security.SecItemDelete.restype=c.c_int32
    security.SecTrustedApplicationCreateFromPath.argtypes=[c.c_char_p,c.POINTER(c.c_void_p)];security.SecTrustedApplicationCreateFromPath.restype=c.c_int32
    security.SecAccessCreate.argtypes=[c.c_void_p,c.c_void_p,c.POINTER(c.c_void_p)];security.SecAccessCreate.restype=c.c_int32
    allocated=[]
    def symbol(name):return c.c_void_p.in_dll(security,name).value
    def string(text):
        pointer=cf.CFStringCreateWithCString(None,text.encode(),0x08000100)
        if not pointer:raise CredentialUnavailable('Keychain allocation failed')
        allocated.append(pointer);return pointer
    def dictionary(entries):
        keys=(c.c_void_p*len(entries))(*(k for k,v in entries));values=(c.c_void_p*len(entries))(*(v for k,v in entries))
        pointer=cf.CFDictionaryCreate(None,keys,values,len(entries),None,None)
        if not pointer:raise CredentialUnavailable('Keychain allocation failed')
        allocated.append(pointer);return pointer
    try:
        entries=[(symbol('kSecClass'),symbol('kSecClassGenericPassword')),
                 (symbol('kSecAttrService'),string(reference.locator)),
                 (symbol('kSecAttrAccount'),string(reference.account)),
                 (symbol('kSecUseAuthenticationUI'),symbol('kSecUseAuthenticationUIFail'))]
        query=dictionary(entries)
        if action=='read':
            result=c.c_void_p()
            read_query=dictionary(entries+[(symbol('kSecReturnData'),c.c_void_p.in_dll(cf,'kCFBooleanTrue').value)])
            code=security.SecItemCopyMatching(read_query,c.byref(result))
            if not code:
                allocated.append(result.value)
                length=cf.CFDataGetLength(result)
                if length>MAX_SECRET_BYTES:raise CredentialUnavailable('Keychain value is too large')
                return c.string_at(cf.CFDataGetBytePtr(result),length).decode('utf-8')
            return macos_security_read(reference,security_bin,timeout)
        if action=='delete':code=security.SecItemDelete(query)
        else:
            raw=value.encode();data=cf.CFDataCreate(None,raw,len(raw));allocated.append(data)
            if not data:raise CredentialUnavailable('Keychain allocation failed')
            attributes=dictionary([(symbol('kSecValueData'),data)])
            code=security.SecItemUpdate(query,attributes)
            if code==-25300:
                trusted=c.c_void_p();access=c.c_void_p()
                if security.SecTrustedApplicationCreateFromPath(b'/usr/bin/security',c.byref(trusted)):
                    raise CredentialUnavailable('Keychain trusted reader is unavailable')
                allocated.append(trusted.value)
                writer=c.c_void_p()
                if security.SecTrustedApplicationCreateFromPath(None,c.byref(writer)):
                    raise CredentialUnavailable('Keychain trusted writer is unavailable')
                allocated.append(writer.value)
                readers=cf.CFArrayCreate(None,(c.c_void_p*2)(trusted.value,writer.value),2,None);allocated.append(readers)
                if security.SecAccessCreate(string('quota-sentinel'),readers,c.byref(access)):
                    raise CredentialUnavailable('Keychain access policy is unavailable')
                allocated.append(access.value)
                code=security.SecItemAdd(dictionary(entries+[(symbol('kSecValueData'),data),(symbol('kSecAttrAccess'),access.value)]),None)
        if code and not (action=='delete' and code==-25300):raise CredentialUnavailable('Keychain write is unavailable')
    finally:
        for pointer in reversed(allocated):
            if pointer:cf.CFRelease(pointer)

def macos_security_read(reference,security_bin,timeout):
    from .paths import resolve_launcher
    from .process import run_bounded
    result=run_bounded((*resolve_launcher('security',explicit=security_bin),
        'find-generic-password','-a',reference.account,'-s',reference.locator,'-w'),
        cwd=Path.cwd(),environment=os.environ,input_data=None,timeout=timeout,
        kill_grace=0,max_bytes=MAX_SECRET_BYTES+1)
    if result.returncode:raise CredentialUnavailable('Keychain item is unavailable')
    return result.stdout.decode('utf-8').strip()

def windows_target(reference):
    import hashlib
    identity=json.dumps([reference.locator,reference.account],ensure_ascii=False).encode()
    return 'Quota-Sentinel/'+hashlib.sha256(identity).hexdigest()

def windows_operation(action, reference, value, api=None):
    import ctypes as c
    from ctypes import wintypes as w
    class Credential(c.Structure):
        _fields_=[('Flags',w.DWORD),('Type',w.DWORD),('TargetName',w.LPWSTR),('Comment',w.LPWSTR),
            ('LastWritten',w.FILETIME),('CredentialBlobSize',w.DWORD),('CredentialBlob',c.POINTER(c.c_ubyte)),
            ('Persist',w.DWORD),('AttributeCount',w.DWORD),('Attributes',c.c_void_p),
            ('TargetAlias',w.LPWSTR),('UserName',w.LPWSTR)]
    api=api or c.WinDLL('advapi32',use_last_error=True)
    if isinstance(api,c.CDLL):
        api.CredReadW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,c.POINTER(c.POINTER(Credential))];api.CredReadW.restype=w.BOOL
        api.CredWriteW.argtypes=[c.POINTER(Credential),w.DWORD];api.CredWriteW.restype=w.BOOL
        api.CredDeleteW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD];api.CredDeleteW.restype=w.BOOL
        api.CredFree.argtypes=[c.c_void_p];api.CredFree.restype=None
    target=windows_target(reference)
    if action=='read':
        pointer=c.POINTER(Credential)()
        if not api.CredReadW(target,1,0,c.byref(pointer)):raise CredentialUnavailable('Windows credential unavailable in this user session')
        try:
            item=pointer.contents
            if item.CredentialBlobSize>2560:raise CredentialUnavailable('Windows credential is too large')
            return c.string_at(item.CredentialBlob,item.CredentialBlobSize).decode('utf-8')
        finally:api.CredFree(pointer)
    if action=='delete':
        if not api.CredDeleteW(target,1,0):raise CredentialUnavailable('Windows credential deletion unavailable')
        return None
    raw=value.encode()
    if len(raw)>2560:raise CredentialUnavailable('Windows credential exceeds the native limit')
    blob=(c.c_ubyte*len(raw)).from_buffer_copy(raw)
    item=Credential(Type=1,TargetName=target,CredentialBlobSize=len(raw),CredentialBlob=blob,Persist=2,UserName=reference.account)
    if not api.CredWriteW(c.byref(item),0):raise CredentialUnavailable('Windows credential write unavailable in this user session')

def secret_service_operation(action, reference, value, module=None):
    if module is None:import secretstorage as module
    connection=module.dbus_init()
    try:
        # Unlike get_default_collection, this never creates a collection or
        # requests that a missing/locked collection be unlocked.
        collection=module.get_collection_by_alias(connection,'default')
        if collection.is_locked():raise CredentialUnavailable('Secret Service is locked')
        service=reference.locator.split(':',1)[1]
        attrs=dict(application='quota-sentinel',service=service,account=reference.account)
        items=list(collection.search_items(attrs))
        if action=='write':
            collection.create_item('quota-sentinel',attrs,value.encode(),replace=True);return None
        if len(items)!=1 or items[0].is_locked():raise CredentialUnavailable('Secret Service item is unavailable or ambiguous')
        if action=='delete':items[0].delete();return None
        return items[0].get_secret().decode('utf-8')
    finally:connection.close()

def kwallet_operation(action, reference, value, module=None):
    if module is None:import dbus as module
    bus=module.SessionBus(private=True)
    try:
        names=bus.list_names()
        name=next((n for n in ('org.kde.kwalletd6','org.kde.kwalletd5') if n in names),None)
        if name is None:raise CredentialUnavailable('KWallet service is not running')
        wallet=module.Interface(bus.get_object(name,'/modules/'+name.rsplit('.',1)[1]),'org.kde.KWallet')
        wallet_name=wallet.networkWallet()
        if not wallet.isOpen(wallet_name):raise CredentialUnavailable('KWallet is locked')
        app='quota-sentinel';handle=wallet.open(wallet_name,module.Int64(0),app)
        if handle<0:raise CredentialUnavailable('KWallet access is unavailable')
        folder='quota-sentinel';key=windows_target(reference)
        try:
            if action=='write':
                if not wallet.hasFolder(handle,folder,app) and not wallet.createFolder(handle,folder,app):raise CredentialUnavailable('KWallet folder is unavailable')
                result=wallet.writePassword(handle,folder,key,value,app)
                if result:raise CredentialUnavailable('KWallet write is unavailable')
                return None
            if not wallet.hasEntry(handle,folder,key,app):raise CredentialUnavailable('KWallet item is missing')
            if action=='delete':
                if wallet.removeEntry(handle,folder,key,app):raise CredentialUnavailable('KWallet deletion unavailable')
                return None
            return str(wallet.readPassword(handle,folder,key,app))
        finally:wallet.close(handle,False,app)
    finally:bus.close()

def dispatch(request):
    reference=CredentialReference(**request['reference']);validate_reference(reference)
    action=request['action'];value=request.get('value')
    if action not in ('read','write','delete'):raise CredentialUnavailable('invalid credential operation')
    if action=='write' and (not isinstance(value,str) or not value or len(value.encode())>MAX_SECRET_BYTES):raise CredentialUnavailable('invalid credential value')
    if reference.kind=='file':return file_operation(action,reference,value)
    if reference.kind!='system':raise CredentialUnavailable('unsupported worker reference')
    system=request['system']
    if system=='Darwin':return macos_operation(action,reference,value,request['security_bin'],request['timeout'])
    if system=='Windows':return windows_operation(action,reference,value)
    if system=='Linux':
        if reference.locator.startswith('secret-service:'):return secret_service_operation(action,reference,value)
        if reference.locator.startswith('kwallet:'):return kwallet_operation(action,reference,value)
    raise CredentialUnavailable('selected system credential backend is unavailable')

def main():
    try:
        request=json.loads(sys.stdin.buffer.read(65537))
        # Optional libraries cannot leak credential-bearing diagnostics to the
        # protocol or parent logs, including their import-time output.
        class Discard:
            def write(self,text):return len(text)
            def flush(self):pass
        with contextlib.redirect_stdout(Discard()),contextlib.redirect_stderr(Discard()):
            value=dispatch(request)
        result=dict(status='ok')
        if request['action']=='read':result['value']=value
        code=0
    except Exception:
        result=dict(status='unavailable');code=69
    sys.stdout.write(json.dumps(result,ensure_ascii=False));return code

if __name__=='__main__':sys.exit(main())
