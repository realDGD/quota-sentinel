"""Strict schema and private atomic CAS publication. Never reads credentials."""
import hashlib,json,math,os,tempfile
from pathlib import Path
from dataclasses import asdict
from .types import *
from .defaults import *
from .edit_lock import configuration_lock

def _keys(value,allowed,what,required=()):
 if not isinstance(value,dict) or set(value)-set(allowed) or set(required)-set(value): raise ConfigurationError('invalid '+what+' fields')
def _bool(v):
 if type(v) is not bool: raise ConfigurationError('feature flags must be boolean')
 return v
def _number(v,name):
 if type(v) not in (int,float) or not math.isfinite(v) or v<0 or (v==0 and name not in ('kill_grace','codexbar_kill_grace','preflight')): raise ConfigurationError('invalid budget '+name)
 return v
def parse_config(document):
 d=dict(document) if isinstance(document,dict) else document
 _keys(d,('schema_version','origin','features','providers','app','budgets','clients','credentials'),'configuration',('schema_version','features','providers'))
 if type(d['schema_version']) is not int or d['schema_version']!=1: raise ConfigurationError('unsupported configuration schema')
 f=d['features']; _keys(f,FeatureSettings.__dataclass_fields__,'features',FeatureSettings.__dataclass_fields__)
 features=FeatureSettings(**{k:_bool(v) for k,v in f.items()}); providers={}
 _keys(d['providers'],PROVIDERS,'providers')
 for p,v in d['providers'].items():
  _keys(v,ProviderSettings.__dataclass_fields__,p,ProviderSettings.__dataclass_fields__)
  enabled=_bool(v['enabled']); opening=_bool(v['opening_enabled'])
  chains=[]
  for key,allowed,needed in (('opening_chain',OPENING_CHANNELS[p],enabled and opening),('quota_chain',QUERY_CHANNELS[p],enabled and (features.quota_queries or features.automatic_opening and opening))):
   chain=v[key]
   if not isinstance(chain,(list,tuple)) or any(not isinstance(x,str) or x not in allowed for x in chain) or len(set(chain))!=len(chain) or needed and not chain: raise ConfigurationError('invalid '+p+' '+key)
   chains.append(tuple(chain))
  if enabled and opening and features.automatic_opening and not set(chains[1])&{'native','codexbar-live','pi-live'}:raise ConfigurationError('automatic opening needs a live quota source')
  providers[p]=ProviderSettings(enabled,opening,*chains)
 app=dict(APP_DEFAULTS); a=d.get('app',{}); _keys(a,app,'app')
 for k,v in a.items():
  if type(v) is not int or v<0 or (v==0 and k in ('initial_attempts','watchdog_attempts','retry_interval')): raise ConfigurationError('invalid app '+k)
  app[k]=v
 budgets={k:dict(v) for k,v in BUDGET_DEFAULTS.items()}; _keys(d.get('budgets',{}),budgets,'budgets')
 for k,v in d.get('budgets',{}).items():
  _keys(v,budgets[k],k+' budget')
  for name,value in v.items():budgets[k][name]=_number(value,name)
 clients=d.get('clients',{}); allowed=('pi','codex','agy','codexbar','curl','node','uv','pi_auth','codex_home','pi_sdk','antigravity_plugin','clinepass_plugin')
 _keys(clients,allowed,'clients')
 if any(not isinstance(v,str) or not v or '\x00' in v for v in clients.values()):raise ConfigurationError('invalid client path')
 refs={}
 for k,v in d.get('credentials',{}).items():
  _keys(v,CredentialReference.__dataclass_fields__,'credential reference',('kind','locator'))
  if v['kind'] not in ('system','environment','file') or any(not isinstance(x,str) or not x for x in v.values()):raise ConfigurationError('invalid credential reference')
  refs[k]=CredentialReference(**v)
 origin=d.get('origin','saved')
 if not isinstance(origin,str) or not origin:raise ConfigurationError('invalid origin')
 return SoftwareConfig(1,origin,features,providers,app,budgets,clients,refs)
def to_document(c):
 return dict(schema_version=c.schema_version,origin=c.origin,features=asdict(c.features),providers={p:asdict(v) for p,v in c.providers.items()},app=dict(c.app),budgets={k:dict(v) for k,v in c.budgets.items()},clients=dict(c.clients),credentials={k:asdict(v) for k,v in c.credentials.items()})
def read_config(path):
 try:raw=Path(path).read_bytes(); c=parse_config(json.loads(raw))
 except (OSError,ValueError,TypeError) as e:raise ConfigurationError('configuration unreadable or invalid') from e
 return EffectiveConfig(c,{'configuration':'saved'},hashlib.sha256(raw).hexdigest())
def save_config(path,config,*,expected_revision):
 path=Path(path); c=parse_config(to_document(config)); raw=(json.dumps(to_document(c),ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
 with configuration_lock(path.parent/'configuration.lock'):
  current=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
  if current!=expected_revision:raise ConfigurationError('configuration changed; reload before saving')
  if current is not None:
   old=read_config(path).settings
   old_active={p for p,v in old.providers.items() if v.enabled and v.opening_enabled}
   new_active={p for p,v in c.providers.items() if v.enabled and v.opening_enabled}
   disabled=old_active-new_active
   if disabled:
    journal=path.with_suffix('.activations.json')
    record=json.loads(journal.read_bytes()) if journal.exists() else {'schema_version':1,'pending':[]}
    if record.get('schema_version')!=1 or not isinstance(record.get('pending'),list):raise ConfigurationError('invalid activation journal')
    record['pending']=sorted(set(record['pending'])|disabled)
    from quota_sentinel.state.store import _publish_atomic
    _publish_atomic(journal,json.dumps(record).encode())
  fd,name=tempfile.mkstemp(dir=path.parent,prefix='config.tmp.')
  try:
   with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
   os.replace(name,path)
  finally:
   if os.path.exists(name):os.unlink(name)
 return hashlib.sha256(raw).hexdigest()
