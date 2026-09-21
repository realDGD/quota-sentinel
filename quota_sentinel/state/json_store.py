"""Whole-document provider state store over versioned JSON (Phase 2).

One ``ProviderState`` is persisted as exactly one document
(``<provider>-state.json``, schema per ``schema.py``) and one business
transition is exactly one atomic document replacement — the transaction
granularity FileStateStore's per-slot publishes could never provide.

Phase 2 ownership (see ARCHITECTURE.md): the shell's per-slot files are
still the AUTHORITATIVE runtime state. JSON documents created here are
shadow snapshots until writer ownership moves in a later phase; they are
NOT re-synced on every shell write.

Contract, deliberately parallel to FileStateStore:

* ``load`` is pure: creates nothing, chmods nothing, migrates nothing,
  repairs nothing. Absent document -> MissingStateDocumentError (absence
  is bootstrap state, never business defaults). Corrupt bytes ->
  DocumentCorruptError. Schema violation -> SchemaError. A whole
  authoritative document failing to parse is LOUD by policy: an
  all-unset default over a corrupted provider schedule would be the
  exact opposite of what this scheduler owes the user.

* ``commit`` is plan-then-execute over the WHOLE document: defensive
  stale detection (NOT a CAS — production writers must serialize via
  the shell's run.lock), full validation + serialization to FINAL BYTES
  before the first filesystem mutation (the stale check is itself a
  pure read), then a single temp-write/fsync/atomic-replace publish.
  Business-value failures therefore cannot leave a half-mutated
  document; the only mid-write crash window resolves to
  old-complete-or-new-complete.

* ``state_dir`` write-side ownership matches FileStateStore: the store
  creates/normalizes the directory to 0700 before its first publish and
  writes documents 0600. Directory creation is migration's/bootstrap's
  business, never load's.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from .models import ProviderState
from .schema import (
    MissingStateDocumentError,
    deserialize_state,
    parse_document_bytes,
    serialize_state,
)
from .store import (
    PROVIDER_NAME_RE,
    ProviderStateStore,
    StaleStateError,
    StateStoreError,
    _publish_atomic,
)

DOCUMENT_SUFFIX = "state.json"


def document_filename(provider: str) -> str:
    return f"{provider}-{DOCUMENT_SUFFIX}"


class JsonStateStore(ProviderStateStore):
    """ProviderStateStore over one versioned JSON document per provider."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        # Test-only seam, called once after each successful whole-document
        # publish; production leaves it None.
        self.on_document_committed: Optional[Callable[[str], None]] = None

    # ---- paths ---------------------------------------------------------
    def document_path(self, provider: str) -> Path:
        # Same provider-name guard as FileStateStore (path traversal block).
        if not provider or not PROVIDER_NAME_RE.fullmatch(provider):
            raise ValueError(f"invalid provider name: {provider!r}")
        return self.state_dir / document_filename(provider)

    # ---- reads (pure) ---------------------------------------------------
    def _read_payload(self, provider: str) -> Optional[bytes]:
        try:
            with open(self.document_path(provider), "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            return None
        except IsADirectoryError as exc:
            raise StateStoreError(
                f"{provider}: state document path is a directory: {exc}"
            ) from exc
        except OSError as exc:
            raise StateStoreError(
                f"{provider}: state document unreadable: {exc}"
            ) from exc

    def exists(self, provider: str) -> bool:
        """Pure existence probe for bootstrap/migration layers."""
        return self.document_path(provider).exists()

    def load(self, provider: str) -> ProviderState:
        raw = self._read_payload(provider)
        if raw is None:
            raise MissingStateDocumentError(
                f"{provider}: no state document at "
                f"{self.document_path(provider)}; the migration bootstrap "
                "must run before this backend serves state (absence is not "
                "business state and never defaults to unset)"
            )
        return deserialize_state(raw, provider)

    # ---- writes (whole-document, preflighted) ---------------------------
    def commit(
        self,
        provider: str,
        old_state: ProviderState,
        new_state: ProviderState,
    ) -> None:
        # Phase 1: defensive stale detection only — NOT concurrency.
        current = self.load(provider)  # loud on missing/corrupt
        if current != old_state:
            raise StaleStateError(
                f"{provider}: state document changed since load; commit "
                "refused (detection is defensive; serialization belongs "
                "to run.lock, not this store)"
            )

        # Phase 2: full validation + final bytes, still zero filesystem
        # operations.
        if old_state == new_state:
            return  # no-op: no write, no mkdir, no chmod
        payload = serialize_state(new_state, provider)

        # Phase 3: the single atomic whole-document publish.
        self._ensure_state_dir()
        path = self.document_path(provider)
        try:
            _publish_atomic(path, payload)
        except OSError as exc:
            raise StateStoreError(
                f"{provider}: failed to publish state document {path}: {exc}"
            ) from exc
        if self.on_document_committed is not None:
            self.on_document_committed(provider)

    def _ensure_state_dir(self) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.state_dir, 0o700)
        except OSError as exc:
            raise StateStoreError(
                f"cannot prepare state dir {self.state_dir}: {exc}"
            ) from exc
