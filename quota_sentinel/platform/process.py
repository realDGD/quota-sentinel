"""Finite, shell-free external calls with owned execution trees and pipes."""
from dataclasses import dataclass
import math
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Protocol

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


def run_bounded(argv, *, cwd, environment, input_data=None, timeout, kill_grace, max_bytes=1048576):
    if not math.isfinite(timeout) or timeout<=0 or not math.isfinite(kill_grace) or kill_grace<0 or type(max_bytes) is not int or max_bytes<1:
        raise ValueError('invalid process budget')
    start=time.monotonic()
    process=spawn_owned(argv,cwd=cwd,environment=environment,
        stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE)
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
