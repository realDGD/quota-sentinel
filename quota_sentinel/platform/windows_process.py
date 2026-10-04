"""Suspended CreateProcessW + Job assignment before any child instruction.

CPython's _winapi owns STARTUPINFOEX/handle-list allocation; its native
CreateProcess wrapper returns the primary thread handle, unlike Popen.
"""
import os
from pathlib import Path
import shutil
import subprocess
import time


def start_contained(api):
    job=api.create_job();process=thread=None
    try:
        process,thread,pid=api.create_suspended()
        api.assign(job,process)
        api.resume(thread)
        api.close(thread);thread=None
        return job,process,pid
    except BaseException:
        if process is not None:
            try:api.terminate(process)
            except OSError:pass
        # Release every handle even when another cleanup operation fails;
        # preserve the launch failure, rather than masking assignment errors.
        for handle in (process,thread,job):
            if handle is not None:
                try:api.close(handle)
                except OSError:pass
        raise


class _WindowsAPI:
    def __init__(self,argv,cwd,environment,startup):
        import _winapi
        import ctypes as c
        from ctypes import wintypes as w
        self.win=_winapi;self.c=c;self.w=w
        self.kernel=c.WinDLL('kernel32',use_last_error=True)
        self.argv,self.cwd,self.environment,self.startup=argv,cwd,environment,startup
        class Basic(c.Structure):
            _fields_=[('process_time',c.c_longlong),('job_time',c.c_longlong),
                ('flags',w.DWORD),('minimum_working',c.c_size_t),('maximum_working',c.c_size_t),
                ('active_processes',w.DWORD),('affinity',c.c_size_t),
                ('priority',w.DWORD),('scheduling',w.DWORD)]
        class Counters(c.Structure):
            _fields_=[(name,c.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(c.Structure):
            _fields_=[('basic',Basic),('io',Counters),('process_memory',c.c_size_t),
                ('job_memory',c.c_size_t),('peak_process',c.c_size_t),('peak_job',c.c_size_t)]
        self.Extended=Extended
        self.kernel.CreateJobObjectW.argtypes=[c.c_void_p,w.LPCWSTR];self.kernel.CreateJobObjectW.restype=w.HANDLE
        self.kernel.SetInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD];self.kernel.SetInformationJobObject.restype=w.BOOL
        self.kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE];self.kernel.AssignProcessToJobObject.restype=w.BOOL
        self.kernel.ResumeThread.argtypes=[w.HANDLE];self.kernel.ResumeThread.restype=w.DWORD
        self.kernel.TerminateJobObject.argtypes=[w.HANDLE,w.UINT];self.kernel.TerminateJobObject.restype=w.BOOL
        self.kernel.QueryInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.c_void_p];self.kernel.QueryInformationJobObject.restype=w.BOOL
        self.kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];self.kernel.OpenProcess.restype=w.HANDLE
        self.kernel.IsProcessInJob.argtypes=[w.HANDLE,w.HANDLE,c.POINTER(w.BOOL)];self.kernel.IsProcessInJob.restype=w.BOOL

    def create_job(self):
        job=self.kernel.CreateJobObjectW(None,None)
        if not job:raise self.c.WinError(self.c.get_last_error())
        limits=self.Extended();limits.basic.flags=0x2000  # KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(job,9,self.c.byref(limits),self.c.sizeof(limits)):
            error=self.c.get_last_error();self.close(job);raise self.c.WinError(error)
        return job

    def create_suspended(self):
        executable=shutil.which(self.argv[0])
        if not executable:raise OSError('selected executable unavailable')
        # No breakaway flag. Nested job assignment either succeeds or fails
        # while the child is still suspended.
        flags=0x4 | 0x400 | 0x80000 | 0x8000000
        process,thread,pid,_=self.win.CreateProcess(executable,
            subprocess.list2cmdline(self.argv),None,None,True,flags,
            dict(self.environment),str(self.cwd),self.startup)
        return process,thread,pid

    def assign(self,job,process):
        if not self.kernel.AssignProcessToJobObject(job,process):raise self.c.WinError(self.c.get_last_error())
    def resume(self,thread):
        if self.kernel.ResumeThread(thread)==0xFFFFFFFF:raise self.c.WinError(self.c.get_last_error())
    def terminate(self,process):self.win.TerminateProcess(process,125)
    def close(self,handle):self.win.CloseHandle(handle)

    def job_process_handles(self,job,*,deadline):
        # Job accounting reaches ActiveProcesses=0 before process objects are
        # signaled. Hold synchronization handles before asynchronous terminate.
        c,w=self.c,self.w;capacity=64;handles=[]
        def check_deadline():
            if time.monotonic()>=deadline:raise subprocess.TimeoutExpired('owned job cleanup',1)
        try:
            while True:
                check_deadline()
                class ProcessIds(c.Structure):
                    _fields_=[('assigned',w.DWORD),('listed',w.DWORD),('ids',c.c_size_t*capacity)]
                ids=ProcessIds()
                if self.kernel.QueryInformationJobObject(job,3,c.byref(ids),c.sizeof(ids),None):break
                error=c.get_last_error()
                if error!=234:raise c.WinError(error)
                capacity=max(capacity*2,ids.assigned)
                if capacity>65536:raise OSError('owned job process list exceeds cleanup limit')
            check_deadline()
            for pid in ids.ids[:ids.listed]:
                check_deadline()
                handle=self.kernel.OpenProcess(0x100000|0x1000,False,pid)
                if not handle:
                    error=c.get_last_error()
                    if error==87:continue  # exited between the list and open
                    raise c.WinError(error)
                try:
                    owned=w.BOOL()
                    if not self.kernel.IsProcessInJob(handle,job,c.byref(owned)):raise c.WinError(c.get_last_error())
                    if owned.value:handles.append(handle);handle=None
                finally:
                    if handle:self.close(handle)
            return handles
        except BaseException:
            for handle in handles:self.close(handle)
            raise


class WindowsProcess:
    def __init__(self,argv,*,cwd,environment,stdin,stdout,stderr):
        import _winapi
        import msvcrt
        self._win=_winapi;self._closed=False;self._job=self._handle=None
        self.stdin=self.stdout=self.stderr=None
        child_handles=[]
        own=_winapi.GetCurrentProcess()
        def prepare(value,reading,attribute,standard):
            fd=None
            if value==subprocess.PIPE:
                read_fd,write_fd=os.pipe()
                fd=read_fd if reading else write_fd
                parent_fd=write_fd if reading else read_fd
                setattr(self,attribute,os.fdopen(parent_fd,'wb' if reading else 'rb',buffering=0))
                handle=msvcrt.get_osfhandle(fd)
            elif value==subprocess.DEVNULL:
                fd=os.open(os.devnull,os.O_RDONLY if reading else os.O_WRONLY)
                handle=msvcrt.get_osfhandle(fd)
            elif value is None:
                handle=_winapi.GetStdHandle(standard)
                if handle in (None,0,-1):
                    fd=os.open(os.devnull,os.O_RDONLY if reading else os.O_WRONLY)
                    handle=msvcrt.get_osfhandle(fd)
            else:
                handle=msvcrt.get_osfhandle(value if isinstance(value,int) else value.fileno())
            try:
                duplicated=_winapi.DuplicateHandle(own,handle,own,0,True,_winapi.DUPLICATE_SAME_ACCESS)
                child_handles.append(duplicated)
                return duplicated
            finally:
                if fd is not None:os.close(fd)
        try:
            startup=subprocess.STARTUPINFO();startup.dwFlags|=_winapi.STARTF_USESTDHANDLES
            startup.hStdInput=prepare(stdin,True,'stdin',_winapi.STD_INPUT_HANDLE)
            startup.hStdOutput=prepare(stdout,False,'stdout',_winapi.STD_OUTPUT_HANDLE)
            startup.hStdError=startup.hStdOutput if stderr==subprocess.STDOUT else prepare(stderr,False,'stderr',_winapi.STD_ERROR_HANDLE)
            startup.lpAttributeList={'handle_list':list(dict.fromkeys(child_handles))}
            self._api=_WindowsAPI(argv,cwd,environment,startup)
            self._job,self._handle,self.pid=start_contained(self._api)
            self._command=argv;self._returncode=None
        except BaseException:
            for stream in (self.stdin,self.stdout,self.stderr):
                if stream:stream.close()
            raise
        finally:
            for handle in child_handles:_winapi.CloseHandle(handle)

    def poll(self):
        if self._returncode is None:
            if self._win.WaitForSingleObject(self._handle,0)==0:
                self._returncode=self._win.GetExitCodeProcess(self._handle)
        return self._returncode

    def wait(self,timeout=None):
        if self.poll() is not None:return self._returncode
        milliseconds=self._win.INFINITE if timeout is None else max(0,int(timeout*1000))
        if self._win.WaitForSingleObject(self._handle,milliseconds)==self._win.WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self._command,timeout)
        self._returncode=self._win.GetExitCodeProcess(self._handle)
        return self._returncode

    def stop(self,grace):
        if self._closed:return
        # Console-free CLIs have no universal graceful signal. Terminating
        # the Job is the deterministic Windows tree-cleanup operation.
        handles=[];deadline=time.monotonic()+1
        try:
            if self._job:
                try:handles=self._api.job_process_handles(self._job,deadline=deadline)
                finally:
                    if not self._api.kernel.TerminateJobObject(self._job,124):
                        raise self._api.c.WinError(self._api.c.get_last_error())
            self.wait(max(0,deadline-time.monotonic()))
            for handle in handles:
                remaining=max(0,int((deadline-time.monotonic())*1000))
                result=self._win.WaitForSingleObject(handle,remaining)
                if result==self._win.WAIT_TIMEOUT:raise subprocess.TimeoutExpired(self._command,1)
                if result!=0:raise self._api.c.WinError(self._api.c.get_last_error())
        finally:
            for handle in handles:self._api.close(handle)

    def close(self):
        if self._closed:return
        try:self.stop(0)
        finally:
            self._closed=True
            if self._job:self._api.close(self._job);self._job=None
            if self._handle:self._api.close(self._handle);self._handle=None
            for stream in (self.stdin,self.stdout,self.stderr):
                if stream:
                    try:stream.close()
                    except OSError:pass
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
