"""Authoritative backend routing (Phase 3B).

One place decides "authority → selected backend"; production code never
branches on the backend itself. Everything that reads or writes scheduler
state goes through :class:`AuthoritativeStateStore`.

Two different consistency problems, two different answers:

WRITERS (``commit``) hold the shell's ``run.lock``. That is the documented
precondition for every authoritative mutation, and the cutover flips the
authority fact while holding the same lock. A writer therefore cannot be
interleaved with a flip; a single authority read would be enough. The
router still re-reads afterwards and raises
:class:`ConcurrentAuthorityChangeError` if the fact moved, because a
precondition violation must be loud rather than silently landing a write
in a backend that is no longer authoritative.

READERS (``load``) mostly do NOT hold the lock: ``status``, ``wait``'s
deadline scan, the listener and one-shot diagnostics all read state
without serialising. For them "read authority → authority flips → read
old backend" is a real, reachable interleaving that would return a stale
value from a backend that has just been retired. The router closes it
with a generation guard: read the fact, read the state, read the fact
again, and accept the value only when both reads agree. On disagreement
it retries with a freshly selected backend (``max_read_attempts``) and
then fails loudly — it never silently returns the older backend's value.

No caching layer exists on purpose. A long-lived process (the precision
timer loops for days; the listener runs for weeks) must not pin a
backend selection at start-up, so every operation re-reads the durable
fact. The cost is one small file read per operation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

from .authority import (
    BACKEND_JSON,
    BACKEND_LEGACY,
    AUTHORITY_FILENAME,
    AuthorityError,
    BackendAuthority,
    ConcurrentAuthorityChangeError,
    read_authority,
)
from .json_store import JsonStateStore
from .models import ProviderState
from .store import FileStateStore, ProviderStateStore

DEFAULT_MAX_ATTEMPTS = 5


class AuthoritativeStateStore(ProviderStateStore):
    """ProviderStateStore that follows the durable authority fact."""

    def __init__(
        self,
        state_dir: Path,
        *,
        max_read_attempts: int = DEFAULT_MAX_ATTEMPTS,
        on_before_authority_read: Optional[Callable[[], None]] = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.max_read_attempts = max_read_attempts
        # Test-only seam, invoked immediately BEFORE each durable authority
        # read. Flipping the authority from this hook reproduces the exact
        # interleaving the generation guard exists for: "authority read →
        # authority changes → state read from the retired backend".
        self.on_before_authority_read = on_before_authority_read

    # ---- selection ------------------------------------------------------
    def authority(self) -> BackendAuthority:
        if self.on_before_authority_read is not None:
            self.on_before_authority_read()
        return read_authority(self.state_dir)

    def store_for(self, authority: BackendAuthority) -> ProviderStateStore:
        """The one authority → backend mapping in the codebase."""
        if authority.is_json:
            return JsonStateStore(self.state_dir)
        return FileStateStore(self.state_dir)

    def selected_store(self) -> ProviderStateStore:
        return self.store_for(self.authority())

    # ---- reads ----------------------------------------------------------
    def load(self, provider: str) -> ProviderState:
        seen = []
        for _ in range(self.max_read_attempts):
            before = self.authority()
            state = self.store_for(before).load(provider)
            after = self.authority()
            if before == after:
                return state
            seen.append((before, after))
        raise ConcurrentAuthorityChangeError(
            f"{provider}: backend authority changed on every one of "
            f"{self.max_read_attempts} read attempts "
            f"({_describe(seen)}); refusing to return a value that may come "
            "from a retired backend"
        )

    def load_all(
        self, providers: Sequence[str]
    ) -> Dict[str, ProviderState]:
        """Read a whole roster through the router.

        One document read per provider, but a single authority epoch for
        the set: the roster is re-read as a unit when the fact moves, so a
        caller never mixes providers from two different backends.
        """
        seen = []
        for _ in range(self.max_read_attempts):
            before = self.authority()
            store = self.store_for(before)
            states = {provider: store.load(provider) for provider in providers}
            after = self.authority()
            if before == after:
                return states
            seen.append((before, after))
        raise ConcurrentAuthorityChangeError(
            f"backend authority changed on every one of "
            f"{self.max_read_attempts} roster read attempts "
            f"({_describe(seen)}); refusing to return a mixed-backend roster"
        )

    # ---- writes ---------------------------------------------------------
    def commit(
        self,
        provider: str,
        old_state: ProviderState,
        new_state: ProviderState,
    ) -> BackendAuthority:
        before = self.authority()
        self.store_for(before).commit(provider, old_state, new_state)
        after = self.authority()
        if before != after:
            raise ConcurrentAuthorityChangeError(
                f"{provider}: authority moved from {before} to {after} during "
                "a commit; the write may have landed in a backend that is no "
                "longer authoritative. Every authoritative writer must hold "
                "run.lock, which cutover also holds — investigate before "
                "retrying."
            )
        return before


def _describe(pairs) -> str:
    return ", ".join(f"{before}→{after}" for before, after in pairs) or "none"


__all__ = [
    "AuthoritativeStateStore",
    "AuthorityError",
    "AUTHORITY_FILENAME",
    "BACKEND_JSON",
    "BACKEND_LEGACY",
]
