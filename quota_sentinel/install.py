"""Build an explicit service application from the reviewed saved profile."""
import sys
from pathlib import Path
from .platform.services import ServiceDefinition, service_environment, ServiceError
from .runtime.selection import build_runtime_plan, selected_extras
from .runtime.budgets import check_budget, listener_usage_budget
from .state import read_authority
from .platform.process import CLEANUP_ALLOWANCE_SECONDS

def service_definition(config,state_dir,config_path,*,environment=None,name='quota-sentinel.service',allow_inactive=False):
 import os
 state_dir=Path(state_dir).resolve();config_path=Path(config_path).resolve()
 plan=build_runtime_plan(config,'serve')
 if not plan.start_scheduler and not plan.start_listener and not allow_inactive:return None
 if not config_path.is_file():raise ServiceError('save and review configuration before installing a service')
 read_authority(state_dir)
 budget=check_budget(config,plan) if plan.start_scheduler else 0
 if plan.start_listener:budget=max(budget,listener_usage_budget({},config=config))
 return ServiceDefinition(name,(sys.executable,'-m','quota_sentinel','--state-dir',str(state_dir),'--config',str(config_path),'serve'),state_dir,service_environment(os.environ if environment is None else environment),max(15,budget+CLEANUP_ALLOWANCE_SECONDS))

def installation_extras(config):
 commands=['serve','run']
 if config.features.quota_queries:commands.append('usage')
 extras=set()
 for command in commands:extras.update(selected_extras(build_runtime_plan(config,command)))
 return tuple(x for x in ('feishu','secret-service','kwallet') if x in extras)
