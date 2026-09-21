"""Durable backend-authority protocol (Phase 3B).

THE PROBLEM
-----------
Phases 1-2 left two durable representations of the same scheduler state:
the shell's per-slot files (legacy, authoritative) and the v1 JSON
documents (shadow). Moving ownership to JSON is easy; the hard part is
the question every process must answer after a crash or a restart:

    which backend is authoritative RIGHT NOW?

Inventing that answer per call site is how a system ends up with a
reader that believes JSON and a writer that believes legacy. This module
therefore defines exactly ONE durable fact and forbids every other
signal.

THE FACT
--------
``<state_dir>/backend-authority.json`` — a single versioned document:

    {"backend": "json", "epoch": 1, "schema_version": 1}

Properties, each one deliberate:

* ATOMIC. Published through the same temp-write/fsync/rename primitive
  as every provider document, so a reader observes the complete old
  document or the complete new one, never a torn mix.

* VERSIONED. ``schema_version`` is validated strictly; an unknown version
  is a loud error, never a guess. ``epoch`` increments by one on every
  durable ownership switch and exists so a reader can tell "the same
  authority" from "a different authority that happens to look similar"
  without comparing state payloads.

* UNIQUE. Nothing else decides ownership. Not an in-memory flag, not a
  process-local cache, not a mtime, not "does the JSON exist", not "we
  probably cut over already". See ``read_authority``.

* RECOVERABLE. The document is self-contained, has no dependency on any
  provider document, and its absence has exactly one meaning: this
  deployment has never been cut over, so legacy is authoritative. That
  is a BOOTSTRAP default with a mechanical rule ("absent manifest"),
  not an inference from state content.

* NOT PROVIDER STATE. Different file name, different schema, different
  key set. No provider name can produce it: provider documents are
  ``<provider>-state.json`` and provider names are validated against
  ``[a-z0-9][a-z0-9_-]*``, so ``backend-authority.json`` is unreachable
  as a provider document and vice versa.

FAILURE POLICY
--------------
A present-but-unreadable/invalid manifest is NEVER defaulted. Ownership
that cannot be determined mechanically must stop the process, not pick a
side: defaulting to legacy while JSON is authoritative would let an old
writer produce a second, diverging source of truth. Mutations fail
closed; readers fail loudly.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .store import StateStoreError, _publish_atomic

AUTHORITY_FILENAME = "backend-authority.json"
AUTHORITY_SCHEMA_VERSION = 1

BACKEND_LEGACY = "legacy"
BACKEND_JSON = "json"
SUPPORTED_BACKENDS = (BACKEND_LEGACY, BACKEND_JSON)

# The bootstrap authority of a deployment that has never been cut over.
# Spelled out as a constructor (not a magic literal sprinkled around) so
# "absent manifest" has exactly one representation.
BOOTSTRAP_EPOCH = 0


class AuthorityError(StateStoreError):
    """Base class for authority-protocol failures."""


class AuthorityCorruptError(AuthorityError):
    """The manifest bytes are unreadable, not UTF-8, or not JSON."""


class AuthoritySchemaError(AuthorityError):
    """The manifest parses but violates the versioned schemas."""


class ConcurrentAuthorityChangeError(AuthorityError):
    """Authority changed while a single logical operation was in flight.

    Raised by the router's generation-guarded read/write wrappers. It
    means another process completed a cutover/rollback concurrently, so
    the value this call was about to return (or just wrote) may belong to
    the wrong backend. Loud by design: the alternative is a silent
    mixed-backend read.
    """


@dataclass(frozen=True)
class BackendAuthority:
    """The single durable ownership fact."""

    backend: str
    epoch: int

    def __post_init__(self) -> None:
        if self.backend not in SUPPORTED_BACKENDS:
            raise AuthoritySchemaError(
                f"unsupported authoritative backend {self.backend!r}; "
                f"this build understands {list(SUPPORTED_BACKENDS)}"
            )
        if type(self.epoch) is not int or self.epoch < 0:
            raise AuthoritySchemaError(
                f"authority epoch must be a plain non-negative int, "
                f"got {self.epoch!r}"
            )

    @property
    def is_json(self) -> bool:
        return self.backend == BACKEND_JSON

    def as_document(self) -> Dict[str, Any]:
        return {
            "schema_version": AUTHORITY_SCHEMA_VERSION,
            "backend": self.backend,
            "epoch": self.epoch,
        }

    def successor(self, backend: str) -> "BackendAuthority":
        """The authority produced by one durable ownership switch."""
        return BackendAuthority(backend, self.epoch + 1)


def bootstrap_authority() -> BackendAuthority:
    """Authority of a state dir whose manifest has never been written."""
    return BackendAuthority(BACKEND_LEGACY, BOOTSTRAP_EPOCH)


def authority_path(state_dir: Path) -> Path:
    return Path(state_dir) / AUTHORITY_FILENAME


def serialize_authority(authority: BackendAuthority) -> bytes:
    """Authority → FINAL document bytes (deterministic, preflighted).

    Encoding happens here, before any caller may touch the filesystem, so
    a publish can only fail on filesystem errors.
    """
    text = json.dumps(
        authority.as_document(), ensure_ascii=False, sort_keys=True, indent=2
    )
    try:
        return (text + "\n").encode("utf-8", errors="strict")
    except UnicodeError as exc:  # unreachable: backend/epoch are ASCII
        raise AuthorityError(
            f"authority document is not UTF-8 representable: {exc}"
        ) from exc


def parse_authority(raw: bytes) -> BackendAuthority:
    """FINAL document bytes → validated authority. Strict by policy."""
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise AuthorityCorruptError(
            f"authority manifest is not valid UTF-8: {exc}"
        ) from exc
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise AuthorityCorruptError(
            f"authority manifest is not valid JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise AuthoritySchemaError(
            f"authority manifest must be a JSON object, "
            f"got {type(document).__name__}"
        )
    expected = {"schema_version", "backend", "epoch"}
    keys = set(document)
    missing = sorted(expected - keys)
    if missing:
        raise AuthoritySchemaError(
            f"authority manifest is missing required keys {missing}"
        )
    unknown = sorted(keys - expected)
    if unknown:
        raise AuthoritySchemaError(
            f"authority manifest has unknown keys {unknown}; schema drift "
            "must fail loudly"
        )
    version = document["schema_version"]
    if type(version) is not int or version != AUTHORITY_SCHEMA_VERSION:
        raise AuthoritySchemaError(
            f"unsupported authority schema_version {version!r}; this build "
            f"understands exactly {AUTHORITY_SCHEMA_VERSION} — refusing to guess"
        )
    backend = document["backend"]
    if not isinstance(backend, str):
        raise AuthoritySchemaError(
            f"authority backend must be a string, got {type(backend).__name__}"
        )
    return BackendAuthority(backend, document["epoch"])


def read_authority(state_dir: Path) -> BackendAuthority:
    """The one and only ownership decision.

    * absent manifest  -> bootstrap authority (legacy)
    * valid manifest   -> that authority
    * damaged manifest -> loud AuthorityError, never a default

    Pure: creates nothing, repairs nothing, chmods nothing. Callers that
    need a stable answer across a following operation must re-read and
    compare (see ``AuthoritativeStateStore``); this function deliberately
    offers no caching hook.
    """
    path = authority_path(state_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return bootstrap_authority()
    except IsADirectoryError as exc:
        raise AuthorityCorruptError(
            f"authority manifest path is a directory: {exc}"
        ) from exc
    except OSError as exc:
        raise AuthorityCorruptError(
            f"authority manifest unreadable: {exc}"
        ) from exc
    return parse_authority(raw)


def write_authority(state_dir: Path, authority: BackendAuthority) -> None:
    """Durably publish the ownership fact in ONE atomic step.

    This is the cutover/rollback commit point: before it returns, every
    process sees the previous authority; after it returns, every process
    sees the new one.
    """
    directory = Path(state_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise AuthorityError(
            f"cannot prepare state dir {directory}: {exc}"
        ) from exc
    payload = serialize_authority(authority)
    try:
        _publish_atomic(authority_path(directory), payload)
    except OSError as exc:
        raise AuthorityError(
            f"failed to publish authority manifest in {directory}: {exc}"
        ) from exc


def read_authority_if_present(state_dir: Path) -> Optional[BackendAuthority]:
    """None when the manifest is absent, the authority when it is valid.

    For diagnostics/CLI reporting only. Ownership decisions must use
    ``read_authority`` so the bootstrap default is applied in one place.
    """
    if not authority_path(state_dir).exists():
        return None
    return read_authority(state_dir)
