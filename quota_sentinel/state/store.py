"""File-backed provider state store.

This is the Phase 1 strangler seam: business code starts expressing state
as one logical ``ProviderState`` with an explicit load/commit boundary,
while the durable layout on disk stays exactly the shell's per-provider
files. Nothing here is wired into scheduler decision paths yet.

Contract (see ARCHITECTURE.md):

* ``load`` is a pure read: it never creates the state dir, never migrates,
  never repairs. A missing, unparsable, or corrupt-text slot reads as
  ``None`` and NEVER poisons the other slots.

* ``commit`` is the only side effect. It is plan-then-execute: the stale
  check, the mutation-plan build (provider-name validation, legality of
  every requested change, and SERIALIZATION of every value to its final
  UTF-8 bytes) all complete BEFORE the first filesystem mutation. A
  validation, encoding or text-encoding error therefore leaves the disk
  byte-for-byte untouched; the publish stage can only fail on filesystem
  errors, never on business values.

* The ``old_state`` comparison is defensive misuse detection only. It is
  NOT a compare-and-swap and NOT cross-process serialization: a writer
  that slips in between the check and the publishes will be overwritten.
  Any production authoritative writer using this class MUST already
  execute inside the shell scheduler's ``run.lock`` serialization
  boundary (see ARCHITECTURE.md). This class provides no locking of its
  own and must not be advertised as if it did.

* The canonical publish order mirrors ``commit_provider_success`` and
  keeps ``next_due_at`` last, so every crash prefix of a SUCCESS
  transition leaves the provider looking due-again rather than done
  (at-least-once, directional). That proof currently covers the success
  transition only; other transitions (generation init, far-reset
  promotion, debt creation/repayment, candidate lifecycle) must each be
  reviewed and crash-tested individually before being moved behind
  ``commit``.

* ``state_dir`` ownership belongs to the store's WRITE path: ``commit``
  ensures the directory exists with mode 0700 before its first publish
  (matching the shell writers); ``load`` never creates anything. An
  empty commit (no changes) publishes nothing and creates nothing.

Value parity with the shell getters holds for every value the project's
writers can produce. Deliberate divergences on pathological,
never-written-by-writers content (a multi-colon ``reset_candidate`` the
shell would split first:last, and zero-padded epochs the shell echoes
raw while this store canonicalizes to their integer value) are pinned
and enumerated by tests/state-store-parity-regression.zsh rather than
left as unknown drift.

File name suffixes mirror the shell's ``provider_*_file()`` helpers one
to one.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional, Sequence

from .models import ProviderState, ResetCandidate

PROVIDER_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
EPOCH_RE = re.compile(r"[0-9]+")

# (attribute, filename suffix, codec) in canonical COMMIT ORDER.
# This order mirrors commit_provider_success (attempt, task, pending,
# candidate/anchor clears, ... deadline last): until next_due_at is
# published, the provider still looks due on its old deadline, so a
# crash anywhere in a SUCCESS commit re-runs instead of losing. Other
# transitions have not been proven against this order yet.
SLOTS = (
    ("last_attempt_at", "last-attempt-at", "epoch"),
    ("last_task_at", "last-task-at", "epoch"),
    ("retry_pending", "retry-pending", "pending"),
    ("reset_candidate", "reset-candidate", "compound"),
    ("reset_anchor", "reset-anchor", "epoch"),
    ("last_known_reset", "last-known-reset-at", "epoch"),
    ("last_triggered_window", "last-triggered-window", "raw"),
    ("next_due_at", "next-due-at", "epoch"),
)

DELETABLE_SLOTS = frozenset({"reset_candidate", "reset_anchor"})


class StateStoreError(RuntimeError):
    """Raised for refused or failed commits (bad input, illegal change,
    or a filesystem error at the persistence boundary)."""


class StaleStateError(StateStoreError):
    """The disk state already differs from old_state when commit started.

    Detection is defensive only: passing this check does not make the
    commit safe against a concurrent writer (see module docstring).
    """


class _Mutation(NamedTuple):
    """One planned filesystem change, fully validated AND serialized to
    its final persistable bytes before any mutation begins."""

    attribute: str
    path: Path
    operation: str          # "write" | "delete"
    payload: Optional[bytes]  # final on-disk bytes for "write"; None for "delete"


def _decode_epoch(raw: Optional[str]) -> Optional[int]:
    if raw is None or not EPOCH_RE.fullmatch(raw):
        return None
    return int(raw)


def _decode_pending(raw: Optional[str]) -> bool:
    # Mirrors the shell: only the exact literal "1" means pending.
    return raw == "1"


def _decode_compound(raw: Optional[str]) -> Optional[ResetCandidate]:
    # Accepts exactly the canonical "reset:observed" emitted by the
    # writers. The shell reader slices first:last of any colon-bearing
    # string ("5:6:7" → 5:7); that path is unreachable by project
    # writers, and being strict here is a deliberate, test-registered
    # divergence, not a parity gap to discover later.
    if raw is None:
        return None
    parts = raw.split(":")
    if len(parts) != 2:
        return None
    reset_at = _decode_epoch(parts[0])
    observed_at = _decode_epoch(parts[1])
    if reset_at is None or observed_at is None:
        return None
    return ResetCandidate(reset_at, observed_at)


def _decode_raw(raw: Optional[str]) -> Optional[str]:
    return raw if raw else None


_DECODERS = {
    "epoch": _decode_epoch,
    "pending": _decode_pending,
    "compound": _decode_compound,
    "raw": _decode_raw,
}


def _is_plain_int(value: object) -> bool:
    # type() (not isinstance) so bool cannot masquerade as int.
    return type(value) is int


def _encode(codec: str, value: object) -> str:
    """Validate-and-encode for persistence. Explicit failures only —
    never assert (python -O must not weaken the contract)."""
    if codec == "epoch":
        if not _is_plain_int(value) or value < 0:
            raise StateStoreError(
                f"epoch slot requires a plain non-negative int, got {value!r}"
            )
        return str(value)
    if codec == "pending":
        if type(value) is not bool:
            raise StateStoreError(
                f"retry_pending requires a bool, got {value!r}"
            )
        return "1" if value else "0"
    if codec == "compound":
        if not isinstance(value, ResetCandidate):
            raise StateStoreError(
                f"reset_candidate requires a ResetCandidate, got {value!r}"
            )
        if not _is_plain_int(value.reset_at) or value.reset_at < 0:
            raise StateStoreError(
                f"reset_candidate.reset_at requires a plain non-negative "
                f"int, got {value.reset_at!r}"
            )
        if not _is_plain_int(value.observed_at) or value.observed_at < 0:
            raise StateStoreError(
                f"reset_candidate.observed_at requires a plain non-negative "
                f"int, got {value.observed_at!r}"
            )
        return value.as_compound()
    if codec == "raw":
        if not isinstance(value, str) or not value:
            raise StateStoreError(
                f"raw slot requires a non-empty str, got {value!r}"
            )
        if "\n" in value or "\r" in value:
            raise StateStoreError(
                f"raw slot cannot round-trip line breaks: {value!r}"
            )
        return value
    raise StateStoreError(f"unknown codec {codec!r}")


def _serialize_for_slot(
    provider: str, attribute: str, codec: str, value: object
) -> bytes:
    """validate → text → FINAL UTF-8 bytes, in the plan stage.

    Everything the persistence write can choke on (type/range legality,
    representability, text-level and byte-level encoding) fails HERE, so
    no value-level failure can surface once filesystem mutations have
    begun. Errors carry provider and slot context.
    """
    try:
        text = _encode(codec, value)
    except StateStoreError as exc:
        raise StateStoreError(f"{provider}: slot {attribute}: {exc}") from exc
    try:
        return (text + "\n").encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise StateStoreError(
            f"{provider}: slot {attribute}: value is not UTF-8 representable "
            f"and cannot be persisted: {exc}"
        ) from exc


def _publish_atomic(path: Path, payload: bytes) -> None:
    """Publish ALREADY-ENCODED bytes: complete-then-rename, so readers see
    old or new value, never a mix. No value-level serialization or
    encoding may happen here — the bytes are final by contract.

    Temp names come from tempfile.mkstemp (kernel-globally unique): a
    pid+random scheme is only probabilistically safe, and mkstemp also
    fixes creation mode at 0600.
    """
    directory = path.parent
    fd, temp_name = tempfile.mkstemp(
        dir=str(directory), prefix=f"{path.name}.tmp.", suffix=""
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        # Clean up our temp file, but let the original error propagate.
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


class ProviderStateStore:
    """Abstract load/commit boundary.

    load() must be a pure read. commit() is the only persistence side
    effect and must complete all validation before its first mutation.
    Concurrency control is NOT provided here: production writers must
    serialize externally (shell: run.lock).
    """

    def load(self, provider: str) -> ProviderState:
        raise NotImplementedError

    def commit(
        self,
        provider: str,
        old_state: ProviderState,
        new_state: ProviderState,
        always_publish: Sequence[str] = (),
    ) -> None:
        raise NotImplementedError


class FileStateStore(ProviderStateStore):
    """ProviderStateStore over the shell's per-provider file layout."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        # Test-only seam: called after each slot is published, enabling
        # crash-injection at every commit prefix. Production leaves it None.
        self.on_slot_committed: Optional[Callable[[str], None]] = None

    def _path(self, provider: str, suffix: str) -> Path:
        if not provider or not PROVIDER_NAME_RE.fullmatch(provider):
            raise ValueError(f"invalid provider name: {provider!r}")
        return self.state_dir / f"{provider}-{suffix}"

    @staticmethod
    def _read_raw(path: Path) -> Optional[str]:
        """Read one slot, degrading ANY unreadable/corrupt slot to None.

        newline="" is REQUIRED: Python's default universal-newline mode
        would silently translate b"42\\r\\n" to "42\\n" and accept a value
        the shell (which sees raw bytes and rejects the CR) unsets. The
        store must see exactly the bytes, then strip only trailing
        newlines like the shell's $(<file) does.
        """
        try:
            with open(path, "r", encoding="utf-8", newline="") as handle:
                value = handle.read()
        except (OSError, UnicodeError):
            return None
        return value.rstrip("\n")

    def load(self, provider: str) -> ProviderState:
        fields = {}
        for attribute, suffix, codec in SLOTS:
            raw = self._read_raw(self._path(provider, suffix))
            fields[attribute] = _DECODERS[codec](raw)
        return ProviderState(**fields)

    def commit(
        self,
        provider: str,
        old_state: ProviderState,
        new_state: ProviderState,
        always_publish: Sequence[str] = (),
    ) -> None:
        # ---- phase 1: detect already-stale input (defensive only) ----
        current = self.load(provider)
        if current != old_state:
            raise StaleStateError(
                f"{provider}: disk state changed since load; commit refused"
            )

        # ---- phase 2: build + validate the ENTIRE mutation plan -------
        # No filesystem mutation may happen in this phase.
        #
        # ``always_publish`` names slots a transition must MATERIALIZE even
        # when the value is unchanged. It exists because "the file is absent"
        # and "the file says 0" are the same business state but not the same
        # on-disk state: the shell's success commit always left an explicit
        # retry_pending=0 behind, and silently stopping that would change a
        # durable contract that operators and regressions both read.
        forced = set(always_publish)
        unknown = sorted(forced - {attribute for attribute, _, _ in SLOTS})
        if unknown:
            raise StateStoreError(
                f"{provider}: cannot force-publish unknown slots {unknown}"
            )
        plan: List[_Mutation] = []
        for attribute, suffix, codec in SLOTS:
            old_value = getattr(old_state, attribute)
            new_value = getattr(new_state, attribute)
            if old_value == new_value and attribute not in forced:
                continue
            path = self._path(provider, suffix)  # validates name; pure
            if new_value is None:
                if attribute not in DELETABLE_SLOTS:
                    raise StateStoreError(
                        f"{provider}: slot {attribute} is never unsettable "
                        "by a transition (clearing it would be a lost-debt "
                        "path)"
                    )
                plan.append(_Mutation(attribute, path, "delete", None))
            else:
                # Full serialization to final bytes, HERE: once phase 3
                # begins, no value-level failure is possible anymore.
                payload = _serialize_for_slot(provider, attribute, codec, new_value)
                plan.append(_Mutation(attribute, path, "write", payload))

        if not plan:
            # An empty transition publishes nothing and creates nothing
            # (state_dir is NOT ensured for a no-op commit).
            return

        # ---- phase 3: persistence-boundary ownership + mutations -----
        self._ensure_state_dir()
        for mutation in plan:
            try:
                if mutation.operation == "delete":
                    mutation.path.unlink(missing_ok=True)
                elif mutation.payload is not None:
                    # Plan invariant: writes carry their final encoded
                    # bytes; nothing here can fail on business values.
                    _publish_atomic(mutation.path, mutation.payload)
                else:
                    raise StateStoreError(
                        f"{provider}: internal plan invariant violated: "
                        f"write mutation for {mutation.attribute} has no payload"
                    )
            except OSError as exc:
                raise StateStoreError(
                    f"{provider}: failed to {mutation.operation} slot "
                    f"{mutation.attribute} at {mutation.path}: {exc}"
                ) from exc
            self._note_slot_committed(mutation.attribute)

    def _ensure_state_dir(self) -> None:
        """Enforce the persistence-directory invariant (exists, mode 0700)
        before the first publish — matching the shell writers. Write-path
        only: load never calls this."""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.state_dir, 0o700)
        except OSError as exc:
            raise StateStoreError(
                f"cannot prepare state dir {self.state_dir}: {exc}"
            ) from exc

    def _note_slot_committed(self, attribute: str) -> None:
        if self.on_slot_committed is not None:
            self.on_slot_committed(attribute)

    def published_slots(self) -> List[str]:
        return [attribute for attribute, _, _ in SLOTS]
