"""Compatibility entry for the installed helper implementation."""
import sys
from quota_sentinel.helpers import task_orchestrator as _implementation

if __name__ != "__main__":
    sys.modules[__name__] = _implementation
