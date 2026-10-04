"""Immutable, nonsecret configuration shared by composition and installers."""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping,Tuple,Optional,FrozenSet
class ConfigurationError(ValueError):
    pass
@dataclass(frozen=True)
class CredentialReference:
    kind: str
    locator: str
    account: str = 'quota-sentinel'
@dataclass(frozen=True)
class FeatureSettings:
    automatic_opening: bool
    quota_queries: bool
    feishu_push: bool
    feishu_listener: bool
@dataclass(frozen=True)
class ProviderSettings:
    enabled: bool
    opening_enabled: bool
    opening_chain: Tuple[str,...]
    quota_chain: Tuple[str,...]
@dataclass(frozen=True)
class SoftwareConfig:
    schema_version: int
    origin: str
    features: FeatureSettings
    providers: Mapping[str,ProviderSettings]
    app: Mapping[str,int]
    budgets: Mapping[str,Mapping[str,float]]
    clients: Mapping[str,str]
    credentials: Mapping[str,CredentialReference]
    def __post_init__(self):
        for name in ('providers','app','clients','credentials'):
            object.__setattr__(self,name,MappingProxyType(dict(getattr(self,name))))
        object.__setattr__(self,'budgets',MappingProxyType({k:MappingProxyType(dict(v)) for k,v in self.budgets.items()}))
@dataclass(frozen=True)
class EffectiveConfig:
    settings: SoftwareConfig
    sources: Mapping[str,str]
    revision: Optional[str]
@dataclass(frozen=True)
class RuntimePlan:
    command: str
    active_providers: Tuple[str,...]
    opening_providers: Tuple[str,...]
    probe_providers: Tuple[str,...]
    opening_chains: Mapping[str,Tuple[str,...]]
    quota_chains: Mapping[str,Tuple[str,...]]
    dependency_ids: FrozenSet[str]
    notify: bool
    start_scheduler: bool
    start_listener: bool
