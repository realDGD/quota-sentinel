"""Lazy composition for resolved profiles; no unselected credential access."""
import os,shutil,json,logging
from dataclasses import replace
from pathlib import Path
from quota_sentinel.app import AppConfig,Application
from quota_sentinel.config import ConfigurationError
from .chains import AttemptChainRunner
class ReadyRunner:
 def __init__(self,runner,check):self.runner=runner;self.check=check
 def __getattr__(self,name):return getattr(self.runner,name)
 def prepare(self,provider,workspace):
  paths=self.runner.prepare(provider,workspace)
  self.check(provider,paths)
  return paths
 def run(self,*args):return self.runner.run(*args)

class NullNotifier:
 def validate_ready(self):pass
 def task(self,*args):pass
 def busy(self,*args):pass
 def usage(self,readings,now):
  print(json.dumps({p:r.document for p,r in readings.items()},ensure_ascii=False))
def runtime_environment(settings,environment):
 from quota_sentinel.config.migration import BUDGET_ENV,APP_ENV
 env=dict(environment)
 env["QUOTA_SENTINEL_VERIFY_PI_PLUGINS"]="1"
 env["QUOTA_SENTINEL_PI_PLUGIN_TIMEOUT"]=str(settings.budgets["pi"]["plugin_timeout"])
 for provider in ("antigravity", "clinepass"):
  if provider+"_plugin" in settings.clients:env["QUOTA_SENTINEL_"+provider.upper()+"_PLUGIN"]=settings.clients[provider+"_plugin"]
 for k,v in settings.app.items():env.setdefault(APP_ENV[k],str(v))
 for group,names in BUDGET_ENV.items():
  for key,suffix in names.items():env.setdefault('QUOTA_SENTINEL_'+suffix,str(settings.budgets[group][key]))
 for name in ('pi','codex','agy','codexbar','curl','uv','node'):
  env.setdefault('QUOTA_SENTINEL_'+name.upper()+'_BIN',settings.clients.get(name,shutil.which(name) or name))
 for key,var in (('pi_auth','QUOTA_SENTINEL_PI_AUTH_FILE'),('codex_home','QUOTA_SENTINEL_CODEX_HOME')):
  if key in settings.clients:env.setdefault(var,settings.clients[key])
 return env
def create_selected_application(state_dir,settings,plan,*,environment,clock,sleep,dry_run_flag=None):
 from .factory import quota_probe_options
 from .quota_probe import QuotaCollector
 from .models import ModelRunner,ModelRunnerConfig
 from .codex_exec import CodexExecConfig,CodexExecRunner
 from .agy_exec import AgyExecConfig,AgyExecRunner
 from .direct import DirectRunner
 env=runtime_environment(settings,environment);options=quota_probe_options(env)
 captures=frozenset(p for p in plan.active_providers if "pi-snapshot" in settings.providers[p].quota_chain)
 def runner_factory(ch):
  exe=env['QUOTA_SENTINEL_'+('curl' if ch=='direct' else ch).upper()+'_BIN']
  if not (shutil.which(exe) or os.access(exe,os.X_OK)):raise ConfigurationError('missing selected client '+ch)
  log=logging.getLogger('quota_sentinel.'+ch).info
  if ch=='pi':
   r=ModelRunner(replace(ModelRunnerConfig.from_env(env),capture_providers=captures),logger=log)
   def pi_ready(provider,paths):
    from .factory import _auth_has_provider
    if not _auth_has_provider(r.config.auth_file,provider):raise ConfigurationError('selected Pi credential unavailable for '+provider)
   return ReadyRunner(r,pi_ready)
  if ch=='codex':
   b=settings.budgets["codex"]; cfg=CodexExecConfig.from_env(env,state_dir=state_dir)
   r=CodexExecRunner(replace(cfg,timeout=b["timeout"],kill_grace=b["kill_grace"],input_ceiling=int(b["input_ceiling"]),output_ceiling=int(b["output_ceiling"])),logger=log)
   def codex_ready(provider,paths):
    from .quota_probe import _run_bounded
    result=_run_bounded([str(r.config.codex_bin),'login','status'],15,1,environment=r._environment())
    if result.returncode:raise ConfigurationError('official Codex login unavailable; run codex login')
   return ReadyRunner(r,codex_ready)
  if ch=='agy':
   b=settings.budgets["agy"];cfg=AgyExecConfig.from_env(env,state_dir=state_dir)
   r=AgyExecRunner(replace(cfg,timeout=b["timeout"],kill_grace=b["kill_grace"],input_ceiling=int(b["input_ceiling"]),output_ceiling=int(b["output_ceiling"]),transient_retries=int(b["transient_attempts"]),preflight=bool(b["preflight"])),logger=log)
   def agy_ready(provider,paths):
    if r.config.preflight and r.agent_listed(paths) is not True:raise ConfigurationError('selected agy agent/auth metadata unavailable')
   return ReadyRunner(r,agy_ready)
  if ch=='direct':
   r=DirectRunner(curl_bin=options['curl_bin'],timeout=settings.budgets['direct']['timeout'],environment=env,logger=log,capture_providers=captures)
   def direct_ready(provider,paths):
    if not r._key_reader(provider):raise ConfigurationError('selected direct credential unavailable for '+provider)
   return ReadyRunner(r,direct_ready)
  raise ConfigurationError('unsupported opening channel')
 runner=AttemptChainRunner(plan.opening_chains,runner_factory)
 notifier=NullNotifier()
 reply_user=env.get('QUOTA_SENTINEL_REPLY_USER', '') if plan.command=='usage' and settings.features.feishu_listener else ''
 if plan.notify or reply_user:
  from .feishu import FeishuClient,FeishuNotifier
  notifier=FeishuNotifier(FeishuClient(environment=env,dry_run=dry_run_flag,total_timeout=settings.budgets["notification"]["timeout"]),roster=plan.active_providers)
  if reply_user:
   from quota_sentinel.daemon import ReplyNotifier
   notifier=ReplyNotifier(notifier.client,reply_user)
 def collector(workspace):
  return QuotaCollector(state_dir,workspace,providers=plan.probe_providers,tier_chains=plan.quota_chains,logger=logging.getLogger('quota_sentinel.quota').info,**options)
 def preflight(providers):
  # Construct only explicitly listed candidates; preparation is still deferred
  # until the application has a private workspace, before state attempts begin.
  for p in providers:
   errors=[]
   for ch in plan.opening_chains[p]:
    try:runner._runner(ch);break
    except ConfigurationError as e:errors.append(str(e))
   else:raise ConfigurationError('; '.join(errors))
  notifier.validate_ready()
 app=Application(state_dir,runner,collector,notifier,config=AppConfig(**settings.app),clock=clock,sleep=sleep,preflight=preflight,runtime_plan=plan)
 app.activation_path=Path(env.get("QUOTA_SENTINEL_CONFIG",str(Path(state_dir)/"config.json"))).with_suffix(".activations.json")
 return app
