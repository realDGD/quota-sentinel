"""Explicit supported plugin entries and metadata-only provider preflight."""
import json,os,shutil,tempfile,subprocess,re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
@dataclass(frozen=True)
class PluginRequirement:
 package: str
 install_source: str
 pi_provider: str
@dataclass(frozen=True)
class PluginCheck:
 available: bool
 entry: Optional[Path]
 reason: str
_REQUIREMENTS={'antigravity':PluginRequirement('pi-antigravity','npm:pi-antigravity','antigravity'),'clinepass':PluginRequirement('pi-clinepass-provider','npm:pi-clinepass-provider','clinepass')}
_MODELS={'antigravity':'gemini-3.7-flash','clinepass':'cline-pass/deepseek-v4.1-flash'}
def plugin_requirement(provider):return _REQUIREMENTS.get(provider)
def plugin_guidance(provider):
 r=plugin_requirement(provider)
 return () if r is None else ('Install '+r.package+': pi install '+r.install_source,'Sign in to '+provider+' in Pi before using this channel.')
def plugin_entry(provider,config):
 r=plugin_requirement(provider)
 if not r:return None
 entries=config.plugin_entries or {}
 if provider in entries:return Path(entries[provider])
 if provider=='antigravity':return Path(config.antigravity_provider_extension)
 root=Path(config.auth_file).parent/'npm/node_modules'/r.package
 try:
  m=json.loads((root/'package.json').read_bytes());ex=m['pi']['extensions']
  if len(ex)!=1:return None
  entry=(root/ex[0]).resolve()
  if root.resolve() not in entry.parents:return None
  return entry
 except (OSError,ValueError,KeyError,TypeError):return None
def check_pi_plugin(provider,config):
 r=plugin_requirement(provider)
 if r is None:return PluginCheck(True,None,'builtin provider')
 entry=plugin_entry(provider,config)
 if entry is None or not entry.is_file():return PluginCheck(False,None,'missing plugin; '+'; '.join(plugin_guidance(provider)))
 root=next((p for p in entry.parents if (p/'package.json').is_file()),None)
 try:
  manifest=json.loads((root/'package.json').read_bytes())
  declared={(root/x).resolve() for x in manifest['pi']['extensions']}
  if manifest['name']!=r.package or entry.resolve() not in declared:raise ValueError()
 except (OSError,ValueError,KeyError,TypeError,AttributeError):return PluginCheck(False,None,'unsupported plugin manifest')
 from quota_sentinel.platform.paths import resolve_launcher
 from quota_sentinel.platform.process import run_bounded
 with tempfile.TemporaryDirectory(prefix='quota-pi-preflight.') as tmp:
  from quota_sentinel.platform.files import private_directory
  agent=private_directory(tmp)
  if Path(config.auth_file).is_file():
   from quota_sentinel.platform.files import publish_private
   publish_private(agent/'auth.json',Path(config.auth_file).read_bytes())
  env=dict(os.environ);env.update(config.environment or {});env.pop('PI_AGENT_DIR',None)
  env['PI_CODING_AGENT_DIR']=str(agent);env['NO_COLOR']='1';env['PI_OFFLINE']='1'
  try:
   command=[*resolve_launcher('pi',explicit=config.pi_bin),'--no-extensions','--extension',str(entry),'--no-skills','--no-context-files','--offline','--list-models',r.pi_provider]
   result=run_bounded(command,cwd=tmp,environment=env,timeout=config.plugin_timeout,kill_grace=0)
  except (OSError,ValueError):return PluginCheck(False,None,'Pi metadata preflight unavailable')
  if result.timed_out:return PluginCheck(False,None,'Pi metadata preflight timed out')
  if result.returncode:return PluginCheck(False,None,'Pi metadata preflight failed')
  text=re.sub(r'\x1b\[[0-9;]*m','',result.stdout.decode('utf-8','replace'))
  valid=any(len(fields)>=2 and fields[0]==r.pi_provider and fields[1]==_MODELS[provider] for fields in (line.split() for line in text.splitlines()))
  return PluginCheck(valid,entry if valid else None,'ready' if valid else 'provider/model unavailable; '+'; '.join(plugin_guidance(provider)))
