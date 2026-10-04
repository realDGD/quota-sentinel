"""Explicit provisioning is allowed only in an exclusively created directory."""
from pathlib import Path
from quota_sentinel.config import save_config,ConfigurationError
from .authority import BackendAuthority,write_authority

def initialize_new_installation(state_dir,config):
 d=Path(state_dir)
 from quota_sentinel.platform.files import private_directory
 try:private_directory(d,exclusive=True)
 except FileExistsError as e:raise ConfigurationError('new installation requires a nonexistent state directory') from e
 from quota_sentinel.platform.locks import initialize_protocol
 initialize_protocol(d)
 # Publish authority last. Any earlier failure leaves a visibly incomplete install.
 save_config(d/'config.json',config,expected_revision=None)
 from .migration import migrate_all
 migrate_all(d)
 authority=BackendAuthority('json',0);write_authority(d,authority)
 return authority
