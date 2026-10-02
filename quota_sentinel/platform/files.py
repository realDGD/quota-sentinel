"""Restrict access before secret bytes are written; publish complete files."""
import os
from pathlib import Path
import stat
import uuid


def private_open(path, mode):
    if mode not in ('rb', 'wb', 'xb', 'ab', 'r+b'):
        raise ValueError('private_open requires a supported binary mode')
    path = Path(path)
    if os.name == 'nt':
        return _windows_open(path, mode)
    flags = {'rb': os.O_RDONLY, 'wb': os.O_WRONLY | os.O_CREAT,
             'xb': os.O_WRONLY | os.O_CREAT | os.O_EXCL,
             'ab': os.O_WRONLY | os.O_CREAT | os.O_APPEND,
             'r+b': os.O_RDWR}[mode]
    flags |= getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError('private file must be a regular file owned by this user')
        if mode != 'rb':
            os.fchmod(fd, 0o600)
        if mode == 'wb':
            os.ftruncate(fd, 0)
        return os.fdopen(fd, mode)
    except BaseException:
        os.close(fd)
        raise


def _windows_open(path, mode):
    import ctypes as c
    from ctypes import wintypes as w
    import msvcrt
    kernel = c.WinDLL('kernel32', use_last_error=True)
    security = c.WinDLL('advapi32', use_last_error=True)
    class Attributes(c.Structure):
        _fields_ = [('length', w.DWORD), ('descriptor', c.c_void_p), ('inherit', w.BOOL)]
    convert = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.c_void_p]
    convert.restype = w.BOOL
    descriptor = c.c_void_p()
    # Protected DACL; owner, SYSTEM and administrators only. No inherited ACEs.
    if not convert('D:P(A;;FA;;;OW)(A;;FA;;;SY)(A;;FA;;;BA)', 1, c.byref(descriptor), None):
        raise c.WinError(c.get_last_error())
    create = kernel.CreateFileW
    create.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.POINTER(Attributes), w.DWORD, w.DWORD, w.HANDLE]
    create.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [c.c_void_p]
    try:
        if mode != 'rb' and path.exists():
            set_security = security.SetFileSecurityW
            set_security.argtypes = [w.LPCWSTR, w.DWORD, c.c_void_p]
            set_security.restype = w.BOOL
            if not set_security(str(path), 0x80000004, descriptor):
                raise c.WinError(c.get_last_error())
        attributes = Attributes(c.sizeof(Attributes), descriptor, False)
        access = 0x80000000 if mode == 'rb' else 0xC0000000
        disposition = {'rb': 3, 'wb': 4, 'xb': 1, 'ab': 4, 'r+b': 3}[mode]
        handle = create(str(path), access, 7, c.byref(attributes), disposition,
                        0x00200080, None)  # OPEN_REPARSE_POINT, NORMAL
        if handle == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        # Reject reparse points rather than traversing a secret-file symlink.
        get_attributes = kernel.GetFileAttributesW
        get_attributes.argtypes = [w.LPCWSTR]; get_attributes.restype = w.DWORD
        if get_attributes(str(path)) & 0x400:
            kernel.CloseHandle(handle)
            raise OSError('private file cannot be a reparse point')
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDONLY if mode == 'rb' else os.O_RDWR))
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        if mode == 'wb':os.ftruncate(fd, 0)
        if mode == 'ab':os.lseek(fd, 0, os.SEEK_END)
        return os.fdopen(fd, mode)
    finally:
        kernel.LocalFree(descriptor)


def publish_private(path, payload):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp.' + uuid.uuid4().hex)
    try:
        with private_open(temporary, 'xb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        # Windows does not provide POSIX directory fsync. File bytes are flushed;
        # rename visibility is atomic, directory durability is OS/filesystem dependent.
        if os.name != 'nt':
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
    finally:
        try:temporary.unlink()
        except FileNotFoundError:pass
