"""File-backed provider state store.

This is the Phase 1 strangler seam: business code starts expressing state
as one logical ``ProviderState`` with an explicit load/commit boundary,
while the durable layout on disk stays exactly the shell's per-provider
files. Nothing here is wired into scheduler decision paths yet.

Contract (see ARCHITECTURE.md):

* ``load`` is a pure read: it never creates the state dir, never migrates,
  never repairs. Unparsable values read as ``None`` — identical to the
  shell getters rejecting a bad value, so a store load can never diverge
  from what the shell would see.
* ``commit`` is the only side effect: it first re-loads and compares
  against ``old_state`` (optimistic concurrency — a stale caller can only
  fail loudly, never clobber), then publishes exactly the changed slots.
* Commit order is canonical and keeps ``next_due_at`` last. Combined with
  the persistence invariant this means a crash mid-commit can only leave
  the provider looking *due again* (retry, or an unfollowed-through
  deadline move), never *done-but-skipped*: at-least-once is directional.
* Only transient slots (``reset_candidate``, ``reset_anchor``) may be
  deleted by a commit; clearing any other slot is a programmer error.

File name suffixes mirror the shell's ``provider_*_file()`` helpers one to
one; tests/state-store-parity-regression.zsh enforces that agreement
against the real shell getters.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .models import ProviderState, ResetCandidate

PROVIDER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
EPOCH_RE = re.compile(r"^[0-9]+$")

# (attribute, filename suffix, codec) in canonical COMMIT ORDER.
# next_due_at stays last: until it is published, the provider still looks
# due on its old deadline, so any crash prefix re-runs instead of losing.
SLOTS: Tuple[Tuple[str, str, str], ...] = (
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
    """Raised by commit paths for programmer/concurrency errors."""


class StaleStateError(StateStoreError):
    """Disk state moved underneath the caller; the commit was refused."""


def _decode_epoch(raw: Optional[str]) -> Optional[int]:
    if raw is None or not EPOCH_RE.match(raw):
        return None
    return int(raw)


def _decode_pending(raw: Optional[str]) -> bool:
    return raw == "1"


def _decode_compound(raw: Optional[str]) -> Optional[ResetCandidate]:
    # Writers always produce exactly "reset:observed" with numeric halves.
    # Anything else is treated like the shell's sync layer treats it: no
    # usable candidate.
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


def _encode(codec: str, value: object) -> str:
    if codec == "epoch":
        assert isinstance(value, int) and value >= 0, value
        return str(value)
    if codec == "pending":
        assert isinstance(value, bool), value
        return "1" if value else "0"
    if codec == "compound":
        assert isinstance(value, ResetCandidate), value
        return value.as_compound()
    if codec == "raw":
        assert isinstance(value, str) and value and "\n" not in value, value
        return value
    raise AssertionError(f"unknown codec {codec}")


def _publish_atomic(path: Path, text: str) -> None:
    """Complete-then-rename: readers see old or new value, never a mix."""
    directory = path.parent
    temp = directory / f"{path.name}.tmp.{os.getpid()}.{os.urandom(4).hex()}"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write((text + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


class ProviderStateStore:
    """Abstract load/commit boundary. load() must be pure; commit() is the
    only persistence side effect."""

    def load(self, provider: str) -> ProviderState:
        raise NotImplementedError

    def commit(
        self,
        provider: str,
        old_state: ProviderState,
        new_state: ProviderState,
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
        if not PROVIDER_NAME_RE.match(provider):
            raise ValueError(f"invalid provider name: {provider!r}")
        return self.state_dir / f"{provider}-{suffix}"

    @staticmethod
    def _read_raw(path: Path) -> Optional[str]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = handle.read()
        except OSError:
            return None
        return value.strip()

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
    ) -> None:
        current = self.load(provider)
        if current != old_state:
            raise StaleStateError(
                f"{provider}: disk state changed since load; commit refused"
            )

        for attribute, suffix, codec in SLOTS:
            old_value = getattr(old_state, attribute)
            new_value = getattr(new_state, attribute)
            if old_value == new_value:
                continue
            path = self._path(provider, suffix)
            if new_value is None:
                if attribute not in DELETABLE_SLOTS:
                    raise StateStoreError(
                        f"{provider}: slot {attribute} is never unsettable by "
                        "a transition (clearing it would be a lost-debt path)"
                    )
                path.unlink(missing_ok=True)
            else:
                _publish_atomic(path, _encode(codec, new_value))
            self._note_slot_committed(attribute)

    def _note_slot_committed(self, attribute: str) -> None:
        if self.on_slot_committed is not None:
            self.on_slot_committed(attribute)

    def published_slots(self) -> List[str]:
        return [attribute for attribute, _, _ in SLOTS]
