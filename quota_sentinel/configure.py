"""Small stdin/stdout configuration editor; saving and service apply are separate."""
import json
from .config import to_document,parse_config,ConfigurationError
from .runtime.pi_plugins import plugin_guidance
from .config.defaults import OPENING_CHANNELS,QUERY_CHANNELS,PROVIDERS,default_provider
from dataclasses import asdict
class _Cancel(Exception):pass

def configure(config,*,input_fn=input,output_fn=print):
 d=to_document(config.settings)
 for p in PROVIDERS:d['providers'].setdefault(p,asdict(default_provider(p)))
 def ask(prompt):
  value=input_fn(prompt).strip()
  if value.lower() in ('cancel','quit','q'):raise _Cancel()
  return value
 def flag(prompt,current):
  value=ask(prompt+' ['+('y' if current else 'n')+'] (Enter keeps): ').lower()
  if not value:return current
  if value not in ('y','yes','n','no'):raise ConfigurationError('choose yes or no')
  return value in ('y','yes')
 try:
  output_fn('Current choices ('+config.settings.origin+'). Cancel at any prompt to leave the file unchanged.')
  for k,v in d['features'].items():d['features'][k]=flag('Enable '+k,v)
  if d['features']['automatic_opening'] and not d['features']['quota_queries']:output_fn('Automatic opening still needs its internal live quota observation.')
  for p,v in d['providers'].items():
   previous=v['enabled'];v['enabled']=flag('Enable '+p,v['enabled'])
   if not v['enabled']:continue
   initial=v['opening_enabled'] if previous or config.settings.origin!='new-installation' else True
   v['opening_enabled']=flag('Open windows for '+p,initial)
   for key,allowed in (('opening_chain',OPENING_CHANNELS[p]),('quota_chain',QUERY_CHANNELS[p])):
    value=ask(p+' '+key+' ['+', '.join(v[key])+']; choices '+', '.join(allowed)+': ')
    if value:v[key]=[x.strip().lower() for x in value.split(',') if x.strip()]
   if 'pi' in v['opening_chain']:
    for line in plugin_guidance(p):output_fn(line)
   if 'pi-live' in v['quota_chain']:
    output_fn('Pi live queries need Node and the selected Pi SDK package; no model is called. Configure clients.pi_sdk if package discovery cannot find it.')
    if p=='antigravity':
     for line in plugin_guidance(p):output_fn(line)
  candidate=parse_config(d)
  output_fn('Preview:\n'+json.dumps(to_document(candidate),ensure_ascii=False,indent=2))
  output_fn('Saving does not restart a running service. Apply services explicitly after reviewing the file.')
  return candidate if flag('Save',False) else None
 except (_Cancel,EOFError,KeyboardInterrupt):return None
