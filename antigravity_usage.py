"""Compatibility entry for the installed helper implementation."""
import sys
from quota_sentinel.helpers import antigravity_usage as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    sys.modules[__name__] = _implementation
