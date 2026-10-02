"""Command-specific dependency graph. Inventory remains in durable state."""
from quota_sentinel.config import RuntimePlan,ConfigurationError

def build_runtime_plan(config,command,*,requested=()):
 active=tuple(p for p,v in config.providers.items() if v.enabled)
 if len(set(requested))!=len(requested) or any(p not in active for p in requested):raise ConfigurationError('requested provider is disabled or unknown')
 if requested:active=tuple(requested)
 f=config.features
 if command=='usage' and not f.quota_queries:raise ConfigurationError('independent quota queries are disabled')
 if command=='run' and requested and any(not config.providers[p].opening_enabled for p in requested):raise ConfigurationError('opening is disabled for requested provider')
 opening=tuple(p for p in active if config.providers[p].opening_enabled) if command in ('run','check','wait','serve') else ()
 if command in ('check','wait','serve') and not f.automatic_opening:opening=()
 probes=active if command=='usage' else opening if command in ('run','check','wait','serve') else ()
 oc={p:config.providers[p].opening_chain for p in opening};qc={p:config.providers[p].quota_chain for p in probes}
 deps=set()
 for p,chain in oc.items():
  for ch in chain:
   deps.add('opening:'+ch)
   if ch=='pi' and p in ('antigravity','clinepass'):deps.add('plugin:'+p)
 for p,chain in qc.items():
  for tier in chain:deps.add('query:'+tier+':'+p)
 notify=f.feishu_push and command in ('run','check','wait','usage','serve')
 if notify:deps.add('feishu-push')
 listener=f.feishu_listener and command=='serve'
 if listener:deps.add('feishu-listener')
 used=set()
 if notify or listener:used.update(('feishu_app_id','feishu_app_secret','feishu_user_id'))
 for p,chain in oc.items():
  if 'direct' in chain:used.add(p)
 for p,chain in qc.items():
  if p in ('opencode','clinepass') and 'native' in chain:used.add(p)
 for name in used:
  ref=config.credentials.get(name)
  if ref and ref.kind=='system':
   for backend in ('secret-service','kwallet'):
    if ref.locator.startswith(backend+':'):deps.add('credential:'+backend)
 return RuntimePlan(command,active,opening,probes,oc,qc,frozenset(deps),notify,f.automatic_opening and command in ('serve','wait'),listener)
def selected_extras(plan):
 return tuple(x for x in ('feishu','secret-service','kwallet') if (x=='feishu' and plan.start_listener) or ('credential:'+x in plan.dependency_ids))
