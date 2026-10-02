"""Explicit provisioning is allowed only in an exclusively created directory."""
from pathlib import Path
from quota_sentinel.config import save_config,ConfigurationError
from .authority import BackendAuthority,write_authority

def initialize_new_installation(state_dir,config):
 d=Path(state_dir)
 try:d.mkdir(parents=True,exist_ok=False,mode=0o700)
 except FileExistsError as e:raise ConfigurationError('new installation requires a nonexistent state directory') from e
 # Publish authority last. Any earlier failure leaves a visibly incomplete install.
 save_config(d/'config.json',config,expected_revision=None)
 authority=BackendAuthority('json',0);write_authority(d,authority)
 return authority
