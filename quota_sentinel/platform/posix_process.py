"""POSIX group ownership survives leader exit; track detached descendants."""
import ctypes
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import threading
import time


def _mac_pids(function_name, pid):
    library = ctypes.CDLL('/usr/lib/libproc.dylib')
    function = getattr(library, function_name)
    function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    values = (ctypes.c_int * 4096)()
    count = function(pid, values, ctypes.sizeof(values))
    return [values[i] for i in range(max(0, min(count, len(values)))) if values[i] > 0]

def _mac_info(pid):
    function=ctypes.CDLL('/usr/lib/libproc.dylib').proc_pidinfo
    function.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_uint64,ctypes.c_void_p,ctypes.c_int]
    function.restype=ctypes.c_int
    # Stable public struct proc_bsdinfo: status at offset 4, start at 120.
    buffer=ctypes.create_string_buffer(136)
    if function(pid,3,0,buffer,136)!=136:return None
    return struct.unpack_from('=II',buffer.raw,0),struct.unpack_from('=QQ',buffer.raw,120)

def _stamp(pid):
    if sys.platform=='darwin':
        info=_mac_info(pid)
        return info[1] if info is not None else None
    try:return (Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[19]
    except (OSError,IndexError):return None

def _mac_live(pid):
    info=_mac_info(pid)
    return info is not None and info[0][1]!=5 and not info[0][0]&4  # SZOMB / INEXIT

def _children(pid):
    if sys.platform == 'darwin':
        return _mac_pids('proc_listchildpids', pid)
    # All threads can fork: enumerate their children rather than just the
    # group leader's /proc/<pid>/task/<pid>/children.
    result = set()
    try:
        for task in (Path('/proc') / str(pid) / 'task').iterdir():
            try:result.update(int(x) for x in (task/'children').read_text().split())
            except (OSError, ValueError):pass
    except OSError:pass
    return result


class PosixProcess:
    def __init__(self, argv, *, cwd, environment, stdin, stdout, stderr):
        self.process = subprocess.Popen(argv, cwd=str(cwd), env=dict(environment),
            stdin=stdin, stdout=stdout, stderr=stderr, start_new_session=True, bufsize=0)
        self.pid = self.process.pid
        self.stdin, self.stdout, self.stderr = self.process.stdin, self.process.stdout, self.process.stderr
        self._groups = {self.pid}
        self._known = {self.pid:_stamp(self.pid)}
        self._group_stamps = {self.pid:self._known[self.pid]}
        self._tracking_done = threading.Event()
        self._guard = threading.RLock()
        self._closed = False
        self._tracker = threading.Thread(target=self._track, name='quota-process-tree', daemon=True)
        self._tracker.start()

    def _snapshot(self):
        pending = list(self._known)
        seen = set()
        while pending and len(seen) < 4096:
            pid = pending.pop()
            if pid in seen:continue
            seen.add(pid)
            if _stamp(pid)!=self._known[pid]:
                self._known.pop(pid,None)
                continue
            for child in _children(pid):
                if child not in seen:pending.append(child)
                evidence=getattr(self,'_trace_children',None)
                if evidence is not None and child not in self._known:
                    from quota_sentinel.diagnostics.trace import process_identity
                    identity=process_identity(child)
                    if identity['identity_known'] and identity['ppid']==pid:
                        if len(evidence)<8:
                            identity['observed_at']=time.time()
                            evidence[child]=identity
                        else:
                            self._trace_children_dropped += 1
                self._known[child]=_stamp(child)
                try:
                    group = os.getpgid(child)
                    if group > 0 and group != os.getpgrp():
                        self._groups.add(group)
                        self._group_stamps.setdefault(group,_stamp(group))
                except ProcessLookupError:pass

    def _track(self):
        while not self._tracking_done.wait(.02):
            with self._guard:
                if self._closed:break
                self._snapshot()

    def poll(self):return self.process.poll()
    def wait(self, timeout=None):return self.process.wait(timeout=timeout)

    def _signal(self, sig):
        alive = False
        for group in tuple(self._groups):
            if group <= 0 or group == os.getpgrp():continue
            current=_stamp(group)
            recorded=self._group_stamps.get(group)
            if current is not None and recorded is not None and current!=recorded:
                self._groups.discard(group);continue  # recycled PID belongs to another execution
            try:os.killpg(group, sig);alive = True
            except ProcessLookupError:pass
            except PermissionError:
                # Darwin can retain an empty process group briefly after
                # its last member is reaped and report EPERM for killpg.
                # Check native membership before treating that as gone.
                if sys.platform == 'darwin' and not any(_mac_live(pid) for pid in _mac_pids('proc_listpgrppids', group)):
                    self._groups.discard(group)
                else:raise
        return alive

    def stop(self, grace):
        with self._guard:
            if self._closed:return
            self._snapshot()
            self._signal(signal.SIGTERM)
            deadline = time.monotonic() + max(0, grace)
            while time.monotonic() < deadline:
                self.process.poll()
                if not self._signal(0):break
                self._snapshot()
                time.sleep(min(.02, max(0, deadline-time.monotonic())))
            self._signal(signal.SIGKILL)
            try:self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:pass

    def close(self):
        if self._closed:return
        self.stop(0)
        with self._guard:self._closed=True
        self._tracking_done.set();self._tracker.join(timeout=.5)
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream:
                try:stream.close()
                except OSError:pass

    def __enter__(self):return self
    def __exit__(self,*args):self.close()
