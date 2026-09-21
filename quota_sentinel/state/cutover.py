"""Phase 3A: cutover PREPARATION — refresh a shadow JSON document from the
CURRENT authoritative legacy state, and verify semantic equality.

This is deliberately NOT migrate and shares none of its rules. Phase 2
migration is seed-if-absent ("existing JSON always wins") because a
frozen shadow is harmless while nothing reads it. Cutover preparation
exists precisely to break that freeze: the shadow may be stale, all-null
or corrupt, and the CURRENT legacy state must replace it. Reusing
migrate_provider() here would be a category error — its skip-if-exists
guarantee is the opposite of what preparation requires.

Source of truth (absolute rule):
    the ProviderState read from legacy slot files AT CALL TIME.
    A pre-existing shadow JSON is comparison material at best. It is
    never trusted, never timestamp-compared, never "resolved" against
    legacy — there is no conflict-resolution algorithm by design.

PRECONDITION (contract; NOT enforced or verified inside this module):
    the caller already holds the shell's run.lock, i.e. it is serialized
    against every authoritative scheduler writer. A lock FILE existing
    does not imply the caller OWNS it — checking is not faking, and
    pretending to verify would be worse than declaring. Mechanical proof
    of the precondition arrives with the Phase 3B wiring, where the
    caller is the lock holder.

POSTCONDITION on success:
    * legacy slot files byte-identical (this module reads them, never
      writes/deletes/repairs them);
    * <provider>-state.json decodes to EXACTLY the legacy ProviderState
      (semantic model equality — not byte equality — verified by
      re-loading through JsonStateStore);
    * authoritative ownership is UNCHANGED: legacy remains
      authoritative; the JSON is a semantically-current shadow. Phase 3A
      "prepared" is NOT "authoritative"; the reader/writer switch is a
      Phase 3B protocol decision (see ARCHITECTURE.md);
    * idempotent: preparing twice against unchanged legacy writes nothing
      the second time.

Failure semantics: any domain/schema/serialization failure happens
before any filesystem mutation; a filesystem failure leaves the shadow
document old-complete-or-new-complete (single atomic replace), and in
every failure case legacy is untouched and stays authoritative —
rollback from Phase 3A failure is literally "do nothing".
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence

from .json_store import JsonStateStore
from .models import ProviderState
from .schema import (
    DocumentCorruptError,
    MissingStateDocumentError,
    SchemaError,
    serialize_state,
)
from .store import FileStateStore, StateStoreError, _publish_atomic
from .migration import DEFAULT_PROVIDERS


@dataclass(frozen=True)
class CutoverPreparation:
    """Outcome of one preparation run: the states compared, and whether a
    replacement was performed."""

    provider: str
    legacy_state: ProviderState
    json_state: ProviderState
    changed: bool


def _read_shadow_if_usable(
    json_store: JsonStateStore, provider: str
) -> Optional[ProviderState]:
    """Best-effort read of the EXISTING shadow for comparison only.

    Missing / corrupt / schema-invalid shadow documents read as None —
    preparation then rebuilds from legacy (the shadow has no standing to
    block a source-of-truth refresh, §38). Other store errors (e.g. raw
    IO) are NOT swallowed: they are real environment failures.
    """
    try:
        return json_store.load(provider)
    except (MissingStateDocumentError, DocumentCorruptError, SchemaError):
        return None


def prepare_provider_cutover(state_dir: Path, provider: str) -> CutoverPreparation:
    """Refresh <provider>-state.json from CURRENT legacy state and verify.

    See module docstring for the full contract (source of truth,
    run.lock PRECONDITION, postconditions, failure semantics).
    """
    file_store = FileStateStore(state_dir)
    json_store = JsonStateStore(state_dir)

    # 1. Source of truth: CURRENT authoritative state, pure read.
    legacy_state = file_store.load(provider)

    # 2. Full preflight to FINAL bytes (validate_state -> document ->
    #    strict UTF-8). Zero filesystem contact yet.
    payload = serialize_state(legacy_state, provider)

    # 3. Compare against the existing shadow semantically, if usable.
    shadow_state = _read_shadow_if_usable(json_store, provider)
    path = json_store.document_path(provider)
    if shadow_state is not None and shadow_state == legacy_state:
        # Idempotent no-op: nothing written, no directory creation churn.
        return CutoverPreparation(provider, legacy_state, shadow_state, False)

    # 4. Replace the stale/absent/corrupt shadow: write-side directory
    #    ownership, then ONE atomic whole-document publish.
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        os.chmod(state_dir, 0o700)
    except OSError as exc:
        raise StateStoreError(
            f"{provider}: cutover preparation cannot prepare state dir: {exc}"
        ) from exc
    try:
        _publish_atomic(path, payload)
    except OSError as exc:
        raise StateStoreError(
            f"{provider}: cutover preparation failed to publish state "
            f"document {path}: {exc} (legacy untouched, still authoritative)"
        ) from exc

    # 5. Verify by SEMANTIC equality: reload the just-published document
    #    through the normal loud loader and compare domain models — byte
    #    tricks (reserializing and memcmp) would test the encoder, not
    #    what cutover actually needs to know.
    verified = json_store.load(provider)
    if verified != legacy_state:
        raise StateStoreError(
            f"{provider}: cutover verification failed: shadow document "
            "decodes differently from authoritative legacy state "
            "(legacy remains authoritative; investigate before retrying)"
        )
    return CutoverPreparation(provider, legacy_state, verified, True)


def prepare_all_cutover(
    state_dir: Path, providers: Optional[Sequence[str]] = None
) -> Dict[str, CutoverPreparation]:
    roster: Sequence[str] = providers if providers else DEFAULT_PROVIDERS
    return {
        provider: prepare_provider_cutover(Path(state_dir), provider)
        for provider in roster
    }
