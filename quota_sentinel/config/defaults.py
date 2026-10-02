from .types import SoftwareConfig,FeatureSettings,ProviderSettings
PROVIDERS=('codex','antigravity','opencode','clinepass')
OPENING_CHANNELS={'codex':('codex','pi'),'antigravity':('agy','pi'),'opencode':('direct','pi'),'clinepass':('direct','pi')}
QUERY_CHANNELS={p:('native','codexbar-live','codexbar-cache','pi-snapshot') + (('pi-live',) if p!='clinepass' else ()) for p in PROVIDERS}
APP_DEFAULTS=dict(initial_attempts=3,watchdog_attempts=2,retry_interval=30,watchdog_retry_gap=780,quota_wait=20,timer_recheck=60)
BUDGET_DEFAULTS={
 'pi':dict(timeout=300,kill_grace=10,auth_timeout=30,plugin_timeout=15),
 'codex':dict(timeout=120,kill_grace=10,input_ceiling=2500,output_ceiling=50),
 'agy':dict(timeout=120,kill_grace=10,input_ceiling=1500,output_ceiling=200,transient_attempts=3,preflight=1),
 'direct':dict(timeout=120,kill_grace=10),
 'probes':dict(codexbar_timeout=20,antigravity_codexbar_timeout=35,opencode_codexbar_timeout=20,clinepass_codexbar_timeout=20,antigravity_native_timeout=20,opencode_native_timeout=15,clinepass_native_timeout=15,codexbar_kill_grace=10),
 'credentials':dict(timeout=15),'notification':dict(timeout=45),
 'pi_live':dict(timeout=30,kill_grace=10),
}
def default_provider(provider,*,enabled=False):
 return ProviderSettings(enabled,enabled,(OPENING_CHANNELS[provider][0],),('native',))
def new_user_defaults():
 return SoftwareConfig(1,'new-installation',FeatureSettings(True,True,False,False),{p:default_provider(p,enabled=p=='codex') for p in PROVIDERS},APP_DEFAULTS,BUDGET_DEFAULTS,{}, {})
