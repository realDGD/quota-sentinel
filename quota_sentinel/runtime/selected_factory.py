"""Lazy composition for resolved profiles; no unselected credential access."""
import os,shutil,json,logging
from pathlib import Path
from quota_sentinel.app import AppConfig,Application
from quota_sentinel.config import ConfigurationError
from .chains import AttemptChainRunner
class NullNotifier:
 def validate_ready(self):pass
 def task(self,*args):pass
 def busy(self,*args):pass
 def usage(self,readings,now):
  print(json.dumps({p:r.document for p,r in readings.items()},ensure_ascii=False))
def runtime_environment(settings,environment):
 from quota_sentinel.config.migration import BUDGET_ENV,APP_ENV
 env=dict(environment)
 for k,v in settings.app.items():env.setdefault(APP_ENV[k],str(v))
 for group,names in BUDGET_ENV.items():
  for key,suffix in names.items():env.setdefault('QUOTA_SENTINEL_'+suffix,str(settings.budgets[group][key]))
 for name in ('pi','codex','agy','codexbar','curl','uv','node'):
  env.setdefault('QUOTA_SENTINEL_'+name.upper()+'_BIN',settings.clients.get(name,shutil.which(name) or name))
 for key,var in (('pi_auth','QUOTA_SENTINEL_PI_AUTH_FILE'),('codex_home','CODEX_HOME')):
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
 def runner_factory(ch):
  exe=env['QUOTA_SENTINEL_'+('curl' if ch=='direct' else ch).upper()+'_BIN']
  if not (shutil.which(exe) or os.access(exe,os.X_OK)):raise ConfigurationError('missing selected client '+ch)
  log=logging.getLogger('quota_sentinel.'+ch).info
  if ch=='pi':return ModelRunner(ModelRunnerConfig.from_env(env),logger=log)
  if ch=='codex':return CodexExecRunner(CodexExecConfig.from_env(env,state_dir=state_dir),logger=log)
  if ch=='agy':return AgyExecRunner(AgyExecConfig.from_env(env,state_dir=state_dir),logger=log)
  if ch=='direct':return DirectRunner(curl_bin=options['curl_bin'],timeout=settings.budgets['direct']['timeout'],environment=env,logger=log)
  raise ConfigurationError('unsupported opening channel')
 runner=AttemptChainRunner(plan.opening_chains,runner_factory)
 notifier=NullNotifier()
 if plan.notify:
  from .feishu import FeishuClient,FeishuNotifier
  notifier=FeishuNotifier(FeishuClient(environment=env,dry_run=dry_run_flag),roster=plan.active_providers)
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
