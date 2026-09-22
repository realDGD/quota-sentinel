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

Failure semantics — the real invariant is "legacy stays untouched and
authoritative", NOT "the shadow keeps its old value". Branch by whether
the publish itself succeeded:
  * domain/schema/serialization failure: surfaces before the first
    filesystem MUTATION (pure reads of legacy + shadow happen earlier,
    by design); the shadow stays old-complete or absent;
  * filesystem failure inside the single atomic publish: the shadow
    stays old-complete (never torn);
  * VERIFICATION failure happens AFTER a successful publish: the shadow
    may hold the newly-published COMPLETE document. That is not an
    ownership change and needs no rollback — do not "fix" the old
    wording by adding shadow rollback;
  * in every branch: legacy untouched and authoritative, no torn JSON,
    the shadow never becomes authoritative, and no result is returned.
Rollback from Phase 3A failure is literally "do nothing" — ownership
never changed away from legacy.

PHASE 3B — THE DURABLE CUTOVER
------------------------------
``cutover_to_json`` is the production ownership switch. It is the only
place that flips the durable authority fact, and it is deliberately
ALL-OR-NOTHING ACROSS THE WHOLE ROSTER: one epoch covers every provider,
so there is no window in which Codex is JSON while antigravity is still
legacy. Per-provider authority would multiply the reachable mixed states
without removing the single-fact requirement; the roster is small and its
states are independent, so a roster-wide flip is both simpler and safer.

Sequence (all under the caller's run.lock, see the precondition above):

    1. read the durable authority fact
       — already json: idempotent no-op, nothing is written;
    2. read CURRENT legacy ProviderState for every provider (pure reads);
    3. refresh + verify every provider document from that current legacy
       state (one atomic publish each, semantic-equality verified);
    4. re-read every document as a set and compare against step 2;
    5. publish the authority fact ONCE — the commit point;
    6. re-read the fact and confirm.

Every crash prefix resolves mechanically because step 5 is a single
atomic document replacement and nothing before it changes ownership:

    crash in steps 1-4  -> authority still legacy; documents may be
                           refreshed shadows, which is harmless and is
                           exactly what a re-run recomputes;
    crash during step 5 -> readers see the complete old or the complete
                           new manifest; both are valid answers;
    crash in step 6     -> authority is already json and every document
                           was verified before the flip.

``rollback_to_legacy`` is the inverse switch, and it REFUSES to run when
the JSON documents have diverged from the legacy files: a rollback is
only allowed while it is a pure undo of the cutover. Once JSON has
advanced, "roll back" would mean discarding authoritative state, which is
a human decision with a human-sized backup, not an automatic one.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

from .authority import (
    BACKEND_JSON,
    BACKEND_LEGACY,
    AuthorityError,
    BackendAuthority,
    read_authority,
    write_authority,
)
from .json_store import JsonStateStore
from .models import ProviderState
from .router import AuthoritativeStateStore
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

    # 2. Full serialization preflight (validate_state -> document ->
    #    strict UTF-8). No filesystem MUTATION has occurred yet; the
    #    reads above are pure by contract.
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


# ---------------------------------------------------------------------------
# Phase 3B: durable ownership cutover
# ---------------------------------------------------------------------------

# Checkpoint names. A checkpoint callback is invoked immediately BEFORE the
# named step performs its work, so raising from one simulates a crash at
# that exact point. Tests reference these constants, never bare strings.
STEP_BEFORE_READ_LEGACY = "before-read-legacy"
STEP_AFTER_READ_LEGACY = "after-read-legacy"
STEP_BEFORE_PREPARE_JSON = "before-prepare-json"
STEP_AFTER_PREPARE_PROVIDER = "after-prepare-json:"   # + provider name
STEP_AFTER_PREPARE_JSON = "after-prepare-json"
STEP_BEFORE_VERIFY_JSON = "before-verify-json"
STEP_AFTER_VERIFY_JSON = "after-verify-json"
STEP_BEFORE_FLIP = "before-flip-authority"
STEP_AFTER_FLIP = "after-flip-authority"
STEP_AFTER_CONFIRM = "after-confirm-authority"


class CutoverVerificationError(StateStoreError):
    """A document did not decode to the legacy state it was built from."""


class RollbackRefusedError(StateStoreError):
    """Rollback was requested while it would not be a pure undo."""


@dataclass(frozen=True)
class BackendSwitch:
    """Outcome of one ownership switch."""

    previous: BackendAuthority
    current: BackendAuthority
    states: Dict[str, ProviderState]
    changed: bool                   # False for an idempotent no-op re-run
    prepared: Dict[str, bool]       # provider -> a document was (re)written


Checkpoint = Optional[Callable[[str], None]]


def _checkpoint(checkpoint: Checkpoint, name: str) -> None:
    if checkpoint is not None:
        checkpoint(name)


def _ensure_state_dir(state_dir: Path, provider: str) -> None:
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        os.chmod(state_dir, 0o700)
    except OSError as exc:
        raise StateStoreError(
            f"{provider}: cutover cannot prepare state dir: {exc}"
        ) from exc


def cutover_to_json(
    state_dir: Path,
    *,
    checkpoint: Checkpoint = None,
) -> BackendSwitch:
    """Make the JSON backend authoritative for the WHOLE roster.

    THERE IS NO PROVIDER SUBSET. The authority manifest is ONE global
    fact, so a switch that prepared a subset of providers and then flipped
    it would make the unprepared providers' authoritative state
    unreachable while the manifest claimed all of them were JSON. The
    roster is therefore not a parameter: it is the deployment's roster,
    every time. Fixtures that only populate one provider simply leave the
    others all-unset; they do not get a narrower switch.

    PRECONDITION (contract, not verified here — see the module docstring):
    the caller holds the shell's run.lock. Every authoritative writer in
    the system is serialised by that lock, so the legacy state read in
    step 2 cannot change underneath the documents built from it. The
    public CLI verbs acquire it themselves; the shell already holds it.

    Idempotent: calling it when JSON is already authoritative returns the
    current authority with ``changed=False`` and writes nothing.
    """
    state_dir = Path(state_dir)
    roster: Sequence[str] = DEFAULT_PROVIDERS

    # 1. Current durable fact.
    previous = read_authority(state_dir)
    if previous.is_json:
        return BackendSwitch(
            previous, previous,
            AuthoritativeStateStore(state_dir).load_all(roster),
            changed=False,
            prepared={provider: False for provider in roster},
        )

    # 2. Source of truth: CURRENT legacy state, one pure read per provider.
    _checkpoint(checkpoint, STEP_BEFORE_READ_LEGACY)
    legacy_states = {
        provider: FileStateStore(state_dir).load(provider)
        for provider in roster
    }
    _checkpoint(checkpoint, STEP_AFTER_READ_LEGACY)

    # 3. Refresh every document from that state (atomic publish each).
    _checkpoint(checkpoint, STEP_BEFORE_PREPARE_JSON)
    prepared: Dict[str, bool] = {}
    for provider in roster:
        preparation = prepare_provider_cutover(state_dir, provider)
        prepared[provider] = preparation.changed
        _checkpoint(checkpoint, STEP_AFTER_PREPARE_PROVIDER + provider)
    _checkpoint(checkpoint, STEP_AFTER_PREPARE_JSON)

    # 4. Whole-roster verification: every document must decode to the
    #    legacy state it was built from, checked as a set so a partial
    #    write cannot sneak through per-provider ordering.
    _checkpoint(checkpoint, STEP_BEFORE_VERIFY_JSON)
    json_store = JsonStateStore(state_dir)
    for provider in roster:
        decoded = json_store.load(provider)
        if decoded != legacy_states[provider]:
            raise CutoverVerificationError(
                f"{provider}: state document decodes differently from the "
                "legacy state it was built from; authority NOT switched "
                "(legacy remains authoritative)"
            )
    _checkpoint(checkpoint, STEP_AFTER_VERIFY_JSON)

    # 5. The commit point: ONE atomic replacement of the ownership fact.
    _checkpoint(checkpoint, STEP_BEFORE_FLIP)
    write_authority(state_dir, previous.successor(BACKEND_JSON))
    _checkpoint(checkpoint, STEP_AFTER_FLIP)

    # 6. Confirm the durable fact (never trust the in-process value).
    confirmed = read_authority(state_dir)
    if not confirmed.is_json:
        raise StateStoreError(
            f"cutover published the authority fact but re-reading it yielded "
            f"{confirmed}; refusing to report success"
        )
    _checkpoint(checkpoint, STEP_AFTER_CONFIRM)
    return BackendSwitch(
        previous, confirmed, legacy_states, changed=True, prepared=prepared
    )


def rollback_to_legacy(
    state_dir: Path,
    *,
    checkpoint: Checkpoint = None,
) -> BackendSwitch:
    """Return ownership to the legacy backend — only as a PURE UNDO.

    THERE IS NO PROVIDER SUBSET, and this is the more dangerous direction
    of the two: verifying only the named providers and then flipping the
    global fact would discard every unnamed provider's advanced JSON state
    without ever looking at it.

    Refuses (``RollbackRefusedError``) as soon as ANY JSON document
    differs from its legacy file, because that means authoritative state
    was written after the cutover and switching back would silently
    discard it. Legacy files are never deleted, so the pure-undo case is
    exactly the cutover's own output.
    """
    state_dir = Path(state_dir)
    roster: Sequence[str] = DEFAULT_PROVIDERS

    previous = read_authority(state_dir)
    if not previous.is_json:
        return BackendSwitch(
            previous, previous,
            AuthoritativeStateStore(state_dir).load_all(roster),
            changed=False,
            prepared={provider: False for provider in roster},
        )

    legacy_states: Dict[str, ProviderState] = {}
    json_store = JsonStateStore(state_dir)
    for provider in roster:
        legacy = FileStateStore(state_dir).load(provider)
        current = json_store.load(provider)   # loud on missing/corrupt
        if current != legacy:
            raise RollbackRefusedError(
                f"{provider}: the JSON document has diverged from the legacy "
                "slot files, so switching back would discard authoritative "
                "state. Rollback is only allowed as a pure undo of the "
                "cutover; recover the intended state first."
            )
        legacy_states[provider] = legacy

    _checkpoint(checkpoint, STEP_BEFORE_FLIP)
    write_authority(state_dir, previous.successor(BACKEND_LEGACY))
    _checkpoint(checkpoint, STEP_AFTER_FLIP)

    confirmed = read_authority(state_dir)
    if confirmed.is_json:
        raise StateStoreError(
            f"rollback published the authority fact but re-reading it yielded "
            f"{confirmed}; refusing to report success"
        )
    _checkpoint(checkpoint, STEP_AFTER_CONFIRM)
    return BackendSwitch(
        previous, confirmed, legacy_states, changed=True,
        prepared={provider: False for provider in roster},
    )
