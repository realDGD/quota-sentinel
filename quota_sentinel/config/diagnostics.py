"""Static prerequisite checks never refresh credentials or run a client."""
import os,shutil
from pathlib import Path
from .types import ConfigurationError

def dependency_problems(c):
 problems=[];clients=set()
 for p,v in c.providers.items():
  if not v.enabled:continue
  if v.opening_enabled:
   clients.update('curl' if ch=='direct' else ch for ch in v.opening_chain)
   if 'pi' in v.opening_chain and p in ('antigravity','clinepass'):
    from quota_sentinel.runtime.models import ModelRunnerConfig
    from quota_sentinel.runtime.pi_plugins import plugin_entry,plugin_guidance
    from quota_sentinel.runtime.selected_factory import runtime_environment
    cfg=ModelRunnerConfig.from_env(runtime_environment(c,{}));entry=plugin_entry(p,cfg)
    if entry is None or not entry.is_file():problems.extend(plugin_guidance(p))
  if c.features.quota_queries or c.features.automatic_opening and v.opening_enabled:
   for tier in v.quota_chain:
    if tier=='native':clients.add({'codex':'codex','antigravity':'agy','opencode':'curl','clinepass':'curl'}[p])
    if tier=='codexbar-live':clients.add('codexbar')
    if tier=='pi-live':
     clients.add('node')
     from quota_sentinel.runtime.pi_live import selected_sdk_root,selected_plugin_root
     sdk=selected_sdk_root(c)
     if sdk is None or not (sdk/'dist/core/model-runtime.js').is_file():problems.append('Selected Pi SDK unavailable; set clients.pi_sdk to the installed @earendil-works/pi-coding-agent package')
     if p=='antigravity':
      plugin=selected_plugin_root(c)
      if plugin is None or not (plugin/'src/usage/usage.ts').is_file():problems.append('Install Pi live quota plugin: pi install npm:pi-antigravity')
 for name in sorted(clients):
  from quota_sentinel.platform.paths import resolve_launcher
  try:resolve_launcher(name,explicit=c.clients.get(name))
  except ValueError:problems.append('Install selected client: '+name)
 if c.features.feishu_listener:
  import importlib.util
  if importlib.util.find_spec('lark_oapi') is None:problems.append('Install selected extra: quota-sentinel[feishu]')
 import sys
 if sys.platform=='linux':
  import importlib.util
  from quota_sentinel.install import installation_extras
  for extra,module in (('secret-service','secretstorage'),('kwallet','dbus')):
   if extra in installation_extras(c) and importlib.util.find_spec(module) is None:problems.append('Install selected extra: quota-sentinel['+extra+']')
 return problems
