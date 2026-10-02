"""Versioned coordination protocols, separate from backend authority."""
from dataclasses import dataclass,field
import json
import math
import os
from pathlib import Path
import platform
import stat
import threading
import time
from .files import private_open,publish_private
from .process import run_bounded

PROTOCOL_FILENAME='lock-protocol.json'
SHLOCK_BIN='/usr/bin/shlock'
PROTOCOLS=('macos-shlock-v1','posix-fd-v1','windows-range-v1')
class LockError(RuntimeError):pass
class LockBusy(LockError):pass
class LockProtocolError(LockError):pass
class LockUnavailable(LockError):pass

def expected_protocol(system=None):
    system=platform.system() if system is None else system
    try:return {'Darwin':PROTOCOLS[0],'Linux':PROTOCOLS[1],'Windows':PROTOCOLS[2]}[system]
    except KeyError as exc:raise LockProtocolError('unsupported lock platform') from exc

def initialize_protocol(directory,*,system=None):
    directory=Path(directory);protocol=expected_protocol(system)
    path=directory/PROTOCOL_FILENAME
    if path.exists():
        return state_protocol(directory,system=system)
    publish_private(path,json.dumps({'schema_version':1,'protocol':protocol}).encode()+b'\n')
    return protocol

def state_protocol(directory,*,system=None,allow_empty_init=False):
    directory=Path(directory);expected=expected_protocol(system)
    path=directory/PROTOCOL_FILENAME
    try:
        with path.open('rb') as stream:raw=stream.read(4097)
    except FileNotFoundError:
        if expected=='macos-shlock-v1':return expected
        if allow_empty_init and (not directory.exists() or not any(directory.iterdir())):
            directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            return initialize_protocol(directory,system=system)
        raise LockProtocolError('lock protocol is missing; provision a new native state directory instead of guessing an old protocol')
    except OSError as exc:raise LockProtocolError('lock protocol cannot be read') from exc
    try:
        document=json.loads(raw)
        if len(raw)>4096 or not isinstance(document,dict) or set(document)!= {'schema_version','protocol'} or type(document['schema_version']) is not int or document['schema_version']!=1 or document['protocol'] not in PROTOCOLS:
            raise ValueError()
    except (ValueError,TypeError):raise LockProtocolError('lock protocol metadata is invalid') from None
    if document['protocol']!=expected:raise LockProtocolError('lock protocol does not match this operating system; refusing shared state access')
    return expected

@dataclass
class HeldLock:
    path: Path
    protocol: str
    pid: int
    handle: object=None
    identity: object=None
    released: bool=False
    _guard: object=field(default_factory=threading.Lock,repr=False)

    def release(self):
        with self._guard:
            if self.released:return
            self.released=True
            if self.protocol=='macos-shlock-v1':
                try:
                    with private_open(self.path,'rb') as stream:
                        info=os.fstat(stream.fileno())
                        if (info.st_dev,info.st_ino)!=self.identity or stream.read(64).strip()!=str(self.pid).encode():return
                    self.path.unlink()
                except FileNotFoundError:pass
                return
            try:
                if self.protocol=='posix-fd-v1':
                    import fcntl
                    fcntl.flock(self.handle.fileno(),fcntl.LOCK_UN)
                else:
                    import msvcrt
                    self.handle.seek(0);msvcrt.locking(self.handle.fileno(),msvcrt.LK_UNLCK,1)
            finally:self.handle.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.release()

def acquire_lock(path,*,timeout,protocol,poll=.05,on_wait=None):
    if protocol not in PROTOCOLS:raise LockProtocolError('unsupported lock protocol')
    if not math.isfinite(timeout) or timeout<0 or not math.isfinite(poll) or poll<=0:raise ValueError('invalid lock budget')
    if protocol=='windows-range-v1' and os.name!='nt' or protocol!='windows-range-v1' and os.name=='nt':raise LockUnavailable('lock protocol is unavailable on this OS')
    if protocol=='macos-shlock-v1' and not os.access(SHLOCK_BIN,os.X_OK):raise LockUnavailable('the compatible macOS shlock utility is unavailable')
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    deadline=time.monotonic()+timeout;first_wait=True;handle=None
    if protocol!='macos-shlock-v1':handle=private_open(path,'ab')
    try:
        while True:
            if protocol=='macos-shlock-v1':
                result=run_bounded([SHLOCK_BIN,'-p',str(os.getpid()),'-f',str(path)],cwd=path.parent,environment=os.environ,
                    timeout=min(5,max(.01,deadline-time.monotonic())) if timeout else 5,kill_grace=0,max_bytes=1024)
                if result.timed_out:raise LockBusy('lock acquisition reached its deadline')
                acquired=result.returncode==0
            else:
                try:
                    if protocol=='posix-fd-v1':
                        import fcntl
                        fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                    else:
                        import msvcrt
                        handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                    acquired=True
                except (BlockingIOError,PermissionError):acquired=False
                except OSError as exc:
                    if exc.errno in (11,13,36):acquired=False
                    else:raise
            if acquired:
                info=path.stat();identity=(info.st_dev,info.st_ino)
                if protocol=='macos-shlock-v1':os.chmod(path,0o600)
                held=HeldLock(path,protocol,os.getpid(),handle,identity);handle=None
                return held
            if time.monotonic()>=deadline:raise LockBusy('lock is held by another process')
            if first_wait and on_wait is not None:on_wait();first_wait=False
            time.sleep(min(poll,max(0,deadline-time.monotonic())))
    finally:
        if handle is not None:handle.close()
