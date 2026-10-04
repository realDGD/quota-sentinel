"""Explicit current-user service application, separate from configuration."""
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import sys
from types import MappingProxyType
from .files import private_directory, private_open, publish_private
from .process import run_bounded, CLEANUP_ALLOWANCE_SECONDS

SAFE_ENVIRONMENT=frozenset(('PATH','HOME','USERPROFILE','LOCALAPPDATA','SYSTEMROOT','WINDIR','TEMP','TMP','TMPDIR','LANG','LC_ALL','DBUS_SESSION_BUS_ADDRESS','XDG_RUNTIME_DIR','XDG_STATE_HOME','XDG_CONFIG_HOME','XDG_DATA_HOME'))
RETIRED_MACOS=('quota-sentinel','quota-sentinel.timer','quota-sentinel.feishu-listener')

class ServiceError(ValueError):pass

def service_environment(environment):
 return {k:v for k,v in environment.items() if k in SAFE_ENVIRONMENT}

@dataclass(frozen=True)
class ServiceDefinition:
 name: str
 argv: tuple
 cwd: Path
 environment: object
 stop_timeout: float
 def __post_init__(self):
  if not re.fullmatch(r'quota-sentinel\.[A-Za-z0-9_-]+',self.name):raise ValueError('service name must be quota-sentinel.<name>')
  if not self.argv or not Path(self.argv[0]).is_absolute() or any(not isinstance(v,str) or any(ord(x)<32 for x in v) for v in self.argv):raise ValueError('service requires an absolute executable and literal arguments')
  if not Path(self.cwd).is_absolute() or any(ord(x)<32 for x in str(self.cwd)):raise ValueError('service requires an absolute working directory')
  if not math.isfinite(self.stop_timeout) or self.stop_timeout<=0:raise ValueError('invalid stop timeout')
  if any(k not in SAFE_ENVIRONMENT or not isinstance(v,str) or any(ord(x)<32 for x in v) for k,v in self.environment.items()):raise ValueError('service environment may contain only approved nonsecret coordinates')
  object.__setattr__(self,'argv',tuple(self.argv));object.__setattr__(self,'cwd',Path(self.cwd));object.__setattr__(self,'environment',MappingProxyType(dict(self.environment)))

class ServiceManager:
 def __init__(self,*,system=None,home=None,runner=None,user_id=None,uid=None,environment=None):
  import platform
  self.system=system or platform.system();self.home=Path(home or Path.home())
  self.runner=runner;self.user_id=user_id;self.uid=uid if uid is not None else (os.getuid() if hasattr(os,'getuid') else None)
  self.environment=service_environment(os.environ if environment is None else environment)
  if self.system not in ('Darwin','Linux','Windows'):raise ServiceError('unsupported service platform; use foreground quota-sentinel serve')
 def path(self,d):
  if self.system=='Darwin':return self.home/'Library/LaunchAgents'/(d.name+'.plist')
  if self.system=='Linux':return Path(self.environment.get('XDG_CONFIG_HOME',str(self.home/'.config')))/'systemd/user'/(d.name+'.service')
  return Path(self.environment.get('LOCALAPPDATA',str(self.home/'AppData/Local')))/'Quota-Sentinel/services'/(d.name+'.xml')
 def manifest_path(self,d):return self.path(d).with_suffix('.launch.json')
 def installed_definition(self,name,state_dir):
  """Stop identity is independent of scheduler/configuration health."""
  import json,plistlib
  d=ServiceDefinition(name,(sys.executable,),Path(state_dir).absolute(),self.environment,300)
  source=self.manifest_path(d) if self.system=='Windows' else self.path(d)
  try:
   with private_open(source,'rb') as file:raw=file.read(65537)
   if len(raw)>65536:raise ValueError('installed service metadata too large')
   if self.system=='Darwin':timeout=plistlib.loads(raw)['ExitTimeOut']
   elif self.system=='Windows':timeout=json.loads(raw)['stop_timeout']
   else:
    lines=[line.split('=',1)[1] for line in raw.decode().splitlines() if line.startswith('TimeoutStopSec=')]
    if len(lines)!=1:raise ValueError('missing installed stop timeout')
    timeout=float(lines[0])
   if type(timeout) not in (int,float) or not math.isfinite(timeout) or timeout<=0:raise ValueError('invalid installed stop timeout')
   from dataclasses import replace
   return replace(d,stop_timeout=timeout)
  except (OSError,ValueError,KeyError,TypeError,OverflowError):return d
 def render(self,d):
  if self.system=='Darwin':
   from .launchd import render
   return render(d)
  if self.system=='Linux':
   from .systemd import render
   return render(d)
  from .windows_tasks import render
  if self.user_id is None:
   from .windows_files import current_user_sid
   self.user_id=current_user_sid()
  return render(d,self.user_id,self.manifest_path(d))
 def _run(self,argv,*,timeout=30,check=True):
  if self.runner is None:
   from .paths import resolve_launcher
   prefix=resolve_launcher(argv[0])
   result=run_bounded((*prefix,*argv[1:]),cwd=self.home,environment=self.environment,timeout=timeout,kill_grace=0,max_bytes=65536,discard_stdout=True)
  else:result=self.runner(argv,cwd=self.home,environment=self.environment,timeout=timeout,kill_grace=0,max_bytes=65536,discard_stdout=True)
  if check and (result.returncode or result.timed_out):raise ServiceError('service manager command failed; check the current-user service session')
  return result
 def _linux_available(self):
  try:result=self._run(('systemctl','--user','show','--property=Version','--value'),check=False)
  except (OSError,ValueError):result=None
  if result is None or result.returncode:raise ServiceError('systemd user manager unavailable; use foreground quota-sentinel serve')
 def _linux_unit_directory(self,path):
  # systemd/user is shared with other applications. Keep its existing modes;
  # only our unit file is private. Refuse unsafe writable/foreign directories.
  import stat
  fd=None
  try:
   path.mkdir(parents=True,mode=0o700,exist_ok=True)
   fd=os.open(str(path),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
   info=os.fstat(fd)
   if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o022:
    raise ServiceError('unsafe systemd user unit directory')
  except OSError:
   raise ServiceError('unsafe systemd user unit directory') from None
  finally:
   if fd is not None:os.close(fd)
 def install(self,d):
  if self.system=='Linux':self._linux_available()
  destination=self.path(d)
  if self.system=='Linux' and os.name!='nt':self._linux_unit_directory(destination.parent)
  else:private_directory(destination.parent)
  if self.system=='Darwin':
   private_directory(d.cwd/'logs')
   for name in ('service.out.log','service.err.log'):
    with private_open(d.cwd/'logs'/name,'ab'):pass
  if self.system=='Windows':
   import json
   if d.argv[1:3]!=('-m','quota_sentinel'):raise ServiceError('Windows service host requires the quota_sentinel module entry')
   publish_private(self.manifest_path(d),json.dumps({'arguments':d.argv[3:],'environment':dict(d.environment),'stop_timeout':d.stop_timeout},ensure_ascii=False).encode())
  publish_private(destination,self.render(d))
  if self.system=='Linux':
   self._run(('systemctl','--user','daemon-reload'));self._run(('systemctl','--user','enable',d.name+'.service'))
  elif self.system=='Windows':self._run(('schtasks.exe','/Create','/TN',d.name,'/XML',str(destination),'/F'))
  return destination
 def _mac_stop_name(self,name,timeout):
  target='gui/'+str(self.uid)+'/'+name
  status=self._run(('/bin/launchctl','print',target),check=False)
  if status.returncode==0:self._run(('/bin/launchctl','bootout',target),timeout=timeout)
 def start(self,d):
  if not self.path(d).is_file():raise ServiceError('install the service before starting it')
  if self.system=='Darwin':
   if d.name=='quota-sentinel.service':
    for name in RETIRED_MACOS:
     self._mac_stop_name(name,d.stop_timeout+CLEANUP_ALLOWANCE_SECONDS)
     self._run(('/bin/launchctl','disable','gui/'+str(self.uid)+'/'+name))
   self._mac_stop_name(d.name,d.stop_timeout+CLEANUP_ALLOWANCE_SECONDS)
   self._run(('/bin/launchctl','enable','gui/'+str(self.uid)+'/'+d.name))
   self._run(('/bin/launchctl','bootstrap','gui/'+str(self.uid),str(self.path(d))))
  elif self.system=='Linux':
   self.stop(d)
   self._run(('systemctl','--user','start',d.name+'.service'))
  else:
   self.stop(d)
   self._run(('schtasks.exe','/Run','/TN',d.name))
 def stop(self,d):
  timeout=d.stop_timeout+CLEANUP_ALLOWANCE_SECONDS
  if self.system=='Darwin':self._mac_stop_name(d.name,timeout)
  elif self.system=='Linux':self._run(('systemctl','--user','stop',d.name+'.service'),timeout=timeout)
  else:
   status=self._run(('schtasks.exe','/Query','/TN',d.name),check=False)
   if status.returncode==0:self._run(('schtasks.exe','/End','/TN',d.name),timeout=timeout)
 def remove(self,d):
  self.stop(d)
  if self.system=='Linux':self._run(('systemctl','--user','disable',d.name+'.service'))
  elif self.system=='Windows':self._run(('schtasks.exe','/Delete','/TN',d.name,'/F'))
  for path in (self.path(d),self.manifest_path(d)):
   try:path.unlink()
   except FileNotFoundError:pass
  if self.system=='Linux':self._run(('systemctl','--user','daemon-reload'))
