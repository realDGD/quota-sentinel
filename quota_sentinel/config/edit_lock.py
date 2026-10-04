"""Serialize compare-and-save across threads and legacy macOS processes."""
import threading
from contextlib import contextmanager
from pathlib import Path
from .types import ConfigurationError
_guard=threading.Lock()
_locks={}
@contextmanager
def configuration_lock(path,*,timeout=15):
 path=Path(path)
 from quota_sentinel.platform.files import private_directory
 private_directory(path.parent)
 with _guard: lock=_locks.setdefault(str(path.absolute()),threading.Lock())
 if not lock.acquire(timeout=timeout): raise ConfigurationError('configuration editor is busy')
 try:
  from quota_sentinel.platform.locks import acquire_lock,state_protocol,LockError
  try:
   protocol=state_protocol(path.parent,allow_empty_init=True)
   held=acquire_lock(path,timeout=timeout,protocol=protocol)
  except LockError as exc:raise ConfigurationError(str(exc)) from exc
  with held:yield
 finally:
  lock.release()
