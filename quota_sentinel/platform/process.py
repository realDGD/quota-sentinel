"""Finite, shell-free external calls with owned execution trees and pipes."""
from dataclasses import dataclass
import math
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Protocol

# Startup and finite stop/reap/pipe joins. All outer budgets use this same
# allowance rather than copying the cleanup timings into several formulas.
CLEANUP_ALLOWANCE_SECONDS = 7.0

@dataclass(frozen=True)
class CommandResult:
    stdout: bytes
    stderr: bytes
    returncode: int
    timed_out: bool

class OwnedProcess(Protocol):
    pid: int
    stdin: object
    stdout: object
    stderr: object
    def poll(self):...
    def wait(self, timeout=None):...
    def stop(self, grace):...
    def close(self):...

def spawn_owned(argv, *, cwd, environment, stdin=None, stdout=None, stderr=None):
    if not argv or isinstance(argv,(str,bytes)) or any(not isinstance(x,(str,os.PathLike)) or '\x00' in str(x) for x in argv):
        raise ValueError('invalid command prefix')
    command = [str(x) for x in argv]
    if os.name=='nt':
        from .windows_process import WindowsProcess
        return WindowsProcess(command,cwd=cwd,environment=environment,stdin=stdin,stdout=stdout,stderr=stderr)
    from .posix_process import PosixProcess
    return PosixProcess(command,cwd=cwd,environment=environment,stdin=stdin,stdout=stdout,stderr=stderr)


def run_bounded(argv, *, cwd, environment, input_data=None, timeout, kill_grace, max_bytes=1048576, discard_stdout=False):
    if not math.isfinite(timeout) or timeout<=0 or not math.isfinite(kill_grace) or kill_grace<0 or type(max_bytes) is not int or max_bytes<1:
        raise ValueError('invalid process budget')
    start=time.monotonic()
    process=spawn_owned(argv,cwd=cwd,environment=environment,
        stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL if discard_stdout else subprocess.PIPE,stderr=subprocess.PIPE)
    return capture_owned(process,input_data=input_data,timeout=timeout,kill_grace=kill_grace,max_bytes=max_bytes)


def capture_owned(process,*,timeout,kill_grace,max_bytes=1048576,input_data=None):
    if not math.isfinite(timeout) or timeout<=0 or not math.isfinite(kill_grace) or kill_grace<0 or type(max_bytes) is not int or max_bytes<1:
        raise ValueError('invalid process budget')
    start=time.monotonic()
    output=[bytearray(),bytearray()];overflow=threading.Event();threads=[]
    def read(stream,index):
        try:
            while True:
                data=stream.read(min(65536,max_bytes+1))
                if not data:break
                available=max_bytes-len(output[index])
                output[index].extend(data[:available])
                if len(data)>available:overflow.set();break
        except (OSError,ValueError):pass
    def write():
        try:
            view=memoryview(input_data)
            while view:
                count=process.stdin.write(view[:65536])
                if not count:break
                view=view[count:]
        except (OSError,ValueError):pass
        finally:
            try:process.stdin.close()
            except OSError:pass
    for index,stream in enumerate((process.stdout,process.stderr)):
        if stream is None:continue
        thread=threading.Thread(target=read,args=(stream,index),daemon=True);thread.start();threads.append(thread)
    if input_data is not None:
        thread=threading.Thread(target=write,daemon=True);thread.start();threads.append(thread)
    timed_out=False;limited=False;code=125
    try:
        while True:
            if overflow.is_set():limited=True;process.stop(0);break
            result=process.poll()
            if result is not None:
                code=result
                # Reap descendants even when a successful leader left its
                # stdout pipe open in another process.
                process.stop(0);break
            if time.monotonic()-start>=timeout:
                timed_out=True;process.stop(kill_grace);code=124;break
            time.sleep(.005)
        for thread in threads:thread.join(timeout=.5)
        limited=limited or overflow.is_set()
        if limited:
            return CommandResult(bytes(output[0]),b'output limit exceeded\n',125,False)
        return CommandResult(bytes(output[0]),bytes(output[1]),code,timed_out)
    finally:process.close()

class BoundedPipes:
    """Finite line reads/writes, including native Windows anonymous pipes."""
    def __init__(self,process,*,max_bytes=1048576):
        import queue
        self.process=process;self.max_bytes=max_bytes;self.lines=queue.Queue(maxsize=64)
        self.failed=threading.Event();self.ended=threading.Event();self.threads=[]
        thread=threading.Thread(target=self._read,daemon=True);thread.start();self.threads.append(thread)
    def _read(self):
        pending=bytearray();total=0
        try:
            while True:
                data=self.process.stdout.read(65536)
                if not data:break
                total+=len(data)
                if total>self.max_bytes:self.failed.set();break
                pending.extend(data)
                while b'\n' in pending:
                    line,_,rest=pending.partition(b'\n');pending=bytearray(rest)
                    try:self.lines.put_nowait(bytes(line))
                    except Exception:self.failed.set();return
        except (OSError,ValueError):pass
        finally:self.ended.set()
    def write(self,data,*,deadline):
        done=threading.Event();failed=threading.Event()
        def send():
            try:
                view=memoryview(data)
                while view:
                    count=self.process.stdin.write(view)
                    if not count:raise BrokenPipeError()
                    view=view[count:]
                self.process.stdin.flush()
            except (OSError,ValueError):failed.set()
            finally:done.set()
        thread=threading.Thread(target=send,daemon=True);thread.start();self.threads.append(thread)
        if not done.wait(max(0,deadline-time.monotonic())):raise subprocess.TimeoutExpired('pipe write',0)
        if failed.is_set():raise BrokenPipeError()
    def readline(self,*,deadline):
        import queue
        while time.monotonic()<deadline:
            if self.failed.is_set():raise ValueError('pipe output limit exceeded')
            try:return self.lines.get(timeout=min(.05,max(.001,deadline-time.monotonic())))
            except queue.Empty:
                if self.ended.is_set():raise EOFError()
        raise subprocess.TimeoutExpired('pipe read',0)
    def close(self):
        self.process.close()
        for thread in self.threads:thread.join(timeout=.5)
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
