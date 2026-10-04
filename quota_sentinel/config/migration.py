"""Allowlisted legacy preferences; resolution never writes or reads secrets."""
import os,json,math
from pathlib import Path
from .types import *
from .store import read_config,to_document,parse_config
from .defaults import *
APP_ENV={k:'QUOTA_SENTINEL_'+k.upper() for k in APP_DEFAULTS}
BUDGET_ENV={
 'pi':{'timeout':'MODEL_TIMEOUT','kill_grace':'MODEL_KILL_GRACE','auth_timeout':'PI_AUTH_TIMEOUT'},
 'codex':{k:'CODEX_'+k.upper() for k in BUDGET_DEFAULTS['codex']},
 'agy':{k:'AGY_'+('TRANSIENT_RETRIES' if k=='transient_attempts' else k.upper()) for k in BUDGET_DEFAULTS['agy']},
 'direct':{'timeout':'DIRECT_TIMEOUT'},
 'probes':{k:k.upper() for k in BUDGET_DEFAULTS['probes']},
}
def _overrides(c,env,sources):
 d=to_document(c)
 for k,var in APP_ENV.items():
  raw=env.get(var,'')
  if raw.strip().isdigit():d['app'][k]=int(raw);sources['app.'+k]='environment'
 for group,values in BUDGET_ENV.items():
  for k,suffix in values.items():
   raw=env.get('QUOTA_SENTINEL_'+suffix,'')
   try:value=float(raw)
   except (ValueError,TypeError):continue
   if k in ('transient_attempts','input_ceiling','output_ceiling'):
    if not raw.strip().isdigit():continue
    value=int(raw)
   if group=='agy' and k=='preflight':value=0 if raw=='0' else 1
   try:finite=math.isfinite(value)
   except OverflowError:continue
   if finite and value>=0 and (value>0 or k in ('kill_grace','codexbar_kill_grace','preflight','transient_attempts')):
    d['budgets'][group][k]=value;sources['budgets.'+group+'.'+k]='environment'
 for client in ('pi','codex','agy','codexbar','curl','node','uv'):
  value=env.get('QUOTA_SENTINEL_'+client.upper()+'_BIN')
  if value:d['clients'][client]=value;sources['clients.'+client]='environment'
 for client,var in (('pi_auth','QUOTA_SENTINEL_PI_AUTH_FILE'),('codex_home','QUOTA_SENTINEL_CODEX_HOME')):
  if env.get(var):d['clients'][client]=env[var];sources['clients.'+client]='environment'
 for pair in env.get('QUOTA_SENTINEL_TRANSPORT','').split(','):
  parts=pair.strip().split('=')
  if len(parts)!=2:continue
  p,ch=map(str.strip,parts)
  if p not in PROVIDERS or ch not in ('pi','codex','agy','direct'):continue
  if ch not in OPENING_CHANNELS[p]:raise ConfigurationError("provider %r does not run on the %r transport; %s supports: %s" % (p,ch,p,', '.join(sorted(OPENING_CHANNELS[p]))))
  if p in d['providers']:
   chain=[ch]
   if p=='codex':chain.append('codex' if ch=='pi' else 'pi')
   elif p=='antigravity' and ch=='agy':chain.append('pi')
   d['providers'][p]['opening_chain']=chain;sources['providers.'+p+'.opening_chain']='environment'
 for p in env.get('QUOTA_SENTINEL_PROBE_ONLY','').replace(' ',',').split(','):
  p=p.strip()
  if p and p not in PROVIDERS:raise ConfigurationError('unknown probe-only provider '+p)
  if p in d['providers']:d['providers'][p]['opening_enabled']=False;sources['providers.'+p+'.opening_enabled']='environment'
 return parse_config(d)
def capture_legacy_config(environment,*,installed_preferences):
 env=dict(installed_preferences);env.update(environment)
 d=to_document(new_user_defaults());d['origin']='legacy-migration'
 d['features']=dict(automatic_opening=env.get('QUOTA_SENTINEL_ORCHESTRATOR_ENABLED','1')!='0',quota_queries=True,feishu_push=True,feishu_listener=True)
 chains={'codex':['pi','codex'],'antigravity':['agy','pi'],'opencode':['direct'],'clinepass':['direct']}
 for p in PROVIDERS:d['providers'][p]=dict(enabled=True,opening_enabled=True,opening_chain=chains[p],quota_chain=['native','codexbar-live','codexbar-cache','pi-snapshot'])
 home=Path(env.get('HOME',str(Path.home())))
 d['clients']={k:'/opt/homebrew/bin/'+k for k in ('pi','codex','agy','codexbar','uv')}
 d['clients'].update(curl='/usr/bin/curl',pi_auth=str(home/'.pi/agent/auth.json'),codex_home=str(home/'.codex'),antigravity_plugin=str(home/'.pi/agent/npm/node_modules/pi-antigravity/src/index.ts'))
 for name,service in [('feishu_app_id','feishu-app-id'),('feishu_app_secret','feishu-app-secret'),('feishu_user_id','feishu-user-id'),('opencode','opencode-go-api-key'),('clinepass','clinepass-api-key')]:
  d['credentials'][name]=dict(kind='system',locator='quota-sentinel.'+service,account='quota-sentinel')
 return _overrides(parse_config(d),env,{})
def resolve_config(path,state_dir,environment,*,installed_preferences=None,command_overrides=None):
 path=Path(path);state_dir=Path(state_dir);sources={}; revision=None
 if path.exists():
  saved=read_config(path);c=saved.settings;revision=saved.revision;sources.update(saved.sources)
 else:
  evidence=bool(installed_preferences) or (state_dir/'backend-authority.json').exists() or any(state_dir.glob('*-state.json')) or any(state_dir.glob('*.slot'))
  if evidence:
   c=capture_legacy_config({},installed_preferences=installed_preferences or {});sources['configuration']='legacy migration preview'
   if installed_preferences is None:sources['service_preferences']='unresolved; confirm before background application'
  else:c=new_user_defaults();sources['configuration']='new defaults'
 c=_overrides(c,environment,sources)
 if command_overrides:
  d=to_document(c)
  for k,v in command_overrides.items():d[k]=v;sources[k]='command'
  c=parse_config(d)
 return EffectiveConfig(c,sources,revision)
