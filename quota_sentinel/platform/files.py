"""Restrict access before secret bytes are written; publish complete files."""
import io
import os
from pathlib import Path
import stat
import uuid


class _OwnedRawFile(io.FileIO):
    """Keep the caller's descriptor until buffered construction succeeds."""
    def __init__(self, fd, mode):
        self._owned_fd = None
        super().__init__(fd, mode, closefd=False)

    def close(self):
        owned = self._owned_fd
        self._owned_fd = None
        try: super().close()
        finally:
            if owned is not None: os.close(owned)


def _owned_stream(fd, mode):
    raw = _OwnedRawFile(fd, mode)
    try:
        buffer = io.BufferedReader if mode == 'rb' else io.BufferedRandom if mode == 'r+b' else io.BufferedWriter
        stream = buffer(raw)
    except BaseException:
        try: raw.close()
        except BaseException: pass
        raise
    raw._owned_fd = fd
    return stream


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
    info = None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError('private file must be a regular file owned by this user')
        if mode != 'rb':
            os.fchmod(fd, 0o600)
        if mode == 'wb':
            os.ftruncate(fd, 0)
        return _owned_stream(fd, mode)
    except BaseException:
        if mode == 'xb':
            try:
                # Only remove the object created by this successful O_EXCL.
                # stat(fd) also works when the initial fstat failed.
                identity = info if info is not None else os.stat(fd)
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    path.unlink()
            except BaseException:
                pass  # Preserve the original open/validation error.
        try: os.close(fd)
        except BaseException: pass  # Preserve the triggering validation/construction error.
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
    # Explicit user owner, including an elevated administrator token whose
    # default object owner may otherwise be the Administrators group.
    from .windows_files import current_user_sid
    sid=current_user_sid()
    sddl='O:'+sid+'D:P(A;;FA;;;'+sid+')(A;;FA;;;SY)(A;;FA;;;BA)'
    if not convert(sddl, 1, c.byref(descriptor), None):
        raise c.WinError(c.get_last_error())
    create = kernel.CreateFileW
    create.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.POINTER(Attributes), w.DWORD, w.DWORD, w.HANDLE]
    create.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [c.c_void_p]
    def discard_created_handle(handle):
        if mode == 'xb':
            try:
                delete_file = c.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile is BOOLEAN.
                # FileDispositionInfo targets our opened object even if its
                # pathname has been replaced; deletion completes on close.
                delete(handle, 4, c.byref(delete_file), c.sizeof(delete_file))
            except BaseException:
                pass  # Cleanup must not replace the original failure.
    try:
        if mode == 'xb':
            delete = kernel.SetFileInformationByHandle
            delete.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
            delete.restype = w.BOOL
        attributes = Attributes(c.sizeof(Attributes), descriptor, False)
        access = (0x80000000 | 0x00020000) if mode == 'rb' else (0xC0000000 | 0x00060000)
        if mode == 'xb': access |= 0x00010000  # DELETE for failed creation cleanup.
        disposition = {'rb': 3, 'wb': 4, 'xb': 1, 'ab': 4, 'r+b': 3}[mode]
        handle = create(str(path), access, 7, c.byref(attributes), disposition,
                        0x00200080, None)  # OPEN_REPARSE_POINT, NORMAL
        if handle == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            from .windows_files import protect_file_handle
            protect_file_handle(handle,descriptor,sid,writable=mode!='rb')
            fd = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDONLY if mode == 'rb' else os.O_RDWR))
        except BaseException:
            discard_created_handle(handle)
            try: kernel.CloseHandle(handle)
            except BaseException: pass
            raise
        try:
            if mode == 'wb':os.ftruncate(fd, 0)
            if mode == 'ab':os.lseek(fd, 0, os.SEEK_END)
            return _owned_stream(fd, mode)
        except BaseException:
            # Buffered construction never owns/closes fd before success.
            discard_created_handle(handle)
            try: os.close(fd)
            except BaseException: pass
            raise
    finally:
        kernel.LocalFree(descriptor)


def publish_private(path, payload):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp.' + uuid.uuid4().hex)
    created = False
    try:
        with private_open(temporary, 'xb') as handle:
            created = True
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
        if created:
            try:temporary.unlink()
            except FileNotFoundError:pass


def private_directory(path, *, exclusive=False):
    """Create a user-owned directory before any private child is created."""
    path = Path(path).absolute()
    if not path.parent.exists():
        private_directory(path.parent)
    if os.name == 'nt':
        from .windows_files import protect_directory
        protect_directory(path, exclusive=exclusive)
    else:
        try: path.mkdir(mode=0o700)
        except FileExistsError:
            if exclusive:raise
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(str(path), flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise OSError('private directory must be owned by this user')
            os.fchmod(fd, 0o700)
        finally: os.close(fd)
    return path
