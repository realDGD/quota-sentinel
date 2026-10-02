"""Bounds from selected work; stdlib-only and independent of composition."""
from . import probe_budget as probes
from quota_sentinel.platform.process import CLEANUP_ALLOWANCE_SECONDS
STARTUP_AND_REAP=CLEANUP_ALLOWANCE_SECONDS

def _guard(c):
 from . import agy_exec
 b=c.budgets['agy']
 total=max(agy_exec._GUARD_MIN_TOTAL_SECONDS,float(b['timeout']))
 grace=max(agy_exec._GUARD_MIN_GRACE_SECONDS,min(max(0,float(b['kill_grace'])),total/2))
 deadline=max(agy_exec._GUARD_MIN_DEADLINE_SECONDS,total-grace)
 return deadline+grace+agy_exec._GUARD_REAP_MARGIN_SECONDS+STARTUP_AND_REAP

def _credential(c):return c.budgets['credentials']['timeout']+STARTUP_AND_REAP

def _prepare(c,p,ch):
 if ch=='pi':
  b=c.budgets['pi'];return (b['auth_timeout']+b['kill_grace']+STARTUP_AND_REAP if p=='codex' else 0)+(b['plugin_timeout']+STARTUP_AND_REAP if p in ('antigravity','clinepass') else 0)
 if ch=='direct':return _credential(c)
 if ch=='agy' and c.budgets['agy']['preflight']:return _guard(c)
 if ch=='codex':return 15+1+STARTUP_AND_REAP
 return 0.0

def _turn(c,p,ch):
 b=c.budgets[ch]
 # Wrapped turns pay the helper's cleanup and the parent's last-resort
 # cleanup. Direct HTTP uses one owner and its existing five-second slack.
 turn=b['timeout']+b['kill_grace']+2*STARTUP_AND_REAP
 if ch=='codex':return turn+20+STARTUP_AND_REAP
 if ch=='agy':return (int(b['transient_attempts'])+1)*turn+(_guard(c) if b['preflight'] else 0)+20+STARTUP_AND_REAP
 if ch=='direct':
  turn=b['timeout']+5+2+STARTUP_AND_REAP
  return (2 if 'pi-snapshot' in c.providers[p].quota_chain else 1)*turn+_credential(c)
 return turn

def probe_phase(c,plan):
 options=c.budgets['probes'];total=0.0
 for p in plan.probe_providers:
  for tier in plan.quota_chains[p]:
   if tier=='native':
    total+=probes._native_bound_seconds(p,options)
    if p in ('opencode','clinepass'):total+=c.budgets['credentials']['timeout']-probes.KEYCHAIN_READ_TIMEOUT_SECONDS
   elif tier=='codexbar-live':total+=probes._codexbar_bound_seconds(p,options)
   elif tier=='pi-live':total+=c.budgets['pi_live']['timeout']+c.budgets['pi_live']['kill_grace']+STARTUP_AND_REAP
   else:total+=1 # selected bounded local record reads
 return total

def _delivery(c,plan):
 if not plan.notify:return 0.0
 # One auth request and up to three send requests; 1+2 second backoffs.
 # user resolution + require_credentials + auth credentials = six store reads.
 return 4*c.budgets['notification']['timeout']+3+6*_credential(c)

def usage_budget(c,plan):
 return 1.1*(probe_phase(c,plan)+c.app['quota_wait']+10+_delivery(c,plan))

def check_budget(c,plan):
 if not plan.opening_providers:return 1.1*(probe_phase(c,plan)+10)
 preparation=sum(_prepare(c,p,plan.opening_chains[p][0]) for p in plan.opening_providers)
 attempt=max(sum(_turn(c,p,ch)+(_prepare(c,p,ch) if i else 0) for i,ch in enumerate(plan.opening_chains[p])) for p in plan.opening_providers)
 rounds=c.app['initial_attempts']+c.app['watchdog_attempts']
 gaps=max(0,c.app['initial_attempts']-1)+max(0,c.app['watchdog_attempts']-1)
 notification_preflight=6*_credential(c) if plan.notify else 0
 return 1.1*(rounds*attempt+2*preparation+gaps*c.app['retry_interval']+2*probe_phase(c,plan)+c.app['quota_wait']+10+2*_delivery(c,plan)+notification_preflight)

def listener_usage_budget(environment,*,config=None,plan=None):
 if config is None:
  from pathlib import Path
  from quota_sentinel.config import read_config
  path=environment.get('QUOTA_SENTINEL_CONFIG')
  if not path:return 480.0 # explicit legacy listener entry until service migration
  config=read_config(Path(path)).settings
 if plan is None:
  from .selection import build_runtime_plan
  plan=build_runtime_plan(config,'usage')
 from dataclasses import replace
 return usage_budget(config,replace(plan,notify=config.features.feishu_listener or plan.notify))
