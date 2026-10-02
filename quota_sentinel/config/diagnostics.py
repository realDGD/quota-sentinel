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
    if tier=='pi-live':clients.add('node')
 for name in sorted(clients):
  executable=c.clients.get(name) or shutil.which(name)
  if not executable or not (shutil.which(executable) or os.access(executable,os.X_OK)):problems.append('Install selected client: '+name)
 if c.features.feishu_listener:
  import importlib.util
  if importlib.util.find_spec('lark_oapi') is None:problems.append('Install selected extra: quota-sentinel[feishu]')
 return problems
